"""
Centralised SSE (Server-Sent Events) Hub for real-time MQTT→browser streaming.

Extracted from app.py (TASK-015).  The module does NOT import ``app`` or
``db`` directly — every dependency is injected via :func:`init` or passed
as function arguments so that circular imports are impossible.
"""
import sqlite3

import json
import logging
import queue
import threading
import time
from collections import deque
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global hub state
# ---------------------------------------------------------------------------
_SSE_HUB_STARTED: bool = False
_SSE_HUB_LOCK: threading.Lock = threading.Lock()
_SSE_HUB_CLIENTS: list = []          # list[queue.Queue]
_SSE_HUB_MQTT: dict = {}             # sid → paho client
_SSE_META_BUFFER: deque = deque(maxlen=100)

# Anti-restart: remember manual stops so we can ignore instant ON bounces
_LAST_MANUAL_STOP: dict[int, float] = {}
_LAST_STOP_LOCK: threading.Lock = threading.Lock()

# Injected dependencies (set via init())
_db = None          # database instance
_mqtt = None        # paho.mqtt.client module
_app_config = None  # app.config dict-like
_publish_mqtt_value_fn = None
_normalize_topic_fn = None
_get_scheduler_fn = None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def init(*, db, mqtt_module, app_config, publish_mqtt_value, normalize_topic, get_scheduler):
    """Inject runtime dependencies.  Call once at app startup (before any
    request that might trigger :func:`ensure_hub_started`)."""
    global _db, _mqtt, _app_config, _publish_mqtt_value_fn, _normalize_topic_fn, _get_scheduler_fn
    _db = db
    _mqtt = mqtt_module
    _app_config = app_config
    _publish_mqtt_value_fn = publish_mqtt_value
    _normalize_topic_fn = normalize_topic
    _get_scheduler_fn = get_scheduler


def get_meta_buffer() -> list:
    """Return a copy of recent meta-messages (for health panel)."""
    try:
        return list(_SSE_META_BUFFER)
    except (OSError, RuntimeError, ValueError) as e:
        logger.debug("Exception in get_meta_buffer: %s", e)
        return []


def broadcast(data_json: str) -> None:
    """Push a JSON string to every connected SSE client."""
    try:
        with _SSE_HUB_LOCK:
            for q in list(_SSE_HUB_CLIENTS):
                try:
                    q.put_nowait(data_json)
                except queue.Full as e:
                    logger.debug("Handled exception in broadcast: %s", e)
    except (RuntimeError, OSError) as e:
        logger.warning("Broadcast failed: %s", e)


def mark_zone_stopped(zone_id: int) -> None:
    """Record a manual stop timestamp for anti-restart window."""
    try:
        with _LAST_STOP_LOCK:
            _LAST_MANUAL_STOP[int(zone_id)] = time.time()
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Handled exception in mark_zone_stopped: %s", e)


def recently_stopped(zone_id: int, window_sec: int = 5) -> bool:
    """True if *zone_id* was manually stopped within *window_sec* seconds."""
    try:
        with _LAST_STOP_LOCK:
            ts = _LAST_MANUAL_STOP.get(int(zone_id))
        return (ts is not None) and ((time.time() - ts) < max(0, int(window_sec)))
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Exception in recently_stopped: %s", e)
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _rebuild_subscriptions():
    """Build zone-topic and master-valve-topic maps from the database."""
    zones = _db.get_zones()
    groups = _db.get_groups() or []
    zone_topics: dict = {}
    mv_topics: dict = {}
    for z in zones:
        sid = z.get('mqtt_server_id')
        topic = (z.get('topic') or '').strip()
        if not sid or not topic:
            continue
        t = topic if str(topic).startswith('/') else '/' + str(topic)
        zone_topics.setdefault(int(sid), {}).setdefault(t, []).append(int(z['id']))
    for g in groups:
        try:
            if int(g.get('use_master_valve') or 0) != 1:
                continue
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in _rebuild_subscriptions: %s", e)
            continue
        mtopic = (g.get('master_mqtt_topic') or '').strip()
        msid = g.get('master_mqtt_server_id')
        if not mtopic or not msid:
            continue
        t = mtopic if str(mtopic).startswith('/') else '/' + str(mtopic)
        mv_topics.setdefault(int(msid), {}).setdefault(t, []).append(int(g.get('id')))
    return zone_topics, mv_topics


def ensure_hub_started() -> None:
    """Idempotently start MQTT subscriptions that fan-out to SSE clients."""
    global _SSE_HUB_STARTED, _SSE_HUB_CLIENTS, _SSE_HUB_MQTT, _SSE_META_BUFFER

    if _mqtt is None:
        return

    # Skip real MQTT connections in tests
    if _app_config and _app_config.get('TESTING'):
        with _SSE_HUB_LOCK:
            _SSE_HUB_STARTED = True
        return

    with _SSE_HUB_LOCK:
        if _SSE_HUB_STARTED:
            return
        zone_topics, mv_topics = _rebuild_subscriptions()
        for sid, topics in zone_topics.items():
            server = _db.get_mqtt_server(int(sid))
            if not server:
                continue
            try:
                client = _mqtt.Client(_mqtt.CallbackAPIVersion.VERSION2,
                                      client_id=(server.get('client_id') or None))
                if server.get('username'):
                    client.username_pw_set(server.get('username'), server.get('password') or None)

                def _on_message(cl, userdata, msg, sid_local=int(sid)):
                    t = str(getattr(msg, 'topic', '') or '')
                    if not t.startswith('/'):
                        t = '/' + t
                    try:
                        payload = msg.payload.decode('utf-8', errors='ignore').strip()
                    except (ValueError, TypeError, KeyError) as e:
                        logger.debug("Exception in _on_message: %s", e)
                        payload = str(msg.payload)

                    # Meta topic → buffer only
                    if t.endswith('/meta'):
                        try:
                            _SSE_META_BUFFER.append({
                                'topic': t,
                                'payload': payload,
                                'ts': datetime.now().strftime('%H:%M:%S'),
                            })
                        except (ValueError, TypeError, KeyError) as e:
                            logger.debug("Handled exception in _on_message: %s", e)
                        return

                    zone_ids = zone_topics.get(sid_local, {}).get(t) or []
                    mv_group_ids = mv_topics.get(sid_local, {}).get(t) or []

                    # Master-valve event
                    if mv_group_ids:
                        mv_state = 'open' if payload in ('1', 'true', 'ON', 'on') else 'closed'
                        for gid in mv_group_ids:
                            try:
                                _db.update_group_fields(int(gid), {'master_valve_observed': mv_state})
                            except (sqlite3.Error, OSError) as e:
                                logger.debug("Handled exception in line_184: %s", e)
                            data_mv = json.dumps({'mv_group_id': int(gid), 'mv_state': mv_state})
                            with _SSE_HUB_LOCK:
                                for q in list(_SSE_HUB_CLIENTS):
                                    try:
                                        q.put_nowait(data_mv)
                                    except queue.Full as e:
                                        logger.debug("Handled exception in line_191: %s", e)
                        return

                    new_state = 'on' if payload in ('1', 'true', 'ON', 'on') else 'off'

                    # Emergency stop override
                    if _app_config.get('EMERGENCY_STOP') and new_state == 'on':
                        new_state = 'off'
                        try:
                            srv = _db.get_mqtt_server(int(sid_local))
                            if srv:
                                _publish_mqtt_value_fn(srv, t, '0')
                        except (ConnectionError, TimeoutError, OSError) as e:
                            logger.debug("Handled exception in line_204: %s", e)

                    # Anti-restart window
                    try:
                        for zid in list(zone_ids):
                            if new_state == 'on' and recently_stopped(int(zid), window_sec=5):
                                new_state = 'off'
                                try:
                                    srv2 = _db.get_mqtt_server(int(sid_local))
                                    if srv2:
                                        _publish_mqtt_value_fn(srv2, t, '0')
                                except (ConnectionError, TimeoutError, OSError) as e:
                                    logger.debug("Handled exception in line_216: %s", e)
                    except (ConnectionError, TimeoutError, OSError) as e:
                        logger.debug("Handled exception in line_218: %s", e)

                    # DB + scheduler update
                    for zid in zone_ids:
                        try:
                            z = _db.get_zone(int(zid)) or {}
                            updates = {'state': new_state}
                            if new_state == 'on':
                                if not z.get('watering_start_time'):
                                    updates['watering_start_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                    updates['watering_start_source'] = 'remote'
                                try:
                                    sched = _get_scheduler_fn()
                                    if sched:
                                        dur = int(z.get('duration') or 0)
                                        if dur > 0:
                                            sched.cancel_zone_jobs(int(zid))
                                            sched.schedule_zone_stop(int(zid), dur,
                                                                     command_id=str(int(time.time())))
                                except (ValueError, TypeError, KeyError) as e:
                                    logger.debug("Handled exception in line_238: %s", e)
                            else:
                                if z.get('watering_start_time'):
                                    updates['last_watering_time'] = z.get('watering_start_time')
                                updates['watering_start_time'] = None
                                try:
                                    sched = _get_scheduler_fn()
                                    if sched:
                                        sched.cancel_zone_jobs(int(zid))
                                except (ValueError, TypeError, KeyError) as e:
                                    logger.debug("Handled exception in line_248: %s", e)
                            try:
                                updates2 = updates.copy()
                            except (TypeError, AttributeError) as e:
                                logger.debug("Exception in line_252: %s", e)
                                updates2 = dict(updates)
                            updates2['observed_state'] = new_state
                            _db.update_zone(int(zid), updates2)
                        except (sqlite3.Error, OSError) as e:
                            logger.debug("Handled exception in line_257: %s", e)

                        data = json.dumps({
                            'zone_id': int(zid),
                            'topic': t,
                            'payload': payload,
                            'state': new_state,
                        })
                        # Fan-out to all SSE subscribers
                        with _SSE_HUB_LOCK:
                            for q in list(_SSE_HUB_CLIENTS):
                                try:
                                    q.put_nowait(data)
                                except queue.Full as e:
                                    logger.debug("Handled exception in line_271: %s", e)

                client.on_message = _on_message
                client.connect(server.get('host') or '127.0.0.1',
                               int(server.get('port') or 1883), 5)
                # Subscribe to zone topics
                for t in topics.keys():
                    try:
                        client.subscribe(t, qos=1)
                    except (ConnectionError, TimeoutError, OSError) as e:
                        logger.debug("Handled exception in line_281: %s", e)
                # Subscribe to master-valve topics for this server
                for t_mv in mv_topics.get(int(sid), {}).keys():
                    try:
                        client.subscribe(t_mv, qos=1)
                    except (ConnectionError, TimeoutError, OSError) as e:
                        logger.debug("Handled exception in line_287: %s", e)
                client.loop_start()
                _SSE_HUB_MQTT[int(sid)] = client
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.warning("SSE hub MQTT client setup failed for server %s: %s", sid, e)
                continue
        _SSE_HUB_STARTED = True


def register_client() -> 'queue.Queue':
    """Create and register a new SSE client queue.  Returns the queue."""
    msg_queue = queue.Queue(maxsize=10000)
    with _SSE_HUB_LOCK:
        _SSE_HUB_CLIENTS.append(msg_queue)
    return msg_queue


def unregister_client(msg_queue: 'queue.Queue') -> None:
    """Remove a client queue from the hub."""
    with _SSE_HUB_LOCK:
        try:
            _SSE_HUB_CLIENTS.remove(msg_queue)
        except ValueError as e:
            logger.debug("Client not in list during unregister: %s", e)
