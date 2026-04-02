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
    try:
        try:
            from services.zone_control import stop_all_in_group as _stop_all
            groups = db.get_groups() or []
            for g in groups:
                try:
                    _stop_all(int(g['id']), reason='emergency_stop', force=True)
                except (ValueError, TypeError, KeyError):
                    logger.exception('emergency stop: stop_all_in_group failed')
        except (ValueError, TypeError, RuntimeError):
            logger.exception('emergency stop: controller unavailable')
        current_app.config['EMERGENCY_STOP'] = True
        db.add_log('emergency_stop', json.dumps({"active": True}))
        try:
            scheduler = get_scheduler()
            if scheduler:
                groups = db.get_groups() or []
                for g in groups:
                    try:
                        scheduler.cancel_group_jobs(int(g['id']))
                    except (ValueError, TypeError, KeyError) as e:
                        logger.debug("Handled exception in api_emergency_stop: %s", e)
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in api_emergency_stop: %s", e)
        try:
            if _events:
                _events.publish({'type': 'emergency_on', 'by': 'api'})
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in api_emergency_stop: %s", e)
        return jsonify({"success": True, "message": "Аварийная остановка выполнена"})
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
