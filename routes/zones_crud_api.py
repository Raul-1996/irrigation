"""Zones CRUD API — create, read, update, delete, import, next-watering, duration-conflicts."""

import json
import logging
import sqlite3
from contextlib import ExitStack

from flask import Blueprint, jsonify, request

from database import db
from services import sse_hub as _sse_hub
from services.audit import audit_log, debug_audit
from services.duration_conflicts import compute_duration_conflicts
from services.locks import group_lock, zone_lock
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


# MQTT wiring of a zone: when these change, the SSE hub must resubscribe —
# its topic maps are built once and would otherwise miss the new topic until
# a service restart (relay echoes lost, observed_state stops updating).
_MQTT_WIRING_FIELDS = ("topic", "mqtt_server_id")
_ZONE_TOPOLOGY_FIELDS = ("topic", "mqtt_server_id", "group_id", "group")


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


def _zone_topology_is_safe(zone: dict) -> bool:
    """Whether a zone is confirmed physically OFF and may be rewired/deleted."""
    state = str(zone.get("state") or "off").strip().lower()
    commanded = str(zone.get("commanded_state") or "").strip().lower()
    observed = str(zone.get("observed_state") or "").strip().lower()
    return state == "off" and commanded in ("", "off") and observed in ("", "off")


def _zone_topology_changed(zone: dict, payload: dict) -> bool:
    if "topic" in payload and (payload.get("topic") or "").strip() != (zone.get("topic") or "").strip():
        return True
    if "mqtt_server_id" in payload and payload.get("mqtt_server_id") != zone.get("mqtt_server_id"):
        return True
    return "group_id" in payload and int(payload["group_id"]) != int(zone.get("group_id") or 0)


def _normalise_zone_payload(
    payload: dict,
    *,
    group_ids: set[int],
    mqtt_server_ids: set[int],
    strict_duration: bool = False,
) -> tuple[dict | None, str | None]:
    """Validate references and canonicalise values before any DB mutation."""
    data = dict(payload)

    if "duration" in data:
        raw_duration = data["duration"]
        if strict_duration and (isinstance(raw_duration, bool) or not isinstance(raw_duration, int)):
            return None, "duration must be a canonical integer in range 1..3600"
        try:
            duration = int(raw_duration)
        except (TypeError, ValueError):
            return None, "duration must be an integer in range 1..3600"
        if duration < 1 or duration > 3600:
            return None, "duration must be in range 1..3600"
        data["duration"] = duration

    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            return None, "name must be non-empty"
        data["name"] = name

    if "group" in data and "group_id" not in data:
        data["group_id"] = data.pop("group")
    elif "group" in data:
        data.pop("group")
    if "group_id" in data:
        try:
            if isinstance(data["group_id"], bool):
                raise ValueError
            group_id = int(data["group_id"])
        except (TypeError, ValueError):
            return None, "group_id must be an integer"
        if group_id not in group_ids:
            return None, "group_id does not reference an existing group"
        data["group_id"] = group_id

    if "mqtt_server_id" in data:
        raw_server_id = data.get("mqtt_server_id")
        if raw_server_id in (None, ""):
            data["mqtt_server_id"] = None
        else:
            try:
                if isinstance(raw_server_id, bool):
                    raise ValueError
                server_id = int(raw_server_id)
            except (TypeError, ValueError):
                return None, "mqtt_server_id must be an integer or null"
            if server_id not in mqtt_server_ids:
                return None, "mqtt_server_id does not reference an existing server"
            data["mqtt_server_id"] = server_id

    if "topic" in data:
        if data["topic"] is not None and not isinstance(data["topic"], str):
            return None, "topic must be a string"
        data["topic"] = (data.get("topic") or "").strip()

    return data, None


def _zone_reference_ids() -> tuple[set[int], set[int]]:
    group_ids = {int(group["id"]) for group in (db.get_groups() or [])}
    mqtt_server_ids = {int(server["id"]) for server in (db.get_mqtt_servers() or [])}
    return group_ids, mqtt_server_ids


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
        raw_data = request.get_json() or {}
        if not isinstance(raw_data, dict):
            return jsonify({"success": False, "message": "invalid zone payload"}), 400
        # Reject state-machine fields in the generic CRUD endpoint — they
        # MUST go through services.zones_state.update_zone_state so audit
        # rows are emitted and the optimistic-lock state machine isn't
        # bypassed.  Reviewer (audit-logging-expansion / C1) flagged this as
        # an audit-evading backdoor.  Returning 400 makes any frontend that
        # accidentally tries this path break loudly instead of silently
        # writing an unaudited transition.
        bad_fields = sorted(set(raw_data.keys()) & _STATE_MACHINE_FIELDS)
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
        prev = db.get_zone(zone_id)
        if not prev:
            return ("Zone not found", 404)
        try:
            group_ids, mqtt_server_ids = _zone_reference_ids()
            data, validation_error = _normalise_zone_payload(
                raw_data,
                group_ids=group_ids,
                mqtt_server_ids=mqtt_server_ids,
            )
        except (ConnectionError, OSError, sqlite3.Error, TypeError, ValueError):
            logger.exception("zone topology validation failed for zone %s", zone_id)
            return jsonify({"success": False, "message": "zone topology validation unavailable"}), 409
        if validation_error:
            return jsonify({"success": False, "message": validation_error}), 400
        assert data is not None
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
        if any(field in data for field in _ZONE_TOPOLOGY_FIELDS):
            group_ids_to_lock = {int(prev.get("group_id") or 0)}
            if data.get("group_id") is not None:
                group_ids_to_lock.add(int(data["group_id"]))
            with ExitStack() as locks:
                for group_id in sorted(group_id for group_id in group_ids_to_lock if group_id):
                    locks.enter_context(group_lock(group_id))
                locks.enter_context(zone_lock(zone_id))
                latest = db.get_zone(zone_id)
                if not latest:
                    return ("Zone not found", 404)
                if _zone_topology_changed(latest, data) and not _zone_topology_is_safe(latest):
                    return jsonify(
                        {
                            "success": False,
                            "message": "Zone topology cannot change until the relay is confirmed off",
                        }
                    ), 409
                prev = latest
                zone = db.update_zone(zone_id, data)
        else:
            zone = db.update_zone(zone_id, data)
        if zone:
            if is_csv:
                try:
                    logging.getLogger("import_export").info(f"PUT result id={zone_id} OK")
                except (OSError, ValueError) as e:
                    logger.debug("Handled exception in line_128: %s", e)
            db.add_log("zone_edit", json.dumps({"zone": zone_id, "changes": data}))
            if prev and any(zone.get(f) != prev.get(f) for f in _MQTT_WIRING_FIELDS):
                _sse_hub.reload_hub()
            return jsonify(zone)
        if is_csv:
            try:
                logging.getLogger("import_export").info(f"PUT result id={zone_id} NOT_FOUND")
            except (OSError, ValueError) as e:
                logger.debug("Handled exception in line_135: %s", e)
        return ("Zone not found", 404)

    elif request.method == "DELETE":
        prev = db.get_zone(zone_id)
        if not prev:
            return ("Zone not found", 404)
        with group_lock(int(prev.get("group_id") or 0)), zone_lock(zone_id):
            latest = db.get_zone(zone_id)
            if not latest:
                return ("Zone not found", 404)
            if not _zone_topology_is_safe(latest):
                return jsonify(
                    {
                        "success": False,
                        "message": "Zone cannot be deleted until the relay is confirmed off",
                    }
                ), 409
            prev = latest
            deleted = db.delete_zone(zone_id)
        if deleted:
            db.add_log("zone_delete", json.dumps({"zone": zone_id}))
            if prev and (prev.get("topic") or "").strip() and prev.get("mqtt_server_id"):
                _sse_hub.reload_hub()
            return ("", 204)
        return ("Zone not found", 404)


@zones_crud_api_bp.route("/api/zones", methods=["POST"])
@audit_log("zone_create")
def api_create_zone():
    raw_data = request.get_json() or {}
    if not isinstance(raw_data, dict):
        return jsonify({"success": False, "message": "invalid zone payload"}), 400
    try:
        group_ids, mqtt_server_ids = _zone_reference_ids()
        data, validation_error = _normalise_zone_payload(
            raw_data,
            group_ids=group_ids,
            mqtt_server_ids=mqtt_server_ids,
        )
    except (ConnectionError, OSError, sqlite3.Error, TypeError, ValueError):
        logger.exception("zone create topology validation failed")
        return jsonify({"success": False, "message": "zone topology validation unavailable"}), 409
    if validation_error:
        return jsonify({"success": False, "message": validation_error}), 400
    assert data is not None
    data.setdefault("name", "Зона")
    data.setdefault("duration", 10)
    data.setdefault("group_id", 1)
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
        if (zone.get("topic") or "").strip():
            _sse_hub.reload_hub()
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
        try:
            group_ids, mqtt_server_ids = _zone_reference_ids()
            existing_zones = {int(zone["id"]): zone for zone in (db.get_zones() or [])}
        except (ConnectionError, OSError, sqlite3.Error, TypeError, ValueError):
            logger.exception("zone import topology preflight failed")
            return jsonify({"success": False, "message": "Проверка топологии недоступна"}), 409
        # B1 FIX: defence-in-depth — strip state-machine fields from the payload
        # BEFORE handing it to bulk_upsert_zones.  The DB-layer whitelist
        # (db/zones.py::_ALLOWED_UPDATE_COLUMNS) is the primary guard, but
        # filtering here keeps the audit log honest: even if a caller smuggled
        # such fields, they never reach SQL nor the audit context.
        sanitised = []
        for index, z in enumerate(zones):
            if not isinstance(z, dict):
                return jsonify({"success": False, "message": f"zones[{index}] должен быть объектом"}), 400
            stripped = {k: v for k, v in z.items() if k not in _STATE_MACHINE_FIELDS}
            if len(stripped) != len(z):
                logger.warning(
                    "api_import_zones_bulk: stripped state-machine fields %s from zone payload (id=%s)",
                    sorted(set(z.keys()) & _STATE_MACHINE_FIELDS),
                    z.get("id"),
                )
            if "id" in stripped:
                raw_zone_id = stripped.get("id")
                if isinstance(raw_zone_id, bool) or not isinstance(raw_zone_id, int) or raw_zone_id <= 0:
                    return jsonify(
                        {"success": False, "message": f"zones[{index}].id должен быть положительным целым"}
                    ), 400

            normalised, validation_error = _normalise_zone_payload(
                stripped,
                group_ids=group_ids,
                mqtt_server_ids=mqtt_server_ids,
                strict_duration=True,
            )
            if validation_error:
                return jsonify({"success": False, "message": f"zones[{index}]: {validation_error}"}), 400
            assert normalised is not None

            zone_id = normalised.get("id")
            current = existing_zones.get(int(zone_id)) if zone_id is not None else None
            if current and _zone_topology_changed(current, normalised) and not _zone_topology_is_safe(current):
                return jsonify(
                    {
                        "success": False,
                        "message": f"Зона {zone_id}: топологию нельзя менять до подтверждённого OFF",
                    }
                ), 409
            sanitised.append(normalised)
        zone_ids_to_lock = sorted(
            {
                int(zone["id"])
                for zone in sanitised
                if zone.get("id") is not None and any(field in zone for field in _ZONE_TOPOLOGY_FIELDS)
            }
        )
        with ExitStack() as locks:
            if zone_ids_to_lock:
                # Group sequences hold group→zone locks. Lock every group in a
                # stable order because one import may move zones across groups.
                for group_id in sorted(group_ids):
                    locks.enter_context(group_lock(group_id))
                for zone_id in zone_ids_to_lock:
                    locks.enter_context(zone_lock(zone_id))

                for zone_data in sanitised:
                    zone_id = zone_data.get("id")
                    if zone_id is None or not any(field in zone_data for field in _ZONE_TOPOLOGY_FIELDS):
                        continue
                    current = db.get_zone(int(zone_id))
                    if current and _zone_topology_changed(current, zone_data) and not _zone_topology_is_safe(current):
                        return jsonify(
                            {
                                "success": False,
                                "message": f"Зона {zone_id}: топологию нельзя менять до подтверждённого OFF",
                            }
                        ), 409
            stats = db.bulk_upsert_zones(sanitised)
        if not isinstance(stats, dict):
            logger.error("zone import repository returned an invalid result")
            return jsonify({"success": False, "message": "Импорт зон не выполнен"}), 500
        if stats.get("success") is False or int(stats.get("failed") or 0) > 0:
            logger.warning("zone import rolled back or failed: %s", stats)
            return jsonify(
                {
                    **stats,
                    "success": False,
                    "message": "Импорт зон полностью отменён из-за ошибки",
                }
            ), 409
        try:
            db.add_log("zones_import", json.dumps({"counts": stats}))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in api_import_zones_bulk: %s", e)
        if any(isinstance(z, dict) and any(f in z for f in _MQTT_WIRING_FIELDS) for z in sanitised):
            _sse_hub.reload_hub()
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
