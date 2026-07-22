import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
except ImportError as e:
    logger.debug("Exception in line_8: %s", e)
    mqtt = None

from utils import SecretDecryptionError, normalize_topic

try:
    from database import db as _db
except ImportError as e:
    logger.debug("Exception in line_15: %s", e)
    _db = None

# Audit helpers — local import on use to avoid circular at module load. We
# import lazily inside publish_mqtt_value to keep startup time clean.

# Caches and locks
_MQTT_CLIENTS: dict[int, object] = {}
_MQTT_CLIENTS_LOCK = threading.RLock()
_MQTT_CLIENT_CREATE_LOCKS: dict[int, threading.Lock] = {}
_MQTT_CLIENT_USERS: dict[int, int] = {}
_RETIRED_MQTT_CLIENTS: dict[int, tuple[int, object]] = {}
_MQTT_CONFIG_FINGERPRINT_KEY = secrets.token_bytes(32)
_MQTT_CLIENT_GENERATION = 0
_TOPIC_LAST_SEND: dict[tuple[int, int, str, str], tuple[str, float]] = {}
_TOPIC_INFLIGHT: dict[tuple[int, int, str, str, str], "_InflightPublish"] = {}
_MQTT_SERVER_EPOCH: dict[int, int] = {}
_TOPIC_LOCK = threading.Lock()
_SERVER_CACHE: dict[int, tuple[dict, float, int, str]] = {}
_SERVER_CACHE_LOCK = threading.Lock()
from constants import MQTT_CACHE_TTL_SEC

_SERVER_CACHE_TTL = float(MQTT_CACHE_TTL_SEC)


@dataclass(frozen=True, slots=True)
class MqttClientSnapshot:
    """Immutable point-in-time attribution for one cached publisher client."""

    server_id: int
    client: Any
    config_fingerprint: str
    generation: int


@dataclass(frozen=True, slots=True)
class _MqttClientProvenance:
    client: Any
    config_fingerprint: str
    generation: int
    server_epoch: int


@dataclass(slots=True)
class _InflightPublish:
    completed: threading.Event = field(default_factory=threading.Event)
    result: bool | None = None


_MQTT_CLIENT_PROVENANCE: dict[int, _MqttClientProvenance] = {}


def _server_key(server_id: Any) -> int:
    try:
        return int(server_id) if server_id is not None else 0
    except (ValueError, TypeError):
        return 0


def _effective_mqtt_config(server: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze exactly the connection settings used to construct a client."""
    return {
        "host": str(server.get("host") or "127.0.0.1"),
        "port": int(server.get("port") or 1883),
        "username": str(server.get("username") or ""),
        "password": str(server.get("password") or ""),
        "client_id": str(server.get("client_id") or ""),
        "enabled": int(server.get("enabled", 1) or 0) == 1,
        "tls_enabled": int(server.get("tls_enabled") or 0) == 1,
        "tls_ca_path": str(server.get("tls_ca_path") or ""),
        "tls_cert_path": str(server.get("tls_cert_path") or ""),
        "tls_key_path": str(server.get("tls_key_path") or ""),
        "tls_insecure": int(server.get("tls_insecure") or 0) == 1,
        "tls_version": str(server.get("tls_version") or "").upper().strip(),
    }


def _fingerprint_effective_mqtt_config(config: Mapping[str, Any]) -> str:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(_MQTT_CONFIG_FINGERPRINT_KEY, canonical, hashlib.sha256).hexdigest()


def mqtt_server_config_fingerprint(server: Mapping[str, Any]) -> str:
    """Return an opaque process-local fingerprint of effective connection config.

    A keyed digest lets status code compare current DB configuration with the
    immutable origin of a cached client without exposing a password digest
    that could be attacked offline.
    """
    return _fingerprint_effective_mqtt_config(_effective_mqtt_config(server))


def snapshot_mqtt_clients() -> dict[int, MqttClientSnapshot]:
    """Return a detached, internally consistent snapshot of publisher clients."""
    with _MQTT_CLIENTS_LOCK:
        snapshots: dict[int, MqttClientSnapshot] = {}
        for sid, client in _MQTT_CLIENTS.items():
            provenance = _MQTT_CLIENT_PROVENANCE.get(sid)
            if provenance is None or provenance.client is not client:
                # Never infer origin from the independently mutable server TTL
                # cache. A client without matching creation metadata is not
                # attributable and therefore cannot be reported as current.
                continue
            snapshots[sid] = MqttClientSnapshot(
                server_id=sid,
                client=client,
                config_fingerprint=provenance.config_fingerprint,
                generation=provenance.generation,
            )
        return snapshots


def _drop_mqtt_client_provenance(sid: int, client: Any | None = None) -> None:
    """Remove provenance only when it still belongs to ``client``."""
    provenance = _MQTT_CLIENT_PROVENANCE.get(sid)
    if provenance is not None and (client is None or provenance.client is client):
        _MQTT_CLIENT_PROVENANCE.pop(sid, None)


def _teardown_mqtt_client(client: Any, sid: int) -> None:
    try:
        client.loop_stop()
    except Exception as e:
        logger.debug("MQTT loop_stop sid=%s: %s", sid, e)
    try:
        client.disconnect()
    except Exception as e:
        logger.debug("MQTT disconnect sid=%s: %s", sid, e)


def _retire_mqtt_client(client: Any, sid: int) -> None:
    """Tear down a client once no publisher thread still has a lease."""
    teardown_now = False
    client_key = id(client)
    with _MQTT_CLIENTS_LOCK:
        if _MQTT_CLIENT_USERS.get(client_key, 0) > 0:
            _RETIRED_MQTT_CLIENTS[client_key] = (sid, client)
        else:
            teardown_now = True
    if teardown_now:
        _teardown_mqtt_client(client, sid)


def _release_mqtt_client(client: Any) -> None:
    """Release a publisher lease and finish deferred teardown if necessary."""
    retired: tuple[int, object] | None = None
    client_key = id(client)
    with _MQTT_CLIENTS_LOCK:
        users = _MQTT_CLIENT_USERS.get(client_key, 0)
        if users <= 1:
            _MQTT_CLIENT_USERS.pop(client_key, None)
            retired = _RETIRED_MQTT_CLIENTS.pop(client_key, None)
        else:
            _MQTT_CLIENT_USERS[client_key] = users - 1
    if retired is not None:
        sid, retired_client = retired
        _teardown_mqtt_client(retired_client, sid)


def _cached_client(
    sid: int,
    *,
    acquire: bool,
    expected_epoch: int,
    expected_fingerprint: str,
    allow_legacy_provenance: bool,
) -> tuple[Any | None, Any | None]:
    """Return ``(usable, stale)`` while touching shared state only briefly."""
    with _MQTT_CLIENTS_LOCK:
        client = _MQTT_CLIENTS.get(sid)
        if client is None:
            return None, None
        provenance = _MQTT_CLIENT_PROVENANCE.get(sid)
        authority_matches = provenance is not None and (
            provenance.client is client
            and provenance.server_epoch == int(expected_epoch)
            and provenance.config_fingerprint == expected_fingerprint
        )
        legacy_unattributed = allow_legacy_provenance and (provenance is None or provenance.client is not client)
        if not authority_matches and not legacy_unattributed:
            if _MQTT_CLIENTS.get(sid) is client:
                _MQTT_CLIENTS.pop(sid, None)
                _drop_mqtt_client_provenance(sid, client)
            return None, client
        try:
            connected = not hasattr(client, "is_connected") or bool(client.is_connected())
        except Exception as e:
            logger.debug("is_connected check sid=%s: %s", sid, e)
            connected = True
        if not connected:
            if _MQTT_CLIENTS.get(sid) is client:
                _MQTT_CLIENTS.pop(sid, None)
                _drop_mqtt_client_provenance(sid, client)
            return None, client
        if acquire:
            client_key = id(client)
            _MQTT_CLIENT_USERS[client_key] = _MQTT_CLIENT_USERS.get(client_key, 0) + 1
        return client, None


def get_or_create_mqtt_client(
    server: dict[str, Any],
    *,
    acquire: bool = False,
    expected_epoch: int | None = None,
    expected_fingerprint: str | None = None,
) -> Any | None:
    global _MQTT_CLIENT_GENERATION

    if mqtt is None:
        return None
    sid = _server_key(server.get("id"))
    config = _effective_mqtt_config(server)
    config_fingerprint = _fingerprint_effective_mqtt_config(config)
    strict_authority = expected_epoch is not None or expected_fingerprint is not None
    with _MQTT_CLIENTS_LOCK:
        captured_epoch = _MQTT_SERVER_EPOCH.get(sid, 0) if expected_epoch is None else int(expected_epoch)
        if _MQTT_SERVER_EPOCH.get(sid, 0) != captured_epoch:
            return None
    if expected_fingerprint is not None and expected_fingerprint != config_fingerprint:
        return None

    client, stale = _cached_client(
        sid,
        acquire=acquire,
        expected_epoch=captured_epoch,
        expected_fingerprint=config_fingerprint,
        allow_legacy_provenance=not strict_authority,
    )
    if stale is not None:
        logger.warning("MQTT cached client sid=%s stale/disconnected — recreating", sid)
        _retire_mqtt_client(stale, sid)
    if client is not None:
        return client

    # Serialize creation only per server.  The global registry lock is never
    # held across DNS/TCP/TLS connect, so one dead broker cannot stall commands
    # to every other configured broker.
    with _MQTT_CLIENTS_LOCK:
        create_lock = _MQTT_CLIENT_CREATE_LOCKS.setdefault(sid, threading.Lock())
    with create_lock:
        with _MQTT_CLIENTS_LOCK:
            if _MQTT_SERVER_EPOCH.get(sid, 0) != captured_epoch:
                return None
        client, stale = _cached_client(
            sid,
            acquire=acquire,
            expected_epoch=captured_epoch,
            expected_fingerprint=config_fingerprint,
            allow_legacy_provenance=not strict_authority,
        )
        if stale is not None:
            logger.warning("MQTT cached client sid=%s stale/disconnected — recreating", sid)
            _retire_mqtt_client(stale, sid)
        if client is not None:
            return client

        client = None
        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=config["client_id"],
            )
            if config["username"]:
                client.username_pw_set(config["username"], config["password"] or None)
            if config["tls_enabled"]:
                try:
                    import ssl

                    ca = config["tls_ca_path"] or None
                    cert = config["tls_cert_path"] or None
                    key = config["tls_key_path"] or None
                    tls_ver = config["tls_version"]
                    version = ssl.PROTOCOL_TLS_CLIENT if tls_ver in ("", "TLS", "TLS_CLIENT") else ssl.PROTOCOL_TLS
                    client.tls_set(ca_certs=ca, certfile=cert, keyfile=key, tls_version=version)
                    if config["tls_insecure"]:
                        client.tls_insecure_set(True)
                except (ImportError, OSError, ValueError, RuntimeError):
                    logger.exception("MQTT TLS setup failed for publisher; refusing plaintext fallback")
                    _teardown_mqtt_client(client, sid)
                    return None

            host = config["host"]
            port = config["port"]

            def _on_disconnect(_client, _userdata, _disconnect_flags, reason_code, _properties):
                try:
                    logger.info(
                        "MQTT client disconnected sid=%s rc=%s (auto-reconnect active)",
                        sid,
                        reason_code,
                    )
                except (ConnectionError, TimeoutError, OSError) as e:
                    logger.debug("Handled exception in _on_disconnect: %s", e)

            client.on_disconnect = _on_disconnect
            try:
                client.reconnect_delay_set(min_delay=1, max_delay=5)
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("MQTT reconnect delay setup sid=%s: %s", sid, e)
            try:
                client.max_inflight_messages_set(100)
            except (ValueError, AttributeError) as e:
                logger.debug("MQTT inflight setup sid=%s: %s", sid, e)
            client.connect(host, port, 10)
            client.loop_start()
        except (ConnectionError, TimeoutError, OSError, ValueError):
            logger.exception("MQTT connect failed sid=%s host=%s:%s", sid, server.get("host"), server.get("port"))
            if client is not None:
                _teardown_mqtt_client(client, sid)
            return None

        install = False
        with _MQTT_CLIENTS_LOCK:
            if _MQTT_SERVER_EPOCH.get(sid, 0) == captured_epoch:
                _MQTT_CLIENT_GENERATION += 1
                _MQTT_CLIENTS[sid] = client
                _MQTT_CLIENT_PROVENANCE[sid] = _MqttClientProvenance(
                    client=client,
                    config_fingerprint=config_fingerprint,
                    generation=_MQTT_CLIENT_GENERATION,
                    server_epoch=captured_epoch,
                )
                if acquire:
                    client_key = id(client)
                    _MQTT_CLIENT_USERS[client_key] = _MQTT_CLIENT_USERS.get(client_key, 0) + 1
                install = True
        if not install:
            logger.warning("MQTT client creation superseded by invalidation sid=%s epoch=%s", sid, captured_epoch)
            _teardown_mqtt_client(client, sid)
            return None
        return client


def _publish_with_retries(cl: Any, topic: str, value: str, qos: int, retain: bool) -> bool:
    """Publish ``value`` to ``topic`` with connect-rc retries (up to 10x)
    and, for QoS≥1, broker-ack wait with backoff (up to 3x).

    Returns True on confirmed delivery, False otherwise.  Actuator commands
    use this for the Wirenboard ``/on`` channel; the base device topic is
    relay-owned observed/report truth and is deliberately never written by
    the application.
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
            delivery_attempts = len(backoff_delays) + 1
            for attempt_idx in range(delivery_attempts):
                if attempt_idx > 0:
                    time.sleep(backoff_delays[attempt_idx - 1])
                    try:
                        res = cl.publish(topic, payload=value, qos=effective_qos, retain=retain)
                    except (ConnectionError, TimeoutError, OSError):
                        logger.exception("MQTT publish (QoS>=1 retry republish) failed topic=%s", topic)
                        continue
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
                        f"(attempt {attempt_idx + 1}/{delivery_attempts}) topic={topic}"
                    )
                except Exception as wfp_err:
                    logger.warning(
                        f"MQTT wait_for_publish failed "
                        f"(attempt {attempt_idx + 1}/{delivery_attempts}) topic={topic}: {wfp_err}"
                    )
            if not published:
                logger.critical(
                    f"MQTT QoS {effective_qos} delivery FAILED after "
                    f"{delivery_attempts} attempts topic={topic} value={value}"
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
                        error=f"QoS{effective_qos} delivery failed after {delivery_attempts} attempts",
                    )
                except Exception:
                    logger.exception("mqtt_publish_failure: record_audit failed")
                return False
        return True
    except (ConnectionError, TimeoutError, OSError):
        logger.exception("MQTT publish failed topic=%s", topic)
        return False


def _invalidate_client(sid: Any, *, expected_client: Any | None = None) -> None:
    """Drop a (likely wedged) cached MQTT client and tear it down.

    The next ``get_or_create_mqtt_client`` rebuilds a fresh client with an
    empty inflight window. This is the recovery path for the failure mode
    where QoS>=1 messages get queued-but-never-published after a
    mid-handshake disconnect leaves orphaned mids occupying the inflight
    window (mqtt-client-recovery).
    """
    key = _server_key(sid)
    with _MQTT_CLIENTS_LOCK:
        cl = _MQTT_CLIENTS.get(key)
        if expected_client is not None and cl is not expected_client:
            return
        if cl is not None:
            _MQTT_CLIENTS.pop(key, None)
        _drop_mqtt_client_provenance(key, cl)
    if cl is not None:
        _retire_mqtt_client(cl, key)
        logger.info("MQTT client invalidated sid=%s (recreate on next publish)", key)


def invalidate_mqtt_server(server_id: Any) -> None:
    """Public runtime invalidation boundary for MQTT server CRUD."""
    sid = _server_key(server_id)
    stale_client = None
    # Epoch advance, cache eviction, and registry eviction are one authority
    # transition. A creator that started before this point may finish its
    # socket handshake, but its install check will fail and close that client.
    with _MQTT_CLIENTS_LOCK:
        _MQTT_SERVER_EPOCH[sid] = _MQTT_SERVER_EPOCH.get(sid, 0) + 1
        stale_client = _MQTT_CLIENTS.pop(sid, None)
        _drop_mqtt_client_provenance(sid, stale_client)
        with _SERVER_CACHE_LOCK:
            _SERVER_CACHE.pop(sid, None)
    with _TOPIC_LOCK:
        # Epoch, not only config fingerprint, separates A -> B -> A rotations
        # from an obsolete in-flight publish that still belongs to the first A
        # client generation.
        stale_topics = [key for key in _TOPIC_LAST_SEND if key[0] == sid]
        for key in stale_topics:
            _TOPIC_LAST_SEND.pop(key, None)
    if stale_client is not None:
        _retire_mqtt_client(stale_client, sid)
        logger.info("MQTT client invalidated sid=%s (config epoch advanced)", sid)


def _invalidate_failed_client(sid: Any, failed_client: Any) -> None:
    """Invalidate only if the failed client is still the cached generation."""
    key = _server_key(sid)
    with _MQTT_CLIENTS_LOCK:
        current = _MQTT_CLIENTS.get(key)
        if current is not None and current is not failed_client:
            return
        _invalidate_client(key)


def _publish_one(
    server: dict,
    sid: Any,
    topic: str,
    value: str,
    qos: int,
    retain: bool,
    *,
    server_epoch: int,
    config_fingerprint: str,
) -> bool:
    """Publish one topic with self-healing.

    On delivery failure the cached client is most likely wedged (its inflight
    window is full of QoS>=1 messages that will never be acknowledged). We
    rebuild the client once and retry on a clean window. Without this, relay
    commands block indefinitely in ``wait_for_publish`` (mqtt-client-recovery).
    """
    cl = get_or_create_mqtt_client(
        server,
        acquire=True,
        expected_epoch=server_epoch,
        expected_fingerprint=config_fingerprint,
    )
    if cl is None:
        logger.warning("MQTT publish: client unavailable, dropping topic=%s", topic)
        return False
    try:
        delivered = _publish_with_retries(cl, topic, value, qos, retain)
    finally:
        _release_mqtt_client(cl)
    if delivered:
        return True
    logger.warning("MQTT delivery failed — recreating client sid=%s topic=%s", sid, topic)
    _invalidate_failed_client(sid, cl)
    cl = get_or_create_mqtt_client(
        server,
        acquire=True,
        expected_epoch=server_epoch,
        expected_fingerprint=config_fingerprint,
    )
    if cl is None:
        return False
    try:
        return _publish_with_retries(cl, topic, value, qos, retain)
    finally:
        _release_mqtt_client(cl)


def _resolve_publish_server(server: dict, sid: int | None) -> tuple[dict, int, str] | None:
    """Resolve one server against a linearizable config epoch/cache entry."""
    server_key = sid or 0
    if sid is None or _db is None:
        resolved = dict(server)
        fingerprint = mqtt_server_config_fingerprint(resolved)
        with _MQTT_CLIENTS_LOCK:
            epoch = _MQTT_SERVER_EPOCH.get(server_key, 0)
        return resolved, epoch, fingerprint

    # A DB read can overlap a CRUD invalidation. Retry rather than installing
    # the pre-invalidation row into the new generation's TTL cache.
    for _attempt in range(4):
        now_ts = time.time()
        with _MQTT_CLIENTS_LOCK:
            epoch = _MQTT_SERVER_EPOCH.get(server_key, 0)
            with _SERVER_CACHE_LOCK:
                cached = _SERVER_CACHE.get(sid)
                if cached and len(cached) >= 4:
                    cached_server, cached_at, cached_epoch, cached_fingerprint = cached[:4]
                    actual_fingerprint = mqtt_server_config_fingerprint(cached_server)
                    if (
                        int(cached_epoch) == epoch
                        and actual_fingerprint == cached_fingerprint
                        and (now_ts - float(cached_at)) < _SERVER_CACHE_TTL
                    ):
                        return dict(cached_server), epoch, cached_fingerprint

        current = _db.get_mqtt_server(sid)
        if not current:
            return None
        resolved = dict(current)
        fingerprint = mqtt_server_config_fingerprint(resolved)
        with _MQTT_CLIENTS_LOCK:
            if _MQTT_SERVER_EPOCH.get(server_key, 0) != epoch:
                continue
            with _SERVER_CACHE_LOCK:
                _SERVER_CACHE[sid] = (resolved, now_ts, epoch, fingerprint)
            return resolved, epoch, fingerprint

    logger.error("MQTT server %s changed repeatedly during cache fill; publish refused", sid)
    return None


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
        if not t:
            logger.error("MQTT publish refused invalid/base-command topic=%r", topic)
            return False
        sid = int(server.get("id")) if server.get("id") else None
        try:
            resolved_authority = _resolve_publish_server(server, sid)
        except (ConnectionError, SecretDecryptionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in publish_mqtt_value: %s", e)
            if isinstance(e, SecretDecryptionError):
                logger.error("MQTT credentials unavailable for server %s; restore the configured secret key", sid)
            return False
        if resolved_authority is None:
            logger.error("MQTT server %s is missing or unavailable; refusing stale/virtual publish", sid)
            return False
        server, epoch, config_fingerprint = resolved_authority

        # Wiren Board owns the base topic as its observed/report channel.  The
        # application must write desired state only to the companion ``/on``
        # command channel; publishing to the base topic makes our own value
        # indistinguishable from a physical relay echo to long-lived
        # subscribers and can falsely complete runs/cancel safety jobs.
        t_command = t + "/on"
        server_key = sid or 0
        now = time.time()
        wait_for: _InflightPublish | None = None
        pending: _InflightPublish | None = None
        with _MQTT_CLIENTS_LOCK:
            if _MQTT_SERVER_EPOCH.get(server_key, 0) != epoch:
                logger.warning("MQTT publish superseded before reservation sid=%s epoch=%s", server_key, epoch)
                return False
            with _TOPIC_LOCK:
                key = (server_key, epoch, config_fingerprint, t_command)
                inflight_key = (server_key, epoch, config_fingerprint, t_command, value)
                wait_for = _TOPIC_INFLIGHT.get(inflight_key)
                if wait_for is None:
                    last = _TOPIC_LAST_SEND.get(key)
                    if last and last[0] == value and (now - last[1]) < min_interval_sec:
                        logger.debug(f"MQTT skip delivered duplicate topic={t_command} value={value}")
                        return True
                    pending = _InflightPublish()
                    _TOPIC_INFLIGHT[inflight_key] = pending
        if wait_for is not None:
            # The first caller owns the physical publish. A concurrent duplicate
            # must observe that same delivery result instead of treating an
            # unconfirmed cache reservation as success.
            wait_for.completed.wait()
            return bool(wait_for.result)

        logger.debug(f"MQTT publish command topic={t_command} value={value}")
        effective_qos = max(0, min(2, int(qos or 0)))
        delivered = False
        try:
            delivered = bool(
                _publish_one(
                    server,
                    sid,
                    t_command,
                    value,
                    effective_qos,
                    retain,
                    server_epoch=epoch,
                    config_fingerprint=config_fingerprint,
                )
            )
        finally:
            with _MQTT_CLIENTS_LOCK:
                epoch_is_current = _MQTT_SERVER_EPOCH.get(server_key, 0) == epoch
                with _TOPIC_LOCK:
                    # An invalidation may complete while the broker call is in
                    # flight. The old caller still receives its delivery result,
                    # but it must not repopulate the newer debounce authority.
                    if delivered and epoch_is_current:
                        _TOPIC_LAST_SEND[key] = (value, time.time())
                    if _TOPIC_INFLIGHT.get(inflight_key) is pending:
                        _TOPIC_INFLIGHT.pop(inflight_key, None)
                    if pending is not None:
                        pending.result = delivered
                        pending.completed.set()
        if not delivered:
            return False

        # Debug-level audit: every successful publish. Volume is high
        # (Wirenboard publishes can be hundreds per hour) — gated behind
        # `settings.logging.debug` so audit_log doesn't blow up in normal use.
        try:
            from services.audit import debug_audit

            debug_audit(
                action_type="mqtt_publish",
                source="mqtt",
                target=t_command,
                payload={
                    "value": value,
                    "qos": effective_qos,
                    "retain": bool(retain),
                    "report_topic": t,
                    "meta": meta if isinstance(meta, dict) else None,
                },
            )
        except Exception:
            logger.debug("debug_audit(mqtt_publish) failed", exc_info=True)

        # Optional: publish meta information to a side topic for diagnostics/idempotence
        try:
            if meta:
                t_meta = t + "/meta"
                payload_meta = ";".join([f"{k}={v}" for k, v in meta.items() if v is not None])
                if payload_meta:
                    cl_meta = get_or_create_mqtt_client(
                        server,
                        expected_epoch=epoch,
                        expected_fingerprint=config_fingerprint,
                    )
                    if cl_meta is not None:
                        cl_meta.publish(t_meta, payload=payload_meta, qos=0, retain=False)
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Exception in line_201: %s", e)
            # meta is best-effort
            pass

        return True
    except (ConnectionError, SecretDecryptionError, TimeoutError, OSError, TypeError, ValueError):
        logger.exception("publish_mqtt_value failed")
        return False


# ── Graceful shutdown ──────────────────────────────────────────────────────
import atexit


def _shutdown_mqtt_clients() -> None:
    """Disconnect all cached MQTT clients on process exit."""
    with _MQTT_CLIENTS_LOCK:
        clients = list(_MQTT_CLIENTS.items())
        _MQTT_CLIENTS.clear()
        _MQTT_CLIENT_PROVENANCE.clear()
    for sid, cl in clients:
        _teardown_mqtt_client(cl, sid)
        logger.info("MQTT client disconnected for server %s", sid)


from config import TESTING as _TESTING

if not _TESTING:
    atexit.register(_shutdown_mqtt_clients)
