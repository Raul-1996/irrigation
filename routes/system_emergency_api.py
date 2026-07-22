"""System Emergency & Backup API — emergency stop/resume, backup."""

import json
import logging
import sqlite3

from flask import Blueprint, current_app, jsonify

from database import db
from irrigation_scheduler import get_scheduler
from services.api_rate_limiter import rate_limit
from services.audit import audit_log

try:
    from services import events as _events
except ImportError:
    _events = None

logger = logging.getLogger(__name__)

system_emergency_api_bp = Blueprint("system_emergency_api", __name__)

_SCHEDULER_STOP_AGGREGATE_KEYS = {
    "success",
    "aggregate_valid",
    "stopped",
    "unresolved",
    "unverified_zone_ids",
    "retry_scheduled",
    "group_id",
}


def _strict_zone_id_bucket(value) -> list[int] | None:
    if type(value) is not list:
        return None
    if any(type(zone_id) is not int or zone_id <= 0 for zone_id in value):
        return None
    if len(value) != len(set(value)):
        return None
    return sorted(value)


def _scheduler_partition_is_exact(result, group_id: int, expected_zone_ids: list[int]) -> bool:
    """Validate one scheduler seven-field aggregate without coercion."""
    gid = int(group_id)
    if type(result) is not dict or set(result) != _SCHEDULER_STOP_AGGREGATE_KEYS:
        return False
    success = result.get("success")
    aggregate_valid = result.get("aggregate_valid")
    retry_scheduled = result.get("retry_scheduled")
    if (
        type(success) is not bool
        or type(aggregate_valid) is not bool
        or aggregate_valid is not True
        or type(retry_scheduled) is not bool
        or type(result.get("group_id")) is not int
        or result.get("group_id") != gid
    ):
        return False

    stopped = _strict_zone_id_bucket(result.get("stopped"))
    unresolved = _strict_zone_id_bucket(result.get("unresolved"))
    unverified = _strict_zone_id_bucket(result.get("unverified_zone_ids"))
    if stopped is None or unresolved is None or unverified is None or unverified:
        return False
    stopped_set = set(stopped)
    unresolved_set = set(unresolved)
    if stopped_set & unresolved_set or stopped_set | unresolved_set != set(expected_zone_ids):
        return False
    return success == (not unresolved) and (not retry_scheduled or bool(unresolved))


def _strict_emergency_group_ids() -> list[int] | None:
    repository = getattr(db, "groups", None)
    connector = getattr(repository, "_connect", None)
    if not callable(connector):
        return None
    try:
        with connector() as conn:
            rows = conn.execute("SELECT id FROM groups ORDER BY id").fetchall()
        if not isinstance(rows, (list, tuple)):
            return None
        group_ids: list[int] = []
        for row in rows:
            group_id = row[0]
            if type(group_id) is not int or group_id <= 0 or group_id in group_ids:
                return None
            group_ids.append(group_id)
        return group_ids
    except (sqlite3.Error, OSError, AttributeError, KeyError, RuntimeError, TypeError, ValueError):
        logger.exception("emergency stop: strict group snapshot failed")
        return None


def _quiesce_emergency_sessions() -> bool:
    """Cancel every group and require an exact full scheduler partition."""
    group_ids = _strict_emergency_group_ids()
    if group_ids is None:
        return False
    if not group_ids:
        return True
    try:
        scheduler = get_scheduler()
    except (AttributeError, ConnectionError, TimeoutError, RuntimeError, OSError, sqlite3.Error, TypeError, ValueError):
        logger.exception("emergency stop: scheduler lookup failed")
        return False
    cancel_group_jobs = getattr(scheduler, "cancel_group_jobs", None)
    if scheduler is None or not callable(cancel_group_jobs):
        return False

    try:
        from services import zone_control

        strict_group_zone_ids = getattr(zone_control, "_strict_group_zone_ids", None)
    except ImportError:
        strict_group_zone_ids = None
    all_valid = callable(strict_group_zone_ids)
    for group_id in group_ids:
        expected_zone_ids = None
        if callable(strict_group_zone_ids):
            try:
                expected_zone_ids = strict_group_zone_ids(group_id)
            except (sqlite3.Error, OSError, AttributeError, KeyError, RuntimeError, TypeError, ValueError):
                logger.exception("emergency stop: strict zone snapshot failed group=%s", group_id)
        try:
            cancel_result = cancel_group_jobs(group_id, master_close_immediately=True)
        except (ConnectionError, TimeoutError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, RuntimeError):
            logger.exception("emergency stop: scheduler cancel failed group=%s", group_id)
            cancel_result = None
        valid = expected_zone_ids is not None and _scheduler_partition_is_exact(
            cancel_result,
            group_id,
            expected_zone_ids,
        )
        all_valid = all_valid and valid
    return all_valid


# ===== Emergency =====


@system_emergency_api_bp.route("/api/emergency-stop", methods=["POST"])
@rate_limit("emergency", max_requests=5, window_sec=60)
@audit_log("emergency_stop", target_extractor=lambda *a, **kw: "system")
def api_emergency_stop():
    """Emergency stop all zones."""
    stats = None
    try:
        # Set the flag FIRST so HTTP API handlers (zones_watering_api, groups_api,
        # system_status_api) and SSE hub immediately reject new zone-on actions.
        # Note: APScheduler ticks do NOT check this flag — they're stopped by
        # cancel_group_jobs() further below via group_cancel_events.
        current_app.config["EMERGENCY_STOP"] = True
        db.add_log("emergency_stop", json.dumps({"active": True}))

        # Sequential, deterministic — no timer race:
        # Phase A: stop all zones across all groups (skip_master_close=True)
        # Phase B: wait until state='off' (force-retry stuck zones at deadline)
        # Phase C: synchronously close all master valves
        try:
            from services.zone_control import emergency_stop_all

            stats = emergency_stop_all(reason="emergency_stop")
            logger.info("api_emergency_stop: stats=%s", stats)
        except (ValueError, TypeError, RuntimeError, ImportError):
            logger.exception("emergency stop: emergency_stop_all failed")

        # Cancel scheduler jobs AFTER masters are closed.  Completion requires
        # one exact seven-field full partition per group; a mere method return
        # is not proof that every session owner has quiesced.
        sessions_quiesced = _quiesce_emergency_sessions()
        try:
            if _events:
                _events.publish({"type": "emergency_on", "by": "api"})
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in api_emergency_stop: %s", e)
        # The emergency flag remains armed independently of response truth.
        # Physical completion belongs exclusively to the core aggregate;
        # scheduler cancellation separately proves that session owners stopped.
        physical_stop_confirmed = isinstance(stats, dict) and stats.get("success") is True
        success = physical_stop_confirmed and sessions_quiesced
        zones_failed = stats.get("zones_failed", []) if isinstance(stats, dict) else []
        masters_failed_publish = stats.get("masters_failed_publish", 0) if isinstance(stats, dict) else 0
        errors = stats.get("errors", []) if isinstance(stats, dict) else []
        response = {
            "success": success,
            "physical_stop_confirmed": physical_stop_confirmed,
            "sessions_quiesced": sessions_quiesced,
            "zones_failed": zones_failed,
            "masters_failed_publish": masters_failed_publish,
            "message": "Аварийная остановка выполнена" if success else "Аварийная остановка требует внимания",
            "stats": stats,
        }
        if not success:
            response["warning"] = (
                f"physical_stop_confirmed={physical_stop_confirmed}, "
                f"sessions_quiesced={sessions_quiesced}, "
                f"zones_failed={zones_failed}, "
                f"errors={errors}, "
                f"masters_failed_publish={masters_failed_publish}"
            )
            return jsonify(response), 503
        return jsonify(response)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка аварийной остановки: {e}")
        return jsonify({"success": False, "message": "Ошибка аварийной остановки"}), 500


@system_emergency_api_bp.route("/api/emergency-resume", methods=["POST"])
@rate_limit("emergency", max_requests=5, window_sec=60)
@audit_log("emergency_resume", target_extractor=lambda *a, **kw: "system")
def api_emergency_resume():
    """Resume after emergency stop."""
    try:
        current_app.config["EMERGENCY_STOP"] = False
        db.add_log("emergency_stop", json.dumps({"active": False}))
        try:
            if _events:
                _events.publish({"type": "emergency_off", "by": "api"})
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in api_emergency_resume: %s", e)
        return jsonify({"success": True, "message": "Полив возобновлен"})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка возобновления после аварийной остановки: {e}")
        return jsonify({"success": False, "message": "Ошибка возобновления"}), 500


# ===== Backup =====


@system_emergency_api_bp.route("/api/backup", methods=["POST"])
@audit_log("backup_create", target_extractor=lambda *a, **kw: "system")
def api_backup():
    try:
        backup_path = db.create_backup()
        if backup_path:
            return jsonify({"success": True, "message": "Резервная копия создана", "backup_path": backup_path})
        else:
            return jsonify({"success": False, "message": "Ошибка создания резервной копии"}), 500
    except (sqlite3.Error, OSError) as e:
        logger.debug("Exception in api_backup: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500
