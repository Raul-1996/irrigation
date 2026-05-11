"""Groups API blueprint — all /api/groups* endpoints + master valve."""
from flask import Blueprint, request, jsonify, current_app
from datetime import datetime, timedelta
import json
import time
import threading
import logging

from database import db
from utils import normalize_topic
from irrigation_scheduler import init_scheduler, get_scheduler
from services.mqtt_pub import publish_mqtt_value as _publish_mqtt_value
from services import sse_hub as _sse_hub
from services.audit import audit_log
from services.api_rate_limiter import rate_limit
from constants import GROUP_DEBOUNCE_SEC, ZONE_CAP_DEFAULT_MIN
import sqlite3

try:
    import paho.mqtt.client as mqtt
except ImportError as e:
    logger.debug("Exception in line_18: %s", e)
    mqtt = None

logger = logging.getLogger(__name__)

groups_api_bp = Blueprint('groups_api', __name__)

# Anti-flapper guard
_GROUP_CHANGE_GUARD = {}
_GROUP_GUARD_LOCK = threading.Lock()

def _should_throttle_group(group_id: int, window_sec: float = GROUP_DEBOUNCE_SEC) -> bool:
    now = time.time()
    with _GROUP_GUARD_LOCK:
        last = _GROUP_CHANGE_GUARD.get(group_id, 0)
        if now - last < window_sec:
            return True
        _GROUP_CHANGE_GUARD[group_id] = now
    return False


@groups_api_bp.route('/api/groups')
def api_groups():
    groups = db.get_groups()
    return jsonify(groups)


@groups_api_bp.route('/api/groups/<int:group_id>', methods=['PUT'])
@audit_log('group_save', target_extractor=lambda *a, **kw: f"group:{kw.get('group_id', a[0] if a else '?')}")
def api_update_group(group_id):
    data = request.get_json() or {}
    updated = False
    if 'name' in data:
        if db.update_group(group_id, data['name']):
            updated = True
    if 'use_rain_sensor' in data:
        try:
            ok = db.set_group_use_rain(group_id, bool(data.get('use_rain_sensor')))
            updated = updated or ok
        except (sqlite3.Error, OSError) as e:
            logger.error(f"Ошибка обновления use_rain_sensor группы {group_id}: {e}")

    fields_map = {
        'use_master_valve': ('use_master_valve', lambda v: 1 if v else 0),
        'master_mqtt_topic': ('master_mqtt_topic', lambda v: (v or '').strip()),
        'master_mode': ('master_mode', lambda v: (str(v or 'NC')).strip().upper()),
        'master_mqtt_server_id': ('master_mqtt_server_id', lambda v: int(v) if v not in (None, '') else None),
        'use_pressure_sensor': ('use_pressure_sensor', lambda v: 1 if v else 0),
        'pressure_mqtt_topic': ('pressure_mqtt_topic', lambda v: (v or '').strip()),
        'pressure_unit': ('pressure_unit', lambda v: (str(v or 'bar')).strip()),
        'pressure_mqtt_server_id': ('pressure_mqtt_server_id', lambda v: int(v) if v not in (None, '') else None),
        'use_water_meter': ('use_water_meter', lambda v: 1 if v else 0),
        'water_mqtt_topic': ('water_mqtt_topic', lambda v: (v or '').strip()),
        'water_mqtt_server_id': ('water_mqtt_server_id', lambda v: int(v) if v not in (None, '') else None),
        'water_pulse_size': ('water_pulse_size', lambda v: (str(v or '1l') if str(v or '1l') in ('1l', '10l', '100l') else '1l')),
        'water_base_value_m3': ('water_base_value_m3', lambda v: float(v) if v not in (None, '') else 0.0),
        'water_base_pulses': ('water_base_pulses', lambda v: int(v) if v not in (None, '') else 0),
        'master_close_delay_sec': ('master_close_delay_sec', lambda v: max(1, min(3600, int(v or 60)))),
    }
    updates = {}
    for k, (col, norm) in fields_map.items():
        if k in data:
            try:
                updates[col] = norm(data.get(k))
            except (KeyError, TypeError, ValueError) as e:
                logger.debug("Handled exception in line_81: %s", e)
    if updates:
        try:
            if 'use_master_valve' in updates and int(updates.get('use_master_valve') or 0) == 1:
                if not (updates.get('master_mqtt_topic') or (db.get_group(group_id) or {}).get('master_mqtt_topic')):
                    return jsonify({'success': False, 'message': 'Нужен MQTT-топик мастер-клапана'}), 400
                sid = updates.get('master_mqtt_server_id') or (db.get_group(group_id) or {}).get('master_mqtt_server_id')
                if not sid or not db.get_mqtt_server(int(sid)):
                    return jsonify({'success': False, 'message': 'Нужен корректный MQTT-сервер для мастер-клапана'}), 400
            if 'master_mode' in updates:
                if updates['master_mode'] not in ('NC', 'NO'):
                    return jsonify({'success': False, 'message': 'master_mode должен быть NC или NO'}), 400
            if 'pressure_unit' in updates:
                if str(updates['pressure_unit']).lower() not in ('bar', 'kpa', 'psi'):
                    return jsonify({'success': False, 'message': 'pressure_unit должен быть bar|kPa|psi'}), 400
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in line_97: %s", e)
        ok = db.update_group_fields(group_id, updates)
        updated = updated or ok
    if updated:
        try:
            payload = {"group": group_id}
            if 'name' in data:
                payload["name"] = data['name']
            if 'use_rain_sensor' in data:
                payload["use_rain_sensor"] = bool(data.get('use_rain_sensor'))
            for k in ('use_master_valve', 'master_mqtt_topic', 'master_mode', 'master_mqtt_server_id',
                      'use_pressure_sensor', 'pressure_mqtt_topic', 'pressure_unit', 'pressure_mqtt_server_id',
                      'use_water_meter', 'water_mqtt_topic', 'water_mqtt_server_id', 'water_pulse_size',
                      'water_base_value_m3', 'water_base_pulses', 'master_close_delay_sec'):
                if k in data:
                    payload[k] = data.get(k)
            db.add_log('group_edit', json.dumps(payload))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in line_115: %s", e)
        return jsonify({"success": True})
    return ('Group not found', 404)


@groups_api_bp.route('/api/groups', methods=['POST'])
@audit_log('group_create', target_extractor=lambda *a, **kw: 'group:new')
def api_create_group():
    data = request.get_json() or {}
    name = data.get('name') or 'Новая группа'
    group = db.create_group(name)
    if group:
        db.add_log('group_create', json.dumps({"group": group['id'], "name": name}))
        return jsonify(group), 201
    return jsonify({"success": False, "message": "Не удалось создать группу"}), 400


@groups_api_bp.route('/api/groups/<int:group_id>', methods=['DELETE'])
@audit_log('group_delete', target_extractor=lambda *a, **kw: f"group:{kw.get('group_id', a[0] if a else '?')}")
def api_delete_group(group_id):
    if db.delete_group(group_id):
        db.add_log('group_delete', json.dumps({"group": group_id}))
        return ('', 204)
    return jsonify({"success": False, "message": "Нельзя удалить группу: переместите или удалите зоны этой группы"}), 400


@groups_api_bp.route('/api/groups/<int:group_id>/stop', methods=['POST'])
@audit_log('group_stop', target_extractor=lambda *a, **kw: f"group:{kw.get('group_id', a[0] if a else '?')}")
def api_stop_group(group_id):
    """Stop all zones in group."""
    try:
        try:
            from services.zone_control import stop_all_in_group as _stop_all
            _stop_all(int(group_id), reason='group_stop', force=True)
        except ImportError:
            logger.exception('group stop: stop_all_in_group failed')

        scheduler = get_scheduler()
        if scheduler:
            scheduler.cancel_group_jobs(int(group_id))
            # Issue #16 §3.5: emit a session_aborted_by_user audit row so
            # a single query on action_type='session_aborted_by_user'
            # lists all user-driven aborts regardless of which button
            # was pressed (zone-card stop, group-card stop). Behaviour
            # unchanged — only the audit signal is added.
            try:
                from services.audit import record_audit
                record_audit(
                    action_type='session_aborted_by_user',
                    source='group_stop',
                    target=f'group:{int(group_id)}',
                    payload={'endpoint': 'api_stop_group'},
                    actor='user',
                )
            except Exception:  # noqa: BLE001 — audit never breaks the stop path
                logger.exception('session_aborted_by_user audit failed')
            try:
                db.clear_group_scheduled_starts(group_id)
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in api_stop_group: %s", e)

        try:
            programs = db.get_programs() or []
            now = datetime.now()
            today = now.strftime('%Y-%m-%d')
            for p in programs:
                try:
                    if now.weekday() not in (p.get('days') or []):
                        continue
                    hh, mm = map(int, str(p.get('time') or '00:00').split(':', 1))
                    start_today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if start_today <= now:
                        db.cancel_program_run_for_group(int(p.get('id')), today, int(group_id))
                except (sqlite3.Error, OSError) as e:
                    logger.debug("Exception in api_stop_group: %s", e)
                    continue
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in line_173: %s", e)
        try:
            db.reschedule_group_to_next_program(group_id)
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in line_177: %s", e)

        db.add_log('group_stop', json.dumps({"group": group_id}))
        return jsonify({"success": True, "message": f"Группа {group_id} остановлена"})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка остановки группы {group_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка остановки группы"}), 500


def _compute_next_zone_id(group_id: int, current_zone_id: int):
    """Best-effort: read zones.scheduled_start_time for this group, return the
    next zone after current_zone_id by plan order. None if current is last or
    no plan exists."""
    try:
        zones = db.get_zones() or []
        gz = [z for z in zones
              if int(z.get('group_id') or 0) == int(group_id)
              and z.get('scheduled_start_time')]
        if not gz:
            return None
        gz.sort(key=lambda z: str(z.get('scheduled_start_time') or ''))
        zids = [int(z['id']) for z in gz]
        if int(current_zone_id) not in zids:
            return None
        idx = zids.index(int(current_zone_id))
        if idx + 1 < len(zids):
            return zids[idx + 1]
        return None
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as e:
        logger.debug("compute_next_zone_id error: %s", e)
        return None


@groups_api_bp.route('/api/groups/<int:group_id>/skip-current', methods=['POST'])
@audit_log('zone_skip', target_extractor=lambda *a, **kw: f"group:{kw.get('group_id', a[0] if a else '?')}")
def api_skip_current_zone(group_id):
    """Skip the currently running zone in the group's sequence; next zone starts now."""
    try:
        group = next((g for g in (db.get_groups() or []) if int(g['id']) == int(group_id)), None)
        if not group:
            return jsonify({'success': False, 'message': 'Группа не найдена'}), 404

        scheduler = get_scheduler()
        if not scheduler:
            return jsonify({'success': False, 'message': 'Планировщик недоступен'}), 500
        if not scheduler.is_group_session_active(int(group_id)):
            return jsonify({'success': False, 'message': 'Нет активного полива в группе'}), 400

        # Capture "current" + "next" from authoritative state BEFORE setting the event.
        zones = db.get_zones() or []
        active = [z for z in zones if int(z.get('group_id') or 0) == int(group_id) and z.get('state') == 'on']
        if not active:
            return jsonify({'success': False, 'message': 'Нет активной зоны для пропуска'}), 400
        current_zone_id = int(active[0]['id'])
        next_zone_id = _compute_next_zone_id(int(group_id), current_zone_id)

        scheduled = scheduler.request_skip_current_zone(int(group_id))
        if scheduled == 'debounced':
            # Issue #14 C2: server-side debounce — second skip request for
            # the same group arrived within 1.0s of the previous successful
            # one. Frontend 1500ms guard is bypassable (multi-tab, scripted
            # callers); this is the authoritative throttle.
            return jsonify({
                'success': False,
                'message': 'Слишком частые запросы — подождите секунду',
            }), 429
        if scheduled != 'ok':
            return jsonify({'success': False, 'message': 'Нет активного полива в группе'}), 400

        try:
            db.add_log('zone_skip', json.dumps({
                'group_id': int(group_id), 'zone_id': current_zone_id,
                'next_zone_id': next_zone_id, 'source': 'manual',
            }))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("zone_skip api log: %s", e)
        return jsonify({
            'success': True,
            'skipped_zone_id': current_zone_id,
            'next_zone_id': next_zone_id,
        })
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка пропуска зоны в группе {group_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка пропуска зоны'}), 500


@groups_api_bp.route('/api/groups/<int:group_id>/start-from-first', methods=['POST'])
@audit_log('group_start_from_first', target_extractor=lambda *a, **kw: f"group:{kw.get('group_id', a[0] if a else '?')}")
def api_start_group_from_first(group_id):
    """Start sequential watering of the group from the first zone."""
    try:
        scheduler = get_scheduler()
        if not scheduler:
            try:
                scheduler = init_scheduler(db)
            except (ValueError, KeyError, RuntimeError) as e:
                logger.debug("Exception in api_start_group_from_first: %s", e)
                scheduler = None
        if not scheduler:
            return jsonify({"success": False, "message": "Планировщик недоступен"}), 500
        body = request.get_json(silent=True) or {}
        # Issue #12 C2: "minutes wins if both sent" — strict. If
        # override_duration is present in the body AT ALL, that is the
        # user's intent. Accept (1..120) or reject the whole request (400).
        # Never silently fall through to percent.
        override_dur_raw = body.get('override_duration')
        minutes_sent = override_dur_raw is not None
        override_dur = None
        if minutes_sent:
            try:
                override_dur = int(override_dur_raw)
            except (ValueError, TypeError):
                return jsonify({"success": False,
                                "message": "override_duration должен быть целым числом 1..120"}), 400
            if not (1 <= override_dur <= 120):
                return jsonify({"success": False,
                                "message": "override_duration должен быть в диапазоне 1..120 мин"}), 400
        # Issue #12: optional duration_percent (one of PERCENT_PRESETS).
        # Only honoured when minutes mode is absent. Anything outside the
        # whitelist is silently ignored — defensive, mirrors 1..120 minutes
        # validation behaviour for non-whitelist values.
        override_pct = None
        if not minutes_sent:
            req_pct = body.get('duration_percent')
            if req_pct is not None:
                try:
                    from services.zone_control import PERCENT_PRESETS
                    p = int(req_pct)
                    if p in PERCENT_PRESETS:
                        override_pct = p
                except (ValueError, TypeError):
                    override_pct = None
        # Issue #12 C1: pre-compute warnings (deduped) so the response
        # mirrors the single-zone endpoint contract. Cheap: same helper
        # the scheduler uses, just called once per group zone here for
        # surface-able tags. Order is sorted for deterministic output.
        warnings: list = []
        if override_pct is not None:
            try:
                from services.zone_control import per_zone_dur as _per_zone_dur
                zones = db.get_zones() or []
                group_zones = [z for z in zones if z.get('group_id') == group_id]
                wset: set = set()
                for z in group_zones:
                    _d, _w = _per_zone_dur(z, override_dur, override_pct)
                    for tag in _w:
                        wset.add(tag)
                warnings = sorted(wset)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                logger.debug("group warnings preflight failed: %s", e)
                warnings = []
        # Issue #31: manual=True — bypass weather skip for user-initiated runs.
        ok = scheduler.start_group_sequence(group_id,
                                            override_duration=override_dur,
                                            override_percent=override_pct,
                                            manual=True)
        if not ok:
            return jsonify({"success": False, "message": "Не удалось запустить последовательный полив группы"}), 400
        try:
            db.add_log('group_start_from_first', json.dumps({"group": group_id}))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in api_start_group_from_first: %s", e)
        return jsonify({"success": True,
                        "message": f"Группа {group_id}: запущен последовательный полив",
                        "warnings": warnings})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка запуска группы {group_id} с первой зоны: {e}")
        return jsonify({"success": False, "message": "Ошибка запуска группы"}), 500


@groups_api_bp.route('/api/groups/<int:group_id>/start-zone/<int:zone_id>', methods=['POST'])
@audit_log('zone_start_exclusive', target_extractor=lambda *a, **kw: f"zone:{kw.get('zone_id', a[1] if len(a) > 1 else '?')}")
def api_start_zone_exclusive(group_id, zone_id):
    """Start a zone, stopping all others in the group."""
    try:
        if current_app.config.get('EMERGENCY_STOP'):
            return jsonify({"success": False, "message": "Аварийная остановка активна. Сначала отключите аварийный режим."}), 400
        if not current_app.config.get('TESTING'):
            if _should_throttle_group(int(group_id)):
                return jsonify({"success": True, "message": "Группа уже обрабатывается"})
        try:
            scheduler = get_scheduler()
            if scheduler:
                scheduler.cancel_group_jobs(int(group_id))
        except (ValueError, TypeError, KeyError):
            logger.exception('exclusive start: cancel_group_jobs failed')
        try:
            from services.zone_control import stop_all_in_group as _stop_all
            _stop_all(int(group_id), reason='manual_zone_start_preempt', force=True)
        except ImportError:
            logger.exception('exclusive start: stop_all_in_group failed')
        try:
            from services.zone_control import exclusive_start_zone as _exclusive_start
            ok = _exclusive_start(int(zone_id))
            if not ok:
                return jsonify({"success": False, "message": "Не удалось запустить зону"}), 400
        except (ValueError, TypeError, KeyError) as _e:
            logger.exception('exclusive_start failed')
            return jsonify({"success": False, "message": "Ошибка запуска зоны"}), 500
        try:
            sched = get_scheduler()
            if sched:
                sched.schedule_zone_cap(int(zone_id), cap_minutes=ZONE_CAP_DEFAULT_MIN)
        except (ValueError, TypeError, KeyError):
            logger.exception('schedule zone cap failed')
        try:
            db.clear_scheduled_for_zone_group_peers(int(zone_id), int(group_id))
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in line_249: %s", e)
        try:
            scheduler = get_scheduler()
            if scheduler:
                try:
                    zrec = db.get_zone(int(zone_id)) or {}
                    db.update_zone(int(zone_id), {'watering_start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
                    dur = int(zrec.get('duration') or 0)
                    if dur > 0:
                        db.update_zone(int(zone_id), {'planned_end_time': (datetime.now() + timedelta(minutes=dur)).strftime('%Y-%m-%d %H:%M:%S')})
                except (sqlite3.Error, OSError) as e:
                    logger.debug("Handled exception in line_260: %s", e)
                if not current_app.config.get('TESTING'):
                    try:
                        dur = int((db.get_zone(int(zone_id)) or {}).get('duration') or 0)
                        if dur > 0:
                            scheduler.schedule_zone_stop(int(zone_id), dur, command_id=str(int(time.time())))
                            try:
                                scheduler.schedule_zone_hard_stop(int(zone_id), datetime.now() + timedelta(minutes=dur))
                            except (ValueError, TypeError, KeyError) as e:
                                logger.debug("Handled exception in line_269: %s", e)
                    except (sqlite3.Error, OSError):
                        logger.exception('schedule auto-stop failed')
        except (sqlite3.Error, OSError):
            logger.exception("api_start_zone_exclusive: schedule_zone_stop failed")
        db.add_log('zone_start_exclusive', json.dumps({"group": group_id, "zone": zone_id}))
        return jsonify({"success": True, "message": f"Зона {zone_id} запущена, остальные остановлены"})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка эксклюзивного запуска зоны {zone_id} в группе {group_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка запуска зоны"}), 500


@groups_api_bp.route('/api/groups/<int:group_id>/master-valve/<action>', methods=['POST'])
@audit_log('master_valve_toggle',
           target_extractor=lambda *a, **kw: f"group:{kw.get('group_id', a[0] if a else '?')}:{kw.get('action', a[1] if len(a) > 1 else '?')}")
def api_master_valve_toggle(group_id, action):
    try:
        if current_app.config.get('EMERGENCY_STOP') and str(action).lower() == 'open':
            return jsonify({"success": False, "message": "Аварийная остановка активна"}), 400
        g = next((x for x in (db.get_groups() or []) if int(x.get('id')) == int(group_id)), None)
        if not g:
            return jsonify({"success": False, "message": "Группа не найдена"}), 404
        try:
            if not bool(int(g.get('use_master_valve') or 0)):
                return jsonify({"success": False, "message": "Мастер-клапан не включён для группы"}), 400
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in api_master_valve_toggle: %s", e)
            return jsonify({"success": False, "message": "Мастер-клапан не включён для группы"}), 400
        topic = (g.get('master_mqtt_topic') or '').strip()
        server_id = g.get('master_mqtt_server_id')
        if not topic or not server_id:
            return jsonify({"success": False, "message": "Не задан MQTT сервер или топик для мастер-клапана"}), 400
        server = db.get_mqtt_server(int(server_id))
        if not server:
            return jsonify({"success": False, "message": "MQTT сервер не найден"}), 400
        mode = (g.get('master_mode') or 'NC').upper().strip()
        want_open = str(action).lower() == 'open'
        if not want_open:
            try:
                t_norm = normalize_topic(topic)
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Exception in api_master_valve_toggle: %s", e)
                t_norm = topic if topic.startswith('/') else '/' + str(topic)
            try:
                related_group_ids = []
                for gg in (db.get_groups() or []):
                    try:
                        if int(gg.get('use_master_valve') or 0) != 1:
                            continue
                    except (ValueError, TypeError, KeyError) as e:
                        logger.debug("Exception in line_317: %s", e)
                        continue
                    t2 = (gg.get('master_mqtt_topic') or '').strip()
                    if not t2:
                        continue
                    if normalize_topic(t2) == t_norm:
                        related_group_ids.append(int(gg.get('id')))
                if related_group_ids:
                    for gid2 in related_group_ids:
                        try:
                            for z in (db.get_zones_by_group(int(gid2)) or []):
                                if str(z.get('state') or '').lower() == 'on':
                                    return jsonify({"success": False, "message": "Нельзя закрыть мастер-клапан: в одной из связанных групп идёт полив"}), 400
                        except (sqlite3.Error, OSError) as e:
                            logger.debug("Handled exception in line_331: %s", e)
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Exception in line_333: %s", e)
                return jsonify({"success": False, "message": "Нельзя закрыть мастер-клапан: проверка состояния групп не выполнена"}), 400
        val = ('0' if want_open else '1') if mode == 'NO' else ('1' if want_open else '0')
        try:
            _publish_mqtt_value(server, normalize_topic(topic), val, min_interval_sec=0.0, qos=2, retain=True)
        except (ConnectionError, TimeoutError, OSError):
            logger.exception('master valve publish failed')
            return jsonify({"success": False, "message": "Не удалось отправить команду"}), 500
        try:
            db.update_group_fields(int(group_id), {'master_valve_observed': ('open' if want_open else 'closed')})
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in line_344: %s", e)
        try:
            sched = get_scheduler()
            if sched:
                if want_open:
                    sched.schedule_master_valve_cap(int(group_id), hours=24)
                else:
                    sched.cancel_master_valve_cap(int(group_id))
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Handled exception in line_353: %s", e)
        try:
            payload = json.dumps({'mv_group_id': int(group_id), 'mv_state': ('open' if want_open else 'closed')})
            try:
                _sse_hub.broadcast(payload)
            except (OSError, ValueError, RuntimeError) as e:
                logger.debug("Handled exception in line_359: %s", e)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in line_361: %s", e)
        return jsonify({"success": True})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"api_master_valve_toggle failed: {e}")
        return jsonify({"success": False, "message": "Ошибка"}), 500


# ---------------------------------------------------------------------------
# Issue #15 — POST /api/groups/<gid>/run-selected
#
# Ad-hoc multi-zone run inside a single group. Goes through the same
# IrrigationScheduler.start_group_sequence path as ``start-from-first`` —
# the only thing that's "ad-hoc-specific" is the negative ``program_id``
# sentinel and the explicit ``zone_ids`` subset.
#
# Body:
#   {
#     "zones": [int, int, ...],         # required, non-empty
#     "duration": 1..120,                # optional, minutes mode
#     "duration_percent": 10..200,       # optional, percent mode (minutes wins)
#   }
#
# NOTE: zones table currently has no ``enabled`` column on this branch
# (db/migrations.py — id/state/name/icon/duration/group_id/topic/...).
# Spec §6.1 → drop the "is enabled" check; only validate exists + group.
# ---------------------------------------------------------------------------
def _parse_run_overrides(body: dict):
    """Return ``(override_duration, override_percent, error_message_or_None)``.

    Contract (mirrors PR #21 / issue #12):
      - ``duration`` int in [1, 120] — "minutes mode"; if BOTH given, minutes win.
      - ``duration_percent`` int in [10, 200] — "percent mode".
      - Invalid values → 400-style error message; do NOT silently fall back.
    """
    raw_dur = body.get('duration')
    raw_pct = body.get('duration_percent')

    parsed_dur = None
    if raw_dur is not None:
        try:
            d = int(raw_dur)
        except (ValueError, TypeError):
            return None, None, 'duration должна быть целым числом 1..120'
        if not (1 <= d <= 120):
            return None, None, 'duration вне диапазона 1..120'
        parsed_dur = d

    parsed_pct = None
    if raw_pct is not None:
        try:
            p = int(raw_pct)
        except (ValueError, TypeError):
            return None, None, 'duration_percent должна быть целым числом 10..200'
        if not (10 <= p <= 200):
            return None, None, 'duration_percent вне диапазона 10..200'
        parsed_pct = p

    # "Minutes wins" — if both given, drop percent.
    if parsed_dur is not None and parsed_pct is not None:
        parsed_pct = None
    return parsed_dur, parsed_pct, None


def _build_ad_hoc_name(zone_ids, override_dur, override_pct) -> str:
    """Compact human label for audit / history."""
    z_part = ', '.join(f'Z{int(z)}' for z in zone_ids[:6])
    if len(zone_ids) > 6:
        z_part += f', …+{len(zone_ids) - 6}'
    if override_dur is not None:
        suffix = f'{int(override_dur)} мин'
    elif override_pct is not None:
        suffix = f'{int(override_pct)}% от нормы'
    else:
        suffix = 'нормы'
    return f'Ad-hoc: {z_part} ({suffix})'


@groups_api_bp.route('/api/groups/<int:gid>/run-selected', methods=['POST'])
@rate_limit('programs', max_requests=10, window_sec=60)
@audit_log('prog_manual_run_selected',
           target_extractor=lambda *a, **kw: f"group:{kw.get('gid', a[0] if a else '?')}")
def api_run_selected(gid):
    """Ad-hoc run of a selected subset of zones in one group (issue #15)."""
    try:
        group = next((g for g in (db.get_groups() or []) if int(g['id']) == int(gid)), None)
        if not group:
            return jsonify({'success': False, 'message': 'Группа не найдена'}), 404

        body = request.get_json(silent=True) or {}
        raw_zones = body.get('zones')
        if not isinstance(raw_zones, list) or not raw_zones:
            return jsonify({'success': False, 'message': 'zones обязательны'}), 400
        try:
            zone_ids = [int(z) for z in raw_zones]
        except (ValueError, TypeError):
            return jsonify({'success': False, 'message': 'zones должны быть int[]'}), 400

        # Per-zone validation: exists + belongs to gid.
        # NB: spec §6.1 — no `enabled` column in zones schema, skip that check.
        all_zones = {int(z['id']): z for z in (db.get_zones() or [])}
        for zid in zone_ids:
            z = all_zones.get(int(zid))
            if z is None:
                return jsonify({'success': False, 'message': f'Зона {zid} не найдена'}), 400
            if int(z.get('group_id') or 0) != int(gid):
                return jsonify({'success': False,
                                'message': f'Зона {zid} не принадлежит группе {gid}'}), 400

        override_dur, override_pct, parse_err = _parse_run_overrides(body)
        if parse_err is not None:
            return jsonify({'success': False, 'message': parse_err}), 400

        scheduler = get_scheduler()
        if not scheduler:
            try:
                scheduler = init_scheduler(db)
            except (ValueError, KeyError, RuntimeError) as e:
                logger.debug("api_run_selected: init_scheduler failed: %s", e)
                scheduler = None
        if not scheduler:
            return jsonify({'success': False, 'message': 'Планировщик недоступен'}), 500

        # Negative sentinel — distinguishes ad-hoc runs in audit/history.
        # See spec §1.4. timestamp() is second-resolution; collisions are
        # benign (audit row PK still distinguishes them).
        ad_hoc_id = -int(time.time())
        ad_hoc_name = _build_ad_hoc_name(zone_ids, override_dur, override_pct)

        # Issue #31: manual=True — bypass weather skip for user-initiated runs.
        ok = scheduler.start_group_sequence(
            int(gid),
            override_duration=override_dur,
            override_percent=override_pct,
            zone_ids=zone_ids,
            ad_hoc_program_id=ad_hoc_id,
            ad_hoc_program_name=ad_hoc_name,
            manual=True,
        )
        if not ok:
            return jsonify({'success': False, 'message': 'Не удалось запустить'}), 400

        try:
            db.add_log('prog_manual_run_selected', json.dumps({
                'group_id': int(gid),
                'zones': zone_ids,
                'ad_hoc_program_id': ad_hoc_id,
                'override_duration': override_dur,
                'override_percent': override_pct,
            }))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("api_run_selected: add_log failed: %s", e)

        return jsonify({
            'success': True,
            'message': f"Группа {group.get('name')}: запущены {len(zone_ids)} зон(ы)",
            'ad_hoc_program_id': ad_hoc_id,
        })
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"api_run_selected failed for group {gid}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка запуска выбранных зон'}), 500
