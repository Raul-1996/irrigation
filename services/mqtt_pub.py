import logging
import threading
import time
from typing import Optional, Dict, Tuple

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None

from utils import normalize_topic

logger = logging.getLogger(__name__)

# Caches and locks
_MQTT_CLIENTS: Dict[int, object] = {}
_MQTT_CLIENTS_LOCK = threading.Lock()
_TOPIC_LAST_SEND: Dict[Tuple[int, str], Tuple[str, float]] = {}
_TOPIC_LOCK = threading.Lock()


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
                    cl.connect(host, port, 30)
                except Exception:
                    # не кэшируем неудачное подключение
                    return None
                try:
                    cl.reconnect_delay_set(min_delay=1, max_delay=4)
                except Exception:
                    pass
                def _on_disconnect(c, u, rc, properties=None):
                    try:
                        with _MQTT_CLIENTS_LOCK:
                            if _MQTT_CLIENTS.get(sid) is c:
                                _MQTT_CLIENTS.pop(sid, None)
                    except Exception:
                        pass
                cl.on_disconnect = _on_disconnect
                try:
                    cl.loop_start()
                except Exception:
                    pass
                _MQTT_CLIENTS[sid] = cl
            except Exception:
                return None
        return cl


def publish_mqtt_value(server: dict, topic: str, value: str, min_interval_sec: float = 0.5, retain: bool = False) -> bool:
    try:
        t = normalize_topic(topic)
        sid = int(server.get('id')) if server.get('id') else None
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
        try:
            res = cl.publish(t, payload=value, qos=0, retain=retain)
            try:
                rc = getattr(res, 'rc', 0)
            except Exception:
                rc = 0
            if rc != 0:
                logger.warning(f"MQTT publish initial rc={rc}, try reconnect")
                raise RuntimeError('publish_failed')
        except Exception:
            try:
                cl2 = get_or_create_mqtt_client(server)
                if cl2 is None:
                    return False
                res2 = cl2.publish(t, payload=value, qos=0, retain=retain)
                rc2 = getattr(res2, 'rc', 0)
                return rc2 == 0
            except Exception:
                logger.exception('MQTT publish failed on retry')
                return False
        return True
    except Exception:
        logger.exception('publish_mqtt_value failed')
        return False
