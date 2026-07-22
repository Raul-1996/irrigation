"""Zones CRUD API — create, read, update, delete, import, next-watering, duration-conflicts."""

import json
import logging
import sqlite3
from contextlib import ExitStack, contextmanager

from flask import Blueprint, jsonify, request

from database import db
from irrigation_scheduler import get_scheduler
from services import sse_hub as _sse_hub
from services.audit import audit_log, debug_audit
from services.duration_conflicts import compute_duration_conflicts
from services.locks import group_lock, zone_lock
from services.next_watering import NextWateringLimitError, compute_next_watering, normalize_requested_zone_ids
from services.security import user_required
from services.zones_state import compare_and_swap_zone_detailed
from utils import normalize_topic, to_iso_with_tz

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

# Postponement is scheduler-owned state.  It must only be mutated through the
# dedicated postpone endpoint, which applies its time/reason validation and
# audit contract.  Generic CRUD/import must never bypass that path.
_POSTPONE_FIELDS = {"postpone_until", "postpone_reason"}
# These columns are internal activation/generation fences.  They are writable
# by the state machine only; accepting them from generic CRUD/import would let
# a caller invalidate (or impersonate) activation-bound safety callbacks.
_INTERNAL_COMMAND_FIELDS = {"command_id", "sequence_id"}
# Photo paths are filesystem-owned metadata. Only the serialized photo API may
# write them; generic CRUD/import must not bind one zone to another zone's file.
_PHOTO_METADATA_FIELDS = {"photo_path", "photo_thumb"}


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
_MAX_NEXT_WATERING_BODY_BYTES = 64 * 1024


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
    state = str(zone.get("state") or "").strip().lower()
    has_physical_channel = bool(zone.get("mqtt_server_id") or (zone.get("topic") or "").strip())
    if not has_physical_channel:
        # A complete virtual zone has no relay that could remain energised and
        # therefore can never acquire a physical observed_state echo.  Its
        # logical OFF state is the complete safety boundary.
        return state == "off"
    commanded = str(zone.get("commanded_state") or "").strip().lower()
    observed = str(zone.get("observed_state") or "").strip().lower()
    return state == "off" and commanded == "off" and observed == "off"


def _zone_topology_changed(zone: dict, payload: dict) -> bool:
    if "topic" in payload and (payload.get("topic") or "").strip() != (zone.get("topic") or "").strip():
        return True
    if "mqtt_server_id" in payload and payload.get("mqtt_server_id") != zone.get("mqtt_server_id"):
        return True
    return "group_id" in payload and int(payload["group_id"]) != int(zone.get("group_id") or 0)


@contextmanager
def _stable_zone_topology_lock(zone_id: int, candidate_group_ids: set[int]):
    """Lock the zone plus every group it occupied during lock acquisition.

    A preflight row can become stale before its old-group lock is entered.  A
    concurrent move to a third group would otherwise let this request mutate or
    delete the zone without serialising against that group's active sequence.
    Accumulate newly observed groups and reacquire in canonical group→zone order
    until the protected row belongs to a group already held by this request.
    """
    locked_group_ids = {int(group_id) for group_id in candidate_group_ids if int(group_id) > 0}
    while True:
        locks = ExitStack()
        try:
            for group_id in sorted(locked_group_ids):
                locks.enter_context(group_lock(group_id))
            locks.enter_context(zone_lock(int(zone_id)))
            latest = db.get_zone(int(zone_id))
            latest_group_id = int((latest or {}).get("group_id") or 0)
            if latest_group_id and latest_group_id not in locked_group_ids:
                locked_group_ids.add(latest_group_id)
                continue
            yield latest
            return
        finally:
            locks.close()


def _canonical_actuator_topic(value, *, allow_empty: bool) -> tuple[str | None, str | None]:
    """Return one safe report/base topic; command channels are never topology."""
    if value is not None and not isinstance(value, str):
        return None, "topic must be a string"
    raw = str(value or "").strip()
    if not raw:
        return ("", None) if allow_empty else (None, "topic must be non-empty")
    if "\x00" in raw or "+" in raw or "#" in raw:
        return None, "topic must not contain NUL or MQTT wildcards"
    collapsed = "/" + raw.lstrip("/")
    if collapsed == "/" or collapsed.endswith("/on"):
        return None, "topic must be a non-root MQTT report topic, not an /on command topic"
    try:
        if len(collapsed.encode("utf-8")) > 65_535:
            return None, "topic is too long"
    except UnicodeEncodeError:
        return None, "topic must be valid UTF-8"
    canonical = normalize_topic(collapsed)
    if not canonical:
        return None, "topic is not a safe MQTT report topic"
    return canonical, None


def _normalise_zone_payload(
    payload: dict,
    *,
    group_ids: set[int],
    mqtt_server_ids: set[int],
    strict_duration: bool = False,
    current: dict | None = None,
) -> tuple[dict | None, str | None]:
    """Validate references and canonicalise values before any DB mutation."""
    data = dict(payload)

    forbidden_commands = sorted(set(data) & _INTERNAL_COMMAND_FIELDS)
    if forbidden_commands:
        return None, f"internal command fields not allowed via zone CRUD: {forbidden_commands}"

    forbidden_postpone = sorted(set(data) & _POSTPONE_FIELDS)
    if forbidden_postpone:
        return None, f"postpone fields not allowed via zone CRUD: {forbidden_postpone}"

    forbidden_photo_metadata = sorted(set(data) & _PHOTO_METADATA_FIELDS)
    if forbidden_photo_metadata:
        return None, f"photo metadata fields not allowed via zone CRUD: {forbidden_photo_metadata}"

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

    topology_touched = "topic" in data or "mqtt_server_id" in data
    if "topic" in data:
        canonical_topic, topic_error = _canonical_actuator_topic(data.get("topic"), allow_empty=True)
        if topic_error:
            return None, topic_error
        data["topic"] = canonical_topic

    # A newly written physical channel must be complete.  Empty+NULL remains a
    # supported virtual zone; topic-only/server-only rows are legacy read
    # compatibility, never a shape public CRUD may create or extend.
    if topology_touched or current is None:
        effective_server_id = data.get(
            "mqtt_server_id",
            current.get("mqtt_server_id") if current is not None else None,
        )
        effective_topic = data.get("topic", current.get("topic") if current is not None else "")
        if effective_server_id is not None and not effective_topic:
            return None, "topic must be non-empty when mqtt_server_id is configured"
        if effective_server_id is None and effective_topic:
            return None, "mqtt_server_id is required when an actuator topic is configured"

    return data, None


def _zone_reference_ids() -> tuple[set[int], set[int]]:
    strict_groups = getattr(db, "get_groups_strict", None)
    groups = strict_groups() if callable(strict_groups) else db.get_groups()
    strict_servers = getattr(db, "get_mqtt_servers_strict", None)
    servers = strict_servers() if callable(strict_servers) else db.get_mqtt_servers()
    group_ids = {int(group["id"]) for group in (groups or [])}
    mqtt_server_ids = {int(server["id"]) for server in (servers or [])}
    return group_ids, mqtt_server_ids


def _zone_version_conflict(expected_version: int, current: dict | None):
    if current is None:
        return jsonify({"success": False, "message": "Zone not found"}), 404
    return jsonify(
        {
            "success": False,
            "message": "Zone version conflict",
            "error_code": "ZONE_VERSION_CONFLICT",
            "expected_version": expected_version,
            "current_version": int(current.get("version") or 0),
        }
    ), 409


def _reconcile_affected_program_schedules(program_ids: object) -> bool:
    """Best-effort post-commit reconciliation for atomic group-999 unlinks.

    Timer callbacks independently revalidate and self-heal stale fingerprints;
    this eager path keeps normal operation current immediately after the DB
    commit.  The write itself is never misreported as rolled back if the live
    scheduler is temporarily unavailable.
    """
    if not program_ids:
        return True
    if not isinstance(program_ids, list) or any(
        type(program_id) is not int or program_id <= 0 for program_id in program_ids
    ):
        logger.error("zone mutation returned invalid affected_program_ids=%r", program_ids)
        return False
    affected = sorted(set(program_ids))
    try:
        scheduler = get_scheduler()
        reconcile = getattr(scheduler, "reconcile_program_from_db", None) if scheduler is not None else None
        if not callable(reconcile):
            logger.error("affected program schedules await self-heal; live reconciler unavailable ids=%s", affected)
            return False
        all_ok = True
        for program_id in affected:
            if reconcile(program_id) is not True:
                all_ok = False
                logger.error("post-commit program schedule reconcile failed program=%s", program_id)
        return all_ok
    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
        logger.exception("post-commit program schedule reconcile failed ids=%s", affected)
        return False


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
        if "expected_version" not in raw_data:
            return jsonify(
                {
                    "success": False,
                    "message": "expected_version is required",
                    "error_code": "EXPECTED_VERSION_REQUIRED",
                }
            ), 428
        expected_version = raw_data.get("expected_version")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            return jsonify(
                {
                    "success": False,
                    "message": "expected_version must be a non-negative integer",
                    "error_code": "INVALID_EXPECTED_VERSION",
                }
            ), 400
        raw_data = {key: value for key, value in raw_data.items() if key != "expected_version"}
        if not raw_data:
            return jsonify({"success": False, "message": "zone update must contain at least one field"}), 400
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
        if int(prev.get("version") or 0) != expected_version:
            return _zone_version_conflict(expected_version, prev)
        try:
            group_ids, mqtt_server_ids = _zone_reference_ids()
            data, validation_error = _normalise_zone_payload(
                raw_data,
                group_ids=group_ids,
                mqtt_server_ids=mqtt_server_ids,
                current=prev,
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
            with _stable_zone_topology_lock(zone_id, group_ids_to_lock) as latest:
                if not latest:
                    return ("Zone not found", 404)
                if int(latest.get("version") or 0) != expected_version:
                    return _zone_version_conflict(expected_version, latest)
                data, validation_error = _normalise_zone_payload(
                    data,
                    group_ids=group_ids,
                    mqtt_server_ids=mqtt_server_ids,
                    current=latest,
                )
                if validation_error:
                    return jsonify({"success": False, "message": validation_error}), 400
                assert data is not None
                if _zone_topology_changed(latest, data) and not _zone_topology_is_safe(latest):
                    return jsonify(
                        {
                            "success": False,
                            "message": "Zone topology cannot change until the relay is confirmed off",
                        }
                    ), 409
                cas_result = compare_and_swap_zone_detailed(
                    zone_id,
                    data,
                    expected_version=expected_version,
                    audit_reason="zone_crud",
                    db=db,
                )
        else:
            cas_result = compare_and_swap_zone_detailed(
                zone_id,
                data,
                expected_version=expected_version,
                audit_reason="zone_crud",
                db=db,
            )
        if cas_result.get("success") is not True:
            if cas_result.get("reason") in {"not_found", "version_conflict"}:
                return _zone_version_conflict(expected_version, cas_result.get("current"))
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "Zone update is temporarily unavailable",
                        "error_code": "ZONE_UPDATE_UNAVAILABLE",
                    }
                ),
                503,
            )

        # The repository builds this enriched read model while its CAS write
        # transaction is still locked. A later writer can commit immediately
        # after that transaction, but can never leak into this response.
        snapshot = cas_result.get("previous")
        current = cas_result.get("current")
        if not isinstance(snapshot, dict) or not isinstance(current, dict):
            logger.error("successful detailed zone CAS returned incomplete snapshots zone=%s", zone_id)
            return jsonify({"success": False, "error_code": "ZONE_UPDATE_RESULT_INVALID"}), 500
        zone = dict(current)
        zone["success"] = True
        _reconcile_affected_program_schedules(cas_result.get("affected_program_ids"))
        if zone:
            if is_csv:
                try:
                    logging.getLogger("import_export").info(f"PUT result id={zone_id} OK")
                except (OSError, ValueError) as e:
                    logger.debug("Handled exception in line_128: %s", e)
            db.add_log("zone_edit", json.dumps({"zone": zone_id, "changes": data}))
            if any(zone.get(f) != snapshot.get(f) for f in _MQTT_WIRING_FIELDS):
                _sse_hub.reload_hub()
            return jsonify(_zone_ts_to_iso(zone))
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
        with _stable_zone_topology_lock(zone_id, {int(prev.get("group_id") or 0)}) as latest:
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
    # Explicit NULL suppresses the repository's legacy convenience auto-select.
    # Public CRUD treats an empty topic as a virtual zone, so it must remain the
    # complete (NULL server, empty topic) topology even when one broker exists.
    data.setdefault("mqtt_server_id", None)
    if int(data["group_id"]) not in group_ids:
        return jsonify({"success": False, "message": "group_id does not reference an existing group"}), 400
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
    if zone:
        _reconcile_affected_program_schedules(zone.get("affected_program_ids"))
        zone = dict(zone)
        zone.pop("affected_program_ids", None)
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
            strict_zones = getattr(db, "get_zones_strict", None)
            all_zones = strict_zones() if callable(strict_zones) else db.get_zones()
            existing_zones = {int(zone["id"]): zone for zone in (all_zones or [])}
        except (ConnectionError, OSError, sqlite3.Error, TypeError, ValueError):
            logger.exception("zone import topology preflight failed")
            return jsonify({"success": False, "message": "Проверка топологии недоступна"}), 409
        # B1 FIX: defence-in-depth — strip state-machine fields from the payload
        # BEFORE handing it to bulk_upsert_zones.  The DB-layer whitelist
        # (db/zones.py::_ALLOWED_UPDATE_COLUMNS) is the primary guard, but
        # filtering here keeps the audit log honest: even if a caller smuggled
        # such fields, they never reach SQL nor the audit context.
        sanitised = []
        seen_zone_ids: set[int] = set()
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
                if raw_zone_id in seen_zone_ids:
                    return jsonify({"success": False, "message": f"zones[{index}].id дублируется в одном импорте"}), 400
                seen_zone_ids.add(raw_zone_id)
            zone_id = stripped.get("id")
            current = existing_zones.get(int(zone_id)) if zone_id is not None else None
            if current is None:
                # Repository create defaults to group 1. Materialise that
                # default before validation so bulk/CSV cannot create an
                # orphan when the system group is unavailable.
                stripped.setdefault("group_id", 1)

            normalised, validation_error = _normalise_zone_payload(
                stripped,
                group_ids=group_ids,
                mqtt_server_ids=mqtt_server_ids,
                strict_duration=True,
                current=current,
            )
            if validation_error:
                return jsonify({"success": False, "message": f"zones[{index}]: {validation_error}"}), 400
            assert normalised is not None

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
        locked_group_ids = set(group_ids)
        while True:
            retry_with_more_groups = False
            with ExitStack() as locks:
                if zone_ids_to_lock:
                    # Group sequences hold group→zone locks.  The group list can
                    # itself become stale before acquisition, so accumulate any
                    # newly observed group and retry before mutating the batch.
                    for group_id in sorted(locked_group_ids):
                        locks.enter_context(group_lock(group_id))
                    for zone_id in zone_ids_to_lock:
                        locks.enter_context(zone_lock(zone_id))

                    latest_by_id = {zone_id: db.get_zone(zone_id) for zone_id in zone_ids_to_lock}
                    newly_observed_groups = {
                        int(current.get("group_id") or 0)
                        for current in latest_by_id.values()
                        if current and int(current.get("group_id") or 0) not in locked_group_ids
                    }
                    newly_observed_groups.discard(0)
                    if newly_observed_groups:
                        locked_group_ids.update(newly_observed_groups)
                        retry_with_more_groups = True
                    else:
                        for index, zone_data in enumerate(sanitised):
                            zone_id = zone_data.get("id")
                            if zone_id is None or not any(field in zone_data for field in _ZONE_TOPOLOGY_FIELDS):
                                continue
                            current = latest_by_id.get(int(zone_id))
                            zone_data, validation_error = _normalise_zone_payload(
                                zone_data,
                                group_ids=group_ids,
                                mqtt_server_ids=mqtt_server_ids,
                                strict_duration=True,
                                current=current,
                            )
                            if validation_error:
                                return (
                                    jsonify({"success": False, "message": f"zones[{index}]: {validation_error}"}),
                                    400,
                                )
                            assert zone_data is not None
                            sanitised[index] = zone_data
                            if (
                                current
                                and _zone_topology_changed(current, zone_data)
                                and not _zone_topology_is_safe(current)
                            ):
                                return jsonify(
                                    {
                                        "success": False,
                                        "message": f"Зона {zone_id}: топологию нельзя менять до подтверждённого OFF",
                                    }
                                ), 409
                if not retry_with_more_groups:
                    stats = db.bulk_upsert_zones(sanitised)
            if retry_with_more_groups:
                continue
            break
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
        _reconcile_affected_program_schedules(stats.get("affected_program_ids"))
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
@user_required
def api_zones_next_watering_bulk():
    try:
        if request.content_length is not None and request.content_length > _MAX_NEXT_WATERING_BODY_BYTES:
            return jsonify({"success": False, "message": "Слишком большой запрос"}), 413
        raw_body = request.get_data(cache=True)
        if len(raw_body) > _MAX_NEXT_WATERING_BODY_BYTES:
            return jsonify({"success": False, "message": "Слишком большой запрос"}), 413
        data = request.get_json(silent=True)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "Тело запроса должно быть объектом"}), 400
        raw_zone_ids = data.get("zone_ids") if "zone_ids" in data else None
        zone_ids = None if raw_zone_ids is None else normalize_requested_zone_ids(raw_zone_ids)
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
    except NextWateringLimitError:
        return jsonify({"success": False, "message": "Слишком много zone_ids"}), 413
    except (ValueError, TypeError, KeyError) as e:
        logger.info("bulk next-watering rejected: %s", e)
        return jsonify({"success": False, "message": "Некорректный список zone_ids"}), 400


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
