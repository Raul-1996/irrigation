#!/usr/bin/env python3
"""
Module-level job callables for APScheduler persistence.
These must be importable top-level functions (not lambdas or methods).
"""

import logging
import os
import sqlite3
from datetime import datetime

logger = logging.getLogger(__name__)

# Логирование: не вызываем logging.basicConfig() на import-time (CQ-012 / MASTER-C2).
# Уровень теперь выставляет setup_logging() в services/logging_setup.py.
try:
    _sched_level_name = os.getenv("SCHEDULER_LOG_LEVEL", "").upper()
    if _sched_level_name:
        _sched_level = getattr(logging, _sched_level_name, None)
        if _sched_level is not None:
            logger.setLevel(_sched_level)
except (KeyError, TypeError, ValueError) as e:
    logger.debug("scheduler jobs log level from env: %s", e)
# В тестах отключаем propagate, чтобы не писать в закрытый stdout; в проде — False
# не нужен, записи должны доходить до root/app.log через propagation.
try:
    if "PYTEST_CURRENT_TEST" in os.environ:
        logger.propagate = False
except (KeyError, TypeError):
    pass
# Урезаем болтливость APScheduler
try:
    aps_logger = logging.getLogger("apscheduler")
    aps_logger.setLevel(logging.ERROR)
except (ImportError, AttributeError) as e:
    logger.debug("Handled exception in apscheduler logger setup: %s", e)


def job_run_program(program_id: int, zones: list, program_name: str, manual: bool = False):
    try:
        from irrigation_scheduler import get_scheduler

        s = get_scheduler()
        if s is not None:
            s._run_program_threaded(int(program_id), [int(z) for z in zones], str(program_name), manual=bool(manual))
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.debug("Handled exception in job_run_program: %s", e)


def job_run_group_sequence(group_id: int, zone_ids: list, override_duration: int | None = None, manual: bool = False):
    try:
        from irrigation_scheduler import get_scheduler

        s = get_scheduler()
        if s is not None:
            s._run_group_sequence(
                int(group_id), [int(z) for z in zone_ids], override_duration=override_duration, manual=bool(manual)
            )
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.debug("Handled exception in job_run_group_sequence: %s", e)


def job_stop_zone(zone_id: int):
    try:
        from irrigation_scheduler import get_scheduler

        s = get_scheduler()
        if s is not None:
            s._stop_zone(int(zone_id))
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.debug("Handled exception in job_stop_zone: %s", e)


def job_close_master_valve(group_id: int):
    """Закрыть мастер-клапан у группы, если он включён."""
    try:
        from database import db
        from services.mqtt_pub import publish_mqtt_value
        from utils import normalize_topic

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


def job_recalc_water_balance():
    """Nightly APScheduler job: recompute the H2 water-balance coefficient.

    Best-effort — ``recalc_balance`` swallows its own errors and leaves the
    previous cached coef in place on failure, so a bad night never crashes the
    scheduler. Runs regardless of the ``balance.enabled`` flag so shadow mode
    keeps writing the audit log while the flag is still off.
    """
    try:
        from database import db
        from services.weather.balance import recalc_balance

        recalc_balance(db.db_path)
    except (sqlite3.Error, OSError, ValueError, TypeError, ImportError) as e:
        logger.debug("Handled exception in job_recalc_water_balance: %s", e)


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
