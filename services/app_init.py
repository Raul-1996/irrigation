"""
One-time application initialisation extracted from before_request (TASK-016).

Call :func:`initialize_app` once at startup (e.g. inside ``create_app()`` or
at module level) instead of running heavy init on every HTTP request.
"""

import contextlib
import logging
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime
from typing import Callable

logger = logging.getLogger(__name__)

_INIT_DONE = False

# Wave 2 F2 — readiness gate.  Flipped to True at the end of _boot_sync() so
# /readyz can tell the difference between "Flask is up but hasn't turned the
# world off yet" and "fully ready to schedule watering".  The gate is a plain
# module-level bool (not a threading primitive) because /readyz is a simple
# read-only check.
_boot_sync_done = False
_boot_recovery_done = False
_boot_reconcile_error = "boot reconciliation not started"
_boot_interrupted_zone_ids: set[int] = set()
_boot_zone_count = 0
_http_listener_ready_notified = False
_HTTP_LISTENER_READY_LOCK = threading.Lock()

_HEALTH_HEARTBEAT_THREAD: threading.Thread | None = None
_HEALTH_HEARTBEAT_STOP = threading.Event()

_DEFAULT_BOOT_RECONCILE_TIMEOUT_SEC = 40.0
_MAX_BOOT_RECONCILE_TIMEOUT_SEC = 45.0
_BOOT_EVIDENCE_CLEAR_TIMEOUT_SEC = 5.0


def reset_init():
    """Allow re-init in tests."""
    global _INIT_DONE, _boot_sync_done, _boot_recovery_done, _boot_reconcile_error
    global _boot_interrupted_zone_ids, _boot_zone_count, _http_listener_ready_notified
    _stop_health_bound_heartbeat(timeout=0.2)
    _INIT_DONE = False
    _boot_sync_done = False
    _boot_recovery_done = False
    _boot_reconcile_error = "boot reconciliation not started"
    _boot_interrupted_zone_ids = set()
    _boot_zone_count = 0
    _http_listener_ready_notified = False


def initialize_app(app, db, *, start_watchdog_fn=None):
    """Run once at boot: scheduler, watchdogs, boot-sync, monitors, MQTT warm-up.

    Args:
        app: Flask application instance.
        db: database handle.
        start_watchdog_fn: callable to start the single-zone exclusivity
            watchdog.  Injected from app.py to avoid circular import.

    Safe to call multiple times — only the first invocation does real work.
    Skipped entirely when ``app.config['TESTING']`` is truthy.
    """
    global _INIT_DONE, _boot_sync_done, _boot_recovery_done, _boot_reconcile_error
    if _INIT_DONE:
        return
    _INIT_DONE = True

    if app.config.get("TESTING"):
        return

    # ── 1. Strict bounded physical reconciliation ───────────────────
    # This MUST precede scheduler/watchdog startup.  Otherwise their ordinary
    # stop paths can close a crash-open run as successful before we classify it
    # as aborted, and can issue sequential MQTT OFF outside the boot deadline.
    boot_sync_ok = bool(_boot_sync(app, db))
    _boot_sync_done = boot_sync_ok

    # ── 2. Load the scheduler paused, then release boot recovery ─────
    _boot_recovery_done = False
    if boot_sync_ok:
        try:
            from irrigation_scheduler import get_scheduler, init_scheduler

            init_scheduler(db)
            logger.info("Scheduler initialised after physical reconciliation")
            scheduler = get_scheduler()
            interrupted = getattr(scheduler, "_boot_interrupted_zone_ids", None)
            if isinstance(interrupted, set):
                interrupted.update(_boot_interrupted_zone_ids)
            elif _boot_interrupted_zone_ids:
                scheduler._boot_interrupted_zone_ids = set(_boot_interrupted_zone_ids)
            complete_boot = getattr(scheduler, "complete_boot_recovery", None)
            if complete_boot is None:
                _boot_reconcile_error = "scheduler boot recovery API unavailable"
                logger.error(_boot_reconcile_error)
            elif complete_boot() is True:
                durable_ack = getattr(scheduler, "boot_recovery_handoff_is_durable", None)
                try:
                    handoff_durable = callable(durable_ack) and durable_ack() is True
                except Exception:
                    handoff_durable = False
                    logger.exception("Scheduler durable recovery acknowledgement failed")
                if not handoff_durable:
                    _boot_reconcile_error = "scheduler recovery handoff is not durable"
                    logger.critical(_boot_reconcile_error)
                    with contextlib.suppress(Exception):
                        scheduler.stop()
                else:
                    from services.lifecycle_storage import clear_boot_interrupted_evidence, run_bounded

                    db_path = getattr(db, "db_path", None)
                    clear_deadline = time.monotonic() + _BOOT_EVIDENCE_CLEAR_TIMEOUT_SEC
                    if not isinstance(db_path, str) or not db_path:
                        cleared, clear_error = False, "db_path missing"
                    else:
                        cleared, _unused, clear_error = run_bounded(
                            lambda: clear_boot_interrupted_evidence(db_path, deadline=clear_deadline),
                            deadline=clear_deadline,
                            name="boot interruption marker clear",
                        )
                    if cleared:
                        _boot_recovery_done = True
                        logger.info("Scheduler boot recovery completed")
                    else:
                        _boot_reconcile_error = clear_error or "boot interruption marker clear failed"
                        logger.critical("Scheduler recovery evidence was not consumed: %s", _boot_reconcile_error)
                        with contextlib.suppress(Exception):
                            scheduler.stop()
            else:
                _boot_reconcile_error = "scheduler boot recovery did not complete"
                logger.error(_boot_reconcile_error)
        except (ImportError, AttributeError, RuntimeError, TypeError, ValueError) as exc:
            _boot_reconcile_error = f"scheduler boot recovery failed: {type(exc).__name__}"
            logger.exception("Scheduler boot recovery completion failed")
    else:
        logger.critical("Scheduler remains boot-paused: %s", _boot_reconcile_error)

    # ── 3. Safety watchdogs and monitors only after full recovery ───
    if _boot_sync_done and _boot_recovery_done:
        if start_watchdog_fn is not None:
            try:
                start_watchdog_fn()
            except Exception:
                logger.exception("single-zone watchdog start failed")
        else:
            logger.warning("start_watchdog_fn not provided, skipping watchdog")

        try:
            import services.zone_control as _zc_module
            from services.watchdog import start_watchdog as _start_cap_watchdog

            _start_cap_watchdog(db, _zc_module, interval=30)
        except ImportError:
            logger.exception("cap-time watchdog start failed")

        _start_monitors(app, db)
        _warm_mqtt_clients(db)

    # ── 4. Graceful shutdown handlers ───────────────────────────────
    _register_shutdown_handlers(db)

    # ── 5. Observability metrics (F2) ───────────────────────────────
    try:
        from routes.health_api import init_metrics as _init_metrics

        _init_metrics(app, db)
    except ImportError as e:
        logger.warning("init_metrics not available: %s", e)
    except Exception:
        logger.exception("init_metrics failed")

    # ── 6. systemd readiness is deferred to the real HTTP listener ──
    # run.py performs a process-token-bound /healthz request after Hypercorn
    # has bound the socket, then calls notify_http_listener_ready(). Import-time
    # boot completion alone must never transition Type=notify to active.
    if _boot_sync_done and _boot_recovery_done:
        logger.info("Boot recovery complete; waiting for HTTP listener acceptance before READY=1")
    else:
        logger.critical("Application remains NOT READY: %s", _boot_reconcile_error)

    logger.info("Application initialisation complete")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _boot_reconcile_timeout(timeout_sec: float | None) -> float:
    """Return a bounded timeout that always leaves margin for systemd start."""
    if timeout_sec is None:
        raw = os.environ.get("WB_BOOT_RECONCILE_TIMEOUT_SEC", str(_DEFAULT_BOOT_RECONCILE_TIMEOUT_SEC))
        try:
            timeout_sec = float(raw)
        except (TypeError, ValueError):
            timeout_sec = _DEFAULT_BOOT_RECONCILE_TIMEOUT_SEC
    try:
        return min(_MAX_BOOT_RECONCILE_TIMEOUT_SEC, max(0.0, float(timeout_sec)))
    except (TypeError, ValueError):
        return _DEFAULT_BOOT_RECONCILE_TIMEOUT_SEC


def _run_boot_tasks(
    tasks: list[tuple[str, Callable[[], tuple[bool, str | None]]]],
    *,
    deadline: float,
) -> tuple[dict[str, tuple[bool, str | None]], set[str]]:
    """Run physical OFF operations concurrently within one global deadline.

    MQTT connect/ack calls can block inside third-party code.  Daemon workers
    allow startup to remain bounded; a worker that finishes after the deadline
    can only publish the same fail-safe OFF value.  Its late result is ignored
    and readiness stays closed until systemd restarts the process.
    """
    results: queue.Queue[tuple[str, bool, str | None]] = queue.Queue()

    def run_one(key: str, fn: Callable[[], tuple[bool, str | None]]) -> None:
        try:
            ok, reason = fn()
        except Exception as exc:  # worker boundary: report, never lose result
            ok, reason = False, f"{type(exc).__name__}: {exc}"
        results.put((key, bool(ok), reason))

    pending = {key for key, _fn in tasks}
    for key, fn in tasks:
        threading.Thread(
            target=run_one,
            args=(key, fn),
            name=f"boot-off-{key}",
            daemon=True,
        ).start()

    completed: dict[str, tuple[bool, str | None]] = {}
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            key, ok, reason = results.get(timeout=remaining)
        except queue.Empty:
            break
        completed[key] = (ok, reason)
        pending.discard(key)
    return completed, pending


def _boot_sync(app, db, *, timeout_sec: float | None = None) -> bool:
    """Reconcile every configured zone and master valve to confirmed OFF.

    The whole sweep shares one deadline.  Readiness is opened only when every
    physical target reports success and the crash-open history cleanup commits.
    """
    del app  # kept in the public helper signature for compatibility
    from services.lifecycle_storage import (
        abort_crash_open_runs,
        persist_boot_zones_off,
        run_bounded,
        strict_snapshot,
    )
    from services.mqtt_pub import get_or_create_mqtt_client
    from services.shutdown import _confirmed_publish
    from utils import normalize_topic

    global _boot_sync_done, _boot_reconcile_error, _boot_interrupted_zone_ids, _boot_zone_count
    _boot_sync_done = False
    _boot_reconcile_error = "boot reconciliation in progress"
    _boot_interrupted_zone_ids = set()
    _boot_zone_count = 0
    deadline = time.monotonic() + _boot_reconcile_timeout(timeout_sec)
    failures: list[str] = []
    db_path = getattr(db, "db_path", None)
    if not isinstance(db_path, str) or not db_path:
        _boot_reconcile_error = "strict lifecycle snapshot unavailable: db_path missing"
        return False

    # A crashed process leaves local-naive timestamps in zone_runs.  Use the
    # controller's local wall clock rather than SQLite CURRENT_TIMESTAMP (UTC),
    # and deliberately leave `confirmed` untouched for forensic truth.
    crash_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    history_ok, interrupted, history_error = run_bounded(
        lambda: abort_crash_open_runs(db_path, deadline=deadline, end_local=crash_end),
        deadline=deadline,
        name="crash history cleanup",
    )
    if not history_ok or interrupted is None:
        _boot_reconcile_error = history_error or "crash history cleanup failed"
        logger.critical("Boot reconciliation failed: %s", _boot_reconcile_error)
        return False
    _boot_interrupted_zone_ids = set(interrupted)
    logger.info("boot_sync: aborted open zone_runs from prior run")

    snapshot_ok, snapshot, snapshot_error = run_bounded(
        lambda: strict_snapshot(db_path, deadline=deadline),
        deadline=deadline,
        name="strict topology snapshot",
    )
    if not snapshot_ok or snapshot is None:
        _boot_reconcile_error = snapshot_error or "strict topology snapshot failed"
        logger.critical("Boot reconciliation failed: %s", _boot_reconcile_error)
        return False

    zones = snapshot.zones
    groups = snapshot.groups
    servers = snapshot.servers
    _boot_zone_count = len(zones)

    tasks: list[tuple[str, Callable[[], tuple[bool, str | None]]]] = []
    zone_states: list[tuple[int, str]] = []

    def publish_command(server: dict, topic: str, value: str) -> tuple[bool, str | None]:
        client = get_or_create_mqtt_client(server)
        if client is None:
            return False, "MQTT client unavailable"
        target = normalize_topic(topic) + "/on"
        info = client.publish(target, payload=value, qos=2, retain=True)
        ok, reason = _confirmed_publish(info, deadline=deadline)
        if not ok:
            return False, f"{target}: {reason}"
        return True, None

    for index, zone in enumerate(zones):
        try:
            zone_id = int(zone["id"])
        except (KeyError, TypeError, ValueError):
            failures.append(f"zone[{index}]: invalid id")
            continue
        current_state = str(zone.get("state") or "").lower()
        zone_states.append((zone_id, current_state))

        def turn_zone_off(z: dict = zone, state: str = current_state) -> tuple[bool, str | None]:
            sid = z.get("mqtt_server_id")
            topic = str(z.get("topic") or "").strip()
            if bool(sid) != bool(topic):
                return False, "incomplete MQTT mapping"
            if not sid and not topic and state != "off":
                return False, f"active state {state!r} has no MQTT mapping"
            if sid and topic:
                server = servers.get(int(sid))
                if not server:
                    return False, "MQTT server missing"
                ok, reason = publish_command(server, topic, "0")
                if not ok:
                    return False, reason or "OFF not confirmed"
                if time.monotonic() > deadline:
                    return False, "global deadline exceeded after OFF"
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
            mode = str(group.get("master_mode") or "NC").strip().upper()
            close_value = "1" if mode == "NO" else "0"
            master_key = (sid, topic)
            prior_value = seen_masters.get(master_key)
            if prior_value is not None:
                if prior_value != close_value:
                    failures.append(f"master group:{group_id or index}: conflicting close mode")
                continue
            seen_masters[master_key] = close_value
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            failures.append(f"master group:{index}: {type(exc).__name__}")
            continue

        def close_master(
            server_id: int = sid,
            master_topic: str = topic,
            value: str = close_value,
        ) -> tuple[bool, str | None]:
            server = servers.get(server_id)
            if not server:
                return False, "MQTT server missing"
            ok, reason = publish_command(server, master_topic, value)
            if not ok:
                return False, reason or "OFF not confirmed"
            if time.monotonic() > deadline:
                return False, "global deadline exceeded after OFF"
            return True, None

        tasks.append((f"master:{sid}:{topic}", close_master))

    completed, pending = _run_boot_tasks(tasks, deadline=deadline)
    for key, (ok, reason) in completed.items():
        if not ok:
            failures.append(f"{key}: {reason or 'failed'}")
    failures.extend(f"{key}: deadline exceeded" for key in sorted(pending))
    if time.monotonic() > deadline and not pending:
        failures.append("global boot reconciliation deadline exceeded")

    if failures:
        _boot_reconcile_error = "; ".join(failures)
        logger.critical("Boot reconciliation failed: %s", _boot_reconcile_error)
        return False

    persist_ok, _unused, persist_error = run_bounded(
        lambda: persist_boot_zones_off(
            db_path,
            zone_states,
            interrupted_zone_ids=_boot_interrupted_zone_ids,
            deadline=deadline,
            updated_local=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
        deadline=deadline,
        name="boot zone state commit",
    )
    if not persist_ok:
        _boot_reconcile_error = persist_error or "boot zone state commit failed"
        logger.critical("Boot reconciliation failed: %s", _boot_reconcile_error)
        return False

    _boot_sync_done = True
    _boot_reconcile_error = ""
    logger.info("Boot reconciliation: every zone and master valve confirmed OFF")
    return True


def _start_monitors(app, db):
    """Start water, rain, and environment monitors."""
    try:
        from services.monitors import (
            env_monitor,
            probe_env_values,
            rain_monitor,
            start_water_monitor,
        )
    except ImportError:
        logger.exception("Failed to import monitors")
        return

    # Water monitor (idempotent)
    try:
        start_water_monitor()
    except (OSError, RuntimeError):  # catch-all: intentional
        logger.exception("WaterMonitor start failed")

    # Rain monitor
    try:
        cfg = db.get_rain_config()
        rain_monitor.start(cfg)
    except (sqlite3.Error, OSError):
        logger.exception("RainMonitor start failed")

    # Env monitor
    try:
        ecfg = db.get_env_config()
        env_monitor.start(ecfg)
        # Probe retained values so data appears immediately
        try:
            probe_env_values(ecfg)
        except (OSError, RuntimeError, ValueError):  # catch-all: intentional
            logger.exception("EnvMonitor probe call failed")
    except (sqlite3.Error, OSError):
        logger.exception("EnvMonitor start failed")


def _warm_mqtt_clients(db):
    """Pre-connect all configured MQTT publisher clients."""
    try:
        from services.mqtt_pub import get_or_create_mqtt_client

        servers = db.get_mqtt_servers() or []
        for s in servers:
            try:
                if int(s.get("enabled") or 1) != 1:
                    continue
                get_or_create_mqtt_client(s)
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Handled exception in _warm_mqtt_clients: %s", e)
        logger.info(f"MQTT clients warmed: {len(servers)}")
    except ImportError:
        logger.exception("MQTT warm-up failed")


def notify_http_listener_ready(*, health_probe_url: str | None = None) -> bool:
    """Emit READY exactly once, after run.py proves this process accepts HTTP."""
    global _http_listener_ready_notified
    with _HTTP_LISTENER_READY_LOCK:
        if _http_listener_ready_notified:
            return True
        if not (_boot_sync_done and _boot_recovery_done):
            logger.critical("HTTP listener is live but boot recovery is unresolved; READY=1 withheld")
            return False
        try:
            from services.systemd_notify import notify_ready, watchdog_is_armed

            watchdog_required = watchdog_is_armed()
        except Exception:
            logger.exception("systemd watchdog contract resolution failed")
            return False

        # Start the sole health-bound thread first. It waits one interval before
        # sending, so READY still remains the first systemd notification. If
        # WatchdogSec is armed, claiming readiness without a live sender would
        # guarantee a restart loop one period later.
        try:
            heartbeat_started = _start_health_bound_heartbeat(health_probe_url=health_probe_url)
        except Exception:
            logger.exception("health-bound systemd watchdog start failed")
            heartbeat_started = False
        if watchdog_required and not heartbeat_started:
            logger.critical("READY=1 withheld because the armed watchdog thread did not start")
            return False

        try:
            sent = bool(notify_ready(status=f"HTTP ready, {_boot_zone_count} zones reconciled"))
        except Exception:
            logger.exception("systemd READY=1 notification failed")
            return False
        if not sent:
            return False
        _http_listener_ready_notified = True
    return True


def _health_heartbeat_once(
    *,
    urlopen_fn=None,
    port: int | None = None,
    timeout_sec: float = 2.0,
    health_probe_url: str | None = None,
) -> bool:
    """Probe the real HTTP request plane, then emit one systemd heartbeat."""
    if urlopen_fn is None:
        from urllib.request import urlopen as urlopen_fn

    try:
        from services.security import local_http_probe_url, resolve_http_transport

        if health_probe_url is None:
            profile = resolve_http_transport()
            probe_url = local_http_probe_url(profile, port=port)
        else:
            probe_url = health_probe_url
        kwargs = {"timeout": max(0.05, float(timeout_sec))}
        if probe_url.lower().startswith("https://"):
            import ssl

            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            kwargs["context"] = context
        with urlopen_fn(probe_url, **kwargs) as response:
            if int(getattr(response, "status", 0)) != 200:
                return False
    except (OSError, RuntimeError, TimeoutError, ValueError, TypeError):
        logger.warning("systemd watchdog HTTP health probe failed", exc_info=True)
        return False

    try:
        from services.systemd_notify import notify_watchdog

        sent = bool(notify_watchdog())
        if sent:
            with contextlib.suppress(Exception):
                from routes.health_api import WB_WATCHDOG_HEARTBEATS

                WB_WATCHDOG_HEARTBEATS.inc()
        return sent
    except Exception:
        logger.warning("systemd watchdog notify failed", exc_info=True)
        return False


def _watchdog_interval_sec() -> float:
    """Use one third of systemd's watchdog period, with a safe default."""
    try:
        watchdog_usec = int(os.environ.get("WATCHDOG_USEC", "60000000"))
        return max(1.0, watchdog_usec / 3_000_000.0)
    except (TypeError, ValueError):
        return 20.0


def _health_heartbeat_loop(health_probe_url: str | None = None) -> None:
    interval = _watchdog_interval_sec()
    probe_timeout = min(5.0, max(0.5, interval / 4.0))
    logger.info("health-bound systemd heartbeat started (interval=%.1fs)", interval)
    while not _HEALTH_HEARTBEAT_STOP.wait(interval):
        try:
            _health_heartbeat_once(
                timeout_sec=probe_timeout,
                health_probe_url=health_probe_url,
            )
        except Exception:
            # The sole watchdog thread must survive any transient probe/config
            # failure and retry on the next interval.
            logger.exception("unexpected systemd watchdog heartbeat failure")
    logger.info("health-bound systemd heartbeat stopped")


def _start_health_bound_heartbeat(*, health_probe_url: str | None = None) -> bool:
    """Start the only production heartbeat, conditional on HTTP liveness."""
    global _HEALTH_HEARTBEAT_THREAD, _HEALTH_HEARTBEAT_STOP
    if not os.environ.get("NOTIFY_SOCKET"):
        return False
    from services.systemd_notify import watchdog_is_armed

    watchdog_disabled = os.environ.get("WB_WATCHDOG_ENABLED", "1") != "1"
    if watchdog_disabled and not watchdog_is_armed():
        logger.info("systemd watchdog disabled via WB_WATCHDOG_ENABLED=0")
        return False
    if watchdog_disabled:
        logger.critical("WB_WATCHDOG_ENABLED=0 ignored because systemd WATCHDOG_USEC is armed")
    if _HEALTH_HEARTBEAT_THREAD is not None and _HEALTH_HEARTBEAT_THREAD.is_alive():
        return True
    _HEALTH_HEARTBEAT_STOP = threading.Event()
    _HEALTH_HEARTBEAT_THREAD = threading.Thread(
        target=_health_heartbeat_loop,
        args=(health_probe_url,),
        name="sd-notify-health-heartbeat",
        daemon=True,
    )
    _HEALTH_HEARTBEAT_THREAD.start()
    return True


def _stop_health_bound_heartbeat(timeout: float = 5.0) -> None:
    global _HEALTH_HEARTBEAT_THREAD
    _HEALTH_HEARTBEAT_STOP.set()
    thread = _HEALTH_HEARTBEAT_THREAD
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=max(0.0, float(timeout)))
    if thread is None or not thread.is_alive():
        _HEALTH_HEARTBEAT_THREAD = None


# ---------------------------------------------------------------------------
# Graceful shutdown: delegate to services.shutdown
# ---------------------------------------------------------------------------
import atexit
import signal

from services.shutdown import reset_shutdown, shutdown_all_zones_off


def shutdown_all_zones(db=None) -> None:
    """Backward-compatible wrapper — delegates to services.shutdown."""
    shutdown_all_zones_off(db=db)


def _register_shutdown_handlers(db=None):
    """Register atexit + signal handlers for graceful zone shutdown.

    Must be called AFTER app init so that MQTT clients are already warm.
    Not registered in TESTING mode.
    """
    from config import TESTING

    if TESTING:
        return

    def _signal_handler(signum, frame):
        logger.info("Shutdown: received signal %s", signum)
        try:
            from services.systemd_notify import notify_stopping

            notify_stopping()
            _stop_health_bound_heartbeat(timeout=2.0)
        except Exception:
            logger.debug("systemd_notify stop failed (non-fatal)", exc_info=True)
        shutdown_all_zones_off(db=db)
        # Re-raise default handler so process actually exits
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    def _atexit_handler():
        try:
            _stop_health_bound_heartbeat(timeout=1.0)
        except Exception:
            pass
        shutdown_all_zones_off(db=db)

    # atexit runs on normal exit and some signal scenarios
    atexit.register(_atexit_handler)

    # SIGTERM (systemctl stop, docker stop)
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
    except (OSError, ValueError) as e:
        logger.debug("Shutdown: cannot register SIGTERM handler: %s", e)

    # SIGINT (Ctrl+C)
    try:
        signal.signal(signal.SIGINT, _signal_handler)
    except (OSError, ValueError) as e:
        logger.debug("Shutdown: cannot register SIGINT handler: %s", e)
