import logging
import math
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from config import TESTING
from constants import MASTER_VALVE_CLOSE_DELAY_SEC, MAX_MANUAL_WATERING_MIN, ZONE_CAP_DEFAULT_MIN
from database import db
from services.locks import group_lock, zone_lock
from services.monitors import water_monitor
from services.mqtt_pub import publish_mqtt_value
from services.observed_state import state_verifier
from utils import normalize_topic

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


# Pending master-valve close timers keyed by normalized master MQTT topic.
# Used to coalesce/cancel concurrent close attempts so that a freshly
# scheduled close supersedes a pending one for the same topic.
_PENDING_CLOSE_TIMERS = {}  # type: dict[str, threading.Timer]
_PENDING_CLOSE_LOCK = threading.Lock()


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

        try:
            t_norm = normalize_topic(mtopic)
        except (ValueError, TypeError, OSError):
            t_norm = mtopic

        # Always log the plant intent so post-incident triage can see WHEN/HOW
        # the master-close timer was armed and from where (group, delay).
        logger.info(
            "master_close planted: gid=%s topic=%s delay=%ds immediate=%s",
            gid,
            t_norm,
            delay,
            immediate,
        )

        def _do_close():
            try:
                # Check ON or STARTING zones across all groups sharing the same master topic
                any_on = False
                blocking_zone_id = None
                for gg in db.get_groups() or []:
                    try:
                        gg_topic = (gg.get("master_mqtt_topic") or "").strip()
                        if not gg_topic:
                            continue
                        if normalize_topic(gg_topic) != t_norm:
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
                ok = publish_mqtt_value(
                    mserver, t_norm, close_val, min_interval_sec=0.0, qos=2, retain=True, meta={"cmd": "master_off"}
                )
                if not ok:
                    # Issue #38: do NOT lie to the UI/DB. Without confirmed
                    # publish (base topic ack + '/on' ack) the relay may still
                    # be open. SSE-hub will write master_valve_observed='closed'
                    # only when it sees the real relay echo on the base topic.
                    logger.warning(
                        "master close publish FAILED — leaving master_valve_observed unchanged gid=%s topic=%s",
                        gid,
                        t_norm,
                    )
                    return
                logger.info("master close published: topic=%s val=%s mode=%s gid=%s", t_norm, close_val, mode, gid)
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
                if gid:
                    try:
                        db.update_group_fields(int(gid), {"master_valve_observed": "closed"})
                        import json as _json_c

                        from services import sse_hub as _sse_hub_c

                        _sse_hub_c.broadcast(_json_c.dumps({"mv_group_id": int(gid), "mv_state": "closed"}))
                    except (sqlite3.Error, OSError, ImportError, ValueError, TypeError) as e:
                        logger.debug("master_valve_observed update (closed) failed: %s", e)
            except Exception:
                logger.exception("master valve delayed close failed (topic=%s)", t_norm)

        # Cancel any pending close for this topic (covers both delayed and immediate paths)
        with _PENDING_CLOSE_LOCK:
            prev = _PENDING_CLOSE_TIMERS.pop(t_norm, None)
        if prev is not None:
            try:
                prev.cancel()
            except (RuntimeError, OSError):
                pass

        if TESTING:
            return

        if delay <= 0:
            # Run inline-but-non-blocking on a daemon thread to keep semantics
            # consistent (callers don't expect to block on master close).
            t = threading.Timer(0.0, _do_close)
            t.daemon = True
            with _PENDING_CLOSE_LOCK:
                _PENDING_CLOSE_TIMERS[t_norm] = t
            t.start()
            return

        timer = threading.Timer(float(delay), _do_close)
        timer.daemon = True
        with _PENDING_CLOSE_LOCK:
            _PENDING_CLOSE_TIMERS[t_norm] = timer
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

from services.zones_state import update_zone_state as _update_zone_state


def _versioned_update(zone_id: int, updates: dict, *, audit_reason: str = "") -> None:
    """Backwards-compatible thin wrapper around ``zones_state.update_zone_state``.

    Pre-existing callers in this module (and in tests) invoke
    ``_versioned_update`` and ignore the return value; we preserve that
    contract here while delegating the actual write + audit emit to the
    canonical helper.
    """
    _update_zone_state(zone_id, updates, audit_reason=audit_reason)


def _is_valid_start_state(state: str) -> bool:
    s = str(state or "").lower()
    return s in ("off", "stopping")


def _is_valid_stop_state(state: str) -> bool:
    s = str(state or "").lower()
    return s in ("on", "starting")


def exclusive_start_zone(zone_id: int, source: str = "manual") -> bool:
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
        # For diagnostics/meta: allow passing through a command id if set by callers in future
        command_id = None  # type: Optional[str]
        group_id = int(z.get("group_id") or 0)
        # Serialize on group
        with group_lock(group_id):
            group_zones = db.get_zones_by_group(group_id) if group_id else []
            start_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Start current with state-machine: off/stopping -> starting -> on
            with zone_lock(zone_id):
                cur_state = str((db.get_zone(zone_id) or {}).get("state") or "").lower()
                if cur_state in ("on", "starting"):
                    pass
                else:
                    _versioned_update(
                        zone_id,
                        {"state": "starting", "commanded_state": "on", "watering_start_time": start_ts},
                        audit_reason="manual_start",
                    )
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
                # Pre-open master valve by group (idempotent, mode-aware)
                if gid and gid != 999:
                    try:
                        g = next((gg for gg in (db.get_groups() or []) if int(gg.get("id")) == gid), None)
                    except (sqlite3.Error, OSError):
                        logger.exception(
                            "exclusive_start_zone: master-valve pre-open: get_groups failed (zone=%s gid=%s)",
                            zone_id,
                            gid,
                        )
                        g = None
                    if g and int(g.get("use_master_valve") or 0) == 1:
                        mtopic = (g.get("master_mqtt_topic") or "").strip()
                        msid = g.get("master_mqtt_server_id")
                        if mtopic and msid:
                            mserver = db.get_mqtt_server(int(msid))
                            if mserver:
                                try:
                                    mode = (g.get("master_mode") or "NC").strip().upper()
                                except (ValueError, TypeError, KeyError) as e:
                                    logger.debug("Exception in line_101: %s", e)
                                    mode = "NC"
                                open_val = "0" if mode == "NO" else "1"
                                publish_mqtt_value(
                                    mserver, normalize_topic(mtopic), open_val, min_interval_sec=0.0, qos=2, retain=True
                                )
                                try:
                                    db.update_group_fields(int(gid), {"master_valve_observed": "open"})
                                    import json as _json

                                    from services import sse_hub as _sse_hub

                                    _sse_hub.broadcast(_json.dumps({"mv_group_id": int(gid), "mv_state": "open"}))
                                except (sqlite3.Error, OSError, ImportError, ValueError, TypeError) as e:
                                    logger.debug("master_valve_observed update (open) failed: %s", e)
                if sid and topic:
                    server = db.get_mqtt_server(int(sid))
                    if server:
                        publish_mqtt_value(
                            server,
                            normalize_topic(topic),
                            "1",
                            min_interval_sec=0.0,
                            qos=2,
                            retain=True,
                            meta={
                                "cmd": str(command_id) if "command_id" in locals() and command_id else None,
                                "ver": str((z.get("version") or 0) + 1),
                            },
                        )
                        # transition to on
                        _versioned_update(zone_id, {"state": "on"}, audit_reason="mqtt_ack_on")
                        # Verify observed_state in background thread
                        try:
                            state_verifier.verify_async(int(zone_id), "on")
                        except (ValueError, TypeError, KeyError):
                            logger.debug("observed_state verify_async(on) launch failed")
            except (ConnectionError, TimeoutError, OSError):
                logger.exception("exclusive_start_zone: mqtt on failed")
            # Stop others in parallel to reduce latency. _stop_peer is defined
            # before the try so the sequential fallback below can reuse it.
            def _stop_peer(other, log_suffix=""):
                try:
                    oid = int(other.get("id"))
                    if oid == int(zone_id):
                        return
                    with zone_lock(oid):
                        ost = str((db.get_zone(oid) or {}).get("state") or "").lower()
                        if ost not in ("off",):
                            _versioned_update(
                                oid, {"state": "stopping", "commanded_state": "off"}, audit_reason="peer_stop"
                            )
                    osid = other.get("mqtt_server_id")
                    otopic = (other.get("topic") or "").strip()
                    if osid and otopic:
                        server_o = db.get_mqtt_server(int(osid))
                        if server_o:
                            publish_mqtt_value(
                                server_o,
                                normalize_topic(otopic),
                                "0",
                                min_interval_sec=0.0,
                                qos=2,
                                retain=True,
                                meta={"cmd": "peer_off", "ver": str((other.get("version") or 0) + 1)},
                            )
                            with zone_lock(oid):
                                # Close the open zone_run before flipping
                                # state — leaves a clean 'ok' row in
                                # history so get_last_watering_time()
                                # picks it up. Pre-refactor this path
                                # leaked open runs: they would only be
                                # marked 'aborted' on the next reboot via
                                # _boot_sync, never count as a successful
                                # watering. The whole find-open-run →
                                # finish-run → state-update sequence runs
                                # under the same lock so concurrent
                                # exclusive_start_zone calls cannot
                                # double-finish the same run.
                                try:
                                    _run = db.get_open_zone_run(oid)
                                    if _run:
                                        db.finish_zone_run(
                                            int(_run["id"]),
                                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                            time.monotonic(),
                                            None,
                                            None,
                                            None,
                                            status="ok",
                                        )
                                except (sqlite3.Error, OSError):
                                    logger.exception("peer_off finish_zone_run failed oid=%s", oid)
                                _versioned_update(
                                    oid, {"state": "off", "watering_start_time": None}, audit_reason="peer_off"
                                )
                except (ConnectionError, TimeoutError, OSError):
                    logger.exception("exclusive_start_zone: mqtt off peer failed%s", log_suffix)

            try:
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(group_zones) - 1))) as pool:
                    pool.map(_stop_peer, group_zones)
            except (ImportError, RuntimeError, OSError) as e:
                logger.warning("Parallel peer stop failed, falling back to sequential: %s", e)
                # Fallback to sequential if parallelization fails for any reason
                for other in group_zones:
                    _stop_peer(other, " (sequential)")
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
    сегодняшних программ отменёнными, делегирование ``exclusive_start_zone``
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
                from irrigation_scheduler import get_scheduler

                sched = get_scheduler()
                if sched:
                    # Remove existing stop jobs for THIS zone only —
                    # cancel_group_jobs would stop running peers.
                    try:
                        for job in sched.scheduler.get_jobs():
                            jid = str(job.id)
                            if f"zone_stop:{int(zone_id)}:" in jid or f"zone_hard_stop:{int(zone_id)}" in jid:
                                job.remove()
                    except (RuntimeError, AttributeError, ValueError) as e:
                        logger.debug("remove old stop jobs: %s", e)
                    if not TESTING:
                        sched.schedule_zone_stop(int(zone_id), override_dur, command_id=str(int(time.time())))
                        sched.schedule_zone_hard_stop(int(zone_id), now_dt + timedelta(minutes=override_dur))
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
    if gid:
        try:
            from irrigation_scheduler import get_scheduler

            sched = get_scheduler()
            if sched:
                sched.cancel_group_jobs(int(gid))
            try:
                programs = db.get_programs() or []
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                for p in programs:
                    try:
                        hh, mm = map(int, str(p.get("time") or "00:00").split(":", 1))
                    except (ValueError, TypeError, KeyError):
                        hh, mm = 0, 0
                    if now.replace(hour=hh, minute=mm, second=0, microsecond=0) <= now:
                        db.cancel_program_run_for_group(int(p.get("id")), today, int(gid))
            except (ConnectionError, TimeoutError, OSError, sqlite3.Error) as e:
                logger.debug("start_zone_orchestrated: cancel programs failed: %s", e)
            try:
                db.reschedule_group_to_next_program(int(gid))
            except (sqlite3.Error, OSError) as e:
                logger.debug("start_zone_orchestrated: reschedule_group_to_next_program failed: %s", e)
        except (ImportError, ConnectionError, TimeoutError, OSError) as e:
            logger.debug("start_zone_orchestrated pre-delegate housekeeping: %s", e)

    # ---- Delegate to single source of truth ----
    # exclusive_start_zone does (in order, all under group/zone locks):
    #   1) state -> 'starting'
    #   2) db.create_zone_run(...)
    #   3) master-valve open (if group uses MV)
    #   4) MQTT publish '1' on zone topic
    #   5) state -> 'on'
    #   6) parallel peer-off (publish '0' + finish their zone_runs)
    # On MQTT failure step 4, the zone_run row stays open and is
    # aborted by _boot_sync on next boot — that's the documented
    # invariant, do not reorder.
    try:
        ok = exclusive_start_zone(int(zone_id), source=source)
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
        _update_zone_state(
            int(zone_id),
            {"planned_end_time": planned_end, "watering_start_source": "manual"},
            audit_reason="manual_start_planned_end",
        )
    except (sqlite3.Error, OSError, ImportError):
        logger.exception(
            "start_zone_orchestrated: audited planned_end_time update failed zone=%s — "
            "falling back to raw update_zone",
            zone_id,
        )
        try:
            db.update_zone(int(zone_id), {"planned_end_time": planned_end, "watering_start_source": "manual"})
        except (sqlite3.Error, OSError) as e:
            logger.debug("start_zone_orchestrated: planned_end_time fallback update failed: %s", e)

    # Schedule auto-stop synchronously (sched calls are non-blocking).
    try:
        from irrigation_scheduler import get_scheduler

        sched = get_scheduler()
        if sched and not TESTING:
            sched.schedule_zone_stop(int(zone_id), dur_min, command_id=str(int(time.time())))
            sched.schedule_zone_hard_stop(int(zone_id), now_dt + timedelta(minutes=dur_min))
            sched.schedule_zone_cap(int(zone_id), cap_minutes=ZONE_CAP_DEFAULT_MIN)
    except (ImportError, ValueError, TypeError) as e:
        logger.debug("start_zone_orchestrated schedule stops: %s", e)

    return "started", {"warnings": warnings, "duration": dur_min}


def _finalize_water_stats(zone_id: int, z: dict, since_iso: str | None, *, log_label: str = "stop_zone") -> None:
    """Посчитать и сохранить водную статистику при остановке зоны.

    Быстрый путь — снапшот пульсов открытого zone_run (finish_zone_run);
    фоллбэк — summarize_run от ``since_iso`` (оригинальное время старта —
    summarize_run интегрирует пульсы начиная с него). Никогда не бросает
    исключений: log-and-continue, остановка зоны важнее статистики.
    """
    try:
        gid = int(z.get("group_id") or 0)
        total_liters = None
        avg_lpm = None
        if gid and gid != 999:
            try:
                run = db.get_open_zone_run(int(zone_id))
            except (sqlite3.Error, OSError) as e:
                logger.debug("%s: get_open_zone_run failed: %s", log_label, e)
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
                    db.finish_zone_run(
                        int(run["id"]),
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        end_mono,
                        end_raw,
                        total_liters,
                        avg_lpm,
                        status="ok",
                    )
                except (sqlite3.Error, OSError):
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
                db.update_zone(int(zone_id), updates)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        logger.exception("%s: water stats update failed", log_label)


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


def stop_zone(
    zone_id: int,
    reason: str = "manual",
    force: bool = False,
    master_close_immediately: bool = False,
    skip_master_close: bool = False,
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
        if (str(z.get("state")).lower() in ("off", "stopping")) and not force:
            logger.info(
                "stop_zone idempotent: zone_id=%s already state=%s reason=%s — "
                "running water-stats finalisation but no MQTT publish",
                zone_id,
                z.get("state"),
                reason,
            )
            # Зона уже оффлайн (часто по MQTT). Тем не менее, попробуем посчитать и сохранить статистику воды.
            _finalize_water_stats(
                zone_id,
                z,
                z.get("last_watering_time") or z.get("watering_start_time"),
                log_label="stop_zone (already off)",
            )
            # Even when the zone was already off (idempotent path), the caller
            # may want to (re)schedule a master-valve close — required for
            # emergency_stop / rain monitor paths and for keeping idempotency
            # on duplicate manual stops.
            _maybe_schedule_master_close(
                z, master_close_immediately, skip_master_close, log_label="stop_zone (already off)"
            )
            return True
        # `start_iso` is the original watering start — used as the lower bound
        # for summarize_run() water-stats fallback below (must NOT be touched).
        # The "last watering time" is now derived from zone_runs.end_utc;
        # we no longer write last_watering_time on the zones row.
        start_iso = z.get("watering_start_time")
        # Стейт: on/starting -> stopping
        with zone_lock(zone_id):
            _versioned_update(zone_id, {"state": "stopping", "commanded_state": "off"}, audit_reason=f"stop_{reason}")
        sid = z.get("mqtt_server_id")
        topic = (z.get("topic") or "").strip()
        try:
            if sid and topic:
                server = db.get_mqtt_server(int(sid))
                if server:
                    # OFF публикуем с retain=True, чтобы состояние восстанавливалось после перезапуска
                    publish_mqtt_value(
                        server,
                        normalize_topic(topic),
                        "0",
                        min_interval_sec=0.0,
                        qos=2,
                        retain=True,
                        meta={"cmd": "stop", "ver": str((z.get("version") or 0) + 1)},
                    )
                    # Verify observed_state in background thread
                    try:
                        state_verifier.verify_async(int(zone_id), "off")
                    except (ValueError, TypeError, KeyError):
                        logger.debug("observed_state verify_async(off) launch failed")
                    # Delayed master valve close — uses per-group delay
                    # (master_close_delay_sec) and proper cancellable timer.
                    _maybe_schedule_master_close(z, master_close_immediately, skip_master_close)
        except (ConnectionError, TimeoutError, OSError, sqlite3.Error):
            logger.exception("stop_zone: mqtt off failed")
        # Завершаем переход: stopping -> off.
        # last_watering_time is no longer a column — the open zone_run
        # row will be finished a few lines below and that becomes the
        # canonical history entry.
        with zone_lock(zone_id):
            _versioned_update(
                zone_id,
                {"state": "off", "watering_start_time": None, "planned_end_time": None},
                audit_reason=f"stop_{reason}_complete",
            )
        # Обновим статистику воды для зоны, если группа использует счётчик.
        # NOTE: summarize_run needs the ORIGINAL start time (start_iso)
        # as its lower bound — it integrates pulse counts since then.
        # Do NOT pass end_iso here.
        _finalize_water_stats(zone_id, z, start_iso)
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


def stop_all_in_group(
    group_id: int,
    reason: str = "group_cancel",
    force: bool = False,
    master_close_immediately: bool = False,
    skip_master_close: bool = False,
) -> None:
    """Немедленно остановить все зоны в группе (идемпотентно).

    master_close_immediately: при True мастер-клапан закрывается без задержки.
    skip_master_close: при True мастер-клапан вообще не планируется
    (вызывающий сам управляет закрытием — например emergency_stop_all).
    """
    try:
        zones = db.get_zones_by_group(int(group_id))
        for z in zones:
            try:
                stop_zone(
                    int(z["id"]),
                    reason=reason,
                    force=force,
                    master_close_immediately=master_close_immediately,
                    skip_master_close=skip_master_close,
                )
                # Небольшая пауза, чтобы избежать всплесков при публикации на слабом железе (пропускаем в тестах)
                try:
                    if not TESTING:
                        time.sleep(0.05)
                except (KeyError, TypeError, ValueError) as e:
                    logger.debug("Handled exception in stop_all_in_group: %s", e)
            except (ValueError, TypeError, KeyError):
                logger.exception("stop_all_in_group: stop_zone failed")
    except (sqlite3.Error, OSError):
        logger.exception("stop_all_in_group failed")


def emergency_stop_all(reason: str = "emergency_stop") -> dict:
    """Синхронная аварийная остановка всех групп с детерминированной последовательностью.

    Phase A: для каждой группы → последовательно stop_zone(skip_master_close=True)
             для всех зон. Master-таймеры здесь не планируются вовсе — мастер
             закроем явно в фазе C.
    Phase B: ожидание до 2с, чтобы все зоны перешли в state='off'. Если по
             таймауту остались зоны в on/starting — повторно force-стопаем их.
    Phase C: для каждой группы с use_master_valve=1 → SYNC publish close_val
             на master_mqtt_topic напрямую (без threading.Timer, без race).

    Возвращает dict со счётчиками для логирования/диагностики.
    """
    stats = {
        "groups_total": 0,
        "zones_stopped": 0,
        "zones_force_retried": 0,
        "masters_closed": 0,
        "masters_skipped_no_use_master": 0,
        "masters_skipped_no_topic": 0,
        "masters_skipped_dup_topic": 0,
        "masters_failed_publish": 0,
        "zones_still_active_after_wait": 0,
    }
    try:
        groups = db.get_groups() or []
    except (sqlite3.Error, OSError):
        logger.exception("emergency_stop_all: get_groups failed")
        return stats

    stats["groups_total"] = len(groups)
    logger.info("emergency_stop_all: starting phase A — stop zones across %d groups", len(groups))

    # Phase A: stop all zones in all groups WITHOUT scheduling any master close.
    # Master will be closed synchronously in Phase C — we do NOT want lingering
    # delay=60 timers firing later and republishing close.
    for g in groups:
        try:
            gid = int(g.get("id") or 0)
        except (ValueError, TypeError):
            continue
        if not gid:
            continue
        try:
            zones = db.get_zones_by_group(gid) or []
        except (sqlite3.Error, OSError):
            logger.exception("emergency_stop_all: get_zones_by_group(%s) failed", gid)
            continue
        for z in zones:
            try:
                stop_zone(
                    int(z["id"]), reason=reason, force=True, master_close_immediately=False, skip_master_close=True
                )
                stats["zones_stopped"] += 1
                if not TESTING:
                    time.sleep(0.02)
            except (ValueError, TypeError, KeyError, sqlite3.Error, OSError):
                logger.exception("emergency_stop_all: stop_zone failed (zone_id=%s)", z.get("id"))

    # Phase B: wait up to 2s for all zones to reach state='off' (or 'stopping' is also OK
    # — we treat 'stopping' as in-flight-but-OFF-published; only on/starting are blockers).
    if not TESTING:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            still_active = 0
            try:
                for g in groups:
                    try:
                        gid = int(g.get("id") or 0)
                    except (ValueError, TypeError):
                        continue
                    if not gid:
                        continue
                    for z in db.get_zones_by_group(gid) or []:
                        st = str(z.get("state") or "").lower()
                        if st in ("on", "starting"):
                            still_active += 1
            except (sqlite3.Error, OSError):
                logger.exception("emergency_stop_all: phase B check failed")
                break
            if still_active == 0:
                break
            time.sleep(0.1)
        # Re-issue stop_zone(force=True) for any zone STILL in on/starting at deadline.
        try:
            stuck_zones = []
            for g in groups:
                try:
                    gid = int(g.get("id") or 0)
                except (ValueError, TypeError):
                    continue
                if not gid:
                    continue
                for z in db.get_zones_by_group(gid) or []:
                    if str(z.get("state") or "").lower() in ("on", "starting"):
                        stuck_zones.append(int(z.get("id") or 0))
            if stuck_zones:
                logger.warning(
                    "emergency_stop_all: %d zones stuck after 2s — force-retry: %s", len(stuck_zones), stuck_zones
                )
                for zid in stuck_zones:
                    if not zid:
                        continue
                    try:
                        stop_zone(
                            zid,
                            reason=reason + "_retry",
                            force=True,
                            master_close_immediately=False,
                            skip_master_close=True,
                        )
                        stats["zones_force_retried"] += 1
                        if not TESTING:
                            time.sleep(0.02)
                    except (ValueError, TypeError, KeyError, sqlite3.Error, OSError):
                        logger.exception("emergency_stop_all: force-retry failed (zone_id=%s)", zid)
            stats["zones_still_active_after_wait"] = len(stuck_zones)
        except (sqlite3.Error, OSError):
            logger.exception("emergency_stop_all: phase B re-issue failed")

    # Phase C: synchronously close each master valve (one publish per master topic)
    logger.info("emergency_stop_all: phase C — closing master valves")
    seen_topics = {}  # type: dict[str, str]  # t_norm -> mode used
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
            logger.warning("emergency_stop_all: group=%s skipped — no master topic/server", gid)
            continue
        try:
            t_norm = normalize_topic(mtopic)
        except (ValueError, TypeError, OSError):
            t_norm = mtopic
        try:
            mode = (g.get("master_mode") or "NC").strip().upper()
        except (ValueError, TypeError, AttributeError):
            mode = "NC"
        # Cancel any pending timer for this topic so it can't override us later
        with _PENDING_CLOSE_LOCK:
            prev = _PENDING_CLOSE_TIMERS.pop(t_norm, None)
        if prev is not None:
            with contextlib.suppress(RuntimeError, OSError):
                prev.cancel()
        # Skip duplicate topics shared across groups (publish once per topic).
        # If a previously-seen group used a different master_mode, that's a
        # configuration smell — log a warning so ops can fix it.
        if t_norm in seen_topics:
            prev_mode = seen_topics[t_norm]
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
        seen_topics[t_norm] = mode
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
            ok = publish_mqtt_value(
                mserver,
                t_norm,
                close_val,
                min_interval_sec=0.0,
                qos=2,
                retain=True,
                meta={"cmd": "master_off", "src": "emergency"},
            )
            if not ok:
                # Issue #38: publish reported failure (base topic or '/on'
                # ack lost). Don't mark observed=closed — SSE-hub will heal
                # from the real relay echo if/when the close lands later.
                stats["masters_failed_publish"] += 1
                logger.warning(
                    "emergency_stop_all: master close publish FAILED — group=%s topic=%s", gid, t_norm
                )
                continue
            stats["masters_closed"] += 1
            logger.info(
                "emergency_stop_all: master closed — group=%s topic=%s val=%s mode=%s", gid, t_norm, close_val, mode
            )
            try:
                db.update_group_fields(int(gid), {"master_valve_observed": "closed"})
                import json as _json_e

                from services import sse_hub as _sse_hub_e

                _sse_hub_e.broadcast(_json_e.dumps({"mv_group_id": int(gid), "mv_state": "closed"}))
            except (sqlite3.Error, OSError, ImportError, ValueError, TypeError) as e:
                logger.debug("emergency_stop_all: master_valve_observed update failed (gid=%s): %s", gid, e)
            if not TESTING:
                time.sleep(0.05)
        except Exception:
            stats["masters_failed_publish"] += 1
            logger.exception("emergency_stop_all: publish failed group=%s topic=%s", gid, t_norm)

    logger.info("emergency_stop_all: done — %s", stats)
    return stats
