"""Zones Watering API — start/stop, watering time, SSE, MQTT control."""
from flask import Blueprint, request, jsonify, current_app, Response, stream_with_context
from datetime import datetime, timedelta
import json
import time
import queue
import logging

from database import db
from utils import normalize_topic
from irrigation_scheduler import get_scheduler
from services.mqtt_pub import publish_mqtt_value as _publish_mqtt_value
from services.helpers import api_error
from services import sse_hub as _sse_hub
from services.api_rate_limiter import rate_limit
import sqlite3

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

logger = logging.getLogger(__name__)

zones_watering_api_bp = Blueprint('zones_watering_api', __name__)


# ---- Zone start/stop ----

@zones_watering_api_bp.route('/api/zones/<int:zone_id>/start', methods=['POST'])
def start_zone(zone_id):
    """Start zone watering."""
    try:
        if current_app.config.get('EMERGENCY_STOP'):
            return jsonify({'success': False, 'message': 'Аварийная остановка активна. Сначала отключите аварийный режим.'}), 400
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404

        try:
            scheduler = get_scheduler()
            if scheduler:
                scheduler.cancel_group_jobs(int(zone['group_id']))
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Handled exception in start_zone: %s", e)

        # Turn off all other zones in group
        try:
            zones = db.get_zones()
            group_id = int(zone.get('group_id') or 0)
            if group_id:
                group_zones = [z for z in zones if z['group_id'] == group_id and int(z['id']) != int(zone_id)]
                for gz in group_zones:
                    try:
                        sid = gz.get('mqtt_server_id'); topic = (gz.get('topic') or '').strip()
                        if mqtt and sid and topic:
                            t = topic if str(topic).startswith('/') else '/' + str(topic)
                            server = db.get_mqtt_server(int(sid))
                            if server:
                                _publish_mqtt_value(server, t, '0', min_interval_sec=0.0, qos=2, retain=True)
                    except (ConnectionError, TimeoutError, OSError):
                        logger.exception("Ошибка публикации MQTT '0' при ручном запуске: выключение соседей")
                    try:
                        db.update_zone(int(gz['id']), {'state': 'off', 'watering_start_time': None})
                    except (sqlite3.Error, OSError) as e:
                        logger.debug("Handled exception in line_796: %s", e)
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in line_798: %s", e)

        try:
            from services.zone_control import exclusive_start_zone as _start_central
            ok = _start_central(int(zone_id))
            if not ok:
                return jsonify({'success': False, 'message': 'Не удалось запустить зону'}), 500
        except (ValueError, TypeError, KeyError):
            logger.exception('start_zone: central start failed')
            return jsonify({'success': False, 'message': 'Не удалось запустить зону'}), 500

        try:
            scheduler = get_scheduler()
            if scheduler:
                scheduler.schedule_zone_stop(zone_id, int(zone['duration']), command_id=str(int(time.time())))
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка планирования остановки зоны {zone_id}: {e}")

        group_id = int(zone.get('group_id') or 0)
        db.add_log('zone_start', json.dumps({
            "zone": zone_id, "group": group_id, "source": "manual", "duration": int(zone['duration'])
        }))
        return jsonify({'success': True, 'message': f'Зона {zone_id} запущена', 'zone_id': zone_id, 'state': 'on'})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка запуска зоны {zone_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка запуска зоны'}), 500


@zones_watering_api_bp.route('/api/zones/<int:zone_id>/stop', methods=['POST'])
def stop_zone(zone_id):
    """Stop zone watering."""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        try:
            from services.zone_control import stop_zone as _stop_central
            if not _stop_central(int(zone_id), reason='manual', force=False):
                return jsonify({'success': False, 'message': 'Не удалось остановить зону'}), 500
        except (ValueError, TypeError, KeyError):
            logger.exception('stop_zone: central stop failed')
            return jsonify({'success': False, 'message': 'Не удалось остановить зону'}), 500
        try:
            db.add_log('zone_stop', json.dumps({
                "zone": int(zone_id), "group": int(zone.get('group_id') or 0), "source": "manual"
            }))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in stop_zone: %s", e)
        return jsonify({'success': True, 'message': f'Зона {zone_id} остановлена', 'zone_id': zone_id, 'state': 'off'})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка остановки зоны {zone_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка остановки зоны'}), 500


@zones_watering_api_bp.route('/api/zones/<int:zone_id>/watering-time')
def api_zone_watering_time(zone_id):
    """Returns remaining and elapsed watering time for a zone."""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            resp = jsonify({'success': False, 'message': 'Зона не найдена'})
            resp.headers['Cache-Control'] = 'no-store'
            return resp, 404

        # Use planned_end_time if available (for override duration), else base duration
        planned_end_str = zone.get('planned_end_time')
        if planned_end_str and zone.get('watering_start_time'):
            try:
                planned_end_dt = datetime.strptime(planned_end_str, '%Y-%m-%d %H:%M:%S')
                start_dt_for_calc = datetime.strptime(zone.get('watering_start_time'), '%Y-%m-%d %H:%M:%S')
                total_duration = max(1, int((planned_end_dt - start_dt_for_calc).total_seconds() / 60))
                logger.debug("watering_time: zone %s using planned_end_time dur=%s", zone_id, total_duration)
            except (ValueError, TypeError) as e:
                logger.debug("planned_end_time parse failed: %s", e)
                total_duration = int(zone.get('duration') or 0)
        else:
            total_duration = int(zone.get('duration') or 0)
        start_str = zone.get('watering_start_time')
        if zone.get('state') != 'on' or not start_str:
            resp = jsonify({
                'success': True, 'zone_id': zone_id, 'is_watering': False,
                'elapsed_time': 0, 'remaining_time': 0, 'total_duration': total_duration,
                'elapsed_seconds': 0, 'remaining_seconds': 0, 'total_seconds': total_duration * 60
            })
            resp.headers['Cache-Control'] = 'no-store'
            return resp

        try:
            start_dt = datetime.strptime(start_str, '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in api_zone_watering_time: %s", e)
            db.update_zone(zone_id, {'watering_start_time': None})
            resp = jsonify({
                'success': True, 'zone_id': zone_id, 'is_watering': False,
                'elapsed_time': 0, 'remaining_time': 0, 'total_duration': total_duration,
                'elapsed_seconds': 0, 'remaining_seconds': 0, 'total_seconds': total_duration * 60
            })
            resp.headers['Cache-Control'] = 'no-store'
            return resp

        now = datetime.now()
        elapsed_seconds = max(0, int((now - start_dt).total_seconds()))
        total_seconds = int(total_duration * 60)
        if elapsed_seconds >= total_seconds:
            db.update_zone(zone_id, {'state': 'off', 'watering_start_time': None})
            resp = jsonify({
                'success': True, 'zone_id': zone_id, 'is_watering': False,
                'elapsed_time': total_duration, 'remaining_time': 0, 'total_duration': total_duration,
                'elapsed_seconds': total_seconds, 'remaining_seconds': 0, 'total_seconds': total_seconds
            })
            resp.headers['Cache-Control'] = 'no-store'
            return resp
        remaining_seconds = max(0, total_seconds - elapsed_seconds)
        elapsed_min = int(elapsed_seconds // 60)
        remaining_min = int(remaining_seconds // 60)
        resp = jsonify({
            'success': True, 'zone_id': zone_id, 'is_watering': True,
            'elapsed_time': elapsed_min, 'remaining_time': remaining_min, 'total_duration': total_duration,
            'elapsed_seconds': elapsed_seconds, 'remaining_seconds': remaining_seconds, 'total_seconds': total_seconds
        })
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения времени полива зоны {zone_id}: {e}")
        resp = jsonify({'success': False, 'message': 'Ошибка получения времени полива'})
        resp.headers['Cache-Control'] = 'no-store'
        return resp, 500


# ---- MQTT zones SSE ----

@zones_watering_api_bp.route('/api/mqtt/zones-sse')
def api_mqtt_zones_sse():
    """SSE endpoint — DISABLED to prevent event loop death on ARM/Hypercorn.
    Frontend uses 5s polling instead. Returns 204 No Content."""
    # Still start the hub for MQTT→DB state sync (zone state tracking)
    try:
        _sse_hub.ensure_hub_started()
    except (OSError, RuntimeError) as e:
        logger.debug("SSE hub start (background): %s", e)
    return ('', 204)


# ---- Zone MQTT start/stop ----

@zones_watering_api_bp.route('/api/zones/<int:zone_id>/mqtt/start', methods=['POST'])
@rate_limit('mqtt_control', max_requests=10, window_sec=60)
def api_zone_mqtt_start(zone_id: int):
    t0 = time.time()
    try:
        z = db.get_zone(zone_id)
        if not z:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        if current_app.config.get('EMERGENCY_STOP'):
            return jsonify({'success': False, 'message': 'Аварийная остановка активна'}), 400
        # Accept optional duration override from request body (one-time, does NOT change zone's base duration)
        try:
            body = request.get_json(silent=True) or {}
            req_duration = body.get('duration')
            if req_duration is not None:
                req_duration = int(req_duration)
                if 1 <= req_duration <= 120:
                    z['duration'] = req_duration  # Override in-memory only, not in DB
                    logger.info("mqtt_start: zone %s using override duration %s min (base unchanged)", zone_id, req_duration)
        except (ValueError, TypeError) as e:
            logger.debug("mqtt_start duration parse: %s", e)
        if str(z.get('state') or '') == 'on':
            # If duration override provided — reschedule stop with new duration
            try:
                body2 = request.get_json(silent=True) or {}
                if body2.get('duration') is not None:
                    override_dur = int(z.get('duration') or 10)  # already overridden above
                    now_dt = datetime.now()
                    new_end = (now_dt + timedelta(minutes=override_dur)).strftime('%Y-%m-%d %H:%M:%S')
                    db.update_zone(zone_id, {
                        'planned_end_time': new_end,
                        'watering_start_time': now_dt.strftime('%Y-%m-%d %H:%M:%S'),
                        'watering_start_source': 'manual'
                    })
                    # Reschedule stop (don't cancel_group_jobs — it stops all zones!)
                    try:
                        sched = get_scheduler()
                        if sched:
                            # Remove existing stop jobs for this zone only
                            try:
                                for job in sched.scheduler.get_jobs():
                                    if f"zone_stop:{zone_id}:" in str(job.id) or f"zone_hard_stop:{zone_id}" in str(job.id):
                                        job.remove()
                            except Exception as e:
                                logger.debug("remove old stop jobs: %s", e)
                            sched.schedule_zone_stop(zone_id, override_dur, command_id=str(int(time.time())))
                            sched.schedule_zone_hard_stop(zone_id, now_dt + timedelta(minutes=override_dur))
                    except (ValueError, TypeError, ImportError) as e:
                        logger.debug("reschedule on override: %s", e)
                    logger.info("mqtt_start: zone %s already ON, rescheduled to %s min (end=%s)", zone_id, override_dur, new_end)
                    return jsonify({'success': True, 'message': f'Зона {zone_id} перезапущена на {override_dur} мин'})
            except (ValueError, TypeError) as e:
                logger.debug("mqtt_start already-on duration: %s", e)
            return jsonify({'success': True, 'message': 'Зона уже запущена'})
        gid = int(z.get('group_id') or 0)
        try:
            if gid:
                sched = get_scheduler()
                if sched:
                    sched.cancel_group_jobs(int(gid))
                try:
                    programs = db.get_programs() or []
                    now = datetime.now()
                    today = now.strftime('%Y-%m-%d')
                    for p in programs:
                        try:
                            hh, mm = map(int, str(p.get('time') or '00:00').split(':', 1))
                        except (ValueError, TypeError, KeyError) as e:
                            logger.debug("Exception in api_zone_mqtt_start: %s", e)
                            hh, mm = 0, 0
                        start_today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                        if start_today <= now:
                            db.cancel_program_run_for_group(int(p.get('id')), today, int(gid))
                except (ConnectionError, TimeoutError, OSError) as e:
                    logger.debug("Handled exception in api_zone_mqtt_start: %s", e)
                try:
                    db.reschedule_group_to_next_program(int(gid))
                except (sqlite3.Error, OSError) as e:
                    logger.debug("Handled exception in line_985: %s", e)
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in line_987: %s", e)
        t1 = time.time()
        # Fast OFF peers in background
        try:
            zones_all = db.get_zones() or []
            peers_on = [zz for zz in zones_all if int(zz.get('group_id') or 0) == gid and int(zz.get('id')) != int(zone_id) and str(zz.get('state') or '').lower() == 'on']
            if peers_on:
                import threading, concurrent.futures
                def _bg_off():
                    try:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(peers_on))) as pool:
                            def _off_peer(peer):
                                try:
                                    sid = peer.get('mqtt_server_id'); topic = (peer.get('topic') or '').strip()
                                    if mqtt and sid and topic:
                                        tpc = normalize_topic(topic)
                                        server = db.get_mqtt_server(int(sid))
                                        if server:
                                            _publish_mqtt_value(server, tpc, '0', min_interval_sec=0.0, qos=2, retain=True)
                                except (ConnectionError, TimeoutError, OSError) as e:
                                    logger.debug("Handled exception in _off_peer: %s", e)
                                try:
                                    db.update_zone(int(peer['id']), {'state': 'off', 'watering_start_time': None})
                                except (sqlite3.Error, OSError) as e:
                                    logger.debug("Handled exception in _off_peer: %s", e)
                            list(pool.map(_off_peer, peers_on))
                    except (ConnectionError, TimeoutError, OSError) as e:
                        logger.debug("Exception in _off_peer: %s", e)
                        try:
                            logger.exception('fast OFF peers bg failed')
                        except (OSError, ValueError) as e:
                            logger.debug("Handled exception in _off_peer: %s", e)
                import threading as _th
                _th.Thread(target=_bg_off, daemon=True).start()
        except (RuntimeError, OSError):
            logger.exception('fast parallel OFF peers failed')
        t2 = time.time()
        # Open master valve + publish zone ON
        try:
            try:
                gid2 = int(z.get('group_id') or 0)
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in line_1029: %s", e)
                gid2 = 0
            if gid2:
                try:
                    g = next((gg for gg in (db.get_groups() or []) if int(gg.get('id')) == gid2), None)
                except (sqlite3.Error, OSError) as e:
                    logger.debug("Exception in line_1035: %s", e)
                    g = None
                if g and int(g.get('use_master_valve') or 0) == 1:
                    mtopic = (g.get('master_mqtt_topic') or '').strip()
                    msid = g.get('master_mqtt_server_id')
                    if mtopic and msid:
                        server_mv = db.get_mqtt_server(int(msid))
                        if server_mv:
                            try:
                                mode = (g.get('master_mode') or 'NC').strip().upper()
                            except (ValueError, TypeError, KeyError) as e:
                                logger.debug("Exception in line_1046: %s", e)
                                mode = 'NC'
                            _publish_mqtt_value(server_mv, normalize_topic(mtopic), ('0' if mode == 'NO' else '1'), min_interval_sec=0.0, qos=2, retain=True)
            sid = z.get('mqtt_server_id'); topic = (z.get('topic') or '').strip()
            if mqtt and sid and topic:
                tpc = normalize_topic(topic)
                server = db.get_mqtt_server(int(sid))
                if server:
                    _publish_mqtt_value(server, tpc, '1', min_interval_sec=0.0, qos=2, retain=True)
        except (ConnectionError, TimeoutError, OSError):
            logger.exception('fast ON publish failed')
            return jsonify({'success': False, 'message': 'MQTT publish failed'}), 500
        t3 = time.time()
        # DB update — use override duration for planned_end_time
        override_dur = int(z.get('duration') or 10)
        now_dt = datetime.now()
        planned_end = (now_dt + timedelta(minutes=override_dur)).strftime('%Y-%m-%d %H:%M:%S')
        try:
            db.update_zone(int(zone_id), {
                'state': 'on',
                'watering_start_time': now_dt.strftime('%Y-%m-%d %H:%M:%S'),
                'watering_start_source': 'manual',
                'commanded_state': 'on',
                'planned_end_time': planned_end
            })
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in line_1063: %s", e)
        t4 = time.time()
        # Schedule auto-stop in background (use override duration)
        t5 = time.time()
        _override_dur_for_bg = override_dur
        try:
            import threading
            _is_testing = current_app.config.get('TESTING', False)
            def _bg_schedule():
                try:
                    sched = get_scheduler()
                    if sched and not _is_testing:
                        dur = _override_dur_for_bg
                        if dur > 0:
                            sched.schedule_zone_stop(int(zone_id), dur, command_id=str(int(time.time())))
                            sched.schedule_zone_hard_stop(int(zone_id), datetime.now() + timedelta(minutes=dur))
                    if (not sched) and not _is_testing:
                        dur = _override_dur_for_bg
                        # Write planned_end_time for fallback too
                        if dur > 0:
                            try:
                                fallback_end = (datetime.now() + timedelta(minutes=dur)).strftime('%Y-%m-%d %H:%M:%S')
                                db.update_zone(zone_id, {'planned_end_time': fallback_end})
                            except (sqlite3.Error, OSError) as e:
                                logger.debug("fallback planned_end_time update failed: %s", e)
                            def _fallback_stop():
                                try:
                                    time.sleep(max(1, dur * 60))
                                    from services.zone_control import stop_zone as _stop
                                    _stop(int(zone_id), reason='auto_fallback', force=True)
                                except ImportError as e:
                                    logger.debug("Exception in _fallback_stop: %s", e)
                                    try:
                                        logger.exception('fallback auto-stop failed')
                                    except (OSError, ValueError) as e:
                                        logger.debug("Handled exception in _fallback_stop: %s", e)
                            import threading as _th2
                            _th2.Thread(target=_fallback_stop, daemon=True).start()
                except ImportError as e:
                    logger.debug("Exception in _fallback_stop: %s", e)
                    try:
                        logger.exception('manual mqtt start: schedule auto-stop failed')
                    except (ConnectionError, TimeoutError, OSError) as e:
                        logger.debug("Handled exception in _fallback_stop: %s", e)
            threading.Thread(target=_bg_schedule, daemon=True).start()
        except (RuntimeError, OSError) as e:
            logger.debug("Handled exception in _fallback_stop: %s", e)
        try:
            db.add_log('diag_manual_start_timing', json.dumps({
                'zone': int(zone_id),
                't_fast_off_ms': int((t2 - t1) * 1000),
                't_on_publish_ms': int((t3 - t2) * 1000),
                't_db_update_ms': int((t4 - t3) * 1000),
                't_schedule_ms': int((t5 - t4) * 1000),
                't_total_ms': int((t5 - t0) * 1000)
            }))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in line_1116: %s", e)
        try:
            db.add_log('zone_start_manual', json.dumps({'zone': int(zone_id), 'group': gid}))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in line_1120: %s", e)
        return jsonify({'success': True, 'message': f'Зона {int(zone_id)} запущена'})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.exception('api_zone_mqtt_start failed')
        return jsonify({'success': False, 'message': 'Ошибка запуска зоны'}), 500


@zones_watering_api_bp.route('/api/zones/<int:zone_id>/mqtt/stop', methods=['POST'])
@rate_limit('mqtt_control', max_requests=10, window_sec=60)
def api_zone_mqtt_stop(zone_id: int):
    z = db.get_zone(zone_id)
    if not z:
        return jsonify({'success': False}), 404
    try:
        from services.zone_control import stop_zone as _stop_central
        if _stop_central(int(zone_id), reason='manual', force=False):
            return jsonify({'success': True, 'message': 'Зона остановлена'})
    except (ValueError, TypeError, KeyError):
        logger.exception('api_zone_mqtt_stop: central stop failed, fallback to direct publish')
    sid = z.get('mqtt_server_id'); topic = (z.get('topic') or '').strip()
    if not sid or not topic:
        return jsonify({'success': False, 'message': 'No MQTT config for zone'}), 400
    t = normalize_topic(topic)
    try:
        server = db.get_mqtt_server(int(sid))
        if not server:
            return jsonify({'success': False, 'message': 'MQTT server not found'}), 400
        logger.info(f"HTTP publish OFF zone={zone_id} topic={t}")
        _publish_mqtt_value(server, t, '0', min_interval_sec=0.0, qos=2, retain=True)
        try:
            db.update_zone(zone_id, {'state': 'off', 'watering_start_time': None})
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in api_zone_mqtt_stop: %s", e)
        return jsonify({'success': True, 'message': 'Зона остановлена'})
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"MQTT publish stop failed: {e}")
        return jsonify({'success': False, 'message': 'MQTT publish failed'}), 500
