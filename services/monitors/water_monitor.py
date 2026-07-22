import logging
import sqlite3
import ssl
import threading
import time
from collections import deque
from datetime import datetime

from database import db

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

logger = logging.getLogger(__name__)

_CONNECT_READY_TIMEOUT_SECONDS = 5.0


def _reason_code_value(reason_code) -> int:
    value = getattr(reason_code, "value", reason_code)
    return int(value)


def _connack_succeeded(reason_code) -> bool:
    is_failure = getattr(reason_code, "is_failure", None)
    if is_failure is not None and bool(is_failure):
        return False
    try:
        return _reason_code_value(reason_code) == 0
    except (TypeError, ValueError):
        return False


def _suback_succeeded(reason_code_list) -> bool:
    if not reason_code_list:
        return False
    for reason_code in reason_code_list:
        is_failure = getattr(reason_code, "is_failure", None)
        if is_failure is not None and bool(is_failure):
            return False
        try:
            if _reason_code_value(reason_code) >= 128:
                return False
        except (TypeError, ValueError):
            return False
    return True


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

    def _retire_client(self, client) -> None:
        """Close both the MQTT socket and its network loop."""
        with self._lock:
            try:
                client._water_monitor_generation_state = "retired"
                client._water_monitor_retired = True
                client._water_monitor_staged = False
                client._water_monitor_connected = False
            except AttributeError:
                pass
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
        """Build and atomically install a complete meter subscription generation."""
        with self._reconfigure_lock:
            if mqtt is None:
                logger.warning("WaterMonitor reconfigure skipped: paho-mqtt not available")
                return False

            try:
                bindings = self._load_bindings_strict()
            except Exception:  # Transaction boundary: any load/validation failure preserves the live generation.
                logger.exception("WaterMonitor configuration load/validation failed; keeping current subscriptions")
                return False

            staged_clients: dict[int, mqtt.Client] = {}
            try:
                deadline = time.monotonic() + _CONNECT_READY_TIMEOUT_SECONDS
                for group_id, server_id, topic, liters, server in bindings:
                    staged_clients[group_id] = self._build_group_client(
                        group_id,
                        topic,
                        server,
                    )
                for group_id, client in staged_clients.items():
                    remaining = max(0.0, deadline - time.monotonic())
                    ready_event = client._water_monitor_ready_event
                    if not ready_event.wait(remaining):
                        raise TimeoutError(f"WaterMonitor group {group_id} MQTT readiness timed out")
                    connect_error = client._water_monitor_connect_error
                    if connect_error is not None:
                        raise connect_error
                    if not client._water_monitor_connected:
                        raise ConnectionError(f"WaterMonitor group {group_id} disconnected before readiness")
                topics = {group_id: topic for group_id, _server_id, topic, _liters, _server in bindings}
                server_ids = {group_id: server_id for group_id, server_id, _topic, _liters, _server in bindings}
                pulse_liters = {group_id: liters for group_id, _server_id, _topic, liters, _server in bindings}

                # This lock is the generation linearization boundary. A staged
                # disconnect callback uses the same lock: it either records a
                # failure before this validation, or observes state="live"
                # afterwards and is handled as a normal runtime disconnect.
                with self._lock:
                    for group_id, client in staged_clients.items():
                        if client._water_monitor_generation_state != "staged":
                            raise ConnectionError(f"WaterMonitor group {group_id} left staged state before swap")
                        if not client._water_monitor_ready_event.is_set():
                            raise ConnectionError(f"WaterMonitor group {group_id} lost readiness before swap")
                        if client._water_monitor_connect_error is not None:
                            raise client._water_monitor_connect_error
                        if not client._water_monitor_connected:
                            raise ConnectionError(f"WaterMonitor group {group_id} disconnected before swap")

                    previous_clients = list(self._clients.values())
                    samples: dict[int, deque[tuple[float, int]]] = {}
                    for group_id, client in staged_clients.items():
                        pending = list(getattr(client, "_water_monitor_pending_samples", ()))
                        samples[group_id] = deque(pending, maxlen=256)
                        client._water_monitor_generation_state = "live"
                        client._water_monitor_staged = False

                    self._clients = staged_clients
                    self._topics = topics
                    self._server_ids = server_ids
                    self._pulse_liters = pulse_liters
                    self._samples = samples
            except Exception:  # Transaction boundary: retire every staged client before returning.
                logger.exception("WaterMonitor generation build failed; keeping current subscriptions")
                for client in staged_clients.values():
                    self._retire_client(client)
                return False

            retired: set[int] = set()
            for client in previous_clients:
                if id(client) in retired:
                    continue
                retired.add(id(client))
                self._retire_client(client)
            return True

    @staticmethod
    def _validate_server(server: dict, server_id: int) -> None:
        if not isinstance(server, dict):
            raise ValueError(f"MQTT server {server_id} has invalid data")
        if "enabled" in server and int(server.get("enabled") or 0) != 1:
            raise ValueError(f"MQTT server {server_id} is disabled")
        port = int(server.get("port") or 1883)
        if port < 1 or port > 65535:
            raise ValueError(f"MQTT server {server_id} has invalid port")

    def _load_bindings_strict(self) -> list[tuple[int, int, str, int, dict]]:
        """Load and validate the whole desired generation without changing live state."""
        load_groups = getattr(db, "get_groups_strict", None)
        if not callable(load_groups):
            raise RuntimeError("Database does not provide get_groups_strict()")

        groups = load_groups()
        if not isinstance(groups, list):
            raise ValueError("Strict group loader returned invalid data")

        bindings: list[tuple[int, int, str, int, dict]] = []
        seen_group_ids: set[int] = set()
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError("Water meter group entry is not an object")

            group_id = int(group.get("id"))
            if group_id in seen_group_ids:
                raise ValueError(f"Duplicate water meter group id: {group_id}")
            seen_group_ids.add(group_id)
            if group_id == 999:
                continue

            enabled = int(group.get("use_water_meter") or 0)
            if enabled not in (0, 1):
                raise ValueError(f"Group {group_id} has invalid use_water_meter value")
            if enabled == 0:
                continue

            topic = str(group.get("water_mqtt_topic") or "").strip()
            raw_server_id = group.get("water_mqtt_server_id")
            if not topic or raw_server_id in (None, ""):
                raise ValueError(f"Group {group_id} has incomplete water meter configuration")
            server_id = int(raw_server_id)
            if server_id <= 0:
                raise ValueError(f"Group {group_id} has invalid MQTT server id")

            pulse = str(group.get("water_pulse_size") or "1l")
            pulse_sizes = {"1l": 1, "10l": 10, "100l": 100}
            if pulse not in pulse_sizes:
                raise ValueError(f"Group {group_id} has invalid water pulse size")

            server = db.get_mqtt_server(server_id)
            if not server:
                raise ValueError(f"MQTT server {server_id} was not found for group {group_id}")
            self._validate_server(server, server_id)
            bindings.append((group_id, server_id, topic, pulse_sizes[pulse], server))
        return bindings

    def _build_group_client(self, group_id: int, topic: str, server: dict):
        client = None
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client._water_monitor_generation_state = "staged"
            client._water_monitor_retired = False
            client._water_monitor_staged = True
            client._water_monitor_pending_samples = deque(maxlen=256)
            client._water_monitor_ready_event = threading.Event()
            client._water_monitor_connect_error = None
            client._water_monitor_connected = False
            client._water_monitor_expected_mid = None
            if server.get("username"):
                client.username_pw_set(server.get("username"), server.get("password") or None)
            _configure_tls(client, server)

            def _on_message(callback_client, userdata, msg):
                try:
                    payload = msg.payload.decode("utf-8", errors="ignore").strip()
                    pulses = int("".join(ch for ch in payload if ch.isdigit() or ch == "-"))
                    timestamp = datetime.now().timestamp()
                    with self._lock:
                        if self._clients.get(group_id) is callback_client:
                            samples = self._samples.setdefault(group_id, deque(maxlen=256))
                            samples.append((timestamp, pulses))
                        elif getattr(callback_client, "_water_monitor_generation_state", None) == "staged":
                            callback_client._water_monitor_pending_samples.append((timestamp, pulses))
                except (ValueError, TypeError, KeyError):
                    logger.exception("WaterMonitor on_message failed for group %s", group_id)

            def _on_connect(client, userdata, connect_flags, reason_code, properties):
                with self._lock:
                    if getattr(client, "_water_monitor_generation_state", None) == "retired":
                        return
                    if not _connack_succeeded(reason_code):
                        client._water_monitor_connected = False
                        client._water_monitor_connect_error = ConnectionError(
                            f"WaterMonitor group {group_id} MQTT CONNACK rejected: {reason_code}"
                        )
                        client._water_monitor_ready_event.set()
                        return
                try:
                    result = client.subscribe(topic, qos=0)
                    if not isinstance(result, (tuple, list)) or len(result) < 2 or int(result[0]) != 0:
                        raise ConnectionError(f"WaterMonitor group {group_id} MQTT subscribe was rejected locally")
                    with self._lock:
                        if getattr(client, "_water_monitor_generation_state", None) == "retired":
                            return
                        client._water_monitor_connected = True
                        client._water_monitor_expected_mid = int(result[1])
                    logger.info("WaterMonitor group %s subscription requested for %s", group_id, topic)
                except (ConnectionError, TimeoutError, OSError, RuntimeError, ValueError, TypeError) as exc:
                    with self._lock:
                        client._water_monitor_connect_error = exc
                        client._water_monitor_ready_event.set()

            def _on_subscribe(client, userdata, mid, reason_code_list, properties):
                with self._lock:
                    if getattr(client, "_water_monitor_generation_state", None) == "retired":
                        return
                    if int(mid) != getattr(client, "_water_monitor_expected_mid", None):
                        return
                    if not _suback_succeeded(reason_code_list):
                        client._water_monitor_connect_error = ConnectionError(
                            f"WaterMonitor group {group_id} MQTT SUBACK rejected: {reason_code_list}"
                        )
                    else:
                        client._water_monitor_connect_error = None
                        logger.info("WaterMonitor group %s subscribed %s", group_id, topic)
                    client._water_monitor_ready_event.set()

            def _on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
                # Preserve the tracked handle across transient disconnects so
                # Paho can reconnect it and disable/reconfigure can retire it.
                with self._lock:
                    client._water_monitor_connected = False
                    if getattr(client, "_water_monitor_generation_state", None) == "staged":
                        client._water_monitor_connect_error = ConnectionError(
                            f"WaterMonitor group {group_id} disconnected before readiness: {reason_code}"
                        )
                        client._water_monitor_ready_event.set()
                logger.info("WaterMonitor group %s disconnected: rc=%s", group_id, reason_code)

            client.on_message = _on_message
            client.on_connect = _on_connect
            client.on_subscribe = _on_subscribe
            client.on_disconnect = _on_disconnect
            host = server.get("host") or "127.0.0.1"
            port = int(server.get("port") or 1883)
            connect_async = getattr(client, "connect_async", None)
            if callable(connect_async):
                connect_async(host, port, 10)
            else:
                client.connect(host, port, 10)
            client.loop_start()
            return client
        except Exception:  # The caller rolls back the rest of the staged generation.
            if client is not None:
                self._retire_client(client)
            raise

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
