"""System API blueprint — all remaining /api/* endpoints."""
from flask import Blueprint, request, jsonify, current_app, session, redirect, url_for
from datetime import datetime, timedelta
import json
import os
import time
import logging

from database import db
from utils import normalize_topic
from irrigation_scheduler import init_scheduler, get_scheduler
from services.mqtt_pub import publish_mqtt_value as _publish_mqtt_value
from services.helpers import api_error, api_soft, parse_dt, MAP_DIR, ALLOWED_MIME_TYPES
from services.security import admin_required
from services.monitors import rain_monitor, env_monitor, water_monitor, probe_env_values
from constants import MIN_PASSWORD_LENGTH
from services.locks import snapshot_all_locks as _locks_snapshot
from services import sse_hub as _sse_hub
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash

try:
    import paho.mqtt.client as mqtt
except Exception as e:
    logger.debug("Exception in line_23: %s", e)
    mqtt = None

try:
    from services import events as _events
except Exception as e:
    logger.debug("Exception in line_29: %s", e)
    _events = None

logger = logging.getLogger(__name__)

system_api_bp = Blueprint('system_api', __name__)

# Password blocklist (TASK-013)
_PASSWORD_BLOCKLIST = {'1234', '12345678', '0000', 'password', 'admin', 'qwerty'}


# ===== Health / Scheduler =====

@system_api_bp.route('/api/health-details')
@admin_required
def api_health_details():
    try:
        sched = get_scheduler()
        jobs = []
        if sched is not None and getattr(sched, 'scheduler', None) is not None:
            try:
                for j in sched.scheduler.get_jobs():
                    try:
                        nrt = getattr(j, 'next_run_time', None)
                        jid = str(j.id)
                        jstore = 'default' if jid.startswith('program:') else 'volatile'
                        trig = str(getattr(j, 'trigger', ''))
                        jobs.append({
                            'id': jid, 'name': str(getattr(j, 'name', '')),
                            'next_run_time': nrt.isoformat() if nrt else None,
                            'jobstore': jstore, 'trigger': trig,
                        })
                    except Exception as e:
                        logger.debug("Exception in api_health_details: %s", e)
                        continue
            except Exception as e:
                logger.debug("Handled exception in api_health_details: %s", e)
        zones = []
        try:
            for z in db.get_zones():
                try:
                    state = str(z.get('state') or '')
                    cstate = str(z.get('commanded_state') or '')
                    if state != 'off' or cstate in ('starting', 'on', 'stopping'):
                        zones.append({
                            'id': int(z.get('id')),
                            'group_id': int(z.get('group_id') or 0),
                            'state': state, 'commanded_state': cstate,
                            'observed_state': str(z.get('observed_state') or ''),
                            'sequence_id': z.get('sequence_id'),
                            'command_id': z.get('command_id'),
                            'version': z.get('version'),
                            'planned_end_time': z.get('planned_end_time'),
                        })
                except Exception as e:
                    logger.debug("Exception in line_84: %s", e)
                    continue
        except Exception as e:
            logger.debug("Handled exception in line_87: %s", e)
        locks = _locks_snapshot()
        group_cancels = []
        try:
            if hasattr(sched, 'group_cancel_events'):
                for gid, ev in (sched.group_cancel_events or {}).items():
                    try:
                        group_cancels.append({'group_id': int(gid), 'set': bool(ev.is_set())})
                    except Exception as e:
                        logger.debug("Exception in line_96: %s", e)
                        continue
        except Exception as e:
            logger.debug("Handled exception in line_99: %s", e)
        try:
            meta_tail = _sse_hub.get_meta_buffer()
        except Exception as e:
            logger.debug("Exception in line_103: %s", e)
            meta_tail = []
        payload = {
            'now': datetime.now().isoformat(timespec='seconds'),
            'scheduler_running': bool(sched and sched.is_running),
            'jobs': jobs, 'zones': zones, 'locks': locks,
            'group_cancels': group_cancels, 'meta_tail': meta_tail,
        }
        return jsonify(payload)
    except Exception as e:
        logger.exception('health-details failed')
        return api_error('health_details_failed', f'health details error: {e}', 500)


@system_api_bp.route('/api/health/job/<path:job_id>/cancel', methods=['POST'])
@admin_required
def api_health_cancel_job(job_id):
    try:
        sched = get_scheduler()
        if not sched or not getattr(sched, 'scheduler', None):
            return api_error('scheduler_unavailable', 'scheduler unavailable', 503)
        try:
            sched.scheduler.remove_job(str(job_id))
            return jsonify({'success': True, 'message': f'job {job_id} removed'})
        except Exception as e:
            logger.debug("Exception in api_health_cancel_job: %s", e)
            return api_error('job_remove_failed', f'failed to remove job: {e}', 400)
    except Exception as e:
        logger.exception('cancel job failed')
        return api_error('cancel_job_failed', f'error: {e}', 500)


@system_api_bp.route('/api/health/group/<int:group_id>/cancel', methods=['POST'])
@admin_required
def api_health_cancel_group(group_id):
    try:
        sched = get_scheduler()
        if not sched:
            return api_error('scheduler_unavailable', 'scheduler unavailable', 503)
        try:
            if hasattr(sched, 'group_cancel_events'):
                import threading as _th
                ev = sched.group_cancel_events.get(int(group_id)) or _th.Event()
                ev.set()
                sched.group_cancel_events[int(group_id)] = ev
            if hasattr(sched, 'cancel_group_jobs'):
                sched.cancel_group_jobs(int(group_id))
        except Exception:
            logger.exception('group cancel failed')
            return api_error('group_cancel_failed', 'failed to cancel group jobs', 400)
        return jsonify({'success': True, 'message': f'group {group_id} cancelled'})
    except Exception as e:
        logger.exception('cancel group failed')
        return api_error('cancel_group_failed', f'error: {e}', 500)


@system_api_bp.route('/api/scheduler/init', methods=['POST'])
def api_scheduler_init():
    """Explicit scheduler init for UI/tests."""
    try:
        init_scheduler(db)
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Ошибка явной инициализации планировщика: {e}")
        return api_error('INTERNAL_ERROR', 'internal error', 500)


@system_api_bp.route('/api/scheduler/status')
def api_scheduler_status():
    """Get scheduler status."""
    try:
        scheduler = get_scheduler()
        if not scheduler:
            return jsonify({'error': 'Планировщик не инициализирован'}), 500
        active_programs = scheduler.get_active_programs()
        active_zones = scheduler.get_active_zones()
        return jsonify({
            'active_programs': active_programs,
            'active_zones': {str(k): v.isoformat() for k, v in active_zones.items()},
            'is_running': scheduler.is_running
        })
    except Exception as e:
        logger.error(f"Ошибка получения статуса планировщика: {e}")
        return jsonify({'error': 'Ошибка получения статуса'}), 500


@system_api_bp.route('/api/scheduler/jobs')
def api_scheduler_jobs():
    try:
        sched = get_scheduler()
        if not sched:
            return jsonify({'success': False, 'message': 'scheduler not running', 'jobs': []}), 200
        jobs = []
        for j in sched.scheduler.get_jobs():
            try:
                jobs.append({
                    'id': j.id,
                    'next_run_time': None if j.next_run_time is None else j.next_run_time.strftime('%Y-%m-%d %H:%M:%S'),
                    'name': getattr(j, 'name', ''),
                })
            except Exception as e:
                logger.debug("Exception in api_scheduler_jobs: %s", e)
                continue
        return jsonify({'success': True, 'jobs': jobs})
    except Exception as e:
        logger.error(f"scheduler jobs list failed: {e}")
        return jsonify({'success': False, 'jobs': []}), 200


# ===== Auth / Password =====

@system_api_bp.route('/api/auth/status')
def api_auth_status():
    return jsonify({
        'authenticated': bool(session.get('logged_in')) or bool(current_app.config.get('TESTING')),
        'role': session.get('role', 'guest')
    })


@system_api_bp.route('/logout', methods=['GET'])
def api_logout():
    session['logged_in'] = False
    session['role'] = 'user'
    return redirect(url_for('auth_bp.login_page'))


@system_api_bp.route('/api/password', methods=['POST'])
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
    except Exception as e:
        logger.error(f"Ошибка смены пароля: {e}")
        return jsonify({'success': False, 'message': 'Ошибка смены пароля'}), 500


# ===== Map =====

@system_api_bp.route('/api/map', methods=['GET', 'POST'])
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
                except Exception as e:
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
    except Exception as e:
        logger.error(f"Ошибка работы с картой зон: {e}")
        return jsonify({'success': False, 'message': 'Ошибка работы с картой'}), 500


@system_api_bp.route('/api/map/<string:filename>', methods=['DELETE'])
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
    except Exception as e:
        logger.error(f"Ошибка удаления карты: {e}")
        return jsonify({'success': False, 'message': 'Ошибка удаления карты'}), 500


# ===== Health check =====

@system_api_bp.route('/health')
def health_check():
    try:
        try:
            _ = db.get_zones()
            db_ok = True
        except Exception as e:
            logger.debug("Exception in health_check: %s", e)
            db_ok = False
        try:
            sched = get_scheduler()
            sched_ok = bool(sched is not None)
        except Exception as e:
            logger.debug("Exception in health_check: %s", e)
            sched_ok = False
        try:
            servers = db.get_mqtt_servers() or []
            mqtt_ok = bool(len(servers) >= 0)
        except Exception as e:
            logger.debug("Exception in health_check: %s", e)
            mqtt_ok = False
        overall = db_ok and sched_ok
        code = 200 if overall else 503
        return jsonify({'ok': overall, 'db': db_ok, 'scheduler': sched_ok, 'mqtt_configured': mqtt_ok}), code
    except Exception as e:
        logger.exception('health check failed')
        return jsonify({'ok': False, 'error': str(e)}), 500


# ===== Rain config =====

@system_api_bp.route('/api/rain', methods=['GET', 'POST'])
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
            except Exception as e:
                logger.debug("Handled exception in api_rain_config: %s", e)
        return jsonify({'success': bool(ok)})
    except Exception as e:
        logger.error(f"rain config failed: {e}")
        return jsonify({'success': False}), 500


# ===== Env config =====

@system_api_bp.route('/api/env', methods=['GET', 'POST'])
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
            except Exception as e:
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
        except Exception as e:
            logger.debug("Handled exception in api_env_config: %s", e)
        ok = db.set_env_config(data)
        try:
            cfg = db.get_env_config()
            env_monitor.start(cfg)
            probe_env_values(cfg)
        except Exception as e:
            logger.debug("Handled exception in line_416: %s", e)
        return jsonify({'success': bool(ok)})
    except Exception as e:
        logger.error(f"env config failed: {e}")
        return jsonify({'success': False}), 500


@system_api_bp.route('/api/env/values', methods=['GET'])
def api_env_values():
    try:
        cfg = db.get_env_config()
        temp_enabled = bool((cfg.get('temp') or {}).get('enabled'))
        hum_enabled = bool((cfg.get('hum') or {}).get('enabled'))
        temperature = None if not temp_enabled else (env_monitor.temp_value if env_monitor.temp_value is not None else 'нет данных')
        humidity = None if not hum_enabled else (env_monitor.hum_value if env_monitor.hum_value is not None else 'нет данных')
        return jsonify({'success': True, 'temperature': temperature, 'humidity': humidity, 'enabled': {'temp': temp_enabled, 'hum': hum_enabled}})
    except Exception as e:
        logger.error(f"env values failed: {e}")
        return jsonify({'success': False}), 500


# ===== Postpone =====

@system_api_bp.route('/api/postpone', methods=['POST'])
def api_postpone():
    """Postpone watering."""
    data = request.get_json()
    group_id = data.get('group_id')
    try:
        group_id = int(group_id)
    except Exception as e:
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
                except Exception:
                    logger.exception("Ошибка остановки зоны при установке отложенного полива")
            try:
                scheduler = get_scheduler()
                if scheduler:
                    scheduler.cancel_group_jobs(group_id)
            except Exception:
                logger.exception("Ошибка отмены заданий планировщика при отложенном поливе группы")
        except Exception:
            logger.exception("Ошибка массовой остановки зон при отложенном поливе группы")
        db.add_log('postpone_set', json.dumps({"group": group_id, "days": days, "until": postpone_until}))
        return jsonify({"success": True, "message": f"Полив отложен на {days} дней", "postpone_until": postpone_date.strftime('%Y-%m-%d %H:%M:%S')})

    return jsonify({"success": False, "message": "Неверное действие"}), 400


# ===== Emergency =====

@system_api_bp.route('/api/emergency-stop', methods=['POST'])
def api_emergency_stop():
    """Emergency stop all zones."""
    try:
        try:
            from services.zone_control import stop_all_in_group as _stop_all
            groups = db.get_groups() or []
            for g in groups:
                try:
                    _stop_all(int(g['id']), reason='emergency_stop', force=True)
                except Exception:
                    logger.exception('emergency stop: stop_all_in_group failed')
        except Exception:
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
                    except Exception as e:
                        logger.debug("Handled exception in api_emergency_stop: %s", e)
        except Exception as e:
            logger.debug("Handled exception in api_emergency_stop: %s", e)
        try:
            if _events:
                _events.publish({'type': 'emergency_on', 'by': 'api'})
        except Exception as e:
            logger.debug("Handled exception in api_emergency_stop: %s", e)
        return jsonify({"success": True, "message": "Аварийная остановка выполнена"})
    except Exception as e:
        logger.error(f"Ошибка аварийной остановки: {e}")
        return jsonify({"success": False, "message": "Ошибка аварийной остановки"}), 500


@system_api_bp.route('/api/emergency-resume', methods=['POST'])
def api_emergency_resume():
    """Resume after emergency stop."""
    try:
        current_app.config['EMERGENCY_STOP'] = False
        db.add_log('emergency_stop', json.dumps({"active": False}))
        try:
            if _events:
                _events.publish({'type': 'emergency_off', 'by': 'api'})
        except Exception as e:
            logger.debug("Handled exception in api_emergency_resume: %s", e)
        return jsonify({"success": True, "message": "Полив возобновлен"})
    except Exception as e:
        logger.error(f"Ошибка возобновления после аварийной остановки: {e}")
        return jsonify({"success": False, "message": "Ошибка возобновления"}), 500


# ===== Backup =====

@system_api_bp.route('/api/backup', methods=['POST'])
def api_backup():
    try:
        backup_path = db.create_backup()
        if backup_path:
            return jsonify({"success": True, "message": "Резервная копия создана", "backup_path": backup_path})
        else:
            return jsonify({"success": False, "message": "Ошибка создания резервной копии"}), 500
    except Exception as e:
        logger.debug("Exception in api_backup: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500


# ===== Water usage =====

@system_api_bp.route('/api/water')
def api_water():
    """Water usage data — real data from DB or empty arrays."""
    try:
        groups = db.get_groups()
        water_data = {}
        for group in groups:
            if group['id'] >= 999:
                continue
            group_id = str(group['id'])
            daily_usage = []
            total_liters = 0
            zone_usage = {}
            try:
                zones = db.get_zones_by_group(group['id'])
                # Try to get real water usage data
                try:
                    usage = db.get_water_usage(group['id']) if hasattr(db, 'get_water_usage') else None
                except Exception as e:
                    logger.debug("Exception in api_water: %s", e)
                    usage = None
                if usage:
                    daily_usage = usage.get('daily_usage', [])
                    total_liters = usage.get('total_liters', 0)
                    zone_usage = usage.get('zone_usage', {})
                else:
                    # Return empty/zero data instead of random
                    for zone in zones:
                        zone_usage[str(zone['id'])] = {
                            'name': zone['name'],
                            'liters': 0,
                            'last_used': None
                        }
                    for i in range(7):
                        date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
                        daily_usage.append({'date': date, 'liters': 0})
                water_data[group_id] = {
                    'group_name': group['name'],
                    'data': {
                        'daily_usage': daily_usage,
                        'total_liters': total_liters,
                        'zone_usage': zone_usage
                    }
                }
            except Exception as e:
                logger.error(f"Ошибка обработки группы {group['id']}: {e}")
                continue
        return jsonify(water_data)
    except Exception as e:
        logger.error(f"Ошибка получения данных о воде: {e}")
        return jsonify({'error': 'Ошибка получения данных о воде'}), 500


# ===== Server time =====

@system_api_bp.route('/api/server-time')
def api_server_time():
    try:
        now = datetime.now()
        try:
            tzname = time.tzname[0] if time.tzname else ''
        except Exception as e:
            logger.debug("Exception in api_server_time: %s", e)
            tzname = ''
        payload = {'now_iso': now.strftime('%Y-%m-%d %H:%M:%S'), 'epoch_ms': int(time.time() * 1000), 'tz': tzname}
        resp = jsonify(payload)
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    except Exception as e:
        logger.error(f"server-time failed: {e}")
        return jsonify({'now_iso': None, 'epoch_ms': int(time.time() * 1000)}), 200


# ===== Status (big endpoint) =====

@system_api_bp.route('/api/status')
def api_status():
    rain_cfg = db.get_rain_config()
    zones = db.get_zones()
    groups = db.get_groups()

    zones_by_group = {}
    for zone in zones:
        group_id = zone['group_id']
        if group_id == 999:
            continue
        if group_id not in zones_by_group:
            zones_by_group[group_id] = []
        zones_by_group[group_id].append(zone)

    groups_status = []
    for group in groups:
        group_id = group['id']
        if group_id == 999:
            continue
        group_zones = zones_by_group.get(group_id, [])
        if not group_zones:
            continue

        active_zones = [z for z in group_zones if z['state'] == 'on']
        postponed_zones = []
        for z in group_zones:
            pu = z.get('postpone_until')
            if not pu:
                continue
            try:
                pu_dt = datetime.strptime(pu, '%Y-%m-%d %H:%M')
                if pu_dt > datetime.now():
                    postponed_zones.append(z)
            except Exception as e:
                logger.debug("Exception in line_679: %s", e)
                postponed_zones.append(z)

        if current_app.config.get('EMERGENCY_STOP'):
            status = 'postponed'
            current_zone = None
        elif active_zones:
            status = 'watering'
            current_zone = active_zones[0]['id']
        elif postponed_zones:
            status = 'postponed'
            current_zone = None
        else:
            status = 'waiting'
            current_zone = None

        next_start = None
        if group_zones:
            programs = db.get_programs()
            group_programs = []
            for program in programs:
                if isinstance(program['zones'], str):
                    program_zones = json.loads(program['zones'])
                else:
                    program_zones = program['zones']
                group_zone_ids = [z['id'] for z in group_zones]
                if any(zone_id in group_zone_ids for zone_id in program_zones):
                    group_programs.append(program)
            if group_programs:
                search_from = datetime.now()
                try:
                    pu_candidates = []
                    for z in group_zones:
                        pu = z.get('postpone_until')
                        if pu:
                            pu_dt = parse_dt(pu)
                            if pu_dt and pu_dt > search_from:
                                pu_candidates.append(pu_dt)
                    if pu_candidates:
                        search_from = max(pu_candidates)
                except Exception as e:
                    logger.debug("Handled exception in line_720: %s", e)
                best_dt = None
                for program in group_programs:
                    program_time = datetime.strptime(program['time'], '%H:%M').time()
                    program_zones_list = program['zones'] if isinstance(program['zones'], list) else json.loads(program['zones'])
                    group_zone_ids = [z['id'] for z in group_zones]
                    if not any(zid in group_zone_ids for zid in program_zones_list):
                        continue
                    prog_weekdays = set(int(d) for d in (program['days'] if isinstance(program['days'], list) else json.loads(program['days'])))
                    for add_days in range(0, 14):
                        day_date = search_from.date() + timedelta(days=add_days)
                        if ((day_date.weekday() + 0) % 7) not in prog_weekdays:
                            continue
                        dt_candidate = datetime.combine(day_date, program_time)
                        if dt_candidate > search_from and (best_dt is None or dt_candidate < best_dt):
                            best_dt = dt_candidate
                            break
                if best_dt:
                    next_start = best_dt.strftime('%H:%M')

        postpone_until = None
        group_postpone_reason = None
        if current_app.config.get('EMERGENCY_STOP'):
            postpone_until = 'До отмены аварийной остановки'
            group_postpone_reason = 'emergency'
        elif postponed_zones:
            postpone_until = postponed_zones[0].get('postpone_until')
            try:
                reasons = [z.get('postpone_reason') for z in postponed_zones if z.get('postpone_reason')]
                if 'manual' in reasons:
                    group_postpone_reason = 'manual'
                elif reasons:
                    group_postpone_reason = reasons[0]
            except Exception as e:
                logger.debug("Handled exception in line_754: %s", e)

        current_zone_source = None
        try:
            if status == 'watering' and current_zone:
                cz = next((z for z in group_zones if int(z['id']) == int(current_zone)), None)
                if cz:
                    src = (cz.get('watering_start_source') or '').strip().lower()
                    if src in ('manual', 'schedule', 'remote'):
                        current_zone_source = src
                    else:
                        current_zone_source = 'remote'
        except Exception as e:
            logger.debug("Handled exception in line_767: %s", e)

        try:
            use_master_valve = bool(int(group.get('use_master_valve') or 0))
        except Exception as e:
            logger.debug("Exception in line_772: %s", e)
            use_master_valve = False
        try:
            mvo = (group.get('master_valve_observed') or '').strip()
            master_valve_state = mvo if mvo in ('open', 'closed') else 'unknown'
        except Exception as e:
            logger.debug("Exception in line_778: %s", e)
            master_valve_state = 'unknown'
        try:
            use_pressure_sensor = bool(int(group.get('use_pressure_sensor') or 0))
        except Exception as e:
            logger.debug("Exception in line_783: %s", e)
            use_pressure_sensor = False
        try:
            use_water_meter = bool(int(group.get('use_water_meter') or 0))
        except Exception as e:
            logger.debug("Exception in line_788: %s", e)
            use_water_meter = False
        pressure_unit = (group.get('pressure_unit') or 'bar') if use_pressure_sensor else None
        pressure_value = None
        meter_value_m3 = None
        flow_value = None
        if use_water_meter:
            try:
                meter_value_m3 = water_monitor.get_current_reading_m3(int(group_id))
                start_iso = None
                if status == 'watering' and current_zone:
                    try:
                        cz = next((z for z in group_zones if int(z['id']) == int(current_zone)), None)
                        start_iso = cz.get('watering_start_time') if cz else None
                    except Exception as e:
                        logger.debug("Exception in line_803: %s", e)
                        start_iso = None
                flow_value = water_monitor.get_flow_lpm(int(group_id), start_iso)
            except Exception as e:
                logger.debug("Exception in line_807: %s", e)
                meter_value_m3 = None
                flow_value = None

        groups_status.append({
            'id': group_id, 'name': group['name'], 'status': status,
            'current_zone': current_zone, 'postpone_until': postpone_until,
            'next_start': next_start, 'postpone_reason': group_postpone_reason,
            'was_postponed': bool(postponed_zones), 'current_zone_source': current_zone_source,
            'use_master_valve': use_master_valve, 'master_valve_state': master_valve_state,
            'use_pressure_sensor': use_pressure_sensor, 'pressure_value': pressure_value,
            'pressure_unit': pressure_unit, 'use_water_meter': use_water_meter,
            'flow_value': flow_value, 'meter_value_m3': meter_value_m3
        })

    if not rain_cfg.get('enabled'):
        rain_sensor_status = 'выключен'
    else:
        try:
            if hasattr(rain_monitor, 'is_rain') and rain_monitor.is_rain is not None:
                rain_sensor_status = 'идёт дождь' if rain_monitor.is_rain else 'дождя нет'
            else:
                rain_sensor_status = 'дождя нет'
        except Exception as e:
            logger.debug("Exception in line_831: %s", e)
            rain_sensor_status = 'дождя нет'

    env_cfg = db.get_env_config()
    temp_enabled = bool(env_cfg.get('temp', {}).get('enabled'))
    hum_enabled = bool(env_cfg.get('hum', {}).get('enabled'))
    temperature = None if not temp_enabled else (env_monitor.temp_value if env_monitor.temp_value is not None else 'нет данных')
    humidity = None if not hum_enabled else (env_monitor.hum_value if env_monitor.hum_value is not None else 'нет данных')

    try:
        servers = db.get_mqtt_servers()
    except Exception as e:
        logger.debug("Exception in line_843: %s", e)
        servers = []
    mqtt_servers_count = len(servers)
    enabled_servers = [s for s in servers if int(s.get('enabled') or 0) == 1]
    mqtt_enabled_count = len(enabled_servers)
    mqtt_connected = False
    try:
        if current_app.config.get('TESTING'):
            mqtt_connected = True  # Skip real MQTT in tests
        elif mqtt_servers_count > 0 and mqtt is not None:
            candidates = enabled_servers if mqtt_enabled_count > 0 else servers
            for s in candidates:
                try:
                    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=(s.get('client_id') or None))
                    if s.get('username'):
                        client.username_pw_set(s.get('username'), s.get('password') or None)
                    client.connect(s.get('host') or '127.0.0.1', int(s.get('port') or 1883), 3)
                    mqtt_connected = True
                    try:
                        client.disconnect()
                    except Exception as e:
                        logger.debug("Handled exception in line_862: %s", e)
                    break
                except Exception as e:
                    logger.debug("Exception in line_865: %s", e)
                    mqtt_connected = False
        if mqtt_servers_count == 0:
            try:
                logger.warning('MQTT: нет ни одного сервера в настройках')
            except Exception as e:
                logger.debug("Handled exception in line_871: %s", e)
            try:
                db.add_log('mqtt_warn', 'нет ни одного сервера в настройках')
            except Exception as e:
                logger.debug("Handled exception in line_875: %s", e)
        elif not mqtt_connected:
            try:
                logger.warning('MQTT: нет связи ни с одним сервером')
            except Exception as e:
                logger.debug("Handled exception in line_880: %s", e)
            try:
                db.add_log('mqtt_warn', 'нет связи ни с одним MQTT сервером')
            except Exception as e:
                logger.debug("Handled exception in line_884: %s", e)
    except Exception as e:
        logger.debug("Handled exception in line_886: %s", e)

    logger.info(f"api_status: temp={temperature} hum={humidity} temp_enabled={temp_enabled} hum_enabled={hum_enabled}")
    try:
        role = session.get('role')
        is_admin = (role == 'admin')
    except Exception as e:
        logger.debug("Exception in line_893: %s", e)
        is_admin = False
    return jsonify({
        'datetime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'temperature': temperature, 'humidity': humidity,
        'rain_enabled': bool(rain_cfg.get('enabled')), 'rain_sensor': rain_sensor_status,
        'groups': groups_status,
        'emergency_stop': current_app.config.get('EMERGENCY_STOP', False),
        'is_admin': is_admin,
        'mqtt_servers_count': mqtt_servers_count,
        'mqtt_enabled_count': mqtt_enabled_count,
        'mqtt_connected': mqtt_connected
    })


# ===== Logs =====

@system_api_bp.route('/api/logs')
def api_logs():
    try:
        from_date = request.args.get('from')
        to_date = request.args.get('to')
        event_type = request.args.get('type')
        logs = db.get_logs()
        if from_date or to_date or event_type:
            filtered_logs = []
            for log in logs:
                if event_type and log['type'] != event_type:
                    continue
                if from_date or to_date:
                    try:
                        log_date = datetime.strptime(log['timestamp'][:10], '%Y-%m-%d').date()
                        if from_date:
                            from_dt = datetime.strptime(from_date, '%Y-%m-%d').date()
                            if log_date < from_dt:
                                continue
                        if to_date:
                            to_dt = datetime.strptime(to_date, '%Y-%m-%d').date()
                            if log_date > to_dt:
                                continue
                    except Exception as e:
                        logger.debug("Bare exception in api_logs: %s", e)
                        continue
                filtered_logs.append(log)
            logs = filtered_logs
        return jsonify(logs)
    except Exception as e:
        logger.error(f"Ошибка получения логов: {e}")
        return jsonify({'error': 'Ошибка получения логов'}), 500


# ===== Settings =====

@system_api_bp.route('/api/settings/early-off', methods=['GET', 'POST'])
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
    except Exception as e:
        logger.error(f"early-off setting failed: {e}")
        return api_error('INTERNAL_ERROR', 'internal error', 500)


@system_api_bp.route('/api/settings/system-name', methods=['GET', 'POST'])
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
    except Exception as e:
        logger.error(f"system-name setting failed: {e}")
        return api_error('INTERNAL_ERROR', 'internal error', 500)


# ===== Logging debug toggle =====

@system_api_bp.route('/api/logging/debug', methods=['GET', 'POST'])
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
            except Exception as e:
                logger.debug("Handled exception in api_logging_debug_toggle: %s", e)
        return jsonify({'debug': db.get_logging_debug()})
    except Exception as e:
        logger.error(f"api_logging_debug_toggle error: {e}")
        return jsonify({'debug': db.get_logging_debug()}), 500
