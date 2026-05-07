import logging
import os
import threading
from datetime import datetime
from typing import Optional
import time

from constants import MASTER_VALVE_CLOSE_DELAY_SEC
from database import db
from services.locks import group_lock, zone_lock
from services.mqtt_pub import publish_mqtt_value
from utils import normalize_topic
from services.monitors import water_monitor
from services.observed_state import state_verifier
import sqlite3

logger = logging.getLogger(__name__)


# Pending master-valve close timers keyed by normalized master MQTT topic.
# Used to coalesce/cancel concurrent close attempts so that a freshly
# scheduled close supersedes a pending one for the same topic.
_PENDING_CLOSE_TIMERS = {}  # type: dict[str, threading.Timer]
_PENDING_CLOSE_LOCK = threading.Lock()


def _schedule_master_close(group_dict: dict, immediate: bool = False) -> None:
    """Schedule (or perform immediately) a master-valve close for the given group.

    - Reads ``master_close_delay_sec`` from the group dict (falls back to
      ``MASTER_VALVE_CLOSE_DELAY_SEC``); ``immediate=True`` forces zero delay.
    - Cancels any pending close for the same master topic before scheduling.
    - When the timer fires (or immediately), checks zones across all groups
      sharing this master topic — counts both ``state == 'on'`` and
      ``state == 'starting'`` to avoid race conditions during transitions.
    - Skips scheduling under TESTING (mirrors prior behaviour).
    """
    try:
        if not group_dict:
            return
        try:
            if int(group_dict.get('use_master_valve') or 0) != 1:
                return
        except (ValueError, TypeError):
            return
        mtopic = (group_dict.get('master_mqtt_topic') or '').strip()
        msid = group_dict.get('master_mqtt_server_id')
        if not mtopic or not msid:
            return
        try:
            gid = int(group_dict.get('id') or 0)
        except (ValueError, TypeError):
            gid = 0
        try:
            _raw_delay = group_dict.get('master_close_delay_sec')
            delay = int(_raw_delay) if _raw_delay is not None else MASTER_VALVE_CLOSE_DELAY_SEC
        except (ValueError, TypeError):
            delay = MASTER_VALVE_CLOSE_DELAY_SEC
        delay = max(1, delay)
        if immediate:
            delay = 0

        try:
            t_norm = normalize_topic(mtopic)
        except (ValueError, TypeError, OSError):
            t_norm = mtopic

        def _do_close():
            try:
                # Check ON or STARTING zones across all groups sharing the same master topic
                any_on = False
                for gg in (db.get_groups() or []):
                    try:
                        gg_topic = (gg.get('master_mqtt_topic') or '').strip()
                        if not gg_topic:
                            continue
                        if normalize_topic(gg_topic) != t_norm:
                            continue
                    except (ValueError, TypeError, OSError):
                        continue
                    for z2 in (db.get_zones_by_group(int(gg.get('id'))) or []):
                        st = str(z2.get('state') or '').lower()
                        if st in ('on', 'starting'):
                            any_on = True
                            break
                    if any_on:
                        break
                if any_on:
                    return
                mserver = db.get_mqtt_server(int(msid))
                if not mserver:
                    return
                try:
                    mode = (group_dict.get('master_mode') or 'NC').strip().upper()
                except (ValueError, TypeError, KeyError):
                    mode = 'NC'
                close_val = '1' if mode == 'NO' else '0'
                publish_mqtt_value(mserver, t_norm, close_val,
                                   min_interval_sec=0.0, qos=2, retain=True,
                                   meta={'cmd': 'master_off'})
                if gid:
                    try:
                        db.update_group_fields(int(gid), {'master_valve_observed': 'closed'})
                        from services import sse_hub as _sse_hub_c
                        import json as _json_c
                        _sse_hub_c.broadcast(_json_c.dumps({'mv_group_id': int(gid), 'mv_state': 'closed'}))
                    except (sqlite3.Error, OSError, ImportError, ValueError, TypeError) as e:
                        logger.debug("master_valve_observed update (closed) failed: %s", e)
            except (ConnectionError, TimeoutError, OSError):
                logger.exception('master valve delayed close failed')

        # Cancel any pending close for this topic (covers both delayed and immediate paths)
        with _PENDING_CLOSE_LOCK:
            prev = _PENDING_CLOSE_TIMERS.pop(t_norm, None)
        if prev is not None:
            try:
                prev.cancel()
            except (RuntimeError, OSError):
                pass

        if os.environ.get('TESTING'):
            return

        if delay <= 0:
            # Run inline-but-non-blocking on a daemon thread to keep semantics
            # consistent (callers don't expect to block on master close).
            t = threading.Timer(0.0, _do_close)
            t.daemon = True
            with _PENDING_CLOSE_LOCK:
                _PENDING_CLOSE_TIMERS[t_norm] = t
            t.start()
            return

        timer = threading.Timer(float(delay), _do_close)
        timer.daemon = True
        with _PENDING_CLOSE_LOCK:
            _PENDING_CLOSE_TIMERS[t_norm] = timer
        timer.start()
    except (RuntimeError, OSError, ValueError, TypeError):
        logger.exception('schedule master close failed')


def _versioned_update(zone_id: int, updates: dict) -> None:
    ok = False
    try:
        ok = db.update_zone_versioned(zone_id, updates)
    except (sqlite3.Error, OSError) as e:
        logger.debug("Exception in _versioned_update: %s", e)
        ok = False
    if not ok:
        db.update_zone(zone_id, updates)


def _is_valid_start_state(state: str) -> bool:
    s = str(state or '').lower()
    return s in ('off', 'stopping')


def _is_valid_stop_state(state: str) -> bool:
    s = str(state or '').lower()
    return s in ('on', 'starting')


def exclusive_start_zone(zone_id: int) -> bool:
    """Start zone and stop others in its group. Returns True on success."""
    try:
        z = db.get_zone(zone_id)
        if not z:
            return False
        # For diagnostics/meta: allow passing through a command id if set by callers in future
        command_id = None  # type: Optional[str]
        group_id = int(z.get('group_id') or 0)
        # Serialize on group
        with group_lock(group_id):
            group_zones = db.get_zones_by_group(group_id) if group_id else []
            start_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            # Start current with state-machine: off/stopping -> starting -> on
            with zone_lock(zone_id):
                cur_state = str((db.get_zone(zone_id) or {}).get('state') or '').lower()
                if cur_state in ('on', 'starting'):
                    pass
                else:
                    _versioned_update(zone_id, {'state': 'starting', 'commanded_state': 'on', 'watering_start_time': start_ts})
            try:
                # Снапшот счётчика воды на старте (если у группы есть счётчик)
                try:
                    gid = int(z.get('group_id') or 0)
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Exception in exclusive_start_zone: %s", e)
                    gid = 0
                if gid and gid != 999:
                    try:
                        g = next((gg for gg in (db.get_groups() or []) if int(gg.get('id')) == gid), None)
                    except (sqlite3.Error, OSError) as e:
                        logger.debug("Exception in line_68: %s", e)
                        g = None
                    if g and int(g.get('use_water_meter') or 0) == 1:
                        try:
                            # Берём пульсы на/до момента старта, чтобы избежать лагов подписки
                            raw = water_monitor.get_pulses_at_or_before(gid, time.time())
                            pulse = str(g.get('water_pulse_size') or '1l')
                            liters = 100 if pulse == '100l' else 10 if pulse == '10l' else 1
                            base_m3 = float(g.get('water_base_value_m3') or 0.0)
                            db.create_zone_run(int(zone_id), gid, start_ts, time.monotonic(), raw, liters, base_m3)
                        except (sqlite3.Error, OSError):
                            logger.exception('start snapshot failed')
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in line_81: %s", e)
            try:
                sid = z.get('mqtt_server_id'); topic = (z.get('topic') or '').strip()
                gid = int(z.get('group_id') or 0)
                # Pre-open master valve by group (idempotent, mode-aware)
                if gid and gid != 999:
                    try:
                        g = next((gg for gg in (db.get_groups() or []) if int(gg.get('id')) == gid), None)
                    except (sqlite3.Error, OSError) as e:
                        logger.debug("Exception in line_90: %s", e)
                        g = None
                    if g and int(g.get('use_master_valve') or 0) == 1:
                        mtopic = (g.get('master_mqtt_topic') or '').strip()
                        msid = g.get('master_mqtt_server_id')
                        if mtopic and msid:
                            mserver = db.get_mqtt_server(int(msid))
                            if mserver:
                                try:
                                    mode = (g.get('master_mode') or 'NC').strip().upper()
                                except (ValueError, TypeError, KeyError) as e:
                                    logger.debug("Exception in line_101: %s", e)
                                    mode = 'NC'
                                open_val = '0' if mode == 'NO' else '1'
                                publish_mqtt_value(mserver, normalize_topic(mtopic), open_val, min_interval_sec=0.0, qos=2, retain=True)
                                try:
                                    db.update_group_fields(int(gid), {'master_valve_observed': 'open'})
                                    from services import sse_hub as _sse_hub
                                    import json as _json
                                    _sse_hub.broadcast(_json.dumps({'mv_group_id': int(gid), 'mv_state': 'open'}))
                                except (sqlite3.Error, OSError, ImportError, ValueError, TypeError) as e:
                                    logger.debug("master_valve_observed update (open) failed: %s", e)
                if sid and topic:
                    server = db.get_mqtt_server(int(sid))
                    if server:
                        publish_mqtt_value(server, normalize_topic(topic), '1', min_interval_sec=0.0, qos=2, retain=True, meta={'cmd': str(command_id) if 'command_id' in locals() and command_id else None, 'ver': str((z.get('version') or 0) + 1)})
                        # transition to on
                        _versioned_update(zone_id, {'state': 'on'})
                        # Verify observed_state in background thread
                        try:
                            state_verifier.verify_async(int(zone_id), 'on')
                        except (ValueError, TypeError, KeyError):
                            logger.debug("observed_state verify_async(on) launch failed")
            except (ConnectionError, TimeoutError, OSError):
                logger.exception("exclusive_start_zone: mqtt on failed")
            # Stop others in parallel to reduce latency
            try:
                import concurrent.futures
                def _stop_peer(other):
                    try:
                        oid = int(other.get('id'))
                        if oid == int(zone_id):
                            return
                        with zone_lock(oid):
                            ost = str((db.get_zone(oid) or {}).get('state') or '').lower()
                            if ost not in ('off',):
                                _versioned_update(oid, {'state': 'stopping', 'commanded_state': 'off'})
                        osid = other.get('mqtt_server_id'); otopic = (other.get('topic') or '').strip()
                        if osid and otopic:
                            server_o = db.get_mqtt_server(int(osid))
                            if server_o:
                                publish_mqtt_value(server_o, normalize_topic(otopic), '0', min_interval_sec=0.0, qos=2, retain=True, meta={'cmd': 'peer_off', 'ver': str((other.get('version') or 0) + 1)})
                                last_time = other.get('watering_start_time')
                                _versioned_update(oid, {'state': 'off', 'watering_start_time': None, 'last_watering_time': last_time})
                    except (ConnectionError, TimeoutError, OSError):
                        logger.exception("exclusive_start_zone: mqtt off peer failed")

                with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(group_zones)-1))) as pool:
                    pool.map(_stop_peer, group_zones)
            except (ImportError, RuntimeError, OSError) as e:
                logger.warning("Parallel peer stop failed, falling back to sequential: %s", e)
                # Fallback to sequential if parallelization fails for any reason
                for other in group_zones:
                    try:
                        oid = int(other.get('id'))
                        if oid == int(zone_id):
                            continue
                        with zone_lock(oid):
                            ost = str((db.get_zone(oid) or {}).get('state') or '').lower()
                            if ost not in ('off',):
                                _versioned_update(oid, {'state': 'stopping', 'commanded_state': 'off'})
                        osid = other.get('mqtt_server_id'); otopic = (other.get('topic') or '').strip()
                        if osid and otopic:
                            server_o = db.get_mqtt_server(int(osid))
                            if server_o:
                                publish_mqtt_value(server_o, normalize_topic(otopic), '0', min_interval_sec=0.0, qos=2, retain=True, meta={'cmd': 'peer_off', 'ver': str((other.get('version') or 0) + 1)})
                                last_time = other.get('watering_start_time')
                                _versioned_update(oid, {'state': 'off', 'watering_start_time': None, 'last_watering_time': last_time})
                    except (ConnectionError, TimeoutError, OSError):
                        logger.exception("exclusive_start_zone: mqtt off peer failed (sequential)")
        try:
            # publish event
            from services import events as _ev
            _ev.publish({'type':'zone_start','id': int(zone_id), 'by':'api'})
        except ImportError as e:
            logger.debug("Handled exception in line_168: %s", e)
        return True
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error, ValueError):
        logger.exception("exclusive_start_zone failed")
        return False


def stop_zone(zone_id: int, reason: str = 'manual', force: bool = False,
              master_close_immediately: bool = False) -> bool:
    """Единый стоп зоны. Идемпотентно. Публикует OFF и фиксирует в БД.
    reason: для журналирования; force — останавливать даже если state уже off.
    master_close_immediately: при True мастер-клапан закрывается без задержки
    (используется для emergency_stop / rain).
    """
    try:
        z = db.get_zone(zone_id)
        if not z:
            return False
        if (str(z.get('state')).lower() in ('off', 'stopping')) and not force:
            # Зона уже оффлайн (часто по MQTT). Тем не менее, попробуем посчитать и сохранить статистику воды.
            try:
                gid = int(z.get('group_id') or 0)
                if gid and gid != 999:
                    total_liters = None; avg_lpm = None
                    # 1) Если есть открытый run — завершим его по текущим пульсам
                    try:
                        run = db.get_open_zone_run(int(zone_id))
                    except (sqlite3.Error, OSError) as e:
                        logger.debug("Exception in stop_zone: %s", e)
                        run = None
                    if run:
                        try:
                            end_raw = water_monitor.get_pulses_at_or_after(gid, time.time())
                        except (ValueError, TypeError, AttributeError, OSError) as e:
                            logger.debug("Exception in stop_zone: %s", e)
                            end_raw = None
                        try:
                            start_raw = run.get('start_raw_pulses')
                            liters_per_pulse = int(run.get('pulse_liters_at_start') or 1)
                            end_mono = time.monotonic()
                            start_mono = float(run.get('start_monotonic') or 0.0)
                            dp = None if (end_raw is None or start_raw is None) else max(0, int(end_raw) - int(start_raw))
                            if dp is not None:
                                total_liters = round(dp * liters_per_pulse, 2)
                                dur_sec = max(1.0, end_mono - start_mono)
                                avg_lpm = round(total_liters / (dur_sec / 60.0), 2)
                            db.finish_zone_run(int(run['id']), datetime.now().strftime('%Y-%m-%d %H:%M:%S'), end_mono, end_raw, total_liters, avg_lpm, status='ok')
                        except (sqlite3.Error, OSError):
                            logger.exception('finish snapshot (already off) failed')
                    # 2) Фоллбэк: по времени последнего полива/старта посчитаем суммарно
                    if (total_liters is None) and (avg_lpm is None):
                        try:
                            since_iso = z.get('last_watering_time') or z.get('watering_start_time')
                        except (KeyError, TypeError, ValueError) as e:
                            logger.debug("Exception in line_219: %s", e)
                            since_iso = None
                        if since_iso:
                            t_l, a_lpm = water_monitor.summarize_run(gid, since_iso)
                            total_liters = t_l if t_l is not None else total_liters
                            avg_lpm = a_lpm if a_lpm is not None else avg_lpm
                    if (total_liters is not None) or (avg_lpm is not None):
                        updates = {}
                        if avg_lpm is not None:
                            updates['last_avg_flow_lpm'] = avg_lpm
                        if total_liters is not None:
                            updates['last_total_liters'] = total_liters
                        if updates:
                            db.update_zone(int(zone_id), updates)
            except (sqlite3.Error, OSError, ValueError, TypeError):
                logger.exception('stop_zone (already off): water stats update failed')
            # Even when the zone was already off (idempotent path), the caller
            # may want to (re)schedule a master-valve close — required for
            # emergency_stop / rain monitor paths and for keeping idempotency
            # on duplicate manual stops.
            try:
                gid_eo = int(z.get('group_id') or 0)
                if gid_eo and gid_eo != 999:
                    try:
                        g_eo = next((gg for gg in (db.get_groups() or []) if int(gg.get('id')) == gid_eo), None)
                    except (sqlite3.Error, OSError) as _e:
                        logger.debug("stop_zone (already off): get_groups failed: %s", _e)
                        g_eo = None
                    if g_eo and int(g_eo.get('use_master_valve') or 0) == 1:
                        _schedule_master_close(g_eo, immediate=bool(master_close_immediately))
            except (ValueError, TypeError, KeyError) as _e:
                logger.debug("stop_zone (already off): master close scheduling skipped: %s", _e)
            return True
        last_time = z.get('watering_start_time')
        # Стейт: on/starting -> stopping
        with zone_lock(zone_id):
            _versioned_update(zone_id, {'state': 'stopping', 'commanded_state': 'off'})
        sid = z.get('mqtt_server_id'); topic = (z.get('topic') or '').strip()
        try:
            if sid and topic:
                server = db.get_mqtt_server(int(sid))
                if server:
                    # OFF публикуем с retain=True, чтобы состояние восстанавливалось после перезапуска
                    publish_mqtt_value(server, normalize_topic(topic), '0', min_interval_sec=0.0, qos=2, retain=True, meta={'cmd':'stop','ver':str((z.get('version') or 0) + 1)})
                    # Verify observed_state in background thread
                    try:
                        state_verifier.verify_async(int(zone_id), 'off')
                    except (ValueError, TypeError, KeyError):
                        logger.debug("observed_state verify_async(off) launch failed")
                    # Delayed master valve close — uses per-group delay
                    # (master_close_delay_sec) and proper cancellable timer.
                    try:
                        gid = int(z.get('group_id') or 0)
                        if gid and gid != 999:
                            g = next((gg for gg in (db.get_groups() or []) if int(gg.get('id')) == gid), None)
                            if g and int(g.get('use_master_valve') or 0) == 1:
                                _schedule_master_close(g, immediate=bool(master_close_immediately))
                    except (ConnectionError, TimeoutError, OSError, RuntimeError):
                        logger.exception('master valve close scheduling failed')
        except (ConnectionError, TimeoutError, OSError, sqlite3.Error):
            logger.exception('stop_zone: mqtt off failed')
        # Завершаем переход: stopping -> off
        with zone_lock(zone_id):
            _versioned_update(zone_id, {'state': 'off', 'watering_start_time': None, 'last_watering_time': last_time, 'planned_end_time': None})
        # Обновим статистику воды для зоны, если группа использует счётчик
        try:
            gid = int(z.get('group_id') or 0)
            total_liters = None; avg_lpm = None
            if gid and gid != 999:
                # Попробуем быстрый расчёт по снапшотам
                try:
                    run = db.get_open_zone_run(int(zone_id))
                except (sqlite3.Error, OSError) as e:
                    logger.debug("Exception in line_305: %s", e)
                    run = None
                if run:
                    try:
                        # Берём пульсы на/после момента стопа, чтобы избежать лагов
                        end_raw = water_monitor.get_pulses_at_or_after(gid, time.time())
                    except (ValueError, TypeError, AttributeError, OSError) as e:
                        logger.debug("Exception in line_312: %s", e)
                        end_raw = None
                    try:
                        start_raw = run.get('start_raw_pulses')
                        liters_per_pulse = int(run.get('pulse_liters_at_start') or 1)
                        end_mono = time.monotonic()
                        start_mono = float(run.get('start_monotonic') or 0.0)
                        dp = None if (end_raw is None or start_raw is None) else max(0, int(end_raw) - int(start_raw))
                        if dp is not None:
                            total_liters = round(dp * liters_per_pulse, 2)
                            dur_sec = max(1.0, end_mono - start_mono)
                            avg_lpm = round(total_liters / (dur_sec / 60.0), 2)
                        db.finish_zone_run(int(run['id']), datetime.now().strftime('%Y-%m-%d %H:%M:%S'), end_mono, end_raw, total_liters, avg_lpm, status='ok')
                    except (sqlite3.Error, OSError):
                        logger.exception('finish snapshot failed')
                # Если снапшоты не дали результата — fallback к summarize_run
                if (total_liters is None) and (avg_lpm is None):
                    t_l, a_lpm = water_monitor.summarize_run(gid, last_time)
                    total_liters = t_l if t_l is not None else total_liters
                    avg_lpm = a_lpm if a_lpm is not None else avg_lpm
            if total_liters is not None or avg_lpm is not None:
                updates = {}
                if avg_lpm is not None:
                    updates['last_avg_flow_lpm'] = avg_lpm
                if total_liters is not None:
                    updates['last_total_liters'] = total_liters
                if updates:
                    db.update_zone(int(zone_id), updates)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            logger.exception('stop_zone: water stats update failed')
        try:
            db.add_log('zone_stop', f'{reason}: zone={int(zone_id)}')
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in line_345: %s", e)
        try:
            from services import events as _ev
            _ev.publish({'type':'zone_stop','id': int(zone_id), 'by': reason})
        except (ImportError, AttributeError) as e:
            logger.debug("Event publish failed: %s", e)
        return True
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error, ValueError):
        logger.exception('stop_zone failed')
        return False


def stop_all_in_group(group_id: int, reason: str = 'group_cancel', force: bool = False,
                      master_close_immediately: bool = False) -> None:
    """Немедленно остановить все зоны в группе (идемпотентно).

    master_close_immediately: при True мастер-клапан закрывается без задержки
    (используется для emergency_stop / rain).
    """
    try:
        zones = db.get_zones_by_group(int(group_id))
        for z in zones:
            try:
                stop_zone(int(z['id']), reason=reason, force=force,
                          master_close_immediately=master_close_immediately)
                # Небольшая пауза, чтобы избежать всплесков при публикации на слабом железе (пропускаем в тестах)
                try:
                    if os.environ.get('TESTING', '0') != '1':
                        time.sleep(0.05)
                except (KeyError, TypeError, ValueError) as e:
                    logger.debug("Handled exception in stop_all_in_group: %s", e)
            except (ValueError, TypeError, KeyError):
                logger.exception('stop_all_in_group: stop_zone failed')
    except (sqlite3.Error, OSError):
        logger.exception('stop_all_in_group failed')
