"""System Emergency & Backup API — emergency stop/resume, backup."""
from flask import Blueprint, request, jsonify, current_app
import json
import logging

from database import db
from irrigation_scheduler import get_scheduler
from services.api_rate_limiter import rate_limit
import sqlite3

try:
    from services import events as _events
except ImportError:
    _events = None

logger = logging.getLogger(__name__)

system_emergency_api_bp = Blueprint('system_emergency_api', __name__)


# ===== Emergency =====

@system_emergency_api_bp.route('/api/emergency-stop', methods=['POST'])
@rate_limit('emergency', max_requests=5, window_sec=60)
def api_emergency_stop():
    """Emergency stop all zones."""
    stats = None
    try:
        # Set the flag FIRST so HTTP API handlers (zones_watering_api, groups_api,
        # system_status_api) and SSE hub immediately reject new zone-on actions.
        # Note: APScheduler ticks do NOT check this flag — they're stopped by
        # cancel_group_jobs() further below via group_cancel_events.
        current_app.config['EMERGENCY_STOP'] = True
        db.add_log('emergency_stop', json.dumps({"active": True}))

        # Sequential, deterministic — no timer race:
        # Phase A: stop all zones across all groups (skip_master_close=True)
        # Phase B: wait until state='off' (force-retry stuck zones at deadline)
        # Phase C: synchronously close all master valves
        try:
            from services.zone_control import emergency_stop_all
            stats = emergency_stop_all(reason='emergency_stop')
            logger.info("api_emergency_stop: stats=%s", stats)
        except (ValueError, TypeError, RuntimeError, ImportError):
            logger.exception('emergency stop: emergency_stop_all failed')

        # Cancel scheduler jobs AFTER masters are closed.
        # master_close_immediately=True so the internal stop_all_in_group call
        # uses the no-delay path (defensive — Phase C already closed the master,
        # but in case anything races we don't want a delay=60 timer scheduled).
        try:
            scheduler = get_scheduler()
            if scheduler:
                groups = db.get_groups() or []
                for g in groups:
                    try:
                        scheduler.cancel_group_jobs(int(g['id']), master_close_immediately=True)
                    except (ValueError, KeyError) as e:
                        logger.debug("Handled exception in api_emergency_stop: %s", e)
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in api_emergency_stop: %s", e)
        try:
            if _events:
                _events.publish({'type': 'emergency_on', 'by': 'api'})
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in api_emergency_stop: %s", e)
        # Surface stats so UI/ops can immediately see if any master failed to publish
        response = {"success": True, "message": "Аварийная остановка выполнена"}
        if stats is not None:
            response["stats"] = stats
            if stats.get('masters_failed_publish', 0) > 0 or stats.get('zones_still_active_after_wait', 0) > 0:
                response["warning"] = (
                    f"masters_failed_publish={stats.get('masters_failed_publish', 0)}, "
                    f"zones_still_active_after_wait={stats.get('zones_still_active_after_wait', 0)}"
                )
        return jsonify(response)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка аварийной остановки: {e}")
        return jsonify({"success": False, "message": "Ошибка аварийной остановки"}), 500


@system_emergency_api_bp.route('/api/emergency-resume', methods=['POST'])
@rate_limit('emergency', max_requests=5, window_sec=60)
def api_emergency_resume():
    """Resume after emergency stop."""
    try:
        current_app.config['EMERGENCY_STOP'] = False
        db.add_log('emergency_stop', json.dumps({"active": False}))
        try:
            if _events:
                _events.publish({'type': 'emergency_off', 'by': 'api'})
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in api_emergency_resume: %s", e)
        return jsonify({"success": True, "message": "Полив возобновлен"})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка возобновления после аварийной остановки: {e}")
        return jsonify({"success": False, "message": "Ошибка возобновления"}), 500


# ===== Backup =====

@system_emergency_api_bp.route('/api/backup', methods=['POST'])
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
