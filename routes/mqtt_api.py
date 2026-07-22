"""MQTT API blueprint — all /api/mqtt* endpoints (except zones-sse which is in zones_api)."""

import json
import logging
import math
import queue
import sqlite3
import ssl
import threading
import time
import uuid
from collections import deque
from functools import wraps

from flask import Blueprint, Response, jsonify, request, stream_with_context

from database import db
from services.audit import audit_log
from services.helpers import api_error
from services.security import admin_required
from utils import SecretDecryptionError, normalize_topic

logger = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
except ImportError as e:
    logger.debug("Exception in line_14: %s", e)
    mqtt = None

# Per-IP SSE connection tracking (P2: prevent connection flood)
_scan_sse_connections: dict[str, int] = {}  # {ip: active_count}
_scan_sse_lock = threading.Lock()
_mqtt_server_mutation_locks: dict[int, threading.RLock] = {}
_mqtt_server_mutation_locks_guard = threading.Lock()
MAX_SCAN_SSE_PER_IP = 2
MAX_PROBE_DURATION_SEC = 30.0
MAX_SCAN_DURATION_SEC = 300.0
MAX_DIAGNOSTIC_MESSAGES = 1000
MAX_DIAGNOSTIC_TOPIC_BYTES = 512
MAX_DIAGNOSTIC_PAYLOAD_BYTES = 4096
MAX_DIAGNOSTIC_FILTER_BYTES = 512
MAX_SCAN_QUEUE_MESSAGES = 1000
MAX_PROBE_TOTAL_BYTES = 256 * 1024
MAX_SCAN_QUEUE_BYTES = 256 * 1024
_MQTT_RUNTIME_FIELDS = frozenset(
    {
        "host",
        "port",
        "username",
        "password",
        "client_id",
        "enabled",
        "tls_enabled",
        "tls_ca_path",
        "tls_cert_path",
        "tls_key_path",
        "tls_insecure",
        "tls_version",
    }
)
_RAIN_ONLY_SETTINGS_SCOPE = frozenset({"rain.server_id"})

mqtt_api_bp = Blueprint("mqtt_api", __name__)


def _serialize_mqtt_server_mutation(fn):
    """Keep DB commit, staged runtime swap and rollback ordered per broker."""

    @wraps(fn)
    def wrapped(server_id, *args, **kwargs):
        with _mqtt_server_mutation_locks_guard:
            lock = _mqtt_server_mutation_locks.setdefault(int(server_id), threading.RLock())
        with lock:
            return fn(server_id, *args, **kwargs)

    return wrapped


def _mask_server_secret(server: dict | None) -> dict | None:
    if server and server.get("password"):
        server["password"] = "***"
    return server


def _annotate_server_references(server: dict) -> dict:
    """Expose only nonsecret topology metadata needed by the settings UI."""
    references = _mqtt_server_references(int(server["id"]))
    server["references"] = references
    server["is_referenced"] = bool(references)
    return server


def _get_diagnostic_server(server_id: int) -> dict | None:
    """Load one server without collapsing database failure into not-found."""
    strict_loader = getattr(db, "get_mqtt_server_strict", None)
    if callable(strict_loader):
        return strict_loader(int(server_id))
    return db.get_mqtt_server(int(server_id))


def _validated_diagnostic_filter(value, default: str) -> str:
    if value in (None, ""):
        return default
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("invalid MQTT filter")
    if len(value.encode("utf-8")) > MAX_DIAGNOSTIC_FILTER_BYTES:
        raise ValueError("MQTT filter is too long")
    return value


def _bounded_utf8(value, max_bytes: int) -> tuple[str, bool]:
    if isinstance(value, bytes):
        raw = value
    else:
        raw = str(value or "").encode("utf-8", errors="replace")
    truncated = len(raw) > max_bytes
    return raw[:max_bytes].decode("utf-8", errors="ignore"), truncated


def _bounded_mqtt_item(topic, payload, *, normalize: bool = False) -> dict:
    # Bound raw broker-controlled values before decode/normalisation.  A huge
    # retained payload must never be decoded or copied into an unbounded SSE
    # frame first and truncated afterwards.
    bounded_topic, topic_truncated = _bounded_utf8(topic, MAX_DIAGNOSTIC_TOPIC_BYTES)
    if normalize:
        bounded_topic = normalize_topic(bounded_topic)
        bounded_topic, normalized_truncated = _bounded_utf8(bounded_topic, MAX_DIAGNOSTIC_TOPIC_BYTES)
        topic_truncated = topic_truncated or normalized_truncated
    bounded_payload, payload_truncated = _bounded_utf8(payload, MAX_DIAGNOSTIC_PAYLOAD_BYTES)
    return {
        "topic": bounded_topic,
        "payload": bounded_payload,
        "truncated": topic_truncated or payload_truncated,
    }


def _mqtt_item_size(item: dict) -> int:
    # Flask's default JSON provider escapes non-ASCII.  Account for that
    # representation so Unicode payloads cannot exceed the response budget.
    return len(json.dumps(item, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) + 1


def _append_probe_message(received: list[dict], message) -> bool:
    if len(received) >= MAX_DIAGNOSTIC_MESSAGES:
        return False
    item = _bounded_mqtt_item(getattr(message, "topic", ""), getattr(message, "payload", b""))
    used_bytes = sum(_mqtt_item_size(existing) for existing in received)
    if used_bytes + _mqtt_item_size(item) > MAX_PROBE_TOTAL_BYTES:
        return False
    received.append(item)
    return True


class _BoundedSseBuffer:
    """Thread-safe frame buffer bounded by both item count and UTF-8 bytes."""

    def __init__(self, max_frames: int, max_bytes: int):
        self._max_frames = max_frames
        self._max_bytes = max_bytes
        self._frames = deque()
        self._bytes = 0
        self._dropped = 0
        self._condition = threading.Condition()

    @property
    def queued_bytes(self) -> int:
        with self._condition:
            return self._bytes

    def put_nowait(self, frame: str, *, control: bool = False) -> bool:
        size = len(frame.encode("utf-8"))
        with self._condition:
            if size > self._max_bytes:
                self._dropped += 1
                self._condition.notify()
                return False
            if control:
                while self._frames and (len(self._frames) >= self._max_frames or self._bytes + size > self._max_bytes):
                    _old_frame, old_size = self._frames.popleft()
                    self._bytes -= old_size
                    self._dropped += 1
            elif len(self._frames) >= self._max_frames or self._bytes + size > self._max_bytes:
                self._dropped += 1
                self._condition.notify()
                return False
            self._frames.append((frame, size))
            self._bytes += size
            self._condition.notify()
            return True

    def get(self, timeout: float | None = None) -> str:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while not self._frames and not self._dropped:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            if self._dropped:
                dropped = self._dropped
                self._dropped = 0
                payload = json.dumps({"success": False, "dropped": dropped}, separators=(",", ":"))
                return f"event: overflow\ndata: {payload}\n\n"
            frame, size = self._frames.popleft()
            self._bytes -= size
            return frame

    def empty(self) -> bool:
        with self._condition:
            return not self._frames and not self._dropped


def _secret_unavailable_response():
    return api_error(
        "MQTT_SECRET_UNAVAILABLE",
        "MQTT credentials cannot be decrypted; restore the configured secret key",
        500,
    )


def _refresh_mqtt_runtime(server_id: int) -> None:
    """Apply MQTT CRUD changes to both long-lived runtime consumers."""
    try:
        from services.mqtt_pub import invalidate_mqtt_server

        invalidate_mqtt_server(server_id)
    except Exception:
        logger.exception("MQTT publisher invalidation failed for server %s", server_id)
    try:
        from services import sse_hub

        sse_hub.reload_hub()
    except Exception:
        logger.exception("MQTT SSE hub reload failed for server %s", server_id)


def _reconfigure_rain_monitor(config: dict) -> bool:
    """Stage/swap the enabled rain subscriber; False preserves old runtime."""
    try:
        from services.monitors import rain_monitor

        reconfigure = getattr(rain_monitor, "reconfigure", None)
        if not callable(reconfigure):
            logger.error("RainMonitor does not expose staged reconfigure")
            return False
        return reconfigure(config) is True
    except (ConnectionError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        logger.exception("RainMonitor staged reconfiguration failed after server mutation")
        return False


def _mqtt_server_references(server_id: int) -> dict[str, list]:
    """Return hardware mappings that must be migrated before server mutation."""
    repository_helper = getattr(db, "get_mqtt_server_references", None)
    if callable(repository_helper):
        references = repository_helper(int(server_id)) or {}
        if isinstance(references, dict):
            return {str(kind): list(items) for kind, items in references.items() if items}

    references: dict[str, list] = {}
    for zone in db.get_zones() or []:
        try:
            if zone.get("mqtt_server_id") is not None and int(zone["mqtt_server_id"]) == int(server_id):
                references.setdefault("zones", []).append(int(zone["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    group_fields = (
        ("master_mqtt_server_id", "groups_master"),
        ("pressure_mqtt_server_id", "groups_pressure"),
        ("water_mqtt_server_id", "groups_water"),
        ("float_mqtt_server_id", "groups_float"),
    )
    for group in db.get_groups() or []:
        for field, role in group_fields:
            try:
                if group.get(field) is not None and int(group[field]) == int(server_id):
                    references.setdefault(role, []).append(int(group["id"]))
            except (KeyError, TypeError, ValueError):
                continue
    settings_refs = []
    try:
        rain = db.get_rain_config() or {}
        if rain.get("server_id") is not None and int(rain["server_id"]) == int(server_id):
            settings_refs.append("rain.server_id")
        master = db.get_master_config() or {}
        if master.get("server_id") is not None and int(master["server_id"]) == int(server_id):
            settings_refs.append("master.server_id")
        env = db.get_env_config() or {}
        for sensor in ("temp", "hum"):
            sensor_config = env.get(sensor) or {}
            if sensor_config.get("server_id") is not None and int(sensor_config["server_id"]) == int(server_id):
                settings_refs.append(f"env.{sensor}.server_id")
    except (AttributeError, KeyError, TypeError, ValueError):
        logger.exception("MQTT settings reference scan failed for server %s", server_id)
        # Fail closed: an incomplete scan is itself a blocking reference.
        settings_refs.append("reference_scan_unavailable")
    if settings_refs:
        references["settings"] = settings_refs
    return references


def _runtime_fields_changed(data: dict, current: dict) -> set[str]:
    """Return real runtime deltas after applying repository conversions."""
    changed = set()
    for field in set(data) & _MQTT_RUNTIME_FIELDS:
        value = data[field]
        if field == "password":
            if value in (None, "***"):
                continue
            target = value or None
            current_value = current.get(field) or None
        elif field == "port":
            if isinstance(value, bool):
                raise ValueError("port must be an integer")
            target = int(value)
            current_value = int(current.get(field) or 0)
        elif field in {"enabled", "tls_enabled", "tls_insecure"}:
            target = 1 if value else 0
            current_value = 1 if current.get(field) else 0
        else:
            target = value
            current_value = current.get(field)
        if target != current_value:
            changed.add(field)
    return changed


def _get_mqtt_servers_strict() -> list[dict]:
    loader = getattr(db, "get_mqtt_servers_strict", None)
    return loader() if callable(loader) else db.get_mqtt_servers()


def _get_mqtt_server_strict(server_id: int) -> dict | None:
    loader = getattr(db, "get_mqtt_server_strict", None)
    return loader(int(server_id)) if callable(loader) else db.get_mqtt_server(int(server_id))


def _new_diagnostic_client(server: dict, purpose: str):
    """Create and configure a temporary client without touching a live session."""
    purpose_slug = "".join(ch for ch in purpose.lower() if ch.isalnum())[:6] or "diag"
    client_id = f"wb-{purpose_slug}-{uuid.uuid4().hex[:10]}"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    if server.get("username"):
        client.username_pw_set(server.get("username"), server.get("password") or None)
    if int(server.get("tls_enabled") or 0) == 1:
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
        except KeyError as e:
            raise ValueError("Unsupported MQTT TLS version") from e
        client.tls_set(
            ca_certs=server.get("tls_ca_path") or None,
            certfile=server.get("tls_cert_path") or None,
            keyfile=server.get("tls_key_path") or None,
            tls_version=tls_version,
        )
        if int(server.get("tls_insecure") or 0) == 1:
            client.tls_insecure_set(True)
    return client


def _mock_diagnostics_in_tests() -> bool:
    from flask import current_app

    return bool(current_app.config.get("TESTING")) and not bool(
        current_app.config.get("MQTT_DIAGNOSTICS_LIVE_IN_TESTS")
    )


def _mqtt_reason_code_success(reason_code) -> bool:
    if bool(getattr(reason_code, "is_failure", False)):
        return False
    try:
        return int(reason_code) == 0
    except (TypeError, ValueError):
        return str(reason_code).strip().lower() in {"0", "success"}


# ===== MQTT Servers CRUD =====


@mqtt_api_bp.route("/api/mqtt/servers", methods=["GET"])
@admin_required
def api_mqtt_servers_list():
    try:
        servers = _get_mqtt_servers_strict()
        for s in servers or []:
            _mask_server_secret(s)
            _annotate_server_references(s)
        return jsonify({"success": True, "servers": servers})
    except SecretDecryptionError:
        logger.error("MQTT server list unavailable: stored credentials cannot be decrypted")
        return _secret_unavailable_response()
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error) as e:
        logger.error(f"Ошибка получения MQTT серверов: {e}")
        return jsonify({"success": False, "message": "Ошибка получения списка"}), 500


@mqtt_api_bp.route("/api/mqtt/servers", methods=["POST"])
@admin_required
@audit_log(
    "mqtt_server_create",
    target_extractor=lambda *a, **kw: "mqtt_server",
    payload_filter=lambda p: {k: v for k, v in p.items() if k != "password"},
)
def api_mqtt_server_create():
    try:
        data = request.get_json() or {}
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "Некорректные данные сервера"}), 400
        server = db.create_mqtt_server(data)
        if not server:
            return jsonify({"success": False, "message": "Не удалось создать сервер"}), 400
        server_id = int(server["id"])
        _refresh_mqtt_runtime(server_id)
        _mask_server_secret(server)
        return jsonify({"success": True, "server": server}), 201
    except SecretDecryptionError:
        logger.error("MQTT server create response unavailable: stored credentials cannot be decrypted")
        return _secret_unavailable_response()
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error) as e:
        logger.error(f"Ошибка создания MQTT сервера: {e}")
        return jsonify({"success": False, "message": "Ошибка создания"}), 500


@mqtt_api_bp.route("/api/mqtt/servers/<int:server_id>", methods=["GET"])
@admin_required
def api_mqtt_server_get(server_id: int):
    try:
        server = _get_mqtt_server_strict(server_id)
        if not server:
            return jsonify({"success": False, "message": "Сервер не найден"}), 404
        _mask_server_secret(server)
        _annotate_server_references(server)
        return jsonify({"success": True, "server": server})
    except SecretDecryptionError:
        logger.error("MQTT server %s unavailable: stored credentials cannot be decrypted", server_id)
        return _secret_unavailable_response()
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error) as e:
        logger.error(f"Ошибка получения MQTT сервера {server_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка получения"}), 500


@mqtt_api_bp.route("/api/mqtt/servers/<int:server_id>", methods=["PUT"])
@admin_required
@_serialize_mqtt_server_mutation
@audit_log(
    "mqtt_server_update",
    target_extractor=lambda *a, **kw: f"mqtt_server:{kw.get('server_id', a[0] if a else '?')}",
    payload_filter=lambda p: {k: v for k, v in p.items() if k != "password"},
)
def api_mqtt_server_update(server_id: int):
    try:
        data = request.get_json() or {}
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "Некорректные данные сервера"}), 400
        current = _get_mqtt_server_strict(server_id)
        if not current:
            return jsonify({"success": False, "message": "Сервер не найден"}), 404
        references = _mqtt_server_references(server_id)
        try:
            runtime_fields = _runtime_fields_changed(data, current)
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Некорректные runtime-поля MQTT-сервера"}), 400
        if runtime_fields:
            from services.monitors import rain_config_transaction_lock

            with rain_config_transaction_lock():
                guarded = db.update_mqtt_server_reference_guarded(
                    server_id,
                    data,
                    allowed_settings=_RAIN_ONLY_SETTINGS_SCOPE,
                )
                guarded_status = guarded.get("status") if isinstance(guarded, dict) else None
                if guarded_status == "blocked":
                    actual_references = guarded.get("references") or references
                    return jsonify(
                        {
                            "success": False,
                            "message": "Сервер используется оборудованием; сначала перенесите ссылки на другой сервер",
                            "references": actual_references,
                            "blocked_fields": sorted(runtime_fields),
                        }
                    ), 409
                if guarded_status != "updated":
                    return jsonify({"success": False, "message": "Не удалось обновить"}), 409

                rain_config = guarded.get("rain_config") or {}
                rain_is_effective = (
                    rain_config.get("enabled") is True
                    and rain_config.get("server_id") is not None
                    and int(rain_config["server_id"]) == int(server_id)
                )
                if rain_is_effective and not _reconfigure_rain_monitor(rain_config):
                    before_snapshot = guarded.get("before_snapshot")
                    committed_snapshot = guarded.get("snapshot")
                    rollback = (
                        db.restore_mqtt_server_snapshot_reference_guarded(
                            before_snapshot,
                            committed_snapshot,
                            allowed_settings=_RAIN_ONLY_SETTINGS_SCOPE,
                        )
                        if isinstance(before_snapshot, dict) and isinstance(committed_snapshot, dict)
                        else {"restored": False, "references": {}}
                    )
                    if not isinstance(rollback, dict) or rollback.get("restored") is not True:
                        logger.critical(
                            "RainMonitor rollback CAS/reference guard failed for MQTT server %s: %s",
                            server_id,
                            rollback,
                        )
                        return jsonify(
                            {
                                "success": False,
                                "message": "Rain runtime не применён, безопасный rollback не выполнен",
                                "error_code": "RAIN_MONITOR_ROLLBACK_CONFLICT",
                                "references": rollback.get("references", {}) if isinstance(rollback, dict) else {},
                            }
                        ), 500
                    return jsonify(
                        {
                            "success": False,
                            "message": "RainMonitor не принял MQTT-конфигурацию; изменения отменены",
                            "error_code": "RAIN_MONITOR_RECONFIGURE_FAILED",
                        }
                    ), 409
                _refresh_mqtt_runtime(server_id)
        else:
            # Never write runtime keys merely because a stale preflight saw
            # them as unchanged.  A concurrent runtime rotation may already
            # have won; replaying a full settings form would otherwise revert
            # it without the reference guard or runtime refresh.
            cosmetic_data = {key: value for key, value in data.items() if key not in _MQTT_RUNTIME_FIELDS}
            if cosmetic_data:
                ok = db.update_mqtt_server(server_id, cosmetic_data)
                if not ok:
                    return jsonify({"success": False, "message": "Не удалось обновить"}), 400
        server = _get_mqtt_server_strict(server_id)
        _mask_server_secret(server)
        return jsonify({"success": True, "server": server})
    except SecretDecryptionError:
        logger.error("MQTT server %s updated but credentials cannot be decrypted", server_id)
        return _secret_unavailable_response()
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"Ошибка обновления MQTT сервера {server_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка обновления"}), 500


@mqtt_api_bp.route("/api/mqtt/servers/<int:server_id>", methods=["DELETE"])
@admin_required
@_serialize_mqtt_server_mutation
@audit_log(
    "mqtt_server_delete", target_extractor=lambda *a, **kw: f"mqtt_server:{kw.get('server_id', a[0] if a else '?')}"
)
def api_mqtt_server_delete(server_id: int):
    try:
        server = _get_mqtt_server_strict(server_id)
        if not server:
            return jsonify({"success": False, "message": "Сервер не найден"}), 404
        references = _mqtt_server_references(server_id)
        if references:
            return jsonify(
                {
                    "success": False,
                    "message": "Сервер используется оборудованием; сначала перенесите ссылки на другой сервер",
                    "references": references,
                }
            ), 409
        ok = db.delete_mqtt_server(server_id)
        if not ok:
            return jsonify({"success": False, "message": "Не удалось удалить"}), 400
        _refresh_mqtt_runtime(server_id)
        return ("", 204)
    except SecretDecryptionError:
        logger.error("MQTT server %s delete failed: stored credentials cannot be decrypted", server_id)
        return _secret_unavailable_response()
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"Ошибка удаления MQTT сервера {server_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка удаления"}), 500


# ===== MQTT Probe =====


@mqtt_api_bp.route("/api/mqtt/<int:server_id>/probe", methods=["POST"])
@admin_required
@audit_log(
    "mqtt_server_probe", target_extractor=lambda *a, **kw: f"mqtt_server:{kw.get('server_id', a[0] if a else '?')}"
)
def api_mqtt_probe(server_id: int):
    try:
        server = _get_diagnostic_server(server_id)
        if not server:
            return api_error("MQTT_SERVER_NOT_FOUND", "server not found", 404, {"items": [], "events": []})
        if mqtt is None:
            return api_error("PAHO_NOT_INSTALLED", "paho-mqtt not installed", 500, {"items": [], "events": []})

        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return api_error("INVALID_REQUEST", "request body must be an object", 400)
        try:
            duration = float(data.get("duration", 3))
        except (TypeError, ValueError):
            return api_error("INVALID_DURATION", "duration must be a finite number between 0 and 30", 400)
        if not math.isfinite(duration) or duration <= 0 or duration > MAX_PROBE_DURATION_SEC:
            return api_error("INVALID_DURATION", "duration must be a finite number between 0 and 30", 400)
        try:
            topic_filter = _validated_diagnostic_filter(data.get("filter"), "#")
        except ValueError:
            return api_error("INVALID_FILTER", "MQTT filter must be a string of at most 512 bytes", 400)

        # Return mock data in tests
        if _mock_diagnostics_in_tests():
            return jsonify(
                {
                    "success": True,
                    "items": [{"topic": "/test/topic", "payload": "test_value"}],
                    "events": ["test: mocked probe response"],
                }
            )

        received = []
        probe_limit_reached = threading.Event()
        connected = threading.Event()
        connection_ok = threading.Event()
        events = [
            f"probe: connecting to {server.get('host')}:{server.get('port')} filter={topic_filter} duration={duration}s"
        ]
        client = _new_diagnostic_client(server, "probe")

        def on_connect(cl, userdata, flags, reason_code, properties=None):
            try:
                if _mqtt_reason_code_success(reason_code):
                    cl.subscribe(topic_filter, qos=0)
                    connection_ok.set()
                    events.append(f"connected rc={reason_code}, subscribed to {topic_filter}")
                else:
                    events.append("broker rejected connection")
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Exception in on_connect: %s", e)
                events.append("subscribe failed")
            finally:
                connected.set()

        def on_message(cl, userdata, msg):
            if not _append_probe_message(received, msg):
                probe_limit_reached.set()

        client.on_connect = on_connect
        client.on_message = on_message
        try:
            client.connect(server.get("host") or "127.0.0.1", int(server.get("port") or 1883), 5)
        except (ConnectionError, TimeoutError, OSError) as ce:
            logger.info("MQTT probe connect failed for server %s: %s", server_id, ce)
            events.append("connect failed")
            return api_error("MQTT_CONNECT_FAILED", "connect failed", 502, {"items": [], "events": events})
        client.loop_start()
        if not connected.wait(5.0) or not connection_ok.is_set():
            client.loop_stop()
            try:
                client.disconnect()
            except (ConnectionError, TimeoutError, OSError):
                pass
            return api_error("MQTT_CONNECT_FAILED", "connect failed", 502, {"items": [], "events": events})
        start = time.monotonic()
        while (
            time.monotonic() - start < duration
            and len(received) < MAX_DIAGNOSTIC_MESSAGES
            and not probe_limit_reached.is_set()
        ):
            time.sleep(0.1)
        client.loop_stop()
        try:
            client.disconnect()
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in line_142: %s", e)
        if not received:
            events.append("no messages received")
        if probe_limit_reached.is_set():
            events.append("probe result limit reached; remaining messages dropped")
        return jsonify(
            {
                "success": True,
                "items": received,
                "events": events,
                "truncated": probe_limit_reached.is_set(),
            }
        )
    except SecretDecryptionError:
        logger.error("MQTT probe unavailable: stored credentials cannot be decrypted")
        return _secret_unavailable_response()
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error) as e:
        logger.error(f"MQTT probe error: {e}")
        return api_error("PROBE_FAILED", "probe failed", 500, {"items": [], "events": []})


# ===== MQTT Status =====


@mqtt_api_bp.route("/api/mqtt/<int:server_id>/status", methods=["GET"])
@admin_required
def api_mqtt_status(server_id: int):
    try:
        server = _get_diagnostic_server(server_id)
        if not server:
            return api_error("MQTT_SERVER_NOT_FOUND", "server not found", 404, {"connected": False})
        if mqtt is None:
            return api_error("PAHO_NOT_INSTALLED", "paho-mqtt not installed", 500, {"connected": False})

        # Skip real MQTT connection test in tests
        if _mock_diagnostics_in_tests():
            return jsonify({"success": True, "connected": True})

        connected = threading.Event()
        connection_ok = threading.Event()
        client = _new_diagnostic_client(server, "status")

        def on_connect(cl, userdata, flags, reason_code, properties=None):
            if _mqtt_reason_code_success(reason_code):
                connection_ok.set()
            connected.set()

        client.on_connect = on_connect
        loop_started = False
        try:
            client.connect(server.get("host") or "127.0.0.1", int(server.get("port") or 1883), 3)
            client.loop_start()
            loop_started = True
            if not connected.wait(3.0) or not connection_ok.is_set():
                return api_error("MQTT_CONNECT_FAILED", "connect failed", 502, {"connected": False})
            return jsonify({"success": True, "connected": True})
        except (ConnectionError, TimeoutError, OSError) as _e:
            logger.info(f"MQTT status connection failed for server {server_id}: {_e}")
            return api_error("MQTT_CONNECT_FAILED", "connect failed", 502, {"connected": False})
        finally:
            if loop_started:
                try:
                    client.loop_stop()
                except (ConnectionError, TimeoutError, OSError):
                    pass
            try:
                client.disconnect()
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Handled exception in api_mqtt_status: %s", e)
    except SecretDecryptionError:
        logger.error("MQTT status unavailable: stored credentials cannot be decrypted")
        return _secret_unavailable_response()
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error) as e:
        logger.error(f"MQTT status error: {e}")
        return api_error("STATUS_FAILED", "status failed", 500, {"connected": False})


# ===== MQTT Scan SSE =====


@mqtt_api_bp.route("/api/mqtt/<int:server_id>/scan-sse")
@admin_required
def api_mqtt_scan_sse(server_id: int):
    """Stream MQTT messages as SSE for continuous scanning."""
    ip = request.remote_addr or "0.0.0.0"
    try:
        # Per-IP connection limit
        with _scan_sse_lock:
            current = _scan_sse_connections.get(ip, 0)
            if current >= MAX_SCAN_SSE_PER_IP:
                return api_error("SSE_LIMIT", "Too many SSE connections", 429)
            _scan_sse_connections[ip] = current + 1

        def _decrement_sse(ip_addr: str):
            with _scan_sse_lock:
                _scan_sse_connections[ip_addr] = max(0, _scan_sse_connections.get(ip_addr, 1) - 1)
                if _scan_sse_connections[ip_addr] == 0:
                    _scan_sse_connections.pop(ip_addr, None)

        server = _get_diagnostic_server(server_id)
        if not server:
            _decrement_sse(ip)
            return api_error("MQTT_SERVER_NOT_FOUND", "server not found", 404)
        if mqtt is None:
            _decrement_sse(ip)
            return api_error("MQTT_LIB_MISSING", "paho-mqtt not installed", 500)
        try:
            sub_filter = _validated_diagnostic_filter(request.args.get("filter"), "/devices/#")
        except ValueError:
            _decrement_sse(ip)
            return api_error("INVALID_FILTER", "MQTT filter must be at most 512 bytes", 400)

        # Return mock SSE in tests
        if _mock_diagnostics_in_tests():

            def mock_gen():
                try:
                    yield "event: open\n" + 'data: {"success": true}\n\n'
                    yield 'data: {"topic": "/test/mock", "payload": "test"}\n\n'
                finally:
                    _decrement_sse(ip)

            return Response(mock_gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})

        msg_queue = _BoundedSseBuffer(MAX_SCAN_QUEUE_MESSAGES, MAX_SCAN_QUEUE_BYTES)
        stop_event = threading.Event()
        stream_done = threading.Event()

        def _queue_frame(frame: str) -> None:
            if not msg_queue.put_nowait(frame, control=True):
                logger.warning("scan-sse queue full; dropping control frame")

        def _run_client():
            client = None
            loop_started = False
            try:
                client = _new_diagnostic_client(server, "scan")
                connected = threading.Event()
                connection_ok = threading.Event()

                def on_connect(cl, userdata, flags, reason_code, properties=None):
                    try:
                        if _mqtt_reason_code_success(reason_code):
                            cl.subscribe(sub_filter, qos=0)
                            connection_ok.set()
                    except (ConnectionError, TimeoutError, OSError) as e:
                        logger.debug("Handled exception in on_connect: %s", e)
                    finally:
                        connected.set()

                def on_message(cl, userdata, msg):
                    data = json.dumps(
                        _bounded_mqtt_item(
                            getattr(msg, "topic", ""),
                            getattr(msg, "payload", b""),
                            normalize=True,
                        )
                    )
                    if not msg_queue.put_nowait(f"data: {data}\n\n"):
                        logger.debug("scan-sse msg_queue full, dropping message")

                client.on_connect = on_connect
                client.on_message = on_message
                client.connect(server.get("host") or "127.0.0.1", int(server.get("port") or 1883), 5)
                client.loop_start()
                loop_started = True
                if not connected.wait(5.0) or not connection_ok.is_set():
                    raise ConnectionError("broker connection was not confirmed")
                _start_ts = time.monotonic()
                while not stop_event.is_set():
                    stop_event.wait(0.2)
                    if time.monotonic() - _start_ts >= MAX_SCAN_DURATION_SEC:
                        _queue_frame("event: end\n" + 'data: {"success": true, "reason": "scan_timeout"}\n\n')
                        break
            except (ConnectionError, TimeoutError, OSError, ValueError):
                logger.exception("MQTT scan connection failed for server %s", server_id)
                payload = json.dumps(
                    {
                        "success": False,
                        "error_code": "MQTT_CONNECT_FAILED",
                        "message": "connect failed",
                    }
                )
                _queue_frame(f"event: error\ndata: {payload}\n\n")
            except Exception:
                logger.exception("MQTT scan worker failed for server %s", server_id)
                payload = json.dumps(
                    {
                        "success": False,
                        "error_code": "MQTT_SCAN_FAILED",
                        "message": "scan failed",
                    }
                )
                _queue_frame(f"event: error\ndata: {payload}\n\n")
            finally:
                if client is not None and loop_started:
                    try:
                        client.loop_stop()
                    except (ConnectionError, TimeoutError, OSError) as e:
                        logger.debug("scan-sse loop_stop failed: %s", e)
                try:
                    if client is not None:
                        client.disconnect()
                except (ConnectionError, TimeoutError, OSError) as e:
                    logger.debug("scan-sse disconnect failed: %s", e)
                stream_done.set()

        th = threading.Thread(target=_run_client, daemon=True)
        th.start()

        @stream_with_context
        def _gen():
            try:
                yield "event: open\n" + 'data: {"success": true}\n\n'
                last_ping = 0

                while True:
                    if stream_done.is_set() and msg_queue.empty():
                        break
                    try:
                        frame = msg_queue.get(timeout=0.5)
                        yield frame
                    except queue.Empty:
                        if stream_done.is_set():
                            break
                    now = int(time.monotonic())
                    if not stream_done.is_set() and now != last_ping:
                        last_ping = now
                        yield "event: ping\n" + "data: {}\n\n"
            finally:
                stop_event.set()
                _decrement_sse(ip)

        return Response(
            _gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )
    except SecretDecryptionError:
        logger.error("MQTT scan unavailable: stored credentials cannot be decrypted")
        _decrement_sse(ip)
        return _secret_unavailable_response()
    except (RuntimeError, OSError, sqlite3.Error) as e:
        logger.error(f"MQTT scan SSE error: {e}")
        _decrement_sse(ip)
        return api_error("SSE_FAILED", "sse init failed", 500)
