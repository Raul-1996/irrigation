import logging
import threading
import time
from typing import Optional, Dict, Tuple

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None

from utils import normalize_topic
try:
    from database import db as _db
except Exception:
    _db = None

logger = logging.getLogger(__name__)

# Caches and locks
_MQTT_CLIENTS: Dict[int, object] = {}
_MQTT_CLIENTS_LOCK = threading.Lock()
_TOPIC_LAST_SEND: Dict[Tuple[int, str], Tuple[str, float]] = {}
_TOPIC_LOCK = threading.Lock()
_SERVER_CACHE: Dict[int, Tuple[dict, float]] = {}
_SERVER_CACHE_TTL = 300.0  # seconds


def get_or_create_mqtt_client(server: dict):
    if mqtt is None:
        return None
    try:
        sid = int(server.get('id')) if server.get('id') is not None else 0
    except Exception:
        sid = 0
    with _MQTT_CLIENTS_LOCK:
        cl = _MQTT_CLIENTS.get(sid)
        if cl is None:
            try:
                cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                if server.get('username'):
                    cl.username_pw_set(server.get('username'), server.get('password') or None)
                # TLS options (если включены)
                try:
                    if int(server.get('tls_enabled') or 0) == 1:
                        import ssl
                        ca = server.get('tls_ca_path') or None
                        cert = server.get('tls_cert_path') or None
                        key = server.get('tls_key_path') or None
                        tls_ver = (server.get('tls_version') or '').upper().strip()
                        version = ssl.PROTOCOL_TLS_CLIENT if tls_ver in ('', 'TLS', 'TLS_CLIENT') else ssl.PROTOCOL_TLS
                        cl.tls_set(ca_certs=ca, certfile=cert, keyfile=key, tls_version=version)
                        if int(server.get('tls_insecure') or 0) == 1:
                            cl.tls_insecure_set(True)
                except Exception:
                    logger.exception('MQTT TLS setup failed for publisher')
                host = server.get('host') or '127.0.0.1'
                port = int(server.get('port') or 1883)
                try:
                    # быстрый авто-ре-коннект и асинхронное подключение
                    try:
                        cl.reconnect_delay_set(min_delay=1, max_delay=5)
                    except Exception:
                        pass
                    try:
                        cl.max_inflight_messages_set(100)
                    except Exception:
                        pass
                    # запускаем цикл и подключаемся асинхронно, чтобы не блокировать HTTP
                    try:
                        cl.loop_start()
                    except Exception:
                        pass
                    cl.connect_async(host, port, 30)
                except Exception:
                    # не кэшируем неудачное подключение
                    return None
                def _on_disconnect(c, u, rc, properties=None):
                    # оставляем клиента в кеше: loop_start и reconnect_delay_set обеспечат авто-переподключение
                    try:
                        logger.info("MQTT client disconnected sid=%s rc=%s (auto-reconnect active)", sid, rc)
                    except Exception:
                        pass
                cl.on_disconnect = _on_disconnect
                _MQTT_CLIENTS[sid] = cl
            except Exception:
                return None
        return cl


def publish_mqtt_value(server: dict, topic: str, value: str, min_interval_sec: float = 0.2, retain: bool = False,
                       meta: Optional[Dict[str, str]] = None, qos: int = 0) -> bool:
    try:
        t = normalize_topic(topic)
        sid = int(server.get('id')) if server.get('id') else None
        # normalize server via TTL cache
        if sid is not None and _db is not None:
            try:
                now_ts = time.time()
                cached = _SERVER_CACHE.get(sid)
                srv = None
                if cached and (now_ts - cached[1]) < _SERVER_CACHE_TTL:
                    srv = cached[0]
                else:
                    srv = _db.get_mqtt_server(sid)
                    if srv:
                        _SERVER_CACHE[sid] = (srv, now_ts)
                if srv:
                    server = srv
            except Exception:
                pass
        key = (sid or 0, t)
        now = time.time()
        with _TOPIC_LOCK:
            last = _TOPIC_LAST_SEND.get(key)
            if last and last[0] == value and (now - last[1]) < min_interval_sec:
                logger.debug(f"MQTT skip duplicate topic={t} value={value}")
                return True
            _TOPIC_LAST_SEND[key] = (value, now)
        logger.debug(f"MQTT publish topic={t} value={value}")
        cl = get_or_create_mqtt_client(server)
        if cl is None:
            logger.warning("MQTT publish: client unavailable, dropping message")
            return False
        # Publish to base topic
        try:
            res = cl.publish(t, payload=value, qos=max(0, min(2, int(qos or 0))), retain=retain)
            try:
                rc = getattr(res, 'rc', 0)
            except Exception:
                rc = 0
            if rc != 0:
                logger.warning(f"MQTT publish initial rc={rc}, try reconnect")
                try:
                    cl.reconnect()
                except Exception:
                    pass
                res = cl.publish(t, payload=value, qos=max(0, min(2, int(qos or 0))), retain=retain)
        except Exception:
            logger.exception('MQTT publish failed')
            return False

        # Also publish to the control topic '/on' for Wirenboard compatibility
        try:
            t_on = t + '/on'
            on_key = (sid or 0, t_on)
            now2 = time.time()
            with _TOPIC_LOCK:
                last2 = _TOPIC_LAST_SEND.get(on_key)
                if last2 and last2[0] == value and (now2 - last2[1]) < min_interval_sec:
                    return True
                _TOPIC_LAST_SEND[on_key] = (value, now2)
            logger.debug(f"MQTT publish topic={t_on} value={value}")
            res_on = cl.publish(t_on, payload=value, qos=max(0, min(2, int(qos or 0))), retain=retain)
            # Ignore rc here; some brokers may not acknowledge the duplicate fast
        except Exception:
            # Soft-fail for '/on' duplication
            pass

        # Optional: publish meta information to a side topic for diagnostics/idempotence
        try:
            if meta:
                t_meta = t + '/meta'
                payload_meta = ';'.join([f"{k}={v}" for k, v in meta.items() if v is not None])
                if payload_meta:
                    cl.publish(t_meta, payload=payload_meta, qos=0, retain=False)
        except Exception:
            # meta is best-effort
            pass

        return True
    except Exception:
        logger.exception('publish_mqtt_value failed')
        return False
