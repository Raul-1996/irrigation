"""Programs API blueprint — all /api/programs* endpoints."""

import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta

from flask import Blueprint, current_app, jsonify, request

from database import db
from db.programs import ProgramZonesNotFoundError
from irrigation_scheduler import get_scheduler
from services.api_rate_limiter import rate_limit
from services.audit import audit_log, debug_audit

logger = logging.getLogger(__name__)

programs_api_bp = Blueprint("programs_api", __name__)

# Допустимые значения v2-полей программ (см. templates/programs.html и
# IrrigationScheduler._schedule_single_time — другие значения молча
# приводят к программе без jobs).
VALID_PROGRAM_TYPES = ("time-based", "smart")
_UNSUPPORTED_PROGRAM_TYPE_ERROR = "program type 'smart' has no implemented execution semantics"
VALID_SCHEDULE_TYPES = ("weekdays", "interval", "even-odd")
MIN_INTERVAL_DAYS = 1
MAX_INTERVAL_DAYS = 30
MAX_EXTRA_TIMES = 24
_TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")
_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}\Z")
_DEFAULT_PROGRAM_COLOR = "#42a5f5"
# Production Hypercorn is configured with one worker.  This lock is the
# application-level transaction boundary joining a committed program row to
# its scheduler reconciliation; every route which mutates either side must
# hold it until both sides have reached a truthful terminal result.
_PROGRAM_MUTATION_LOCK = threading.RLock()
_PROGRAM_WRITE_FIELDS = (
    "name",
    "time",
    "days",
    "zones",
    "type",
    "schedule_type",
    "interval_days",
    "even_odd",
    "color",
    "enabled",
    "extra_times",
)


def _is_hhmm(value: object) -> bool:
    return isinstance(value, str) and _TIME_RE.fullmatch(value) is not None


def _normalize_color(value: object) -> str | None:
    if not isinstance(value, str) or _COLOR_RE.fullmatch(value) is None:
        return None
    return value.lower()


def _missing_program_zones_response(error: ProgramZonesNotFoundError):
    return (
        jsonify(
            {
                "success": False,
                "message": "One or more program zones no longer exist",
                "error_code": "PROGRAM_ZONES_NOT_FOUND",
                "missing_zone_ids": error.missing_zone_ids,
            }
        ),
        409,
    )


def _program_type_unsupported_response():
    return jsonify(
        {
            "success": False,
            "supported": False,
            "error_code": "PROGRAM_TYPE_UNSUPPORTED",
            "message": "Program type 'smart' is not supported",
        }
    ), 422


def _program_validation_error_response(error: str):
    if error == _UNSUPPORTED_PROGRAM_TYPE_ERROR:
        return _program_type_unsupported_response()
    return jsonify({"success": False, "message": error}), 400


def _normalize_program_payload(
    data: dict,
    *,
    current: dict | None = None,
    creating: bool,
) -> tuple[dict, dict, str | None]:
    """Validate strict JSON types and return (changes, effective, error)."""
    if creating:
        missing = [field for field in ("name", "time", "zones") if field not in data]
        if missing:
            return {}, {}, f"Missing required fields: {', '.join(missing)}"
        effective = {
            "type": "time-based",
            "schedule_type": "weekdays",
            "days": [],
            "extra_times": [],
            "enabled": True,
            "color": _DEFAULT_PROGRAM_COLOR,
            **data,
        }
    else:
        effective = {**(current or {}), **data}

    if effective.get("schedule_type") == "even_odd":
        effective["schedule_type"] = "even-odd"

    name = effective.get("name")
    if not isinstance(name, str) or not name.strip():
        return {}, effective, "name must be a non-empty string"
    if not _is_hhmm(effective.get("time")):
        return {}, effective, "time must use strict HH:MM format (00:00-23:59)"

    zones = effective.get("zones")
    if (
        not isinstance(zones, list)
        or not zones
        or any(isinstance(zone_id, bool) or not isinstance(zone_id, int) or zone_id <= 0 for zone_id in zones)
    ):
        return {}, effective, "zones must be a non-empty list of positive integers"
    if len(zones) != len(set(zones)):
        return {}, effective, "zones must not contain duplicate IDs"

    days = effective.get("days", [])
    if not isinstance(days, list) or any(
        isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in days
    ):
        return {}, effective, "days must be a list of integers between 0 and 6"
    effective["days"] = sorted(set(days))

    extra_times = effective.get("extra_times", [])
    if not isinstance(extra_times, list) or any(not _is_hhmm(slot) for slot in extra_times):
        return {}, effective, "extra_times must be a list of strict HH:MM strings"
    if len(extra_times) > MAX_EXTRA_TIMES:
        return {}, effective, f"extra_times must contain at most {MAX_EXTRA_TIMES} entries"
    if len(extra_times) != len(set(extra_times)):
        return {}, effective, "extra_times must not contain duplicates"
    if effective["time"] in extra_times:
        return {}, effective, "extra_times must not duplicate the primary time"
    effective["extra_times"] = list(extra_times)

    enabled = effective.get("enabled", True)
    if not isinstance(enabled, bool):
        return {}, effective, "enabled must be a JSON boolean"

    color = _normalize_color(effective.get("color", _DEFAULT_PROGRAM_COLOR))
    if color is None:
        return {}, effective, "color must use strict #RRGGBB format"
    effective["color"] = color

    program_type = effective.get("type", "time-based")
    if not isinstance(program_type, str) or program_type not in VALID_PROGRAM_TYPES:
        return (
            {},
            effective,
            f"Invalid type: {program_type!r} (expected one of: {', '.join(VALID_PROGRAM_TYPES)})",
        )
    if program_type == "smart":
        return {}, effective, _UNSUPPORTED_PROGRAM_TYPE_ERROR
    schedule_type = effective.get("schedule_type", "weekdays")
    if not isinstance(schedule_type, str) or schedule_type not in VALID_SCHEDULE_TYPES:
        return (
            {},
            effective,
            f"Invalid schedule_type: {schedule_type!r} (expected one of: {', '.join(VALID_SCHEDULE_TYPES)})",
        )
    if schedule_type == "weekdays" and not effective["days"]:
        return {}, effective, "schedule_type='weekdays' requires at least one day"
    if schedule_type == "interval":
        interval_days = effective.get("interval_days")
        if (
            isinstance(interval_days, bool)
            or not isinstance(interval_days, int)
            or not MIN_INTERVAL_DAYS <= interval_days <= MAX_INTERVAL_DAYS
        ):
            return (
                {},
                effective,
                "schedule_type='interval' requires interval_days "
                f"as an integer between {MIN_INTERVAL_DAYS} and {MAX_INTERVAL_DAYS}",
            )
    elif effective.get("interval_days") is not None:
        interval_days = effective["interval_days"]
        if (
            isinstance(interval_days, bool)
            or not isinstance(interval_days, int)
            or not MIN_INTERVAL_DAYS <= interval_days <= MAX_INTERVAL_DAYS
        ):
            return {}, effective, "interval_days must be null or an integer between 1 and 30"
    if schedule_type == "even-odd" and effective.get("even_odd") not in ("even", "odd"):
        return {}, effective, "schedule_type='even-odd' requires even_odd ('even' or 'odd')"
    if effective.get("even_odd") not in (None, "even", "odd"):
        return {}, effective, "even_odd must be null, 'even' or 'odd'"

    normalized = dict(data)
    for field in (
        "name",
        "time",
        "zones",
        "days",
        "extra_times",
        "enabled",
        "type",
        "schedule_type",
        "interval_days",
        "even_odd",
        "color",
    ):
        if creating or field in data:
            if field in effective:
                normalized[field] = effective[field]
    return normalized, effective, None


def _validate_program_v2_fields(data: dict) -> str | None:
    """Backward-compatible validator used by older callers/tests."""
    normalized, _effective, error = _normalize_program_payload(data, creating=True)
    if error is None:
        data.update(normalized)
    return error


def _slot_anchor_map(program: dict, scheduler_anchors: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    main_time = program.get("time")
    if _is_hhmm(main_time) and "main" in scheduler_anchors:
        result[main_time] = scheduler_anchors["main"]
    for index, slot in enumerate(program.get("extra_times") or []):
        key = f"extra:{index}"
        if _is_hhmm(slot) and key in scheduler_anchors:
            result[slot] = scheduler_anchors[key]
    return result


def _next_candidate_interval_anchors(program: dict, scheduler: object | None) -> dict[str, object]:
    timezone = getattr(getattr(scheduler, "scheduler", None), "timezone", None)
    if timezone is None:
        timezone = datetime.now().astimezone().tzinfo
    now = datetime.now(timezone)
    result: dict[str, object] = {}
    slots = [program["time"], *(program.get("extra_times") or [])]
    for slot in slots:
        hours, minutes = (int(part) for part in slot.split(":"))
        anchor = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if anchor <= now:
            anchor += timedelta(days=1)
        result[slot] = anchor
    return result


def _scheduler_interval_anchor_map(program: dict, candidate_anchors: dict[str, object]) -> dict[str, datetime] | None:
    """Translate admitted time-keyed anchors to the scheduler's stable keys."""
    if program.get("schedule_type") != "interval":
        return None
    result: dict[str, datetime] = {}
    slots = [("main", program.get("time"))]
    slots.extend((f"extra:{index}", slot) for index, slot in enumerate(program.get("extra_times") or []))
    for scheduler_key, slot in slots:
        anchor = candidate_anchors.get(slot)
        if (
            not _is_hhmm(slot)
            or not isinstance(anchor, datetime)
            or anchor.tzinfo is None
            or anchor.utcoffset() is None
            or anchor.strftime("%H:%M") != slot
        ):
            return None
        result[scheduler_key] = anchor
    return result


def _interval_anchor_unavailable_response():
    return (
        jsonify(
            {
                "success": False,
                "message": "Authoritative interval anchors are unavailable",
                "error_code": "INTERVAL_ANCHOR_UNAVAILABLE",
            }
        ),
        503,
    )


def _program_schedule_failed_response(
    *,
    rollback_succeeded: bool | None = None,
    schedule_restored: bool | None = None,
):
    payload = {
        "success": False,
        "message": "Failed to apply program schedule",
        "error_code": "PROGRAM_SCHEDULE_FAILED",
    }
    if rollback_succeeded is not None:
        payload["rollback_succeeded"] = rollback_succeeded
    if schedule_restored is not None:
        payload["schedule_restored"] = schedule_restored
    return jsonify(payload), 503


def _program_write_snapshot(program: dict) -> dict:
    """Return only repository-owned fields needed to restore a prior row."""
    return {field: program.get(field) for field in _PROGRAM_WRITE_FIELDS}


def _schedule_admitted_program(scheduler: object, program: dict, anchors: dict[str, datetime] | None) -> object:
    kwargs: dict[str, object] = {}
    if program.get("schedule_type") == "interval":
        kwargs["interval_anchors"] = anchors
    fingerprint = getattr(scheduler, "program_schedule_fingerprint", None)
    if callable(fingerprint):
        kwargs["expected_fingerprint"] = fingerprint(program["id"], program)
    return scheduler.schedule_program(program["id"], program, **kwargs)


def _reconcile_program_schedule(
    scheduler: object | None,
    program: dict,
    anchors: dict[str, datetime] | None,
) -> bool:
    if scheduler is None:
        # Disabled state is self-enforcing at timer fire.  Enabled mutations
        # require a live scheduler in production; isolated API tests
        # intentionally exercise repository contracts without starting one.
        return not program.get("enabled", True) or current_app.testing
    return _schedule_admitted_program(scheduler, program, anchors) is not False


def _restore_program_row(program_id: int, previous: dict) -> dict | None:
    try:
        return db.update_program(program_id, _program_write_snapshot(previous))
    except ProgramZonesNotFoundError:
        logger.exception("Не удалось откатить программу %s: её зоны изменились", program_id)
        return None


def _cancel_program_schedule(scheduler: object | None, program_id: int) -> bool:
    if scheduler is None:
        return current_app.testing
    return scheduler.cancel_program(program_id) is not False


def _interval_anchor_context(
    program_id: int | None,
    effective: dict,
    *,
    preserve_candidate_anchor: bool,
) -> tuple[dict[str, object], dict[int, dict[str, object]]]:
    """Read authoritative stored anchors and derive a new candidate anchor."""
    scheduler = get_scheduler()
    getter = getattr(scheduler, "get_program_interval_anchors", None)
    stored: dict[int, dict[str, object]] = {}
    if callable(getter):
        for program in db.get_programs():
            if program.get("schedule_type") != "interval":
                continue
            try:
                anchors = getter(int(program["id"]))
            except (KeyError, TypeError, ValueError, RuntimeError):
                logger.debug("Не удалось прочитать interval anchors программы %s", program.get("id"), exc_info=True)
                continue
            if isinstance(anchors, dict):
                stored[int(program["id"])] = _slot_anchor_map(program, anchors)

    candidate: dict[str, object] = {}
    if effective.get("schedule_type") == "interval":
        if preserve_candidate_anchor and program_id is not None:
            candidate = dict(stored.get(program_id, {}))
        else:
            candidate = _next_candidate_interval_anchors(effective, scheduler)
    return candidate, stored


def _check_candidate_conflicts(
    program_id: int | None,
    time_value: str,
    zones: list[int],
    days: list[int],
    extra_times: object = None,
    schedule_type: str = "weekdays",
    interval_days: int | None = None,
    even_odd: str | None = None,
    candidate_interval_anchors: dict[str, object] | None = None,
    stored_interval_anchors: dict[int, dict[str, object]] | None = None,
    weather_factor: int | None = None,
    include_weather: bool = False,
    return_details: bool = False,
) -> list[dict] | dict:
    """Check the primary and every candidate extra start through the facade.

    The public ``IrrigationDB`` facade exposes the legacy one-start-at-a-time
    signature. Calling it per candidate remains backward-compatible while the
    repository compares each candidate against stored primary and extra slots.
    """
    repository = getattr(db, "programs", None)
    repository_checker = getattr(repository, "check_program_conflicts", None)
    if callable(repository_checker):
        result = repository_checker(
            program_id=program_id,
            time=time_value,
            zones=zones,
            days=days,
            extra_times=extra_times,
            schedule_type=schedule_type,
            interval_days=interval_days,
            even_odd=even_odd,
            candidate_interval_anchors=candidate_interval_anchors,
            stored_interval_anchors=stored_interval_anchors,
            weather_factor=weather_factor,
            include_weather=include_weather,
            raise_on_error=True,
            detailed=return_details,
        )
        if isinstance(result, dict):
            details = {
                "has_conflicts": bool(result.get("conflicts")),
                "conflicts": list(result.get("conflicts") or []),
                "current_weather_coefficient": int(result.get("current_weather_coefficient", 100)),
            }
        else:
            conflicts = list(result or [])
            details = {
                "has_conflicts": bool(conflicts),
                "conflicts": conflicts,
                "current_weather_coefficient": 100,
            }
        return details if return_details else details["conflicts"]

    candidate_times = [time_value, *(extra_times if isinstance(extra_times, list) else [])]
    conflicts = []
    checked_times = []
    for candidate_time in candidate_times:
        if candidate_time in checked_times:
            continue
        checked_times.append(candidate_time)
        result = db.check_program_conflicts(
            program_id=program_id,
            time=candidate_time,
            zones=zones,
            days=days,
        )
        if isinstance(result, dict):
            result = result.get("conflicts", [])
        for conflict in result or []:
            if conflict not in conflicts:
                conflicts.append(conflict)

    details = {
        "has_conflicts": bool(conflicts),
        "conflicts": conflicts,
        "current_weather_coefficient": 100,
    }
    return details if return_details else conflicts


@programs_api_bp.route("/api/programs")
def api_programs():
    programs = db.get_programs()
    return jsonify(programs)


@programs_api_bp.route("/api/programs/<int:prog_id>", methods=["GET", "PUT", "DELETE"])
@rate_limit("programs", max_requests=20, window_sec=60)
@audit_log("program_modify", target_extractor=lambda *a, **kw: f"program:{kw.get('prog_id', a[0] if a else '?')}")
def api_program(prog_id):
    if request.method == "GET":
        program = db.get_program(prog_id)
        return jsonify(program) if program else ("Program not found", 404)

    elif request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "JSON body must be an object"}), 400

        with _PROGRAM_MUTATION_LOCK:
            current_program = db.get_program(prog_id)
            if not current_program:
                return jsonify({"success": False, "message": "Program not found"}), 404
            normalized, effective_data, validation_error = _normalize_program_payload(
                data,
                current=current_program,
                creating=False,
            )
            if validation_error:
                return _program_validation_error_response(validation_error)
            preserve_candidate_anchor = all(
                current_program.get(field) == effective_data.get(field)
                for field in ("schedule_type", "time", "extra_times", "interval_days")
            )
            candidate_anchors, stored_anchors = _interval_anchor_context(
                prog_id,
                effective_data,
                preserve_candidate_anchor=preserve_candidate_anchor,
            )
            scheduler_anchors = _scheduler_interval_anchor_map(effective_data, candidate_anchors)
            if (
                effective_data.get("enabled")
                and effective_data.get("schedule_type") == "interval"
                and scheduler_anchors is None
            ):
                return _interval_anchor_unavailable_response()
            previous_scheduler_anchors = _scheduler_interval_anchor_map(
                current_program,
                stored_anchors.get(prog_id, {}),
            )
            try:
                conflicts = _check_candidate_conflicts(
                    program_id=prog_id,
                    time_value=effective_data["time"],
                    zones=effective_data["zones"],
                    days=effective_data["days"],
                    extra_times=effective_data.get("extra_times"),
                    schedule_type=effective_data["schedule_type"],
                    interval_days=effective_data.get("interval_days"),
                    even_odd=effective_data.get("even_odd"),
                    candidate_interval_anchors=candidate_anchors,
                    stored_interval_anchors=stored_anchors,
                )
            except (sqlite3.Error, OSError) as error:
                logger.error("Ошибка серверной проверки конфликтов: %s", error)
                return jsonify({"success": False, "message": "Ошибка проверки конфликтов"}), 503
            if conflicts:
                return jsonify(
                    {
                        "success": False,
                        "has_conflicts": True,
                        "conflicts": conflicts,
                        "message": "Обнаружены конфликты программ",
                    }
                )
            try:
                program = db.update_program(prog_id, normalized)
            except ProgramZonesNotFoundError as error:
                return _missing_program_zones_response(error)
            if program is None:
                return ("Program not found", 404)

            scheduler = get_scheduler()
            try:
                schedule_applied = _reconcile_program_schedule(scheduler, program, scheduler_anchors)
            except Exception:
                logger.exception("Ошибка перепланирования программы %s", prog_id)
                schedule_applied = False
            if not schedule_applied:
                if not program.get("enabled"):
                    # As with PATCH disable, the persisted fail-safe state is
                    # preferable to rolling back to an enabled program when
                    # jobstore cancellation reports failure.
                    return _program_schedule_failed_response()

                restored = _restore_program_row(prog_id, current_program)
                schedule_restored = False
                if restored is not None:
                    if not (
                        restored.get("enabled")
                        and restored.get("schedule_type") == "interval"
                        and previous_scheduler_anchors is None
                    ):
                        try:
                            schedule_restored = _reconcile_program_schedule(
                                scheduler,
                                restored,
                                previous_scheduler_anchors,
                            )
                        except Exception:
                            logger.exception("Не удалось восстановить расписание программы %s", prog_id)
                else:
                    logger.critical("Откат строки программы %s после scheduler failure не выполнен", prog_id)
                    try:
                        _cancel_program_schedule(scheduler, prog_id)
                    except Exception:
                        logger.exception("Не удалось fail-safe отменить программу %s", prog_id)
                return _program_schedule_failed_response(
                    rollback_succeeded=restored is not None,
                    schedule_restored=schedule_restored if restored is not None else None,
                )

        db.add_log("prog_edit", json.dumps({"prog": prog_id, "changes": normalized}))
        return jsonify(program)

    elif request.method == "DELETE":
        with _PROGRAM_MUTATION_LOCK:
            if not db.delete_program(prog_id):
                return jsonify({"success": False, "message": "Program not found"}), 404
            scheduler = get_scheduler()
            try:
                schedule_cancelled = _cancel_program_schedule(scheduler, prog_id)
            except Exception:
                logger.exception("Ошибка отмены программы %s в планировщике", prog_id)
                schedule_cancelled = False
            if not schedule_cancelled:
                return _program_schedule_failed_response()
        db.add_log("prog_delete", json.dumps({"prog": prog_id}))
        return ("", 204)


@programs_api_bp.route("/api/programs", methods=["POST"])
@rate_limit("programs", max_requests=20, window_sec=60)
@audit_log("program_create")
def api_create_program():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "message": "JSON body must be an object"}), 400
    normalized, effective_data, validation_error = _normalize_program_payload(data, creating=True)
    if validation_error:
        return _program_validation_error_response(validation_error)

    with _PROGRAM_MUTATION_LOCK:
        candidate_anchors, stored_anchors = _interval_anchor_context(
            None,
            effective_data,
            preserve_candidate_anchor=False,
        )
        scheduler_anchors = _scheduler_interval_anchor_map(effective_data, candidate_anchors)
        if (
            effective_data.get("enabled")
            and effective_data.get("schedule_type") == "interval"
            and scheduler_anchors is None
        ):
            return _interval_anchor_unavailable_response()
        try:
            conflicts = _check_candidate_conflicts(
                program_id=None,
                time_value=effective_data["time"],
                zones=effective_data["zones"],
                days=effective_data["days"],
                extra_times=effective_data.get("extra_times"),
                schedule_type=effective_data["schedule_type"],
                interval_days=effective_data.get("interval_days"),
                even_odd=effective_data.get("even_odd"),
                candidate_interval_anchors=candidate_anchors,
                stored_interval_anchors=stored_anchors,
            )
        except (sqlite3.Error, OSError) as error:
            logger.error("Ошибка серверной проверки конфликтов (create): %s", error)
            return jsonify({"success": False, "message": "Ошибка проверки конфликтов"}), 503
        if conflicts:
            return jsonify(
                {
                    "success": False,
                    "has_conflicts": True,
                    "conflicts": conflicts,
                    "message": "Обнаружены конфликты программ",
                }
            )
        try:
            program = db.create_program(normalized)
        except ProgramZonesNotFoundError as error:
            return _missing_program_zones_response(error)
        if program is None:
            return ("Error creating program", 400)

        scheduler = get_scheduler()
        try:
            schedule_applied = _reconcile_program_schedule(scheduler, program, scheduler_anchors)
        except Exception:
            logger.exception("Ошибка планирования новой программы %s", program["id"])
            schedule_applied = False
        if not schedule_applied:
            rollback_succeeded = db.delete_program(program["id"])
            try:
                _cancel_program_schedule(scheduler, program["id"])
            except Exception:
                logger.exception("Не удалось очистить jobs отклонённой программы %s", program["id"])
            if not rollback_succeeded:
                logger.critical("Не удалось удалить программу %s после scheduler failure", program["id"])
            return _program_schedule_failed_response(rollback_succeeded=rollback_succeeded)

    db.add_log("prog_create", json.dumps({"prog": program["id"], "name": program["name"]}))
    return jsonify(program), 201


@programs_api_bp.route("/api/programs/check-conflicts", methods=["POST"])
@rate_limit("program-conflicts", max_requests=30, window_sec=60)
def check_program_conflicts():
    """Check watering program conflicts."""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "JSON body must be an object"}), 400
        program_id = data.get("program_id")
        if program_id is not None and (
            isinstance(program_id, bool) or not isinstance(program_id, int) or program_id <= 0
        ):
            return jsonify({"success": False, "message": "program_id must be a positive integer or null"}), 400
        time_val = data.get("time")
        zones = data.get("zones", [])
        days = data.get("days", [])
        extra_times = data.get("extra_times")
        schedule_type = data.get("schedule_type", "weekdays")
        weather_factor = data.get("weather_factor")
        include_weather = data.get("include_weather", False)
        if weather_factor is not None and (
            isinstance(weather_factor, bool) or not isinstance(weather_factor, int) or not 100 <= weather_factor <= 1000
        ):
            return jsonify({"success": False, "message": "weather_factor must be an integer between 100 and 1000"}), 400
        if not isinstance(include_weather, bool):
            return jsonify({"success": False, "message": "include_weather must be a JSON boolean"}), 400
        candidate = {
            "name": "Conflict probe",
            "time": time_val,
            "zones": zones,
            "days": days,
            "extra_times": [] if extra_times is None else extra_times,
            "schedule_type": schedule_type,
            "interval_days": data.get("interval_days"),
            "even_odd": data.get("even_odd"),
            "enabled": True,
        }
        _normalized, effective, validation_error = _normalize_program_payload(candidate, creating=True)

        if validation_error:
            # Best-effort debug trace of UI intent — only when debug logging is on.
            try:
                debug_audit(
                    action_type="program_check_conflicts",
                    source="api",
                    target=f"program:{program_id}" if program_id else None,
                    payload={"time": time_val, "zones": zones, "days": days, "result": "invalid_input"},
                )
            except Exception:
                logger.debug("check_program_conflicts: debug_audit failed", exc_info=True)
            return jsonify({"success": False, "message": validation_error}), 400

        current_program = db.get_program(program_id) if program_id is not None else None
        preserve_candidate_anchor = bool(current_program) and all(
            current_program.get(field) == effective.get(field)
            for field in ("schedule_type", "time", "extra_times", "interval_days")
        )
        candidate_anchors, stored_anchors = _interval_anchor_context(
            program_id,
            effective,
            preserve_candidate_anchor=preserve_candidate_anchor,
        )
        conflict_result = _check_candidate_conflicts(
            program_id,
            effective["time"],
            effective["zones"],
            effective["days"],
            effective["extra_times"],
            effective["schedule_type"],
            effective.get("interval_days"),
            effective.get("even_odd"),
            candidate_anchors,
            stored_anchors,
            weather_factor,
            include_weather,
            return_details=True,
        )
        conflicts = conflict_result["conflicts"]
        # Debug-level trace — this is read-only intent, useful for triaging
        # "why did the UI block save" without flooding audit_log in prod.
        try:
            debug_audit(
                action_type="program_check_conflicts",
                source="api",
                target=f"program:{program_id}" if program_id else None,
                payload={
                    "time": time_val,
                    "zones": zones,
                    "days": days,
                    "has_conflicts": len(conflicts) > 0,
                    "conflict_count": len(conflicts),
                },
            )
        except Exception:
            logger.debug("check_program_conflicts: debug_audit failed", exc_info=True)
        return jsonify({"success": True, **conflict_result})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка проверки конфликтов программ: {e}")
        return jsonify({"success": False, "message": "Ошибка проверки конфликтов"}), 500


@programs_api_bp.route("/api/programs/<int:prog_id>/duplicate", methods=["POST"])
@rate_limit("programs", max_requests=10, window_sec=60)
@audit_log("program_duplicate", target_extractor=lambda *a, **kw: f"program:{kw.get('prog_id', a[0] if a else '?')}")
def api_duplicate_program(prog_id):
    """Duplicate program (create copy with '(копия)' suffix)."""
    try:
        with _PROGRAM_MUTATION_LOCK:
            source_program = db.get_program(prog_id)
            if source_program is None:
                return jsonify({"success": False, "message": "Program not found"}), 404
            if source_program.get("type") == "smart":
                return _program_type_unsupported_response()
            try:
                new_program = db.duplicate_program(prog_id)
            except ProgramZonesNotFoundError as error:
                return _missing_program_zones_response(error)
        if new_program:
            db.add_log("prog_duplicate", json.dumps({"original": prog_id, "copy": new_program["id"]}))
            return jsonify({"success": True, "program": new_program}), 201
        return jsonify({"success": False, "message": "Program not found"}), 404
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка дублирования программы {prog_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка дублирования программы"}), 500


@programs_api_bp.route("/api/programs/<int:prog_id>/enabled", methods=["PATCH"])
@rate_limit("programs", max_requests=20, window_sec=60)
@audit_log("program_toggle", target_extractor=lambda *a, **kw: f"program:{kw.get('prog_id', a[0] if a else '?')}")
def api_toggle_program_enabled(prog_id):
    """Toggle program enabled/disabled state."""
    try:
        if str(db.get_setting_value("password_must_change") or "0") == "1":
            return jsonify(
                {
                    "success": False,
                    "message": "password change required",
                    "error_code": "PASSWORD_MUST_CHANGE",
                }
            ), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "JSON body must be an object"}), 400
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify({"success": False, "message": "enabled must be a JSON boolean"}), 400
        if "color" in data and _normalize_color(data["color"]) is None:
            return jsonify({"success": False, "message": "color must use strict #RRGGBB format"}), 400

        scheduler_anchors = None
        previous_scheduler_anchors = None
        with _PROGRAM_MUTATION_LOCK:
            current_program = db.get_program(prog_id)
            if current_program is None:
                return jsonify({"success": False, "message": "Program not found"}), 404

            if enabled:
                _normalized, effective, validation_error = _normalize_program_payload(
                    current_program,
                    creating=True,
                )
                if validation_error:
                    if validation_error == _UNSUPPORTED_PROGRAM_TYPE_ERROR:
                        return _program_type_unsupported_response()
                    return (
                        jsonify(
                            {
                                "success": False,
                                "message": validation_error,
                                "error_code": "PROGRAM_INVALID_STATE",
                            }
                        ),
                        409,
                    )
                candidate_anchors, stored_anchors = _interval_anchor_context(
                    prog_id,
                    effective,
                    preserve_candidate_anchor=bool(current_program.get("enabled")),
                )
                scheduler_anchors = _scheduler_interval_anchor_map(effective, candidate_anchors)
                if effective.get("schedule_type") == "interval" and scheduler_anchors is None:
                    return _interval_anchor_unavailable_response()
                previous_scheduler_anchors = _scheduler_interval_anchor_map(
                    current_program,
                    stored_anchors.get(prog_id, {}),
                )
                try:
                    conflicts = _check_candidate_conflicts(
                        program_id=prog_id,
                        time_value=effective["time"],
                        zones=effective["zones"],
                        days=effective["days"],
                        extra_times=effective.get("extra_times"),
                        schedule_type=effective["schedule_type"],
                        interval_days=effective.get("interval_days"),
                        even_odd=effective.get("even_odd"),
                        candidate_interval_anchors=candidate_anchors,
                        stored_interval_anchors=stored_anchors,
                    )
                except (sqlite3.Error, OSError) as error:
                    logger.error("Ошибка серверной проверки конфликтов (enable): %s", error)
                    return jsonify({"success": False, "message": "Ошибка проверки конфликтов"}), 503
                if conflicts:
                    return (
                        jsonify(
                            {
                                "success": False,
                                "has_conflicts": True,
                                "conflicts": conflicts,
                                "message": "Обнаружены конфликты программ",
                            }
                        ),
                        409,
                    )

            try:
                program = db.update_program(prog_id, {"enabled": enabled})
            except ProgramZonesNotFoundError as error:
                return _missing_program_zones_response(error)
            if program is None:
                return jsonify({"success": False, "message": "Program not found"}), 404

            scheduler = get_scheduler()
            try:
                schedule_applied = _reconcile_program_schedule(scheduler, program, scheduler_anchors)
            except Exception:
                logger.exception("Ошибка перепланирования программы %s после toggle", prog_id)
                schedule_applied = False
            if not schedule_applied:
                if not enabled:
                    # Persisting enabled=0 is already the fail-safe state.  A
                    # timer callback revalidates it even if jobstore removal is
                    # temporarily unavailable.
                    return _program_schedule_failed_response()

                restored = _restore_program_row(prog_id, current_program)
                schedule_restored = False
                if restored is not None:
                    if not (
                        restored.get("enabled")
                        and restored.get("schedule_type") == "interval"
                        and previous_scheduler_anchors is None
                    ):
                        try:
                            schedule_restored = _reconcile_program_schedule(
                                scheduler,
                                restored,
                                previous_scheduler_anchors,
                            )
                        except Exception:
                            logger.exception("Не удалось восстановить расписание программы %s", prog_id)
                else:
                    logger.critical("Откат enabled программы %s после scheduler failure не выполнен", prog_id)
                    try:
                        _cancel_program_schedule(scheduler, prog_id)
                    except Exception:
                        logger.exception("Не удалось fail-safe отменить программу %s", prog_id)
                return _program_schedule_failed_response(
                    rollback_succeeded=restored is not None,
                    schedule_restored=schedule_restored if restored is not None else None,
                )

        db.add_log("prog_toggle", json.dumps({"prog": prog_id, "enabled": enabled}))
        return jsonify({"success": True, "program": program})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка toggle enabled для программы {prog_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка обновления программы"}), 500


@programs_api_bp.route("/api/programs/<int:prog_id>/run", methods=["POST"])
@rate_limit("programs", max_requests=10, window_sec=60)
@audit_log("program_manual_run", target_extractor=lambda *a, **kw: f"program:{kw.get('prog_id', a[0] if a else '?')}")
def api_run_program(prog_id):
    """Accept a manual ad-hoc run for asynchronous scheduler processing."""
    if current_app.config.get("EMERGENCY_STOP"):
        return jsonify(
            {"success": False, "message": "Аварийная остановка активна. Сначала отключите аварийный режим."}
        ), 400
    program = db.get_program(prog_id)
    if not program:
        return jsonify({"success": False, "message": "Program not found"}), 404
    if program.get("type") == "smart":
        return _program_type_unsupported_response()
    if not program.get("enabled", True):
        return jsonify(
            {
                "success": False,
                "message": "Program is disabled; enable it before manual run",
            }
        ), 409

    zones = program.get("zones") or []
    if not zones:
        return jsonify({"success": False, "message": "Program has no zones"}), 400

    scheduler = get_scheduler()
    if scheduler is None:
        return jsonify(
            {
                "success": False,
                "message": "Scheduler unavailable",
                "error_code": "SCHEDULER_UNAVAILABLE",
            }
        ), 503

    try:
        zone_ids = [int(zone_id) for zone_id in zones]
        runnable_zones = [
            zone_id
            for zone_id in zone_ids
            if (zone := db.get_zone(zone_id)) is not None and str(zone.get("state") or "").lower() != "fault"
        ]
    except (OSError, TypeError, ValueError) as e:
        logger.error("Ошибка проверки зон программы %s: %s", prog_id, e)
        return jsonify({"success": False, "message": "Ошибка запуска программы"}), 500
    if not runnable_zones:
        return jsonify(
            {
                "success": False,
                "message": "Program has no runnable zones",
                "error_code": "PROGRAM_NO_RUNNABLE_ZONES",
            }
        ), 409

    name = program.get("name") or f"Program {prog_id}"
    try:
        from irrigation_scheduler import job_run_program

        # Issue #31: manual=True — bypass weather skip / coefficient for user-initiated runs.
        threading.Thread(
            target=job_run_program,
            args=(int(prog_id), zone_ids, str(name)),
            kwargs={"manual": True},
            daemon=True,
        ).start()
    except (ImportError, OSError, RuntimeError, ValueError, TypeError) as e:
        logger.error(f"Ошибка ручного запуска программы {prog_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка запуска программы"}), 500

    try:
        db.add_log(
            "prog_manual_run",
            json.dumps(
                {
                    "prog": prog_id,
                    "name": name,
                    "zones": list(zones),
                    "status": "accepted",
                    "started": False,
                }
            ),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.debug("Handled exception logging manual program run: %s", e)
    return jsonify(
        {
            "success": True,
            "accepted": True,
            "started": False,
            "status": "accepted",
            "message": f"Программа {name}: запрос на запуск принят",
        }
    ), 202


@programs_api_bp.route("/api/programs/<int:prog_id>/log", methods=["GET"])
def api_program_log(prog_id):
    """Report the unavailable run-history capability truthfully."""
    try:
        program = db.get_program(prog_id)
        if not program:
            return jsonify({"success": False, "message": "Program not found"}), 404
        return jsonify(
            {
                "success": False,
                "supported": False,
                "capability": "program_log",
                "error_code": "PROGRAM_RUN_IDENTITY_UNAVAILABLE",
                "message": "Program execution identity is not stored; this capability is unavailable",
            }
        ), 501
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения журнала программы {prog_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка получения журнала"}), 500


@programs_api_bp.route("/api/programs/<int:prog_id>/stats", methods=["GET"])
def api_program_stats(prog_id):
    """Report the unavailable program-statistics capability truthfully."""
    try:
        program = db.get_program(prog_id)
        if not program:
            return jsonify({"success": False, "message": "Program not found"}), 404

        return jsonify(
            {
                "success": False,
                "supported": False,
                "capability": "program_stats",
                "error_code": "PROGRAM_RUN_IDENTITY_UNAVAILABLE",
                "message": "Program execution identity is not stored; this capability is unavailable",
            }
        ), 501
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения статистики программы {prog_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка получения статистики"}), 500
