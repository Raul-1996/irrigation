import logging
import sqlite3
import ssl
import threading
from collections import deque
from datetime import datetime

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


class WaterMonitor:
    """Подписывается на топики счётчиков воды по группам, хранит последние импульсы и рассчитывает поток."""

    def __init__(self):
        self._clients: dict[int, mqtt.Client] = {}  # key: group_id
        self._topics: dict[int, str] = {}
        self._server_ids: dict[int, int] = {}
        self._pulse_liters: dict[int, int] = {}  # 1|10|100
        self._samples: dict[int, deque[tuple[float, int]]] = {}  # ts, pulses
        self._lock = threading.Lock()
        self._reconfigure_lock = threading.RLock()

    @staticmethod
    def _retire_client(client) -> None:
        """Close both the MQTT socket and its network loop."""
        try:
            client.disconnect()
        except (ConnectionError, TimeoutError, OSError, RuntimeError, AttributeError) as exc:
            logger.debug("WaterMonitor disconnect failed during retirement: %s", exc)
        try:
            client.loop_stop()
        except (ConnectionError, TimeoutError, OSError, RuntimeError, AttributeError) as exc:
            logger.debug("WaterMonitor loop_stop failed during retirement: %s", exc)

    def disable(self) -> bool:
        """Idempotently disable every meter and discard its binding samples."""
        with self._reconfigure_lock:
            with self._lock:
                clients = list(self._clients.values())
                self._clients.clear()
                self._topics.clear()
                self._server_ids.clear()
                self._pulse_liters.clear()
                # Samples are meaningful only for the broker/topic/pulse-size
                # binding that produced them. Never carry them into a new one.
                self._samples.clear()

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

    def start(self) -> bool:
        """Backward-compatible entry point for a full reconfiguration."""
        return self.reconfigure()

    def reconfigure(self) -> bool:
        """Reload all meter bindings without leaving old subscriptions alive."""
        with self._reconfigure_lock:
            self.disable()
            if mqtt is None:
                logger.warning("WaterMonitor reconfigure skipped: paho-mqtt not available")
                return False

            try:
                groups = db.get_groups() or []
            except (sqlite3.Error, ConnectionError, TimeoutError, OSError):
                logger.exception("WaterMonitor could not load group configuration")
                return False

            success = True
            for g in groups:
                try:
                    gid = int(g.get("id"))
                    if gid == 999:
                        continue
                    if int(g.get("use_water_meter") or 0) != 1:
                        continue
                    topic = (g.get("water_mqtt_topic") or "").strip()
                    sid = g.get("water_mqtt_server_id")
                    if not topic or not sid:
                        continue
                    pulse = str(g.get("water_pulse_size") or "1l")
                    liters = 100 if pulse == "100l" else 10 if pulse == "10l" else 1
                    if not self._start_group(gid, int(sid), topic, liters):
                        success = False
                except (ConnectionError, TimeoutError, OSError, RuntimeError, ValueError, TypeError):
                    logger.exception("WaterMonitor start group failed")
                    success = False
            return success

    def _start_group(self, group_id: int, server_id: int, topic: str, liters: int) -> bool:
        client = None
        try:
            server = db.get_mqtt_server(server_id)
            if not server:
                logger.error("WaterMonitor MQTT server %s was not found for group %s", server_id, group_id)
                return False

            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            if server.get("username"):
                client.username_pw_set(server.get("username"), server.get("password") or None)
            _configure_tls(client, server)

            def _on_message(callback_client, userdata, msg):
                try:
                    payload = msg.payload.decode("utf-8", errors="ignore").strip()
                    pulses = int("".join(ch for ch in payload if ch.isdigit() or ch == "-"))
                    timestamp = datetime.now().timestamp()
                    with self._lock:
                        if self._clients.get(group_id) is not callback_client:
                            return
                        samples = self._samples.setdefault(group_id, deque(maxlen=256))
                        samples.append((timestamp, pulses))
                except (ValueError, TypeError, KeyError):
                    logger.exception("WaterMonitor on_message failed for group %s", group_id)

            def _on_connect(client, userdata, connect_flags, reason_code, properties):
                try:
                    with self._lock:
                        if self._clients.get(group_id) is not client:
                            return
                    client.subscribe(topic, qos=0)
                    logger.info("WaterMonitor group %s subscribed %s", group_id, topic)
                except (ConnectionError, TimeoutError, OSError):
                    logger.exception("WaterMonitor subscribe failed for group %s", group_id)

            def _on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
                # Preserve the tracked handle across transient disconnects so
                # Paho can reconnect it and disable/reconfigure can retire it.
                logger.info("WaterMonitor group %s disconnected: rc=%s", group_id, reason_code)

            client.on_message = _on_message
            client.on_connect = _on_connect
            client.on_disconnect = _on_disconnect
            client.connect(server.get("host") or "127.0.0.1", int(server.get("port") or 1883), 10)
            client.subscribe(topic, qos=0)

            with self._lock:
                self._clients[group_id] = client
                self._topics[group_id] = topic
                self._server_ids[group_id] = server_id
                self._pulse_liters[group_id] = liters
                self._samples[group_id] = deque(maxlen=256)
            try:
                client.loop_start()
            except (ConnectionError, TimeoutError, OSError, RuntimeError):
                with self._lock:
                    if self._clients.get(group_id) is client:
                        self._clients.pop(group_id, None)
                        self._topics.pop(group_id, None)
                        self._server_ids.pop(group_id, None)
                        self._pulse_liters.pop(group_id, None)
                        self._samples.pop(group_id, None)
                raise
            return True
        except (ConnectionError, TimeoutError, OSError, RuntimeError, ValueError, AttributeError):
            logger.exception("WaterMonitor start failed for group %s", group_id)
            if client is not None:
                with self._lock:
                    if self._clients.get(group_id) is client:
                        self._clients.pop(group_id, None)
                        self._topics.pop(group_id, None)
                        self._server_ids.pop(group_id, None)
                        self._pulse_liters.pop(group_id, None)
                        self._samples.pop(group_id, None)
                self._retire_client(client)
            return False

    def get_current_reading_m3(self, group_id: int) -> float | None:
        try:
            g = next((gg for gg in (db.get_groups() or []) if int(gg.get("id")) == int(group_id)), None)
            if not g:
                return None
            base_m3 = float(g.get("water_base_value_m3") or 0.0)
            base_p = int(g.get("water_base_pulses") or 0)
            liters = self._pulse_liters.get(int(group_id), 1)
            with self._lock:
                dq = self._samples.get(int(group_id)) or deque()
                cur_p = dq[-1][1] if dq else base_p
            delta_p = max(0, cur_p - base_p)
            val = base_m3 + (delta_p * liters) / 1000.0
            return round(val, 3)
        except (sqlite3.Error, OSError) as e:
            logger.debug("Exception in get_current_reading_m3: %s", e)
            return None

    def get_flow_lpm(self, group_id: int, since_iso: str | None) -> float | None:
        try:
            if not since_iso:
                return None
            try:
                since_ts = datetime.strptime(since_iso, "%Y-%m-%d %H:%M:%S").timestamp()
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in get_flow_lpm: %s", e)
                return None
            liters = self._pulse_liters.get(int(group_id), 1)
            with self._lock:
                dq = list(self._samples.get(int(group_id)) or [])
            # filter samples after start
            samples = [(ts, p) for ts, p in dq if ts >= since_ts]
            if len(samples) < 2:
                return None
            # Prefer at least 5 increments if available
            p0 = samples[0][1]
            idx = 0
            target_delta = 5
            for i in range(1, len(samples)):
                if samples[i][1] - p0 >= target_delta:
                    idx = i
                    break
            if idx == 0:
                idx = len(samples) - 1
            ts0, p_start = samples[0]
            ts1, p_end = samples[idx]
            dp = max(0, p_end - p_start)
            dt_sec = max(1.0, ts1 - ts0)
            lpm = (dp * liters) / (dt_sec / 60.0)
            return round(lpm, 2)
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in line_430: %s", e)
            return None

    def summarize_run(self, group_id: int, since_iso: str | None) -> tuple[float | None, float | None]:
        """Возвращает (total_liters, avg_flow_lpm) за интервал с момента старта до последнего сэмпла."""
        try:
            if not since_iso:
                return (None, None)
            try:
                since_ts = datetime.strptime(since_iso, "%Y-%m-%d %H:%M:%S").timestamp()
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in summarize_run: %s", e)
                return (None, None)
            liters_per_pulse = self._pulse_liters.get(int(group_id), 1)
            with self._lock:
                dq = list(self._samples.get(int(group_id)) or [])
            samples = [(ts, p) for ts, p in dq if ts >= since_ts]
            if len(samples) < 2:
                return (0.0, 0.0)
            ts0, p0 = samples[0]
            ts1, p1 = samples[-1]
            total_p = max(0, p1 - p0)
            total_l = total_p * liters_per_pulse
            dt_min = max(0.001, (ts1 - ts0) / 60.0)
            avg_lpm = total_l / dt_min
            return (round(total_l, 2), round(avg_lpm, 2))
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in summarize_run: %s", e)
            return (None, None)

    def get_raw_pulses(self, group_id: int) -> int | None:
        """Возвращает последние сырые импульсы для группы (или None, если нет сэмплов)."""
        try:
            with self._lock:
                dq = self._samples.get(int(group_id))
                if not dq or len(dq) == 0:
                    return None
                return int(dq[-1][1])
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in get_raw_pulses: %s", e)
            return None

    def get_pulses_at_or_before(self, group_id: int, ts: float) -> int | None:
        """Пульсы на момент ts (берём последний сэмпл с ts' <= заданного)."""
        try:
            with self._lock:
                arr = list(self._samples.get(int(group_id)) or [])
            if not arr:
                return None
            best = None
            for t, p in arr:
                if t <= ts:
                    best = p
                else:
                    break
            return int(best) if best is not None else int(arr[0][1]) if arr else None
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in get_pulses_at_or_before: %s", e)
            return None

    def get_pulses_at_or_after(self, group_id: int, ts: float) -> int | None:
        """Пульсы после/на момент ts (берём первый сэмпл с ts' >= заданного)."""
        try:
            with self._lock:
                arr = list(self._samples.get(int(group_id)) or [])
            if not arr:
                return None
            for t, p in arr:
                if t >= ts:
                    return int(p)
            return int(arr[-1][1]) if arr else None
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in get_pulses_at_or_after: %s", e)
            return None


water_monitor = WaterMonitor()


def start_water_monitor():
    try:
        water_monitor.start()
    except (ConnectionError, TimeoutError, OSError, ValueError):
        logger.exception("start_water_monitor failed")
