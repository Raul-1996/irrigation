"""
Centralised SSE (Server-Sent Events) Hub for real-time MQTT→browser streaming.

Extracted from app.py (TASK-015).  The module does NOT import ``app`` or
``db`` directly — every dependency is injected via :func:`init` or passed
as function arguments so that circular imports are impossible.
"""

import contextlib
import json
import logging
import queue
import sqlite3
import ssl
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta

from services.locks import zone_lock
from services.observed_state import canonical_relay_state, state_verifier
from utils import normalize_topic

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global hub state
# ---------------------------------------------------------------------------
MAX_SSE_CLIENTS: int = 20

_SSE_HUB_STARTED: bool = False
_SSE_HUB_LOCK: threading.Lock = threading.Lock()
_SSE_HUB_CLIENTS: list = []  # list[queue.Queue]
_SSE_HUB_MQTT: dict = {}  # sid → paho client
_SSE_HUB_SERVER_KEYS: dict = {}  # sid → immutable connection-settings tuple
_SSE_HUB_ZONE_TOPICS: dict = {}  # sid → topic → zone ids
_SSE_HUB_MV_TOPICS: dict = {}  # sid → topic → (group id, master mode)
_SSE_META_BUFFER: deque = deque(maxlen=100)
_SSE_CLEANER_STARTED: bool = False

# Rebuilds are requested under ``_SSE_HUB_LOCK`` and performed by one daemon
# thread. Socket connect/disconnect and paho loop joins must never execute
# while that lock is held: broadcast/register are safety-path operations too.
_SSE_HUB_REQUESTED_GENERATION: int = 0
_SSE_HUB_APPLIED_GENERATION: int = 0
_SSE_HUB_REBUILD_RUNNING: bool = False

# paho invokes callbacks on its network-loop thread. DB writes, scheduler
# calls and reliable counter-publishes run on a separate ordered worker so a
# slow SQLite writer or broker cannot starve MQTT keepalive processing.
_SSE_EVENT_QUEUE: "queue.Queue" = queue.Queue(maxsize=1000)
_SSE_EVENT_WORKER_STARTED: bool = False
_SSE_EVENT_WORKER_LOCK: threading.Lock = threading.Lock()

# Anti-restart: remember observed/manual stops so instant ON bounces can be
# rejected. The DB timestamp fallback also covers stops made while the hub was
# temporarily disconnected.
_LAST_MANUAL_STOP: dict[int, float] = {}
_LAST_STOP_LOCK: threading.Lock = threading.Lock()

# Injected dependencies (set via init())
_db = None
_mqtt = None
_app_config = None
_publish_mqtt_value_fn = None
_normalize_topic_fn = None
_get_scheduler_fn = None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def init(*, db, mqtt_module, app_config, publish_mqtt_value, normalize_topic, get_scheduler):
    """Inject runtime dependencies and start the permanent safety subscriber."""
    global _db, _mqtt, _app_config, _publish_mqtt_value_fn, _normalize_topic_fn, _get_scheduler_fn
    _db = db
    _mqtt = mqtt_module
    _app_config = app_config
    _publish_mqtt_value_fn = publish_mqtt_value
    _normalize_topic_fn = normalize_topic
    _get_scheduler_fn = get_scheduler
    _ensure_event_worker_started()
    # The hub is a safety subscriber, not merely a browser transport. Start it
    # at dependency injection time so physical/retained relay events are still
    # handled when nobody has opened the zones page. The rebuild itself is
    # asynchronous, so an unavailable broker cannot hold up application boot.
    if not (_app_config and _app_config.get("TESTING")):
        ensure_hub_started()


def get_meta_buffer() -> list:
    """Return a copy of recent meta-messages (for health panel)."""
    try:
        return list(_SSE_META_BUFFER)
    except (OSError, RuntimeError, ValueError) as e:
        logger.debug("Exception in get_meta_buffer: %s", e)
        return []


def _terminate_client(msg_queue: "queue.Queue") -> None:
    """Make a removed SSE queue terminate even when it was already full."""
    try:
        while True:
            msg_queue.get_nowait()
    except queue.Empty:
        pass
    try:
        msg_queue.put_nowait(None)
    except queue.Full:
        # A concurrent producer can only be a stale snapshot. Drain once more
        # so the terminal marker remains the next item for the SSE generator.
        with contextlib.suppress(queue.Empty):
            msg_queue.get_nowait()
        with contextlib.suppress(queue.Full):
            msg_queue.put_nowait(None)


def broadcast(data_json: str) -> None:
    """Push a JSON string to every connected SSE client."""
    dead: list = []
    try:
        with _SSE_HUB_LOCK:
            for msg_queue in list(_SSE_HUB_CLIENTS):
                try:
                    msg_queue.put_nowait(data_json)
                except queue.Full:
                    dead.append(msg_queue)
            for msg_queue in dead:
                with contextlib.suppress(ValueError):
                    _SSE_HUB_CLIENTS.remove(msg_queue)
    except (RuntimeError, OSError) as e:
        logger.warning("Broadcast failed: %s", e)
    for msg_queue in dead:
        _terminate_client(msg_queue)
    if dead:
        logger.info("Removed %d dead SSE clients (queue full)", len(dead))


def mark_zone_stopped(zone_id: int) -> None:
    """Record a stop timestamp for the anti-restart window."""
    try:
        with _LAST_STOP_LOCK:
            _LAST_MANUAL_STOP[int(zone_id)] = time.time()
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Handled exception in mark_zone_stopped: %s", e)


def recently_stopped(zone_id: int, window_sec: int = 5) -> bool:
    """True if *zone_id* was stopped within *window_sec* seconds."""
    try:
        with _LAST_STOP_LOCK:
            ts = _LAST_MANUAL_STOP.get(int(zone_id))
        return (ts is not None) and ((time.time() - ts) < max(0, int(window_sec)))
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Exception in recently_stopped: %s", e)
        return False


# ---------------------------------------------------------------------------
# Subscription maps and message processing
# ---------------------------------------------------------------------------


def _subscription_topic(topic: object) -> str:
    """Canonical report topic; configured command channels are rejected."""
    raw = str(topic or "")
    normalizer = _normalize_topic_fn if callable(_normalize_topic_fn) else normalize_topic
    try:
        candidate = normalizer(raw)
    except (ValueError, TypeError, AttributeError):
        logger.exception("sse_hub: injected topic normalizer failed")
        candidate = raw
    # ``init`` keeps the normalizer injectable for application wiring, but
    # subscription safety must not depend on a stale/test implementation that
    # merely adds a slash and leaves the actuator command suffix intact.
    return normalize_topic(candidate if isinstance(candidate, str) else raw)


def _incoming_report_topic(topic: object) -> str:
    """Normalize an incoming report topic without accepting command echoes."""
    raw = str(topic or "").strip()
    if not raw:
        return ""
    normalized = normalize_topic(raw)
    if not normalized:
        logger.warning("sse_hub: ignoring invalid/command-channel echo topic=%s", raw)
        return ""
    return normalized


def _rebuild_subscriptions():
    """Build zone-topic and master-valve-topic maps from the database."""
    zones = _db.get_zones()
    groups = _db.get_groups() or []
    zone_topics: dict = {}
    mv_topics: dict = {}
    for zone in zones:
        sid = zone.get("mqtt_server_id")
        topic = (zone.get("topic") or "").strip()
        if not sid or not topic:
            continue
        normalized = _subscription_topic(topic)
        if not normalized:
            continue
        zone_topics.setdefault(int(sid), {}).setdefault(normalized, []).append(int(zone["id"]))
    for group in groups:
        try:
            if int(group.get("use_master_valve") or 0) != 1:
                continue
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in _rebuild_subscriptions: %s", e)
            continue
        topic = (group.get("master_mqtt_topic") or "").strip()
        sid = group.get("master_mqtt_server_id")
        if not topic or not sid:
            continue
        normalized = _subscription_topic(topic)
        if not normalized:
            continue
        mode = str(group.get("master_mode") or "NC").strip().upper()
        mv_topics.setdefault(int(sid), {}).setdefault(normalized, []).append((int(group["id"]), mode))
    return zone_topics, mv_topics


def _message_targets(sid: int, topic: str, source_client):
    with _SSE_HUB_LOCK:
        if source_client is not None and _SSE_HUB_MQTT.get(int(sid)) is not source_client:
            return None, None
        zones = list(_SSE_HUB_ZONE_TOPICS.get(int(sid), {}).get(topic) or [])
        master_groups = list(_SSE_HUB_MV_TOPICS.get(int(sid), {}).get(topic) or [])
    return zones, master_groups


def _recent_db_stop(zone: dict, window_sec: int = 5) -> bool:
    """Cover a stop performed while this subscriber was disconnected."""
    if str(zone.get("commanded_state") or "").lower() != "off":
        return False
    if str(zone.get("state") or "").lower() not in ("off", "stopping"):
        return False
    raw = zone.get("updated_at")
    if not raw:
        return False
    try:
        changed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        now = datetime.now(changed.tzinfo) if changed.tzinfo else datetime.now()
        age = (now - changed).total_seconds()
        return 0 <= age < max(0, int(window_sec))
    except (TypeError, ValueError, OverflowError):
        return False


def _get_mqtt_server_safe(sid: int):
    """Isolate one unreadable/malformed server record from the whole hub."""
    try:
        return _db.get_mqtt_server(int(sid))
    except Exception:  # Credential decoder has a typed external failure.
        logger.exception("sse_hub: MQTT server settings unavailable sid=%s", sid)
        return None


def _publish_counter_off(sid: int, topic: str) -> bool:
    try:
        server = _get_mqtt_server_safe(int(sid))
        if not server or _publish_mqtt_value_fn is None:
            logger.error("sse_hub: cannot counter ON sid=%s topic=%s: publisher unavailable", sid, topic)
            return False
        handled = bool(
            _publish_mqtt_value_fn(
                server,
                topic,
                "0",
                min_interval_sec=0.0,
                qos=2,
                retain=True,
            )
        )
        if not handled:
            logger.error("sse_hub: reliable counter-OFF failed sid=%s topic=%s", sid, topic)
        return handled
    except (ConnectionError, TimeoutError, OSError, TypeError, ValueError):
        logger.exception("sse_hub: reliable counter-OFF raised sid=%s topic=%s", sid, topic)
        return False


def _cancel_zone_safety_jobs(zone_id: int) -> bool:
    """Cancel ordinary, hard-stop and cap jobs after fresh physical OFF."""
    try:
        scheduler = _get_scheduler_fn() if _get_scheduler_fn else None
        if scheduler is None:
            return True
        try:
            scheduler.cancel_zone_jobs(int(zone_id), include_cap=True)
        except TypeError:
            # Rolling-upgrade compatibility with the pre-activation scheduler.
            scheduler.cancel_zone_jobs(int(zone_id))
            scheduler.cancel_zone_cap(int(zone_id))
        return True
    except (ValueError, TypeError, KeyError, RuntimeError, AttributeError):
        logger.exception("sse_hub: cancel fresh-OFF safety jobs failed zone=%s", zone_id)
        return False


def _schedule_counter_off_safety(zone_id: int, activation_token: str) -> None:
    """Plant a near-term retry and absolute cap for an observed unsafe ON."""
    try:
        scheduler = _get_scheduler_fn() if _get_scheduler_fn else None
        if scheduler is None:
            return
        try:
            scheduler.schedule_zone_hard_stop(
                int(zone_id),
                datetime.now() + timedelta(seconds=5),
                activation_token=activation_token,
            )
        except TypeError:
            scheduler.schedule_zone_hard_stop(int(zone_id), datetime.now() + timedelta(seconds=5))
        try:
            scheduler.schedule_zone_cap(int(zone_id), activation_token=activation_token)
        except TypeError:
            scheduler.schedule_zone_cap(int(zone_id))
    except (ValueError, TypeError, KeyError, RuntimeError, AttributeError):
        logger.exception("sse_hub: schedule counter-OFF safety failed zone=%s", zone_id)


def _audited_zone_update(zone_id: int, updates: dict, *, snapshot: dict) -> bool:
    try:
        from services.zones_state import update_zone_state_internal

        applied, _current = update_zone_state_internal(
            int(zone_id),
            updates,
            snapshot=snapshot,
            audit_reason="mqtt_observed_change",
            db=_db,
        )
        if not applied:
            logger.warning("sse_hub: mqtt observed-state CAS conflicted zone=%s", zone_id)
        return applied
    except (sqlite3.Error, OSError, ImportError):
        logger.exception("sse_hub: audited mqtt_observed_change CAS failed zone=%s", zone_id)
        return False


def _finish_observed_run(zone_id: int, *, status: str = "ok") -> bool:
    try:
        run = _db.get_open_zone_run(int(zone_id))
        if not run:
            return True
        finished = _db.finish_zone_run(
            int(run["id"]),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            time.monotonic(),
            None,
            None,
            None,
            status=str(status),
        )
        if finished is not True:
            logger.error("sse_hub: finish_zone_run rejected zid=%s run=%s", zone_id, run.get("id"))
            return False
        return True
    except (sqlite3.Error, OSError):
        logger.exception("sse_hub: finish_zone_run on observed off failed zid=%s", zone_id)
        return False


def _zone_activation_snapshot(zone: dict) -> tuple[object, object, str, str, object]:
    """Return the fields that identify one zone activation generation."""
    return (
        zone.get("version"),
        zone.get("command_id"),
        str(zone.get("state") or "").lower(),
        str(zone.get("commanded_state") or "").lower(),
        zone.get("watering_start_time"),
    )


def _process_mqtt_message(
    sid: int,
    source_client,
    topic: str,
    payload: str,
    retained: bool = False,
    received_at: float | None = None,
) -> None:
    """Process one decoded MQTT event away from paho's network thread."""
    normalized = _incoming_report_topic(topic)
    if not normalized:
        return
    zone_ids, master_groups = _message_targets(int(sid), normalized, source_client)
    if zone_ids is None:
        # The client was atomically evicted/replaced by a newer generation.
        return

    if normalized.endswith("/meta"):
        _SSE_META_BUFFER.append(
            {
                "topic": normalized,
                "payload": payload,
                "ts": datetime.now().strftime("%H:%M:%S"),
            }
        )
        return

    relay_state = canonical_relay_state(payload)
    if relay_state is None:
        # Presence/error/status payloads are not physical OFF evidence.  In
        # particular, never finish a run or cancel its safety jobs merely
        # because a payload is outside the ON set.
        logger.warning(
            "sse_hub: ignoring unknown relay payload sid=%s topic=%s payload=%r",
            sid,
            normalized,
            payload,
        )
        return
    relay_high = relay_state == "on"
    if master_groups:
        if retained:
            logger.info("sse_hub: retained master replay ignored sid=%s topic=%s", sid, normalized)
            return
        event_received_at = time.time() if received_at is None else float(received_at)
        for group_id, mode in master_groups:
            # Normally-closed: 1=open, 0=closed. Normally-open is inverted.
            is_open = (not relay_high) if mode == "NO" else relay_high
            state = "open" if is_open else "closed"
            if state == "closed":
                try:
                    from services.zone_control import confirm_master_closed_from_echo

                    if not confirm_master_closed_from_echo(
                        int(sid),
                        normalized,
                        mode,
                        received_at=event_received_at,
                    ):
                        logger.info(
                            "sse_hub: stale/unreconciled master CLOSED echo ignored sid=%s topic=%s group=%s",
                            sid,
                            normalized,
                            group_id,
                        )
                        continue
                except (ImportError, RuntimeError, TypeError, ValueError):
                    logger.exception("master activation reconciliation failed sid=%s topic=%s", sid, normalized)
                    continue
            try:
                _db.update_group_fields(int(group_id), {"master_valve_observed": state})
            except (sqlite3.Error, OSError) as e:
                logger.debug("master observed update failed group=%s: %s", group_id, e)
            broadcast(json.dumps({"mv_group_id": int(group_id), "mv_state": state}))
        return
    if retained and not relay_high:
        # Historical broker state cannot prove the currently active relay is
        # physically closed. Never finish a run or disarm safety from it.
        logger.info("sse_hub: retained relay OFF replay ignored sid=%s topic=%s", sid, normalized)
        return

    zones: dict[int, dict] = {}
    for zone_id in zone_ids:
        try:
            zones[int(zone_id)] = _db.get_zone(int(zone_id)) or {}
        except (sqlite3.Error, OSError) as e:
            logger.debug("sse_hub: get_zone failed zone=%s: %s", zone_id, e)
            zones[int(zone_id)] = {}

    active_generations = {
        zid for zid, zone in zones.items() if str(zone.get("observed_state") or "").lower() == "unconfirmed"
    }
    eligible_zone_ids = [zid for zid in zones if not retained or zid not in active_generations]
    emergency = bool(_app_config and _app_config.get("EMERGENCY_STOP"))
    bounced = relay_high and any(
        recently_stopped(zone_id, window_sec=5) or _recent_db_stop(zones[zone_id], window_sec=5)
        for zone_id in eligible_zone_ids
    )
    failed_stop_on = relay_high and any(
        str(zones[zid].get("state") or "").lower() == "fault"
        or (
            str(zones[zid].get("commanded_state") or "").lower() == "off"
            and str(zones[zid].get("state") or "").lower() in {"starting", "on", "stopping"}
        )
        for zid in eligible_zone_ids
    )
    counter_required = relay_high and bool(eligible_zone_ids) and (emergency or bounced or failed_stop_on)
    counter_handled = _publish_counter_off(int(sid), normalized) if counter_required else None

    for zone_id in zone_ids:
        zid = int(zone_id)
        zone = zones.get(zid, {})
        if retained and zid in active_generations:
            # Retained replay can predate the command currently marked
            # unconfirmed. It must not complete that generation, rewrite an
            # active ON, or disarm any activation-bound safety job.
            logger.info("sse_hub: retained relay replay ignored for active generation zone=%s", zid)
            continue
        observed_state = "on" if relay_high else "off"

        if not relay_high:
            # The queue snapshot can be overtaken by a new activation before
            # this worker reaches its DB/job mutations. Serialize with command
            # registration, re-read the exact generation, and make a stale
            # event a complete no-op instead of cancelling the new UUID jobs.
            snapshot = _zone_activation_snapshot(zone)
            with zone_lock(zid):
                current = _db.get_zone(zid) or {}
                if not current or _zone_activation_snapshot(current) != snapshot:
                    logger.info("sse_hub: stale fresh OFF event ignored after activation changed zone=%s", zid)
                    continue
                command_registered_at = state_verifier.command_registered_at(zid)
                if (
                    received_at is not None
                    and command_registered_at is not None
                    and float(received_at) < command_registered_at
                ):
                    logger.info(
                        "sse_hub: queued OFF predates current command generation zone=%s received_at=%s command_at=%s",
                        zid,
                        received_at,
                        command_registered_at,
                    )
                    continue
                current_command = str(current.get("commanded_state") or "").lower()
                if current_command == "off":
                    applied = state_verifier.apply_live_confirmation(
                        zid,
                        "off",
                        received_at=received_at,
                        db_instance=_db,
                        scheduler_getter=_get_scheduler_fn,
                    )
                    if applied:
                        mark_zone_stopped(zid)
                        broadcast(
                            json.dumps(
                                {
                                    "zone_id": zid,
                                    "topic": normalized,
                                    "payload": payload,
                                    "state": "off",
                                }
                            )
                        )
                    continue
                try:
                    invalidated_at = state_verifier.command_invalidated_at(zid)
                    if not isinstance(invalidated_at, (int, float)):
                        invalidated_at = None
                except (AttributeError, TypeError, ValueError):
                    invalidated_at = None
                commanded_on = str(current.get("commanded_state") or "").lower() == "on"
                was_fault = str(current.get("state") or "").lower() == "fault"
                pre_fault_off = bool(
                    was_fault
                    and received_at is not None
                    and invalidated_at is not None
                    and float(received_at) < invalidated_at
                )
                if not commanded_on:
                    updates = {"observed_state": "off", "planned_end_time": None}
                    if not was_fault or pre_fault_off:
                        updates["state"] = "off"
                else:
                    # Physical evidence for a still-commanded ON is retained,
                    # but cannot complete or disarm that activation.
                    updates = {"observed_state": "off"}
                applied = _audited_zone_update(zid, updates, snapshot=current)
                if not applied:
                    # No history/job/token side effect may outlive a rejected
                    # generation CAS. A newer activation now owns the relay.
                    continue
                if not commanded_on:
                    mark_zone_stopped(zid)
                    if current.get("watering_start_time"):
                        if not _finish_observed_run(
                            zid,
                            status="failed" if was_fault and not pre_fault_off else "ok",
                        ):
                            # Keep token-bound safety jobs armed and let a
                            # reconciliation/fresh report retry history close.
                            continue
                    if _cancel_zone_safety_jobs(zid):
                        latest = _db.get_zone(zid) or {}
                        if (
                            latest
                            and str(latest.get("observed_state") or "").lower() == "off"
                            and str(latest.get("commanded_state") or "").lower() != "on"
                            and latest.get("command_id") == current.get("command_id")
                        ):
                            _audited_zone_update(
                                zid,
                                {"watering_start_time": None, "command_id": None},
                                snapshot=latest,
                            )
            broadcast(
                json.dumps(
                    {
                        "zone_id": zid,
                        "topic": normalized,
                        "payload": payload,
                        "state": observed_state,
                    }
                )
            )
            continue

        if relay_high and not counter_required and str(zone.get("commanded_state") or "").lower() == "on":
            applied = state_verifier.apply_live_confirmation(
                zid,
                "on",
                received_at=received_at,
                db_instance=_db,
                scheduler_getter=_get_scheduler_fn,
            )
            if applied:
                broadcast(
                    json.dumps(
                        {
                            "zone_id": zid,
                            "topic": normalized,
                            "payload": payload,
                            "state": "on",
                        }
                    )
                )
            continue

        updates = {"state": observed_state, "observed_state": observed_state}
        persisted = False

        if relay_high and counter_required:
            # A new safety OFF generation begins at the physical ON report.
            # Persist its activation token before planting callbacks so an
            # immediate watchdog tick cannot mistake it for an ownerless OFF.
            activation_token = str(zone.get("command_id") or uuid.uuid4().hex)
            watering_start = str(zone.get("watering_start_time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            failed_stop_fault = str(zone.get("state") or "").lower() == "fault"
            updates.update(
                {
                    # A failed stop remains sticky FAULT.  The live ON is
                    # physical evidence, not permission to resurrect it as a
                    # normal activation; retain that evidence until fresh OFF.
                    "state": "fault" if failed_stop_fault else "on",
                    "commanded_state": "off",
                    "observed_state": "on" if failed_stop_fault else "unconfirmed",
                    "watering_start_time": watering_start,
                    "command_id": activation_token,
                }
            )
            persisted = _audited_zone_update(zid, updates, snapshot=zone)
            if persisted:
                _schedule_counter_off_safety(zid, activation_token)
        elif relay_high:
            # Application commands are published only to ``<topic>/on`` and
            # rejected by ``_incoming_report_topic``. A live base-topic event
            # is therefore legitimate physical truth; the command-scoped
            # verifier still owns generation-specific completion.
            active_run = bool(zone.get("watering_start_time")) or str(zone.get("state") or "").lower() in (
                "on",
                "starting",
            )
            if not active_run:
                now = datetime.now()
                duration = int(zone.get("duration") or 0)
                updates["watering_start_time"] = now.strftime("%Y-%m-%d %H:%M:%S")
                updates["command_id"] = uuid.uuid4().hex
                updates["watering_start_source"] = "remote"
                if duration > 0:
                    updates["planned_end_time"] = (now + timedelta(minutes=duration)).strftime("%Y-%m-%d %H:%M:%S")
                    # The scheduler captures this token when jobs are planted.
                    # Make the activation durable before any callback exists.
                    persisted = _audited_zone_update(zid, updates, snapshot=zone)
                    try:
                        if not persisted:
                            continue
                        scheduler = _get_scheduler_fn()
                        if scheduler:
                            scheduler.cancel_zone_jobs(zid)
                            scheduler.schedule_zone_stop(zid, duration, command_id=str(time.time_ns()))
                            try:
                                scheduler.schedule_zone_hard_stop(
                                    zid,
                                    now + timedelta(minutes=duration),
                                    activation_token=updates["command_id"],
                                )
                            except TypeError:
                                scheduler.schedule_zone_hard_stop(zid, now + timedelta(minutes=duration))
                            try:
                                scheduler.schedule_zone_cap(
                                    zid,
                                    activation_token=updates["command_id"],
                                )
                            except TypeError:
                                # Backwards-compatible while rolling upgrades
                                # still have the pre-token scheduler object.
                                scheduler.schedule_zone_cap(zid)
                    except (ValueError, TypeError, KeyError, RuntimeError) as e:
                        logger.debug("sse_hub: remote auto-stop schedule failed zone=%s: %s", zid, e)
            # Own/retained ON echoes for an active run only confirm observation.
            # In particular they never cancel or reset override/hard-stop jobs.
        if not persisted:
            try:
                _audited_zone_update(zid, updates, snapshot=zone)
            except (sqlite3.Error, OSError) as e:
                logger.debug("sse_hub: zone update failed zone=%s: %s", zid, e)

        event = {
            "zone_id": zid,
            "topic": normalized,
            "payload": payload,
            "state": observed_state,
        }
        if counter_required:
            event["counter_off_handled"] = bool(counter_handled)
        if retained:
            event["retained"] = True
        broadcast(json.dumps(event))


def _event_worker() -> None:
    while True:
        sid, source_client, topic, payload, retained, received_at = _SSE_EVENT_QUEUE.get()
        try:
            _process_mqtt_message(sid, source_client, topic, payload, retained, received_at)
        except Exception:  # A malformed event must not terminate the permanent safety worker.
            logger.exception("sse_hub: MQTT event worker failed sid=%s topic=%s", sid, topic)
        finally:
            _SSE_EVENT_QUEUE.task_done()


def _ensure_event_worker_started() -> None:
    global _SSE_EVENT_WORKER_STARTED
    with _SSE_EVENT_WORKER_LOCK:
        if _SSE_EVENT_WORKER_STARTED:
            return
        thread = threading.Thread(target=_event_worker, daemon=True, name="sse-mqtt-events")
        thread.start()
        _SSE_EVENT_WORKER_STARTED = True


def _enqueue_mqtt_message(sid: int, source_client, msg) -> None:
    _ensure_event_worker_started()
    topic = str(getattr(msg, "topic", "") or "")
    try:
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
    except (ValueError, TypeError, AttributeError):
        payload = str(getattr(msg, "payload", ""))
    item = (int(sid), source_client, topic, payload, bool(getattr(msg, "retain", False)), time.time())
    try:
        _SSE_EVENT_QUEUE.put_nowait(item)
    except queue.Full:
        # Relay state is level-triggered; keeping the newest state is safer than
        # blocking paho's keepalive thread behind a saturated DB worker.
        try:
            _SSE_EVENT_QUEUE.get_nowait()
            _SSE_EVENT_QUEUE.task_done()
        except queue.Empty:
            pass
        try:
            _SSE_EVENT_QUEUE.put_nowait(item)
        except queue.Full:
            logger.error("sse_hub: MQTT event queue saturated; newest event dropped topic=%s", topic)


# ---------------------------------------------------------------------------
# MQTT client lifecycle
# ---------------------------------------------------------------------------


def _server_key(server: dict) -> tuple:
    fields = (
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
    )
    return tuple(server.get(field) for field in fields)


def _topics_for_server(zone_topics: dict, mv_topics: dict, sid: int) -> set[str]:
    return set(zone_topics.get(int(sid), {})) | set(mv_topics.get(int(sid), {}))


def _subscribe_topics(client, topics: set[str], sid: int) -> set[str]:
    accepted: set[str] = set()
    for topic in sorted(topics):
        try:
            result = client.subscribe(topic, qos=1)
            if isinstance(result, (tuple, list)) and result and result[0] == 0:
                accepted.add(topic)
        except (ConnectionError, TimeoutError, OSError, RuntimeError, AttributeError) as e:
            logger.warning("sse_hub: subscribe failed sid=%s topic=%s: %s", sid, topic, e)
    return accepted


def _configure_tls(client, server: dict) -> None:
    if int(server.get("tls_enabled") or 0) != 1:
        return
    ca = server.get("tls_ca_path") or None
    cert = server.get("tls_cert_path") or None
    key = server.get("tls_key_path") or None
    tls_version = str(server.get("tls_version") or "").upper().strip()
    protocol = ssl.PROTOCOL_TLS_CLIENT if tls_version in ("", "TLS", "TLS_CLIENT") else ssl.PROTOCOL_TLS
    client.tls_set(ca_certs=ca, certfile=cert, keyfile=key, tls_version=protocol)
    if int(server.get("tls_insecure") or 0) == 1:
        client.tls_insecure_set(True)


def _create_mqtt_client(sid: int, server: dict, desired_topics: set[str]):
    client = _mqtt.Client(
        _mqtt.CallbackAPIVersion.VERSION2,
        client_id=(server.get("client_id") or None),
    )
    if server.get("username"):
        client.username_pw_set(server.get("username"), server.get("password") or None)
    _configure_tls(client, server)
    client._sse_desired_topics = set(desired_topics)

    def _on_message(cl, userdata, msg, sid_local=int(sid)):
        _enqueue_mqtt_message(sid_local, cl, msg)

    def _on_connect(cl, userdata, flags, reason_code, properties=None, sid_local=int(sid)):
        topics = set(getattr(cl, "_sse_desired_topics", set()))
        pre_subscribed = set(getattr(cl, "_sse_pre_subscribed", set()))
        cl._sse_pre_subscribed = set()
        _subscribe_topics(cl, topics - pre_subscribed, sid_local)
        logger.info("sse_hub: (re)subscribed %d topics on connect (sid=%s)", len(topics), sid_local)

    client.on_message = _on_message
    client.on_connect = _on_connect
    try:
        client.reconnect_delay_set(min_delay=1, max_delay=30)
    except (ValueError, AttributeError, OSError) as e:
        logger.debug("sse_hub reconnect_delay_set failed: %s", e)
    # 60 seconds is a keepalive value, not a socket-connect timeout. Heavy
    # message work is offloaded above, so the network loop remains responsive.
    host = server.get("host") or "127.0.0.1"
    port = int(server.get("port") or 1883)
    connect_async = getattr(client, "connect_async", None)
    if callable(connect_async):
        # paho's loop thread owns DNS/socket retries, so a broker that is down
        # during process boot will reconnect later without another web request.
        connect_async(host, port, 60)
    else:
        # Compatibility for older/fake paho clients. This remains outside the
        # hub lock and outside the application/request thread.
        client.connect(host, port, 60)
    # Queueing a SUBSCRIBE before loop_start preserves the historical API
    # contract and is safe: paho reports NO_CONN until it can accept the packet.
    # Accepted topics are skipped once in the first on_connect callback so a
    # retained ON is never requested twice.
    client._sse_pre_subscribed = _subscribe_topics(client, desired_topics, int(sid))
    return client


def _stop_clients(clients: list[tuple[int, object]]) -> None:
    for sid, client in clients:
        try:
            client.loop_stop()
        except (ConnectionError, TimeoutError, OSError, RuntimeError, AttributeError) as e:
            logger.debug("sse_hub: loop_stop sid=%s failed: %s", sid, e)
        try:
            client.disconnect()
        except (ConnectionError, TimeoutError, OSError, RuntimeError, AttributeError) as e:
            logger.debug("sse_hub: disconnect sid=%s failed: %s", sid, e)


def _apply_rebuild(target_generation: int) -> bool:
    """Build/connect outside the hub lock, then atomically publish one map."""
    zone_topics, mv_topics = _rebuild_subscriptions()
    desired_sids = set(zone_topics) | set(mv_topics)

    with _SSE_HUB_LOCK:
        old_clients = dict(_SSE_HUB_MQTT)
        old_keys = dict(_SSE_HUB_SERVER_KEYS)
        old_zone_topics = {sid: dict(topics) for sid, topics in _SSE_HUB_ZONE_TOPICS.items()}
        old_mv_topics = {sid: dict(topics) for sid, topics in _SSE_HUB_MV_TOPICS.items()}

    servers: dict[int, dict] = {}
    unavailable_sids: set[int] = set()
    for sid in desired_sids:
        server = _get_mqtt_server_safe(int(sid))
        if server is None:
            unavailable_sids.add(int(sid))
        elif int(server.get("enabled", 1) or 0) == 1:
            servers[int(sid)] = server

    candidates: dict[int, object] = {}
    candidate_keys: dict[int, tuple] = {}
    created: list[tuple[int, object]] = []
    if unavailable_sids:
        logger.error(
            "sse_hub: retiring previous authority for unreadable MQTT servers=%s",
            sorted(unavailable_sids),
        )
    for sid, server in servers.items():
        key = _server_key(server)
        current = old_clients.get(sid)
        if current is not None and old_keys.get(sid) == key:
            candidates[sid] = current
            candidate_keys[sid] = key
            continue
        try:
            client = _create_mqtt_client(sid, server, _topics_for_server(zone_topics, mv_topics, sid))
            candidates[sid] = client
            candidate_keys[sid] = key
            created.append((sid, client))
        except (ConnectionError, TimeoutError, OSError, RuntimeError, ValueError, AttributeError) as e:
            logger.warning("SSE hub MQTT client setup failed for server %s: %s", sid, e)
            # ``current`` belongs to a different connection authority because
            # equal keys were handled above. Keeping it after a failed
            # replacement grants stale host/credential/TLS authority,
            # including TLS -> TLS certificate rotations. Fail unsubscribed.

    with _SSE_HUB_LOCK:
        if target_generation != _SSE_HUB_REQUESTED_GENERATION:
            superseded = True
            stale: list[tuple[int, object]] = []
        else:
            superseded = False
            stale = [(sid, client) for sid, client in old_clients.items() if candidates.get(sid) is not client]
            _SSE_HUB_ZONE_TOPICS.clear()
            _SSE_HUB_ZONE_TOPICS.update(zone_topics)
            _SSE_HUB_MV_TOPICS.clear()
            _SSE_HUB_MV_TOPICS.update(mv_topics)
            _SSE_HUB_MQTT.clear()
            _SSE_HUB_MQTT.update(candidates)
            _SSE_HUB_SERVER_KEYS.clear()
            _SSE_HUB_SERVER_KEYS.update(candidate_keys)

    if superseded:
        _stop_clients(created)
        return False

    # A new client's callbacks become live only after its registry and topic
    # maps are atomically current. This prevents initial retained messages from
    # being discarded as stale between loop_start() and the map swap.
    for sid, client in created:
        if candidates.get(sid) is not client:
            continue
        try:
            client.loop_start()
        except (ConnectionError, TimeoutError, OSError, RuntimeError, AttributeError) as e:
            logger.warning("sse_hub: loop_start failed sid=%s: %s", sid, e)
            with _SSE_HUB_LOCK:
                if _SSE_HUB_MQTT.get(sid) is client:
                    _SSE_HUB_MQTT.pop(sid, None)
                    _SSE_HUB_SERVER_KEYS.pop(sid, None)

    # Update desired reconnect subscriptions and enqueue subscriptions only
    # after the atomic map swap. Existing clients stayed alive throughout, so
    # unchanged relay topics never have a no-subscriber reload window.
    created_ids = {id(client) for _, client in created}
    for sid, client in candidates.items():
        desired = _topics_for_server(zone_topics, mv_topics, sid)
        previous = _topics_for_server(old_zone_topics, old_mv_topics, sid)
        client._sse_desired_topics = set(desired)
        if id(client) in created_ids:
            # on_connect owns the initial subscribe; sending a second SUBSCRIBE
            # can make brokers redeliver retained ON and duplicate semantics.
            continue
        _subscribe_topics(client, desired - previous, sid)
        for topic in sorted(previous - desired):
            try:
                client.unsubscribe(topic)
            except (ConnectionError, TimeoutError, OSError, RuntimeError, AttributeError) as e:
                logger.debug("sse_hub: unsubscribe failed sid=%s topic=%s: %s", sid, topic, e)
    _stop_clients(stale)
    return True


def _retire_failed_rebuild_authority(target_generation: int) -> None:
    """Fail closed when a rebuild aborts before its normal atomic swap."""
    with _SSE_HUB_LOCK:
        if target_generation != _SSE_HUB_REQUESTED_GENERATION:
            return
        stale = list(_SSE_HUB_MQTT.items())
        _SSE_HUB_MQTT.clear()
        _SSE_HUB_SERVER_KEYS.clear()
        _SSE_HUB_ZONE_TOPICS.clear()
        _SSE_HUB_MV_TOPICS.clear()
    _stop_clients(stale)


def _rebuild_loop() -> None:
    global _SSE_HUB_APPLIED_GENERATION, _SSE_HUB_REBUILD_RUNNING, _SSE_HUB_STARTED
    while True:
        with _SSE_HUB_LOCK:
            target = _SSE_HUB_REQUESTED_GENERATION
        applied = False
        try:
            applied = _apply_rebuild(target)
        except Exception:  # Keep the lifecycle worker state consistent on unexpected dependency failures.
            logger.exception("sse_hub: subscription rebuild failed generation=%s", target)
            _retire_failed_rebuild_authority(target)
        with _SSE_HUB_LOCK:
            if applied:
                _SSE_HUB_APPLIED_GENERATION = target
                _SSE_HUB_STARTED = True
            if target == _SSE_HUB_REQUESTED_GENERATION:
                # A failed initial build is still considered started: paho setup
                # logged the fault, and an explicit reload can retry without an
                # HTTP request spawning competing generations.
                _SSE_HUB_STARTED = True
                _SSE_HUB_REBUILD_RUNNING = False
                return


def _schedule_rebuild(*, force: bool) -> None:
    global _SSE_HUB_REQUESTED_GENERATION, _SSE_HUB_REBUILD_RUNNING
    start_worker = False
    with _SSE_HUB_LOCK:
        if not force and (_SSE_HUB_STARTED or _SSE_HUB_REBUILD_RUNNING):
            return
        _SSE_HUB_REQUESTED_GENERATION += 1
        if not _SSE_HUB_REBUILD_RUNNING:
            _SSE_HUB_REBUILD_RUNNING = True
            start_worker = True
    if not start_worker:
        return
    try:
        threading.Thread(target=_rebuild_loop, daemon=True, name="sse-hub-rebuild").start()
    except (RuntimeError, OSError):
        with _SSE_HUB_LOCK:
            _SSE_HUB_REBUILD_RUNNING = False
        logger.exception("sse_hub: failed to start rebuild worker")


def ensure_hub_started() -> None:
    """Idempotently request permanent MQTT subscriptions and return quickly."""
    global _SSE_HUB_STARTED
    if _mqtt is None:
        return
    if _app_config and _app_config.get("TESTING"):
        with _SSE_HUB_LOCK:
            _SSE_HUB_STARTED = True
        return
    _ensure_event_worker_started()
    _schedule_rebuild(force=False)


def reload_hub() -> None:
    """Asynchronously apply fresh topic maps without a subscription gap."""
    global _SSE_HUB_STARTED
    with _SSE_HUB_LOCK:
        active = _SSE_HUB_STARTED or _SSE_HUB_REBUILD_RUNNING
    if not active:
        return

    # Preserve deterministic no-network semantics used by unit-test app
    # instances while still obeying the no-loop_stop-under-lock invariant.
    if _app_config and _app_config.get("TESTING"):
        with _SSE_HUB_LOCK:
            stale = list(_SSE_HUB_MQTT.items())
            _SSE_HUB_MQTT.clear()
            _SSE_HUB_SERVER_KEYS.clear()
            _SSE_HUB_ZONE_TOPICS.clear()
            _SSE_HUB_MV_TOPICS.clear()
            _SSE_HUB_STARTED = True
        _stop_clients(stale)
        return

    _schedule_rebuild(force=True)


# ---------------------------------------------------------------------------
# SSE client lifecycle
# ---------------------------------------------------------------------------


def _ensure_cleaner_started() -> None:
    """Start the background cleaner thread once."""
    global _SSE_CLEANER_STARTED
    with _SSE_HUB_LOCK:
        if _SSE_CLEANER_STARTED:
            return
        _SSE_CLEANER_STARTED = True

    def _clean_loop():
        while True:
            time.sleep(60)
            with _SSE_HUB_LOCK:
                count = len(_SSE_HUB_CLIENTS)
            if count > 0:
                logger.debug("SSE clients connected: %d", count)

    threading.Thread(target=_clean_loop, daemon=True, name="sse-cleaner").start()


def register_client() -> "queue.Queue":
    """Create/register a bounded SSE queue, evicting oldest clients safely."""
    _ensure_cleaner_started()
    evicted: list = []
    with _SSE_HUB_LOCK:
        while len(_SSE_HUB_CLIENTS) >= MAX_SSE_CLIENTS:
            evicted.append(_SSE_HUB_CLIENTS.pop(0))
        msg_queue = queue.Queue(maxsize=100)
        _SSE_HUB_CLIENTS.append(msg_queue)
    for oldest in evicted:
        _terminate_client(oldest)
        logger.info("SSE client evicted (limit %d reached)", MAX_SSE_CLIENTS)
    return msg_queue


def unregister_client(msg_queue: "queue.Queue") -> None:
    """Remove a client queue from the hub."""
    with _SSE_HUB_LOCK:
        try:
            _SSE_HUB_CLIENTS.remove(msg_queue)
        except ValueError as e:
            logger.debug("Client not in list during unregister: %s", e)
