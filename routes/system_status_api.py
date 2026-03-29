"""System Status API — status, health, scheduler, logs, water, server-time."""
from flask import Blueprint, request, jsonify, current_app, session
from datetime import datetime, timedelta
import json
import time
import logging

from database import db
from irrigation_scheduler import get_scheduler
from services.helpers import api_error
from services.security import admin_required
from services.monitors import rain_monitor, env_monitor, water_monitor
from services.locks import snapshot_all_locks as _locks_snapshot
from services import sse_hub as _sse_hub
import sqlite3

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

logger = logging.getLogger(__name__)

system_status_api_bp = Blueprint('system_status_api', __name__)


# ===== Health / Scheduler =====

@system_status_api_bp.route('/api/health-details')
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
                    except (ValueError, TypeError, KeyError) as e:
                        logger.debug("Exception in api_health_details: %s", e)
                        continue
            except (ValueError, TypeError, KeyError) as e:
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
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Exception in line_84: %s", e)
                    continue
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in line_87: %s", e)
        locks = _locks_snapshot()
        group_cancels = []
        try:
            if hasattr(sched, 'group_cancel_events'):
                for gid, ev in (sched.group_cancel_events or {}).items():
                    try:
                        group_cancels.append({'group_id': int(gid), 'set': bool(ev.is_set())})
                    except (ValueError, TypeError, KeyError) as e:
                        logger.debug("Exception in line_96: %s", e)
                        continue
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Handled exception in line_99: %s", e)
        try:
            meta_tail = _sse_hub.get_meta_buffer()
        except (OSError, ValueError, RuntimeError) as e:
            logger.debug("Exception in line_103: %s", e)
            meta_tail = []
        payload = {
            'now': datetime.now().isoformat(timespec='seconds'),
            'scheduler_running': bool(sched and sched.is_running),
            'jobs': jobs, 'zones': zones, 'locks': locks,
            'group_cancels': group_cancels, 'meta_tail': meta_tail,
        }
        return jsonify(payload)
    except (sqlite3.Error, OSError) as e:
        logger.exception('health-details failed')
        return api_error('health_details_failed', f'health details error: {e}', 500)


@system_status_api_bp.route('/api/health/job/<path:job_id>/cancel', methods=['POST'])
@admin_required
def api_health_cancel_job(job_id):
    try:
        sched = get_scheduler()
        if not sched or not getattr(sched, 'scheduler', None):
            return api_error('scheduler_unavailable', 'scheduler unavailable', 503)
        try:
            sched.scheduler.remove_job(str(job_id))
            return jsonify({'success': True, 'message': f'job {job_id} removed'})
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in api_health_cancel_job: %s", e)
            return api_error('job_remove_failed', f'failed to remove job: {e}', 400)
    except (ValueError, TypeError, KeyError) as e:
        logger.exception('cancel job failed')
        return api_error('cancel_job_failed', f'error: {e}', 500)


@system_status_api_bp.route('/api/health/group/<int:group_id>/cancel', methods=['POST'])
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
        except (ValueError, TypeError, RuntimeError):
            logger.exception('group cancel failed')
            return api_error('group_cancel_failed', 'failed to cancel group jobs', 400)
        return jsonify({'success': True, 'message': f'group {group_id} cancelled'})
    except (ValueError, TypeError, KeyError) as e:
        logger.exception('cancel group failed')
        return api_error('cancel_group_failed', f'error: {e}', 500)


@system_status_api_bp.route('/api/scheduler/init', methods=['POST'])
def api_scheduler_init():
    """Explicit scheduler init for UI/tests."""
    try:
        from irrigation_scheduler import init_scheduler
        init_scheduler(db)
        return jsonify({'success': True})
    except (ValueError, KeyError, RuntimeError) as e:
        logger.error(f"Ошибка явной инициализации планировщика: {e}")
        return api_error('INTERNAL_ERROR', 'internal error', 500)


@system_status_api_bp.route('/api/scheduler/status')
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
    except (ValueError, TypeError, KeyError) as e:
        logger.error(f"Ошибка получения статуса планировщика: {e}")
        return jsonify({'error': 'Ошибка получения статуса'}), 500


@system_status_api_bp.route('/api/scheduler/jobs')
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
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in api_scheduler_jobs: %s", e)
                continue
        return jsonify({'success': True, 'jobs': jobs})
    except (ValueError, TypeError, KeyError) as e:
        logger.error(f"scheduler jobs list failed: {e}")
        return jsonify({'success': False, 'jobs': []}), 200


# ===== Health check =====

@system_status_api_bp.route('/health')
def health_check():
    try:
        try:
            _ = db.get_zones()
            db_ok = True
        except (sqlite3.Error, OSError) as e:
            logger.debug("Exception in health_check: %s", e)
            db_ok = False
        try:
            sched = get_scheduler()
            sched_ok = bool(sched is not None)
        except (ValueError, KeyError, RuntimeError) as e:
            logger.debug("Exception in health_check: %s", e)
            sched_ok = False
        try:
            servers = db.get_mqtt_servers() or []
            mqtt_ok = bool(len(servers) >= 0)
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Exception in health_check: %s", e)
            mqtt_ok = False
        overall = db_ok and sched_ok
        code = 200 if overall else 503
        return jsonify({'ok': overall, 'db': db_ok, 'scheduler': sched_ok, 'mqtt_configured': mqtt_ok}), code
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.exception('health check failed')
        return jsonify({'ok': False, 'error': str(e)}), 500


# ===== Server time =====

@system_status_api_bp.route('/api/server-time')
def api_server_time():
    try:
        now = datetime.now()
        try:
            tzname = time.tzname[0] if time.tzname else ''
        except (KeyError, TypeError, ValueError) as e:
            logger.debug("Exception in api_server_time: %s", e)
            tzname = ''
        payload = {'now_iso': now.strftime('%Y-%m-%d %H:%M:%S'), 'epoch_ms': int(time.time() * 1000), 'tz': tzname}
        resp = jsonify(payload)
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    except (ValueError, TypeError, KeyError) as e:
        logger.error(f"server-time failed: {e}")
        return jsonify({'now_iso': None, 'epoch_ms': int(time.time() * 1000)}), 200


# ===== Status (big endpoint) =====

@system_status_api_bp.route('/api/status')
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
            except (ValueError, TypeError, KeyError) as e:
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
                from services.helpers import parse_dt
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
                except (KeyError, TypeError, ValueError) as e:
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
            except (KeyError, TypeError, ValueError) as e:
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
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Handled exception in line_767: %s", e)

        try:
            use_master_valve = bool(int(group.get('use_master_valve') or 0))
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in line_772: %s", e)
            use_master_valve = False
        try:
            mvo = (group.get('master_valve_observed') or '').strip()
            master_valve_state = mvo if mvo in ('open', 'closed') else 'unknown'
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in line_778: %s", e)
            master_valve_state = 'unknown'
        try:
            use_pressure_sensor = bool(int(group.get('use_pressure_sensor') or 0))
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in line_783: %s", e)
            use_pressure_sensor = False
        try:
            use_water_meter = bool(int(group.get('use_water_meter') or 0))
        except (ValueError, TypeError, KeyError) as e:
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
                    except (ValueError, TypeError, KeyError) as e:
                        logger.debug("Exception in line_803: %s", e)
                        start_iso = None
                flow_value = water_monitor.get_flow_lpm(int(group_id), start_iso)
            except (ValueError, TypeError, KeyError) as e:
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
        except (ValueError, TypeError, RuntimeError) as e:
            logger.debug("Exception in line_831: %s", e)
            rain_sensor_status = 'дождя нет'

    env_cfg = db.get_env_config()
    temp_enabled = bool(env_cfg.get('temp', {}).get('enabled'))
    hum_enabled = bool(env_cfg.get('hum', {}).get('enabled'))
    temperature = None if not temp_enabled else (env_monitor.temp_value if env_monitor.temp_value is not None else 'нет данных')
    humidity = None if not hum_enabled else (env_monitor.hum_value if env_monitor.hum_value is not None else 'нет данных')

    try:
        servers = db.get_mqtt_servers()
    except (ConnectionError, TimeoutError, OSError) as e:
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
                    except (ConnectionError, TimeoutError, OSError) as e:
                        logger.debug("Handled exception in line_862: %s", e)
                    break
                except (ConnectionError, TimeoutError, OSError) as e:
                    logger.debug("Exception in line_865: %s", e)
                    mqtt_connected = False
        if mqtt_servers_count == 0:
            try:
                logger.warning('MQTT: нет ни одного сервера в настройках')
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Handled exception in line_871: %s", e)
            try:
                db.add_log('mqtt_warn', 'нет ни одного сервера в настройках')
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Handled exception in line_875: %s", e)
        elif not mqtt_connected:
            try:
                logger.warning('MQTT: нет связи ни с одним сервером')
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Handled exception in line_880: %s", e)
            try:
                db.add_log('mqtt_warn', 'нет связи ни с одним MQTT сервером')
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Handled exception in line_884: %s", e)
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.debug("Handled exception in line_886: %s", e)

    logger.info(f"api_status: temp={temperature} hum={humidity} temp_enabled={temp_enabled} hum_enabled={hum_enabled}")
    try:
        role = session.get('role')
        is_admin = (role == 'admin')
    except (KeyError, TypeError, ValueError) as e:
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

@system_status_api_bp.route('/api/logs')
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
                    except (ValueError, TypeError, KeyError) as e:
                        logger.debug("Bare exception in api_logs: %s", e)
                        continue
                filtered_logs.append(log)
            logs = filtered_logs
        return jsonify(logs)
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения логов: {e}")
        return jsonify({'error': 'Ошибка получения логов'}), 500


# ===== Water usage =====

@system_status_api_bp.route('/api/water')
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
                try:
                    usage = db.get_water_usage(group['id']) if hasattr(db, 'get_water_usage') else None
                except (sqlite3.Error, OSError) as e:
                    logger.debug("Exception in api_water: %s", e)
                    usage = None
                if usage:
                    daily_usage = usage.get('daily_usage', [])
                    total_liters = usage.get('total_liters', 0)
                    zone_usage = usage.get('zone_usage', {})
                else:
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
            except (sqlite3.Error, OSError) as e:
                logger.error(f"Ошибка обработки группы {group['id']}: {e}")
                continue
        return jsonify(water_data)
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения данных о воде: {e}")
        return jsonify({'error': 'Ошибка получения данных о воде'}), 500
