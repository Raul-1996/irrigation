"""System Config API — auth, password, rain, env, map, postpone, settings."""
from flask import Blueprint, request, jsonify, current_app, session, redirect, url_for
from datetime import datetime, timedelta
import json
import os
import time
import logging

from database import db
from utils import normalize_topic
from irrigation_scheduler import get_scheduler
from services.mqtt_pub import publish_mqtt_value as _publish_mqtt_value
from services.helpers import MAP_DIR, ALLOWED_MIME_TYPES
from services.monitors import env_monitor, probe_env_values
from constants import MIN_PASSWORD_LENGTH
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash
import sqlite3

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

logger = logging.getLogger(__name__)

system_config_api_bp = Blueprint('system_config_api', __name__)

# Password blocklist (TASK-013)
_PASSWORD_BLOCKLIST = {'1234', '12345678', '0000', 'password', 'admin', 'qwerty'}


# ===== Auth / Password =====

@system_config_api_bp.route('/api/auth/status')
def api_auth_status():
    return jsonify({
        'authenticated': bool(session.get('logged_in')) or bool(current_app.config.get('TESTING')),
        'role': session.get('role', 'guest')
    })


@system_config_api_bp.route('/logout', methods=['GET'])
def api_logout():
    session['logged_in'] = False
    session['role'] = 'user'
    return redirect(url_for('auth_bp.login_page'))


@system_config_api_bp.route('/api/password', methods=['POST'])
def api_change_password():
    try:
        if not session.get('logged_in') and not current_app.config.get('TESTING'):
            return jsonify({'success': False, 'message': 'Требуется аутентификация'}), 401
        data = request.get_json() or {}
        old_password = data.get('old_password', '')
        new_password = data.get('new_password', '')
        if len(new_password) < MIN_PASSWORD_LENGTH:
            return jsonify({'success': False, 'message': f'Пароль должен быть не менее {MIN_PASSWORD_LENGTH} символов'}), 400
        if len(new_password) > 32:
            return jsonify({'success': False, 'message': 'Пароль не может быть длиннее 32 символов'}), 400
        if not new_password:
            return jsonify({'success': False, 'message': 'Новый пароль обязателен'}), 400
        if new_password.lower() in _PASSWORD_BLOCKLIST:
            return jsonify({'success': False, 'message': 'Этот пароль слишком простой. Выберите другой.'}), 400
        stored_hash = db.get_password_hash()
        if stored_hash and (current_app.config.get('TESTING') or check_password_hash(stored_hash, old_password)):
            if db.set_password(new_password):
                return jsonify({'success': True})
            return jsonify({'success': False, 'message': 'Не удалось обновить пароль'}), 500
        return jsonify({'success': False, 'message': 'Старый пароль неверен'}), 400
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка смены пароля: {e}")
        return jsonify({'success': False, 'message': 'Ошибка смены пароля'}), 500


# ===== Map =====

@system_config_api_bp.route('/api/map', methods=['GET', 'POST'])
def api_map():
    try:
        if request.method == 'GET':
            allowed_ext = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
            items = []
            for f in os.listdir(MAP_DIR):
                p = os.path.join(MAP_DIR, f)
                try:
                    ext = os.path.splitext(f)[1].lower()
                    if os.path.isfile(p) and ext in allowed_ext:
                        items.append({'name': f, 'path': f"media/maps/{f}", 'mtime': os.path.getmtime(p)})
                except (IOError, OSError, PermissionError) as e:
                    logger.debug("Exception in api_map: %s", e)
                    continue
            items.sort(key=lambda x: x['mtime'], reverse=True)
            return jsonify({'success': True, 'items': items})
        else:
            if not (current_app.config.get('TESTING') or session.get('role') == 'admin'):
                return jsonify({'success': False, 'message': 'Только администратор может загружать карты'}), 403
            if 'file' not in request.files:
                return jsonify({'success': False, 'message': 'Файл не найден'}), 400
            file = request.files['file']
            if file.filename == '':
                return jsonify({'success': False, 'message': 'Файл не выбран'}), 400
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
                return jsonify({'success': False, 'message': 'Неподдерживаемый формат'}), 400
            m = request.files.get('file')
            if not m or (getattr(m, 'mimetype', None) not in ALLOWED_MIME_TYPES):
                return jsonify({'success': False, 'message': 'Неподдерживаемый тип содержимого'}), 400
            filename = f"zones_map_{int(time.time())}{ext}"
            save_path = os.path.join(MAP_DIR, filename)
            file.save(save_path)
            return jsonify({'success': True, 'message': 'Карта загружена', 'path': f"media/maps/{filename}"})
    except (IOError, OSError, PermissionError) as e:
        logger.error(f"Ошибка работы с картой зон: {e}")
        return jsonify({'success': False, 'message': 'Ошибка работы с картой'}), 500


@system_config_api_bp.route('/api/map/<string:filename>', methods=['DELETE'])
def api_map_delete(filename):
    try:
        if not (current_app.config.get('TESTING') or session.get('role') == 'admin'):
            return jsonify({'success': False, 'message': 'Только администратор может удалять карты'}), 403
        safe = secure_filename(filename)
        if safe != filename:
            return jsonify({'success': False, 'message': 'Некорректное имя файла'}), 400
        path = os.path.join(MAP_DIR, safe)
        if not os.path.exists(path):
            return jsonify({'success': False, 'message': 'Файл не найден'}), 404
        os.remove(path)
        return jsonify({'success': True})
    except (IOError, OSError, PermissionError) as e:
        logger.error(f"Ошибка удаления карты: {e}")
        return jsonify({'success': False, 'message': 'Ошибка удаления карты'}), 500


# ===== Rain config =====

@system_config_api_bp.route('/api/rain', methods=['GET', 'POST'])
def api_rain_config():
    try:
        if request.method == 'GET':
            return jsonify({'success': True, 'config': db.get_rain_config()})
        data = request.get_json() or {}
        cfg = {
            'enabled': bool(data.get('enabled')),
            'topic': (data.get('topic') or '').strip(),
            'type': data.get('type') if data.get('type') in ('NO', 'NC') else 'NO',
            'server_id': data.get('server_id')
        }
        if cfg['enabled'] and not cfg['topic']:
            return jsonify({'success': False, 'message': 'Требуется MQTT-топик для датчика дождя'}), 400
        ok = db.set_rain_config(cfg)
        if ok and cfg.get('enabled'):
            try:
                for g in (db.get_groups() or []):
                    gid = int(g.get('id'))
                    if gid == 999:
                        continue
                    db.set_group_use_rain(gid, True)
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in api_rain_config: %s", e)
        return jsonify({'success': bool(ok)})
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"rain config failed: {e}")
        return jsonify({'success': False}), 500


# ===== Env config =====

@system_config_api_bp.route('/api/env', methods=['GET', 'POST'])
def api_env_config():
    try:
        if request.method == 'GET':
            cfg = db.get_env_config()
            values = {'temp': env_monitor.temp_value, 'hum': env_monitor.hum_value}
            return jsonify({'success': True, 'config': cfg, 'values': values})
        data = request.get_json() or {}
        action = data.get('action')
        if action == 'restart':
            try:
                cfg = db.get_env_config()
                env_monitor.start(cfg)
                probe_env_values(cfg)
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in api_env_config: %s", e)
            return jsonify({'success': True})
        try:
            temp_cfg = (data.get('temp') or {})
            hum_cfg = (data.get('hum') or {})
            errors = {}
            if bool(temp_cfg.get('enabled')) and not str(temp_cfg.get('topic') or '').strip():
                errors['temp_topic'] = 'Требуется MQTT-топик для датчика температуры'
            if bool(hum_cfg.get('enabled')) and not str(hum_cfg.get('topic') or '').strip():
                errors['hum_topic'] = 'Требуется MQTT-топик для датчика влажности'
            if errors:
                return jsonify({'success': False, 'errors': errors}), 400
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in api_env_config: %s", e)
        ok = db.set_env_config(data)
        try:
            cfg = db.get_env_config()
            env_monitor.start(cfg)
            probe_env_values(cfg)
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in line_416: %s", e)
        return jsonify({'success': bool(ok)})
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"env config failed: {e}")
        return jsonify({'success': False}), 500


@system_config_api_bp.route('/api/env/values', methods=['GET'])
def api_env_values():
    try:
        cfg = db.get_env_config()
        temp_enabled = bool((cfg.get('temp') or {}).get('enabled'))
        hum_enabled = bool((cfg.get('hum') or {}).get('enabled'))
        temperature = None if not temp_enabled else (env_monitor.temp_value if env_monitor.temp_value is not None else 'нет данных')
        humidity = None if not hum_enabled else (env_monitor.hum_value if env_monitor.hum_value is not None else 'нет данных')
        return jsonify({'success': True, 'temperature': temperature, 'humidity': humidity, 'enabled': {'temp': temp_enabled, 'hum': hum_enabled}})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"env values failed: {e}")
        return jsonify({'success': False}), 500


# ===== Postpone =====

@system_config_api_bp.route('/api/postpone', methods=['POST'])
def api_postpone():
    """Postpone watering."""
    data = request.get_json()
    group_id = data.get('group_id')
    try:
        group_id = int(group_id)
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Exception in api_postpone: %s", e)
        return jsonify({"success": False, "message": "Некорректный идентификатор группы"}), 400
    days = data.get('days', 1)
    action = data.get('action')

    if action == 'cancel':
        zones = db.get_zones()
        group_zones = [z for z in zones if int(z.get('group_id') or 0) == int(group_id)]
        for zone in group_zones:
            db.update_zone_postpone(zone['id'], None, None)
        db.add_log('postpone_cancel', json.dumps({"group": group_id}))
        return jsonify({"success": True, "message": "Отложенный полив отменен"})

    elif action == 'postpone':
        postpone_date = datetime.now() + timedelta(days=days)
        postpone_until = postpone_date.strftime('%Y-%m-%d 23:59:59')
        zones = db.get_zones()
        group_zones = [z for z in zones if int(z.get('group_id') or 0) == int(group_id)]
        for zone in group_zones:
            db.update_zone_postpone(zone['id'], postpone_until, 'manual')
        try:
            for zone in group_zones:
                try:
                    if (zone.get('state') == 'on') or zone.get('watering_start_time'):
                        db.update_zone(zone['id'], {'state': 'off', 'watering_start_time': None})
                        sid = zone.get('mqtt_server_id')
                        topic = (zone.get('topic') or '').strip()
                        if mqtt and sid and topic:
                            t = normalize_topic(topic)
                            server = db.get_mqtt_server(int(sid))
                            if server:
                                _publish_mqtt_value(server, t, '0', min_interval_sec=0.0, qos=2, retain=True)
                except (ConnectionError, TimeoutError, OSError):
                    logger.exception("Ошибка остановки зоны при установке отложенного полива")
            try:
                scheduler = get_scheduler()
                if scheduler:
                    scheduler.cancel_group_jobs(group_id)
            except (ValueError, KeyError, RuntimeError):
                logger.exception("Ошибка отмены заданий планировщика при отложенном поливе группы")
        except (ConnectionError, TimeoutError, OSError):
            logger.exception("Ошибка массовой остановки зон при отложенном поливе группы")
        db.add_log('postpone_set', json.dumps({"group": group_id, "days": days, "until": postpone_until}))
        return jsonify({"success": True, "message": f"Полив отложен на {days} дней", "postpone_until": postpone_date.strftime('%Y-%m-%d %H:%M:%S')})

    return jsonify({"success": False, "message": "Неверное действие"}), 400


# ===== Settings =====

@system_config_api_bp.route('/api/settings/early-off', methods=['GET', 'POST'])
def api_setting_early_off():
    try:
        if request.method == 'GET':
            seconds = db.get_early_off_seconds()
            return jsonify({'success': True, 'seconds': seconds})
        data = request.get_json(silent=True) or {}
        seconds = int(data.get('seconds', 3))
        if seconds < 0 or seconds > 15:
            return jsonify({'success': False, 'message': 'seconds must be within 0..15'}), 400
        ok = db.set_early_off_seconds(seconds)
        return jsonify({'success': bool(ok), 'seconds': seconds})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"early-off setting failed: {e}")
        from services.helpers import api_error
        return api_error('INTERNAL_ERROR', 'internal error', 500)


@system_config_api_bp.route('/api/settings/system-name', methods=['GET', 'POST'])
def api_setting_system_name():
    try:
        if request.method == 'GET':
            name = db.get_setting_value('system_name') or ''
            return jsonify({'success': True, 'name': name})
        if not (current_app.config.get('TESTING') or session.get('role') == 'admin'):
            return jsonify({'success': False, 'message': 'admin required'}), 403
        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        ok = db.set_setting_value('system_name', name if name else None)
        return jsonify({'success': bool(ok), 'name': name})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"system-name setting failed: {e}")
        from services.helpers import api_error
        return api_error('INTERNAL_ERROR', 'internal error', 500)


# ===== Logging debug toggle =====

@system_config_api_bp.route('/api/logging/debug', methods=['GET', 'POST'])
def api_logging_debug_toggle():
    try:
        if request.method == 'POST':
            payload = request.get_json(force=True, silent=True) or {}
            enable = bool(payload.get('enabled'))
            db.set_logging_debug(enable)
            # Apply runtime log level
            try:
                is_debug = db.get_logging_debug()
                level = logging.DEBUG if is_debug else logging.WARNING
                root = logging.getLogger()
                root.setLevel(level)
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in api_logging_debug_toggle: %s", e)
        return jsonify({'debug': db.get_logging_debug()})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"api_logging_debug_toggle error: {e}")
        return jsonify({'debug': db.get_logging_debug()}), 500
