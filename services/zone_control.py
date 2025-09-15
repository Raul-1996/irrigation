import logging
from datetime import datetime
from typing import Optional
import time

from database import db
from services.locks import group_lock, zone_lock
from services.mqtt_pub import publish_mqtt_value
from utils import normalize_topic

logger = logging.getLogger(__name__)


def _versioned_update(zone_id: int, updates: dict) -> None:
    ok = False
    try:
        ok = db.update_zone_versioned(zone_id, updates)
    except Exception:
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
                    # transition to starting
                    _versioned_update(zone_id, {'state': 'starting', 'commanded_state': 'on', 'watering_start_time': start_ts})
            try:
                sid = z.get('mqtt_server_id'); topic = (z.get('topic') or '').strip()
                if sid and topic:
                    server = db.get_mqtt_server(int(sid))
                    if server:
                        # Per-group master valve pre-open (idempotent; also covers peer groups on same topic)
                        try:
                            gid = int(z.get('group_id') or 0)
                            if gid and gid != 999:
                                g = next((gg for gg in (db.get_groups() or []) if int(gg.get('id')) == gid), None)
                                if g and int(g.get('use_master_valve') or 0) == 1:
                                    mtopic = (g.get('master_mqtt_topic') or '').strip()
                                    msid = g.get('master_mqtt_server_id')
                                    if mtopic and msid:
                                        mserver = db.get_mqtt_server(int(msid))
                                        if mserver:
                                            publish_mqtt_value(mserver, normalize_topic(mtopic), '1', min_interval_sec=0.0)
                        except Exception:
                            logger.exception('master valve pre-open failed')
                        publish_mqtt_value(server, normalize_topic(topic), '1', min_interval_sec=0.0, meta={'cmd': str(command_id) if 'command_id' in locals() and command_id else None, 'ver': str((z.get('version') or 0) + 1)})
                        # transition to on
                        _versioned_update(zone_id, {'state': 'on'})
            except Exception:
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
                                publish_mqtt_value(server_o, normalize_topic(otopic), '0', min_interval_sec=0.0, meta={'cmd': 'peer_off', 'ver': str((other.get('version') or 0) + 1)})
                                last_time = other.get('watering_start_time')
                                _versioned_update(oid, {'state': 'off', 'watering_start_time': None, 'last_watering_time': last_time})
                    except Exception:
                        logger.exception("exclusive_start_zone: mqtt off peer failed")

                with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(group_zones)-1))) as pool:
                    pool.map(_stop_peer, group_zones)
            except Exception:
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
                                publish_mqtt_value(server_o, normalize_topic(otopic), '0', min_interval_sec=0.0, meta={'cmd': 'peer_off', 'ver': str((other.get('version') or 0) + 1)})
                                last_time = other.get('watering_start_time')
                                _versioned_update(oid, {'state': 'off', 'watering_start_time': None, 'last_watering_time': last_time})
                    except Exception:
                        logger.exception("exclusive_start_zone: mqtt off peer failed (sequential)")
        return True
    except Exception:
        logger.exception("exclusive_start_zone failed")
        return False


def stop_zone(zone_id: int, reason: str = 'manual', force: bool = False) -> bool:
    """Единый стоп зоны. Идемпотентно. Публикует OFF и фиксирует в БД.
    reason: для журналирования; force — останавливать даже если state уже off.
    """
    try:
        z = db.get_zone(zone_id)
        if not z:
            return False
        if (str(z.get('state')).lower() in ('off', 'stopping')) and not force:
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
                    publish_mqtt_value(server, normalize_topic(topic), '0', min_interval_sec=0.0, retain=True, meta={'cmd':'stop','ver':str((z.get('version') or 0) + 1)})
                    # Delayed master valve close (60s) if no zones ON on the same master topic across peer groups
                    try:
                        gid = int(z.get('group_id') or 0)
                        if gid and gid != 999:
                            g = next((gg for gg in (db.get_groups() or []) if int(gg.get('id')) == gid), None)
                            if g and int(g.get('use_master_valve') or 0) == 1:
                                mtopic = (g.get('master_mqtt_topic') or '').strip()
                                msid = g.get('master_mqtt_server_id')
                                if mtopic and msid:
                                    def _delayed_close():
                                        try:
                                            time.sleep(60)
                                            # Check any ON zone in any group sharing this master topic
                                            any_on = False
                                            for gg in (db.get_groups() or []):
                                                if (gg.get('master_mqtt_topic') or '').strip() != mtopic:
                                                    continue
                                                for z2 in (db.get_zones_by_group(int(gg.get('id'))) or []):
                                                    if str(z2.get('state')).lower() == 'on':
                                                        any_on = True
                                                        break
                                                if any_on:
                                                    break
                                            if not any_on:
                                                mserver = db.get_mqtt_server(int(msid))
                                                if mserver:
                                                    publish_mqtt_value(mserver, normalize_topic(mtopic), '0', min_interval_sec=0.0, retain=True, meta={'cmd':'master_off'})
                                        except Exception:
                                            logger.exception('master valve delayed close failed')
                                    import threading as _th
                                    _th.Thread(target=_delayed_close, daemon=True).start()
                    except Exception:
                        logger.exception('master valve close scheduling failed')
        except Exception:
            logger.exception('stop_zone: mqtt off failed')
        # Завершаем переход: stopping -> off
        with zone_lock(zone_id):
            _versioned_update(zone_id, {'state': 'off', 'watering_start_time': None, 'last_watering_time': last_time, 'planned_end_time': None})
        try:
            db.add_log('zone_stop', f'{reason}: zone={int(zone_id)}')
        except Exception:
            pass
        return True
    except Exception:
        logger.exception('stop_zone failed')
        return False


def stop_all_in_group(group_id: int, reason: str = 'group_cancel', force: bool = False) -> None:
    """Немедленно остановить все зоны в группе (идемпотентно)."""
    try:
        zones = db.get_zones_by_group(int(group_id))
        for z in zones:
            try:
                stop_zone(int(z['id']), reason=reason, force=force)
                # Небольшая пауза, чтобы избежать всплесков при публикации на слабом железе (пропускаем в тестах)
                try:
                    from app import app as app_module
                    if not app_module.config.get('TESTING'):
                        time.sleep(0.05)
                except Exception:
                    pass
            except Exception:
                logger.exception('stop_all_in_group: stop_zone failed')
    except Exception:
        logger.exception('stop_all_in_group failed')
