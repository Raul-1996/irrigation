import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
except ImportError as e:
    logger.debug("Exception in line_8: %s", e)
    mqtt = None

from utils import normalize_topic

try:
    from database import db as _db
except ImportError as e:
    logger.debug("Exception in line_15: %s", e)
    _db = None

# Audit helpers — local import on use to avoid circular at module load. We
# import lazily inside publish_mqtt_value to keep startup time clean.

# Caches and locks
_MQTT_CLIENTS: dict[int, object] = {}
_MQTT_CLIENTS_LOCK = threading.Lock()
_TOPIC_LAST_SEND: dict[tuple[int, str], tuple[str, float]] = {}
_TOPIC_LOCK = threading.Lock()
_SERVER_CACHE: dict[int, tuple[dict, float]] = {}
from constants import MQTT_CACHE_TTL_SEC

_SERVER_CACHE_TTL = float(MQTT_CACHE_TTL_SEC)


def get_or_create_mqtt_client(server: dict[str, Any]) -> Any | None:
    if mqtt is None:
        return None
    try:
        sid = int(server.get("id")) if server.get("id") is not None else 0
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Exception in get_or_create_mqtt_client: %s", e)
        sid = 0
    # Proactively evict a cached client that lost its broker connection.
    # Eviction (pop) is done under the lock, but teardown (loop_stop/disconnect,
    # which join the network thread and may block) runs OUTSIDE the lock so a
    # slow teardown can't serialise every other publisher — same pattern as
    # _invalidate_client.
    stale = None
    with _MQTT_CLIENTS_LOCK:
        cl = _MQTT_CLIENTS.get(sid)
        if cl is not None:
            try:
                if hasattr(cl, "is_connected") and not cl.is_connected():
                    _MQTT_CLIENTS.pop(sid, None)
                    stale, cl = cl, None
            except Exception as e:
                logger.debug("is_connected check sid=%s: %s", sid, e)
    if stale is not None:
        logger.warning("MQTT cached client sid=%s disconnected — recreating", sid)
        try:
            stale.loop_stop()
        except Exception as e:
            logger.debug("recreate loop_stop sid=%s: %s", sid, e)
        try:
            stale.disconnect()
        except Exception as e:
            logger.debug("recreate disconnect sid=%s: %s", sid, e)

    with _MQTT_CLIENTS_LOCK:
        cl = _MQTT_CLIENTS.get(sid)
        if cl is None:
            try:
                cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                if server.get("username"):
                    cl.username_pw_set(server.get("username"), server.get("password") or None)
                # TLS options (если включены)
                try:
                    if int(server.get("tls_enabled") or 0) == 1:
                        import ssl

                        ca = server.get("tls_ca_path") or None
                        cert = server.get("tls_cert_path") or None
                        key = server.get("tls_key_path") or None
                        tls_ver = (server.get("tls_version") or "").upper().strip()
                        version = ssl.PROTOCOL_TLS_CLIENT if tls_ver in ("", "TLS", "TLS_CLIENT") else ssl.PROTOCOL_TLS
                        cl.tls_set(ca_certs=ca, certfile=cert, keyfile=key, tls_version=version)
                        if int(server.get("tls_insecure") or 0) == 1:
                            cl.tls_insecure_set(True)
                except (ImportError, OSError, ValueError):
                    logger.exception("MQTT TLS setup failed for publisher")
                host = server.get("host") or "127.0.0.1"
                port = int(server.get("port") or 1883)
                try:
                    # быстрый авто-ре-коннект
                    try:
                        cl.reconnect_delay_set(min_delay=1, max_delay=5)
                    except (ConnectionError, TimeoutError, OSError) as e:
                        logger.debug("Handled exception in line_65: %s", e)
                    try:
                        cl.max_inflight_messages_set(100)
                    except (ValueError, AttributeError) as e:
                        logger.debug("Handled exception in line_69: %s", e)
                    # Синхронное подключение (надёжнее для тестов/первой публикации)
                    cl.connect(host, port, 10)
                    try:
                        cl.loop_start()
                    except (ConnectionError, TimeoutError, OSError):
                        # Promoted to logger.exception — silent debug masked
                        # cases where loop_start fails to spawn the network
                        # thread (very rare, but breaks all subsequent publishes).
                        logger.exception("MQTT loop_start failed sid=%s host=%s:%s", sid, host, port)
                except (ConnectionError, TimeoutError, OSError):
                    # Promoted to logger.exception — connection failures here
                    # silently dropped publishes during MASTER-C2 audit.
                    logger.exception("MQTT connect failed sid=%s host=%s:%s", sid, host, port)
                    # не кэшируем неудачное подключение
                    return None

                def _on_disconnect(c, u, rc, properties=None):
                    # оставляем клиента в кеше: loop_start и reconnect_delay_set обеспечат авто-переподключение
                    try:
                        logger.info("MQTT client disconnected sid=%s rc=%s (auto-reconnect active)", sid, rc)
                    except (ConnectionError, TimeoutError, OSError) as e:
                        logger.debug("Handled exception in _on_disconnect: %s", e)

                cl.on_disconnect = _on_disconnect
                _MQTT_CLIENTS[sid] = cl
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Exception in _on_disconnect: %s", e)
                return None
        return cl


def _publish_with_retries(cl: Any, topic: str, value: str, qos: int, retain: bool) -> bool:
    """Publish ``value`` to ``topic`` with connect-rc retries (up to 10x)
    and, for QoS≥1, broker-ack wait with backoff (up to 3x).

    Returns True on confirmed delivery, False otherwise. Used for both the
    base device topic and the Wirenboard ``/on`` command companion topic so
    that BOTH channels get the same delivery guarantee — without this
    symmetry the relay-command publish would silently drop on transient
    broker hiccups while the report-channel publish would succeed (Issue
    #38 root cause).
    """
    effective_qos = max(0, min(2, int(qos or 0)))
    try:
        attempts = 0
        rc = 0
        res = None
        while attempts < 10:
            attempts += 1
            res = cl.publish(topic, payload=value, qos=effective_qos, retain=retain)
            try:
                rc = getattr(res, "rc", 0)
            except (KeyError, TypeError, ValueError) as e:
                logger.debug("Exception reading publish rc: %s", e)
                rc = 0
            if rc == 0:
                break
            logger.warning(f"MQTT publish rc={rc}, retry {attempts} topic={topic}")
            try:
                cl.reconnect()
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Handled reconnect exception: %s", e)
            time.sleep(0.1)
        if attempts >= 10 and rc != 0:
            logger.error("MQTT publish failed after retries topic=%s", topic)
            if effective_qos >= 1:
                try:
                    from services.audit import record_audit

                    record_audit(
                        action_type="mqtt_publish_failure",
                        source="mqtt",
                        target=topic,
                        payload={
                            "value": value,
                            "qos": effective_qos,
                            "retain": bool(retain),
                            "rc": int(rc),
                            "reason": "connect_rc_retries_exhausted",
                            "attempts": attempts,
                        },
                        actor="system",
                        result="failure",
                        error=f"rc={rc} after {attempts} attempts",
                    )
                except Exception:
                    logger.exception("mqtt_publish_failure: record_audit failed")
            return False
        if effective_qos >= 1 and res is not None:
            backoff_delays = [1, 2, 4]
            published = False
            for retry_idx, delay in enumerate(backoff_delays):
                try:
                    res.wait_for_publish(timeout=5.0)
                    # wait_for_publish() returns silently on timeout when rc==0
                    # (message accepted into the queue but never sent — the
                    # wedged-inflight failure mode). Confirm actual delivery
                    # via is_published() instead of trusting "no exception".
                    if res.is_published():
                        published = True
                        break
                    logger.warning(
                        f"MQTT message not delivered — queued but unpublished "
                        f"(attempt {retry_idx + 1}/3) topic={topic}"
                    )
                except Exception as wfp_err:
                    logger.warning(
                        f"MQTT wait_for_publish failed (attempt {retry_idx + 1}/3) topic={topic}: {wfp_err}"
                    )
                # Not delivered (timeout or error) → backoff and republish.
                time.sleep(delay)
                try:
                    res = cl.publish(topic, payload=value, qos=effective_qos, retain=retain)
                except (ConnectionError, TimeoutError, OSError):
                    logger.exception("MQTT publish (QoS>=1 retry republish) failed topic=%s", topic)
            if not published:
                logger.critical(
                    f"MQTT QoS {effective_qos} delivery FAILED after 3 retries topic={topic} value={value}"
                )
                try:
                    from services.audit import record_audit

                    record_audit(
                        action_type="mqtt_publish_failure",
                        source="mqtt",
                        target=topic,
                        payload={
                            "value": value,
                            "qos": effective_qos,
                            "retain": bool(retain),
                            "reason": "wait_for_publish_retries_exhausted",
                        },
                        actor="system",
                        result="failure",
                        error=f"QoS{effective_qos} delivery failed after 3 retries",
                    )
                except Exception:
                    logger.exception("mqtt_publish_failure: record_audit failed")
                return False
        return True
    except (ConnectionError, TimeoutError, OSError):
        logger.exception("MQTT publish failed topic=%s", topic)
        return False


def _invalidate_client(sid: Any) -> None:
    """Drop a (likely wedged) cached MQTT client and tear it down.

    The next ``get_or_create_mqtt_client`` rebuilds a fresh client with an
    empty inflight window. This is the recovery path for the failure mode
    where QoS>=1 messages get queued-but-never-published after a
    mid-handshake disconnect leaves orphaned mids occupying the inflight
    window (mqtt-client-recovery).
    """
    try:
        key = int(sid) if sid is not None else 0
    except (ValueError, TypeError):
        key = 0
    with _MQTT_CLIENTS_LOCK:
        cl = _MQTT_CLIENTS.pop(key, None)
    if cl is not None:
        try:
            cl.loop_stop()
        except Exception as e:
            logger.debug("invalidate loop_stop sid=%s: %s", key, e)
        try:
            cl.disconnect()
        except Exception as e:
            logger.debug("invalidate disconnect sid=%s: %s", key, e)
        logger.info("MQTT client invalidated sid=%s (recreate on next publish)", key)


def _publish_one(server: dict, sid: Any, topic: str, value: str, qos: int, retain: bool) -> bool:
    """Publish one topic with self-healing.

    On delivery failure the cached client is most likely wedged (its inflight
    window is full of QoS>=1 messages that will never be acknowledged). We
    rebuild the client once and retry on a clean window. Without this, relay
    commands block indefinitely in ``wait_for_publish`` (mqtt-client-recovery).
    """
    cl = get_or_create_mqtt_client(server)
    if cl is None:
        logger.warning("MQTT publish: client unavailable, dropping topic=%s", topic)
        return False
    if _publish_with_retries(cl, topic, value, qos, retain):
        return True
    logger.warning("MQTT delivery failed — recreating client sid=%s topic=%s", sid, topic)
    _invalidate_client(sid)
    cl = get_or_create_mqtt_client(server)
    if cl is None:
        return False
    return _publish_with_retries(cl, topic, value, qos, retain)


def publish_mqtt_value(
    server: dict,
    topic: str,
    value: str,
    min_interval_sec: float = 0.2,
    retain: bool = False,
    meta: dict[str, str] | None = None,
    qos: int = 0,
) -> bool:
    try:
        t = normalize_topic(topic)
        sid = int(server.get("id")) if server.get("id") else None
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
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Handled exception in publish_mqtt_value: %s", e)
        key = (sid or 0, t)
        now = time.time()
        with _TOPIC_LOCK:
            last = _TOPIC_LAST_SEND.get(key)
            if last and last[0] == value and (now - last[1]) < min_interval_sec:
                logger.debug(f"MQTT skip duplicate topic={t} value={value}")
                return True
            _TOPIC_LAST_SEND[key] = (value, now)
        logger.debug(f"MQTT publish topic={t} value={value}")
        # Publish to base topic with retries + (QoS≥1) broker-ack wait, and
        # self-heal a wedged client (recreate + retry) on delivery failure.
        effective_qos = max(0, min(2, int(qos or 0)))
        if not _publish_one(server, sid, t, value, effective_qos, retain):
            return False

        # Debug-level audit: every successful publish. Volume is high
        # (Wirenboard publishes can be hundreds per hour) — gated behind
        # `settings.logging.debug` so audit_log doesn't blow up in normal use.
        try:
            from services.audit import debug_audit

            debug_audit(
                action_type="mqtt_publish",
                source="mqtt",
                target=t,
                payload={
                    "value": value,
                    "qos": effective_qos,
                    "retain": bool(retain),
                    "meta": meta if isinstance(meta, dict) else None,
                },
            )
        except Exception:
            logger.debug("debug_audit(mqtt_publish) failed", exc_info=True)

        # Also publish to the control topic '/on' for Wirenboard compatibility.
        # Issue #38: the base topic is the *report* channel; the relay only
        # reacts to '/on'. Previously this publish was fire-and-forget — a
        # transient broker hiccup silently dropped the command, leaving the
        # base topic (and the UI via SSE-hub) showing 'closed' while the
        # relay stayed open. Now we use the same retry+ack guarantee as the
        # base topic and propagate failure to the caller.
        t_on = t + "/on"
        on_key = (sid or 0, t_on)
        now2 = time.time()
        with _TOPIC_LOCK:
            last2 = _TOPIC_LAST_SEND.get(on_key)
            if last2 and last2[0] == value and (now2 - last2[1]) < min_interval_sec:
                # Skipping the duplicate suppression — base already delivered.
                return True
            _TOPIC_LAST_SEND[on_key] = (value, now2)
        logger.debug(f"MQTT publish topic={t_on} value={value}")
        if not _publish_one(server, sid, t_on, value, effective_qos, retain):
            logger.error("MQTT publish to /on companion FAILED topic=%s", t_on)
            return False

        # Optional: publish meta information to a side topic for diagnostics/idempotence
        try:
            if meta:
                t_meta = t + "/meta"
                payload_meta = ";".join([f"{k}={v}" for k, v in meta.items() if v is not None])
                if payload_meta:
                    cl_meta = get_or_create_mqtt_client(server)
                    if cl_meta is not None:
                        cl_meta.publish(t_meta, payload=payload_meta, qos=0, retain=False)
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Exception in line_201: %s", e)
            # meta is best-effort
            pass

        return True
    except (ConnectionError, TimeoutError, OSError):
        logger.exception("publish_mqtt_value failed")
        return False


# ── Graceful shutdown ──────────────────────────────────────────────────────
import atexit


def _shutdown_mqtt_clients() -> None:
    """Disconnect all cached MQTT clients on process exit."""
    for sid, cl in list(_MQTT_CLIENTS.items()):
        try:
            cl.loop_stop()
            cl.disconnect()
            logger.info("MQTT client disconnected for server %s", sid)
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("MQTT shutdown error for %s: %s", sid, e)
    _MQTT_CLIENTS.clear()


from config import TESTING as _TESTING

if not _TESTING:
    atexit.register(_shutdown_mqtt_clients)
