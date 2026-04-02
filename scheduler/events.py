#!/usr/bin/env python3
"""
Event handling mixin: postpone sweeper, bot subscriptions dispatcher.
"""
import logging
from datetime import datetime
from apscheduler.triggers.interval import IntervalTrigger

from scheduler.jobs import job_clear_expired_postpones, job_dispatch_bot_subscriptions

logger = logging.getLogger(__name__)


class EventsMixin:
    """Mixin for periodic event scheduling on IrrigationScheduler."""

    def schedule_postpone_sweeper(self) -> None:
        """Планирует периодическую очистку истекших отложек (каждую минуту)."""
        try:
            self.scheduler.add_job(
                job_clear_expired_postpones,
                trigger=IntervalTrigger(minutes=1),
                id='postpone_sweeper',
                replace_existing=True,
                coalesce=False,
                max_instances=1,
                next_run_time=datetime.now()
            )
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Не удалось добавить джоб postpone_sweeper: {e}")
        try:
            self.scheduler.add_job(
                job_dispatch_bot_subscriptions,
                trigger=IntervalTrigger(minutes=1),
                id='bot_sub_dispatcher',
                replace_existing=True,
                coalesce=False,
                max_instances=1,
                next_run_time=datetime.now()
            )
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Не удалось добавить джоб bot_sub_dispatcher: {e}")
