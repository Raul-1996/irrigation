#!/usr/bin/env python3
"""
Система планировщика полива WB-Irrigation
Реализует алгоритм последовательного запуска зон с APScheduler
"""

import hashlib
import inspect
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, tzinfo
from typing import Any

logger = logging.getLogger(__name__)

import json

_BOOT_RECOVERY_INTENTS_KEY = "scheduler.boot_recovery_intents.v1"

from apscheduler.schedulers import SchedulerNotRunningError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_PAUSED, STATE_RUNNING, STATE_STOPPED, BaseScheduler
from apscheduler.util import TIMEOUT_MAX

from config import TESTING
from database import IrrigationDB
from utils import normalize_topic

try:
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
except ImportError as e:
    logger.debug("SQLAlchemyJobStore not available: %s", e)
    SQLAlchemyJobStore = None
try:
    from apscheduler.jobstores.memory import MemoryJobStore
except ImportError as e:
    logger.debug("MemoryJobStore not available: %s", e)
    MemoryJobStore = None
import os

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

try:
    from zoneinfo import ZoneInfo  # py3.9+
except ImportError as e:
    logger.debug("ZoneInfo not available: %s", e)
    ZoneInfo = None
try:
    import paho.mqtt.client as mqtt
except ImportError as e:
    logger.debug("paho.mqtt not available: %s", e)
    mqtt = None


class _DeterministicBackgroundScheduler(BackgroundScheduler):
    """Stop the scheduler thread before shutting down its executors.

    APScheduler 3.10.4 shuts down executors before joining its background
    thread.  A pending wakeup can consequently enter ``_process_jobs()`` and
    submit a due job to an already-closed ThreadPoolExecutor.  Rechecking the
    stopped state after every wakeup and joining first removes that race.
    """

    def _main_loop(self) -> None:
        wait_seconds = TIMEOUT_MAX
        while True:
            self._event.wait(wait_seconds)
            self._event.clear()
            if self.state == STATE_STOPPED:
                return
            wait_seconds = self._process_jobs()

    def shutdown(self, wait: bool = True) -> None:
        if self.state == STATE_STOPPED:
            raise SchedulerNotRunningError
        thread = getattr(self, "_thread", None)
        if thread is threading.current_thread():
            raise RuntimeError("scheduler cannot shut itself down from its coordinator thread")

        # Keep executors alive until the coordinator has observed STOPPED and
        # exited.  BaseScheduler.shutdown() then performs the normal executor,
        # jobstore and event cleanup without any concurrent submitter.
        self.state = STATE_STOPPED
        self.wakeup()
        if thread is not None:
            thread.join()
        self.state = STATE_PAUSED
        BaseScheduler.shutdown(self, wait=wait)
        self._thread = None
        self._event.set()


# Логирование: не вызываем logging.basicConfig() на import-time (CQ-012 / MASTER-C2).
# Ранее этот вызов срабатывал ДО services/logging_setup.py и поднимал уровень
# root-логгера до WARNING, из-за чего app.log оставался пустым.
# Уровень теперь выставляет setup_logging() в services/logging_setup.py;
# при необходимости локально поднять уровень — через SCHEDULER_LOG_LEVEL env var.
logger = logging.getLogger(__name__)
try:
    _sched_level_name = os.getenv("SCHEDULER_LOG_LEVEL", "").upper()
    if _sched_level_name:
        _sched_level = getattr(logging, _sched_level_name, None)
        if _sched_level is not None:
            logger.setLevel(_sched_level)
except (KeyError, TypeError, ValueError) as e:
    logger.debug("scheduler log level from env: %s", e)
# В тестах отключаем распространение в root, чтобы не писать в закрытый stdout
# из фоновых потоков APScheduler. В проде propagate=True нужен, чтобы сообщения
# доходили до file handler на root (см. services/logging_setup.py).
try:
    if "PYTEST_CURRENT_TEST" in os.environ:
        logger.propagate = False
except (KeyError, TypeError):
    pass
# Урезаем болтливость APScheduler, чтобы в тестах и проде не было лишних сообщений
try:
    aps_logger = logging.getLogger("apscheduler")
    aps_logger.setLevel(logging.ERROR)
except (ImportError, AttributeError) as e:  # catch-all: intentional
    logger.debug("Handled exception in line_59: %s", e)


# === Module-level job callables for APScheduler persistence ===
def _audit_timer_fire(action: str, target: str, payload: dict) -> None:
    """Best-effort debug emit for scheduler timer fire events.

    Centralised so individual job callables stay readable. Only writes when
    ``settings.logging.debug`` is true — debug_audit() guards that internally.
    """
    try:
        from services.audit import debug_audit

        debug_audit(
            action_type=action,
            source="scheduler",
            target=target,
            payload=payload,
        )
    except Exception:
        logger.exception("scheduler timer fire audit failed (action=%s)", action)


def job_run_program(program_id: int, zones: list, program_name: str, manual: bool = False):
    # Ручной запуск (POST /api/programs/<id>/run) уже аудируется роутом как
    # prog_manual_run — scheduler_timer_fire пишем только для срабатываний
    # таймера, чтобы ручное действие не маскировалось под планировщик.
    if not manual:
        _audit_timer_fire(
            "scheduler_timer_fire",
            f"program:{int(program_id)}",
            {"job": "run_program", "zones": list(zones), "program_name": str(program_name), "manual": False},
        )
    try:
        from irrigation_scheduler import get_scheduler

        s = get_scheduler()
        if s is not None:
            # Persistent APScheduler args are a snapshot.  Always revalidate a
            # concrete scheduler job against the current DB so a stale/orphan
            # fire cannot water deleted zones while a concurrent DELETE is
            # still cancelling jobstore rows.
            if isinstance(s, IrrigationScheduler):
                current = s.db.get_program(int(program_id))
                if not current or not current.get("enabled", True) or not current.get("zones"):
                    s.cancel_program(int(program_id))
                    return
                zones = sorted({int(z) for z in current.get("zones") or []})
                program_name = str(current.get("name") or program_name)
            s._run_program_threaded(
                int(program_id),
                sorted({int(z) for z in zones}),
                str(program_name),
                manual=bool(manual),
            )
    except (sqlite3.Error, OSError, ValueError, TypeError):
        # Promoted to logger.exception — scheduled program runs that silently
        # fail are catastrophic; we want a stack trace in app.log.
        logger.exception("job_run_program failed (program_id=%s)", program_id)


def job_run_boot_recovery(intent_id: str):
    """Execute one durable boot-recovery intent.

    APScheduler removes a DateTrigger when it submits the callback. The intent
    in the application DB therefore remains the owner until this callback
    reaches a terminal safe result; a process crash can recreate the job on
    the next boot.
    """
    _audit_timer_fire(
        "scheduler_timer_fire",
        f"boot_recovery:{intent_id}",
        {"job": "boot_recovery", "intent_id": str(intent_id)},
    )
    try:
        from irrigation_scheduler import get_scheduler

        scheduler = get_scheduler()
        if scheduler is not None:
            scheduler._execute_boot_recovery_intent(str(intent_id))
    except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError, KeyError):
        # Never consume the durable intent on callback failure.
        logger.exception("boot recovery callback failed (intent_id=%s)", intent_id)


def job_run_group_sequence(
    group_id: int,
    zone_ids: list,
    override_duration: int | None = None,
    override_percent: int | None = None,
    ad_hoc_program_id: int | None = None,
    ad_hoc_program_name: str | None = None,
    manual: bool = False,
):
    # Issue #12: override_percent kwarg added with default=None — APScheduler
    # binds args positionally, but our scheduler.add_job(args=[...]) call passes
    # only positional args we control, so adding a trailing optional kwarg is
    # safe for in-flight jobs from prior deploys (they won't include it).
    # Issue #15: ad_hoc_program_id/name pass through to _run_group_sequence
    # for ad-hoc audit metadata (negative sentinel id).
    # Issue #31: manual flag bypasses weather skip in _run_group_sequence.
    _audit_timer_fire(
        "scheduler_timer_fire",
        f"group:{int(group_id)}",
        {
            "job": "run_group_sequence",
            "zone_ids": list(zone_ids),
            "override_duration": override_duration,
            "override_percent": override_percent,
            "manual": bool(manual),
        },
    )
    try:
        from irrigation_scheduler import get_scheduler

        s = get_scheduler()
        if s is not None:
            s._run_group_sequence(
                int(group_id),
                [int(z) for z in zone_ids],
                override_duration=override_duration,
                override_percent=override_percent,
                ad_hoc_program_id=ad_hoc_program_id,
                ad_hoc_program_name=ad_hoc_program_name,
                manual=bool(manual),
            )
    except (sqlite3.Error, OSError, ValueError, TypeError):
        logger.exception("job_run_group_sequence failed (group_id=%s)", group_id)


def job_stop_zone(zone_id: int):
    _audit_timer_fire("scheduler_timer_fire", f"zone:{int(zone_id)}", {"job": "stop_zone"})
    try:
        from irrigation_scheduler import get_scheduler

        s = get_scheduler()
        if s is not None:
            s._stop_zone(int(zone_id))
    except (sqlite3.Error, OSError, ValueError, TypeError):
        logger.exception("job_stop_zone failed (zone_id=%s)", zone_id)


def job_stop_zone_if_activation(
    zone_id: int,
    activation_token: str | None,
    force: bool = False,
):
    """Stop only the zone activation that planted this safety timer.

    A DateTrigger can already be queued in an executor while a new manual run
    replaces its job id. Binding the callback to the durable ``command_id``
    keeps that stale callback from stopping the new physical activation.
    """
    _audit_timer_fire(
        "scheduler_timer_fire",
        f"zone:{int(zone_id)}",
        {
            "job": "activation_bound_stop",
            "activation_token": activation_token,
            "force": bool(force),
        },
    )
    try:
        from irrigation_scheduler import get_scheduler

        s = get_scheduler()
        if s is None:
            return
        zone = s.db.get_zone(int(zone_id))
        if not zone:
            return
        current_token = zone.get("command_id")
        if current_token in (None, ""):
            # Upgrade compatibility for an activation that predates durable
            # command ids.  Never compare a legacy timestamp when the current
            # activation already has a UUID: seconds-resolution timestamps can
            # collide across rapid OFF/ON transitions.
            current_token = zone.get("watering_start_time")
        if activation_token is not None and str(current_token or "") != str(activation_token):
            logger.info(
                "Stale stop callback ignored for zone %s (expected activation=%s, current=%s)",
                zone_id,
                activation_token,
                current_token,
            )
            return
        s._stop_zone(
            int(zone_id),
            reason="activation_bound_stop",
            activation_token=None if activation_token is None else str(activation_token),
            force=bool(force),
        )
    except (sqlite3.Error, OSError, ValueError, TypeError):
        logger.exception("job_stop_zone_if_activation failed (zone_id=%s)", zone_id)


def job_close_master_valve(group_id: int):
    """Закрыть мастер-клапан у группы, если он включён."""
    try:
        from database import db
        from services.mqtt_pub import publish_mqtt_value

        g = next((gg for gg in (db.get_groups() or []) if int(gg.get("id")) == int(group_id)), None)
        if not g:
            return
        if int(g.get("use_master_valve") or 0) != 1:
            return
        topic = (g.get("master_mqtt_topic") or "").strip()
        sid = g.get("master_mqtt_server_id")
        if not topic or not sid:
            return
        server = db.get_mqtt_server(int(sid))
        if not server:
            return
        try:
            mode = (g.get("master_mode") or "NC").strip().upper()
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in job_close_master_valve: %s", e)
            mode = "NC"
        close_val = "1" if mode == "NO" else "0"
        publish_mqtt_value(
            server,
            normalize_topic(topic),
            close_val,
            min_interval_sec=0.0,
            qos=2,
            retain=True,
            meta={"cmd": "master_cap_close"},
        )
        logger.info(f"Master valve cap close: group {group_id}")
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.error(f"Ошибка cap-закрытия мастер-клапана для группы {group_id}: {e}")


def job_clear_expired_postpones():
    try:
        from irrigation_scheduler import get_scheduler

        s = get_scheduler()
        if s is not None:
            s.clear_expired_postpones()
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.debug("Handled exception in job_clear_expired_postpones: %s", e)


def job_dispatch_bot_subscriptions():
    try:
        from database import db
        from services.reports import build_report_text
        from services.telegram_bot import notifier

        now = datetime.now()
        due = db.get_due_bot_subscriptions(now)
        for sub in due:
            try:
                fmt = str(sub.get("format") or "brief")
                ptype = str(sub.get("type") or "daily")
                period = "today" if ptype == "daily" else "7"
                txt = build_report_text(period=period, fmt="brief" if fmt != "full" else "full")
                chat_id = int(sub.get("chat_id"))
                if chat_id:
                    notifier.send_text(chat_id, txt)
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Handled exception in job_dispatch_bot_subscriptions: %s", e)
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.debug("Handled exception in job_dispatch_bot_subscriptions: %s", e)


def job_audit_cleanup():
    """Daily APScheduler job: prune audit_log rows older than 7 days."""
    try:
        from database import db

        deleted = db.cleanup_audit_logs(older_than_days=7)
        logger.info("audit_cleanup job: removed %d audit_log rows older than 7 days", int(deleted or 0))
        # Self-audit so the cleanup itself is observable.
        try:
            from services.audit import record_audit

            record_audit(
                action_type="audit_cleanup",
                source="scheduler",
                actor="system",
                target="audit_log",
                payload={"deleted": int(deleted or 0), "older_than_days": 7},
                result="success",
            )
        except (ImportError, sqlite3.Error, OSError, ValueError, TypeError) as e:
            logger.debug("audit_cleanup self-audit failed: %s", e)
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.error("job_audit_cleanup failed: %s", e)


def job_daily_backup():
    """Daily APScheduler job: create sanity-checked DB backup at 03:15."""
    try:
        from database import db

        path = db.create_backup()
        if path:
            logger.info("Daily backup: %s", path)
        else:
            logger.error("Daily backup failed (sanity check rejected)")
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.error("job_daily_backup failed: %s", e)


class IrrigationScheduler:
    """Планировщик полива с последовательным запуском зон"""

    def __init__(self, db: IrrigationDB):
        self.db = db
        # Явно задаём таймзону для надёжности (иначе возможен UTC на некоторых системах)
        tz = None
        try:
            tzname = os.getenv("WB_TZ") or os.getenv("TZ")
            if not tzname:
                try:
                    with open("/etc/timezone") as f:
                        tzname = f.read().strip()
                except (OSError, PermissionError) as e:
                    logger.debug("Exception in __init__: %s", e)
                    tzname = None
            if ZoneInfo and tzname:
                tz = ZoneInfo(tzname)
        except (OSError, PermissionError) as e:
            logger.debug("Exception in __init__: %s", e)
            tz = None
        # Инициализация APScheduler с SQLAlchemyJobStore (PHYS-2 / MASTER-H10).
        #
        # Persistence contract:
        #   * 'default' → SQLAlchemyJobStore on a DEDICATED file `jobs.db`
        #     (sibling of irrigation.db). Separate DB keeps APScheduler
        #     WAL writes away from application backups and simplifies
        #     disaster recovery — `jobs.db` can be deleted without losing
        #     zones/programs/settings.
        #   * 'volatile' → MemoryJobStore for one-shot helper jobs that
        #     must not survive a restart (e.g., delayed_close tracking).
        #
        # If SQLAlchemy is missing (should not happen — it is now pinned in
        # requirements.txt), we fall back to MemoryJobStore and emit a
        # loud WARNING: persistence is a SAFETY property, not a convenience.
        scheduler_kwargs = {}
        jobstores: dict[str, Any] = {}
        jobstore_backend = "none"
        try:
            if SQLAlchemyJobStore is not None:
                # Dedicated file alongside irrigation.db
                db_dir = os.path.dirname(os.path.abspath(self.db.db_path)) or "."
                jobs_db_path = os.path.join(db_dir, "jobs.db")
                jobstores["default"] = SQLAlchemyJobStore(url=f"sqlite:///{jobs_db_path}")
                jobstore_backend = "sqlalchemy"
            elif MemoryJobStore is not None:
                # Degraded mode — persistence lost, but we still want to run
                jobstores["default"] = MemoryJobStore()
                jobstore_backend = "memory-fallback"
                logger.warning(
                    "APScheduler: SQLAlchemy unavailable, falling back to MemoryJobStore — "
                    "one-shot zone_stop/master_close jobs WILL be lost on restart (PHYS-2 risk)."
                )
            if MemoryJobStore is not None:
                jobstores["volatile"] = MemoryJobStore()  # эфемерные задачи (не требуют persist)
            if jobstores:
                scheduler_kwargs["jobstores"] = jobstores
        except (sqlite3.Error, OSError) as e:
            logger.error("APScheduler jobstore init failed: %s", e)

        # Misfire / coalesce policy for persistent jobs (PHYS-2).
        # If the service was down during a zone_stop time, we still want the
        # job to fire within 5 min of restart (misfire_grace_time=300). If
        # multiple fires accumulated (e.g., laptop suspend) coalesce into one.
        # max_instances=1 prevents the same job running in parallel.
        scheduler_kwargs["job_defaults"] = {
            "coalesce": True,
            "misfire_grace_time": 300,
            "max_instances": 1,
        }

        self.scheduler = (
            _DeterministicBackgroundScheduler(timezone=tz, **scheduler_kwargs)
            if tz
            else _DeterministicBackgroundScheduler(**scheduler_kwargs)
        )
        # Флаги доступности jobstore-ов + backend идентификация для health endpoints
        self.jobstore_backend = jobstore_backend
        try:
            stores = getattr(self.scheduler, "_jobstores", {}) or {}
            self.has_default_jobstore = "default" in stores
            self.has_volatile_jobstore = "volatile" in stores
        except (KeyError, TypeError, ValueError) as e:
            logger.debug("Exception in line_195: %s", e)
            self.has_default_jobstore = False
            self.has_volatile_jobstore = False
        self.active_zones: dict[int, datetime] = {}
        self.program_jobs: dict[int, list[str]] = {}  # program_id -> list(job_id)
        self.is_running = False
        self._program_jobs_lock = threading.RLock()
        self._group_session_lock = threading.RLock()
        self.group_cancel_events: dict[int, threading.Event] = {}
        # Distinguish an executor-owned session from a DateTrigger that is
        # merely pending.  Cancelling a pending ``group_seq`` must release its
        # claim because no runner ``finally`` block will ever execute.
        self._running_group_sessions: set[int] = set()
        # Per-group "skip current zone" events. Lifetime mirrors group_cancel_events:
        # populated lazily by request_skip_current_zone, cleared in the same finally
        # block as the cancel event. The in-thread sequencer loop polls .is_set()
        # alongside cancel/shutdown checks.
        self.group_skip_current_events: dict[int, threading.Event] = {}
        # Issue #14 C2: per-group monotonic-clock timestamp of the last
        # *successful* skip request, for server-side debounce. The frontend
        # 1500ms guard is bypassable (multi-tab, mobile+desktop, scripted
        # callers); this is the authoritative second layer.
        self._last_skip_ts: dict[int, float] = {}
        self._skip_debounce_seconds: float = 1.0
        # Shutdown event: set to interrupt all sleeping threads for graceful stop
        self._shutdown_event = threading.Event()
        # Runner lifecycle fence.  ``quiesce`` closes admission first and then
        # waits for every admitted program/group runner to leave.  This lets the
        # process shutdown path publish its final retained OFF only after no
        # scheduler-owned worker can publish a later ON.
        self._runner_condition = threading.Condition(threading.Lock())
        self._active_runner_count = 0
        self._accepting_runs = True
        # Boot recovery is two-phase: init_scheduler starts APScheduler paused
        # and records which zones were interrupted; app startup calls
        # complete_boot_recovery only after its physical OFF reconciliation.
        self._boot_interrupted_zone_ids: set[int] = set()
        self._boot_recovery_completed = False
        self._started_paused = False
        self._boot_reconcile_ok: bool | None = None
        self._boot_reconcile_failures: set[str] = set()
        self._boot_recovery_intent_lock = threading.RLock()
        self._boot_recovery_handoff_durable = False

    def _controller_timezone(self) -> tzinfo | None:
        value = getattr(self.scheduler, "timezone", None)
        return value if isinstance(value, tzinfo) else None

    def _controller_now(self, *, naive: bool = False) -> datetime:
        """Return now in the scheduler/controller timezone.

        ``WB_TZ`` may intentionally differ from the process ``TZ``.  All
        scheduler-owned wall-clock calculations must therefore derive from the
        APScheduler timezone rather than bare ``datetime.now()``.
        """
        timezone = self._controller_timezone()
        value = datetime.now(timezone) if timezone is not None else datetime.now().astimezone()
        return value.replace(tzinfo=None) if naive else value

    def _normalise_caller_run_at(self, value: datetime) -> datetime:
        """Convert an external run instant into the controller timezone.

        Legacy callers pass naive values produced by process-local
        ``datetime.now()``.  They must be interpreted in ``TZ`` first, not as
        WB/controller wall time, otherwise differing ``TZ``/``WB_TZ`` values
        can collapse a multi-minute deadline to the one-second past clamp.
        """
        if not isinstance(value, datetime):
            raise TypeError("run_at must be a datetime")
        if value.tzinfo is None:
            source_timezone: tzinfo | None = None
            source_name = os.getenv("TZ")
            if ZoneInfo is not None and source_name:
                try:
                    source_timezone = ZoneInfo(source_name)
                except (ValueError, OSError, KeyError):
                    logger.warning("Invalid process TZ %r; using host local timezone", source_name)
            if source_timezone is None:
                source_timezone = datetime.now().astimezone().tzinfo
            value = value.replace(tzinfo=source_timezone)
        controller_timezone = self._controller_timezone()
        return value.astimezone(controller_timezone) if controller_timezone is not None else value.astimezone()

    def _current_activation_token(self, zone_id: int) -> str | None:
        try:
            zone = self.db.get_zone(int(zone_id))
            token = (zone or {}).get("command_id") or (zone or {}).get("watering_start_time")
            return None if token in (None, "") else str(token)
        except (sqlite3.Error, OSError, TypeError, ValueError, KeyError):
            logger.debug("activation token lookup failed for zone %s", zone_id, exc_info=True)
            return None

    def _zone_requires_observed_off(self, zone_id: int) -> bool:
        """Whether this zone has a physical MQTT channel to confirm."""
        try:
            zone = self.db.get_zone(int(zone_id)) or {}
            return bool(zone.get("mqtt_server_id") and str(zone.get("topic") or "").strip())
        except (sqlite3.Error, OSError, TypeError, ValueError, KeyError):
            # Unknown topology is safety-sensitive: retain the retry.
            logger.warning("Could not classify zone %s physical channel; retaining safety", zone_id)
            return True

    @property
    def active_runner_count(self) -> int:
        with self._runner_condition:
            return self._active_runner_count

    def _begin_runner(self) -> bool:
        """Admit one scheduler-owned runner unless shutdown has fenced starts."""
        with self._runner_condition:
            if not self._accepting_runs or self._shutdown_event.is_set():
                return False
            self._active_runner_count += 1
            return True

    def _finish_runner(self) -> None:
        with self._runner_condition:
            self._active_runner_count = max(0, self._active_runner_count - 1)
            self._runner_condition.notify_all()

    @staticmethod
    def _is_emergency_stop_active() -> bool:
        """Read the shared Flask config without requiring a worker app context.

        Flask cron workers have no request/app context.  The SSE hub already
        receives the live ``app.config`` mapping during application bootstrap,
        so it is the process-local source available to both HTTP and workers.
        ``current_app`` remains useful for direct calls made inside a context.
        """
        try:
            from flask import current_app, has_app_context

            if has_app_context():
                return bool(current_app.config.get("EMERGENCY_STOP"))
        except (ImportError, RuntimeError, TypeError):
            pass
        # Test suites create many short-lived Flask apps while the injected
        # SSE config is process-global.  Outside an app context that mapping
        # may belong to a finished fixture; production has one long-lived app.
        if TESTING:
            return False
        try:
            from services import sse_hub

            app_config = getattr(sse_hub, "_app_config", None)
            return bool(app_config and app_config.get("EMERGENCY_STOP"))
        except (ImportError, AttributeError, RuntimeError, TypeError):
            return False

    @staticmethod
    def _is_group_rain_blocked(group_id: int) -> bool:
        """Read the persistent rain admission gate.

        Older installations may not expose the gate yet, so an absent module
        API stays backward compatible.  Once the API exists, however, an
        exception is not permission to energise a relay: fail closed until the
        monitor can make an explicit dry decision.
        """
        try:
            from services.monitors.rain_monitor import is_group_blocked
        except ImportError:
            return False
        try:
            return bool(is_group_blocked(int(group_id)))
        except Exception:
            logger.exception("Rain admission gate failed for group %s; blocking start", group_id)
            return True

    @staticmethod
    def _callable_accepts_keyword(function: object, keyword: str) -> bool:
        """Feature-detect a cross-package keyword during rolling integration."""
        try:
            parameters = inspect.signature(function).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.name == keyword or parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
        )

    def start(self, paused: bool = False):
        if self.is_running:
            return
        self._shutdown_event.clear()
        with self._runner_condition:
            self._accepting_runs = True
        self.scheduler.start(paused=bool(paused))
        self.is_running = True
        self._started_paused = bool(paused)
        try:
            backend = getattr(self, "jobstore_backend", "unknown")
            tz = str(getattr(self.scheduler, "timezone", "default"))
            logger.info(
                "Планировщик полива (APScheduler) запущен, timezone=%s, jobstore=%s (PHYS-2)",
                tz,
                backend,
            )
            # Log the count of restored persistent jobs — observability for
            # restart-recovery. If jobstore=memory-fallback this will always be 0.
            try:
                restored = len(self.scheduler.get_jobs(jobstore="default") or [])
                logger.info("Восстановлено из persistent jobstore: %d задач", restored)
            except (AttributeError, KeyError, ValueError) as _e:
                logger.debug("restored jobs count failed: %s", _e)
        except (ValueError, TypeError, KeyError):
            logger.info("Планировщик полива (APScheduler) запущен")
        # Плановый джоб: регулярная очистка истекших отложек
        try:
            self.schedule_postpone_sweeper()
        except (OSError, ValueError) as e:
            logger.error(f"Не удалось запланировать очистку отложек: {e}")
        # Плановый джоб: ежедневная очистка audit_log (>7 дней)
        try:
            self.schedule_audit_cleanup()
        except (OSError, ValueError) as e:
            logger.error(f"Не удалось запланировать очистку audit_log: {e}")
        # Плановый джоб: ежедневный бэкап БД (03:15)
        try:
            self.schedule_daily_backup()
        except (OSError, ValueError) as e:
            logger.error(f"Не удалось запланировать ежедневный бэкап: {e}")
        # Плановый джоб: ночной пересчёт водного баланса (H2, 03:30)
        try:
            self.schedule_water_balance_recalc()
        except (OSError, ValueError) as e:
            logger.error(f"Не удалось запланировать пересчёт водного баланса: {e}")

    def quiesce(self, timeout_seconds: float = 10.0) -> bool:
        """Fence new starts, cancel active sessions, and wait boundedly.

        Returns ``True`` only when all admitted program/group runners have
        exited.  The admission fence remains closed after a timeout, so callers
        can report degraded shutdown without reopening a late-ON race.
        """
        try:
            timeout = max(0.0, float(timeout_seconds))
        except (TypeError, ValueError):
            timeout = 0.0
        deadline = time.monotonic() + timeout
        with self._runner_condition:
            self._accepting_runs = False
            self._shutdown_event.set()
        with self._group_session_lock:
            for event in list(self.group_cancel_events.values()):
                event.set()
        try:
            if self.is_running:
                self.scheduler.pause()
                self._started_paused = True
        except (RuntimeError, AttributeError):
            logger.debug("quiesce: scheduler pause skipped", exc_info=True)
        with self._runner_condition:
            while self._active_runner_count > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._runner_condition.wait(timeout=remaining)
            return True

    def stop(self, wait: bool = False, timeout_seconds: float = 10.0) -> bool:
        if not self.is_running:
            return self.quiesce(timeout_seconds=timeout_seconds)
        drained = self.quiesce(timeout_seconds=timeout_seconds)
        # Never turn a bounded drain into an unbounded shutdown wait.  Once
        # drained, wait=True is safe because scheduler-owned watering workers
        # are already gone.
        self.scheduler.shutdown(wait=bool(wait and drained))
        self.is_running = False
        logger.info("Планировщик полива остановлен")
        return drained

    # --- Отложки: парсинг и фоновая очистка ---
    @staticmethod
    def _parse_dt(s: str | None) -> datetime | None:
        if not s:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(s, fmt)
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in _parse_dt: %s", e)
                continue
        return None

    def _clear_zone_postpone_if_expired(
        self,
        zone_id: int,
        observed_deadline: str,
        now: datetime,
    ) -> bool:
        """Clear only the exact expired deadline observed by this caller.

        HTTP, Telegram, rain-monitor and scheduler mutations can race.  A
        stale sweeper snapshot must never erase a newer/longer manual safety
        deadline.  The shared postpone service owns the process lock after its
        Phase-4 integration; the SQL fallback preserves the same exact-CAS
        contract for isolated scheduler deployments and tests.
        """
        observed = self._parse_dt(observed_deadline)
        if observed is None or now.tzinfo is not None or observed > now:
            return False
        try:
            from services.postpone import clear_zone_postpone_if_expired as _clear_expired
        except ImportError:
            _clear_expired = None

        if _clear_expired is not None:
            try:
                return bool(
                    _clear_expired(
                        int(zone_id),
                        observed_deadline,
                        now,
                        db_facade=self.db,
                    )
                )
            except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError, KeyError):
                logger.exception("Shared postpone expiry CAS failed for zone %s", zone_id)
                return False

        try:
            with sqlite3.connect(self.db.db_path, timeout=5) as conn:
                cursor = conn.execute(
                    "UPDATE zones SET postpone_until = NULL, postpone_reason = NULL, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND postpone_until = ?",
                    (int(zone_id), observed_deadline),
                )
                conn.commit()
            return cursor.rowcount == 1
        except (sqlite3.Error, OSError, TypeError, ValueError):
            logger.exception("Fallback postpone expiry CAS failed for zone %s", zone_id)
            return False

    def clear_expired_postpones(self) -> None:
        """Сбрасывает отложенный полив для зон, у которых срок истек."""
        try:
            zones = self.db.get_zones()
            now = self._controller_now(naive=True)
            expired: list[tuple[int, str]] = []
            for z in zones:
                pu = z.get("postpone_until")
                if not pu:
                    continue
                dt = self._parse_dt(pu)
                if dt is None:
                    logger.error("Malformed postpone deadline retained for zone %s", z.get("id"))
                    continue
                if now >= dt:
                    expired.append((int(z["id"]), str(pu)))
            cleared: list[int] = []
            for zone_id, observed_deadline in expired:
                try:
                    if not self._clear_zone_postpone_if_expired(zone_id, observed_deadline, now):
                        continue
                    cleared.append(zone_id)
                    try:
                        self.db.add_log("postpone_expired", json.dumps({"zone": zone_id}))
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                        logger.debug("Handled exception in clear_expired_postpones: %s", e)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    logger.error(f"Не удалось сбросить отложку для зоны {zone_id}: {e}")
            if cleared:
                logger.info(f"Сброшены истекшие отложки зон: {cleared}")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.error(f"Ошибка очистки истекших отложек: {e}")

    def schedule_postpone_sweeper(self) -> None:
        """Планирует периодическую очистку истекших отложек (каждую минуту)."""
        try:
            if self.scheduler.get_job("postpone_sweeper") is not None:
                logger.info("postpone_sweeper restored; next_run_time preserved")
            else:
                self.scheduler.add_job(
                    job_clear_expired_postpones,
                    trigger=IntervalTrigger(minutes=1, timezone=self._controller_timezone()),
                    id="postpone_sweeper",
                    replace_existing=False,
                    coalesce=False,
                    max_instances=1,
                    next_run_time=self._controller_now(),
                )
            # первая отработка — немедленно
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Не удалось добавить джоб postpone_sweeper: {e}")
        try:
            if self.scheduler.get_job("bot_sub_dispatcher") is not None:
                logger.info("bot_sub_dispatcher restored; next_run_time preserved")
            else:
                self.scheduler.add_job(
                    job_dispatch_bot_subscriptions,
                    trigger=IntervalTrigger(minutes=1, timezone=self._controller_timezone()),
                    id="bot_sub_dispatcher",
                    replace_existing=False,
                    coalesce=False,
                    max_instances=1,
                    next_run_time=self._controller_now(),
                )
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Не удалось добавить джоб bot_sub_dispatcher: {e}")

    def schedule_audit_cleanup(self) -> None:
        """Plan daily audit_log cleanup at 03:30 (rows older than 7 days)."""
        try:
            if self.scheduler.get_job("audit_cleanup") is not None:
                logger.info("audit_cleanup restored; next_run_time preserved")
                return
            self.scheduler.add_job(
                job_audit_cleanup,
                trigger=CronTrigger(hour=3, minute=30, timezone=self._controller_timezone()),
                id="audit_cleanup",
                replace_existing=False,
                coalesce=True,
                max_instances=1,
            )
            logger.info("audit_cleanup job scheduled: daily at 03:30")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Не удалось добавить джоб audit_cleanup: {e}")

    def schedule_daily_backup(self) -> None:
        """Plan daily DB backup at 03:15 (sanity-checked, see LogRepository.create_backup)."""
        try:
            if self.scheduler.get_job("daily_backup") is not None:
                logger.info("daily_backup restored; next_run_time preserved")
                return
            self.scheduler.add_job(
                job_daily_backup,
                trigger=CronTrigger(hour=3, minute=15, timezone=self._controller_timezone()),
                id="daily_backup",
                name="daily DB backup",
                replace_existing=False,
                coalesce=True,
                max_instances=1,
            )
            logger.info("daily_backup job scheduled: daily at 03:15")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Не удалось добавить джоб daily_backup: {e}")

    def schedule_water_balance_recalc(self) -> None:
        """Plan nightly H2 water-balance recalc at 03:35 (before morning watering).

        Job body lives in ``scheduler.jobs.job_recalc_water_balance`` (a stable
        top-level importable, required for APScheduler persistence). Runs every
        night regardless of the ``balance.enabled`` flag so shadow mode keeps
        accumulating the audit log. 03:35 (not 03:30) to avoid sharing a slot
        with ``audit_cleanup``.
        """
        try:
            from scheduler.jobs import job_recalc_water_balance

            if self.scheduler.get_job("water_balance_recalc") is not None:
                logger.info("water_balance_recalc restored; next_run_time preserved")
                return
            self.scheduler.add_job(
                job_recalc_water_balance,
                trigger=CronTrigger(hour=3, minute=35, timezone=self._controller_timezone()),
                id="water_balance_recalc",
                name="nightly water-balance recalc",
                replace_existing=False,
                coalesce=True,
                max_instances=1,
            )
            logger.info("water_balance_recalc job scheduled: daily at 03:35")
        except (ValueError, TypeError, KeyError, ImportError) as e:
            logger.error(f"Не удалось добавить джоб water_balance_recalc: {e}")

    def _stop_zone(
        self,
        zone_id: int,
        *,
        reason: str = "auto_stop",
        activation_token: str | None = None,
        force: bool = False,
    ) -> bool:
        """Issue a physical OFF and keep a retry armed until it succeeds.

        A failed publish must never be converted into a DB-only ``off`` state:
        that would hide the energised relay from both watchdogs.  The current
        activation token is captured before the command so a delayed retry
        cannot stop a later activation of the same zone.
        """
        zid = int(zone_id)
        token = activation_token if activation_token is not None else self._current_activation_token(zid)
        try:
            from services.zone_control import stop_zone as _stop_zone_central

            stopped = bool(_stop_zone_central(zid, reason=reason, force=bool(force)))
        except (sqlite3.Error, OSError, ImportError, ValueError, TypeError):
            logger.exception("Scheduler OFF failed for zone %s", zid)
            stopped = False

        if not stopped:
            # A DateTrigger removes itself when submitted.  Re-arm the exact
            # hard-stop id so every failed OFF retains a future retry.
            retry_at = self._controller_now() + timedelta(seconds=30)
            try:
                self.schedule_zone_hard_stop(zid, retry_at, activation_token=token)
            except (RuntimeError, ValueError, TypeError, KeyError):
                logger.exception("Failed to re-arm OFF retry for zone %s", zid)
            logger.error("Zone %s OFF unresolved; safety retry retained", zid)
            return False

        if not self._zone_requires_observed_off(zid):
            # DB-only/virtual zones have no report channel and therefore no
            # later physical echo. Their completed central transition is final.
            self.cancel_zone_jobs(zid, include_cap=True)
        else:
            # A broker ACK is not physical OFF confirmation. Remove ordinary
            # deadlines, but retain the activation's hard/cap safety ownership
            # until the verifier/SSE path receives a fresh observed-OFF echo.
            self.cancel_zone_jobs(zid, preserve_safety=True)
            if not self._has_zone_safety_job(zid):
                retry_at = self._controller_now() + timedelta(seconds=30)
                replanted = self.schedule_zone_hard_stop(zid, retry_at, activation_token=token)
                if replanted is False:
                    logger.critical("Zone %s OFF acknowledged without a surviving safety retry", zid)
                    return False
        try:
            zone = self.db.get_zone(zid)
            if zone:
                self.db.add_log("zone_auto_stop", f"Зона {zid} ({zone['name']}) автоматически остановлена")
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
            logger.debug("zone_auto_stop log failed for %s", zid, exc_info=True)
        logger.info("Зона %s остановлена", zid)
        return True

    def _check_weather_skip(self, zone_id: int, program_id: int = 0) -> dict:
        """Check if watering should be skipped due to weather. Returns skip info dict."""
        try:
            from services.weather_adjustment import get_weather_adjustment

            adj = get_weather_adjustment(self.db.db_path)
            if not adj.is_enabled():
                return {"skip": False}
            skip_info = adj.should_skip()
            if skip_info.get("skip"):
                reason = skip_info.get("reason", "weather")
                logger.info(f"Weather skip: zone={zone_id} program={program_id} reason={reason}")
                try:
                    self.db.add_log(
                        "weather_skip",
                        json.dumps(
                            {
                                "zone_id": zone_id,
                                "program_id": program_id,
                                "reason": reason,
                                "details": skip_info.get("details", {}),
                            }
                        ),
                    )
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    logger.debug("Weather skip log error: %s", e)
                # Send Telegram notification for weather skip
                try:
                    from services.telegram_bot import notifier

                    chat_id = self.db.get_setting_value("telegram_admin_chat_id")
                    if chat_id:
                        skip_type = skip_info.get("details", {}).get("type", "weather")
                        emoji = {"rain": "🌧", "rain_forecast": "🌧", "freeze": "❄️", "wind": "💨"}.get(skip_type, "⛅")
                        notifier.send_text(int(chat_id), f"{emoji} Полив пропущен: {reason}")
                except (ImportError, OSError, ValueError, TypeError) as e:
                    logger.debug("Weather skip telegram: %s", e)
                # Log to weather_log
                try:
                    zone_data = self.db.get_zone(zone_id)
                    original = int(zone_data["duration"]) if zone_data else 0
                    _w = adj._get_weather()
                    _snap = _w.to_dict() if _w is not None else None
                    adj.log_adjustment(zone_id, original, 0, 0, True, reason, weather_snapshot=_snap)
                except (sqlite3.Error, OSError) as e:
                    logger.debug("Weather log error: %s", e)
            return skip_info
        except (ImportError, OSError, ValueError, TypeError) as e:
            logger.debug("Weather check error: %s", e)
            return {"skip": False}

    def _get_weather_adjusted_duration(self, zone_id: int, base_duration: int) -> int:
        """Get weather-adjusted zone duration."""
        try:
            from services.weather_adjustment import get_weather_adjustment

            adj = get_weather_adjustment(self.db.db_path)
            if not adj.is_enabled():
                return base_duration
            coeff = adj.get_coefficient()
            adjusted = round(base_duration * coeff / 100.0)
            adjusted = 0 if coeff == 0 else max(1, adjusted)
            if adjusted != base_duration:
                logger.info(
                    f"Weather adjustment: zone={zone_id} base={base_duration}min adjusted={adjusted}min (coeff={coeff}%)"
                )
            try:
                _w = adj._get_weather()
                _snap = _w.to_dict() if _w is not None else None
                adj.log_adjustment(zone_id, base_duration, adjusted, coeff, False, "", weather_snapshot=_snap)
            except (sqlite3.Error, OSError) as e:
                logger.debug("Weather log error: %s", e)
            return adjusted
        except (ImportError, OSError, ValueError, TypeError) as e:
            logger.debug("Weather adjustment error: %s", e)
            return base_duration

    def _run_program_threaded(
        self,
        program_id: int,
        zones: list[int],
        program_name: str,
        manual: bool = False,
    ) -> bool:
        """Последовательный запуск зон в отдельном потоке, чтобы не блокировать APScheduler.

        Issue #31: ``manual=True`` — ручной запуск из UI/API. Bypass weather skip
        и weather-adjusted duration (пользователь сам решил полить, погода — его
        ответственность). По умолчанию manual=False — scheduled cron jobs
        продолжают уважать погодные ограничения.
        """
        if self._is_emergency_stop_active():
            logger.warning("Программа %s заблокирована: активен EMERGENCY_STOP", program_id)
            return False
        # Issue #16 §6.4 + Issue #14 C1: pre-register a cancel-event for
        # every distinct group this program will touch, so
        # is_group_session_active(gid) returns True while the program is in
        # flight.  Without this:
        # - scheduled-program runs would be invisible to the API layer's
        #   session detector → single-zone stop endpoints would fall through
        #   to the solo path (issue #16 bug), and the skip-current endpoint
        #   would 400 for all scheduled programs (issue #14 C1).
        #
        # We use dict.setdefault (atomic in CPython, single PyDict_SetDefault
        # bytecode) so a concurrent start_group_sequence's already-planted
        # Event is preserved.  We track the (gid, our_event) tuples THIS
        # invocation actually planted so the finally cleanup below pops
        # only entries that still hold OUR Event identity, never one a
        # concurrent sequence owns.  Mirrors the lifecycle in the
        # `finally` block of `_run_group_sequence`.
        registered_gids: list[tuple[int, threading.Event]] = []
        # Compute program_gids ONCE up front: needed by both the manual-vs-
        # scheduled guard (must run BEFORE pre-register so it doesn't see
        # our own planted events) and by the pre-register block.
        program_gids: set = set()
        try:
            for z in zones:
                try:
                    zd = self.db.get_zone(z)
                    if not zd:
                        continue
                    g = int(zd.get("group_id") or 0)
                    # Skip the "no group" sentinels (gid==0 unset, gid==999
                    # is the legacy "ungrouped" bucket per project convention).
                    if g and g != 999:
                        program_gids.add(g)
                except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                    logger.debug("_run_program_threaded: gid lookup failed for zone=%s: %s", z, e)
        except (sqlite3.Error, OSError, ValueError, TypeError) as e:
            logger.debug("_run_program_threaded: program_gids collection failed: %s", e)
        blocked_gids = sorted(gid for gid in program_gids if self._is_group_rain_blocked(gid))
        if blocked_gids:
            logger.warning("Программа %s заблокирована дождём для групп %s", program_id, blocked_gids)
            return False
        # Admission happens after fallible DB discovery.  If quiesce lands
        # during that lookup, this check rejects the run; if discovery raises,
        # no tracked-runner slot can leak.
        if not self._begin_runner():
            logger.info("Программа %s не запущена: scheduler quiescing", program_id)
            return False
        run_ok = True
        try:
            # ----- Issue #15 manual-vs-scheduled guard -----
            # If a manual / ad-hoc run owns ANY group this program touches,
            # skip this scheduled fire entirely. APScheduler's normal cron tick
            # will fire the next slot AFTER manual ends.
            # MUST run BEFORE the pre-register block below — otherwise the
            # guard would see our own planted cancel events and self-block.
            # NOTE: APScheduler misfire_grace_time caps how long a missed fire
            # stays valid. Older fires (>grace) are dropped silently and we
            # don't see them — see specs/issue-15-architecture.md §1.6 + §6.2
            # for the misfire-window logging gap (deferred to a follow-up).
            try:
                # Check-and-claim is one critical section.  A separate
                # is-active check followed by setdefault allowed two program
                # fires (or a manual group start) to both proceed.
                with self._group_session_lock:
                    blocking_gids = [g for g in program_gids if g in self.group_cancel_events]
                    if not blocking_gids:
                        for gid in program_gids:
                            new_event = threading.Event()
                            self.group_cancel_events[gid] = new_event
                            self._running_group_sessions.add(gid)
                            registered_gids.append((gid, new_event))
                if blocking_gids:
                    try:
                        self.db.add_log(
                            "prog_skipped_manual_running",
                            json.dumps(
                                {
                                    "program_id": program_id,
                                    "program_name": program_name,
                                    "blocking_gids": blocking_gids,
                                    "scheduled_at": self._controller_now(naive=True).strftime("%Y-%m-%d %H:%M:%S"),
                                }
                            ),
                        )
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as _e:
                        logger.debug("prog_skipped_manual_running log failed: %s", _e)
                    try:
                        from services.audit import record_audit

                        record_audit(
                            action_type="prog_skipped_manual_running",
                            source="scheduler",
                            target=f"program:{program_id}",
                            payload={
                                "program_id": program_id,
                                "program_name": program_name,
                                "blocking_gids": blocking_gids,
                            },
                            actor="system",
                        )
                    except Exception:
                        logger.exception("prog_skipped_manual_running audit failed")
                    logger.info(
                        "Программа %s (%s) пропущена: ручной запуск активен в группах %s",
                        program_id,
                        program_name,
                        blocking_gids,
                    )
                    return False
            except (sqlite3.Error, OSError, ValueError, TypeError) as _e:
                logger.debug("manual-vs-scheduled guard error: %s", _e)
            # ----- end issue #15 guard -----
            logger.info(f"Запуск программы {program_id} ({program_name})")
            try:
                self.db.add_log("program_start", json.dumps({"program_id": program_id, "program_name": program_name}))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                logger.debug("Handled exception in _run_program_threaded: %s", e)

            # Weather check before program: skip entire program if conditions are bad.
            # Issue #31: manual runs bypass weather skip — user just looked at the
            # sky and pressed Run, system must not second-guess.
            if not manual:
                skip_info = self._check_weather_skip(zones[0] if zones else 0, program_id)
                try:
                    from services.weather_adjustment import get_weather_adjustment

                    _adj = get_weather_adjustment(self.db.db_path)
                    if _adj.is_enabled():
                        _w = _adj._get_weather()
                        _coeff = _adj.get_coefficient()
                        _adj.log_decision(_w, _coeff, bool(skip_info.get("skip")), skip_info.get("reason", ""))
                except Exception as e:
                    logger.debug("log_decision error: %s", e)
                if skip_info.get("skip"):
                    logger.info(
                        f"Программа {program_id} ({program_name}) пропущена из-за погоды: {skip_info.get('reason')}"
                    )
                    try:
                        self.db.add_log(
                            "program_weather_skip",
                            json.dumps(
                                {
                                    "program_id": program_id,
                                    "program_name": program_name,
                                    "reason": skip_info.get("reason", ""),
                                }
                            ),
                        )
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                        logger.debug("Program weather skip log error: %s", e)
                    return True

            for _i, zone_id in enumerate(zones):
                if self._shutdown_event.is_set() or self._is_emergency_stop_active():
                    logger.info("Программа %s остановлена до запуска следующей зоны", program_id)
                    run_ok = False
                    break
                zone = self.db.get_zone(zone_id)
                if not zone:
                    logger.warning(f"Зона {zone_id} не найдена")
                    continue
                if str(zone.get("state") or "").lower() == "fault":
                    logger.warning("Программа %s: fault-зона %s исключена из запуска", program_id, zone_id)
                    continue

                # Если для группы зоны установлена отмена, пропускаем её
                group_id = int(zone.get("group_id") or 0)
                # Проверяем отмену текущего запуска программы для этой группы на сегодня
                try:
                    today = self._controller_now(naive=True).strftime("%Y-%m-%d")
                    from database import db as _db

                    if _db.is_program_run_cancelled_for_group(int(program_id), today, int(group_id)):
                        logger.info(
                            f"Программа {program_id}: отменена для группы {group_id} на {today}, зона {zone_id} пропущена"
                        )
                        continue
                except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                    logger.debug("Handled exception in _run_program_threaded: %s", e)
                cancel_event = self.group_cancel_events.get(group_id)
                if cancel_event and cancel_event.is_set():
                    logger.info(f"Программа {program_id}: группа {group_id} отменена, зона {zone_id} пропущена")
                    continue
                # Drop a stale skip event from a previous zone — see _run_group_sequence comment.
                _stale_skip = self.group_skip_current_events.get(group_id)
                if _stale_skip and _stale_skip.is_set():
                    _stale_skip.clear()
                skipped_this_zone = False

                # Проверяем отложенный полив
                postpone_until = zone.get("postpone_until")
                if postpone_until:
                    postpone_dt = self._parse_dt(postpone_until)
                    postpone_now = self._controller_now(naive=True)
                    if postpone_dt is None:
                        # Malformed persisted safety state is not permission to
                        # water. Keep it visible for operator remediation.
                        logger.error("Программа %s: некорректная отложка зоны %s сохранена", program_id, zone_id)
                        continue
                    if postpone_now < postpone_dt:
                        logger.info(f"Зона {zone_id} отложена до {postpone_until}")
                        continue
                    if not self._clear_zone_postpone_if_expired(
                        zone_id,
                        str(postpone_until),
                        postpone_now,
                    ):
                        # The deadline changed after our snapshot or could not
                        # be cleared safely. Re-read and fail closed for this
                        # run instead of watering through a concurrent manual
                        # or rain postpone.
                        latest = self.db.get_zone(zone_id) or {}
                        if latest.get("postpone_until"):
                            logger.info(
                                "Программа %s: отложка зоны %s изменилась конкурентно; запуск пропущен",
                                program_id,
                                zone_id,
                            )
                            continue

                # Issue #31: manual runs use full zone duration without weather coefficient.
                if manual:
                    duration = int(zone["duration"])
                else:
                    duration = self._get_weather_adjusted_duration(zone_id, int(zone["duration"]))
                if duration <= 0:
                    logger.info(
                        f"Программа {program_id}: зона {zone_id} имеет нулевую длительность (weather coef=0), пропуск"
                    )
                    continue

                # Quiesce/emergency may have landed during DB/weather work.
                # Fence again immediately before the state/MQTT start path.
                if self._shutdown_event.is_set() or self._is_emergency_stop_active():
                    logger.info("Программа %s остановлена перед ON зоны %s", program_id, zone_id)
                    run_ok = False
                    break
                if group_id not in {0, 999} and self._is_group_rain_blocked(group_id):
                    logger.warning("Программа %s: группа %s заблокирована дождём перед ON", program_id, group_id)
                    run_ok = False
                    break

                # The central controller owns the fault guard, state machine,
                # peer OFF, master valve, MQTT ON, and zone_run creation.  Do
                # not prewrite state='on': that erases fault and makes the
                # controller mistake the zone for an already-running no-op.
                try:
                    from services.zone_control import exclusive_start_zone as _start_central

                    started = bool(_start_central(int(zone_id), source="program"))
                except (sqlite3.Error, OSError, ImportError, ValueError, TypeError):
                    logger.exception("Программа %s: central start failed zone=%s", program_id, zone_id)
                    started = False
                if not started:
                    logger.warning("Программа %s: зона %s не стартовала, пропуск success-учёта", program_id, zone_id)
                    run_ok = False
                    break
                try:
                    latest_state = str((self.db.get_zone(int(zone_id)) or {}).get("state") or "").lower()
                    if latest_state == "fault":
                        logger.error("Программа %s: зона %s перешла в fault при старте", program_id, zone_id)
                        run_ok = False
                        break
                    end_time = self._controller_now() + timedelta(minutes=duration)
                    self.active_zones[zone_id] = end_time
                    # write planned_end_time for watchdogs/diagnostics
                    try:
                        planned_str = end_time.strftime("%Y-%m-%d %H:%M:%S")
                        metadata = {
                            "planned_end_time": planned_str,
                            "watering_start_source": "schedule",
                        }
                        # Compatibility for legacy DB-only virtual zones on
                        # older controllers and TESTING synthetic transports.
                        # Physical state remains owned by exclusive_start_zone;
                        # most importantly, never turn a fault result back ON.
                        legacy_virtual_state = latest_state in {"off", "starting"}
                        if legacy_virtual_state and (not zone.get("mqtt_server_id") or not zone.get("topic")):
                            metadata["state"] = "on"
                        self.db.update_zone(zone_id, metadata)
                    except (sqlite3.Error, OSError) as e:
                        logger.debug("Handled exception in line_413: %s", e)
                    # Watchdog job
                    try:
                        self.schedule_zone_hard_stop(zone_id, end_time)
                    except (ValueError, KeyError, RuntimeError) as e:
                        logger.debug("Handled exception in line_418: %s", e)
                    self.db.add_log(
                        "zone_auto_start",
                        json.dumps(
                            {
                                "zone_id": zone_id,
                                "zone_name": zone["name"],
                                "program_id": program_id,
                                "program_name": program_name,
                                "duration": duration,
                                "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
                            }
                        ),
                    )
                except (json.JSONDecodeError, KeyError, TypeError, ValueError, sqlite3.Error, OSError) as e:
                    logger.error(f"Ошибка запуска зоны {zone_id}: {e}")
                    run_ok = False
                    break

                # Ждем окончания текущей зоны с ранним выключением, проверяя отмену группы каждую секунду
                # Раннее выключение настраивается в settings (0..15 сек)
                try:
                    from database import db as _db

                    early = int(_db.get_early_off_seconds())
                except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                    logger.debug("Exception in line_437: %s", e)
                    early = 3
                early = 0 if early < 0 else (15 if early > 15 else early)
                total_seconds = duration * 60
                if TESTING:
                    total_seconds = min(6, max(1, duration))
                    early = 0  # в тестовом режиме не усложняем тайминги
                remaining = max(0, total_seconds - early)
                interrupted_by_shutdown = False
                while remaining > 0:
                    cancel_event = self.group_cancel_events.get(group_id)
                    if cancel_event and cancel_event.is_set():
                        logger.info(
                            f"Программа {program_id}: отмена группы {group_id}, досрочно останавливаем зону {zone_id}"
                        )
                        break
                    skip_event = self.group_skip_current_events.get(group_id)
                    if skip_event and skip_event.is_set():
                        skip_event.clear()
                        skipped_this_zone = True
                        logger.info(f"Программа {program_id}: skip current zone {zone_id} (group {group_id})")
                        break
                    if self._shutdown_event.wait(timeout=1):
                        logger.info(f"Программа {program_id}: shutdown, досрочно останавливаем зону {zone_id}")
                        interrupted_by_shutdown = True
                        break
                    remaining -= 1

                # Keep hard/cap retries and active ownership until physical OFF
                # succeeds.  _stop_zone removes them only on confirmed success.
                stopped = self._stop_zone(int(zone_id), reason="auto")
                if not stopped:
                    logger.error("Программа %s: OFF зоны %s не подтверждён", program_id, zone_id)
                    run_ok = False
                    break

                if interrupted_by_shutdown:
                    run_ok = False
                    break

                if skipped_this_zone:
                    try:
                        self.db.add_log(
                            "zone_skip",
                            json.dumps(
                                {
                                    "program_id": program_id,
                                    "group_id": group_id,
                                    "zone_id": zone_id,
                                    "zone_name": zone.get("name"),
                                    "reason": "manual_skip",
                                }
                            ),
                        )
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                        logger.debug("zone_skip log error: %s", e)
                    continue

                # Дождёмся оставшиеся ранние секунды до «номинального» конца зоны, чтобы старт следующей был вовремя
                if early > 0:
                    waited = 0
                    while waited < early:
                        cancel_event = self.group_cancel_events.get(group_id)
                        if cancel_event and cancel_event.is_set():
                            break
                        if self._shutdown_event.wait(timeout=1):
                            break
                        waited += 1

                # Если отмена — пропускаем оставшиеся зоны этой группы, но не мешаем другим группам
                cancel_event = self.group_cancel_events.get(group_id)
                if cancel_event and cancel_event.is_set():
                    logger.info(
                        f"Программа {program_id}: отменена для группы {group_id}, продолжаем с другими группами (если есть)"
                    )
                    continue

            logger.info(f"Программа {program_id} ({program_name}) завершена")
            try:
                self.db.add_log("program_finish", json.dumps({"program_id": program_id, "program_name": program_name}))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                logger.debug("Handled exception in line_482: %s", e)
            return run_ok
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.error(f"Ошибка в выполнении программы {program_id}: {e}")
            return False
        finally:
            # Issue #16 §6.4 + Issue #14: identity-aware cleanup. Clear/pop
            # ONLY the cancel-events this invocation registered AND that the
            # dict still holds. If a concurrent start_group_sequence has
            # since replaced our entry with its own Event, the identity
            # check fails and we leave that Event alone — it belongs to the
            # sequence. Mirrors the cleanup in the `finally` block of
            # `_run_group_sequence`. Also drop any accumulated skip events
            # so the dict doesn't grow forever.
            for gid, our_event in registered_gids:
                try:
                    with self._group_session_lock:
                        self._running_group_sessions.discard(gid)
                        if self.group_cancel_events.get(gid) is our_event:
                            our_event.clear()
                            self.group_cancel_events.pop(gid, None)
                except (KeyError, TypeError, ValueError) as e:
                    logger.debug("_run_program_threaded cleanup gid=%s: %s", gid, e)
                try:
                    sk = self.group_skip_current_events.get(gid)
                    if sk is not None:
                        sk.clear()
                    self.group_skip_current_events.pop(gid, None)
                except (KeyError, TypeError, ValueError) as e:
                    logger.debug("_run_program_threaded skip-cleanup gid=%s: %s", gid, e)
            self._finish_runner()

    def _program_job_ids(self, program_id: int) -> list[str]:
        """Return recurring job ids from memory and every live jobstore."""
        prefix = f"program:{int(program_id)}:"
        job_ids = {str(job_id) for job_id in self.program_jobs.get(int(program_id), [])}
        try:
            for job in self.scheduler.get_jobs():
                jid = str(job.id)
                if jid.startswith(prefix):
                    job_ids.add(jid)
        except (RuntimeError, AttributeError, ValueError, TypeError):
            logger.debug("program jobstore scan failed for %s", program_id, exc_info=True)
        return sorted(job_ids)

    def schedule_program(
        self,
        program_id: int,
        program_data: dict[str, Any],
        *,
        interval_anchors: dict[str, datetime] | None = None,
        expected_fingerprint: str | None = None,
    ) -> bool:
        """Reconcile one program without destroying valid persistent jobs.

        Returns ``True`` when the persisted scheduler state matches the DB
        revision.  Invalid payloads and jobstore failures fail closed (no
        partial/legacy jobs remain) and return ``False``. A supplied
        ``expected_fingerprint`` that no longer matches the DB is a successful
        stale no-op: newer jobs are never cancelled by an older continuation.
        """
        with self._program_jobs_lock:
            return self._schedule_program_locked(
                int(program_id),
                program_data,
                interval_anchors=interval_anchors,
                expected_fingerprint=expected_fingerprint,
            )

    @staticmethod
    def _parse_program_time(value: object) -> tuple[int, int, str]:
        if not isinstance(value, str):
            raise ValueError("program time must be HH:MM")
        parts = value.strip().split(":")
        if len(parts) != 2:
            raise ValueError("program time must be HH:MM")
        hours, minutes = (int(part) for part in parts)
        if not 0 <= hours <= 23 or not 0 <= minutes <= 59:
            raise ValueError("program time is outside 00:00..23:59")
        return hours, minutes, f"{hours:02d}:{minutes:02d}"

    @staticmethod
    def _trigger_timezone_key(trigger: object) -> str:
        timezone = getattr(trigger, "timezone", None)
        return str(getattr(timezone, "key", timezone))

    def _trigger_semantics_match(self, current: object, desired: object) -> bool:
        if type(current) is not type(desired):
            return False
        if self._trigger_timezone_key(current) != self._trigger_timezone_key(desired):
            return False
        if isinstance(current, CronTrigger):
            return [str(field) for field in current.fields] == [str(field) for field in desired.fields]
        if isinstance(current, IntervalTrigger):
            current_start = current.start_date.astimezone(current.timezone)
            desired_start = desired.start_date.astimezone(desired.timezone)
            return current.interval == desired.interval and (
                current_start.hour,
                current_start.minute,
                current_start.second,
            ) == (
                desired_start.hour,
                desired_start.minute,
                desired_start.second,
            )
        return current == desired

    def _normalise_program_payload(self, program_id: int, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("program payload must be an object")
        enabled = bool(raw.get("enabled", True))
        if not enabled:
            return {"id": int(program_id), "enabled": False}

        hours, minutes, time_value = self._parse_program_time(raw.get("time"))
        name = str(raw.get("name") or f"program_{program_id}")
        raw_zones = raw.get("zones")
        if not isinstance(raw_zones, (list, tuple)) or isinstance(raw_zones, (str, bytes)):
            raise ValueError("program zones must be a list")
        zones = sorted({int(zone_id) for zone_id in raw_zones})
        if not zones or any(zone_id <= 0 for zone_id in zones):
            raise ValueError("program must contain positive zone ids")

        schedule_type = str(raw.get("schedule_type") or "weekdays").replace("_", "-")
        if schedule_type not in {"weekdays", "interval", "even-odd"}:
            raise ValueError(f"unsupported schedule_type: {schedule_type}")
        raw_days = raw.get("days") or []
        if not isinstance(raw_days, (list, tuple)) or isinstance(raw_days, (str, bytes)):
            raise ValueError("program days must be a list")
        days = sorted({int(day) for day in raw_days})
        if any(day < 0 or day > 6 for day in days):
            raise ValueError("program weekdays must be in 0..6")
        if schedule_type == "weekdays" and not days:
            raise ValueError("weekdays program must contain at least one day")

        interval_days = 1
        if schedule_type == "interval":
            value = raw.get("interval_days")
            if isinstance(value, bool):
                raise ValueError("interval_days must be an integer")
            interval_days = int(value)
            if not 1 <= interval_days <= 30:
                raise ValueError("interval_days must be in 1..30")

        even_odd = str(raw.get("even_odd") or "even")
        if schedule_type == "even-odd" and even_odd not in {"even", "odd"}:
            raise ValueError("even_odd must be even or odd")

        extra_times = raw.get("extra_times") or []
        if isinstance(extra_times, str):
            try:
                extra_times = json.loads(extra_times)
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError("extra_times must be a JSON list") from exc
        if not isinstance(extra_times, (list, tuple)) or isinstance(extra_times, (str, bytes)):
            raise ValueError("extra_times must be a list")
        canonical_extra = [self._parse_program_time(value)[2] for value in extra_times]

        return {
            "id": int(program_id),
            "enabled": True,
            "name": name,
            "time": time_value,
            "hours": hours,
            "minutes": minutes,
            "zones": zones,
            "schedule_type": schedule_type,
            "days": days,
            "interval_days": interval_days,
            "even_odd": even_odd,
            "extra_times": canonical_extra,
        }

    def program_schedule_fingerprint(self, program_id: int, program_data: dict[str, Any]) -> str:
        """Return the canonical DB-revision token accepted by schedule_program."""
        program = self._normalise_program_payload(int(program_id), program_data)
        canonical = {
            key: program.get(key)
            for key in (
                "id",
                "enabled",
                "name",
                "time",
                "zones",
                "schedule_type",
                "days",
                "interval_days",
                "even_odd",
                "extra_times",
            )
        }
        encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _normalise_interval_anchors(
        self,
        program: dict[str, Any],
        interval_anchors: dict[str, datetime] | None,
    ) -> dict[str, datetime] | None:
        if interval_anchors is None:
            return None
        if program["schedule_type"] != "interval":
            if interval_anchors:
                raise ValueError("interval anchors are valid only for interval programs")
            return None
        expected = {"main"} | {f"extra:{index}" for index, _ in enumerate(program["extra_times"])}
        if set(interval_anchors) != expected:
            raise ValueError("interval anchors must cover every main/extra slot exactly")
        timezone = self._controller_timezone()
        slot_times = {"main": program["time"]} | {
            f"extra:{index}": value for index, value in enumerate(program["extra_times"])
        }
        normalised: dict[str, datetime] = {}
        for slot, anchor in interval_anchors.items():
            if not isinstance(anchor, datetime) or anchor.tzinfo is None or anchor.utcoffset() is None:
                raise ValueError(f"interval anchor {slot} must be timezone-aware")
            local_anchor = anchor.astimezone(timezone) if timezone is not None else anchor.astimezone()
            hours, minutes, _ = self._parse_program_time(slot_times[slot])
            if (
                local_anchor.hour,
                local_anchor.minute,
                local_anchor.second,
                local_anchor.microsecond,
            ) != (hours, minutes, 0, 0):
                raise ValueError(f"interval anchor {slot} does not match its requested local time")
            normalised[slot] = local_anchor
        return normalised

    @staticmethod
    def _interval_anchor_matches(current: object, desired: object) -> bool:
        return (
            isinstance(current, IntervalTrigger)
            and isinstance(desired, IntervalTrigger)
            and current.start_date == desired.start_date
        )

    def _build_program_job_specs(
        self,
        program: dict[str, Any],
        existing_jobs: dict[str, Any],
        interval_anchors: dict[str, datetime] | None = None,
    ) -> list[dict[str, Any]]:
        timezone = self._controller_timezone()
        slots = [("main", program["time"])] + [
            (f"extra:{index}", value) for index, value in enumerate(program["extra_times"])
        ]
        specs: list[dict[str, Any]] = []
        for suffix, time_value in slots:
            hours, minutes, _ = self._parse_program_time(time_value)
            schedule_type = program["schedule_type"]
            triggers: list[tuple[str, object]] = []
            if schedule_type == "weekdays":
                triggers.extend(
                    (
                        f"program:{program['id']}:{suffix}:d{day}",
                        CronTrigger(day_of_week=day, hour=hours, minute=minutes, timezone=timezone),
                    )
                    for day in program["days"]
                )
            elif schedule_type == "interval":
                explicit_anchor = interval_anchors.get(suffix) if interval_anchors is not None else None
                if explicit_anchor is None:
                    now = self._controller_now()
                    start_date = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
                    if start_date <= now:
                        start_date += timedelta(days=1)
                else:
                    start_date = explicit_anchor
                triggers.append(
                    (
                        f"program:{program['id']}:{suffix}",
                        IntervalTrigger(
                            days=program["interval_days"],
                            start_date=start_date,
                            timezone=timezone,
                        ),
                    )
                )
            else:
                day_value = (
                    "2,4,6,8,10,12,14,16,18,20,22,24,26,28,30"
                    if program["even_odd"] == "even"
                    else "1,3,5,7,9,11,13,15,17,19,21,23,25,27,29,31"
                )
                triggers.append(
                    (
                        f"program:{program['id']}:{suffix}",
                        CronTrigger(day=day_value, hour=hours, minute=minutes, timezone=timezone),
                    )
                )

            for job_id, desired_trigger in triggers:
                current = existing_jobs.get(job_id)
                next_run_time = None
                require_exact_anchor = schedule_type == "interval" and interval_anchors is not None
                anchor_matches = not require_exact_anchor or (
                    current is not None and self._interval_anchor_matches(current.trigger, desired_trigger)
                )
                if (
                    current is not None
                    and anchor_matches
                    and self._trigger_semantics_match(current.trigger, desired_trigger)
                ):
                    # Preserve cadence anchors and a pending valid misfire even
                    # when only args/name/zones changed.
                    desired_trigger = current.trigger
                    next_run_time = getattr(current, "next_run_time", None)
                specs.append(
                    {
                        "id": job_id,
                        "trigger": desired_trigger,
                        "next_run_time": next_run_time,
                        "args": [program["id"], program["zones"], program["name"]],
                        "require_exact_anchor": require_exact_anchor,
                    }
                )
        return specs

    def _program_job_matches_spec(self, job: object, spec: dict[str, Any]) -> bool:
        try:
            func_ref = str(getattr(job, "func_ref", ""))
            return (
                str(job.id) == spec["id"]
                and func_ref.endswith(":job_run_program")
                and list(job.args) == spec["args"]
                and not dict(job.kwargs or {})
                and self._trigger_semantics_match(job.trigger, spec["trigger"])
                and (
                    not spec.get("require_exact_anchor") or self._interval_anchor_matches(job.trigger, spec["trigger"])
                )
                and job.misfire_grace_time == 3600
                and job.coalesce is False
                and job.max_instances == 1
            )
        except (AttributeError, TypeError, ValueError, KeyError):
            return False

    def _schedule_program_locked(
        self,
        program_id: int,
        program_data: dict[str, Any],
        *,
        interval_anchors: dict[str, datetime] | None = None,
        expected_fingerprint: str | None = None,
    ) -> bool:
        try:
            # PUT/DELETE mutate the DB before entering this scheduler boundary.
            # Re-read under the scheduler lock: if DELETE won, a delayed PUT
            # continuation must not recreate an orphan persistent job.  Direct
            # unit callers historically schedule unpersisted dictionaries, so
            # that compatibility is retained only in TESTING and only for a
            # never-before-known id.
            persisted = self.db.get_program(program_id)
            if expected_fingerprint is not None:
                if (
                    not isinstance(expected_fingerprint, str)
                    or len(expected_fingerprint) != 64
                    or any(char not in "0123456789abcdef" for char in expected_fingerprint)
                ):
                    logger.error("Program %s schedule fingerprint is invalid", program_id)
                    return False
                if persisted is None:
                    logger.info("Program %s schedule call is stale after delete; no-op", program_id)
                    return True
                persisted_fingerprint = self.program_schedule_fingerprint(program_id, persisted)
                if persisted_fingerprint != expected_fingerprint:
                    # A newer DB mutation won after this caller committed. Do
                    # not let stale interval anchors cancel or replace its jobs.
                    logger.info("Program %s stale schedule revision ignored", program_id)
                    return True
                program_data = persisted
            known_job = bool(self._program_job_ids(program_id)) or program_id in self.program_jobs
            if persisted is None:
                if not TESTING or known_job:
                    self._cancel_program_jobs_locked(program_id)
                    logger.info("Программа %s уже удалена; stale schedule отклонён", program_id)
                    return False
            elif not TESTING:
                program_data = persisted

            program = self._normalise_program_payload(program_id, program_data)
            if not program["enabled"]:
                logger.info("Программа %s выключена; расписание снято", program_id)
                return self._cancel_program_jobs_locked(program_id)

            normalised_anchors = self._normalise_interval_anchors(program, interval_anchors)
            existing = {
                str(job.id): job
                for job in self.scheduler.get_jobs()
                if str(job.id).startswith(f"program:{program_id}:")
            }
            specs = self._build_program_job_specs(program, existing, normalised_anchors)
            desired_ids = {spec["id"] for spec in specs}
            if not specs:
                raise ValueError("program produces no scheduler jobs")

            if desired_ids == set(existing) and all(
                self._program_job_matches_spec(existing[spec["id"]], spec) for spec in specs
            ):
                job_ids = sorted(desired_ids)
                self.program_jobs[program_id] = job_ids
                self._update_program_scheduled_starts(program_id, program["zones"], job_ids)
                logger.info("Программа %s восстановлена без пересоздания (%d jobs)", program_id, len(job_ids))
                return True

            was_running = getattr(self.scheduler, "state", None) == STATE_RUNNING
            if was_running:
                self.scheduler.pause()
            try:
                for spec in specs:
                    current = existing.get(spec["id"])
                    if current is not None and self._program_job_matches_spec(current, spec):
                        continue
                    kwargs = {
                        "args": spec["args"],
                        "id": spec["id"],
                        "replace_existing": True,
                        "misfire_grace_time": 3600,
                        "coalesce": False,
                        "max_instances": 1,
                    }
                    if getattr(self, "has_default_jobstore", False):
                        kwargs["jobstore"] = "default"
                    if spec["next_run_time"] is not None:
                        kwargs["next_run_time"] = spec["next_run_time"]
                    self.scheduler.add_job(job_run_program, spec["trigger"], **kwargs)

                for stale_id in set(existing) - desired_ids:
                    self.scheduler.remove_job(stale_id)
            except (sqlite3.Error, OSError, RuntimeError, KeyError, ValueError, TypeError):
                logger.exception("Atomic program reconcile failed for %s; failing closed", program_id)
                self._cancel_program_jobs_locked(program_id)
                return False
            finally:
                if was_running and getattr(self.scheduler, "state", None) == STATE_PAUSED:
                    self.scheduler.resume()

            job_ids = sorted(desired_ids)
            self.program_jobs[program_id] = job_ids
            self._update_program_scheduled_starts(program_id, program["zones"], job_ids)
            logger.info(
                "Программа %s (%s) согласована: %s, %d jobs",
                program_id,
                program["name"],
                program["schedule_type"],
                len(job_ids),
            )
            return True
        except (sqlite3.Error, OSError, KeyError, ValueError, TypeError, RuntimeError) as e:
            logger.error("Ошибка планирования программы %s: %s", program_id, e)
            self._cancel_program_jobs_locked(program_id)
            return False

    def _update_program_scheduled_starts(self, program_id: int, zones: list[int], job_ids: list[str]) -> None:
        """Store zone offsets from the program's next actual trigger fire."""
        try:
            tz = self._controller_timezone()
            now = self._controller_now()
            next_fires: list[datetime] = []
            for job_id in job_ids:
                job = self.scheduler.get_job(job_id)
                trigger = getattr(job, "trigger", None) if job is not None else None
                if trigger is None:
                    continue
                fire = trigger.get_next_fire_time(None, now)
                if fire is not None:
                    next_fires.append(fire)
            if not next_fires:
                return
            base = min(next_fires)
            if base.tzinfo is not None:
                base = (
                    base.astimezone(tz).replace(tzinfo=None)
                    if tz is not None
                    else base.astimezone().replace(tzinfo=None)
                )
            cumulative = 0
            for zone_id in zones:
                zone = self.db.get_zone(zone_id)
                if not zone:
                    continue
                start_dt = base + timedelta(minutes=cumulative)
                self.db.update_zone(zone_id, {"scheduled_start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S")})
                cumulative += int(zone.get("duration") or 0)
        except (sqlite3.Error, OSError, AttributeError, TypeError, ValueError) as e:
            logger.error("Ошибка расчета следующего старта программы %s: %s", program_id, e)

    def cancel_program(self, program_id: int) -> bool:
        """Remove every recurring job and truthfully acknowledge the result."""
        try:
            program_id = int(program_id)
        except (TypeError, ValueError):
            return False
        with self._program_jobs_lock:
            return self._cancel_program_jobs_locked(program_id)

    def _cancel_program_jobs_locked(self, program_id: int) -> bool:
        prefix = f"program:{int(program_id)}:"
        remembered = {str(job_id) for job_id in self.program_jobs.get(int(program_id), [])}
        try:
            live_before = {str(job.id) for job in self.scheduler.get_jobs() if str(job.id).startswith(prefix)}
        except (AttributeError, RuntimeError, ValueError, TypeError, KeyError):
            logger.exception("Could not enumerate recurring jobs for program %s", program_id)
            return False

        for job_id in sorted(remembered | live_before):
            try:
                self.scheduler.remove_job(job_id)
                self._emit_timer_audit(
                    "scheduler_timer_cancel",
                    f"program:{int(program_id)}",
                    {"job_id": job_id},
                )
            except KeyError:
                # Already absent is an idempotent successful removal, subject
                # to the authoritative post-scan below.
                continue
            except (ValueError, RuntimeError):
                logger.exception("Could not remove recurring program job %s", job_id)

        try:
            remaining = sorted(str(job.id) for job in self.scheduler.get_jobs() if str(job.id).startswith(prefix))
        except (AttributeError, RuntimeError, ValueError, TypeError, KeyError):
            logger.exception("Could not verify recurring-job removal for program %s", program_id)
            return False

        self.program_jobs[int(program_id)] = remaining
        if remaining:
            logger.error("Программа %s не отменена; jobs remain: %s", program_id, remaining)
            return False
        logger.info("Программа %s отменена", program_id)
        return True

    @staticmethod
    def _emit_timer_audit(action: str, target: str, payload: dict) -> None:
        """Best-effort debug emit for scheduler timer plant/cancel events."""
        try:
            from services.audit import debug_audit

            debug_audit(
                action_type=action,
                source="scheduler",
                target=target,
                payload=payload,
            )
        except Exception:
            logger.exception("scheduler timer audit failed (action=%s)", action)

    def schedule_zone_stop(self, zone_id: int, duration_minutes: int, command_id: str | None = None):
        """Запланировать автоматическую остановку зоны через duration_minutes минут (для ручных запусков)."""
        try:
            if duration_minutes is None:
                return
            # Раннее выключение: за N секунд до окончания (настраивается), по умолчанию 3
            try:
                from database import db as _db

                early = int(_db.get_early_off_seconds())
            except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                logger.debug("Exception in schedule_zone_stop: %s", e)
                early = 3
            if early < 0:
                early = 0
            if early > 15:
                early = 15
            # В тестовом режиме ускоряем автостоп до секунд, чтобы не было «хвостов» после тестов
            if TESTING:
                total_seconds = min(6, max(1, int(duration_minutes)))
                early = 0
                run_at = self._controller_now(naive=True) + timedelta(seconds=total_seconds)
            else:
                run_at = (
                    self._controller_now(naive=True)
                    + timedelta(minutes=int(duration_minutes))
                    - timedelta(seconds=early)
                )
            # Гарантируем, что время в будущем (минимум +1 сек)
            now = self._controller_now(naive=True)
            if run_at <= now:
                run_at = now + timedelta(seconds=1)
            activation_token = self._current_activation_token(int(zone_id))
            # Стандартизованный ID (используем command_id при наличии)
            _kwargs = dict(
                args=[zone_id, activation_token],
                id=(
                    f"zone_stop:{int(zone_id)}:{command_id!s}"
                    if command_id
                    else f"zone_stop:{int(zone_id)}:{int(run_at.timestamp())}"
                ),
                replace_existing=False,
                misfire_grace_time=120,
            )
            if getattr(self, "has_volatile_jobstore", False):
                _kwargs["jobstore"] = "volatile"
            self.scheduler.add_job(
                job_stop_zone_if_activation,
                DateTrigger(run_date=run_at, timezone=self._controller_timezone()),
                **_kwargs,
            )
            self.active_zones[zone_id] = run_at
            self._emit_timer_audit(
                "scheduler_timer_plant",
                f"zone:{int(zone_id)}",
                {
                    "job": "zone_stop",
                    "job_id": _kwargs.get("id"),
                    "duration_minutes": int(duration_minutes),
                    "run_at": run_at.isoformat(timespec="seconds"),
                },
            )
            logger.info(f"Автоостановка зоны {zone_id} запланирована на {run_at}")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка планирования автоостановки зоны {zone_id}: {e}")

    def schedule_zone_hard_stop(
        self,
        zone_id: int,
        run_at: datetime,
        *,
        activation_token: str | None = None,
    ) -> bool:
        """Жёсткий watchdog-стоп зоны на точное время run_at (доп. страховка)."""
        try:
            run_at = self._normalise_caller_run_at(run_at)
            now = self._controller_now()
            if run_at <= now:
                run_at = now + timedelta(seconds=1)
            if activation_token is None:
                activation_token = self._current_activation_token(int(zone_id))
            _kwargs = dict(
                args=[zone_id, activation_token, True],
                id=f"zone_hard_stop:{int(zone_id)}",
                replace_existing=True,
                misfire_grace_time=60,
                coalesce=False,
                max_instances=1,
            )
            if getattr(self, "has_volatile_jobstore", False):
                _kwargs["jobstore"] = "volatile"
            self.scheduler.add_job(
                job_stop_zone_if_activation,
                DateTrigger(run_date=run_at, timezone=self._controller_timezone()),
                **_kwargs,
            )
            self._emit_timer_audit(
                "scheduler_timer_plant",
                f"zone:{int(zone_id)}",
                {"job": "zone_hard_stop", "job_id": _kwargs.get("id"), "run_at": run_at.isoformat(timespec="seconds")},
            )
            logger.info(f"Watchdog: zone {zone_id} hard-stop at {run_at}")
            return True
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка планирования watchdog-стопа зоны {zone_id}: {e}")
            return False

    def schedule_zone_cap(
        self,
        zone_id: int,
        cap_minutes: int = 240,
        activation_token: str | None = None,
    ):
        """Абсолютный лимит работы зоны: форс-стоп через cap_minutes от текущего момента."""
        try:
            run_at = self._controller_now(naive=True) + timedelta(minutes=int(cap_minutes))
            if activation_token is None:
                activation_token = self._current_activation_token(int(zone_id))
            # Уникальный job id для капа
            job_id = f"zone_cap_stop:{int(zone_id)}"
            _kwargs = dict(
                args=[zone_id, activation_token, True],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=300,
                coalesce=False,
                max_instances=1,
            )
            if getattr(self, "has_volatile_jobstore", False):
                _kwargs["jobstore"] = "volatile"
            self.scheduler.add_job(
                job_stop_zone_if_activation,
                DateTrigger(run_date=run_at, timezone=self._controller_timezone()),
                **_kwargs,
            )
            self._emit_timer_audit(
                "scheduler_timer_plant",
                f"zone:{int(zone_id)}",
                {
                    "job": "zone_cap_stop",
                    "job_id": job_id,
                    "cap_minutes": int(cap_minutes),
                    "run_at": run_at.isoformat(timespec="seconds"),
                },
            )
            logger.info(f"Zone cap: zone {zone_id} hard-stop at {run_at} (cap {cap_minutes}m)")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка планирования cap-стопа зоны {zone_id}: {e}")

    def cancel_zone_cap(self, zone_id: int):
        try:
            job_id = f"zone_cap_stop:{int(zone_id)}"
            try:
                self.scheduler.remove_job(job_id)
                self._emit_timer_audit(
                    "scheduler_timer_cancel",
                    f"zone:{int(zone_id)}",
                    {"job": "zone_cap_stop", "job_id": job_id},
                )
            except (ValueError, KeyError, RuntimeError) as e:
                logger.debug("Handled exception in cancel_zone_cap: %s", e)
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка отмены cap-стопа зоны {zone_id}: {e}")

    def schedule_master_valve_cap(self, group_id: int, hours: int = 24):
        """Абсолютный лимит открытого мастер-клапана — закрыть через hours часов.
        Перепланируется при повторных вызовах.
        """
        try:
            run_at = self._controller_now(naive=True) + timedelta(hours=int(hours))
            job_id = f"master_cap_close:{int(group_id)}"
            _kwargs = dict(
                args=[group_id],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=600,
                coalesce=False,
                max_instances=1,
            )
            if getattr(self, "has_volatile_jobstore", False):
                _kwargs["jobstore"] = "volatile"
            self.scheduler.add_job(
                job_close_master_valve,
                DateTrigger(run_date=run_at, timezone=self._controller_timezone()),
                **_kwargs,
            )
            self._emit_timer_audit(
                "scheduler_timer_plant",
                f"group:{int(group_id)}",
                {
                    "job": "master_cap_close",
                    "job_id": job_id,
                    "cap_hours": int(hours),
                    "run_at": run_at.isoformat(timespec="seconds"),
                },
            )
            logger.info(f"Master valve cap: group {group_id} close at {run_at} (cap {hours}h)")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка планирования cap-закрытия мастер-клапана для группы {group_id}: {e}")

    def cancel_master_valve_cap(self, group_id: int):
        try:
            job_id = f"master_cap_close:{int(group_id)}"
            try:
                self.scheduler.remove_job(job_id)
                self._emit_timer_audit(
                    "scheduler_timer_cancel",
                    f"group:{int(group_id)}",
                    {"job": "master_cap_close", "job_id": job_id},
                )
            except (ValueError, KeyError, RuntimeError) as e:
                logger.debug("Handled exception in cancel_master_valve_cap: %s", e)
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка отмены cap-закрытия мастер-клапана для группы {group_id}: {e}")

    def _release_group_session(self, group_id: int, owned_event: threading.Event | None) -> None:
        """Identity-safe release for one group-session claim."""
        if owned_event is None:
            return
        with self._group_session_lock:
            if self.group_cancel_events.get(int(group_id)) is owned_event:
                self.group_cancel_events.pop(int(group_id), None)

    # ===== Ручной последовательный запуск всех зон в группе =====
    def start_group_sequence(
        self,
        group_id: int,
        override_duration: int | None = None,
        override_percent: int | None = None,
        zone_ids: list[int] | None = None,
        ad_hoc_program_id: int | None = None,
        ad_hoc_program_name: str | None = None,
        manual: bool = False,
    ):
        """Остановить все зоны группы и запустить последовательный полив всех зон по порядку.

        Issue #12: ``override_percent`` (one of PERCENT_PRESETS) — when set,
        each zone runs for ``round_up(zone.duration * pct/100)`` clipped to
        [1, MAX_MANUAL_WATERING_MIN]. ``override_duration`` (minutes mode)
        wins if both are passed.

        Issue #15: ``zone_ids`` (subset of group), ``ad_hoc_program_id``
        (negative sentinel), ``ad_hoc_program_name`` for audit-only ad-hoc
        manual runs.
        """
        try:
            group_id = int(group_id)
        except (TypeError, ValueError):
            return False
        if self._is_emergency_stop_active():
            logger.warning("Группа %s не запущена: активен EMERGENCY_STOP", group_id)
            return False
        if self._is_group_rain_blocked(group_id):
            logger.warning("Группа %s не запущена: активна блокировка дождём", group_id)
            return False
        with self._runner_condition:
            if not self._accepting_runs or self._shutdown_event.is_set():
                return False
        # Claim the group before any DB/MQTT initialisation.  This makes both
        # repeated starts and Stop-during-init observable immediately.
        cancel_event = threading.Event()
        with self._group_session_lock:
            if group_id in self.group_cancel_events:
                logger.info("Группа %s уже имеет активную сессию", group_id)
                return False
            self.group_cancel_events[group_id] = cancel_event
        sequence_handed_off = False
        try:
            zones = self.db.get_zones()
            all_group_zones = sorted([z for z in zones if z["group_id"] == group_id], key=lambda x: x["id"])
            group_zones = list(all_group_zones)
            if zone_ids is not None:
                wanted = set(int(z) for z in zone_ids)
                group_zones = [z for z in group_zones if int(z["id"]) in wanted]
                # Preserve user-requested order if it differs from id-asc.
                order = {int(z): i for i, z in enumerate(zone_ids)}
                group_zones.sort(key=lambda z: order.get(int(z["id"]), 9999))
            if not group_zones:
                logger.info(f"Группа {group_id}: нет зон для последовательного запуска")
                return False

            if cancel_event.is_set() or self._shutdown_event.is_set():
                return False

            # Physically stop EVERY zone in the group, including peers omitted
            # from an ad-hoc subset.  A DB-only bulk-off hides an energised
            # relay from both watchdogs and removes its eventual stop job.
            from services.zone_control import stop_all_in_group as _stop_all

            stop_kwargs: dict[str, Any] = {
                "reason": "group_sequence_restart",
                "force": True,
                "skip_master_close": True,
            }
            if self._callable_accepts_keyword(_stop_all, "require_observed_confirmation"):
                stop_kwargs["require_observed_confirmation"] = True
            else:
                logger.warning("Group pre-OFF confirmation API unavailable; relying on central start gate")
            stop_outcome = _stop_all(group_id, **stop_kwargs)
            if cancel_event.is_set() or self._shutdown_event.is_set() or self._is_emergency_stop_active():
                return False
            if self._is_group_rain_blocked(group_id):
                logger.warning("Группа %s: дождь начался во время pre-OFF; запуск отменён", group_id)
                return False
            # Newer zone-control versions expose a structured aggregate.  Do
            # not turn an explicitly unresolved physical OFF into a successful
            # group start; the DB re-read below remains the compatibility and
            # defence-in-depth check for older versions.
            if isinstance(stop_outcome, dict):
                unresolved = stop_outcome.get("unresolved")
                if stop_outcome.get("success") is not True or not isinstance(unresolved, (list, tuple, set)):
                    logger.error("Группа %s: invalid or unsuccessful bulk OFF outcome", group_id)
                    return False
                if unresolved:
                    logger.error("Группа %s: bulk OFF unresolved for zones %s", group_id, sorted(unresolved))
                    return False
            elif stop_outcome is False:
                logger.error("Группа %s: legacy bulk OFF explicitly failed", group_id)
                return False

            # Re-read the full group before deleting ordinary deadlines or
            # planting a new ON sequence. P05 pins a failed OFF publish to
            # ``fault``; any logical state other than ``off`` is unsafe.
            refreshed = {int(z["id"]): z for z in (self.db.get_zones() or [])}
            unsafe_after_stop = [
                int(z["id"])
                for z in all_group_zones
                if str((refreshed.get(int(z["id"])) or z).get("state") or "").lower() != "off"
            ]
            if unsafe_after_stop:
                logger.error(
                    "Группа %s: sequence aborted, zones not safely off after stop: %s",
                    group_id,
                    unsafe_after_stop,
                )
                return False

            # Считаем и записываем плановые времена стартов для зон группы
            try:
                from services.zone_control import per_zone_dur as _per_zone_dur

                start_base = self._controller_now(naive=True)
                cumulative = 0
                schedule_map: dict[int, str] = {}
                for z in group_zones:
                    start_dt = start_base + timedelta(minutes=cumulative)
                    schedule_map[z["id"]] = start_dt.strftime("%Y-%m-%d %H:%M:%S")
                    # Issue #12: per-zone math via the shared helper. With
                    # override_duration set this returns the scalar (legacy
                    # behaviour); with override_percent it computes
                    # ceil(zone.duration * pct/100), clipped.
                    _d, _w = _per_zone_dur(z, override_duration, override_percent)
                    cumulative += _d
                # Очистим предыдущие плановые старты и запишем новые
                self.db.clear_group_scheduled_starts(group_id)
                self.db.set_group_scheduled_starts(group_id, schedule_map)
            except (sqlite3.Error, OSError, ImportError) as e:
                logger.error(f"Ошибка расчета плановых стартов для группы {group_id}: {e}")

            # Remove the stale sequence job and ordinary stop deadlines. A
            # physical broker ACK is not fresh relay confirmation: hard/cap
            # jobs and their activation marker remain until observed OFF.
            try:
                all_zone_ids = [int(z["id"]) for z in all_group_zones]
                sequence_jobs = []
                for job in self.scheduler.get_jobs():
                    jid = str(job.id)
                    if jid.startswith(f"group_seq:{int(group_id)}:"):
                        sequence_jobs.append(jid)
                for jid in sequence_jobs:
                    try:
                        self.scheduler.remove_job(jid)
                    except (ValueError, KeyError, RuntimeError) as e:
                        logger.debug("Handled exception in line_743: %s", e)
                for zid in all_zone_ids:
                    current = refreshed.get(zid) or {}
                    physical_unconfirmed = (
                        self._zone_requires_observed_off(zid)
                        and str(current.get("observed_state") or "").lower() != "off"
                    )
                    if physical_unconfirmed:
                        self.cancel_zone_jobs(zid, preserve_safety=True)
                    else:
                        self.cancel_zone_jobs(zid, include_cap=True)
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Handled exception in line_747: %s", e)

            zone_ids = [z["id"] for z in group_zones]
            # In TESTING mode, run synchronously to avoid APScheduler thread timing issues
            if TESTING:
                self._run_group_sequence(
                    group_id,
                    zone_ids,
                    override_duration=override_duration,
                    override_percent=override_percent,
                    ad_hoc_program_id=ad_hoc_program_id,
                    ad_hoc_program_name=ad_hoc_program_name,
                    manual=manual,
                )
            else:
                # Запускаем последовательность в отдельном джобе прямо сейчас
                _kwargs = dict(
                    args=[group_id, zone_ids, override_duration],
                    kwargs={
                        "override_percent": override_percent,
                        "ad_hoc_program_id": ad_hoc_program_id,
                        "ad_hoc_program_name": ad_hoc_program_name,
                        "manual": manual,
                    },
                    id=f"group_seq:{group_id}:{int(self._controller_now().timestamp())}",
                    replace_existing=False,
                    misfire_grace_time=120,
                    coalesce=False,
                    max_instances=1,
                )
                if getattr(self, "has_volatile_jobstore", False):
                    _kwargs["jobstore"] = "volatile"
                self.scheduler.add_job(
                    job_run_group_sequence,
                    DateTrigger(
                        run_date=self._controller_now(naive=True),
                        timezone=self._controller_timezone(),
                    ),
                    **_kwargs,
                )
            sequence_handed_off = True
            try:
                pass  # dlog replaced by logger
                logger.debug("group-seq start group=%s zones=%s", group_id, zone_ids)
            except (OSError, ValueError) as e:
                logger.debug("Handled exception in line_770: %s", e)
            logger.info(f"Группа {group_id}: последовательный полив запущен для зон {zone_ids}")
            return True
        except (sqlite3.Error, OSError, ImportError, ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка старта последовательного полива для группы {group_id}: {e}")
            return False
        finally:
            if not sequence_handed_off:
                self._release_group_session(group_id, cancel_event)

    def _run_group_sequence(
        self,
        group_id: int,
        zone_ids: list[int],
        override_duration: int | None = None,
        override_percent: int | None = None,
        ad_hoc_program_id: int | None = None,
        ad_hoc_program_name: str | None = None,
        manual: bool = False,
    ):
        """Выполняет последовательный полив зон группы. Выполняется в пуле потоков APScheduler.

        Issue #12: ``override_percent`` plumbed through; per-zone duration is
        computed via :func:`services.zone_control.per_zone_dur`.
        Issue #15: ``ad_hoc_program_*`` arrive as kwargs for ad-hoc audit only.
        Legacy callers (cron-driven group_seq jobs from before #15) keep working.
        """
        from services.zone_control import per_zone_dur as _per_zone_dur

        with self._group_session_lock:
            cancel_event = self.group_cancel_events.get(int(group_id))
        if self._is_emergency_stop_active():
            logger.warning("Группа %s заблокирована: активен EMERGENCY_STOP", group_id)
            self._release_group_session(group_id, cancel_event)
            return
        if self._is_group_rain_blocked(group_id):
            logger.warning("Группа %s заблокирована дождём до запуска runner", group_id)
            self._release_group_session(group_id, cancel_event)
            return

        # Test-only bypass: when SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ=1
        # we skip the synchronous-first-zone short-circuit and run the real
        # per-zone loop (still truncated to a few seconds via the TESTING
        # branch lower in this method).  Used by the issue #16 outcome test
        # to verify zones 2/3 never reach state='on' after a mid-sequence
        # cancel.  Production never sets this env var.
        if TESTING and not os.environ.get("SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ"):
            logger.debug("TESTING mode: simplified _run_group_sequence for group %s", group_id)
            # In TESTING mode, set the first zone ON in the DB (skip MQTT/hardware)
            try:
                from services.zone_control import per_zone_dur as _per_zone_dur
            except ImportError:
                _per_zone_dur = None
            synthetic_started = False
            for zone_id in zone_ids:
                zone = self.db.get_zone(zone_id)
                if not zone:
                    continue
                if str(zone.get("state") or "").lower() == "fault":
                    continue
                duration, _ = _per_zone_dur(zone, override_duration, override_percent)
                if duration <= 0:
                    continue
                start_ts = self._controller_now(naive=True).strftime("%Y-%m-%d %H:%M:%S")
                planned_end = (self._controller_now(naive=True) + timedelta(minutes=duration)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                # TESTING-mode start — flagged via audit_reason so any audit
                # log scrub on prod can filter these synthetic transitions out.
                try:
                    from services.zones_state import update_zone_state as _uzs

                    _uzs(
                        zone_id,
                        {"state": "on", "watering_start_time": start_ts, "planned_end_time": planned_end},
                        audit_reason="testing_mode_start",
                        db=self.db,
                    )
                except (sqlite3.Error, OSError, ImportError):
                    logger.exception(
                        "irrigation_scheduler: TESTING start audited path failed zone=%s",
                        zone_id,
                    )
                    self.db.update_zone(
                        zone_id, {"state": "on", "watering_start_time": start_ts, "planned_end_time": planned_end}
                    )
                synthetic_started = True
                break  # Only start the first zone in TESTING mode
            if not synthetic_started:
                self._release_group_session(group_id, cancel_event)
            return
        if not self._begin_runner():
            logger.info("Группа %s не запущена: scheduler quiescing", group_id)
            self._release_group_session(group_id, cancel_event)
            return
        with self._group_session_lock:
            self._running_group_sessions.add(int(group_id))
        try:
            # Weather check before group sequence.
            # Issue #31: manual runs bypass weather skip entirely.
            if not manual:
                skip_info = self._check_weather_skip(zone_ids[0] if zone_ids else 0, 0)
                try:
                    from services.weather_adjustment import get_weather_adjustment

                    _adj = get_weather_adjustment(self.db.db_path)
                    if _adj.is_enabled():
                        _w = _adj._get_weather()
                        _coeff = _adj.get_coefficient()
                        _adj.log_decision(_w, _coeff, bool(skip_info.get("skip")), skip_info.get("reason", ""))
                except Exception as e:
                    logger.debug("log_decision error: %s", e)
            else:
                skip_info = {"skip": False, "reason": ""}
            if skip_info.get("skip"):
                logger.info(
                    f"Группа {group_id}: последовательный полив пропущен из-за погоды: {skip_info.get('reason')}"
                )
                try:
                    self.db.add_log(
                        "group_weather_skip",
                        json.dumps(
                            {
                                "group_id": group_id,
                                "reason": skip_info.get("reason", ""),
                            }
                        ),
                    )
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    logger.debug("Group weather skip log error: %s", e)
                return
            for zone_id in zone_ids:
                if self._shutdown_event.is_set() or self._is_emergency_stop_active():
                    logger.info("Группа %s остановлена до запуска следующей зоны", group_id)
                    break
                if cancel_event and cancel_event.is_set():
                    logger.info(f"Группа {group_id}: последовательный полив отменен перед запуском зоны {zone_id}")
                    break
                # Drop a stale skip event that arrived during the inter-zone gap —
                # the user wanted to skip the previous zone, which already ended.
                # Without this, the next zone we haven't yet started would be
                # immediately skipped on its first sleep tick.
                _stale_skip = self.group_skip_current_events.get(group_id)
                if _stale_skip and _stale_skip.is_set():
                    _stale_skip.clear()
                zone = self.db.get_zone(zone_id)
                if not zone:
                    logger.warning(f"Группа {group_id}: зона {zone_id} не найдена, пропуск")
                    continue
                if str(zone.get("state") or "").lower() == "fault":
                    logger.warning("Группа %s: fault-зона %s исключена из запуска", group_id, zone_id)
                    continue

                # Issue #12: % unfolds per-zone via the helper. Without
                # override_percent the helper returns the scalar override or
                # the zone's own duration — preserving prior behaviour byte-
                # for-byte.
                base_dur, _ = _per_zone_dur(zone, override_duration, override_percent)
                # Issue #31: manual runs use base duration without weather coefficient.
                if manual:
                    duration = base_dur
                else:
                    duration = self._get_weather_adjusted_duration(zone_id, base_dur)
                if duration <= 0:
                    logger.info(f"Группа {group_id}: зона {zone_id} имеет нулевую длительность, пропуск")
                    continue
                skipped_this_zone = False

                if self._shutdown_event.is_set() or self._is_emergency_stop_active():
                    logger.info("Группа %s остановлена перед ON зоны %s", group_id, zone_id)
                    break
                if self._is_group_rain_blocked(group_id):
                    logger.warning("Группа %s заблокирована дождём перед ON зоны %s", group_id, zone_id)
                    break

                # Central start is the single owner of fault admission, peer
                # shutdown, MV/MQTT ON, state transitions, and zone_run rows.
                try:
                    from services.zone_control import exclusive_start_zone as _start_central

                    started = bool(_start_central(int(zone_id), source="program"))
                except (sqlite3.Error, OSError, ImportError, ValueError, TypeError):
                    logger.exception("Группа %s: central start failed zone=%s", group_id, zone_id)
                    started = False
                if not started:
                    logger.warning("Группа %s: зона %s не стартовала, success-учёт пропущен", group_id, zone_id)
                    continue

                latest_state = str((self.db.get_zone(int(zone_id)) or {}).get("state") or "").lower()
                if latest_state == "fault":
                    logger.error("Группа %s: зона %s перешла в fault при старте", group_id, zone_id)
                    continue
                end_time = self._controller_now() + timedelta(minutes=duration)
                self.active_zones[int(zone_id)] = end_time
                try:
                    metadata = {
                        "planned_end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
                        "watering_start_source": "schedule",
                    }
                    legacy_virtual_state = latest_state in {"off", "starting"}
                    if legacy_virtual_state and (not zone.get("mqtt_server_id") or not zone.get("topic")):
                        metadata["state"] = "on"
                    self.db.update_zone(
                        int(zone_id),
                        metadata,
                    )
                except (sqlite3.Error, OSError) as e:
                    logger.debug("group sequence planned end update failed: %s", e)
                try:
                    self.schedule_zone_hard_stop(int(zone_id), end_time)
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Handled exception in _run_group_sequence: %s", e)
                try:
                    _zs_payload = {
                        "group_id": group_id,
                        "zone_id": zone_id,
                        "zone_name": zone.get("name"),
                        "duration": duration,
                    }
                    if ad_hoc_program_id is not None:
                        _zs_payload["program_id"] = int(ad_hoc_program_id)
                        _zs_payload["program_name"] = ad_hoc_program_name
                        _zs_payload["is_ad_hoc"] = True
                    self.db.add_log("group_seq_zone_start", json.dumps(_zs_payload))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    logger.debug("Handled exception in line_860: %s", e)

                # Ждем окончание полива зоны, проверяя флаг отмены каждую секунду
                # Раннее выключение и выравнивание старта следующей зоны
                try:
                    from database import db as _db

                    early = int(_db.get_early_off_seconds())
                except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                    logger.debug("Exception in line_868: %s", e)
                    early = 3
                early = 0 if early < 0 else (15 if early > 15 else early)
                total_seconds = duration * 60
                if TESTING:
                    total_seconds = min(6, max(1, duration))
                    early = 0
                remaining = max(0, total_seconds - early)
                while remaining > 0:
                    if cancel_event and cancel_event.is_set():
                        try:
                            pass  # dlog replaced by logger
                            logger.debug(
                                "group-seq cancel tick group=%s zone=%s remaining=%s", group_id, zone_id, remaining
                            )
                        except (OSError, ValueError) as e:
                            logger.debug("Handled exception in line_882: %s", e)
                        logger.info(f"Группа {group_id}: получена отмена, досрочно останавливаем зону {zone_id}")
                        break
                    skip_event = self.group_skip_current_events.get(group_id)
                    if skip_event and skip_event.is_set():
                        skip_event.clear()  # one-shot per zone
                        skipped_this_zone = True
                        logger.info(f"Группа {group_id}: skip current zone {zone_id}")
                        break
                    if self._shutdown_event.wait(timeout=1):
                        logger.info(f"Группа {group_id}: shutdown, досрочно останавливаем зону {zone_id}")
                        break
                    remaining -= 1
                # Централизованный OFF и снятие активности только после
                # подтверждённого результата. Failed OFF сохраняет safety retry.
                stopped = self._stop_zone(int(zone_id), reason="group_sequence")
                if stopped:
                    try:
                        self.db.update_zone(zone_id, {"planned_end_time": None})
                    except (sqlite3.Error, OSError) as e:
                        logger.debug("Handled exception in line_899: %s", e)
                else:
                    logger.error("Группа %s: OFF зоны %s не подтверждён", group_id, zone_id)
                    break
                if skipped_this_zone:
                    try:
                        self.db.add_log(
                            "zone_skip",
                            json.dumps(
                                {
                                    "group_id": group_id,
                                    "zone_id": zone_id,
                                    "zone_name": zone.get("name"),
                                    "reason": "manual_skip",
                                }
                            ),
                        )
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                        logger.debug("zone_skip log error: %s", e)
                    # Skip the post-zone make-up wait — user wants the next zone NOW.
                    continue
                # Добираем ранние секунды, чтобы следующий старт был вовремя
                if early > 0 and not (cancel_event and cancel_event.is_set()):
                    self._shutdown_event.wait(timeout=early)
                # Если отменено — выходим из последовательности
                if cancel_event and cancel_event.is_set():
                    break

            # По завершении очищаем плановые старты группы
            try:
                # Перестраиваем расписание группы на ближайшее будущее
                self.db.reschedule_group_to_next_program(group_id)
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in line_912: %s", e)

            try:
                _gc_payload = {"group_id": group_id, "zones": zone_ids}
                if ad_hoc_program_id is not None:
                    _gc_payload["program_id"] = int(ad_hoc_program_id)
                    _gc_payload["program_name"] = ad_hoc_program_name
                    _gc_payload["is_ad_hoc"] = True
                self.db.add_log("group_seq_complete", json.dumps(_gc_payload))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                logger.debug("Handled exception in line_917: %s", e)
            logger.info(f"Группа {group_id}: последовательный полив завершен")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.error(f"Ошибка выполнения последовательного полива группы {group_id}: {e}")
        finally:
            # Issue #16 C2: only pop the dict entry if it still IS the
            # Event we observed at startup.  If a concurrent caller has
            # since replaced it (e.g. _run_program_threaded planted its
            # own Event between our setdefault and now, or vice-versa),
            # leave that Event alone — its planter owns the cleanup.
            try:
                with self._group_session_lock:
                    self._running_group_sessions.discard(int(group_id))
                self._release_group_session(group_id, cancel_event)
            except (KeyError, TypeError, ValueError) as e:
                logger.debug("_run_group_sequence cleanup gid=%s: %s", group_id, e)
            try:
                sk = self.group_skip_current_events.get(group_id)
                if sk:
                    sk.clear()
                self.group_skip_current_events.pop(group_id, None)
            except (KeyError, TypeError, ValueError) as e:
                logger.debug("group_skip_current_events cleanup: %s", e)
            self._finish_runner()

    def get_active_programs(self) -> dict[int, dict[str, Any]]:
        # Возвращаем список запланированных программ и их job_ids
        return {pid: {"job_ids": jobs} for pid, jobs in self.program_jobs.items()}

    @staticmethod
    def _program_slot_from_job_id(program_id: int, job_id: str) -> str | None:
        prefix = f"program:{int(program_id)}:"
        if not str(job_id).startswith(prefix):
            return None
        tail = str(job_id)[len(prefix) :]
        if tail.startswith("extra:"):
            parts = tail.split(":")
            return f"extra:{parts[1]}" if len(parts) > 1 else None
        return tail.split(":", 1)[0] or None

    def get_program_trigger_metadata(self, program_id: int) -> dict[str, Any]:
        """Read live persistent trigger metadata without mutating job state.

        ``slots`` groups weekday fan-out jobs under ``main``/``extra:N`` and
        exposes an IntervalTrigger's authoritative anchor.  Callers must treat
        a missing interval anchor as unknown rather than inventing a new one.
        """
        pid = int(program_id)
        result: dict[str, Any] = {
            "program_id": pid,
            "timezone": str(getattr(self.scheduler, "timezone", "")),
            "slots": {},
        }
        with self._program_jobs_lock:
            jobs = sorted(
                (job for job in self.scheduler.get_jobs() if str(job.id).startswith(f"program:{pid}:")),
                key=lambda job: str(job.id),
            )
            for job in jobs:
                slot = self._program_slot_from_job_id(pid, str(job.id))
                if slot is None:
                    continue
                trigger = job.trigger
                entry = {
                    "job_id": str(job.id),
                    "trigger_type": type(trigger).__name__,
                    "next_run_time": getattr(job, "next_run_time", None),
                    "interval_days": None,
                    "anchor": None,
                }
                if isinstance(trigger, IntervalTrigger):
                    entry["interval_days"] = int(trigger.interval.total_seconds() // 86400)
                    entry["anchor"] = trigger.start_date
                result["slots"].setdefault(slot, []).append(entry)
        return result

    def get_program_interval_anchors(self, program_id: int) -> dict[str, datetime]:
        """Return authoritative live anchors keyed by ``main``/``extra:N``."""
        metadata = self.get_program_trigger_metadata(int(program_id))
        anchors: dict[str, datetime] = {}
        for slot, entries in metadata["slots"].items():
            values = [entry["anchor"] for entry in entries if entry.get("anchor") is not None]
            if values:
                anchors[str(slot)] = min(values)
        return anchors

    def get_program_occurrences(
        self,
        program_id: int,
        start: datetime,
        end: datetime,
        *,
        limit: int = 512,
    ) -> dict[str, list[datetime]]:
        """Enumerate live trigger occurrences over a bounded horizon by slot."""
        bounded_limit = max(1, min(int(limit), 4096))
        timezone = self._controller_timezone()

        def aware(value: datetime) -> datetime:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone) if timezone is not None else value.astimezone()
            return value.astimezone(timezone) if timezone is not None else value

        range_start = aware(start)
        range_end = aware(end)
        if range_end < range_start:
            raise ValueError("end must not precede start")
        result: dict[str, list[datetime]] = {}
        pid = int(program_id)
        with self._program_jobs_lock:
            jobs = [job for job in self.scheduler.get_jobs() if str(job.id).startswith(f"program:{pid}:")]
            for job in jobs:
                slot = self._program_slot_from_job_id(pid, str(job.id))
                if slot is None:
                    continue
                previous = None
                fire = job.trigger.get_next_fire_time(previous, range_start)
                emitted = 0
                while fire is not None and fire <= range_end and emitted < bounded_limit:
                    if fire >= range_start:
                        result.setdefault(slot, []).append(fire)
                        emitted += 1
                    next_fire = job.trigger.get_next_fire_time(fire, fire)
                    if next_fire is None or next_fire <= fire:
                        break
                    previous, fire = fire, next_fire

        for slot, values in result.items():
            result[slot] = sorted(set(values))[:bounded_limit]
        return result

    def get_active_zones(self) -> dict[int, datetime]:
        return self.active_zones.copy()

    def is_group_session_active(self, group_id: int) -> bool:
        """True iff the group currently has an in-flight sequence or program run.

        Equivalent to "group_cancel_events[gid] exists", because that key is
        created by start_group_sequence at sequence kickoff and by
        _run_program_threaded when a scheduled program fires. The Event being
        set means cancel-in-progress; the existence of the Event indicates
        a session.
        """
        try:
            with self._group_session_lock:
                return self.group_cancel_events.get(int(group_id)) is not None
        except (TypeError, ValueError, KeyError):
            return False

    def request_skip_current_zone(self, group_id: int) -> str:
        """Mark the currently running zone in this group's sequencer as 'skip me'.

        The in-thread loop polls the event each second; on detection it stops
        the current zone and continues to the next iteration. Idempotent
        within a single zone — repeated calls within the same zone produce a
        single skip.

        Returns:
          'ok'         — skip scheduled (event set).
          'no_session' — no active group session; nothing to skip.
          'debounced'  — a successful skip for this group landed <1.0s ago;
                         caller should treat as 429 Too Many Requests. Server
                         enforces this regardless of the frontend 1500ms
                         debounce, since that guard is bypassable
                         (multi-tab, mobile+desktop, scripted callers).
        """
        gid = int(group_id)
        if not self.is_group_session_active(gid):
            return "no_session"
        # Issue #14 C2: server-side per-group debounce.
        now = time.monotonic()
        last = self._last_skip_ts.get(gid, 0.0)
        if (now - last) < self._skip_debounce_seconds:
            return "debounced"
        self._last_skip_ts[gid] = now
        ev = self.group_skip_current_events.get(gid)
        if ev is None:
            ev = threading.Event()
            self.group_skip_current_events[gid] = ev
        ev.set()
        return "ok"

    def cancel_group_jobs(self, group_id: int, master_close_immediately: bool = False) -> dict[str, Any]:
        """Отменяет все активные задачи планировщика для указанной группы.

        master_close_immediately: при True мастер-клапан закрывается без задержки.
        Используется emergency_stop, чтобы внутренний _stop_all не перезаписал
        уже выполненный синхронный master close таймером с delay=60.

        Returns a structured physical outcome.  Safety timers are removed only
        for confirmed/idempotent OFF zones; unresolved relays retain (or gain)
        an activation-bound hard retry.
        """
        gid = int(group_id)
        result: dict[str, Any] = {"success": False, "stopped": [], "unresolved": [], "group_id": gid}
        try:
            # Ставим флаг отмены немедленно
            with self._group_session_lock:
                owned_event = self.group_cancel_events.get(gid)
                if owned_event is not None:
                    owned_event.set()

            zones = self.db.get_zones() or []
            group_zones = [z for z in zones if int(z.get("group_id") or 0) == gid]
            group_zone_ids = {int(z["id"]) for z in group_zones}

            # Немедленный OFF всем зонам группы через централизованный контроллер
            stop_result: object = None
            stop_call_failed = False
            try:
                from services.zone_control import stop_all_in_group as _stop_all

                stop_kwargs: dict[str, Any] = {
                    "reason": "group_cancel",
                    "force": True,
                    "master_close_immediately": bool(master_close_immediately),
                }
                if self._callable_accepts_keyword(_stop_all, "require_observed_confirmation"):
                    stop_kwargs["require_observed_confirmation"] = True
                else:
                    logger.warning("Confirmed group OFF API unavailable; treating legacy aggregate defensively")
                stop_result = _stop_all(gid, **stop_kwargs)
            except (sqlite3.Error, OSError, ValueError, TypeError):
                stop_call_failed = True
                logger.exception("cancel_group_jobs: stop_all_in_group failed")

            explicit_unresolved: set[int] = set()
            explicit_stopped: set[int] = set()
            if isinstance(stop_result, dict):
                raw_unresolved = stop_result.get("unresolved")
                raw_stopped = stop_result.get("stopped")
                if (
                    stop_result.get("success") is not True
                    or not isinstance(raw_unresolved, (list, tuple, set))
                    or not isinstance(raw_stopped, (list, tuple, set))
                ):
                    stop_call_failed = True
                else:
                    for value in raw_unresolved:
                        try:
                            explicit_unresolved.add(int(value))
                        except (TypeError, ValueError):
                            stop_call_failed = True
                    for value in raw_stopped:
                        try:
                            explicit_stopped.add(int(value))
                        except (TypeError, ValueError):
                            stop_call_failed = True
            elif stop_result is False:
                stop_call_failed = True

            unexpected_reported = (explicit_unresolved | explicit_stopped) - group_zone_ids
            if unexpected_reported:
                # Never let a malformed aggregate cancel safety ownership for
                # a zone outside the requested group.
                logger.error("Group %s bulk OFF reported foreign zones %s", gid, sorted(unexpected_reported))
                stop_call_failed = True
                explicit_unresolved.intersection_update(group_zone_ids)
                explicit_stopped.intersection_update(group_zone_ids)

            refreshed = {
                int(zone["id"]): zone for zone in (self.db.get_zones() or []) if int(zone.get("group_id") or 0) == gid
            }
            unresolved = (
                explicit_unresolved
                | (group_zone_ids if stop_call_failed else set())
                | {
                    zone_id
                    for zone_id in group_zone_ids
                    if str((refreshed.get(zone_id) or {}).get("state") or "").lower() != "off"
                }
            )
            stopped = (group_zone_ids | explicit_stopped) - unresolved

            for zone_id in sorted(group_zone_ids | explicit_unresolved | explicit_stopped):
                if zone_id in unresolved:
                    # Remove ordinary deadlines but preserve hard/cap safety.
                    self.cancel_zone_jobs(zone_id, preserve_safety=True)
                    if not self._has_zone_safety_job(zone_id):
                        self.schedule_zone_hard_stop(
                            zone_id,
                            self._controller_now() + timedelta(seconds=30),
                            activation_token=self._current_activation_token(zone_id),
                        )
                else:
                    if not self._zone_requires_observed_off(zone_id):
                        self.cancel_zone_jobs(zone_id, include_cap=True)
                    else:
                        # A broker ACK is not a physical OFF observation. Keep
                        # every surviving activation-bound safety retry until
                        # verifier/SSE confirmation, and replace a firing/absent
                        # last job before reporting a safe command outcome.
                        self.cancel_zone_jobs(zone_id, preserve_safety=True)
                        if not self._has_zone_safety_job(zone_id):
                            replanted = self.schedule_zone_hard_stop(
                                zone_id,
                                self._controller_now() + timedelta(seconds=30),
                                activation_token=self._current_activation_token(zone_id),
                            )
                            if replanted is False:
                                unresolved.add(zone_id)
                                stopped.discard(zone_id)

            # Отменяем задачи последовательного полива группы
            job_ids_to_remove = []
            for job in self.scheduler.get_jobs():
                jid = str(job.id)
                if jid.startswith(f"group_seq:{gid}:"):
                    job_ids_to_remove.append(job.id)

            removed_sequence_jobs = 0
            for job_id in job_ids_to_remove:
                try:
                    self.scheduler.remove_job(job_id)
                    removed_sequence_jobs += 1
                except (ValueError, KeyError, RuntimeError) as e:
                    logger.debug("Handled exception in line_980: %s", e)

            # A removed pending DateTrigger has no runner/finally to release
            # the claim.  Active executor sessions retain ownership until their
            # identity-safe cleanup completes.
            with self._group_session_lock:
                can_release_pending = removed_sequence_jobs > 0 or TESTING
                if (
                    can_release_pending
                    and gid not in self._running_group_sessions
                    and self.group_cancel_events.get(gid) is owned_event
                ):
                    self.group_cancel_events.pop(gid, None)

            # Перестраиваем расписание группы на ближайшую программу
            try:
                self.db.reschedule_group_to_next_program(gid)
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in line_986: %s", e)

            result.update(
                {
                    "success": not stop_call_failed and not unresolved,
                    "stopped": sorted(stopped),
                    "unresolved": sorted(unresolved),
                }
            )
            if stop_call_failed or unresolved:
                logger.error(
                    "Группа %s: OFF не подтверждён для зон %s; safety retries сохранены",
                    gid,
                    sorted(unresolved),
                )
            else:
                logger.info("Отменены все задачи планировщика для группы %s", gid)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as e:
            logger.error("Ошибка отмены задач группы %s: %s", gid, e)
            if not result["unresolved"]:
                try:
                    result["unresolved"] = sorted(
                        int(zone["id"]) for zone in (self.db.get_zones() or []) if int(zone.get("group_id") or 0) == gid
                    )
                except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
                    logger.exception("Could not enumerate unresolved zones for failed group cancel %s", gid)
        return result

    def _has_zone_safety_job(self, zone_id: int) -> bool:
        hard_id = f"zone_hard_stop:{int(zone_id)}"
        cap_id = f"zone_cap_stop:{int(zone_id)}"
        try:
            return any(str(job.id) in {hard_id, cap_id} for job in self.scheduler.get_jobs())
        except (AttributeError, RuntimeError, ValueError, TypeError):
            return False

    def cancel_zone_jobs(
        self,
        zone_id: int,
        *,
        include_cap: bool = False,
        preserve_safety: bool = False,
    ):
        """Cancel deadlines after a confirmed OFF.

        ``preserve_safety`` is used by failed group cancellation: ordinary
        stop jobs are removed, while the last hard/cap retry and active-zone
        marker remain authoritative.
        """
        try:
            job_ids_to_remove = []
            for job in self.scheduler.get_jobs():
                if job.id.startswith(f"zone_stop:{int(zone_id)}:"):
                    job_ids_to_remove.append(job.id)
                if not preserve_safety and job.id == f"zone_hard_stop:{int(zone_id)}":
                    job_ids_to_remove.append(job.id)
                if not preserve_safety and include_cap and job.id == f"zone_cap_stop:{int(zone_id)}":
                    job_ids_to_remove.append(job.id)
            for job_id in job_ids_to_remove:
                try:
                    self.scheduler.remove_job(job_id)
                    self._emit_timer_audit(
                        "scheduler_timer_cancel",
                        f"zone:{int(zone_id)}",
                        {"job_id": job_id},
                    )
                except (ValueError, KeyError, RuntimeError) as e:
                    logger.debug("Handled exception in cancel_zone_jobs: %s", e)
            if not preserve_safety:
                self.active_zones.pop(int(zone_id), None)
            logger.info(f"Отменены задачи автоостановки для зоны {zone_id}")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка отмены задач зоны {zone_id}: {e}")

    def _read_programs_for_reconcile(self) -> list[dict[str, Any]] | None:
        """Read programs strictly while isolating corrupt JSON per row.

        ProgramRepository historically deserializes the whole result set in a
        single loop.  One legacy row containing malformed JSON can therefore
        raise before later valid rows are returned, and some repository errors
        are represented as an unsafe empty list. Direct SQLite preserves
        malformed fields verbatim so validation can fail that row closed while
        continuing with the rest, while actual read errors return ``None``.
        """
        try:
            with sqlite3.connect(self.db.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM programs ORDER BY id").fetchall()
            programs: list[dict[str, Any]] = []
            for row in rows:
                program = dict(row)
                for field in ("days", "zones", "extra_times"):
                    value = program.get(field)
                    if isinstance(value, str):
                        try:
                            program[field] = json.loads(value)
                        except (json.JSONDecodeError, TypeError):
                            # Keep the invalid scalar; normalisation rejects it
                            # without preventing later rows from loading.
                            program[field] = value
                program["enabled"] = bool(program.get("enabled", 1))
                programs.append(program)
            return programs
        except (sqlite3.Error, OSError, TypeError, ValueError):
            logger.exception("Raw program fallback failed")
            return None

    def load_programs(self) -> bool:
        """Reconcile every DB program while preserving valid restored jobs.

        Corrupt legacy rows are isolated per program: their potentially stale
        persistent jobs are removed, other programs still load, and startup
        remains alive.  ``False`` reports that at least one row failed.
        """
        programs = self._read_programs_for_reconcile()
        if programs is None:
            return False

        all_ok = True
        seen_ids: set[int] = set()
        for program in programs:
            try:
                program_id = int(program["id"])
                seen_ids.add(program_id)
                if not self.schedule_program(program_id, program):
                    all_ok = False
            except (sqlite3.Error, OSError, KeyError, TypeError, ValueError, RuntimeError):
                all_ok = False
                logger.exception("Malformed persisted program skipped: %r", program)
                try:
                    if isinstance(program, dict) and program.get("id") is not None:
                        self.cancel_program(int(program["id"]))
                except (TypeError, ValueError, KeyError, RuntimeError):
                    logger.debug("Could not cancel malformed program jobs", exc_info=True)

        # Direct load_programs() callers do not necessarily run boot cleanup.
        # Remove orphan recurring jobs whose DB row no longer exists.
        for job in list(self.scheduler.get_jobs()):
            job_id = str(job.id)
            if not job_id.startswith("program:"):
                continue
            try:
                program_id = int(job_id.split(":", 2)[1])
            except (IndexError, TypeError, ValueError):
                try:
                    self.scheduler.remove_job(job_id)
                except (KeyError, RuntimeError, ValueError):
                    logger.debug("Could not remove malformed program job %s", job_id, exc_info=True)
                all_ok = False
                continue
            if program_id not in seen_ids:
                if self.cancel_program(program_id) is not True:
                    all_ok = False

        logger.info("Согласовано %d программ (ok=%s)", len(programs), all_ok)
        return all_ok

    @staticmethod
    def _decode_recovery_program_row(row: sqlite3.Row) -> dict[str, Any]:
        program = dict(row)
        for field in ("days", "zones", "extra_times"):
            value = program.get(field)
            if value is None and field != "zones":
                value = []
            if isinstance(value, str):
                value = json.loads(value)
            if not isinstance(value, list):
                raise ValueError(f"program {program.get('id')} has invalid {field}")
            program[field] = value
        program["enabled"] = bool(program.get("enabled", 1))
        return program

    def _read_recovery_inputs_strict(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Read boot recovery inputs without repository swallow-on-error semantics."""
        with sqlite3.connect(self.db.db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            # Python's sqlite3 legacy transaction mode does not start a read
            # transaction for SELECT.  Pin programs and zones to one snapshot:
            # mixing a pre-edit program row with post-edit zone durations can
            # otherwise invent a recovery window that never existed.
            conn.execute("BEGIN")
            program_rows = conn.execute("SELECT * FROM programs ORDER BY id").fetchall()
            zone_rows = conn.execute("SELECT * FROM zones ORDER BY id").fetchall()
            conn.commit()
        programs = [self._decode_recovery_program_row(row) for row in program_rows]
        return programs, [dict(row) for row in zone_rows]

    def _normalise_boot_recovery_intent(self, intent_id: str, raw: object) -> dict[str, Any]:
        if not isinstance(raw, dict) or str(raw.get("id") or "") != str(intent_id):
            raise ValueError("invalid boot recovery intent identity")
        program_id = raw.get("program_id")
        if isinstance(program_id, bool) or not isinstance(program_id, int) or program_id <= 0:
            raise ValueError("invalid boot recovery program id")
        zones = raw.get("zones")
        program_zones = raw.get("program_zones")
        if (
            not isinstance(zones, list)
            or not zones
            or any(isinstance(zone_id, bool) or not isinstance(zone_id, int) or zone_id <= 0 for zone_id in zones)
            or not isinstance(program_zones, list)
            or not program_zones
            or any(
                isinstance(zone_id, bool) or not isinstance(zone_id, int) or zone_id <= 0 for zone_id in program_zones
            )
        ):
            raise ValueError("invalid boot recovery zones")
        scheduled_start = str(raw.get("scheduled_start") or "")
        window_end = str(raw.get("window_end") or "")
        if self._parse_dt(scheduled_start) is None or self._parse_dt(window_end) is None:
            raise ValueError("invalid boot recovery window")
        program_name = raw.get("program_name")
        if not isinstance(program_name, str) or not program_name:
            raise ValueError("invalid boot recovery program name")
        return {
            "id": str(intent_id),
            "program_id": int(program_id),
            "program_name": program_name,
            "program_zones": [int(zone_id) for zone_id in program_zones],
            "zones": [int(zone_id) for zone_id in zones],
            "scheduled_start": scheduled_start,
            "window_end": window_end,
        }

    def _read_boot_recovery_intents_from_connection(self, conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (_BOOT_RECOVERY_INTENTS_KEY,),
        ).fetchone()
        if row is None:
            return {}
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("intents"), dict):
            raise ValueError("invalid durable boot recovery intent payload")
        return {
            str(intent_id): self._normalise_boot_recovery_intent(str(intent_id), raw)
            for intent_id, raw in payload["intents"].items()
        }

    def _read_boot_recovery_intents_strict(self) -> dict[str, dict[str, Any]]:
        with self._boot_recovery_intent_lock, sqlite3.connect(self.db.db_path, timeout=5) as conn:
            return self._read_boot_recovery_intents_from_connection(conn)

    @staticmethod
    def _write_boot_recovery_intents(conn: sqlite3.Connection, intents: dict[str, dict[str, Any]]) -> None:
        if not intents:
            conn.execute("DELETE FROM settings WHERE key = ?", (_BOOT_RECOVERY_INTENTS_KEY,))
            return
        payload = json.dumps(
            {"version": 1, "intents": intents},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            (_BOOT_RECOVERY_INTENTS_KEY, payload),
        )

    def _persist_boot_recovery_intent(self, intent: dict[str, Any]) -> bool:
        intent_id = str(intent["id"])
        canonical = self._normalise_boot_recovery_intent(intent_id, intent)
        try:
            with self._boot_recovery_intent_lock, sqlite3.connect(self.db.db_path, timeout=5) as conn:
                conn.execute("BEGIN IMMEDIATE")
                intents = self._read_boot_recovery_intents_from_connection(conn)
                intents[intent_id] = canonical
                self._write_boot_recovery_intents(conn, intents)
                conn.commit()
            return self._read_boot_recovery_intents_strict().get(intent_id) == canonical
        except (sqlite3.Error, OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.exception("Could not persist boot recovery intent %s", intent_id)
            return False

    def _clear_boot_recovery_intent(self, intent: dict[str, Any]) -> bool:
        intent_id = str(intent["id"])
        try:
            with self._boot_recovery_intent_lock, sqlite3.connect(self.db.db_path, timeout=5) as conn:
                conn.execute("BEGIN IMMEDIATE")
                intents = self._read_boot_recovery_intents_from_connection(conn)
                if intents.get(intent_id) != intent:
                    conn.rollback()
                    return False
                intents.pop(intent_id, None)
                self._write_boot_recovery_intents(conn, intents)
                conn.commit()
            return intent_id not in self._read_boot_recovery_intents_strict()
        except (sqlite3.Error, OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.exception("Could not clear boot recovery intent %s", intent_id)
            return False

    @staticmethod
    def _boot_recovery_job_id(intent_id: str) -> str:
        return f"boot_recovery:{intent_id}"

    def _ensure_boot_recovery_job(
        self,
        intent: dict[str, Any],
        *,
        run_at: datetime | None = None,
    ) -> bool:
        """Persist and strictly re-read one APS job for a durable intent."""
        if (
            self.jobstore_backend != "sqlalchemy"
            or not self.has_default_jobstore
            or getattr(self.scheduler, "state", STATE_STOPPED) == STATE_STOPPED
        ):
            logger.critical("Durable boot recovery requires the running SQLAlchemy jobstore")
            return False
        intent_id = str(intent["id"])
        job_id = self._boot_recovery_job_id(intent_id)
        run_at = run_at or self._controller_now()
        if run_at.tzinfo is None:
            timezone = self._controller_timezone()
            run_at = run_at.replace(tzinfo=timezone) if timezone is not None else run_at.astimezone()
        try:
            self.scheduler.add_job(
                job_run_boot_recovery,
                DateTrigger(run_date=run_at, timezone=self._controller_timezone()),
                args=[intent_id],
                id=job_id,
                jobstore="default",
                replace_existing=True,
                misfire_grace_time=None,
                coalesce=False,
                max_instances=1,
            )
            persisted = self.scheduler.get_job(job_id, jobstore="default")
            return bool(
                persisted is not None
                and str(persisted.id) == job_id
                and str(getattr(persisted, "func_ref", "")).endswith(":job_run_boot_recovery")
                and list(persisted.args) == [intent_id]
                and getattr(persisted, "misfire_grace_time", 0) is None
                and getattr(persisted, "_jobstore_alias", None) == "default"
            )
        except (RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error, OSError):
            logger.exception("Could not persist boot recovery job %s", job_id)
            return False

    def _execute_boot_recovery_intent(self, intent_id: str) -> bool:
        """Consume an intent only after success or a strict no-longer-applicable decision."""
        try:
            intent = self._read_boot_recovery_intents_strict().get(str(intent_id))
            if intent is None:
                return True
            now = self._controller_now(naive=True)
            window_end = self._parse_dt(intent["window_end"])
            if window_end is None:
                return False
            if now >= window_end:
                logger.info("Boot recovery intent %s expired safely", intent_id)
                return self._clear_boot_recovery_intent(intent)

            programs, _zones = self._read_recovery_inputs_strict()
            program = next((item for item in programs if int(item["id"]) == int(intent["program_id"])), None)
            if program is None or not program.get("enabled", True):
                logger.info("Boot recovery intent %s is no longer applicable", intent_id)
                return self._clear_boot_recovery_intent(intent)
            current_program_zones = sorted({int(zone_id) for zone_id in (program.get("zones") or [])})
            if current_program_zones != intent["program_zones"]:
                logger.info("Boot recovery intent %s superseded by program edit", intent_id)
                return self._clear_boot_recovery_intent(intent)

            completed = self._run_program_threaded(
                int(intent["program_id"]),
                list(intent["zones"]),
                str(intent["program_name"]),
                manual=False,
            )
            if completed is True:
                return self._clear_boot_recovery_intent(intent)

            # Keep the intent authoritative after a failed start/stop. Plant a
            # terminal expiry callback so it cannot linger forever in a live
            # process; a crash before then is recovered from the intent itself.
            expiry = window_end.replace(tzinfo=self._controller_timezone())
            self._ensure_boot_recovery_job(intent, run_at=expiry)
            return False
        except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            logger.exception("Boot recovery intent execution failed (intent_id=%s)", intent_id)
            return False

    def boot_recovery_handoff_is_durable(self) -> bool:
        """Public lifecycle ACK: recovery ownership is durably transferred."""
        return bool(self._boot_recovery_completed and self._boot_recovery_handoff_durable)

    @staticmethod
    def _program_start_times(p: dict[str, Any]) -> list[str]:
        """Основное время программы плюс extra_times (строки 'HH:MM')."""
        times = [str(p.get("time") or "00:00")]
        extra = p.get("extra_times") or []
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except (json.JSONDecodeError, TypeError):
                extra = []
        times.extend(str(t) for t in extra)
        return times

    @staticmethod
    def _program_scheduled_on(p: dict[str, Any], when: datetime) -> bool:
        """Мог ли в указанную локальную дату быть плановый запуск программы.

        Семантика повторяет расписание для calendar-триггеров: weekdays — день
        недели в days, even-odd — чётность числа месяца. Interval здесь
        намеренно возвращает False: recovery получает его даты только из
        authoritative live IntervalTrigger через get_program_occurrences().
        """
        schedule_type = p.get("schedule_type") or "weekdays"
        if schedule_type == "interval":
            return False
        if schedule_type == "even-odd":
            want_even = p.get("even_odd", "even") == "even"
            return (when.day % 2 == 0) == want_even
        return when.weekday() in (p.get("days") or [])

    @staticmethod
    def _program_scheduled_today(p: dict[str, Any], now: datetime) -> bool:
        """Backward-compatible alias used by existing scheduler tests."""
        return IrrigationScheduler._program_scheduled_on(p, now)

    def recover_missed_runs(self, require_interrupted_evidence: bool = False) -> bool:
        """Догоняем прерванный старт после безопасной boot-реконсиляции.

        Учитываются enabled, schedule_type (weekdays/interval/even-odd) и все
        времена старта (time + extra_times) — как в schedule_program.  Boot
        callers set ``require_interrupted_evidence`` so a merely nominal time
        window cannot resurrect a weather-shortened or manually stopped run.
        """
        all_ok = True
        try:
            if require_interrupted_evidence:
                programs, zones_all = self._read_recovery_inputs_strict()
            else:
                programs = self.db.get_programs()
                zones_all = self.db.get_zones()
            now = self._controller_now(naive=True)
            zones_by_id = {int(z["id"]): z for z in zones_all}

            if require_interrupted_evidence:
                # A scheduler-owned intent survives APScheduler's one-shot
                # submit/removal window. Recreate every missing Date job before
                # evaluating new lifecycle evidence.
                intents = self._read_boot_recovery_intents_strict()
                for intent in intents.values():
                    window_end = self._parse_dt(intent["window_end"])
                    if window_end is None:
                        all_ok = False
                        continue
                    if now >= window_end:
                        all_ok = self._clear_boot_recovery_intent(intent) and all_ok
                    elif not self._ensure_boot_recovery_job(intent):
                        all_ok = False

            for p in programs:
                try:
                    if not p.get("enabled", True):
                        continue
                    zones = sorted([int(z) for z in (p.get("zones") or [])])
                    if not zones:
                        continue
                    interrupted_indices = [
                        idx for idx, zone_id in enumerate(zones) if zone_id in self._boot_interrupted_zone_ids
                    ]
                    if require_interrupted_evidence and not interrupted_indices:
                        continue
                    # Если какая-то зона уже включена — программа идёт
                    if any((zones_by_id.get(zid) or {}).get("state") == "on" for zid in zones):
                        continue
                    durations = [int((zones_by_id.get(zid) or {}).get("duration") or 0) for zid in zones]
                    total_min = sum(durations)
                    candidates: list[datetime] = []
                    if (p.get("schedule_type") or "weekdays") == "interval":
                        # Interval phase is defined by the preserved live
                        # IntervalTrigger anchor, never by service-start time.
                        # Missing metadata fails closed rather than inventing a
                        # new occurrence and watering on the wrong phase.
                        occurrence_start = now - timedelta(minutes=max(1, total_min))
                        occurrences = self.get_program_occurrences(
                            int(p["id"]),
                            occurrence_start,
                            now,
                        )
                        timezone = self._controller_timezone()
                        for fires in occurrences.values():
                            for fire in fires:
                                if fire.tzinfo is not None:
                                    fire = (
                                        fire.astimezone(timezone).replace(tzinfo=None)
                                        if timezone is not None
                                        else fire.astimezone().replace(tzinfo=None)
                                    )
                                if fire <= now < fire + timedelta(minutes=total_min):
                                    candidates.append(fire)
                    else:
                        # Include yesterday so windows crossing local midnight
                        # are recoverable. When main/extra windows overlap, the
                        # earliest fire owns the single group session.
                        for day_offset in (0, -1):
                            scheduled_date = now + timedelta(days=day_offset)
                            if not self._program_scheduled_on(p, scheduled_date):
                                continue
                            for time_str in self._program_start_times(p):
                                try:
                                    hh, mm = map(int, str(time_str).split(":", 1))
                                except (ValueError, TypeError):
                                    continue
                                sd = scheduled_date.replace(hour=hh, minute=mm, second=0, microsecond=0)
                                if sd <= now < sd + timedelta(minutes=total_min):
                                    candidates.append(sd)
                    if not candidates:
                        continue
                    start_dt = min(candidates)
                    # Индекс первой незавершённой зоны согласно прошедшему времени
                    elapsed_min = int((now - start_dt).total_seconds() // 60)
                    cumulative = 0
                    start_idx = 0
                    for idx, dur in enumerate(durations):
                        if elapsed_min >= cumulative + dur:
                            cumulative += dur
                            start_idx = idx + 1
                        else:
                            start_idx = idx
                            break
                    if require_interrupted_evidence:
                        # The persisted active zone is stronger evidence than
                        # nominal/weather-unadjusted elapsed math.  Restart that
                        # interrupted zone, then continue its tail.
                        start_idx = min(interrupted_indices)
                    if start_idx >= len(zones):
                        continue
                    recovery_zones = zones[start_idx:]
                    recovered_name = str(p.get("name") or f"program_{p.get('id')}") + " (recovered)"
                    recovery_run_at = self._controller_now()
                    if getattr(self.scheduler, "state", None) == STATE_RUNNING:
                        recovery_run_at += timedelta(seconds=1)

                    if require_interrupted_evidence:
                        intent_id = f"{int(p['id'])}:{start_dt.strftime('%Y%m%dT%H%M%S')}"
                        intent = {
                            "id": intent_id,
                            "program_id": int(p["id"]),
                            "program_name": recovered_name,
                            "program_zones": zones,
                            "zones": recovery_zones,
                            "scheduled_start": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                            "window_end": (start_dt + timedelta(minutes=total_min)).strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        if not self._persist_boot_recovery_intent(intent):
                            all_ok = False
                            continue
                        if not self._ensure_boot_recovery_job(intent, run_at=recovery_run_at):
                            all_ok = False
                            continue
                    else:
                        _kwargs = dict(
                            args=[int(p["id"]), recovery_zones, recovered_name],
                            id=f"program_{int(p['id'])}_recover_{int(time.time())}",
                            replace_existing=False,
                            misfire_grace_time=300,
                            coalesce=False,
                            max_instances=1,
                        )
                        if getattr(self, "has_volatile_jobstore", False):
                            _kwargs["jobstore"] = "volatile"
                        self.scheduler.add_job(
                            job_run_program,
                            DateTrigger(
                                run_date=recovery_run_at,
                                timezone=self._controller_timezone(),
                            ),
                            **_kwargs,
                        )
                    logger.info(f"Recovery: программа {p['id']} — запущены оставшиеся зоны с индекса {start_idx}")
                except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
                    all_ok = False
                    logger.error(f"Ошибка recovery для программы {p.get('id')}: {e}")
        except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            all_ok = False
            logger.error(f"Ошибка recover_missed_runs: {e}")
        return all_ok

    # === Boot-time remediation ===
    def cleanup_jobs_on_boot(self) -> bool:
        all_ok = True
        try:
            job_ids_to_remove = []
            # Boot cleanup cannot trust repository fail-soft semantics: an
            # empty list caused by a read error would look like permission to
            # delete every persistent program job.
            with sqlite3.connect(self.db.db_path, timeout=5) as conn:
                valid_program_ids = {int(row[0]) for row in conn.execute("SELECT id FROM programs").fetchall()}
            for job in self.scheduler.get_jobs():
                jid = str(job.id)
                if jid.startswith("zone_stop:") or jid.startswith("group_seq:"):
                    job_ids_to_remove.append(jid)
                    continue
                if jid.startswith("program:"):
                    try:
                        program_id = int(jid.split(":", 2)[1])
                    except (IndexError, TypeError, ValueError):
                        program_id = -1
                    if program_id not in valid_program_ids:
                        job_ids_to_remove.append(jid)
            removed_count = 0
            for jid in job_ids_to_remove:
                try:
                    self.scheduler.remove_job(jid)
                    removed_count += 1
                except KeyError:
                    continue
                except (ValueError, RuntimeError) as e:
                    all_ok = False
                    logger.debug("Handled exception in cleanup_jobs_on_boot: %s", e)
            logger.info("Boot cleanup: removed %d/%d jobs", removed_count, len(job_ids_to_remove))
        except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError) as e:
            all_ok = False
            logger.error(f"Boot cleanup failed: {e}")
        return all_ok

    def stop_on_boot_active_zones(self) -> bool:
        """Abort crash-open history before issuing ordinary forced OFF paths."""
        all_ok = True
        try:
            # A repository read error is historically represented as ``[]``.
            # At boot that is indistinguishable from "no active relays" and is
            # therefore unsafe; use a strict direct read instead.
            with sqlite3.connect(self.db.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                zones = [dict(row) for row in conn.execute("SELECT * FROM zones ORDER BY id").fetchall()]
            interrupted = {
                int(z["id"])
                for z in zones
                if str(z.get("state") or "").lower() in ("starting", "on", "stopping", "paused")
            }
            self._boot_interrupted_zone_ids = interrupted
            # init_scheduler runs before services.app_init._boot_sync.  Closing
            # crash-open rows here is therefore mandatory: otherwise the first
            # normal stop_zone call below finalizes yesterday's interrupted run
            # as a successful watering at boot time.
            try:
                with sqlite3.connect(self.db.db_path, timeout=5) as conn:
                    conn.execute(
                        "UPDATE zone_runs SET end_utc = CURRENT_TIMESTAMP, "
                        "status = 'aborted', updated_at = CURRENT_TIMESTAMP "
                        "WHERE end_utc IS NULL"
                    )
                    conn.commit()
            except (sqlite3.Error, OSError):
                all_ok = False
                logger.exception("Boot remediation: could not abort crash-open zone_runs")
            for z in zones:
                st = str(z.get("state") or "").lower()
                if st in ("starting", "on", "stopping", "paused"):
                    try:
                        from services.zone_control import stop_zone as _stop

                        if not _stop(int(z["id"]), reason="recovery_boot", force=True):
                            all_ok = False
                    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                        all_ok = False
                        logger.debug("Handled exception in stop_on_boot_active_zones: %s", e)
            logger.info("Boot remediation: active zones forced to OFF")
        except (sqlite3.Error, OSError, ValueError, TypeError) as e:
            all_ok = False
            logger.error(f"Boot remediation failed: {e}")
        return all_ok

    def complete_boot_recovery(self) -> bool:
        """Recover interrupted runs and unpause jobs after external boot OFF.

        Application startup must call this only after its MQTT/master-valve
        reconciliation has completed.  Until then init_scheduler leaves
        APScheduler paused, preventing both persistent cron misfires and
        recovery Date jobs from racing the final boot OFF.
        """
        if self._boot_recovery_completed:
            return self.boot_recovery_handoff_is_durable()
        self._boot_recovery_handoff_durable = False
        if self._boot_reconcile_ok is not True:
            failures = sorted(self._boot_reconcile_failures) or ["not_recorded"]
            logger.critical(
                "Scheduler remains paused: required boot reconciliation failed (%s)",
                ", ".join(failures),
            )
            return False
        try:
            if self.recover_missed_runs(require_interrupted_evidence=True) is not True:
                logger.critical("Scheduler remains paused: boot recovery durable handoff failed")
                return False
            if self._started_paused:
                self.scheduler.resume()
                self._started_paused = False
            self._boot_recovery_handoff_durable = True
            self._boot_recovery_completed = True
            return True
        except (RuntimeError, AttributeError, ValueError, TypeError) as e:
            self._boot_recovery_handoff_durable = False
            logger.error("Boot recovery completion failed: %s", e)
            return False


# Глобальный экземпляр планировщика
scheduler: IrrigationScheduler | None = None


def init_scheduler(db: IrrigationDB):
    global scheduler
    if scheduler is None:
        scheduler = IrrigationScheduler(db)
        # Start paused: restored persistent jobs must not run before boot
        # cleanup and the application's physical OFF reconciliation.
        scheduler.start(paused=True)
        scheduler._started_paused = True
        reconcile_failures: set[str] = set()
        # Очистим истекшие отложки на старте
        try:
            scheduler.clear_expired_postpones()
        except (ValueError, KeyError, RuntimeError) as e:
            logger.debug("Handled exception in init_scheduler: %s", e)
        # Boot-time cleanup and stop lingering active zones while paused.
        try:
            if scheduler.cleanup_jobs_on_boot() is not True:
                reconcile_failures.add("jobs")
        except (sqlite3.Error, OSError, ValueError, KeyError, RuntimeError, TypeError, AttributeError):
            reconcile_failures.add("jobs")
            logger.exception("Boot job cleanup failed")
        try:
            if scheduler.stop_on_boot_active_zones() is not True:
                reconcile_failures.add("active_zones")
        except (sqlite3.Error, OSError, ValueError, KeyError, RuntimeError, TypeError, AttributeError):
            reconcile_failures.add("active_zones")
            logger.exception("Boot active-zone reconciliation failed")
        try:
            if scheduler.load_programs() is not True:
                reconcile_failures.add("programs")
        except (sqlite3.Error, OSError, ValueError, KeyError, RuntimeError, TypeError, AttributeError):
            reconcile_failures.add("programs")
            logger.exception("Boot program reconciliation failed")
        scheduler._boot_reconcile_failures = reconcile_failures
        scheduler._boot_reconcile_ok = not reconcile_failures
        if reconcile_failures:
            logger.critical(
                "Scheduler boot reconciliation failed closed; recurring jobs remain paused (%s)",
                ", ".join(sorted(reconcile_failures)),
            )
        # Recovery is deliberately deferred to complete_boot_recovery(),
        # called by app startup after its own MQTT/master OFF sync.
    return scheduler


def get_scheduler() -> IrrigationScheduler | None:
    return scheduler
