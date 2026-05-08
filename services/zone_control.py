import logging
import os
import threading
from datetime import datetime
from typing import Optional
import time

from constants import MASTER_VALVE_CLOSE_DELAY_SEC
from config import TESTING
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

        # Always log the plant intent so post-incident triage can see WHEN/HOW
        # the master-close timer was armed and from where (group, delay).
        logger.info(
            "master_close planted: gid=%s topic=%s delay=%ds immediate=%s",
            gid, t_norm, delay, immediate,
        )

        def _do_close():
            try:
                # Check ON or STARTING zones across all groups sharing the same master topic
                any_on = False
                blocking_zone_id = None
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
                            blocking_zone_id = z2.get('id')
                            break
                    if any_on:
                        break
                if any_on:
                    logger.info("master close skipped: topic=%s blocked by zone=%s state=on/starting",
                                t_norm, blocking_zone_id)
                    return
                mserver = db.get_mqtt_server(int(msid))
                if not mserver:
                    logger.warning("master close skipped: topic=%s msid=%s server not found", t_norm, msid)
                    return
                try:
                    mode = (group_dict.get('master_mode') or 'NC').strip().upper()
                except (ValueError, TypeError, KeyError):
                    mode = 'NC'
                close_val = '1' if mode == 'NO' else '0'
                publish_mqtt_value(mserver, t_norm, close_val,
                                   min_interval_sec=0.0, qos=2, retain=True,
                                   meta={'cmd': 'master_off'})
                logger.info("master close published: topic=%s val=%s mode=%s gid=%s",
                            t_norm, close_val, mode, gid)
                # Always-on audit: delayed/auto master-valve close.
                # Important for triage of "why did watering stop early?" — links
                # the publish to the originating group and mode (NC/NO).
                try:
                    from services.audit import record_audit
                    record_audit(
                        action_type='master_valve_auto_close',
                        source='zone_control',
                        target=f'group:{int(gid)}' if gid else f'master_topic:{t_norm}',
                        payload={'topic': t_norm, 'value': close_val,
                                 'mode': mode, 'group_id': int(gid) if gid else None},
                        actor='system',
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("master_valve_auto_close: record_audit failed")
                if gid:
                    try:
                        db.update_group_fields(int(gid), {'master_valve_observed': 'closed'})
                        from services import sse_hub as _sse_hub_c
                        import json as _json_c
                        _sse_hub_c.broadcast(_json_c.dumps({'mv_group_id': int(gid), 'mv_state': 'closed'}))
                    except (sqlite3.Error, OSError, ImportError, ValueError, TypeError) as e:
                        logger.debug("master_valve_observed update (closed) failed: %s", e)
            except Exception:
                logger.exception('master valve delayed close failed (topic=%s)', t_norm)

        # Cancel any pending close for this topic (covers both delayed and immediate paths)
        with _PENDING_CLOSE_LOCK:
            prev = _PENDING_CLOSE_TIMERS.pop(t_norm, None)
        if prev is not None:
            try:
                prev.cancel()
            except (RuntimeError, OSError):
                pass

        if TESTING:
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


# Canonical state-write helper lives in services/zones_state.py to avoid the
# circular-import problem (sse_hub / observed_state want to emit audited
# state transitions but those modules are imported by zone_control itself).
# zone_control keeps a thin alias so existing internal callers (and any
# downstream code that imports services.zone_control._versioned_update)
# continue to work unchanged.
from services.zones_state import update_zone_state as _update_zone_state


def _versioned_update(zone_id: int, updates: dict, *, audit_reason: str = '') -> None:
    """Backwards-compatible thin wrapper around ``zones_state.update_zone_state``.

    Pre-existing callers in this module (and in tests) invoke
    ``_versioned_update`` and ignore the return value; we preserve that
    contract here while delegating the actual write + audit emit to the
    canonical helper.
    """
    _update_zone_state(zone_id, updates, audit_reason=audit_reason)


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
                    _versioned_update(zone_id, {'state': 'starting', 'commanded_state': 'on', 'watering_start_time': start_ts}, audit_reason='manual_start')
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
                    except (sqlite3.Error, OSError):
                        logger.exception("exclusive_start_zone: get_groups failed (zone=%s gid=%s)", zone_id, gid)
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
                    except (sqlite3.Error, OSError):
                        logger.exception("exclusive_start_zone: master-valve pre-open: get_groups failed (zone=%s gid=%s)", zone_id, gid)
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
                        _versioned_update(zone_id, {'state': 'on'}, audit_reason='mqtt_ack_on')
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
                                _versioned_update(oid, {'state': 'stopping', 'commanded_state': 'off'}, audit_reason='peer_stop')
                        osid = other.get('mqtt_server_id'); otopic = (other.get('topic') or '').strip()
                        if osid and otopic:
                            server_o = db.get_mqtt_server(int(osid))
                            if server_o:
                                publish_mqtt_value(server_o, normalize_topic(otopic), '0', min_interval_sec=0.0, qos=2, retain=True, meta={'cmd': 'peer_off', 'ver': str((other.get('version') or 0) + 1)})
                                # Issue #2: last_watering_time must reflect when watering ENDED, not when it started.
                                last_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                _versioned_update(oid, {'state': 'off', 'watering_start_time': None, 'last_watering_time': last_time}, audit_reason='peer_off')
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
                                _versioned_update(oid, {'state': 'stopping', 'commanded_state': 'off'}, audit_reason='peer_stop')
                        osid = other.get('mqtt_server_id'); otopic = (other.get('topic') or '').strip()
                        if osid and otopic:
                            server_o = db.get_mqtt_server(int(osid))
                            if server_o:
                                publish_mqtt_value(server_o, normalize_topic(otopic), '0', min_interval_sec=0.0, qos=2, retain=True, meta={'cmd': 'peer_off', 'ver': str((other.get('version') or 0) + 1)})
                                # Issue #2: last_watering_time must reflect when watering ENDED, not when it started.
                                last_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                _versioned_update(oid, {'state': 'off', 'watering_start_time': None, 'last_watering_time': last_time}, audit_reason='peer_off')
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
              master_close_immediately: bool = False,
              skip_master_close: bool = False) -> bool:
    """Единый стоп зоны. Идемпотентно. Публикует OFF и фиксирует в БД.
    reason: для журналирования; force — останавливать даже если state уже off.
    master_close_immediately: при True мастер-клапан закрывается без задержки
    (используется для emergency_stop / rain).
    skip_master_close: при True мастер-клапан вообще не планируется к закрытию
    (вызывающий сам управляет master close, например emergency_stop_all Phase C).
    """
    # Audit-friendly entry log — captures WHO/WHY before any state mutation
    # so post-incident triage can replay the call from logs alone.
    logger.info(
        "stop_zone called: zone_id=%s reason=%s force=%s "
        "master_close_immediately=%s skip_master_close=%s",
        zone_id, reason, force, master_close_immediately, skip_master_close,
    )
    try:
        z = db.get_zone(zone_id)
        if not z:
            logger.info("stop_zone exit: zone_id=%s not found in DB", zone_id)
            return False
        if (str(z.get('state')).lower() in ('off', 'stopping')) and not force:
            logger.info(
                "stop_zone idempotent: zone_id=%s already state=%s reason=%s — "
                "running water-stats finalisation but no MQTT publish",
                zone_id, z.get('state'), reason,
            )
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
                    if g_eo and int(g_eo.get('use_master_valve') or 0) == 1 and not skip_master_close:
                        _schedule_master_close(g_eo, immediate=bool(master_close_immediately))
            except (ValueError, TypeError, KeyError) as _e:
                logger.debug("stop_zone (already off): master close scheduling skipped: %s", _e)
            return True
        # Issue #2: separate start/end timestamps.
        # `start_iso` is the original watering start — used as the lower bound
        # for summarize_run() water-stats fallback below (must NOT be touched).
        # `end_iso` is the moment the zone is actually being stopped — that's
        # what UI/users mean by "last watering time".
        start_iso = z.get('watering_start_time')
        end_iso = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # Стейт: on/starting -> stopping
        with zone_lock(zone_id):
            _versioned_update(zone_id, {'state': 'stopping', 'commanded_state': 'off'}, audit_reason=f'stop_{reason}')
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
                        if gid and gid != 999 and not skip_master_close:
                            g = next((gg for gg in (db.get_groups() or []) if int(gg.get('id')) == gid), None)
                            if g and int(g.get('use_master_valve') or 0) == 1:
                                _schedule_master_close(g, immediate=bool(master_close_immediately))
                    except (ConnectionError, TimeoutError, OSError, RuntimeError):
                        logger.exception('master valve close scheduling failed')
        except (ConnectionError, TimeoutError, OSError, sqlite3.Error):
            logger.exception('stop_zone: mqtt off failed')
        # Завершаем переход: stopping -> off
        with zone_lock(zone_id):
            _versioned_update(zone_id, {'state': 'off', 'watering_start_time': None, 'last_watering_time': end_iso, 'planned_end_time': None}, audit_reason=f'stop_{reason}_complete')
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
                # Если снапшоты не дали результата — fallback к summarize_run.
                # NOTE: summarize_run needs the ORIGINAL start time (start_iso)
                # as its lower bound — it integrates pulse counts since then.
                # Do NOT pass end_iso here.
                if (total_liters is None) and (avg_lpm is None):
                    t_l, a_lpm = water_monitor.summarize_run(gid, start_iso)
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
                      master_close_immediately: bool = False,
                      skip_master_close: bool = False) -> None:
    """Немедленно остановить все зоны в группе (идемпотентно).

    master_close_immediately: при True мастер-клапан закрывается без задержки.
    skip_master_close: при True мастер-клапан вообще не планируется
    (вызывающий сам управляет закрытием — например emergency_stop_all).
    """
    try:
        zones = db.get_zones_by_group(int(group_id))
        for z in zones:
            try:
                stop_zone(int(z['id']), reason=reason, force=force,
                          master_close_immediately=master_close_immediately,
                          skip_master_close=skip_master_close)
                # Небольшая пауза, чтобы избежать всплесков при публикации на слабом железе (пропускаем в тестах)
                try:
                    if not TESTING:
                        time.sleep(0.05)
                except (KeyError, TypeError, ValueError) as e:
                    logger.debug("Handled exception in stop_all_in_group: %s", e)
            except (ValueError, TypeError, KeyError):
                logger.exception('stop_all_in_group: stop_zone failed')
    except (sqlite3.Error, OSError):
        logger.exception('stop_all_in_group failed')


def emergency_stop_all(reason: str = 'emergency_stop') -> dict:
    """Синхронная аварийная остановка всех групп с детерминированной последовательностью.

    Phase A: для каждой группы → последовательно stop_zone(skip_master_close=True)
             для всех зон. Master-таймеры здесь не планируются вовсе — мастер
             закроем явно в фазе C.
    Phase B: ожидание до 2с, чтобы все зоны перешли в state='off'. Если по
             таймауту остались зоны в on/starting — повторно force-стопаем их.
    Phase C: для каждой группы с use_master_valve=1 → SYNC publish close_val
             на master_mqtt_topic напрямую (без threading.Timer, без race).

    Возвращает dict со счётчиками для логирования/диагностики.
    """
    stats = {
        'groups_total': 0,
        'zones_stopped': 0,
        'zones_force_retried': 0,
        'masters_closed': 0,
        'masters_skipped_no_use_master': 0,
        'masters_skipped_no_topic': 0,
        'masters_skipped_dup_topic': 0,
        'masters_failed_publish': 0,
        'zones_still_active_after_wait': 0,
    }
    try:
        groups = db.get_groups() or []
    except (sqlite3.Error, OSError):
        logger.exception('emergency_stop_all: get_groups failed')
        return stats

    stats['groups_total'] = len(groups)
    logger.info("emergency_stop_all: starting phase A — stop zones across %d groups", len(groups))

    # Phase A: stop all zones in all groups WITHOUT scheduling any master close.
    # Master will be closed synchronously in Phase C — we do NOT want lingering
    # delay=60 timers firing later and republishing close.
    for g in groups:
        try:
            gid = int(g.get('id') or 0)
        except (ValueError, TypeError):
            continue
        if not gid:
            continue
        try:
            zones = db.get_zones_by_group(gid) or []
        except (sqlite3.Error, OSError):
            logger.exception('emergency_stop_all: get_zones_by_group(%s) failed', gid)
            continue
        for z in zones:
            try:
                stop_zone(int(z['id']), reason=reason, force=True,
                          master_close_immediately=False, skip_master_close=True)
                stats['zones_stopped'] += 1
                if not TESTING:
                    time.sleep(0.02)
            except (ValueError, TypeError, KeyError, sqlite3.Error, OSError):
                logger.exception('emergency_stop_all: stop_zone failed (zone_id=%s)', z.get('id'))

    # Phase B: wait up to 2s for all zones to reach state='off' (or 'stopping' is also OK
    # — we treat 'stopping' as in-flight-but-OFF-published; only on/starting are blockers).
    if not TESTING:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            still_active = 0
            try:
                for g in groups:
                    try:
                        gid = int(g.get('id') or 0)
                    except (ValueError, TypeError):
                        continue
                    if not gid:
                        continue
                    for z in (db.get_zones_by_group(gid) or []):
                        st = str(z.get('state') or '').lower()
                        if st in ('on', 'starting'):
                            still_active += 1
            except (sqlite3.Error, OSError):
                logger.exception('emergency_stop_all: phase B check failed')
                break
            if still_active == 0:
                break
            time.sleep(0.1)
        # Re-issue stop_zone(force=True) for any zone STILL in on/starting at deadline.
        try:
            stuck_zones = []
            for g in groups:
                try:
                    gid = int(g.get('id') or 0)
                except (ValueError, TypeError):
                    continue
                if not gid:
                    continue
                for z in (db.get_zones_by_group(gid) or []):
                    if str(z.get('state') or '').lower() in ('on', 'starting'):
                        stuck_zones.append(int(z.get('id') or 0))
            if stuck_zones:
                logger.warning("emergency_stop_all: %d zones stuck after 2s — force-retry: %s",
                               len(stuck_zones), stuck_zones)
                for zid in stuck_zones:
                    if not zid:
                        continue
                    try:
                        stop_zone(zid, reason=reason + '_retry', force=True,
                                  master_close_immediately=False, skip_master_close=True)
                        stats['zones_force_retried'] += 1
                        if not TESTING:
                            time.sleep(0.02)
                    except (ValueError, TypeError, KeyError, sqlite3.Error, OSError):
                        logger.exception('emergency_stop_all: force-retry failed (zone_id=%s)', zid)
            stats['zones_still_active_after_wait'] = len(stuck_zones)
        except (sqlite3.Error, OSError):
            logger.exception('emergency_stop_all: phase B re-issue failed')

    # Phase C: synchronously close each master valve (one publish per master topic)
    logger.info("emergency_stop_all: phase C — closing master valves")
    seen_topics = {}  # type: dict[str, str]  # t_norm -> mode used
    for g in groups:
        try:
            gid = int(g.get('id') or 0)
        except (ValueError, TypeError):
            continue
        if not gid:
            continue
        try:
            use_mv = int(g.get('use_master_valve') or 0)
        except (ValueError, TypeError):
            use_mv = 0
        if use_mv != 1:
            stats['masters_skipped_no_use_master'] += 1
            continue
        mtopic = (g.get('master_mqtt_topic') or '').strip()
        msid = g.get('master_mqtt_server_id')
        if not mtopic or not msid:
            stats['masters_skipped_no_topic'] += 1
            logger.warning("emergency_stop_all: group=%s skipped — no master topic/server", gid)
            continue
        try:
            t_norm = normalize_topic(mtopic)
        except (ValueError, TypeError, OSError):
            t_norm = mtopic
        try:
            mode = (g.get('master_mode') or 'NC').strip().upper()
        except (ValueError, TypeError, AttributeError):
            mode = 'NC'
        # Cancel any pending timer for this topic so it can't override us later
        with _PENDING_CLOSE_LOCK:
            prev = _PENDING_CLOSE_TIMERS.pop(t_norm, None)
        if prev is not None:
            try:
                prev.cancel()
            except (RuntimeError, OSError):
                pass
        # Skip duplicate topics shared across groups (publish once per topic).
        # If a previously-seen group used a different master_mode, that's a
        # configuration smell — log a warning so ops can fix it.
        if t_norm in seen_topics:
            prev_mode = seen_topics[t_norm]
            if prev_mode != mode:
                logger.warning("emergency_stop_all: group=%s shares topic=%s with earlier "
                               "group but master_mode differs (this=%s vs first=%s) — "
                               "first wins, check group config", gid, t_norm, mode, prev_mode)
            stats['masters_skipped_dup_topic'] += 1
            continue
        seen_topics[t_norm] = mode
        try:
            mserver = db.get_mqtt_server(int(msid))
        except (sqlite3.Error, OSError, ValueError, TypeError):
            logger.exception('emergency_stop_all: get_mqtt_server(%s) failed', msid)
            stats['masters_failed_publish'] += 1
            continue
        if not mserver:
            logger.warning("emergency_stop_all: group=%s msid=%s — server not found", gid, msid)
            stats['masters_failed_publish'] += 1
            continue
        close_val = '1' if mode == 'NO' else '0'
        try:
            publish_mqtt_value(mserver, t_norm, close_val,
                               min_interval_sec=0.0, qos=2, retain=True,
                               meta={'cmd': 'master_off', 'src': 'emergency'})
            stats['masters_closed'] += 1
            logger.info("emergency_stop_all: master closed — group=%s topic=%s val=%s mode=%s",
                        gid, t_norm, close_val, mode)
            try:
                db.update_group_fields(int(gid), {'master_valve_observed': 'closed'})
                from services import sse_hub as _sse_hub_e
                import json as _json_e
                _sse_hub_e.broadcast(_json_e.dumps({'mv_group_id': int(gid), 'mv_state': 'closed'}))
            except (sqlite3.Error, OSError, ImportError, ValueError, TypeError) as e:
                logger.debug("emergency_stop_all: master_valve_observed update failed (gid=%s): %s", gid, e)
            if not TESTING:
                time.sleep(0.05)
        except Exception:
            stats['masters_failed_publish'] += 1
            logger.exception('emergency_stop_all: publish failed group=%s topic=%s', gid, t_norm)

    logger.info("emergency_stop_all: done — %s", stats)
    return stats
