import hashlib
import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Callable

from config import TESTING
from constants import MASTER_VALVE_CLOSE_DELAY_SEC, MAX_MANUAL_WATERING_MIN, ZONE_CAP_DEFAULT_MIN
from database import db
from services.locks import group_lock, zone_lock
from services.monitors import water_monitor
from services.mqtt_pub import publish_mqtt_value
from services.observed_state import state_verifier
from utils import SecretDecryptionError, normalize_topic

logger = logging.getLogger(__name__)


# Allowed presets for the % selector (Issue #12).
PERCENT_PRESETS = (50, 75, 100, 125, 150, 200)
# Fallback base when zone.duration is missing/corrupt (NULL/0/negative) and we
# need to multiply by a percent. Per spec section 4 / TL;DR.
PERCENT_FALLBACK_BASE_MIN = 15


def per_zone_dur(
    zone: dict, override_duration: int | None = None, override_percent: int | None = None
) -> tuple[int, list[str]]:
    """Compute the effective per-zone duration for manual run.

    Issue #12 — single source of truth for the %-of-norm calculation.
    Mirrors decisions in specs/issue-12-architecture.md sections 3.3 and 4.

    Precedence:
      1. ``override_duration`` (minutes mode) wins if set — returned verbatim.
      2. ``override_percent`` (percent mode) — multiply zone['duration']
         (the norm) by pct/100, round UP, clip to [1, MAX_MANUAL_WATERING_MIN].
         If norm <= 0, fall back to PERCENT_FALLBACK_BASE_MIN and emit
         'norm_not_set' warning.
      3. Neither: zone's own ``duration`` (existing default behaviour).

    Returns ``(duration_min, warnings)`` where warnings is a list of tags
    callers may surface to the client. Empty list when nothing notable.
    """
    warnings: list[str] = []
    if override_duration is not None:
        return int(override_duration), warnings
    if override_percent is not None:
        try:
            base = int(zone.get("duration") or 0)
        except (ValueError, TypeError):
            base = 0
        if base <= 0:
            base = PERCENT_FALLBACK_BASE_MIN
            warnings.append("norm_not_set")
        computed = math.ceil(base * int(override_percent) / 100.0)
        if computed < 1:
            computed = 1
            warnings.append("clipped_min")
        elif computed > MAX_MANUAL_WATERING_MIN:
            computed = MAX_MANUAL_WATERING_MIN
            warnings.append("clipped_max")
        return int(computed), warnings
    try:
        return int(zone.get("duration") or 0), warnings
    except (ValueError, TypeError):
        return 0, warnings


# Pending master-valve close timers keyed by physical master identity
# ``(mqtt_server_id, normalized_topic)``.
# Used to coalesce/cancel concurrent close attempts so that a freshly
# scheduled close supersedes a pending one for the same physical valve while
# equal topic strings on different brokers remain independent.
_PENDING_CLOSE_TIMERS = {}  # type: dict[tuple[int, str], threading.Timer]
_PENDING_CLOSE_LOCK = threading.Lock()
_MASTER_TOPIC_LOCKS: dict[tuple[int, str], threading.RLock] = {}
_MASTER_TOPIC_LOCKS_LOCK = threading.Lock()
_MASTER_ACTIVATIONS: dict[tuple[int, str], "MasterActivation"] = {}


@dataclass(frozen=True, slots=True)
class MasterActivation:
    """Durable identity of one manual physical master-valve activation."""

    group_id: int
    server_id: int
    topic: str
    mode: str
    token: str
    created_at: float


def master_identity(server_id: object, topic: object) -> tuple[int, str]:
    """Return the canonical identity of one physical master valve."""
    sid = int(server_id)
    try:
        normalized = normalize_topic(str(topic or ""))
    except (ValueError, TypeError, OSError):
        normalized = ""
    return sid, normalized


def master_topic_lock(server_id: object, topic: object | None = None) -> threading.RLock:
    """Return the process-local serialization lock for a physical master.

    Groups may intentionally share a master valve.  The topic, rather than a
    group id, is therefore part of the command-serialization boundary; the
    broker id is equally required because equal topics on separate brokers are
    different hardware.

    The one-argument compatibility form maps to broker ``0``.  Production
    callers use the two-argument physical identity.
    """
    identity = master_identity(0, server_id) if topic is None else master_identity(server_id, topic)
    with _MASTER_TOPIC_LOCKS_LOCK:
        lock = _MASTER_TOPIC_LOCKS.get(identity)
        if lock is None:
            lock = threading.RLock()
            _MASTER_TOPIC_LOCKS[identity] = lock
        return lock


def cancel_pending_master_close(server_id: object, topic: object | None = None) -> bool:
    """Atomically disarm the delayed close for one physical master."""
    identity = master_identity(0, server_id) if topic is None else master_identity(server_id, topic)
    with _PENDING_CLOSE_LOCK:
        pending = _PENDING_CLOSE_TIMERS.pop(identity, None)
        # Transitional compatibility for pending timers created by a process
        # image predating broker-aware keys.  New timers are never stored this
        # way.
        if pending is None:
            pending = _PENDING_CLOSE_TIMERS.pop(identity[1], None)
        if pending is not None:
            try:
                pending.cancel()
            except (RuntimeError, OSError):
                logger.debug("pending master close cancel failed identity=%s", identity)
            return True
    return False


def _master_activation_key(identity: tuple[int, str]) -> str:
    topic_digest = hashlib.sha256(identity[1].encode("utf-8")).hexdigest()
    return f"safety.master_activation.{identity[0]}.{topic_digest}"


def _load_master_activation_locked(identity: tuple[int, str]) -> MasterActivation | None:
    cached = _MASTER_ACTIVATIONS.get(identity)
    if cached is not None:
        return cached
    getter = getattr(db, "get_setting_value", None)
    if not callable(getter):
        return None
    raw = getter(_master_activation_key(identity))
    if not raw:
        return None
    try:
        payload = json.loads(str(raw))
        activation = MasterActivation(
            group_id=int(payload["group_id"]),
            server_id=int(payload["server_id"]),
            topic=normalize_topic(payload["topic"]),
            mode=str(payload["mode"]).strip().upper(),
            token=str(payload["token"]),
            created_at=float(payload["created_at"]),
        )
        uuid.UUID(hex=activation.token)
        if (activation.server_id, activation.topic) != identity or activation.mode not in {"NC", "NO"}:
            raise ValueError("master activation identity mismatch")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        logger.exception("invalid durable master activation identity=%s", identity)
        return None
    _MASTER_ACTIVATIONS[identity] = activation
    return activation


def _persist_master_activation_locked(activation: MasterActivation) -> bool:
    identity = (activation.server_id, activation.topic)
    setter = getattr(db, "set_setting_value", None)
    if callable(setter):
        try:
            if setter(_master_activation_key(identity), json.dumps(asdict(activation), sort_keys=True)) is False:
                return False
        except (sqlite3.Error, OSError, TypeError, ValueError):
            logger.exception("persist master activation failed identity=%s", identity)
            return False
    _MASTER_ACTIVATIONS[identity] = activation
    return True


def _clear_master_activation_locked(expected: MasterActivation) -> bool:
    identity = (expected.server_id, expected.topic)
    if _load_master_activation_locked(identity) != expected:
        return False
    setter = getattr(db, "set_setting_value", None)
    if callable(setter):
        try:
            if setter(_master_activation_key(identity), None) is False:
                return False
        except (sqlite3.Error, OSError, TypeError, ValueError):
            logger.exception("clear master activation failed identity=%s", identity)
            return False
    _MASTER_ACTIVATIONS.pop(identity, None)
    return True


def _cancel_master_activation_cap_locked(activation: MasterActivation) -> bool:
    try:
        from irrigation_scheduler import get_scheduler

        scheduler = get_scheduler()
        if scheduler is None:
            return True
        result = scheduler.cancel_master_valve_cap(
            activation.group_id,
            activation.server_id,
            activation.topic,
            activation.mode,
            activation.token,
        )
        return result is True
    except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
        logger.exception("cancel exact master cap failed activation=%s", activation)
        return False


def _complete_master_close_locked(identity: tuple[int, str], mode: str) -> bool:
    activation = _load_master_activation_locked(identity)
    if activation is None:
        return True
    if activation.mode != mode:
        logger.error(
            "master close mode does not match durable activation identity=%s current=%s requested=%s",
            identity,
            activation.mode,
            mode,
        )
        return False
    # Cancel while the physical-identity lock prevents a newer activation
    # from replacing this token. Only then clear durable ownership.
    if not _cancel_master_activation_cap_locked(activation):
        return False
    return _clear_master_activation_locked(activation)


def activate_manual_master_open(
    group_id: int,
    server_id: int,
    topic: str,
    mode: str,
    publish_command: Callable[[], bool],
    *,
    hours: int = 24,
) -> bool:
    """Persist UUID + plant an exact cap before one manual master OPEN."""
    identity = master_identity(server_id, topic)
    normalized_mode = str(mode or "NC").strip().upper()
    if not identity[1] or normalized_mode not in {"NC", "NO"}:
        return False
    with master_topic_lock(*identity):
        try:
            activation = MasterActivation(
                group_id=int(group_id),
                server_id=identity[0],
                topic=identity[1],
                mode=normalized_mode,
                token=uuid.uuid4().hex,
                created_at=time.time(),
            )
        except (TypeError, ValueError):
            logger.exception("manual master OPEN rejected invalid group identity group=%r", group_id)
            return False
        scheduler = None
        try:
            from irrigation_scheduler import get_scheduler

            scheduler = get_scheduler()
            if scheduler is None:
                raise RuntimeError("scheduler unavailable")
            planted = scheduler.schedule_master_valve_cap(
                activation.group_id,
                activation.server_id,
                activation.topic,
                activation.mode,
                activation.token,
                hours=int(hours),
            )
            if planted is not True:
                raise RuntimeError("master cap planting rejected")
        except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
            logger.exception("manual master OPEN blocked: exact cap unavailable activation=%s", activation)
            return False
        # Crash ordering: the token-unique new cap becomes durable first. The
        # previous activation/cap remains current until this settings commit;
        # after commit its eventual callback sees a stale token and no-ops.
        if not _persist_master_activation_locked(activation):
            try:
                cancelled = scheduler.cancel_master_valve_cap(
                    activation.group_id,
                    activation.server_id,
                    activation.topic,
                    activation.mode,
                    activation.token,
                )
                if cancelled is not True:
                    logger.critical("failed to cancel uncommitted master cap activation=%s", activation)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                logger.exception("cancel uncommitted master cap raised activation=%s", activation)
            return False
        cancel_pending_master_close(*identity)
        try:
            # An ACK failure is physically uncertain: retain token/cap so its
            # callback can still force and freshly confirm a later close.
            return bool(publish_command())
        except (ConnectionError, TimeoutError, OSError, RuntimeError, TypeError, ValueError):
            logger.exception("manual master OPEN publish failed activation=%s", activation)
            return False


def close_master_valve_confirmed(
    server_id: int,
    topic: str,
    mode: str,
    publish_command: Callable[[], bool],
) -> bool:
    """Subscribe, publish CLOSE, require fresh echo, then cancel exact cap."""
    identity = master_identity(server_id, topic)
    normalized_mode = str(mode or "NC").strip().upper()
    if not identity[1] or normalized_mode not in {"NC", "NO"}:
        return False
    close_value = "1" if normalized_mode == "NO" else "0"
    with master_topic_lock(*identity):
        if not state_verifier.verify_master_command(
            identity[0],
            identity[1],
            close_value,
            publish_command,
        ):
            return False
        # Preserve an existing delayed-close timer as retry authority until a
        # fresh physical CLOSED echo exists. Cancel it only inside this same
        # master transaction so it cannot race a newer activation/timer.
        cancel_pending_master_close(*identity)
        return _complete_master_close_locked(identity, normalized_mode)


def close_master_valve_if_activation(
    group_id: int,
    server_id: int,
    topic: str,
    mode: str,
    activation_token: str,
) -> bool:
    """Cap callback: fresh-close only the exact still-current activation."""
    identity = master_identity(server_id, topic)
    normalized_mode = str(mode or "NC").strip().upper()
    if not identity[1] or normalized_mode not in {"NC", "NO"}:
        return False
    try:
        expected = MasterActivation(
            group_id=int(group_id),
            server_id=identity[0],
            topic=identity[1],
            mode=normalized_mode,
            token=str(activation_token),
            created_at=0.0,
        )
    except (TypeError, ValueError):
        return False
    with master_topic_lock(*identity):
        current = _load_master_activation_locked(identity)
        if current is None or (
            current.group_id,
            current.server_id,
            current.topic,
            current.mode,
            current.token,
        ) != (
            expected.group_id,
            expected.server_id,
            expected.topic,
            expected.mode,
            expected.token,
        ):
            logger.info("stale master cap callback ignored identity=%s token=%s", identity, activation_token)
            return True
        try:
            server = db.get_mqtt_server(identity[0])
        except (sqlite3.Error, OSError, SecretDecryptionError, TypeError, ValueError):
            logger.exception("master cap server unavailable identity=%s", identity)
            return False
        if not server:
            return False
        close_value = "1" if normalized_mode == "NO" else "0"
        return close_master_valve_confirmed(
            identity[0],
            identity[1],
            normalized_mode,
            lambda: publish_mqtt_value(
                server,
                identity[1],
                close_value,
                min_interval_sec=0.0,
                qos=2,
                retain=True,
                meta={"cmd": "master_cap_close", "activation": current.token},
            ),
        )


def confirm_master_closed_from_echo(
    server_id: int,
    topic: str,
    mode: str,
    *,
    received_at: float | None = None,
) -> bool:
    """Clear an exact manual activation only after a fresh physical echo."""
    identity = master_identity(server_id, topic)
    normalized_mode = str(mode or "NC").strip().upper()
    if not identity[1] or normalized_mode not in {"NC", "NO"}:
        return False
    with master_topic_lock(*identity):
        activation = _load_master_activation_locked(identity)
        if activation is not None and received_at is not None:
            try:
                stale_echo = float(received_at) < activation.created_at
            except (TypeError, ValueError):
                return False
            if stale_echo:
                logger.info(
                    "stale master CLOSED echo predates current activation identity=%s token=%s",
                    identity,
                    activation.token,
                )
                return False
        return _complete_master_close_locked(identity, normalized_mode)


def _schedule_master_close(group_dict: dict, immediate: bool = False) -> None:
    """Schedule (or perform immediately) a master-valve close for the given group.

    - Reads ``master_close_delay_sec`` from the group dict (falls back to
      ``MASTER_VALVE_CLOSE_DELAY_SEC``); ``immediate=True`` forces zero delay.
    - Cancels any pending close for the same master topic before scheduling.
    - When the timer fires (or immediately), checks zones across all groups
      sharing this master topic — counts both ``state == 'on'`` and
      ``state == 'starting'`` to avoid race conditions during transitions.
    - Skips scheduling under TESTING (mirrors prior behaviour).
    """
    try:
        if not group_dict:
            return
        try:
            if int(group_dict.get("use_master_valve") or 0) != 1:
                return
        except (ValueError, TypeError):
            return
        mtopic = (group_dict.get("master_mqtt_topic") or "").strip()
        msid = group_dict.get("master_mqtt_server_id")
        if not mtopic or not msid:
            return
        try:
            gid = int(group_dict.get("id") or 0)
        except (ValueError, TypeError):
            gid = 0
        try:
            _raw_delay = group_dict.get("master_close_delay_sec")
            delay = int(_raw_delay) if _raw_delay is not None else MASTER_VALVE_CLOSE_DELAY_SEC
        except (ValueError, TypeError):
            delay = MASTER_VALVE_CLOSE_DELAY_SEC
        delay = max(1, delay)
        if immediate:
            delay = 0

        identity = master_identity(msid, mtopic)
        _, t_norm = identity
        if not t_norm:
            logger.error("master close refused invalid/command-channel topic=%r", mtopic)
            return

        # Always log the plant intent so post-incident triage can see WHEN/HOW
        # the master-close timer was armed and from where (group, delay).
        logger.info(
            "master_close planted: gid=%s topic=%s delay=%ds immediate=%s",
            gid,
            t_norm,
            delay,
            immediate,
        )

        def _do_close_locked():
            try:
                # Check ON or STARTING zones across all groups sharing the same
                # physical master (broker + normalized topic).
                any_on = False
                blocking_zone_id = None
                for gg in db.get_groups() or []:
                    try:
                        if int(gg.get("use_master_valve") or 0) != 1:
                            continue
                        gg_topic = (gg.get("master_mqtt_topic") or "").strip()
                        gg_sid = gg.get("master_mqtt_server_id")
                        if not gg_topic or not gg_sid:
                            continue
                        if master_identity(gg_sid, gg_topic) != identity:
                            continue
                    except (ValueError, TypeError, OSError):
                        continue
                    for z2 in db.get_zones_by_group(int(gg.get("id"))) or []:
                        st = str(z2.get("state") or "").lower()
                        if st in ("on", "starting"):
                            any_on = True
                            blocking_zone_id = z2.get("id")
                            break
                    if any_on:
                        break
                if any_on:
                    logger.info(
                        "master close skipped: topic=%s blocked by zone=%s state=on/starting", t_norm, blocking_zone_id
                    )
                    return
                mserver = db.get_mqtt_server(int(msid))
                if not mserver:
                    logger.warning("master close skipped: topic=%s msid=%s server not found", t_norm, msid)
                    return
                try:
                    mode = (group_dict.get("master_mode") or "NC").strip().upper()
                except (ValueError, TypeError, KeyError):
                    mode = "NC"
                close_val = "1" if mode == "NO" else "0"
                ok = state_verifier.verify_master_command(
                    int(msid),
                    t_norm,
                    close_val,
                    lambda: publish_mqtt_value(
                        mserver,
                        t_norm,
                        close_val,
                        min_interval_sec=0.0,
                        qos=2,
                        retain=True,
                        meta={"cmd": "master_off"},
                    ),
                )
                if not ok:
                    # Issue #38: do NOT lie to the UI/DB. Without confirmed
                    # command-channel publish the relay may still
                    # be open. SSE-hub will write master_valve_observed='closed'
                    # only when it sees the real relay echo on the base topic.
                    logger.warning(
                        "master close fresh-echo verification FAILED — leaving master_valve_observed unchanged "
                        "gid=%s topic=%s",
                        gid,
                        t_norm,
                    )
                    return
                if not _complete_master_close_locked(identity, mode):
                    logger.error("master close confirmed but exact activation cap cleanup failed identity=%s", identity)
                    return
                logger.info("master close verified: topic=%s val=%s mode=%s gid=%s", t_norm, close_val, mode, gid)
                # Always-on audit: delayed/auto master-valve close.
                # Important for triage of "why did watering stop early?" — links
                # the publish to the originating group and mode (NC/NO).
                try:
                    from services.audit import record_audit

                    record_audit(
                        action_type="master_valve_auto_close",
                        source="zone_control",
                        target=f"group:{int(gid)}" if gid else f"master_topic:{t_norm}",
                        payload={
                            "topic": t_norm,
                            "value": close_val,
                            "mode": mode,
                            "group_id": int(gid) if gid else None,
                        },
                        actor="system",
                    )
                except Exception:
                    logger.exception("master_valve_auto_close: record_audit failed")
                # Broker ACK proves only command delivery. The permanent base-
                # topic subscriber is the sole writer of physical observed
                # master state after a fresh relay echo.
            except Exception:
                logger.exception("master valve delayed close failed (topic=%s)", t_norm)

        def _do_close():
            # The scan and close publish are one master-topic transaction.
            # A concurrent start either becomes visible as state=starting and
            # blocks this close, or publishes master-open after this close.
            with master_topic_lock(msid, t_norm):
                _do_close_locked()

        # Replacement is one registry transaction.  Keeping timer creation
        # and start inside the lock prevents two callers from both observing
        # an empty slot and leaving an untracked live timer behind.
        with _PENDING_CLOSE_LOCK:
            prev = _PENDING_CLOSE_TIMERS.pop(identity, None)
            if prev is not None:
                try:
                    prev.cancel()
                except (RuntimeError, OSError):
                    logger.debug("pending master close cancel failed topic=%s", t_norm)
            if TESTING:
                return
            timer = threading.Timer(0.0 if delay <= 0 else float(delay), _do_close)
            timer.daemon = True
            _PENDING_CLOSE_TIMERS[identity] = timer
            timer.start()
    except (RuntimeError, OSError, ValueError, TypeError):
        logger.exception("schedule master close failed")


# Canonical state-write helper lives in services/zones_state.py to avoid the
# circular-import problem (sse_hub / observed_state want to emit audited
# state transitions but those modules are imported by zone_control itself).
# zone_control keeps a thin alias so existing internal callers (and any
# downstream code that imports services.zone_control._versioned_update)
# continue to work unchanged.
import contextlib

from services.zones_state import update_zone_state_internal as _update_zone_state_internal


def _versioned_update(zone_id: int, updates: dict, *, audit_reason: str = "") -> tuple[bool, dict | None]:
    """Apply an internal transition against a freshly read row snapshot."""
    snapshot = db.get_zone(int(zone_id))
    if not snapshot:
        return False, None
    return _update_zone_state_internal(
        zone_id,
        updates,
        snapshot=snapshot,
        audit_reason=audit_reason,
        db=db,
    )


def _is_valid_start_state(state: str) -> bool:
    s = str(state or "").lower()
    return s in ("off", "stopping")


def _is_valid_stop_state(state: str) -> bool:
    s = str(state or "").lower()
    return s in ("on", "starting")


def mark_zone_command_fault(
    zone_id: int,
    expected: str,
    *,
    reason: str,
    activation_token: str | None = None,
) -> bool:
    """Persist a physically-unknown command result without claiming success.

    This is the synchronous command-side complement to StateVerifier's
    timeout fault.  It is public so scheduler/group command owners can use the
    same state contract when they migrate to the centralized primitives.
    """
    with zone_lock(int(zone_id)):
        zone = db.get_zone(int(zone_id)) or {}
        if not zone:
            return False
        expected_activation = str(activation_token or "").strip() or None
        if expected_activation is not None:
            current_activation = zone.get("command_id") or zone.get("watering_start_time")
            if str(current_activation or "") != expected_activation:
                logger.info(
                    "stale command fault ignored zone=%s expected_activation=%s current=%s",
                    zone_id,
                    expected_activation,
                    current_activation,
                )
                return False
        fields = {
            "state": "fault",
            "commanded_state": "on" if str(expected).lower() in ("on", "1") else "off",
            "fault_count": int(zone.get("fault_count") or 0) + 1,
            "last_fault": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        # A command-path failure is not new physical evidence.  Preserve the
        # most recent observed relay value (including confirmed ON/OFF) rather
        # than rewriting it to an invented "unconfirmed" state.
        applied, _current = _update_zone_state_internal(
            int(zone_id),
            fields,
            snapshot=zone,
            audit_reason=f"mqtt_publish_failed_{reason}",
            db=db,
        )
        if applied:
            try:
                state_verifier.invalidate_verifiers(int(zone_id))
            except (AttributeError, ValueError, TypeError):
                logger.debug("command-fault verifier invalidation failed zone=%s", zone_id)
    if not applied:
        logger.warning(
            "zone command fault CAS rejected: zone=%s expected=%s reason=%s",
            zone_id,
            expected,
            reason,
        )
        return False
    logger.critical(
        "zone command publish failed: zone=%s expected=%s reason=%s; state pinned to fault",
        zone_id,
        expected,
        reason,
    )
    return True


def _finish_open_zone_run_failed(zone_id: int, *, db_instance=None) -> bool:
    """Close a command-created run as failed without inventing water stats."""
    repo = db if db_instance is None else db_instance
    try:
        run = repo.get_open_zone_run(int(zone_id))
        if not run:
            return True
        finished = repo.finish_zone_run(
            int(run["id"]),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            time.monotonic(),
            None,
            None,
            None,
            status="failed",
        )
        if finished is not True:
            logger.error("failed zone_run close was rejected zone=%s run=%s", zone_id, run.get("id"))
            return False
        return True
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        logger.exception("failed to close unsuccessful zone_run zone=%s", zone_id)
        return False


def _compensate_start_ack_cas_conflict(
    zone_id: int,
    *,
    server: dict,
    server_id: int,
    topic: str,
    activation_token: str,
    current: dict | None,
    group: dict,
    use_master: bool,
) -> None:
    """Fail-safe a relay whose ON publish outlived its persistence CAS.

    A newer command on the same physical channel owns the relay and must not
    be counter-stopped.  Otherwise the old captured channel is driven OFF,
    independently of later topology edits, and its matching activation is
    faulted without touching a newer row generation.
    """
    normalized_topic = normalize_topic(topic)
    current = current or {}
    try:
        same_channel = (
            int(current.get("mqtt_server_id") or 0) == int(server_id)
            and normalize_topic(str(current.get("topic") or "")) == normalized_topic
        )
    except (TypeError, ValueError, OSError):
        same_channel = False
    current_token = current.get("command_id") or current.get("watering_start_time")
    current_command = str(current.get("commanded_state") or "").lower()
    current_state = str(current.get("state") or "").lower()
    newer_owner = same_channel and (
        (
            str(current_token or "") != str(activation_token)
            and current_command == "on"
            and current_state in {"starting", "on"}
        )
        or (current_command == "off" and current_state in {"stopping", "off"})
    )
    if newer_owner:
        logger.warning(
            "exclusive_start_zone: newer command owns relay after ACK CAS conflict zone=%s token=%s",
            zone_id,
            current_token,
        )
        return

    faulted = mark_zone_command_fault(
        int(zone_id),
        "on",
        reason="mqtt_ack_on_cas_conflict",
        activation_token=activation_token,
    )

    def publish_counter_off() -> bool:
        return bool(
            publish_mqtt_value(
                server,
                normalized_topic,
                "0",
                min_interval_sec=0.0,
                qos=2,
                retain=True,
                meta={"cmd": "mqtt_ack_on_cas_conflict", "activation": activation_token},
            )
        )

    try:
        counter_confirmed = bool(
            state_verifier.verify_master_command(
                int(server_id),
                normalized_topic,
                "0",
                publish_counter_off,
            )
        )
    except (ConnectionError, TimeoutError, OSError, RuntimeError, TypeError, ValueError, AttributeError):
        logger.exception("exclusive_start_zone: counter-OFF verification raised zone=%s", zone_id)
        counter_confirmed = False
    if not counter_confirmed:
        try:
            counter_confirmed = publish_counter_off()
        except (ConnectionError, TimeoutError, OSError, RuntimeError, TypeError, ValueError):
            logger.exception("exclusive_start_zone: counter-OFF fallback raised zone=%s", zone_id)
            counter_confirmed = False
    if not counter_confirmed:
        logger.critical("exclusive_start_zone: counter-OFF unresolved after ACK CAS conflict zone=%s", zone_id)
    if use_master:
        _schedule_master_close(group, immediate=True)
    if faulted:
        _finish_open_zone_run_failed(int(zone_id))


def cancel_group_program_execution(group_id: int, scheduler=None) -> bool:
    """Cancel the in-memory sequencer without stopping the selected zone.

    Daily ``program_cancellations`` cannot represent an individual main or
    ``extra_times`` slot, so this primitive intentionally targets only the
    active runtime session.  It is used when an already-on program zone is
    manually extended: the program thread stops, while the selected relay is
    kept on and receives a fresh stop deadline.
    """
    gid = int(group_id)
    if scheduler is None:
        try:
            from irrigation_scheduler import get_scheduler

            scheduler = get_scheduler()
        except ImportError:
            scheduler = None
    if scheduler is None:
        return False
    cancelled = False
    try:
        event = getattr(scheduler, "group_cancel_events", {}).get(gid)
        if event is not None:
            event.set()
            cancelled = True
    except (AttributeError, KeyError, TypeError, ValueError):
        logger.exception("cancel active program event failed group=%s", gid)
    try:
        backend = getattr(scheduler, "scheduler", None)
        if backend is not None:
            for job in backend.get_jobs():
                if str(job.id).startswith(f"group_seq:{gid}:"):
                    job.remove()
                    cancelled = True
    except (AttributeError, RuntimeError, ValueError, KeyError):
        logger.debug("cancel active group sequence jobs failed group=%s", gid)
    return cancelled


def stop_group_peers(zone_id: int, *, reason: str = "manual_restart") -> bool:
    """Stop every non-selected zone in the selected zone's group."""
    selected = db.get_zone(int(zone_id))
    if not selected:
        return False
    gid = int(selected.get("group_id") or 0)
    ok = True
    for peer in db.get_zones_by_group(gid) or []:
        try:
            peer_id = int(peer.get("id"))
        except (ValueError, TypeError, KeyError):
            continue
        if peer_id == int(zone_id):
            continue
        current_peer = db.get_zone(peer_id) or {}
        if str(current_peer.get("state") or "").lower() == "off" and not _physical_zone_needs_off_confirmation(
            current_peer
        ):
            continue
        if not stop_zone(
            peer_id,
            reason=reason,
            force=True,
            require_observed_confirmation=bool(current_peer.get("mqtt_server_id")),
        ):
            ok = False
    return ok


def _physical_zone_needs_off_confirmation(zone: dict) -> bool:
    """Return whether start admission still lacks fresh physical OFF truth."""
    return bool(zone.get("mqtt_server_id")) and str(zone.get("observed_state") or "").lower() != "off"


def _emergency_stop_active() -> bool:
    """Read the live process emergency fence in HTTP and worker contexts."""
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            return bool(current_app.config.get("EMERGENCY_STOP"))
    except (ImportError, RuntimeError, TypeError):
        pass
    if TESTING:
        return False
    try:
        from services import sse_hub

        app_config = getattr(sse_hub, "_app_config", None)
        return bool(app_config and app_config.get("EMERGENCY_STOP"))
    except (ImportError, AttributeError, RuntimeError, TypeError):
        logger.exception("exclusive_start_zone: emergency fence read failed")
        return True


def _rain_group_blocked(group_id: int) -> bool:
    """Read the durable rain gate; missing older runtime is compatible-open."""
    try:
        from services.monitors import rain_monitor
    except ImportError:
        return False
    gate = getattr(rain_monitor, "is_group_blocked", None)
    if not callable(gate):
        return False
    try:
        return bool(gate(int(group_id)))
    except Exception:  # External monitor boundary: a read error must fail closed.
        logger.exception("exclusive_start_zone: rain admission read failed group=%s", group_id)
        return True


def _start_admission_blocked(group_id: int, cancel_guard: Callable[[], bool] | None = None) -> bool:
    if cancel_guard is not None:
        try:
            if bool(cancel_guard()):
                logger.warning("exclusive_start_zone blocked by scheduler cancel generation group=%s", group_id)
                return True
        except Exception:  # Cross-service guard boundary must fail closed.
            logger.exception("exclusive_start_zone: scheduler cancel guard failed group=%s", group_id)
            return True
    if _emergency_stop_active():
        logger.warning("exclusive_start_zone blocked by EMERGENCY_STOP group=%s", group_id)
        return True
    if _rain_group_blocked(group_id):
        logger.warning("exclusive_start_zone blocked by rain gate group=%s", group_id)
        return True
    return False


def _plant_activation_safety(
    zone_id: int,
    activation_token: str,
    duration_minutes: int,
) -> bool:
    """Persist hard/cap callbacks before any physical OPEN command."""
    if TESTING:
        return True
    try:
        from irrigation_scheduler import get_scheduler

        scheduler = get_scheduler()
        if scheduler is None:
            logger.error("activation safety unavailable: scheduler missing zone=%s", zone_id)
            return False
        run_at = datetime.now() + timedelta(minutes=max(1, int(duration_minutes)))
        hard_result = scheduler.schedule_zone_hard_stop(
            int(zone_id),
            run_at,
            activation_token=activation_token,
        )
        cap_result = scheduler.schedule_zone_cap(
            int(zone_id),
            cap_minutes=ZONE_CAP_DEFAULT_MIN,
            activation_token=activation_token,
        )
        return hard_result is not False and cap_result is not False
    except (ImportError, AttributeError, RuntimeError, ValueError, TypeError):
        logger.exception("activation safety planting failed zone=%s", zone_id)
        return False


def exclusive_start_zone(
    zone_id: int,
    source: str = "manual",
    *,
    safety_duration_minutes: int | None = None,
    cancel_guard: Callable[[], bool] | None = None,
) -> bool:
    """Start zone and stop others in its group. Returns True on success.

    ``source`` propagates to ``zone_runs.source`` so the history UI can
    distinguish program-triggered runs ('program') from manual API/UI
    runs ('manual'). Default 'manual' preserves prior behaviour for
    callers that don't pass it explicitly.
    """
    try:
        z = db.get_zone(zone_id)
        if not z:
            return False
        group_id = int(z.get("group_id") or 0)
        # Serialize on group
        with group_lock(group_id):
            # Topology CRUD takes the same group boundary. Re-read after lock
            # acquisition so a start that waited behind a rewire cannot command
            # the stale topic/server snapshot captured above. A concurrent
            # group move changes the required lock itself, so fail closed and
            # let the caller retry on the new group rather than nesting locks.
            fresh_zone = db.get_zone(zone_id)
            if not fresh_zone:
                return False
            fresh_group_id = int(fresh_zone.get("group_id") or 0)
            if fresh_group_id != group_id:
                logger.warning(
                    "exclusive_start_zone: topology changed while waiting for lock zone=%s old_group=%s new_group=%s",
                    zone_id,
                    group_id,
                    fresh_group_id,
                )
                return False
            z = fresh_zone
            if _start_admission_blocked(group_id, cancel_guard):
                return False
            group_zones = db.get_zones_by_group(group_id) if group_id else []
            # Validate the target before touching peers, but do not expose a
            # new STARTING/ON activation until every active peer has a physical
            # OFF confirmation.  This is the break-before-make boundary.
            with zone_lock(zone_id):
                current_target = db.get_zone(zone_id) or {}
                cur_state = str(current_target.get("state") or "").lower()
                if cur_state == "fault":
                    logger.warning("exclusive_start_zone blocked fault zone=%s", zone_id)
                    return False
                if cur_state in ("on", "starting"):
                    return True

            # A broker ACK from a previous OFF generation is not physical
            # truth.  Subscribe before publishing a fresh OFF and require a
            # non-retained report before allowing a new ON generation.  This
            # also establishes the invariant on a never-commanded physical
            # relay whose observed state is still unknown after startup.
            if _physical_zone_needs_off_confirmation(current_target):
                if not _stop_zone_locked(
                    int(zone_id),
                    reason="start_admission",
                    force=True,
                    skip_master_close=True,
                    require_observed_confirmation=True,
                    expected_group_id=group_id,
                ):
                    logger.error(
                        "exclusive_start_zone: target OFF unconfirmed; new activation blocked target=%s",
                        zone_id,
                    )
                    return False
                z = db.get_zone(zone_id)
                if not z or str(z.get("state") or "").lower() == "fault":
                    return False

            for peer in group_zones:
                try:
                    peer_id = int(peer.get("id"))
                except (ValueError, TypeError, KeyError):
                    continue
                if peer_id == int(zone_id):
                    continue
                current_peer = db.get_zone(peer_id) or {}
                peer_state = str(current_peer.get("state") or "").lower()
                if peer_state == "off" and not _physical_zone_needs_off_confirmation(current_peer):
                    continue
                if not _stop_zone_locked(
                    peer_id,
                    reason="peer_handover",
                    force=True,
                    skip_master_close=True,
                    require_observed_confirmation=True,
                    expected_group_id=group_id,
                ):
                    logger.error(
                        "exclusive_start_zone: peer OFF unconfirmed; target remains closed target=%s peer=%s",
                        zone_id,
                        peer_id,
                    )
                    return False

            # Only after all peer relays are physically OFF may this activation
            # enter STARTING and open its master/target channels.
            if _start_admission_blocked(group_id, cancel_guard):
                return False
            start_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            activation_token = uuid.uuid4().hex
            command_generation = None
            with zone_lock(zone_id):
                persisted, _previous = _versioned_update(
                    zone_id,
                    {
                        "state": "starting",
                        "commanded_state": "on",
                        "observed_state": "unconfirmed",
                        "watering_start_time": start_ts,
                        "command_id": activation_token,
                    },
                    audit_reason="manual_start",
                )
                persisted_zone = db.get_zone(zone_id) or {}
                if not persisted or persisted_zone.get("command_id") != activation_token:
                    logger.critical(
                        "exclusive_start_zone: activation token persistence failed zone=%s",
                        zone_id,
                    )
                    return False
                z = persisted_zone
                # Commit commanded_state first.  Advancing the in-memory
                # generation before a rejected CAS would retire the still-valid
                # previous verifier even though no new command was published.
                try:
                    command_generation = state_verifier.register_command(int(zone_id), "on")
                except (AttributeError, ValueError, TypeError):
                    logger.debug("start verifier generation registration failed zone=%s", zone_id)
            g = None
            try:
                # Снапшот счётчика воды на старте (если у группы есть счётчик)
                try:
                    gid = int(z.get("group_id") or 0)
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Exception in exclusive_start_zone: %s", e)
                    gid = 0
                if gid and gid != 999:
                    try:
                        g = next((gg for gg in (db.get_groups() or []) if int(gg.get("id")) == gid), None)
                    except (sqlite3.Error, OSError):
                        logger.exception("exclusive_start_zone: get_groups failed (zone=%s gid=%s)", zone_id, gid)
                        g = None
                    # ALWAYS open a zone_runs row — meter snapshot is best-effort.
                    # Pre-refactor this was gated on use_water_meter=1, leaving
                    # non-meter zones with no run history at all and forcing
                    # last_watering_time to live on the zones row. Now zone_runs
                    # is the single source of truth, so every start writes a row;
                    # meter columns stay NULL when the group has no meter.
                    raw: int | None = None
                    liters: int = 1
                    base_m3: float | None = None
                    if g and int(g.get("use_water_meter") or 0) == 1:
                        try:
                            raw = water_monitor.get_pulses_at_or_before(gid, time.time())
                            pulse = str(g.get("water_pulse_size") or "1l")
                            liters = 100 if pulse == "100l" else 10 if pulse == "10l" else 1
                            base_m3 = float(g.get("water_base_value_m3") or 0.0)
                        except (sqlite3.Error, OSError):
                            logger.exception("start meter snapshot failed (continuing without)")
                    try:
                        db.create_zone_run(
                            int(zone_id), gid, start_ts, time.monotonic(), raw, liters, base_m3, source=source
                        )
                    except (sqlite3.Error, OSError):
                        logger.exception("start: create_zone_run failed (zone=%s gid=%s)", zone_id, gid)
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in line_81: %s", e)
            try:
                sid = z.get("mqtt_server_id")
                topic = (z.get("topic") or "").strip()
                gid = int(z.get("group_id") or 0)
                if gid and gid != 999 and g is None:
                    try:
                        g = next((gg for gg in (db.get_groups() or []) if int(gg.get("id")) == gid), None)
                    except (sqlite3.Error, OSError):
                        logger.exception(
                            "exclusive_start_zone: master-valve pre-open: get_groups failed (zone=%s gid=%s)",
                            zone_id,
                            gid,
                        )
                        g = None
                use_master = bool(g and int(g.get("use_master_valve") or 0) == 1)
                # A persisted broker id is the authoritative marker that this
                # is a physical MQTT zone. Topic-only legacy rows remain
                # virtual for backwards compatibility; once a server id is
                # configured, a missing topic/server/secret must fail closed.
                physical_channel_configured = bool(sid)
                server = None
                normalized_topic = normalize_topic(topic) if topic else ""
                if sid and normalized_topic:
                    try:
                        server = db.get_mqtt_server(int(sid))
                    except SecretDecryptionError:
                        logger.exception("exclusive_start_zone: MQTT credentials unavailable zone=%s", zone_id)

                if not physical_channel_configured and not use_master:
                    # Only rows with neither half of an MQTT channel are truly
                    # virtual. A dangling server id, topic-only row, missing
                    # server, or undecryptable credentials is physical
                    # misconfiguration and must fail closed.
                    if _start_admission_blocked(group_id, cancel_guard):
                        with zone_lock(zone_id):
                            rolled_back, _current = _versioned_update(
                                zone_id,
                                {
                                    "state": "off",
                                    "commanded_state": "off",
                                    "observed_state": "off",
                                    "watering_start_time": None,
                                    "command_id": None,
                                },
                                audit_reason="virtual_start_admission_revoked",
                            )
                        if rolled_back:
                            _finish_open_zone_run_failed(zone_id)
                        return False
                    activated, _current = _versioned_update(
                        zone_id,
                        {"state": "on"},
                        audit_reason="virtual_zone_on",
                    )
                    if not activated:
                        logger.warning("exclusive_start_zone: virtual ON CAS conflicted zone=%s", zone_id)
                        return False
                elif not server:
                    if mark_zone_command_fault(
                        zone_id, "on", reason="missing_zone_channel", activation_token=activation_token
                    ):
                        _finish_open_zone_run_failed(zone_id)
                    return False
                else:
                    prepared_verification = None
                    try:
                        prepared_verification = state_verifier.prepare_verification(
                            int(zone_id),
                            "on",
                            generation=command_generation,
                        )
                    except (ConnectionError, TimeoutError, OSError, RuntimeError, ValueError, TypeError):
                        logger.exception("observed_state ON pre-subscribe failed zone=%s", zone_id)
                    if prepared_verification is None:
                        if mark_zone_command_fault(
                            zone_id, "on", reason="zone_on_subscribe_failed", activation_token=activation_token
                        ):
                            _finish_open_zone_run_failed(zone_id)
                        return False

                    safety_duration = (
                        int(safety_duration_minutes)
                        if safety_duration_minutes is not None
                        else int(z.get("duration") or 10)
                    )
                    if not _plant_activation_safety(zone_id, activation_token, safety_duration):
                        state_verifier.cancel_verification(prepared_verification)
                        if mark_zone_command_fault(
                            zone_id, "on", reason="activation_safety_unavailable", activation_token=activation_token
                        ):
                            _finish_open_zone_run_failed(zone_id)
                        return False

                    # Recheck both process-wide admission fences after all
                    # potentially slow preparation and immediately before the
                    # first physical OPEN command.
                    if _start_admission_blocked(group_id, cancel_guard):
                        state_verifier.cancel_verification(prepared_verification)
                        if mark_zone_command_fault(
                            zone_id, "on", reason="admission_revoked", activation_token=activation_token
                        ):
                            _finish_open_zone_run_failed(zone_id)
                        return False

                    # Pre-open master only after the selected zone's own
                    # control channel has been validated.  This prevents an
                    # uncommandable zone from leaving the main line pressurized.
                    if use_master:
                        mtopic = (g.get("master_mqtt_topic") or "").strip()
                        msid = g.get("master_mqtt_server_id")
                        try:
                            mserver = db.get_mqtt_server(int(msid)) if mtopic and msid else None
                        except SecretDecryptionError:
                            logger.exception("exclusive_start_zone: master credentials unavailable group=%s", gid)
                            mserver = None
                        normalized_master = normalize_topic(mtopic)
                        if not normalized_master or not mserver:
                            state_verifier.cancel_verification(prepared_verification)
                            if mark_zone_command_fault(
                                zone_id, "on", reason="missing_master_channel", activation_token=activation_token
                            ):
                                _finish_open_zone_run_failed(zone_id)
                            return False
                        try:
                            mode = (g.get("master_mode") or "NC").strip().upper()
                        except (ValueError, TypeError, KeyError) as e:
                            logger.debug("exclusive_start_zone: invalid master mode: %s", e)
                            mode = "NC"
                        if mode not in {"NC", "NO"}:
                            mode = "NC"
                        open_val = "0" if mode == "NO" else "1"
                        with master_topic_lock(msid, normalized_master):
                            if _start_admission_blocked(group_id, cancel_guard):
                                state_verifier.cancel_verification(prepared_verification)
                                if mark_zone_command_fault(
                                    zone_id,
                                    "on",
                                    reason="admission_revoked_before_master",
                                    activation_token=activation_token,
                                ):
                                    _finish_open_zone_run_failed(zone_id)
                                return False
                            cancel_pending_master_close(msid, normalized_master)
                            master_ok = publish_mqtt_value(
                                mserver,
                                normalized_master,
                                open_val,
                                min_interval_sec=0.0,
                                qos=2,
                                retain=True,
                            )
                        if not master_ok:
                            state_verifier.cancel_verification(prepared_verification)
                            if mark_zone_command_fault(
                                zone_id, "on", reason="master_open", activation_token=activation_token
                            ):
                                _finish_open_zone_run_failed(zone_id)
                            return False
                        # Do not optimistically publish physical observed truth
                        # from a command-channel ACK. SSE updates it only after
                        # the relay reports on the base topic.

                    # The admission fence can flip while the master publish is
                    # waiting for QoS ACK. Recheck under the still-held group
                    # lock immediately before target OPEN. If revoked, freshly
                    # confirm master CLOSE before returning.
                    if _start_admission_blocked(group_id, cancel_guard):
                        if use_master:
                            close_val = "1" if mode == "NO" else "0"
                            master_closed = close_master_valve_confirmed(
                                int(msid),
                                normalized_master,
                                mode,
                                lambda: publish_mqtt_value(
                                    mserver,
                                    normalized_master,
                                    close_val,
                                    min_interval_sec=0.0,
                                    qos=2,
                                    retain=True,
                                    meta={"cmd": "master_off", "src": "admission_revoked"},
                                ),
                            )
                            if not master_closed:
                                logger.critical(
                                    "admission revoked after master OPEN and fresh CLOSE unresolved group=%s identity=%s",
                                    group_id,
                                    master_identity(msid, normalized_master),
                                )
                        state_verifier.cancel_verification(prepared_verification)
                        if mark_zone_command_fault(
                            zone_id,
                            "on",
                            reason="admission_revoked_before_target",
                            activation_token=activation_token,
                        ):
                            _finish_open_zone_run_failed(zone_id)
                        return False

                    published = publish_mqtt_value(
                        server,
                        normalized_topic,
                        "1",
                        min_interval_sec=0.0,
                        qos=2,
                        retain=True,
                        meta={
                            "cmd": activation_token,
                            "ver": str((z.get("version") or 0) + 1),
                        },
                    )
                    if not published:
                        state_verifier.cancel_verification(prepared_verification)
                        if mark_zone_command_fault(zone_id, "on", reason="zone_on", activation_token=activation_token):
                            _finish_open_zone_run_failed(zone_id)
                        if use_master:
                            _schedule_master_close(g, immediate=True)
                        return False
                    activated, _current = _versioned_update(
                        zone_id,
                        {"state": "on"},
                        audit_reason="mqtt_ack_on",
                    )
                    if not activated:
                        state_verifier.cancel_verification(prepared_verification)
                        logger.warning("exclusive_start_zone: MQTT ACK ON CAS conflicted zone=%s", zone_id)
                        _compensate_start_ack_cas_conflict(
                            int(zone_id),
                            server=server,
                            server_id=int(sid),
                            topic=normalized_topic,
                            activation_token=activation_token,
                            current=_current,
                            group=g,
                            use_master=use_master,
                        )
                        return False
                    try:
                        state_verifier.verify_async(
                            int(zone_id),
                            "on",
                            generation=command_generation,
                            prepared=prepared_verification,
                        )
                    except (RuntimeError, ValueError, TypeError, KeyError, OSError):
                        logger.exception("observed_state verify_async(on) launch failed zone=%s", zone_id)
                        state_verifier.cancel_verification(prepared_verification)
                        _compensate_start_ack_cas_conflict(
                            int(zone_id),
                            server=server,
                            server_id=int(sid),
                            topic=normalized_topic,
                            activation_token=activation_token,
                            current=db.get_zone(int(zone_id)),
                            group=g,
                            use_master=use_master,
                        )
                        return False
            except (ConnectionError, TimeoutError, OSError):
                logger.exception("exclusive_start_zone: mqtt on failed")
                if mark_zone_command_fault(zone_id, "on", reason="exception", activation_token=activation_token):
                    _finish_open_zone_run_failed(zone_id)
                return False
        try:
            # publish event
            from services import events as _ev

            _ev.publish({"type": "zone_start", "id": int(zone_id), "by": "api"})
        except ImportError as e:
            logger.debug("Handled exception in line_168: %s", e)
        return True
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error, ValueError):
        logger.exception("exclusive_start_zone failed")
        return False


def _scheduler_group_stop_is_complete(group_id: int, expected_zone_ids: list[int], outcome: object) -> bool:
    """Validate the scheduler's exact physical-OFF aggregate for a new start."""
    required_keys = {
        "success",
        "group_id",
        "aggregate_valid",
        "stopped",
        "unresolved",
        "unverified_zone_ids",
        "retry_scheduled",
    }
    if type(outcome) is not dict or set(outcome) != required_keys:
        return False
    if (
        type(outcome["success"]) is not bool
        or type(outcome["aggregate_valid"]) is not bool
        or type(outcome["group_id"]) is not int
        or outcome["group_id"] != int(group_id)
        or type(outcome["retry_scheduled"]) is not bool
        or type(outcome["stopped"]) is not list
        or type(outcome["unresolved"]) is not list
        or type(outcome["unverified_zone_ids"]) is not list
    ):
        return False

    expected = list(expected_zone_ids)
    if any(type(zone_id) is not int or zone_id <= 0 for zone_id in expected) or len(set(expected)) != len(expected):
        return False
    partitions = (outcome["stopped"], outcome["unresolved"], outcome["unverified_zone_ids"])
    flattened = [zone_id for partition in partitions for zone_id in partition]
    if any(type(zone_id) is not int or zone_id <= 0 for zone_id in flattened):
        return False
    if len(flattened) != len(set(flattened)) or set(flattened) != set(expected):
        return False
    return (
        outcome["success"] is True
        and outcome["aggregate_valid"] is True
        and outcome["retry_scheduled"] is False
        and outcome["unresolved"] == []
        and outcome["unverified_zone_ids"] == []
        and outcome["stopped"] == expected
    )


def start_zone_orchestrated(
    zone_id: int,
    *,
    override_duration: int | None = None,
    override_percent: int | None = None,
    source: str = "manual",
    restart_if_on: bool = False,
) -> tuple[str, dict]:
    """Полная оркестрация ручного старта зоны вокруг ``exclusive_start_zone``.

    Единый путь для /api/zones/<id>/start, /api/zones/<id>/mqtt/start и
    /api/groups/<gid>/start-zone/<zid>: снятие заданий группы и отметка
    активного program-runner, делегирование ``exclusive_start_zone``
    (peer-off, мастер-клапан, MQTT, zone_run), аудируемая запись
    planned_end_time/watering_start_source и планирование auto-stop /
    hard-stop / cap.

    ``override_duration`` (минуты) имеет приоритет над ``override_percent``
    (валидация диапазонов — на вызывающей стороне; здесь значения считаются
    доверенными). Возвращает ``(status, ctx)``: status ∈ {"not_found",
    "already_on", "rescheduled", "failed", "started"}, ctx содержит
    "warnings" (list[str]) и "duration" (эффективная длительность, мин).
    """
    z = db.get_zone(zone_id)
    if not z:
        return "not_found", {"warnings": [], "duration": None}
    if str(z.get("state") or "").lower() == "fault":
        return "failed", {"warnings": ["zone_fault"], "duration": None}

    # ---- Optional one-time duration override ----
    # Carried as a local only — exclusive_start_zone does NOT consult
    # z['duration'] for any scheduling; base duration in DB stays untouched.
    override_dur = None
    warnings: list[str] = []
    if override_duration is not None:
        override_dur = int(override_duration)
        logger.info(
            "start_zone_orchestrated: zone %s using override duration %s min (base unchanged)", zone_id, override_dur
        )
    elif override_percent is not None:
        override_dur, warnings = per_zone_dur(z, None, int(override_percent))
        logger.info(
            "start_zone_orchestrated: zone %s using override percent %s%% -> %s min (warnings=%s)",
            zone_id,
            override_percent,
            override_dur,
            warnings,
        )

    # ---- Already-ON branch — reschedule stop, do NOT delegate ----
    # Delegating would peer-off siblings (none changed) and re-publish
    # ON (no-op with retain). The only useful effect of a re-start while
    # ON is updating the auto-stop time, so handle it here without a
    # new zone_run row. restart_if_on: /start и /start-zone исторически
    # перевзводили auto-stop на полную длительность зоны — повторный клик
    # «Старт» продлевает полив, а не делает молчаливый no-op.
    if str(z.get("state") or "") == "on":
        gid = int(z.get("group_id") or 0)
        sched = None
        try:
            from irrigation_scheduler import get_scheduler

            sched = get_scheduler()
            cancel_group_program_execution(gid, scheduler=sched)
        except (ImportError, ValueError, TypeError) as e:
            logger.debug("cancel active program before zone extension failed: %s", e)
        if not stop_group_peers(int(zone_id), reason="manual_restart"):
            return "failed", {"warnings": ["peer_stop_failed"], "duration": None}
        if override_dur is None and restart_if_on:
            override_dur = int(z.get("duration") or 0) or None
        if override_dur is not None:
            now_dt = datetime.now()
            new_end = (now_dt + timedelta(minutes=override_dur)).strftime("%Y-%m-%d %H:%M:%S")
            db.update_zone(
                int(zone_id),
                {
                    "planned_end_time": new_end,
                    "watering_start_time": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "watering_start_source": "manual",
                },
            )
            try:
                if sched:
                    # Remove existing stop jobs for THIS zone only —
                    # cancel_group_jobs would stop running peers.
                    try:
                        for job in sched.scheduler.get_jobs():
                            jid = str(job.id)
                            if jid.startswith(f"zone_stop:{int(zone_id)}:") or jid == f"zone_hard_stop:{int(zone_id)}":
                                job.remove()
                    except (RuntimeError, AttributeError, ValueError) as e:
                        logger.debug("remove old stop jobs: %s", e)
                    if not TESTING:
                        sched.schedule_zone_stop(int(zone_id), override_dur, command_id=str(int(time.time())))
                        sched.schedule_zone_hard_stop(
                            int(zone_id),
                            now_dt + timedelta(minutes=override_dur),
                            activation_token=z.get("command_id"),
                        )
            except (ImportError, ValueError, TypeError) as e:
                logger.debug("reschedule on override: %s", e)
            logger.info(
                "start_zone_orchestrated: zone %s already ON, rescheduled to %s min (end=%s)",
                zone_id,
                override_dur,
                new_end,
            )
            return "rescheduled", {"warnings": warnings, "duration": override_dur}
        return "already_on", {"warnings": warnings, "duration": None}

    # ---- Pre-delegate housekeeping: cancel scheduled program/stop jobs ----
    # cancel_group_jobs sets the cancel-event flag and removes APScheduler
    # jobs for the group (programs, peer zone_stops). It also calls
    # stop_all_in_group(force=True) — which the delegate's parallel
    # peer-off would do anyway, but we still need cancel_group_jobs for
    # the program/job-removal side effects.
    gid = int(z.get("group_id") or 0)
    try:
        from irrigation_scheduler import get_scheduler

        sched = get_scheduler()
        if not gid or sched is None:
            logger.error("start_zone_orchestrated: scheduler OFF barrier unavailable group=%s", gid)
            return "failed", {"warnings": [*warnings, "group_stop_unconfirmed"], "duration": None}
        stop_outcome = sched.cancel_group_jobs(int(gid))
        expected_zone_ids = _strict_group_zone_ids(int(gid))
        if (
            expected_zone_ids is None
            or int(zone_id) not in expected_zone_ids
            or not _scheduler_group_stop_is_complete(int(gid), expected_zone_ids, stop_outcome)
        ):
            logger.error(
                "start_zone_orchestrated: scheduler OFF aggregate rejected group=%s expected=%s outcome=%r",
                gid,
                expected_zone_ids,
                stop_outcome,
            )
            return "failed", {"warnings": [*warnings, "group_stop_unconfirmed"], "duration": None}
        try:
            db.reschedule_group_to_next_program(int(gid))
        except (sqlite3.Error, OSError) as e:
            logger.debug("start_zone_orchestrated: reschedule_group_to_next_program failed: %s", e)
    except Exception:
        logger.exception("start_zone_orchestrated: scheduler OFF barrier failed group=%s", gid)
        return "failed", {"warnings": [*warnings, "group_stop_unconfirmed"], "duration": None}

    # ---- Delegate to single source of truth ----
    # exclusive_start_zone does (in order, all under group/zone locks):
    #   1) state -> 'starting'
    #   2) db.create_zone_run(...)
    #   3) master-valve open (if group uses MV)
    #   4) MQTT publish '1' on zone topic
    #   5) state -> 'on'
    #   6) parallel peer-off (publish '0' + finish their zone_runs)
    # A failed MQTT command pins the zone to fault and closes the attempted
    # run as failed; it is never reported as a successful start.
    try:
        if override_dur is None:
            ok = exclusive_start_zone(int(zone_id), source=source)
        else:
            ok = exclusive_start_zone(
                int(zone_id),
                source=source,
                safety_duration_minutes=override_dur,
            )
    except (ValueError, TypeError, KeyError):
        logger.exception("start_zone_orchestrated: central start failed")
        return "failed", {"warnings": warnings, "duration": None}
    if not ok:
        return "failed", {"warnings": warnings, "duration": None}

    # ---- Post-delegate: planned_end_time + source, schedule auto-stop ----
    # exclusive_start_zone writes state/commanded_state/watering_start_time
    # but NOT planned_end_time or watering_start_source. Add them here so
    # the UI auto-stop timer / "manual" badge work.
    dur_min = override_dur if override_dur is not None else int(z.get("duration") or 10)
    now_dt = datetime.now()
    planned_end = (now_dt + timedelta(minutes=dur_min)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        updated, _snapshot = _versioned_update(
            int(zone_id),
            {"planned_end_time": planned_end, "watering_start_source": "manual"},
            audit_reason="manual_start_planned_end",
        )
        if not updated:
            logger.warning("start_zone_orchestrated: planned metadata CAS conflicted zone=%s", zone_id)
    except (sqlite3.Error, OSError, ImportError):
        logger.exception("start_zone_orchestrated: planned metadata CAS failed zone=%s", zone_id)

    # Schedule auto-stop synchronously (sched calls are non-blocking).
    try:
        from irrigation_scheduler import get_scheduler

        sched = get_scheduler()
        if sched and not TESTING:
            activation_token = (db.get_zone(int(zone_id)) or {}).get("command_id")
            sched.schedule_zone_stop(int(zone_id), dur_min, command_id=str(int(time.time())))
            sched.schedule_zone_hard_stop(
                int(zone_id),
                now_dt + timedelta(minutes=dur_min),
                activation_token=activation_token,
            )
            sched.schedule_zone_cap(
                int(zone_id),
                cap_minutes=ZONE_CAP_DEFAULT_MIN,
                activation_token=activation_token,
            )
    except (ImportError, ValueError, TypeError) as e:
        logger.debug("start_zone_orchestrated schedule stops: %s", e)

    return "started", {"warnings": warnings, "duration": dur_min}


def _finalize_water_stats(
    zone_id: int,
    z: dict,
    since_iso: str | None,
    *,
    log_label: str = "stop_zone",
    db_instance=None,
) -> bool:
    """Посчитать и сохранить водную статистику при остановке зоны.

    Быстрый путь — снапшот пульсов открытого zone_run (finish_zone_run);
    фоллбэк — summarize_run от ``since_iso`` (оригинальное время старта —
    summarize_run интегрирует пульсы начиная с него). Никогда не бросает
    исключений: log-and-continue, остановка зоны важнее статистики.
    """
    repo = db if db_instance is None else db_instance
    history_ok = True
    try:
        gid = int(z.get("group_id") or 0)
        total_liters = None
        avg_lpm = None
        if gid and gid != 999:
            try:
                run = repo.get_open_zone_run(int(zone_id))
            except (sqlite3.Error, OSError) as e:
                history_ok = False
                logger.error("%s: get_open_zone_run failed: %s", log_label, e)
                run = None
            if run:
                try:
                    # Берём пульсы на/после момента стопа, чтобы избежать лагов
                    end_raw = water_monitor.get_pulses_at_or_after(gid, time.time())
                except (ValueError, TypeError, AttributeError, OSError) as e:
                    logger.debug("%s: get_pulses_at_or_after failed: %s", log_label, e)
                    end_raw = None
                try:
                    start_raw = run.get("start_raw_pulses")
                    liters_per_pulse = int(run.get("pulse_liters_at_start") or 1)
                    end_mono = time.monotonic()
                    start_mono = float(run.get("start_monotonic") or 0.0)
                    dp = None if (end_raw is None or start_raw is None) else max(0, int(end_raw) - int(start_raw))
                    if dp is not None:
                        total_liters = round(dp * liters_per_pulse, 2)
                        dur_sec = max(1.0, end_mono - start_mono)
                        avg_lpm = round(total_liters / (dur_sec / 60.0), 2)
                    finished = repo.finish_zone_run(
                        int(run["id"]),
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        end_mono,
                        end_raw,
                        total_liters,
                        avg_lpm,
                        status="ok",
                    )
                    if finished is not True:
                        history_ok = False
                        logger.error("%s: finish_zone_run rejected run=%s", log_label, run.get("id"))
                except (sqlite3.Error, OSError):
                    history_ok = False
                    logger.exception("%s: finish snapshot failed", log_label)
            # Если снапшоты не дали результата — fallback к summarize_run.
            if (total_liters is None) and (avg_lpm is None) and since_iso:
                t_l, a_lpm = water_monitor.summarize_run(gid, since_iso)
                total_liters = t_l if t_l is not None else total_liters
                avg_lpm = a_lpm if a_lpm is not None else avg_lpm
        if (total_liters is not None) or (avg_lpm is not None):
            updates = {}
            if avg_lpm is not None:
                updates["last_avg_flow_lpm"] = avg_lpm
            if total_liters is not None:
                updates["last_total_liters"] = total_liters
            if updates:
                stats_row = repo.update_zone(int(zone_id), updates)
                if not stats_row:
                    history_ok = False
                    logger.error("%s: zone water-stat update rejected zone=%s", log_label, zone_id)
        return history_ok
    except (sqlite3.Error, OSError, ValueError, TypeError):
        logger.exception("%s: water stats update failed", log_label)
        return False


def _maybe_schedule_master_close(
    z: dict, master_close_immediately: bool, skip_master_close: bool, *, log_label: str = "stop_zone"
) -> None:
    """Запланировать закрытие мастер-клапана группы зоны (если он включён).

    Log-and-continue: любые ошибки поиска группы или планирования не должны
    ронять stop_zone.
    """
    try:
        gid = int(z.get("group_id") or 0)
        if not gid or gid == 999 or skip_master_close:
            return
        try:
            g = next((gg for gg in (db.get_groups() or []) if int(gg.get("id")) == gid), None)
        except (sqlite3.Error, OSError) as e:
            logger.debug("%s: get_groups failed: %s", log_label, e)
            g = None
        if g and int(g.get("use_master_valve") or 0) == 1:
            _schedule_master_close(g, immediate=bool(master_close_immediately))
    except (ValueError, TypeError, KeyError, ConnectionError, TimeoutError, OSError, RuntimeError):
        logger.exception("%s: master valve close scheduling failed", log_label)


def _cancel_completed_zone_cap(zone_id: int) -> None:
    """Disarm the cap only after one activation is physically complete."""
    try:
        from irrigation_scheduler import get_scheduler

        scheduler = get_scheduler()
        if scheduler is not None:
            scheduler.cancel_zone_cap(int(zone_id))
    except (ImportError, AttributeError, RuntimeError, ValueError, TypeError):
        logger.exception("cancel completed zone cap failed zone=%s", zone_id)


def stop_zone(
    zone_id: int,
    reason: str = "manual",
    force: bool = False,
    master_close_immediately: bool = False,
    skip_master_close: bool = False,
    require_observed_confirmation: bool = False,
    activation_token: str | None = None,
) -> bool:
    """Serialize an OFF command against starts in its current group.

    For a physical zone, ``True`` means the command was durably accepted.  The
    row remains ``stopping`` until a fresh relay report completes it.  Callers
    that require physical completion must pass ``require_observed_confirmation``.
    """
    try:
        zone = db.get_zone(int(zone_id))
        if not zone:
            return False
        gid = int(zone.get("group_id") or 0)
        with group_lock(gid):
            fresh_zone = db.get_zone(int(zone_id))
            if not fresh_zone:
                return False
            fresh_gid = int(fresh_zone.get("group_id") or 0)
            if fresh_gid != gid:
                logger.warning(
                    "stop_zone: topology changed while waiting for lock zone=%s old_group=%s new_group=%s",
                    zone_id,
                    gid,
                    fresh_gid,
                )
                return False
            expected_activation = str(activation_token or "").strip() or None
            if expected_activation is not None:
                current_activation = fresh_zone.get("command_id") or fresh_zone.get("watering_start_time")
                if str(current_activation or "") != expected_activation:
                    logger.info(
                        "stop_zone: stale activation owner ignored zone=%s expected=%s current=%s",
                        zone_id,
                        expected_activation,
                        current_activation,
                    )
                    return False
            return _stop_zone_locked(
                int(zone_id),
                reason=reason,
                force=force,
                master_close_immediately=master_close_immediately,
                skip_master_close=skip_master_close,
                require_observed_confirmation=require_observed_confirmation,
                expected_group_id=gid,
                expected_activation_token=expected_activation,
            )
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error, ValueError, TypeError):
        logger.exception("stop_zone serialization failed zone=%s", zone_id)
        return False


def _stop_zone_locked(
    zone_id: int,
    reason: str = "manual",
    force: bool = False,
    master_close_immediately: bool = False,
    skip_master_close: bool = False,
    require_observed_confirmation: bool = False,
    expected_group_id: int | None = None,
    expected_activation_token: str | None = None,
) -> bool:
    """Единый стоп зоны. Идемпотентно. Публикует OFF и фиксирует в БД.
    reason: для журналирования; force — останавливать даже если state уже off.
    master_close_immediately: при True мастер-клапан закрывается без задержки
    (используется для emergency_stop / rain).
    skip_master_close: при True мастер-клапан вообще не планируется к закрытию
    (вызывающий сам управляет master close, например emergency_stop_all Phase C).
    """
    # Audit-friendly entry log — captures WHO/WHY before any state mutation
    # so post-incident triage can replay the call from logs alone.
    logger.info(
        "stop_zone called: zone_id=%s reason=%s force=%s master_close_immediately=%s skip_master_close=%s",
        zone_id,
        reason,
        force,
        master_close_immediately,
        skip_master_close,
    )
    try:
        z = db.get_zone(zone_id)
        if not z:
            logger.info("stop_zone exit: zone_id=%s not found in DB", zone_id)
            return False
        current_group_id = int(z.get("group_id") or 0)
        if expected_group_id is not None and current_group_id != int(expected_group_id):
            logger.warning(
                "stop_zone: topology changed inside lock zone=%s expected_group=%s current_group=%s",
                zone_id,
                expected_group_id,
                current_group_id,
            )
            return False
        if expected_activation_token is not None:
            current_activation = z.get("command_id") or z.get("watering_start_time")
            if str(current_activation or "") != str(expected_activation_token):
                logger.info(
                    "stop_zone: activation changed inside lock zone=%s expected=%s current=%s",
                    zone_id,
                    expected_activation_token,
                    current_activation,
                )
                return False
        if reason == "auto" and str(z.get("watering_start_source") or "").lower() == "manual":
            logger.info(
                "stop_zone ignored stale program owner: zone_id=%s is now manual",
                zone_id,
            )
            return True
        initial_state = str(z.get("state") or "").lower()
        was_fault = initial_state == "fault"
        stop_activation = str(z.get("command_id") or z.get("watering_start_time") or "").strip() or None
        if initial_state == "stopping" and not force:
            logger.info(
                "stop_zone idempotent: zone_id=%s physical OFF still pending reason=%s",
                zone_id,
                reason,
            )
            # A duplicate request must not turn the broker ACK into historical
            # success.  The matching fresh relay OFF remains the sole owner of
            # zone_run finalisation and activation-token cleanup.
            _maybe_schedule_master_close(
                z,
                master_close_immediately,
                skip_master_close,
                log_label="stop_zone (pending off)",
            )
            return True
        physical_channel = bool(z.get("mqtt_server_id") and str(z.get("topic") or "").strip())
        fresh_physical_off = (
            str(z.get("observed_state") or "").lower() == "off" and str(z.get("commanded_state") or "").lower() == "off"
        )
        if initial_state == "off" and not force and (not physical_channel or fresh_physical_off):
            logger.info(
                "stop_zone idempotent: zone_id=%s already state=%s reason=%s — "
                "running water-stats finalisation but no MQTT publish",
                zone_id,
                z.get("state"),
                reason,
            )
            # Зона уже оффлайн (часто по MQTT). Тем не менее, попробуем посчитать и сохранить статистику воды.
            history_ok = _finalize_water_stats(
                zone_id,
                z,
                z.get("last_watering_time") or z.get("watering_start_time"),
                log_label="stop_zone (already off)",
            )
            if history_ok is not True:
                logger.error("stop_zone: already-OFF history finalization rejected zone=%s", zone_id)
                return False
            # Even when the zone was already off (idempotent path), the caller
            # may want to (re)schedule a master-valve close — required for
            # emergency_stop / rain monitor paths and for keeping idempotency
            # on duplicate manual stops.
            _maybe_schedule_master_close(
                z, master_close_immediately, skip_master_close, log_label="stop_zone (already off)"
            )
            return True
        if initial_state == "off" and not force:
            logger.warning(
                "stop_zone: logical OFF lacks fresh physical confirmation; reissuing zone=%s observed=%s",
                zone_id,
                z.get("observed_state"),
            )
        # `start_iso` is the original watering start — used as the lower bound
        # for summarize_run() water-stats fallback below (must NOT be touched).
        # The "last watering time" is now derived from zone_runs.end_utc;
        # we no longer write last_watering_time on the zones row.
        start_iso = z.get("watering_start_time")
        command_generation = None
        # Стейт: on/starting -> stopping
        with zone_lock(zone_id):
            if was_fault:
                transitioned, _current = _versioned_update(
                    zone_id,
                    {"commanded_state": "off", "observed_state": "unconfirmed"},
                    audit_reason=f"fault_stop_{reason}",
                )
            else:
                transitioned, _current = _versioned_update(
                    zone_id,
                    {
                        "state": "stopping",
                        "commanded_state": "off",
                        "observed_state": "unconfirmed",
                    },
                    audit_reason=f"stop_{reason}",
                )
            if transitioned:
                try:
                    command_generation = state_verifier.register_command(int(zone_id), "off")
                except (AttributeError, ValueError, TypeError):
                    logger.debug("stop verifier generation registration failed zone=%s", zone_id)
        if not transitioned:
            logger.warning("stop_zone: initial transition CAS conflicted zone=%s reason=%s", zone_id, reason)
            return False
        sid = z.get("mqtt_server_id")
        topic = (z.get("topic") or "").strip()
        try:
            if sid and topic:
                try:
                    server = db.get_mqtt_server(int(sid))
                except SecretDecryptionError:
                    logger.exception("stop_zone: MQTT credentials unavailable zone=%s", zone_id)
                    server = None
                if not server:
                    mark_zone_command_fault(
                        zone_id, "off", reason="missing_zone_server", activation_token=stop_activation
                    )
                    return False
                prepared_verification = None
                try:
                    prepared_verification = state_verifier.prepare_verification(
                        int(zone_id),
                        "off",
                        generation=command_generation,
                    )
                except (ConnectionError, TimeoutError, OSError, RuntimeError, ValueError, TypeError):
                    logger.exception("observed_state OFF pre-subscribe failed zone=%s", zone_id)
                if require_observed_confirmation and prepared_verification is None:
                    mark_zone_command_fault(
                        zone_id,
                        "off",
                        reason="peer_off_subscribe_failed",
                        activation_token=stop_activation,
                    )
                    return False
                # OFF публикуем с retain=True, чтобы состояние восстанавливалось после перезапуска
                published = publish_mqtt_value(
                    server,
                    normalize_topic(topic),
                    "0",
                    min_interval_sec=0.0,
                    qos=2,
                    retain=True,
                    meta={"cmd": "stop", "ver": str((z.get("version") or 0) + 1)},
                )
                if not published:
                    if prepared_verification is not None:
                        state_verifier.cancel_verification(prepared_verification)
                    mark_zone_command_fault(zone_id, "off", reason="zone_off", activation_token=stop_activation)
                    return False
                if require_observed_confirmation:
                    try:
                        confirmed = bool(
                            state_verifier.verify(
                                int(zone_id),
                                "off",
                                generation=command_generation,
                                prepared=prepared_verification,
                            )
                        )
                    except (ConnectionError, TimeoutError, OSError, RuntimeError, ValueError, TypeError):
                        logger.exception("observed_state synchronous OFF verification failed zone=%s", zone_id)
                        confirmed = False
                    if not confirmed:
                        if str((db.get_zone(zone_id) or {}).get("state") or "").lower() != "fault":
                            mark_zone_command_fault(
                                zone_id,
                                "off",
                                reason="peer_off_unconfirmed",
                                activation_token=stop_activation,
                            )
                        return False
                else:
                    # Normal stop stays low-latency; the dedicated verifier
                    # owns eventual physical confirmation/fault escalation.
                    try:
                        state_verifier.verify_async(
                            int(zone_id),
                            "off",
                            generation=command_generation,
                            prepared=prepared_verification,
                        )
                    except (RuntimeError, ValueError, TypeError, KeyError, OSError):
                        logger.exception("observed_state verify_async(off) launch failed zone=%s", zone_id)
                        state_verifier.cancel_verification(prepared_verification)
                        mark_zone_command_fault(
                            zone_id,
                            "off",
                            reason="peer_off_verify_launch_failed",
                            activation_token=stop_activation,
                        )
                        _maybe_schedule_master_close(
                            z,
                            True,
                            skip_master_close,
                            log_label="stop_zone (verify launch failed)",
                        )
                        return False
            elif sid:
                mark_zone_command_fault(
                    zone_id, "off", reason="incomplete_zone_channel", activation_token=stop_activation
                )
                return False
        except (ConnectionError, TimeoutError, OSError, sqlite3.Error):
            logger.exception("stop_zone: mqtt off failed")
            mark_zone_command_fault(zone_id, "off", reason="zone_off_exception", activation_token=stop_activation)
            return False
        # Delayed master valve close is a group concern and must run even for
        # DB-only/misconfigured zone channels after a completed stop path.
        _maybe_schedule_master_close(z, master_close_immediately, skip_master_close)
        if sid and topic:
            # QoS acknowledgement proves only that the broker accepted OFF.
            # State, run history and the completion event stay pending until
            # StateVerifier applies a matching fresh physical relay report.
            return True
        # Завершаем переход: stopping -> off.
        # last_watering_time is no longer a column — the open zone_run
        # row will be finished a few lines below and that becomes the
        # canonical history entry.
        with zone_lock(zone_id):
            completion_updates = {
                "state": "fault" if was_fault else "off",
                "planned_end_time": None,
            }
            completed, _current = _versioned_update(
                zone_id,
                completion_updates,
                audit_reason=f"stop_{reason}_complete",
            )
        if not completed:
            logger.warning("stop_zone: completion CAS conflicted zone=%s reason=%s", zone_id, reason)
            return False
        # Обновим статистику воды для зоны, если группа использует счётчик.
        # NOTE: summarize_run needs the ORIGINAL start time (start_iso)
        # as its lower bound — it integrates pulse counts since then.
        # Do NOT pass end_iso here.
        if was_fault:
            history_ok = _finish_open_zone_run_failed(zone_id)
        else:
            history_ok = _finalize_water_stats(zone_id, z, start_iso)
        if history_ok is not True:
            logger.error("stop_zone: history finalization rejected zone=%s reason=%s", zone_id, reason)
            return False
        # Token cleanup is the history commit's dependent mutation.  Keeping
        # it armed on a rejected history write preserves retry/cap ownership.
        with zone_lock(zone_id):
            latest = db.get_zone(int(zone_id))
            if not latest or (
                latest.get("command_id") != z.get("command_id")
                or latest.get("watering_start_time") != z.get("watering_start_time")
            ):
                logger.warning("stop_zone: activation changed before cleanup zone=%s", zone_id)
                return False
            cleaned, _current = _update_zone_state_internal(
                int(zone_id),
                {"watering_start_time": None, "command_id": None},
                snapshot=latest,
                audit_reason=f"stop_{reason}_cleanup",
                db=db,
            )
        if not cleaned:
            logger.warning("stop_zone: completion cleanup CAS conflicted zone=%s reason=%s", zone_id, reason)
            return False
        if not (sid and topic) or require_observed_confirmation:
            _cancel_completed_zone_cap(zone_id)
        try:
            db.add_log("zone_stop", f"{reason}: zone={int(zone_id)}")
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in line_345: %s", e)
        try:
            from services import events as _ev

            _ev.publish({"type": "zone_stop", "id": int(zone_id), "by": reason})
        except (ImportError, AttributeError) as e:
            logger.debug("Event publish failed: %s", e)
        return True
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error, ValueError):
        logger.exception("stop_zone failed")
        return False


def _strict_zone_ids(group_id: int | None = None) -> list[int] | None:
    """Read a complete zone partition without repository error collapsing.

    ``ZoneRepository.get_zones_by_group`` intentionally preserves its legacy
    ``[]``-on-SQLite-error contract. Safety callers cannot distinguish that
    from a legitimately empty group, so this central primitive uses the same
    repository connection boundary but lets read failures remain explicit.
    """
    repository = getattr(db, "zones", None)
    connector = getattr(repository, "_connect", None)
    if not callable(connector):
        logger.error("strict zone repository unavailable group=%s", group_id)
        return None
    try:
        with connector() as conn:
            conn.row_factory = sqlite3.Row
            if group_id is None:
                rows = conn.execute("SELECT id, group_id FROM zones ORDER BY id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, group_id FROM zones WHERE group_id = ? ORDER BY id",
                    (int(group_id),),
                ).fetchall()
        if not isinstance(rows, (list, tuple)):
            raise TypeError("strict group query returned a non-sequence")
        ids: list[int] = []
        seen: set[int] = set()
        for row in rows:
            row_keys = set(row.keys()) if hasattr(row, "keys") else set()
            if not {"id", "group_id"}.issubset(row_keys):
                raise TypeError("strict group query returned an invalid row")
            zone_id = int(row["id"])
            row_group_id = int(row["group_id"])
            wrong_group = group_id is not None and row_group_id != int(group_id)
            if zone_id <= 0 or wrong_group or zone_id in seen:
                raise ValueError("strict group query returned an invalid partition")
            ids.append(zone_id)
            seen.add(zone_id)
        return ids
    except (sqlite3.Error, OSError, AttributeError, KeyError, RuntimeError, TypeError, ValueError):
        logger.exception("strict zone partition read failed group=%s", group_id)
        return None


def _strict_group_zone_ids(group_id: int) -> list[int] | None:
    return _strict_zone_ids(int(group_id))


def stop_all_in_group(
    group_id: int,
    reason: str = "group_cancel",
    force: bool = False,
    master_close_immediately: bool = False,
    skip_master_close: bool = False,
    require_observed_confirmation: bool = False,
) -> dict:
    """Немедленно остановить все зоны в группе (идемпотентно).

    master_close_immediately: при True мастер-клапан закрывается без задержки.
    skip_master_close: при True мастер-клапан вообще не планируется
    (вызывающий сам управляет закрытием — например emergency_stop_all).

    Возвращает строгий aggregate ``{success, group_id, stopped, unresolved,
    retry_scheduled}``; callers must not report success or remove final safety
    jobs while ``unresolved`` is nonempty. Core never owns a deferred retry.
    """
    try:
        normalized_group_id = int(group_id)
    except (TypeError, ValueError):
        return {
            "success": False,
            "group_id": None,
            "stopped": [],
            "unresolved": [],
            "retry_scheduled": False,
        }
    result = {
        "success": True,
        "group_id": normalized_group_id,
        "stopped": [],
        "unresolved": [],
        # Core performs the immediate attempt only. A scheduler consumer may
        # truthfully flip this after it owns complete retry coverage.
        "retry_scheduled": False,
    }
    try:
        # Keep the expected-ID snapshot, all OFF attempts, and the final
        # partition validation under the same central group boundary used by
        # starts and topology mutations.
        with group_lock(normalized_group_id):
            expected_ids = _strict_group_zone_ids(normalized_group_id)
            if expected_ids is None:
                result["success"] = False
                return result
            for zone_id in expected_ids:
                stopped = False
                try:
                    stopped = stop_zone(
                        zone_id,
                        reason=reason,
                        force=force,
                        master_close_immediately=master_close_immediately,
                        skip_master_close=skip_master_close,
                        require_observed_confirmation=require_observed_confirmation,
                    )
                except (
                    ConnectionError,
                    TimeoutError,
                    OSError,
                    RuntimeError,
                    ValueError,
                    TypeError,
                    KeyError,
                    sqlite3.Error,
                ):
                    logger.exception("stop_all_in_group: stop_zone failed zone=%s", zone_id)
                target = result["stopped"] if stopped is True else result["unresolved"]
                target.append(zone_id)
                # Небольшая пауза, чтобы избежать всплесков при публикации на слабом железе (пропускаем в тестах)
                if not TESTING:
                    time.sleep(0.05)

            final_ids = _strict_group_zone_ids(normalized_group_id)
            if final_ids is None:
                result["success"] = False
                return result
            if final_ids != expected_ids:
                logger.error(
                    "stop_all_in_group: group partition changed during stop group=%s before=%s after=%s",
                    normalized_group_id,
                    expected_ids,
                    final_ids,
                )
                changed_ids = set(expected_ids) ^ set(final_ids)
                result["stopped"] = [zone_id for zone_id in result["stopped"] if zone_id not in changed_ids]
                result["unresolved"] = sorted(set(result["unresolved"]) | changed_ids)
                result["success"] = False
                return result
    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
        logger.exception("stop_all_in_group failed")
        result["success"] = False
        return result
    result["success"] = not result["unresolved"]
    return result


def _zone_has_fresh_physical_off(zone: dict) -> bool:
    """Return physical emergency truth, not broker/logical acknowledgement."""
    if not isinstance(zone, dict):
        return False
    if zone.get("mqtt_server_id"):
        return (
            str(zone.get("observed_state") or "").lower() == "off"
            and str(zone.get("commanded_state") or "").lower() == "off"
        )
    return str(zone.get("state") or "").lower() == "off"


def emergency_stop_all(reason: str = "emergency_stop") -> dict:
    """Синхронная аварийная остановка всех групп с детерминированной последовательностью.

    Phase A: для каждой группы → последовательно stop_zone(skip_master_close=True)
             для всех зон. Master-таймеры здесь не планируются вовсе — мастер
             закроем явно в фазе C.
    Phase B: ожидание до 2с свежего физического OFF. Если подтверждение не
             пришло — повторно force-стопаем зону с новой pre-subscription.
    Phase C: для каждого master valve → subscribe, SYNC publish close_val и
             ожидание свежего non-retained физического echo.

    Возвращает dict со счётчиками для логирования/диагностики.
    """
    stats = {
        "success": True,
        "errors": [],
        "groups_total": 0,
        "zones_stopped": 0,
        "zones_failed": [],
        "zones_force_retried": 0,
        "masters_closed": 0,
        "masters_skipped_no_use_master": 0,
        "masters_skipped_no_topic": 0,
        "masters_skipped_dup_topic": 0,
        "masters_failed_publish": 0,
        "zones_still_active_after_wait": 0,
    }
    groups: list[dict] = []
    try:
        group_snapshot = db.get_groups()
        if not isinstance(group_snapshot, (list, tuple)) or not group_snapshot:
            stats["errors"].append("groups:snapshot_empty_or_unavailable")
        else:
            for group in group_snapshot:
                try:
                    if not isinstance(group, dict) or int(group["id"]) <= 0:
                        raise ValueError("invalid group row")
                    groups.append(group)
                except (KeyError, TypeError, ValueError):
                    stats["errors"].append("groups:invalid_snapshot_row")
    except (sqlite3.Error, OSError, TypeError, ValueError):
        logger.exception("emergency_stop_all: get_groups failed")
        stats["errors"].append("groups:snapshot_empty_or_unavailable")

    # Zone OFF is independent of master/group reconciliation. A fail-soft
    # groups=[] read must never prevent closing relays that are still present
    # in the strict physical zone partition.
    expected_zone_ids = _strict_zone_ids()
    if expected_zone_ids is None:
        stats["errors"].append("zones:snapshot_unavailable")
        stats["success"] = False
        return stats

    stats["groups_total"] = len(groups)
    logger.info(
        "emergency_stop_all: starting phase A — stop %d zones; master snapshot has %d groups",
        len(expected_zone_ids),
        len(groups),
    )

    # Phase A: stop the complete zone snapshot WITHOUT scheduling any master
    # close. Group-only iteration can silently omit orphaned/reassigned zones.
    # Master will be closed synchronously in Phase C — we do NOT want lingering
    # delay=60 timers firing later and republishing close.
    for zid in expected_zone_ids:
        try:
            stopped = stop_zone(
                zid,
                reason=reason,
                force=True,
                master_close_immediately=False,
                skip_master_close=True,
                require_observed_confirmation=True,
            )
            current = db.get_zone(zid)
            if stopped and current and _zone_has_fresh_physical_off(current):
                stats["zones_stopped"] += 1
            elif zid not in stats["zones_failed"]:
                stats["zones_failed"].append(zid)
            if not TESTING:
                time.sleep(0.02)
        except (ValueError, TypeError, KeyError, sqlite3.Error, OSError):
            logger.exception("emergency_stop_all: stop_zone/readback failed zone_id=%s", zid)
            if zid not in stats["zones_failed"]:
                stats["zones_failed"].append(zid)

    # Phase B: wait up to 2s for every configured relay to prove physical OFF.
    if not TESTING:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            still_active = 0
            try:
                for zid in expected_zone_ids:
                    current = db.get_zone(zid)
                    if not current or not _zone_has_fresh_physical_off(current):
                        still_active += 1
            except (sqlite3.Error, OSError):
                logger.exception("emergency_stop_all: phase B check failed")
                break
            if still_active == 0:
                break
            time.sleep(0.1)
        # Re-issue stop_zone(force=True) for every unresolved relay at deadline.
        try:
            stuck_zones = []
            for zid in expected_zone_ids:
                current = db.get_zone(zid)
                if not current or not _zone_has_fresh_physical_off(current):
                    stuck_zones.append(zid)
            if stuck_zones:
                logger.warning(
                    "emergency_stop_all: %d zones stuck after 2s — force-retry: %s", len(stuck_zones), stuck_zones
                )
                for zid in stuck_zones:
                    if not zid:
                        continue
                    try:
                        stopped = stop_zone(
                            zid,
                            reason=reason + "_retry",
                            force=True,
                            master_close_immediately=False,
                            skip_master_close=True,
                            require_observed_confirmation=True,
                        )
                        stats["zones_force_retried"] += 1
                        current = db.get_zone(zid)
                        if stopped and current and _zone_has_fresh_physical_off(current):
                            if zid in stats["zones_failed"]:
                                stats["zones_failed"].remove(zid)
                                stats["zones_stopped"] += 1
                        elif zid not in stats["zones_failed"]:
                            stats["zones_failed"].append(zid)
                        if not TESTING:
                            time.sleep(0.02)
                    except (ValueError, TypeError, KeyError, sqlite3.Error, OSError):
                        logger.exception("emergency_stop_all: force-retry failed (zone_id=%s)", zid)
            stats["zones_still_active_after_wait"] = len(stuck_zones)
        except (sqlite3.Error, OSError):
            logger.exception("emergency_stop_all: phase B re-issue failed")

    # Reconcile the aggregate from fresh physical truth even in TESTING. A
    # mocked/broker-ACK success must never become HTTP emergency success while
    # a configured relay remains observed ON/unknown.
    confirmed_zone_ids: set[int] = set()
    unresolved_zone_ids: set[int] = set(stats["zones_failed"])
    try:
        latest_partition = _strict_zone_ids()
        if latest_partition is None:
            raise OSError("zone snapshot unavailable during reconciliation")
        latest_ids = set(latest_partition)
        initial_ids = set(expected_zone_ids)
        if latest_ids != initial_ids:
            stats["errors"].append("zones:partition_changed_during_emergency")
            unresolved_zone_ids.update(latest_ids - initial_ids)
            expected_zone_ids = sorted(initial_ids | latest_ids)
        for zid in expected_zone_ids:
            current = db.get_zone(zid)
            if current and _zone_has_fresh_physical_off(current):
                confirmed_zone_ids.add(zid)
                unresolved_zone_ids.discard(zid)
            else:
                unresolved_zone_ids.add(zid)
                if not current:
                    stats["errors"].append(f"zone:{zid}:readback_unavailable")
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        logger.exception("emergency_stop_all: physical zone reconciliation failed")
        stats["errors"].append("zones:physical_reconciliation_failed")
    stats["zones_stopped"] = len(confirmed_zone_ids)
    stats["zones_failed"] = sorted(unresolved_zone_ids)
    stats["zones_still_active_after_wait"] = len(unresolved_zone_ids)

    # Phase C: synchronously close each master valve (one publish per master topic)
    logger.info("emergency_stop_all: phase C — closing master valves")
    seen_topics = {}  # type: dict[tuple[int, str], str]  # physical identity -> mode
    for g in groups:
        try:
            gid = int(g.get("id") or 0)
        except (ValueError, TypeError):
            continue
        if not gid:
            continue
        try:
            use_mv = int(g.get("use_master_valve") or 0)
        except (ValueError, TypeError):
            use_mv = 0
        if use_mv != 1:
            stats["masters_skipped_no_use_master"] += 1
            continue
        mtopic = (g.get("master_mqtt_topic") or "").strip()
        msid = g.get("master_mqtt_server_id")
        if not mtopic or not msid:
            stats["masters_skipped_no_topic"] += 1
            stats["masters_failed_publish"] += 1
            logger.warning("emergency_stop_all: group=%s skipped — no master topic/server", gid)
            continue
        try:
            identity = master_identity(msid, mtopic)
            _, t_norm = identity
        except (ValueError, TypeError, OSError):
            stats["masters_failed_publish"] += 1
            continue
        if not t_norm:
            stats["masters_failed_publish"] += 1
            logger.warning("emergency_stop_all: invalid master report topic group=%s topic=%r", gid, mtopic)
            continue
        try:
            mode = (g.get("master_mode") or "NC").strip().upper()
        except (ValueError, TypeError, AttributeError):
            mode = "NC"
        # Cancel any pending timer for this topic so it can't override us later
        with _PENDING_CLOSE_LOCK:
            prev = _PENDING_CLOSE_TIMERS.pop(identity, None)
        if prev is not None:
            with contextlib.suppress(RuntimeError, OSError):
                prev.cancel()
        # Skip duplicate topics shared across groups (publish once per topic).
        # If a previously-seen group used a different master_mode, that's a
        # configuration smell — log a warning so ops can fix it.
        if identity in seen_topics:
            prev_mode = seen_topics[identity]
            if prev_mode != mode:
                logger.warning(
                    "emergency_stop_all: group=%s shares topic=%s with earlier "
                    "group but master_mode differs (this=%s vs first=%s) — "
                    "first wins, check group config",
                    gid,
                    t_norm,
                    mode,
                    prev_mode,
                )
            stats["masters_skipped_dup_topic"] += 1
            continue
        seen_topics[identity] = mode
        try:
            mserver = db.get_mqtt_server(int(msid))
        except (sqlite3.Error, OSError, ValueError, TypeError):
            logger.exception("emergency_stop_all: get_mqtt_server(%s) failed", msid)
            stats["masters_failed_publish"] += 1
            continue
        if not mserver:
            logger.warning("emergency_stop_all: group=%s msid=%s — server not found", gid, msid)
            stats["masters_failed_publish"] += 1
            continue
        close_val = "1" if mode == "NO" else "0"
        try:
            with master_topic_lock(msid, t_norm):
                ok = state_verifier.verify_master_command(
                    int(msid),
                    t_norm,
                    close_val,
                    lambda: publish_mqtt_value(
                        mserver,
                        t_norm,
                        close_val,
                        min_interval_sec=0.0,
                        qos=2,
                        retain=True,
                        meta={"cmd": "master_off", "src": "emergency"},
                    ),
                )
                if not ok:
                    # Issue #38: command-channel publish failed. Don't mark
                    # observed=closed — SSE-hub will heal from the real relay
                    # echo if/when the close lands later.
                    stats["masters_failed_publish"] += 1
                    logger.warning(
                        "emergency_stop_all: master close fresh-echo verification FAILED — group=%s topic=%s",
                        gid,
                        t_norm,
                    )
                    continue
                # Confirmation and exact cap/token cleanup are one physical-
                # identity transaction. A concurrent manual OPEN cannot replace
                # the activation between the fresh echo and this cleanup.
                if not _complete_master_close_locked(identity, mode):
                    stats["masters_failed_publish"] += 1
                    logger.error(
                        "emergency_stop_all: master physically closed but activation-cap cleanup failed identity=%s",
                        identity,
                    )
                    continue
            stats["masters_closed"] += 1
            logger.info(
                "emergency_stop_all: master physically closed — group=%s topic=%s val=%s mode=%s",
                gid,
                t_norm,
                close_val,
                mode,
            )
            # Physical master state remains unchanged until the permanent
            # subscriber receives a fresh base-topic echo.
            if not TESTING:
                time.sleep(0.05)
        except Exception:
            stats["masters_failed_publish"] += 1
            logger.exception("emergency_stop_all: publish failed group=%s topic=%s", gid, t_norm)

    stats["zones_failed"] = sorted(set(stats["zones_failed"]))
    stats["success"] = not stats["errors"] and not stats["zones_failed"] and stats["masters_failed_publish"] == 0
    logger.info("emergency_stop_all: done — %s", stats)
    return stats
