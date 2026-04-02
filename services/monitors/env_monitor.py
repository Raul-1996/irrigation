import logging
import threading
import time as _time
from typing import Optional

from database import db

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

logger = logging.getLogger(__name__)


class EnvMonitor:
    def __init__(self):
        self.temp_client = None
        self.hum_client = None
        self.temp_value: Optional[float] = None
        self.hum_value: Optional[float] = None
        self.cfg = None
        self.last_temp_rx_ts: float = 0.0
        self.last_hum_rx_ts: float = 0.0
        self._lock = threading.Lock()

    def stop(self):
        for cl in (self.temp_client, self.hum_client):
            try:
                if cl is not None:
                    cl.loop_stop()
                    cl.disconnect()
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Handled exception in stop: %s", e)
        self.temp_client = None
        self.hum_client = None
        self.last_temp_rx_ts = 0.0
        self.last_hum_rx_ts = 0.0

    def start(self, cfg: dict):
        self.stop()
        self.cfg = cfg or {}
        try:
            if mqtt is None:
                logger.warning('EnvMonitor start skipped: paho-mqtt not available')
                return
            logger.info(f"EnvMonitor starting with cfg={cfg}")
            tcfg = cfg.get('temp') or {}
            hcfg = cfg.get('hum') or {}
            if tcfg.get('enabled') and tcfg.get('topic') and tcfg.get('server_id'):
                self._start_sensor('temp', int(tcfg['server_id']), str(tcfg['topic']))
            if hcfg.get('enabled') and hcfg.get('topic') and hcfg.get('server_id'):
                self._start_sensor('hum', int(hcfg['server_id']), str(hcfg['topic']))
        except (ConnectionError, TimeoutError, OSError):
            logger.exception('EnvMonitor start failed')

    # Keep legacy method names as thin wrappers for backward compat / tests
    def _start_temp(self, server_id: int, topic: str):
        self._start_sensor('temp', server_id, topic)

    def _start_hum(self, server_id: int, topic: str):
        self._start_sensor('hum', server_id, topic)

    def _start_sensor(self, sensor_type: str, server_id: int, topic: str):
        """Unified MQTT subscription for temp or hum sensor.

        Args:
            sensor_type: 'temp' or 'hum'
            server_id: MQTT server id from DB
            topic: MQTT topic to subscribe
        """
        try:
            server = db.get_mqtt_server(server_id)
            if not server:
                return
            cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            if server.get('username'):
                cl.username_pw_set(server.get('username'), server.get('password') or None)

            def _on_message(c, u, msg, _st=sensor_type):
                try:
                    p = msg.payload.decode('utf-8', errors='ignore').strip().replace(',', '.')
                    try:
                        val = round(float(p))
                        with self._lock:
                            if _st == 'temp':
                                self.temp_value = val
                            else:
                                self.hum_value = val
                        if _st == 'temp':
                            self.last_temp_rx_ts = _time.time()
                        else:
                            self.last_hum_rx_ts = _time.time()
                        logger.info(f"EnvMonitor {_st} RX topic={getattr(msg, 'topic', topic)} value={val}")
                    except (ValueError, TypeError, KeyError):
                        logger.exception(f'EnvMonitor {_st} parse failed')
                except (ValueError, TypeError, KeyError):
                    logger.exception(f'EnvMonitor {_st} RX failed')

            def _on_connect(c, u, flags, reason_code, properties=None, _st=sensor_type):
                try:
                    c.subscribe(topic, qos=0)
                    logger.info(f"EnvMonitor {_st} subscribed {topic}")
                except (ConnectionError, TimeoutError, OSError):
                    logger.exception(f'EnvMonitor {_st} subscribe failed')

            def _on_disconnect(c, u, rc, properties=None, _st=sensor_type):
                try:
                    if _st == 'temp':
                        self.temp_client = None
                    else:
                        self.hum_client = None
                    logger.info(f"EnvMonitor {_st} disconnected: rc={rc}")
                except (ConnectionError, TimeoutError, OSError) as e:
                    logger.debug("Handled exception in _on_disconnect_%s: %s", _st, e)

            cl.on_message = _on_message
            cl.on_connect = _on_connect
            cl.on_disconnect = _on_disconnect
            cl.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 5)
            cl.loop_start()
            try:
                cl.subscribe(topic, qos=0)
            except (ConnectionError, TimeoutError, OSError):
                logger.exception(f'EnvMonitor {sensor_type} immediate subscribe failed')
            if sensor_type == 'temp':
                self.temp_client = cl
            else:
                self.hum_client = cl
        except (ConnectionError, TimeoutError, OSError):
            logger.exception(f'EnvMonitor {sensor_type} start failed')


env_monitor = EnvMonitor()


def start_env_monitor(cfg: dict):
    try:
        env_monitor.start(cfg or {})
    except (ConnectionError, TimeoutError, OSError, ValueError):
        logger.exception('start_env_monitor failed')


def probe_env_values(cfg: dict) -> None:
    """One-shot subscribe to fetch retained env values (temp/hum) from MQTT brokers."""
    try:
        if mqtt is None:
            return
        logger.info('EnvProbe: starting')
        topics = []
        tcfg = (cfg.get('temp') or {})
        hcfg = (cfg.get('hum') or {})
        if tcfg.get('enabled') and tcfg.get('topic') and tcfg.get('server_id'):
            topics.append((int(tcfg['server_id']), (tcfg['topic'] or '').strip(), 'temp'))
        if hcfg.get('enabled') and hcfg.get('topic') and hcfg.get('server_id'):
            topics.append((int(hcfg['server_id']), (hcfg['topic'] or '').strip(), 'hum'))
        for sid, topic, kind in topics:
            server = db.get_mqtt_server(int(sid))
            if not server or not topic:
                continue
            try:
                logger.info(f"EnvProbe: connect sid={sid} host={server.get('host')} port={server.get('port')} topic={topic} kind={kind}")
                from services.mqtt_pub import get_or_create_mqtt_client
                cl = get_or_create_mqtt_client(server)
                if cl is None:
                    logger.warning(f"EnvProbe: could not get client for sid={sid}")
                    continue
                # Subscribe to fetch retained value (the EnvMonitor's on_message will pick it up)
                try:
                    cl.subscribe(topic, qos=0)
                except (ConnectionError, TimeoutError, OSError):
                    logger.exception(f'EnvProbe: subscribe failed for {topic}')
            except ImportError:
                logger.exception(f'EnvProbe: failed for sid={sid} topic={topic}')
    except ImportError:
        logger.exception('EnvProbe: outer failed')
