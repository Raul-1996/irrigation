"""Zones CRUD API — create, read, update, delete, import, next-watering, duration-conflicts."""

import json
import logging
import sqlite3

from flask import Blueprint, jsonify, request

from database import db
from services.audit import audit_log, debug_audit
from services.duration_conflicts import compute_duration_conflicts
from services.next_watering import compute_next_watering
from utils import to_iso_with_tz

logger = logging.getLogger(__name__)

zones_crud_api_bp = Blueprint("zones_crud_api", __name__)


# Fields that drive the zone state machine. They MUST flow through
# services.zones_state.update_zone_state so an audit row is emitted and the
# optimistic-lock / observed-state machinery is exercised. Any CRUD or bulk
# entry point silently strips these to keep audit integrity intact (B1).
_STATE_MACHINE_FIELDS = {
    "state",
    "commanded_state",
    "observed_state",
    "fault_count",
    "last_fault",
}


# Fields that store controller-local "YYYY-MM-DD HH:MM:SS" timestamps and are
# consumed by the browser as ``new Date(...)``. Without an explicit TZ the
# browser parses them as device-local time, so on a device whose TZ differs
# from the controller's the UI timer drifts by the offset (issue #47). We
# serialise the API response with an ISO-8601 + offset suffix while keeping
# the DB storage format unchanged (server-side ``strptime`` callers and the
# existing tests parse the DB string directly via ``db.get_zone()``).
_ZONE_TS_FIELDS = ("planned_end_time", "watering_start_time")


def _zone_ts_to_iso(zone: dict | None) -> dict | None:
    """Return a shallow copy of ``zone`` with timestamp fields ISO-formatted.

    Idempotent: ``to_iso_with_tz`` short-circuits on TZ-aware input.
    """
    if not zone:
        return zone
    out = dict(zone)
    for f in _ZONE_TS_FIELDS:
        if out.get(f):
            out[f] = to_iso_with_tz(out[f])
    return out


# ---- Zone CRUD ----


@zones_crud_api_bp.route("/api/zones")
def api_zones():
    zones = db.get_zones()
    return jsonify([_zone_ts_to_iso(z) for z in zones])


@zones_crud_api_bp.route("/api/zones/<int:zone_id>", methods=["GET", "PUT", "DELETE"])
@audit_log("zone_modify", target_extractor=lambda *a, **kw: f"zone:{kw.get('zone_id', a[0] if a else '?')}")
def api_zone(zone_id):
    if request.method == "GET":
        zone = db.get_zone(zone_id)
        if zone:
            return jsonify(_zone_ts_to_iso(zone))
        return jsonify({"success": False, "message": "Zone not found"}), 404

    elif request.method == "PUT":
        data = request.get_json() or {}
        # Reject state-machine fields in the generic CRUD endpoint — they
        # MUST go through services.zones_state.update_zone_state so audit
        # rows are emitted and the optimistic-lock state machine isn't
        # bypassed.  Reviewer (audit-logging-expansion / C1) flagged this as
        # an audit-evading backdoor.  Returning 400 makes any frontend that
        # accidentally tries this path break loudly instead of silently
        # writing an unaudited transition.
        bad_fields = sorted(set(data.keys()) & _STATE_MACHINE_FIELDS)
        if bad_fields:
            logger.warning(
                "api_zone PUT rejected state-machine field(s) %s for zone %s — "
                "callers must use /api/zones/<id>/start|stop or zones_state.update_zone_state",
                bad_fields,
                zone_id,
            )
            return jsonify(
                {
                    "success": False,
                    "message": f"state-machine fields not allowed via CRUD: {bad_fields}",
                }
            ), 400
        try:
            if "duration" in data:
                d = int(data["duration"])
                if d < 1 or d > 3600:
                    return jsonify({"success": False, "message": "duration must be 1..3600"}), 400
            if "name" in data and (not str(data["name"]).strip()):
                return jsonify({"success": False, "message": "name must be non-empty"}), 400
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in api_zone: %s", e)
            return jsonify({"success": False, "message": "invalid zone payload"}), 400
        try:
            is_csv = (request.headers.get("X-Import-Op") == "csv") or (request.args.get("source") == "csv")
        except (KeyError, TypeError, ValueError) as e:
            logger.debug("Exception in api_zone: %s", e)
            is_csv = False
        if is_csv:
            try:
                logging.getLogger("import_export").info(
                    f"PUT zone from CSV id={zone_id} payload={json.dumps(data, ensure_ascii=False)}"
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                logger.debug("Handled exception in api_zone: %s", e)
        zone = db.update_zone(zone_id, data)
        if zone:
            if is_csv:
                try:
                    logging.getLogger("import_export").info(f"PUT result id={zone_id} OK")
                except (OSError, ValueError) as e:
                    logger.debug("Handled exception in line_128: %s", e)
            db.add_log("zone_edit", json.dumps({"zone": zone_id, "changes": data}))
            return jsonify(zone)
        if is_csv:
            try:
                logging.getLogger("import_export").info(f"PUT result id={zone_id} NOT_FOUND")
            except (OSError, ValueError) as e:
                logger.debug("Handled exception in line_135: %s", e)
        return ("Zone not found", 404)

    elif request.method == "DELETE":
        if db.delete_zone(zone_id):
            db.add_log("zone_delete", json.dumps({"zone": zone_id}))
            return ("", 204)
        return ("Zone not found", 404)


@zones_crud_api_bp.route("/api/zones", methods=["POST"])
@audit_log("zone_create")
def api_create_zone():
    data = request.get_json() or {}
    try:
        name = str(data.get("name") or "Зона").strip()
        duration = int(data.get("duration") or 10)
        if duration < 1 or duration > 3600:
            return jsonify({"success": False, "message": "duration must be 1..3600"}), 400
        if not name:
            return jsonify({"success": False, "message": "name must be non-empty"}), 400
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Exception in api_create_zone: %s", e)
        return jsonify({"success": False, "message": "invalid zone payload"}), 400
    try:
        is_csv = (request.headers.get("X-Import-Op") == "csv") or (request.args.get("source") == "csv")
    except (KeyError, TypeError, ValueError) as e:
        logger.debug("Exception in api_create_zone: %s", e)
        is_csv = False
    if is_csv:
        try:
            logging.getLogger("import_export").info(
                f"POST create zone from CSV payload={json.dumps(data, ensure_ascii=False)}"
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in api_create_zone: %s", e)
    zone = db.create_zone(data)
    if zone and zone.get("mqtt_server_id") is None:
        # Zone created but no MQTT server assigned — warn caller
        db.add_log(
            "zone_create", json.dumps({"zone": zone["id"], "name": zone["name"], "warning": "mqtt_server_id is NULL"})
        )
        return jsonify(
            {
                "success": True,
                "warning": "MQTT-сервер не выбран. Выберите сервер в настройках зоны для управления реле.",
                "zone": zone,
            }
        ), 201
    if zone:
        db.add_log("zone_create", json.dumps({"zone": zone["id"], "name": zone["name"]}))
        if is_csv:
            try:
                logging.getLogger("import_export").info(f"POST result id={zone.get('id')} OK")
            except (KeyError, TypeError, ValueError) as e:
                logger.debug("Handled exception in api_create_zone: %s", e)
        return jsonify(zone), 201
    if is_csv:
        try:
            logging.getLogger("import_export").info("POST result ERROR")
        except (OSError, ValueError) as e:
            logger.debug("Handled exception in line_181: %s", e)
    return ("Error creating zone", 400)


@zones_crud_api_bp.route("/api/zones/import", methods=["POST"])
@audit_log("zones_import_bulk")
def api_import_zones_bulk():
    """Import/bulk apply zone changes in one transaction."""
    try:
        body = request.get_json(silent=True) or {}
        zones = body.get("zones") or []
        if not isinstance(zones, list) or not zones:
            return jsonify({"success": False, "message": "Нет данных для импорта"}), 400
        # B1 FIX: defence-in-depth — strip state-machine fields from the payload
        # BEFORE handing it to bulk_upsert_zones.  The DB-layer whitelist
        # (db/zones.py::_ALLOWED_UPDATE_COLUMNS) is the primary guard, but
        # filtering here keeps the audit log honest: even if a caller smuggled
        # such fields, they never reach SQL nor the audit context.
        sanitised = []
        for z in zones:
            if not isinstance(z, dict):
                sanitised.append(z)
                continue
            stripped = {k: v for k, v in z.items() if k not in _STATE_MACHINE_FIELDS}
            if len(stripped) != len(z):
                logger.warning(
                    "api_import_zones_bulk: stripped state-machine fields %s from zone payload (id=%s)",
                    sorted(set(z.keys()) & _STATE_MACHINE_FIELDS),
                    z.get("id"),
                )
            sanitised.append(stripped)
        stats = db.bulk_upsert_zones(sanitised)
        try:
            db.add_log("zones_import", json.dumps({"counts": stats}))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in api_import_zones_bulk: %s", e)
        return jsonify({"success": True, **stats})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка импорта зон: {e}")
        return jsonify({"success": False, "message": "Ошибка импорта"}), 500


# ---- Next watering ----


@zones_crud_api_bp.route("/api/zones/<int:zone_id>/next-watering")
def api_zone_next_watering(zone_id):
    """API для получения времени следующего полива зоны"""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({"error": "Зона не найдена"}), 404

        info = compute_next_watering([zone_id]).get(int(zone_id)) or {}
        if not info.get("has_programs"):
            return jsonify(
                {"zone_id": zone_id, "next_watering": "Никогда", "reason": "Зона не включена ни в одну программу"}
            )
        best_dt = info.get("next_dt")
        if best_dt is None:
            return jsonify({"zone_id": zone_id, "next_watering": "Никогда"})
        program = info.get("program") or {}
        return jsonify(
            {
                "zone_id": zone_id,
                "next_watering": best_dt.strftime("%H:%M"),
                "next_datetime": best_dt.strftime("%Y-%m-%d %H:%M"),
                "program_name": program.get("name"),
                "program_time": program.get("time"),
                "zone_position": info.get("zone_position"),
                "total_zones_in_program": info.get("total_zones"),
            }
        )

    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения времени следующего полива для зоны {zone_id}: {e}")
        return jsonify({"error": "Ошибка получения времени полива"}), 500


@zones_crud_api_bp.route("/api/zones/next-watering-bulk", methods=["POST"])
@audit_log("zones_next_watering_bulk", target_extractor=lambda *a, **kw: "zones:bulk")
def api_zones_next_watering_bulk():
    try:
        data = request.get_json(silent=True) or {}
        zone_ids = data.get("zone_ids") or None
        results = compute_next_watering(zone_ids)
        items = []
        for zid, info in results.items():
            best_dt = info.get("next_dt")
            items.append(
                {
                    "zone_id": int(zid),
                    "next_datetime": best_dt.strftime("%Y-%m-%d %H:%M:%S") if best_dt else None,
                    "next_watering": "Никогда" if best_dt is None else best_dt.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        return jsonify({"success": True, "items": items})
    except (ValueError, TypeError, KeyError) as e:
        logger.error(f"bulk next-watering failed: {e}")
        return jsonify({"success": False}), 500


# ---- Duration conflict checks ----


@zones_crud_api_bp.route("/api/zones/check-duration-conflicts", methods=["POST"])
def api_check_zone_duration_conflicts():
    """Check program conflicts when changing a zone's duration."""
    try:
        data = request.get_json() or {}
        zone_id = data.get("zone_id")
        new_duration = data.get("new_duration")

        # Debug-level trace of UI intent — read-only by design, no DB mutation.
        try:
            debug_audit(
                action_type="zones_check_duration_conflicts",
                source="api",
                target=f"zone:{zone_id}" if zone_id is not None else None,
                payload={"zone_id": zone_id, "new_duration": new_duration},
            )
        except Exception:
            logger.debug("check-duration-conflicts: debug_audit failed", exc_info=True)

        if not isinstance(zone_id, int) or not isinstance(new_duration, int):
            return jsonify({"success": False, "message": "Некорректные параметры"}), 400

        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({"success": False, "message": "Зона не найдена"}), 404

        programs = db.get_programs()
        zones_cache = {z["id"]: z for z in db.get_zones()}
        conflicts = compute_duration_conflicts(zone_id, new_duration, programs, zones_cache)

        return jsonify({"success": True, "has_conflicts": len(conflicts) > 0, "conflicts": conflicts})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка проверки конфликтов длительности зоны: {e}")
        return jsonify({"success": False, "message": "Ошибка проверки конфликтов"}), 500


@zones_crud_api_bp.route("/api/zones/check-duration-conflicts-bulk", methods=["POST"])
def api_check_zone_duration_conflicts_bulk():
    """Bulk duration conflict check for multiple zones."""
    try:
        payload = request.get_json() or {}
        changes = payload.get("changes") or []
        normalized = []
        for ch in changes:
            try:
                zid = int(ch.get("zone_id"))
                dur = int(ch.get("new_duration"))
                normalized.append((zid, dur))
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in api_check_zone_duration_conflicts_bulk: %s", e)
                continue

        # Debug-level trace of UI intent — read-only by design.
        try:
            debug_audit(
                action_type="zones_check_duration_conflicts_bulk",
                source="api",
                target="zones:bulk",
                payload={"change_count": len(normalized), "changes_preview": normalized[:10]},
            )
        except Exception:
            logger.debug("check-duration-conflicts-bulk: debug_audit failed", exc_info=True)

        if not normalized:
            return jsonify({"success": False, "message": "Нет валидных изменений"}), 400

        all_programs = db.get_programs()
        zones_cache = {z["id"]: z for z in db.get_zones()}

        results = {}
        for zone_id, new_duration in normalized:
            conflicts = compute_duration_conflicts(zone_id, new_duration, all_programs, zones_cache)
            results[str(zone_id)] = {"has_conflicts": len(conflicts) > 0, "conflicts": conflicts}

        return jsonify({"success": True, "results": results})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка bulk-проверки конфликтов длительности зон: {e}")
        return jsonify({"success": False, "message": "Ошибка проверки конфликтов"}), 500
