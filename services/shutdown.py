"""Graceful shutdown — send OFF to ALL zones and close master valves.

Called from signal handlers (SIGTERM/SIGINT) and atexit fallback.
Must never raise, never hang, never break existing functionality.
"""

import logging
import queue
import threading
import time
from datetime import datetime
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
    - Updates a zone to ``off`` only after its retained actuator command is
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
        from services.lifecycle_storage import (
            persist_confirmed_shutdown_off,
            run_bounded,
            strict_snapshot,
        )
        from services.mqtt_pub import get_or_create_mqtt_client
        from utils import normalize_topic
    except ImportError:
        logger.warning("Shutdown: cannot import lifecycle storage / mqtt_pub / utils")
        return False

    # ── strict bounded topology snapshot ────────────────────────────
    db_path = getattr(db, "db_path", None)
    if not isinstance(db_path, str) or not db_path:
        logger.error("Shutdown: strict lifecycle snapshot unavailable: db_path missing")
        return False
    snapshot_ok, snapshot, snapshot_error = run_bounded(
        lambda: strict_snapshot(db_path, deadline=deadline),
        deadline=deadline,
        name="strict topology snapshot",
    )
    if not snapshot_ok or snapshot is None:
        logger.error("Shutdown: %s", snapshot_error or "strict topology snapshot failed")
        return False
    zones = snapshot.zones
    groups = snapshot.groups
    servers = snapshot.servers

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
                if current_state != "off":
                    zone_results[zone_id] = (False, f"active state {current_state!r} has no MQTT mapping")
                    continue

                def normalize_unmapped_off(
                    zid: int = zone_id,
                    state: str = current_state,
                ) -> tuple[bool, str | None]:
                    end_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    persist_confirmed_shutdown_off(
                        db_path,
                        zid,
                        current_state=state,
                        deadline=deadline,
                        end_local=end_local,
                        end_monotonic=time.monotonic(),
                    )
                    return True, None

                tasks.append((f"zone:{zone_id}", normalize_unmapped_off))
                continue
            if not sid_raw or not topic_raw:
                zone_results[zone_id] = (False, "incomplete MQTT mapping")
                continue
            server = servers.get(int(sid_raw))
            if not server:
                zone_results[zone_id] = (False, "MQTT server missing")
                continue
            topic = normalize_topic(topic_raw)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            failures.append(f"zone[{index}]: {type(exc).__name__}")
            continue

        def turn_zone_off(
            mqtt_server: dict = server,
            mqtt_topic: str = topic,
            zid: int = zone_id,
            current_state: str = str(zone.get("state") or "").lower(),
        ) -> tuple[bool, str | None]:
            client = get_or_create_mqtt_client(mqtt_server)
            if client is None:
                return False, "MQTT client unavailable"
            target = mqtt_topic + "/on"
            info = client.publish(target, payload="0", qos=2, retain=True)
            ok, reason = _confirmed_publish(info, deadline=deadline)
            if not ok:
                return False, f"{target}: {reason}"
            end_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            persist_confirmed_shutdown_off(
                db_path,
                zid,
                current_state=current_state,
                deadline=deadline,
                end_local=end_local,
                end_monotonic=time.monotonic(),
            )
            return True, None

        tasks.append((f"zone:{zone_id}", turn_zone_off))

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
            server = servers.get(sid)
            if not server:
                failures.append(f"master:{sid}:{topic}: MQTT server missing")
                continue
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
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
            target = mqtt_topic + "/on"
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

    confirmed_zones = 0
    for zone_id in zone_by_id:
        ok, reason = zone_results.get(zone_id, (False, "global deadline exceeded"))
        if not ok:
            failures.append(f"zone:{zone_id}: {reason or 'failed'}")
            continue
        confirmed_zones += 1

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
