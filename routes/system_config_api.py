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
from services.api_rate_limiter import rate_limit
from services.audit import audit_log
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


@system_config_api_bp.route('/logout', methods=['GET', 'POST'])
@audit_log('logout', target_extractor=lambda *a, **kw: 'session')
def api_logout():
    """Terminate the current session.

    Security fixes:
      * SEC-007: `session.clear()` fully destroys the server-side session
        payload AND forces Flask to rotate the signed cookie. The previous
        implementation left `logged_in=False` but kept role='user' which
        (via the `_is_status_action` whitelist) still allowed mutating
        calls on zone/group control endpoints.
      * SEC-008: GET-based logout was CSRF-able (e.g. `<img src=/logout>`
        in email/IM would forcibly log admin out). The GET variant is
        preserved for backward compatibility with the existing link in
        the sidebar template, but mutating side-effects are now identical
        and the cookie is rotated. New integrations should POST to
        `/logout`.
    """
    # Capture whether the session had any sign-in so audit logs make
    # sense, but never log the role/user because that is PII-adjacent.
    was_logged_in = bool(session.get('logged_in'))
    session.clear()
    # Force-invalidate any Flask-Session server-side entry too, if present.
    try:
        session.modified = True
    except (AttributeError, TypeError):
        pass
    logger.info("logout: session cleared (was_logged_in=%s)", was_logged_in)
    return redirect(url_for('auth_bp.login_page'))


@system_config_api_bp.route('/api/password', methods=['POST'])
@rate_limit('password_change', max_requests=3, window_sec=300)
@audit_log('password_change',
           target_extractor=lambda *a, **kw: 'admin',
           payload_filter=lambda p: {'changed': True})
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
@audit_log('map_upload', target_extractor=lambda *a, **kw: 'map')
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
@audit_log('map_delete',
           target_extractor=lambda *a, **kw: f"map:{kw.get('filename', a[0] if a else '?')}")
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
@audit_log('rain_config_save', target_extractor=lambda *a, **kw: 'rain_config')
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
@audit_log('env_config_save', target_extractor=lambda *a, **kw: 'env_config')
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
@audit_log('postpone_action', target_extractor=lambda *a, **kw: 'group')
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
                        # Postpone applies to a group — each zone going OFF
                        # is an audited transition (operator action via
                        # /api/postpone) that we want to see in audit_log.
                        try:
                            from services.zones_state import update_zone_state as _uzs
                            _uzs(zone['id'],
                                 {'state': 'off', 'watering_start_time': None},
                                 audit_reason='postpone_action')
                        except (sqlite3.Error, OSError, ImportError):
                            logger.exception(
                                "system_config.postpone: audited path failed zone=%s — "
                                "falling back to raw update_zone", zone.get('id'),
                            )
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
@audit_log('setting_early_off', target_extractor=lambda *a, **kw: 'setting:early_off')
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
@audit_log('setting_system_name', target_extractor=lambda *a, **kw: 'setting:system_name')
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

def _disable_debug_logging_job():
    """APScheduler job: turn off DEBUG mode automatically.

    Persists 'logging.debug=0' to settings, drops root logger to WARNING,
    records an audit event so operators can see the auto-off in /logs.
    Best-effort — never raises.
    """
    try:
        db.set_logging_debug(False)
        # Invalidate the debug_audit() TTL cache so the flip takes effect
        # immediately for high-volume diagnostic emits (mqtt_publish, scheduler
        # timers) instead of waiting up to ~5s for the cache to expire.
        try:
            from services.audit import invalidate_debug_audit_cache
            invalidate_debug_audit_cache()
        except (ImportError, RuntimeError) as e:
            logger.debug("auto-off: invalidate_debug_audit_cache failed: %s", e)
        try:
            logging.getLogger().setLevel(logging.WARNING)
        except (TypeError, ValueError) as e:
            logger.debug("auto-off: setLevel failed: %s", e)
        try:
            from services.audit import record_audit
            record_audit(
                action_type='debug_log_auto_off',
                source='scheduler',
                target='logging:debug',
                payload={'auto_off': True},
                actor='system',
            )
        except (ImportError, RuntimeError) as e:
            logger.debug("auto-off: record_audit failed: %s", e)
        logger.info("debug logging auto-off triggered (job=debug_auto_off)")
    except (sqlite3.Error, OSError) as e:
        logger.warning("debug_auto_off job failed: %s", e)


@system_config_api_bp.route('/api/logging/debug', methods=['GET', 'POST'])
@audit_log('debug_log_toggle', target_extractor=lambda *a, **kw: 'logging:debug')
def api_logging_debug_toggle():
    """Toggle DEBUG-level logging (Level 2 — operational debug).

    POST body:
        {"enabled": true|false, "auto_off_minutes": 60}

    `auto_off_minutes` is optional (1..720 = 12h). When supplied with
    enabled=true, schedules a one-shot APScheduler DateTrigger to flip
    the flag back off — protects against operators forgetting to disable
    debug mode and filling the disk with logs.
    """
    try:
        if request.method == 'POST':
            payload = request.get_json(force=True, silent=True) or {}
            enable = bool(payload.get('enabled'))
            try:
                auto_off_min = payload.get('auto_off_minutes')
                auto_off_min = int(auto_off_min) if auto_off_min is not None else None
                if auto_off_min is not None and (auto_off_min < 1 or auto_off_min > 720):
                    auto_off_min = max(1, min(720, auto_off_min))
            except (TypeError, ValueError):
                auto_off_min = None
            db.set_logging_debug(enable)
            # Invalidate debug_audit() TTL cache so manual toggle takes effect
            # immediately instead of waiting for the ~5s cache to expire.
            try:
                from services.audit import invalidate_debug_audit_cache
                invalidate_debug_audit_cache()
            except (ImportError, RuntimeError) as e:
                logger.debug("debug toggle: invalidate_debug_audit_cache failed: %s", e)
            # Apply runtime log level
            try:
                is_debug = db.get_logging_debug()
                level = logging.DEBUG if is_debug else logging.WARNING
                root = logging.getLogger()
                root.setLevel(level)
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in api_logging_debug_toggle: %s", e)
            # Manage the auto-off job
            try:
                from apscheduler.triggers.date import DateTrigger
                sched = get_scheduler()
                if sched and getattr(sched, 'scheduler', None):
                    # Remove any pending auto-off first
                    try:
                        sched.scheduler.remove_job('debug_auto_off')
                    except (ValueError, KeyError):
                        pass  # not scheduled — fine
                    if enable and auto_off_min:
                        run_at = datetime.now() + timedelta(minutes=auto_off_min)
                        sched.scheduler.add_job(
                            _disable_debug_logging_job,
                            trigger=DateTrigger(run_date=run_at),
                            id='debug_auto_off',
                            replace_existing=True,
                            coalesce=True,
                            max_instances=1,
                        )
                        logger.info("debug logging auto-off scheduled for %s (in %d min)",
                                    run_at.isoformat(timespec='seconds'), auto_off_min)
            except (ImportError, RuntimeError, KeyError, ValueError) as e:
                logger.warning("Failed to (re)schedule debug auto-off: %s", e)
        # GET (and POST response): include auto_off info if known
        info = {'debug': db.get_logging_debug()}
        try:
            sched = get_scheduler()
            if sched and getattr(sched, 'scheduler', None):
                job = sched.scheduler.get_job('debug_auto_off')
                if job and job.next_run_time:
                    info['auto_off_at'] = job.next_run_time.isoformat(timespec='seconds')
        except (ImportError, RuntimeError, AttributeError):
            pass
        return jsonify(info)
    except (sqlite3.Error, OSError) as e:
        logger.error(f"api_logging_debug_toggle error: {e}")
        return jsonify({'debug': db.get_logging_debug()}), 500
