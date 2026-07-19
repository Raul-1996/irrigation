import logging
import ssl
import threading
import time as _time

from database import db

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

logger = logging.getLogger(__name__)


def _configure_tls(client, server: dict) -> None:
    """Apply all persisted MQTT TLS options or raise before connecting."""
    if int(server.get("tls_enabled") or 0) != 1:
        return

    configured_version = (server.get("tls_version") or "TLS_CLIENT").upper().replace("_", ".").strip()
    tls_versions = {
        "TLS": ssl.PROTOCOL_TLS_CLIENT,
        "TLS.CLIENT": ssl.PROTOCOL_TLS_CLIENT,
        "TLSV1": ssl.PROTOCOL_TLSv1,
        "TLSV1.0": ssl.PROTOCOL_TLSv1,
        "TLSV1.1": ssl.PROTOCOL_TLSv1_1,
        "TLSV1.2": ssl.PROTOCOL_TLSv1_2,
    }
    try:
        tls_version = tls_versions[configured_version]
    except KeyError as exc:
        raise ValueError(f"Unsupported MQTT TLS version: {configured_version}") from exc

    client.tls_set(
        ca_certs=server.get("tls_ca_path") or None,
        certfile=server.get("tls_cert_path") or None,
        keyfile=server.get("tls_key_path") or None,
        tls_version=tls_version,
    )
    if int(server.get("tls_insecure") or 0) == 1:
        client.tls_insecure_set(True)


class EnvMonitor:
    def __init__(self):
        self.temp_client = None
        self.hum_client = None
        self.temp_value: float | None = None
        self.hum_value: float | None = None
        self.cfg = None
        self.last_temp_rx_ts: float = 0.0
        self.last_hum_rx_ts: float = 0.0
        self._lock = threading.Lock()
        self._reconfigure_lock = threading.RLock()

    @staticmethod
    def _retire_client(client) -> None:
        """Close both the MQTT socket and its network loop."""
        try:
            client.disconnect()
        except (ConnectionError, TimeoutError, OSError, RuntimeError, AttributeError) as exc:
            logger.debug("EnvMonitor disconnect failed during retirement: %s", exc)
        try:
            client.loop_stop()
        except (ConnectionError, TimeoutError, OSError, RuntimeError, AttributeError) as exc:
            logger.debug("EnvMonitor loop_stop failed during retirement: %s", exc)

    def disable(self) -> bool:
        """Idempotently disable all environment subscriptions."""
        with self._reconfigure_lock:
            with self._lock:
                clients = [client for client in (self.temp_client, self.hum_client) if client is not None]
                self.temp_client = None
                self.hum_client = None
                self.last_temp_rx_ts = 0.0
                self.last_hum_rx_ts = 0.0

            # Do not hold the state lock while loop_stop() joins the Paho
            # thread: a callback may already be waiting for that lock.
            retired: set[int] = set()
            for client in clients:
                if id(client) in retired:
                    continue
                retired.add(id(client))
                self._retire_client(client)
        return True

    def stop(self) -> bool:
        """Backward-compatible alias for :meth:`disable`."""
        return self.disable()

    def reconfigure(self, cfg: dict | None) -> bool:
        """Replace all subscriptions with ``cfg`` without leaking old clients."""
        with self._reconfigure_lock:
            self.disable()
            self.cfg = cfg or {}
            temp_cfg = self.cfg.get("temp") or {}
            hum_cfg = self.cfg.get("hum") or {}
            requested = [
                ("temp", temp_cfg),
                ("hum", hum_cfg),
            ]
            enabled = [item for item in requested if item[1].get("enabled")]

            if mqtt is None:
                if enabled:
                    logger.warning("EnvMonitor reconfigure skipped: paho-mqtt not available")
                    return False
                return True

            success = True
            for sensor_type, sensor_cfg in enabled:
                topic = str(sensor_cfg.get("topic") or "").strip()
                server_id = sensor_cfg.get("server_id")
                if not topic or not server_id:
                    logger.error("EnvMonitor %s configuration is incomplete", sensor_type)
                    success = False
                    continue
                if not self._start_sensor(sensor_type, int(server_id), topic):
                    success = False
            return success

    def start(self, cfg: dict) -> bool:
        """Backward-compatible entry point for a full reconfiguration."""
        try:
            logger.info("EnvMonitor starting with cfg=%s", cfg)
            return self.reconfigure(cfg)
        except (ConnectionError, TimeoutError, OSError, RuntimeError, ValueError):
            logger.exception("EnvMonitor start failed")
            self.disable()
            return False

    # Keep legacy method names as thin wrappers for backward compat / tests
    def _start_temp(self, server_id: int, topic: str) -> bool:
        return self._start_sensor("temp", server_id, topic)

    def _start_hum(self, server_id: int, topic: str) -> bool:
        return self._start_sensor("hum", server_id, topic)

    def _start_sensor(self, sensor_type: str, server_id: int, topic: str) -> bool:
        """Unified MQTT subscription for temp or hum sensor.

        Args:
            sensor_type: 'temp' or 'hum'
            server_id: MQTT server id from DB
            topic: MQTT topic to subscribe
        """
        client = None
        try:
            server = db.get_mqtt_server(server_id)
            if not server:
                logger.error("EnvMonitor %s MQTT server %s was not found", sensor_type, server_id)
                return False
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            if server.get("username"):
                client.username_pw_set(server.get("username"), server.get("password") or None)
            _configure_tls(client, server)

            attr_name = "temp_client" if sensor_type == "temp" else "hum_client"

            def _on_message(callback_client, userdata, msg):
                try:
                    with self._lock:
                        if getattr(self, attr_name) is not callback_client:
                            return
                    p = msg.payload.decode("utf-8", errors="ignore").strip().replace(",", ".")
                    try:
                        val = round(float(p))
                        with self._lock:
                            if getattr(self, attr_name) is not callback_client:
                                return
                            if sensor_type == "temp":
                                self.temp_value = val
                                self.last_temp_rx_ts = _time.time()
                            else:
                                self.hum_value = val
                                self.last_hum_rx_ts = _time.time()
                        logger.info(
                            "EnvMonitor %s RX topic=%s value=%s",
                            sensor_type,
                            getattr(msg, "topic", topic),
                            val,
                        )
                    except (ValueError, TypeError, KeyError):
                        logger.exception("EnvMonitor %s parse failed", sensor_type)
                except (ValueError, TypeError, KeyError):
                    logger.exception("EnvMonitor %s RX failed", sensor_type)

            def _on_connect(client, userdata, connect_flags, reason_code, properties):
                try:
                    with self._lock:
                        if getattr(self, attr_name) is not client:
                            return
                    client.subscribe(topic, qos=0)
                    logger.info("EnvMonitor %s subscribed %s", sensor_type, topic)
                except (ConnectionError, TimeoutError, OSError):
                    logger.exception("EnvMonitor %s subscribe failed", sensor_type)

            def _on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
                # Paho owns reconnects. Lifecycle methods are the only code
                # allowed to clear the handle, otherwise a transient disconnect
                # creates an untracked loop/socket that reappears later.
                logger.info("EnvMonitor %s disconnected: rc=%s", sensor_type, reason_code)

            client.on_message = _on_message
            client.on_connect = _on_connect
            client.on_disconnect = _on_disconnect
            client.connect(server.get("host") or "127.0.0.1", int(server.get("port") or 1883), 5)
            client.subscribe(topic, qos=0)

            with self._lock:
                previous_client = getattr(self, attr_name)
                setattr(self, attr_name, client)
            try:
                client.loop_start()
            except (ConnectionError, TimeoutError, OSError, RuntimeError):
                with self._lock:
                    if getattr(self, attr_name) is client:
                        setattr(self, attr_name, previous_client)
                raise

            if previous_client is not None and previous_client is not client:
                self._retire_client(previous_client)
            return True
        except (ConnectionError, TimeoutError, OSError, RuntimeError, ValueError, AttributeError):
            logger.exception("EnvMonitor %s start failed", sensor_type)
            if client is not None:
                with self._lock:
                    if self.temp_client is client:
                        self.temp_client = None
                    if self.hum_client is client:
                        self.hum_client = None
                self._retire_client(client)
            return False


env_monitor = EnvMonitor()


def start_env_monitor(cfg: dict):
    try:
        env_monitor.start(cfg or {})
    except (ConnectionError, TimeoutError, OSError, ValueError):
        logger.exception("start_env_monitor failed")


def probe_env_values(cfg: dict) -> None:
    """One-shot subscribe to fetch retained env values (temp/hum) from MQTT brokers."""
    try:
        if mqtt is None:
            return
        logger.info("EnvProbe: starting")
        topics = []
        tcfg = cfg.get("temp") or {}
        hcfg = cfg.get("hum") or {}
        if tcfg.get("enabled") and tcfg.get("topic") and tcfg.get("server_id"):
            topics.append((int(tcfg["server_id"]), (tcfg["topic"] or "").strip(), "temp"))
        if hcfg.get("enabled") and hcfg.get("topic") and hcfg.get("server_id"):
            topics.append((int(hcfg["server_id"]), (hcfg["topic"] or "").strip(), "hum"))
        for sid, topic, kind in topics:
            server = db.get_mqtt_server(int(sid))
            if not server or not topic:
                continue
            try:
                logger.info(
                    f"EnvProbe: connect sid={sid} host={server.get('host')} port={server.get('port')} topic={topic} kind={kind}"
                )
                from services.mqtt_pub import get_or_create_mqtt_client

                cl = get_or_create_mqtt_client(server)
                if cl is None:
                    logger.warning(f"EnvProbe: could not get client for sid={sid}")
                    continue
                # Subscribe to fetch retained value (the EnvMonitor's on_message will pick it up)
                try:
                    cl.subscribe(topic, qos=0)
                except (ConnectionError, TimeoutError, OSError):
                    logger.exception(f"EnvProbe: subscribe failed for {topic}")
            except ImportError:
                logger.exception(f"EnvProbe: failed for sid={sid} topic={topic}")
    except ImportError:
        logger.exception("EnvProbe: outer failed")
