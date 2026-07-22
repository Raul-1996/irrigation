#!/usr/bin/env python3
"""
Module-level job callables for APScheduler persistence.
These must be importable top-level functions (not lambdas or methods).

Канонические реализации живут в ``irrigation_scheduler`` и ре-экспортируются
отсюда: SQLAlchemyJobStore (jobs.db) хранит func-ref строкой
``scheduler.jobs:<name>`` для ранее записанных джобов, поэтому все имена
обязаны оставаться импортируемыми из этого модуля.
``job_recalc_water_balance`` определён только здесь — персистентные джобы
ссылаются на ``scheduler.jobs:job_recalc_water_balance``.
"""

import logging
import os
import sqlite3

from irrigation_scheduler import (
    job_clear_expired_postpones,
    job_close_master_valve,
    job_dispatch_bot_subscriptions,
    job_run_group_sequence,
    job_run_program,
    job_stop_zone,
)

__all__ = [
    "job_clear_expired_postpones",
    "job_close_master_valve",
    "job_dispatch_bot_subscriptions",
    "job_recalc_water_balance",
    "job_run_group_sequence",
    "job_run_program",
    "job_stop_zone",
]

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
