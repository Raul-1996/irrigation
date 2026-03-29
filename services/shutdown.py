"""Graceful shutdown — send OFF to ALL zones and close master valves.

Called from signal handlers (SIGTERM/SIGINT) and atexit fallback.
Must never raise, never hang, never break existing functionality.
"""
import logging
import sqlite3
import threading

logger = logging.getLogger(__name__)

_shutdown_done = False
_shutdown_lock = threading.Lock()


def shutdown_all_zones_off(timeout_sec: float = 10, db=None) -> None:
    """Send OFF (QoS 2, retain) to every zone and close master valves.

    - Idempotent: safe to call from both signal handler and atexit.
    - Never raises — all errors are logged as warnings.
    - Updates zone state to 'off' in the database.

    Args:
        timeout_sec: max seconds to wait for each publish acknowledgement.
        db: optional database handle; if None, imports the global singleton.
    """
    global _shutdown_done
    with _shutdown_lock:
        if _shutdown_done:
            return
        _shutdown_done = True

    # ── imports (late, to avoid circular) ───────────────────────────
    try:
        if db is None:
            from database import db as _default_db
            db = _default_db
    except ImportError:
        logger.warning('Shutdown: cannot import database')
        return

    try:
        from utils import normalize_topic
        from services.mqtt_pub import get_or_create_mqtt_client
    except ImportError:
        logger.warning('Shutdown: cannot import mqtt_pub / utils')
        return

    # ── fetch zones ─────────────────────────────────────────────────
    zones = []
    try:
        zones = db.get_zones() or []
    except (sqlite3.Error, OSError, Exception) as exc:
        logger.warning('Shutdown: cannot read zones: %s', exc)
        return

    # ── build publish list ──────────────────────────────────────────
    server_cache: dict = {}
    zone_tasks = []  # (server, topic, zone_id)

    for z in zones:
        try:
            sid = z.get('mqtt_server_id')
            topic_raw = (z.get('topic') or '').strip()
            if not sid or not topic_raw:
                continue
            sid = int(sid)
            if sid not in server_cache:
                try:
                    server_cache[sid] = db.get_mqtt_server(sid)
                except (sqlite3.Error, OSError, Exception):
                    server_cache[sid] = None
            server = server_cache.get(sid)
            if not server:
                continue
            zone_tasks.append((server, normalize_topic(topic_raw), int(z.get('id', 0))))
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning('Shutdown: bad zone data: %s', exc)

    # ── publish OFF to every zone ───────────────────────────────────
    inflight = []
    zone_count = 0
    for server, topic, zone_id in zone_tasks:
        try:
            cl = get_or_create_mqtt_client(server)
            if cl is None:
                logger.warning('Shutdown: MQTT client unavailable, skip %s', topic)
                continue
            res = cl.publish(topic, payload='0', qos=2, retain=True)
            inflight.append((topic, res))
            # Wirenboard /on compat
            try:
                res_on = cl.publish(topic + '/on', payload='0', qos=2, retain=True)
                inflight.append((topic + '/on', res_on))
            except (ConnectionError, TimeoutError, OSError) as exc:
                logger.debug('Shutdown: /on publish error %s: %s', topic, exc)
            zone_count += 1
        except (ConnectionError, TimeoutError, OSError, Exception) as exc:
            logger.warning('Shutdown: publish error %s: %s', topic, exc)

    # ── master valves ───────────────────────────────────────────────
    master_count = 0
    try:
        groups = db.get_groups() or []
    except (sqlite3.Error, OSError, Exception):
        groups = []

    seen_master_topics: set = set()
    for g in groups:
        try:
            if int(g.get('use_master_valve') or 0) != 1:
                continue
            mtopic_raw = (g.get('master_mqtt_topic') or '').strip()
            msid = g.get('master_mqtt_server_id')
            if not mtopic_raw or not msid:
                continue
            msid = int(msid)
            mtopic = normalize_topic(mtopic_raw)
            # deduplicate — same physical valve may be shared across groups
            if mtopic in seen_master_topics:
                continue
            seen_master_topics.add(mtopic)

            if msid not in server_cache:
                try:
                    server_cache[msid] = db.get_mqtt_server(msid)
                except (sqlite3.Error, OSError, Exception):
                    server_cache[msid] = None
            mserver = server_cache.get(msid)
            if not mserver:
                continue

            try:
                mode = (g.get('master_mode') or 'NC').strip().upper()
            except (ValueError, TypeError, KeyError):
                mode = 'NC'
            # Close: NC → '0' (de-energise = closed), NO → '1' (energise = closed)
            close_val = '0' if mode == 'NC' else '1'

            cl = get_or_create_mqtt_client(mserver)
            if cl is None:
                continue
            res = cl.publish(mtopic, payload=close_val, qos=2, retain=True)
            inflight.append((mtopic, res))
            try:
                res_on = cl.publish(mtopic + '/on', payload=close_val, qos=2, retain=True)
                inflight.append((mtopic + '/on', res_on))
            except (ConnectionError, TimeoutError, OSError):
                pass
            master_count += 1
        except (ValueError, TypeError, KeyError, ConnectionError, TimeoutError, OSError, Exception) as exc:
            logger.warning('Shutdown: master valve error: %s', exc)

    # ── wait for publish acknowledgements ───────────────────────────
    success = 0
    failed = 0
    for topic, res in inflight:
        try:
            res.wait_for_publish(timeout=timeout_sec)
            success += 1
        except (ValueError, RuntimeError, OSError, Exception):
            failed += 1

    # ── update DB state ─────────────────────────────────────────────
    for z in zones:
        try:
            zid = int(z.get('id', 0))
            if zid:
                db.update_zone(zid, {'state': 'off'})
        except (sqlite3.Error, OSError, Exception) as exc:
            logger.debug('Shutdown: DB update error zone %s: %s', z.get('id'), exc)

    logger.info(
        'Shutdown: sent OFF to %d zones, %d master valves (%d confirmed, %d failed)',
        zone_count, master_count, success, failed,
    )


def reset_shutdown() -> None:
    """Reset the idempotency flag — for tests only."""
    global _shutdown_done
    with _shutdown_lock:
        _shutdown_done = False
