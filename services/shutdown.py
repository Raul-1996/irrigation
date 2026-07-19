"""Graceful shutdown — send OFF to ALL zones and close master valves.

Called from signal handlers (SIGTERM/SIGINT) and atexit fallback.
Must never raise, never hang, never break existing functionality.
"""

import logging
import queue
import sqlite3
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

_shutdown_done = False
_shutdown_result: bool | None = None
_shutdown_lock = threading.Lock()


def _quiesce_scheduler(timeout_sec: float) -> bool:
    """Fence new watering starts and drain active scheduler runners.

    ``IrrigationScheduler.quiesce`` is the scheduler-owned lifecycle boundary.
    Older scheduler versions do not expose it, so ``stop`` remains a best-effort
    compatibility fallback until the scheduler package is integrated.
    """
    try:
        from irrigation_scheduler import get_scheduler

        scheduler = get_scheduler()
        if scheduler is None:
            return True
        quiesce = getattr(scheduler, "quiesce", None)
        if quiesce is not None:
            drained = bool(quiesce(timeout_seconds=timeout_sec))
            if not drained:
                logger.error("Shutdown: scheduler quiesce timed out after %.1fs", timeout_sec)
            return drained

        logger.warning("Shutdown: scheduler has no quiesce API; using non-draining stop fallback")
        scheduler.stop()
        return False
    except (ImportError, AttributeError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning("Shutdown: scheduler quiesce failed: %s", exc)
        return False


def _confirmed_publish(info, *, deadline: float) -> tuple[bool, str | None]:
    """Wait for a Paho MessageInfo without trusting silent timeout returns."""
    try:
        rc = getattr(info, "rc", None)
        if rc is None or int(rc) != 0:
            return False, f"publish rc={rc!r}"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, "global deadline exceeded before acknowledgement"
        info.wait_for_publish(timeout=remaining)
        if time.monotonic() > deadline:
            return False, "global deadline exceeded while awaiting acknowledgement"
        if not bool(info.is_published()):
            return False, "wait_for_publish returned without acknowledgement"
        return True, None
    except (AttributeError, RuntimeError, TypeError, ValueError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _run_publish_tasks(
    tasks: list[tuple[str, Callable[[], tuple[bool, str | None]]]],
    *,
    deadline: float,
) -> tuple[dict[str, tuple[bool, str | None]], set[str]]:
    """Run OFF tasks concurrently and collect them under one deadline."""
    result_queue: queue.Queue[tuple[str, bool, str | None]] = queue.Queue()

    def run_one(key: str, fn: Callable[[], tuple[bool, str | None]]) -> None:
        try:
            ok, reason = fn()
        except Exception as exc:  # worker boundary: shutdown must never raise
            ok, reason = False, f"{type(exc).__name__}: {exc}"
        result_queue.put((key, bool(ok), reason))

    pending = {key for key, _fn in tasks}
    for key, fn in tasks:
        threading.Thread(
            target=run_one,
            args=(key, fn),
            name=f"shutdown-off-{key}",
            daemon=True,
        ).start()

    completed: dict[str, tuple[bool, str | None]] = {}
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            key, ok, reason = result_queue.get(timeout=remaining)
        except queue.Empty:
            break
        completed[key] = (ok, reason)
        pending.discard(key)
    return completed, pending


def shutdown_all_zones_off(timeout_sec: float = 10, db=None) -> bool:
    """Send OFF (QoS 2, retain) to every zone and close master valves.

    - Idempotent: safe to call from both signal handler and atexit.
    - Never raises — all errors are logged as warnings and return ``False``.
    - Updates a zone to ``off`` only after both retained OFF publications are
      acknowledged by Paho.

    Args:
        timeout_sec: one global budget for scheduler drain and all MQTT acks.
        db: optional database handle; if None, imports the global singleton.

    Returns:
        ``True`` only when the scheduler drained and every configured physical
        OFF command was acknowledged before the deadline.
    """
    global _shutdown_done, _shutdown_result
    with _shutdown_lock:
        if _shutdown_done:
            return bool(_shutdown_result)
        _shutdown_done = True
        _shutdown_result = False

    try:
        timeout = max(0.0, float(timeout_sec))
    except (TypeError, ValueError):
        timeout = 0.0
    deadline = time.monotonic() + timeout

    # ── imports (late, to avoid circular) ───────────────────────────
    try:
        if db is None:
            from database import db as _default_db

            db = _default_db
    except ImportError:
        logger.warning("Shutdown: cannot import database")
        return False

    # A retained OFF is only final after every scheduler worker capable of
    # publishing ON has crossed a strict drain fence. Do this before reading
    # zones or sending the first safety command.
    remaining = max(0.0, deadline - time.monotonic())
    if not _quiesce_scheduler(remaining):
        logger.critical(
            "Shutdown: final OFF withheld because scheduler did not cross the drain fence; "
            "boot reconciliation will recover physical state"
        )
        return False

    try:
        from services.mqtt_pub import get_or_create_mqtt_client
        from utils import normalize_topic
    except ImportError:
        logger.warning("Shutdown: cannot import mqtt_pub / utils")
        return False

    # ── fetch zones ─────────────────────────────────────────────────
    zones = []
    try:
        zones = db.get_zones() or []
    except (sqlite3.Error, OSError, Exception) as exc:
        logger.warning("Shutdown: cannot read zones: %s", exc)
        return False

    # ── build physical OFF tasks ────────────────────────────────────
    failures: list[str] = []
    tasks: list[tuple[str, Callable[[], tuple[bool, str | None]]]] = []
    zone_results: dict[int, tuple[bool, str | None]] = {}
    zone_by_id: dict[int, dict] = {}

    for index, zone in enumerate(zones):
        try:
            zone_id = int(zone["id"])
            zone_by_id[zone_id] = zone
            sid_raw = zone.get("mqtt_server_id")
            topic_raw = str(zone.get("topic") or "").strip()
            if not sid_raw and not topic_raw:
                current_state = str(zone.get("state") or "").lower()
                zone_results[zone_id] = (
                    (True, None)
                    if current_state == "off"
                    else (False, f"active state {current_state!r} has no MQTT mapping")
                )
                continue
            if not sid_raw or not topic_raw:
                zone_results[zone_id] = (False, "incomplete MQTT mapping")
                continue
            server = db.get_mqtt_server(int(sid_raw))
            if not server:
                zone_results[zone_id] = (False, "MQTT server missing")
                continue
            topic = normalize_topic(topic_raw)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            failures.append(f"zone[{index}]: {type(exc).__name__}")
            continue

        def turn_zone_off(
            mqtt_server: dict = server,
            mqtt_topic: str = topic,
        ) -> tuple[bool, str | None]:
            client = get_or_create_mqtt_client(mqtt_server)
            if client is None:
                return False, "MQTT client unavailable"
            for target in (mqtt_topic, mqtt_topic + "/on"):
                info = client.publish(target, payload="0", qos=2, retain=True)
                ok, reason = _confirmed_publish(info, deadline=deadline)
                if not ok:
                    return False, f"{target}: {reason}"
            return True, None

        tasks.append((f"zone:{zone_id}", turn_zone_off))

    try:
        groups = db.get_groups() or []
    except (sqlite3.Error, OSError, Exception):
        groups = []
        failures.append("groups read failed")

    seen_masters: dict[tuple[int, str], str] = {}
    for index, group in enumerate(groups):
        try:
            if int(group.get("use_master_valve") or 0) != 1:
                continue
            group_id = int(group.get("id") or 0)
            sid_raw = group.get("master_mqtt_server_id")
            topic_raw = str(group.get("master_mqtt_topic") or "").strip()
            if not sid_raw or not topic_raw:
                failures.append(f"master group:{group_id or index}: incomplete MQTT mapping")
                continue
            sid = int(sid_raw)
            topic = normalize_topic(topic_raw)
            close_value = "1" if str(group.get("master_mode") or "NC").strip().upper() == "NO" else "0"
            key = (sid, topic)
            if key in seen_masters:
                if seen_masters[key] != close_value:
                    failures.append(f"master group:{group_id or index}: conflicting close mode")
                continue
            seen_masters[key] = close_value
            server = db.get_mqtt_server(sid)
            if not server:
                failures.append(f"master:{sid}:{topic}: MQTT server missing")
                continue
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            failures.append(f"master group:{index}: {type(exc).__name__}")
            continue

        def close_master(
            mqtt_server: dict = server,
            mqtt_topic: str = topic,
            value: str = close_value,
        ) -> tuple[bool, str | None]:
            client = get_or_create_mqtt_client(mqtt_server)
            if client is None:
                return False, "MQTT client unavailable"
            for target in (mqtt_topic, mqtt_topic + "/on"):
                info = client.publish(target, payload=value, qos=2, retain=True)
                ok, reason = _confirmed_publish(info, deadline=deadline)
                if not ok:
                    return False, f"{target}: {reason}"
            return True, None

        tasks.append((f"master:{sid}:{topic}", close_master))

    completed, pending = _run_publish_tasks(tasks, deadline=deadline)
    for key, result in completed.items():
        if key.startswith("zone:"):
            zone_results[int(key.split(":", 1)[1])] = result
        elif not result[0]:
            failures.append(f"{key}: {result[1] or 'failed'}")
    failures.extend(f"{key}: global deadline exceeded" for key in sorted(pending))
    if time.monotonic() > deadline and not pending:
        failures.append("global shutdown deadline exceeded")

    # ── update DB only for zones whose OFF was confirmed ────────────
    try:
        from services.zones_state import update_zone_state
    except ImportError:
        update_zone_state = None

    confirmed_zones = 0
    for zone_id, zone in zone_by_id.items():
        ok, reason = zone_results.get(zone_id, (False, "global deadline exceeded"))
        if not ok:
            failures.append(f"zone:{zone_id}: {reason or 'failed'}")
            continue
        updates = {
            "commanded_state": "off",
            "watering_start_time": None,
            "planned_end_time": None,
        }
        if str(zone.get("state") or "").lower() != "fault":
            updates["state"] = "off"
        try:
            if update_zone_state is None:
                updated = db.update_zone(zone_id, updates)
                if updated is None:
                    raise sqlite3.OperationalError("zone update returned None")
            else:
                update_zone_state(
                    zone_id,
                    updates,
                    audit_reason="graceful_shutdown_confirmed",
                    db=db,
                )
                persisted = db.get_zone(zone_id)
                expected_state = "fault" if str(zone.get("state") or "").lower() == "fault" else "off"
                if (
                    not persisted
                    or str(persisted.get("state") or "").lower() != expected_state
                    or str(persisted.get("commanded_state") or "").lower() != "off"
                    or persisted.get("watering_start_time") is not None
                    or persisted.get("planned_end_time") is not None
                ):
                    raise sqlite3.OperationalError("confirmed state update did not persist")
            confirmed_zones += 1
        except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError) as exc:
            failures.append(f"zone:{zone_id}: DB update {type(exc).__name__}")

    success = not failures
    _shutdown_result = success
    log = logger.info if success else logger.error
    log(
        "Shutdown: confirmed OFF for %d/%d zones and %d master valves; failures=%s",
        confirmed_zones,
        len(zone_by_id),
        len(seen_masters) - sum(1 for failure in failures if failure.startswith("master:")),
        failures,
    )
    return success


def reset_shutdown() -> None:
    """Reset the idempotency flag — for tests only."""
    global _shutdown_done, _shutdown_result
    with _shutdown_lock:
        _shutdown_done = False
        _shutdown_result = None
