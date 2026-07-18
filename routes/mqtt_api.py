"""MQTT API blueprint — all /api/mqtt* endpoints (except zones-sse which is in zones_api)."""

import json
import logging
import math
import queue
import ssl
import threading
import uuid

from flask import Blueprint, Response, jsonify, request, stream_with_context

from database import db
from services.audit import audit_log
from services.helpers import api_error, api_soft
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
MAX_SCAN_SSE_PER_IP = 2
MAX_PROBE_DURATION_SEC = 30.0
MAX_SCAN_DURATION_SEC = 300.0

mqtt_api_bp = Blueprint("mqtt_api", __name__)


def _mask_server_secret(server: dict | None) -> dict | None:
    if server and server.get("password"):
        server["password"] = "***"
    return server


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


# ===== MQTT Servers CRUD =====


@mqtt_api_bp.route("/api/mqtt/servers", methods=["GET"])
@admin_required
def api_mqtt_servers_list():
    try:
        servers = db.get_mqtt_servers()
        for s in servers or []:
            _mask_server_secret(s)
        return jsonify({"success": True, "servers": servers})
    except SecretDecryptionError:
        logger.error("MQTT server list unavailable: stored credentials cannot be decrypted")
        return _secret_unavailable_response()
    except (ConnectionError, TimeoutError, OSError) as e:
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
        server = db.create_mqtt_server(data)
        if not server:
            return jsonify({"success": False, "message": "Не удалось создать сервер"}), 400
        _mask_server_secret(server)
        return jsonify({"success": True, "server": server}), 201
    except SecretDecryptionError:
        logger.error("MQTT server create response unavailable: stored credentials cannot be decrypted")
        return _secret_unavailable_response()
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"Ошибка создания MQTT сервера: {e}")
        return jsonify({"success": False, "message": "Ошибка создания"}), 500


@mqtt_api_bp.route("/api/mqtt/servers/<int:server_id>", methods=["GET"])
@admin_required
def api_mqtt_server_get(server_id: int):
    try:
        server = db.get_mqtt_server(server_id)
        if not server:
            return jsonify({"success": False, "message": "Сервер не найден"}), 404
        _mask_server_secret(server)
        return jsonify({"success": True, "server": server})
    except SecretDecryptionError:
        logger.error("MQTT server %s unavailable: stored credentials cannot be decrypted", server_id)
        return _secret_unavailable_response()
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"Ошибка получения MQTT сервера {server_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка получения"}), 500


@mqtt_api_bp.route("/api/mqtt/servers/<int:server_id>", methods=["PUT"])
@admin_required
@audit_log(
    "mqtt_server_update",
    target_extractor=lambda *a, **kw: f"mqtt_server:{kw.get('server_id', a[0] if a else '?')}",
    payload_filter=lambda p: {k: v for k, v in p.items() if k != "password"},
)
def api_mqtt_server_update(server_id: int):
    try:
        data = request.get_json() or {}
        ok = db.update_mqtt_server(server_id, data)
        if not ok:
            return jsonify({"success": False, "message": "Не удалось обновить"}), 400
        _refresh_mqtt_runtime(server_id)
        server = db.get_mqtt_server(server_id)
        _mask_server_secret(server)
        return jsonify({"success": True, "server": server})
    except SecretDecryptionError:
        logger.error("MQTT server %s updated but credentials cannot be decrypted", server_id)
        return _secret_unavailable_response()
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"Ошибка обновления MQTT сервера {server_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка обновления"}), 500


@mqtt_api_bp.route("/api/mqtt/servers/<int:server_id>", methods=["DELETE"])
@admin_required
@audit_log(
    "mqtt_server_delete", target_extractor=lambda *a, **kw: f"mqtt_server:{kw.get('server_id', a[0] if a else '?')}"
)
def api_mqtt_server_delete(server_id: int):
    try:
        ok = db.delete_mqtt_server(server_id)
        if not ok:
            return jsonify({"success": False, "message": "Не удалось удалить"}), 400
        _refresh_mqtt_runtime(server_id)
        return ("", 204)
    except SecretDecryptionError:
        logger.error("MQTT server %s delete failed: stored credentials cannot be decrypted", server_id)
        return _secret_unavailable_response()
    except (ConnectionError, TimeoutError, OSError) as e:
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
        from flask import current_app

        server = db.get_mqtt_server(server_id)
        if not server:
            return api_soft("MQTT_SERVER_NOT_FOUND", "server not found", {"items": [], "events": []})
        if mqtt is None:
            return api_soft("PAHO_NOT_INSTALLED", "paho-mqtt not installed", {"items": [], "events": []})

        data = request.get_json(silent=True) or {}
        try:
            duration = float(data.get("duration", 3))
        except (TypeError, ValueError):
            return api_error("INVALID_DURATION", "duration must be a finite number between 0 and 30", 400)
        if not math.isfinite(duration) or duration <= 0 or duration > MAX_PROBE_DURATION_SEC:
            return api_error("INVALID_DURATION", "duration must be a finite number between 0 and 30", 400)

        # Return mock data in tests
        if current_app.config.get("TESTING"):
            return jsonify(
                {
                    "success": True,
                    "items": [{"topic": "/test/topic", "payload": "test_value"}],
                    "events": ["test: mocked probe response"],
                }
            )

        topic_filter = data.get("filter", "#")

        received = []
        events = [
            f"probe: connecting to {server.get('host')}:{server.get('port')} filter={topic_filter} duration={duration}s"
        ]
        client = _new_diagnostic_client(server, "probe")

        def on_connect(cl, userdata, flags, reason_code, properties=None):
            try:
                cl.subscribe(topic_filter, qos=0)
                events.append(f"connected rc={reason_code}, subscribed to {topic_filter}")
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Exception in on_connect: %s", e)
                events.append("subscribe failed")

        def on_message(cl, userdata, msg):
            try:
                topic = msg.topic
            except (AttributeError, ValueError) as e:
                logger.debug("Exception in on_message: %s", e)
                topic = getattr(msg, "topic", "")
            if len(received) < 1000:
                try:
                    payload = msg.payload.decode("utf-8", errors="ignore")
                except (UnicodeDecodeError, AttributeError) as e:
                    logger.debug("Exception in on_message: %s", e)
                    payload = str(msg.payload)
                received.append({"topic": topic, "payload": payload})

        client.on_connect = on_connect
        client.on_message = on_message
        try:
            client.connect(server.get("host") or "127.0.0.1", int(server.get("port") or 1883), 5)
        except (ConnectionError, TimeoutError, OSError) as ce:
            logger.debug("Exception in on_message: %s", ce)
            events.append(f"connect error: {ce}")
            return api_soft("MQTT_CONNECT_FAILED", "connect failed", {"items": [], "events": events})
        client.loop_start()
        import time as _t

        start = _t.time()
        while _t.time() - start < duration and len(received) < 5000:
            _t.sleep(0.1)
        client.loop_stop()
        try:
            client.disconnect()
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in line_142: %s", e)
        if not received:
            events.append("no messages received")
        return jsonify({"success": True, "items": received, "events": events})
    except SecretDecryptionError:
        logger.error("MQTT probe unavailable: stored credentials cannot be decrypted")
        return _secret_unavailable_response()
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"MQTT probe error: {e}")
        return api_soft("PROBE_FAILED", "probe failed", {"items": [], "events": [str(e)]})


# ===== MQTT Status =====


@mqtt_api_bp.route("/api/mqtt/<int:server_id>/status", methods=["GET"])
@admin_required
def api_mqtt_status(server_id: int):
    try:
        from flask import current_app

        server = db.get_mqtt_server(server_id)
        if not server:
            return jsonify({"success": True, "connected": False, "message": "server not found"}), 200
        if mqtt is None:
            return jsonify({"success": True, "connected": False, "message": "paho-mqtt not installed"}), 200

        # Skip real MQTT connection test in tests
        if current_app.config.get("TESTING"):
            return jsonify({"success": True, "connected": True})

        ok = False
        client = _new_diagnostic_client(server, "status")
        try:
            client.connect(server.get("host") or "127.0.0.1", int(server.get("port") or 1883), 3)
            ok = True
            try:
                client.disconnect()
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Handled exception in api_mqtt_status: %s", e)
        except (ConnectionError, TimeoutError, OSError) as _e:
            logger.info(f"MQTT status connection failed for server {server_id}: {_e}")
            ok = False
        return jsonify({"success": True, "connected": ok})
    except SecretDecryptionError:
        logger.error("MQTT status unavailable: stored credentials cannot be decrypted")
        return _secret_unavailable_response()
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"MQTT status error: {e}")
        return jsonify({"success": True, "connected": False, "message": "status failed"}), 200


# ===== MQTT Scan SSE =====


@mqtt_api_bp.route("/api/mqtt/<int:server_id>/scan-sse")
@admin_required
def api_mqtt_scan_sse(server_id: int):
    """Stream MQTT messages as SSE for continuous scanning."""
    ip = request.remote_addr or "0.0.0.0"
    try:
        from flask import current_app

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

        server = db.get_mqtt_server(server_id)
        if not server:
            _decrement_sse(ip)
            return api_error("MQTT_SERVER_NOT_FOUND", "server not found", 404)
        if mqtt is None:
            _decrement_sse(ip)
            return api_error("MQTT_LIB_MISSING", "paho-mqtt not installed", 500)

        # Return mock SSE in tests
        if current_app.config.get("TESTING"):

            def mock_gen():
                try:
                    yield "event: open\n" + 'data: {"success": true}\n\n'
                    yield 'data: {"topic": "/test/mock", "payload": "test"}\n\n'
                finally:
                    _decrement_sse(ip)

            return Response(mock_gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})

        sub_filter = request.args.get("filter", "/devices/#") or "/devices/#"
        msg_queue: queue.Queue[str] = queue.Queue(maxsize=10000)
        stop_event = threading.Event()
        stream_done = threading.Event()

        def _queue_frame(frame: str) -> None:
            try:
                msg_queue.put_nowait(frame)
            except queue.Full:
                logger.warning("scan-sse queue full; dropping control frame")

        def _run_client():
            client = None
            loop_started = False
            try:
                client = _new_diagnostic_client(server, "scan")

                def on_connect(cl, userdata, flags, reason_code, properties=None):
                    try:
                        cl.subscribe(sub_filter, qos=0)
                    except (ConnectionError, TimeoutError, OSError) as e:
                        logger.debug("Handled exception in on_connect: %s", e)

                def on_message(cl, userdata, msg):
                    try:
                        topic = msg.topic
                    except (AttributeError, ValueError) as e:
                        logger.debug("Exception in on_message: %s", e)
                        topic = getattr(msg, "topic", "")
                    try:
                        payload = msg.payload.decode("utf-8", errors="ignore")
                    except (UnicodeDecodeError, AttributeError) as e:
                        logger.debug("Exception in on_message: %s", e)
                        payload = str(msg.payload)
                    data = json.dumps({"topic": normalize_topic(topic), "payload": payload})
                    try:
                        msg_queue.put_nowait(f"data: {data}\n\n")
                    except queue.Full:
                        logger.debug("scan-sse msg_queue full, dropping message for topic %s", topic)

                client.on_connect = on_connect
                client.on_message = on_message
                client.connect(server.get("host") or "127.0.0.1", int(server.get("port") or 1883), 5)
                client.loop_start()
                loop_started = True
                import time as _t

                _start_ts = _t.time()
                while not stop_event.is_set():
                    stop_event.wait(0.2)
                    if _t.time() - _start_ts >= MAX_SCAN_DURATION_SEC:
                        _queue_frame(
                            "event: end\n"
                            + 'data: {"success": true, "reason": "scan_timeout"}\n\n'
                        )
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
                import time as _t

                while True:
                    if stream_done.is_set() and msg_queue.empty():
                        break
                    try:
                        frame = msg_queue.get(timeout=0.5)
                        yield frame
                    except queue.Empty:
                        if stream_done.is_set():
                            break
                    now = int(_t.time())
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
    except (RuntimeError, OSError) as e:
        logger.error(f"MQTT scan SSE error: {e}")
        _decrement_sse(ip)
        return api_error("SSE_FAILED", "sse init failed", 500)
