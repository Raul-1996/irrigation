from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import ssl
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from database import db

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

logger = logging.getLogger(__name__)

_MQTT_READY_TIMEOUT_SECONDS = 5.0
_RAIN_POSTPONE_FALLBACK_LOCK = threading.Lock()


def _db_path() -> str | None:
    path = getattr(db, "db_path", None)
    return os.fspath(path) if isinstance(path, (str, Path)) else None


def _strict_target_groups() -> list[int]:
    """Read rain-enabled groups without fail-soft repository fallbacks."""
    path = _db_path()
    if path is not None:
        with sqlite3.connect(path, timeout=5) as conn:
            rows = conn.execute("SELECT id, use_rain_sensor FROM groups ORDER BY id").fetchall()
        return [int(group_id) for group_id, enabled in rows if int(group_id) != 999 and int(enabled or 0) == 1]

    # Small compatibility path for isolated unit-test facades. Production
    # always has ``db_path`` and therefore always takes the strict SQL path.
    rows = db.get_groups()
    if not isinstance(rows, list):
        raise RuntimeError("invalid groups snapshot")
    target_groups: list[int] = []
    for row in rows:
        if not isinstance(row, dict) or "id" not in row:
            raise RuntimeError("invalid group row")
        group_id = int(row["id"])
        if group_id == 999:
            continue
        enabled = row.get("use_rain_sensor")
        if "use_rain_sensor" not in row:
            enabled = db.get_group_use_rain(group_id)
        if bool(int(enabled or 0)):
            target_groups.append(group_id)
    return target_groups


def _strict_setting_value(key: str) -> str | None:
    path = _db_path()
    if path is not None:
        with sqlite3.connect(path, timeout=5) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ? LIMIT 1", (key,)).fetchone()
        return None if row is None else row[0]
    value = db.get_setting_value(key)
    return value if isinstance(value, str) or value is None else None


def _strict_group_zones(group_id: int) -> list[dict[str, Any]]:
    path = _db_path()
    if path is not None:
        with sqlite3.connect(path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, group_id, postpone_until, postpone_reason
                FROM zones
                WHERE group_id = ?
                ORDER BY id
                """,
                (int(group_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    rows = db.get_zones()
    if not isinstance(rows, list):
        raise RuntimeError("invalid zones snapshot")
    return [row for row in rows if isinstance(row, dict) and int(row.get("group_id") or 0) == int(group_id)]


def _apply_rain_postpone_compat(group_id: int, postpone_until: str) -> bool:
    """Fallback rain overlay: fill only unset rows and preserve provenance."""
    requested = datetime.strptime(postpone_until, "%Y-%m-%d %H:%M:%S")
    serialized = requested.strftime("%Y-%m-%d %H:%M:%S")
    path = _db_path()
    with _RAIN_POSTPONE_FALLBACK_LOCK:
        if path is not None:
            with sqlite3.connect(path, timeout=5) as conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT id, postpone_until, postpone_reason FROM zones WHERE group_id = ? ORDER BY id",
                    (int(group_id),),
                ).fetchall()
                unset_ids = [
                    int(zone_id)
                    for zone_id, current, reason in rows
                    if current is None and reason is None
                ]
                for zone_id in unset_ids:
                    cursor = conn.execute(
                        """
                        UPDATE zones
                        SET postpone_until = ?, postpone_reason = 'rain', updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND postpone_until IS NULL AND postpone_reason IS NULL
                        """,
                        (serialized, zone_id),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("concurrent postpone mutation")
                conn.commit()
            return True

        zones = _strict_group_zones(group_id)
        unset = [
            zone
            for zone in zones
            if zone.get("postpone_until") is None and zone.get("postpone_reason") is None
        ]
        for zone in unset:
            if db.update_zone_postpone(int(zone["id"]), serialized, "rain") is not True:
                return False
        return True


def _apply_rain_postpone_deadline(group_id: int, postpone_until: str) -> bool:
    """Use the shared source-preserving postpone overlay when integrated."""
    try:
        import services.postpone as postpone_service

        apply_rain = postpone_service.apply_group_rain_postpone
    except (AttributeError, ImportError):
        return _apply_rain_postpone_compat(group_id, postpone_until)

    # Tests can inject a facade into this module. Keep all mutations on that
    # same facade even before the shared helper's integration commit lands.
    if getattr(postpone_service, "db", db) is not db:
        return _apply_rain_postpone_compat(group_id, postpone_until)

    try:
        result = apply_rain(group_id, postpone_until, db_facade=db)
        return result is True or isinstance(result, dict)
    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
        logger.exception("RainMonitor: rain postpone apply failed for group=%s", group_id)
        return False


def _clear_rain_postpone_compat(group_id: int) -> bool:
    """Fallback source-aware clear with snapshot+mutation serialization."""
    path = _db_path()
    with _RAIN_POSTPONE_FALLBACK_LOCK:
        if path is not None:
            with sqlite3.connect(path, timeout=5) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE zones
                    SET postpone_until = NULL, postpone_reason = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE group_id = ? AND postpone_reason = 'rain'
                    """,
                    (int(group_id),),
                )
                conn.commit()
            return True

        zones = _strict_group_zones(group_id)
        rain_zones = [zone for zone in zones if zone.get("postpone_reason") == "rain"]
        for zone in rain_zones:
            if db.update_zone_postpone(int(zone["id"]), None, None) is not True:
                return False
        return True


def _clear_rain_postpone(group_id: int) -> bool:
    try:
        import services.postpone as postpone_service

        clear_rain = postpone_service.clear_group_rain_postpone
    except (AttributeError, ImportError):
        return _clear_rain_postpone_compat(group_id)

    if getattr(postpone_service, "db", db) is not db:
        return _clear_rain_postpone_compat(group_id)
    try:
        return clear_rain(group_id, db_facade=db) is True
    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
        logger.exception("RainMonitor: rain postpone clear failed for group=%s", group_id)
        return False


def _reason_code_failed(reason_code: Any) -> bool:
    if reason_code is None:
        return True
    is_failure = getattr(reason_code, "is_failure", None)
    if is_failure is not None:
        return bool(is_failure)
    value = getattr(reason_code, "value", reason_code)
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return True


def _suback_succeeded(reason_codes: Any) -> bool:
    if not isinstance(reason_codes, (list, tuple)) or not reason_codes:
        return False
    return all(not _reason_code_failed(reason_code) for reason_code in reason_codes)


class RainMonitor:
    def __init__(self):
        self.client = None
        self.topic: str | None = None
        self.server_id: int | None = None
        self.is_rain: bool | None = None
        self._cfg: dict | None = None
        self._generation = 0
        self._blocked_groups: set[int] = set()
        self._block_all = False
        self._persisted_state_loaded = False
        self._last_sensor_value: bool | None = None
        self._lifecycle_lock = threading.RLock()
        self._callback_lock = threading.RLock()
        self._state_lock = threading.RLock()

    @staticmethod
    def _retire_client(client) -> None:
        if client is None:
            return
        try:
            client.disconnect()
        except (ConnectionError, TimeoutError, OSError, RuntimeError):
            logger.exception("RainMonitor disconnect failed")
        try:
            client.loop_stop()
        except (ConnectionError, TimeoutError, OSError, RuntimeError):
            logger.exception("RainMonitor loop stop failed")

    def _load_persisted_state(self) -> None:
        with self._state_lock:
            if self._persisted_state_loaded:
                return
            self._persisted_state_loaded = True
        try:
            persisted = _strict_setting_value("rain.active")
        except (sqlite3.Error, OSError, AttributeError, RuntimeError):
            logger.exception("RainMonitor: persisted rain state read failed; blocking all groups")
            with self._state_lock:
                self.is_rain = True
                self._block_all = True
            return
        if str(persisted).strip().lower() in {"1", "true"}:
            with self._state_lock:
                self.is_rain = True
                self._block_all = True
            try:
                groups = _strict_target_groups()
            except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
                logger.exception("RainMonitor: persisted rain group read failed; blocking all groups")
            else:
                with self._state_lock:
                    self._blocked_groups = set(groups)
                    self._block_all = False

    def stop(self):
        with self._lifecycle_lock:
            with self._callback_lock, self._state_lock:
                old_client = self.client
                self.client = None
                self._generation += 1
            self._retire_client(old_client)

    def start(self, cfg: dict) -> bool:
        """Backward-compatible alias for atomic runtime reconfiguration."""
        return self.reconfigure(cfg)

    def reconfigure(self, config: dict) -> bool:
        """Stage MQTT and swap only after CONNACK plus matching SUBACK.

        ``False`` is a hard no-op for the active generation: the previous
        client, configuration, topic, and generation remain authoritative.
        """
        if not isinstance(config, dict):
            return False
        cfg = dict(config)
        enabled = cfg.get("enabled") is True
        topic_raw = cfg.get("topic", "")
        if not isinstance(topic_raw, str):
            return False
        topic = topic_raw.strip()
        server_id = cfg.get("server_id")
        if isinstance(server_id, bool):
            return False
        if server_id is not None:
            try:
                server_id = int(server_id)
            except (TypeError, ValueError):
                return False
        sensor_type = cfg.get("type", "NO")
        if not isinstance(sensor_type, str) or sensor_type.upper() not in {"NO", "NC"}:
            return False
        sensor_type = sensor_type.upper()
        cfg.update({"enabled": enabled, "topic": topic, "server_id": server_id, "type": sensor_type})

        with self._lifecycle_lock:
            self._load_persisted_state()
            if not enabled:
                with self._callback_lock, self._state_lock:
                    old_client = self.client
                    self.client = None
                    self._generation += 1
                    self._cfg = cfg
                    self.topic = None
                    self.server_id = None
                self._retire_client(old_client)
                return True

            if not topic or server_id is None or server_id <= 0 or mqtt is None:
                return False
            if "+" in topic or "#" in topic or "\x00" in topic:
                return False

            try:
                server = db.get_mqtt_server(server_id)
            except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
                logger.exception("RainMonitor: broker read failed")
                return False
            if not isinstance(server, dict):
                return False
            try:
                if int(server.get("enabled", 1) or 0) != 1:
                    return False
            except (TypeError, ValueError):
                return False

            try:
                staged_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            except (AttributeError, TypeError, ValueError, RuntimeError):
                logger.exception("RainMonitor: MQTT client creation failed")
                return False

            ready = threading.Event()
            failed = threading.Event()
            settled = threading.Event()
            stage_lock = threading.Lock()
            stage: dict[str, Any] = {
                "expected_mid": None,
                "early_subacks": {},
                "latest_payload": None,
                "connected": False,
                "activated": False,
            }
            candidate_generation = self._generation + 1

            def _mark_ready() -> None:
                ready.set()
                settled.set()

            def _mark_failed() -> None:
                failed.set()
                settled.set()

            def _is_active() -> bool:
                with self._state_lock:
                    return self.client is staged_client and self._generation == candidate_generation

            def _is_retired() -> bool:
                with stage_lock:
                    activated = bool(stage["activated"])
                return activated and not _is_active()

            def _on_connect(client, userdata, flags, reason_code, properties=None):
                del userdata, flags, properties
                if client is not staged_client:
                    return
                if _is_retired():
                    return
                if _reason_code_failed(reason_code):
                    if not _is_active():
                        _mark_failed()
                    return
                try:
                    with stage_lock:
                        stage["connected"] = True
                    subscribe_result = client.subscribe(topic, qos=0)
                    result_code, message_id = subscribe_result
                    if int(result_code) != 0:
                        _mark_failed()
                        return
                    message_id = int(message_id)
                    with stage_lock:
                        stage["expected_mid"] = message_id
                        early_result = stage["early_subacks"].pop(message_id, None)
                    if early_result is not None:
                        (_mark_ready if early_result else _mark_failed)()
                except (AttributeError, TypeError, ValueError, RuntimeError, OSError):
                    logger.exception("RainMonitor: subscribe failed")
                    _mark_failed()

            def _on_subscribe(client, userdata, mid, reason_codes, properties=None):
                del userdata, properties
                if client is not staged_client:
                    return
                if _is_retired():
                    return
                try:
                    message_id = int(mid)
                except (TypeError, ValueError):
                    _mark_failed()
                    return
                succeeded = _suback_succeeded(reason_codes)
                with stage_lock:
                    expected_mid = stage["expected_mid"]
                    if expected_mid is None:
                        stage["early_subacks"][message_id] = succeeded
                        return
                if message_id != expected_mid:
                    return
                (_mark_ready if succeeded else _mark_failed)()

            def _on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
                del userdata, disconnect_flags, reason_code, properties
                if client is not staged_client:
                    return
                active = _is_active()
                with stage_lock:
                    stage["connected"] = False
                if not active:
                    _mark_failed()

            def _on_message(client, userdata, message):
                del userdata
                if client is not staged_client:
                    return
                payload = getattr(message, "payload", b"")
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8", errors="ignore")
                else:
                    payload = str(payload)
                with self._callback_lock:
                    if not _is_active():
                        with stage_lock:
                            if not stage.get("activated"):
                                stage["latest_payload"] = payload
                        return
                    try:
                        self._handle_payload(payload)
                    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
                        logger.exception("RainMonitor on_message failed")

            staged_client.on_connect = _on_connect
            staged_client.on_subscribe = _on_subscribe
            staged_client.on_disconnect = _on_disconnect
            staged_client.on_message = _on_message

            try:
                if server.get("username"):
                    staged_client.username_pw_set(server.get("username"), server.get("password") or None)
                self._configure_tls(staged_client, server)
                host = server.get("host") or "127.0.0.1"
                port = int(server.get("port") or 1883)
                connect = getattr(staged_client, "connect_async", None)
                if not callable(connect):
                    connect = staged_client.connect
                connect_result = connect(host, port, 30)
                if connect_result not in (None, 0):
                    raise ConnectionError(f"MQTT connect returned {connect_result}")
                staged_client.loop_start()
            except (ConnectionError, TimeoutError, OSError, RuntimeError, TypeError, ValueError):
                logger.exception("RainMonitor staged client init failed")
                self._retire_client(staged_client)
                return False

            if not settled.wait(_MQTT_READY_TIMEOUT_SECONDS) or not ready.is_set() or failed.is_set():
                logger.error("RainMonitor: MQTT readiness failed or timed out")
                self._retire_client(staged_client)
                return False

            with stage_lock:
                connected = stage["connected"]
            if not connected:
                logger.error("RainMonitor: staged MQTT disconnected before activation")
                self._retire_client(staged_client)
                return False

            with self._callback_lock:
                with self._state_lock:
                    old_client = self.client
                    self.client = staged_client
                    self._generation = candidate_generation
                    self._cfg = cfg
                    self.topic = topic
                    self.server_id = server_id
                with stage_lock:
                    stage["activated"] = True
                    retained_payload = stage["latest_payload"]
                    stage["latest_payload"] = None
                if retained_payload is not None:
                    try:
                        self._handle_payload(str(retained_payload))
                    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
                        logger.exception("RainMonitor staged retained payload failed")
            self._retire_client(old_client)
            return True

    def _ensure_client(self) -> bool:
        """Compatibility helper retained for older callers/tests."""
        cfg = dict(self._cfg or {})
        cfg.update({"enabled": True, "topic": self.topic or "", "server_id": self.server_id})
        return self.reconfigure(cfg)

    @staticmethod
    def _configure_tls(client, server: dict) -> None:
        """Configure broker TLS or raise before any network connection."""
        if int(server.get("tls_enabled") or 0) != 1:
            return

        configured = (server.get("tls_version") or "TLS_CLIENT").upper().replace("_", ".").strip()
        versions = {
            "TLS": ssl.PROTOCOL_TLS_CLIENT,
            "TLS.CLIENT": ssl.PROTOCOL_TLS_CLIENT,
            "TLSV1": ssl.PROTOCOL_TLSv1,
            "TLSV1.0": ssl.PROTOCOL_TLSv1,
            "TLSV1.1": ssl.PROTOCOL_TLSv1_1,
            "TLSV1.2": ssl.PROTOCOL_TLSv1_2,
        }
        try:
            tls_version = versions[configured]
        except KeyError as error:
            raise ValueError("Unsupported MQTT TLS version") from error

        client.tls_set(
            ca_certs=server.get("tls_ca_path") or None,
            certfile=server.get("tls_cert_path") or None,
            keyfile=server.get("tls_key_path") or None,
            tls_version=tls_version,
        )
        if int(server.get("tls_insecure") or 0) == 1:
            client.tls_insecure_set(True)

    def _handle_payload(self, payload: str):
        normalized = (payload or "").strip().lower()
        value = (
            True
            if normalized in ("1", "true", "rain", "yes", "on")
            else False
            if normalized in ("0", "false", "no_rain", "no", "off")
            else None
        )
        if value is None:
            return
        try:
            sensor_type = str((self._cfg or {}).get("type") or db.get_rain_config().get("type") or "NO").upper()
        except (sqlite3.Error, OSError, AttributeError):
            logger.exception("RainMonitor: sensor type read failed; retaining configured/default NO")
            sensor_type = str((self._cfg or {}).get("type") or "NO").upper()
        logical_rain = not value if sensor_type == "NC" else value

        with self._state_lock:
            repeated = self._last_sensor_value is logical_rain
        if repeated:
            return
        if logical_rain:
            with self._state_lock:
                self.is_rain = True
            applied = self._on_rain_start()
            if applied is not False:
                with self._state_lock:
                    self._last_sensor_value = True
        else:
            cleared = self._on_rain_stop()
            if cleared is not False:
                with self._state_lock:
                    self.is_rain = False
                    self._last_sensor_value = False

    @staticmethod
    def _cancel_and_stop_group(group_id: int) -> bool:
        cancellation_ok = False
        try:
            from irrigation_scheduler import get_scheduler

            scheduler = get_scheduler()
            if scheduler is not None:
                result = scheduler.cancel_group_jobs(int(group_id), master_close_immediately=True)
                cancellation_ok = (
                    isinstance(result, dict)
                    and result.get("success") is True
                    and not (result.get("unresolved") or [])
                )
        except (ImportError, sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
            logger.exception("RainMonitor: structured scheduler cancellation failed group=%s", group_id)

        if cancellation_ok:
            return True
        try:
            from services.zone_control import stop_all_in_group

            result = stop_all_in_group(
                int(group_id),
                reason="rain",
                force=True,
                master_close_immediately=True,
            )
            return result is not False
        except (ImportError, ConnectionError, TimeoutError, OSError, RuntimeError, sqlite3.Error):
            logger.exception("RainMonitor: fallback group stop failed group=%s", group_id)
            return False

    @staticmethod
    def _persist_rain_active(active: bool) -> bool:
        try:
            return db.set_setting_value("rain.active", "1" if active else "0") is True
        except (sqlite3.Error, OSError, AttributeError):
            logger.exception("RainMonitor: persisted rain state write failed")
            return False

    def _on_rain_start(self) -> bool:
        """Establish admission gate first, then cancel and stop active runs."""
        try:
            target_groups = _strict_target_groups()
        except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
            logger.exception("RainMonitor: strict group read failed; blocking all groups")
            with self._state_lock:
                self.is_rain = True
                self._block_all = True
            self._persist_rain_active(True)
            return False

        with self._state_lock:
            self.is_rain = True
            self._blocked_groups = set(target_groups)
            self._block_all = False
        persisted = self._persist_rain_active(True)

        postpone_until = datetime.now().strftime("%Y-%m-%d 23:59:59")
        all_safe = persisted
        for group_id in target_groups:
            all_safe = self._cancel_and_stop_group(group_id) and all_safe
            try:
                all_safe = _apply_rain_postpone_deadline(group_id, postpone_until) and all_safe
            except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
                logger.exception("RainMonitor: postpone failed group=%s", group_id)
                all_safe = False

        with contextlib.suppress(sqlite3.Error, OSError, AttributeError):
            db.add_log("rain_postpone", str({"groups": target_groups, "until": postpone_until}))
        return all_safe

    def _on_rain_stop(self) -> bool:
        """Clear rain-owned rows under CAS; open the gate only after success."""
        try:
            current_targets = set(_strict_target_groups())
        except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
            logger.exception("RainMonitor: dry edge group read failed; keeping rain gate closed")
            return False
        with self._state_lock:
            target_groups = sorted(current_targets | self._blocked_groups)

        for group_id in target_groups:
            if not _clear_rain_postpone(group_id):
                logger.error("RainMonitor: dry cleanup failed group=%s; keeping gate closed", group_id)
                return False
        if not self._persist_rain_active(False):
            return False

        with self._state_lock:
            self.is_rain = False
            self._blocked_groups.clear()
            self._block_all = False
        with contextlib.suppress(sqlite3.Error, OSError, AttributeError):
            db.add_log("rain_resume", str({"groups": target_groups}))
        return True

    def is_group_blocked(self, group_id: int) -> bool:
        group_id = int(group_id)
        if group_id == 999:
            return False
        self._load_persisted_state()
        with self._state_lock:
            if self.is_rain is not True:
                return False
            if self._block_all or group_id in self._blocked_groups:
                return True

        # Refresh on admission so groups enabled while rain is already active
        # cannot slip through until another sensor edge arrives.
        try:
            targets = set(_strict_target_groups())
        except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
            logger.exception("RainMonitor: admission group read failed; blocking all groups")
            with self._state_lock:
                self._block_all = True
            return True
        with self._state_lock:
            self._blocked_groups.update(targets)
            return group_id in self._blocked_groups


rain_monitor = RainMonitor()


def is_group_blocked(group_id: int) -> bool:
    """Scheduler admission contract: strict/fail-closed while rain is active."""
    return rain_monitor.is_group_blocked(group_id)


def start_rain_monitor():
    try:
        cfg = db.get_rain_config()
        if cfg:
            rain_monitor.reconfigure(cfg)
    except (sqlite3.Error, OSError, RuntimeError):
        logger.exception("start_rain_monitor failed")
