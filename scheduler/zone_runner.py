#!/usr/bin/env python3
"""
Zone runner mixin: zone start/stop, zone job scheduling, master valve caps.
"""
import sqlite3
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.triggers.date import DateTrigger

from scheduler.jobs import job_stop_zone, job_close_master_valve

logger = logging.getLogger(__name__)


class ZoneRunnerMixin:
    """Mixin for zone-level operations on IrrigationScheduler."""

    def _stop_zone(self, zone_id: int):
        try:
            zone = self.db.get_zone(zone_id)
            last_time = None
            if zone and zone.get('watering_start_time'):
                last_time = zone['watering_start_time']
            try:
                from services.zone_control import stop_zone as _stop_zone_central
                _stop_zone_central(zone_id, reason='auto_stop')
            except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                logger.debug("Exception in _stop_zone: %s", e)
                self.db.update_zone(zone_id, {
                    'state': 'off',
                    'watering_start_time': None,
                    'last_watering_time': last_time
                })
            try:
                logger.debug("auto-stop zone=%s", zone_id)
            except (ValueError, KeyError, RuntimeError) as e:
                logger.debug("Handled exception in _stop_zone: %s", e)
            zone = self.db.get_zone(zone_id)
            if zone:
                self.db.add_log('zone_auto_stop', f'Зона {zone_id} ({zone["name"]}) автоматически остановлена')
            logger.info(f"Зона {zone_id} остановлена")
        except (sqlite3.Error, OSError, ValueError, TypeError) as e:
            logger.error(f"Ошибка остановки зоны {zone_id}: {e}")

    def schedule_zone_stop(self, zone_id: int, duration_minutes: int, command_id: Optional[str] = None):
        """Запланировать автоматическую остановку зоны через duration_minutes минут (для ручных запусков)."""
        try:
            if duration_minutes is None:
                return
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
            if os.getenv('TESTING') == '1':
                total_seconds = min(6, max(1, int(duration_minutes)))
                early = 0
                run_at = datetime.now() + timedelta(seconds=total_seconds)
            else:
                run_at = datetime.now() + timedelta(minutes=int(duration_minutes)) - timedelta(seconds=early)
            now = datetime.now()
            if run_at <= now:
                run_at = now + timedelta(seconds=1)
            _kwargs = dict(
                args=[zone_id],
                id=(f"zone_stop:{int(zone_id)}:{str(command_id)}" if command_id else f"zone_stop:{int(zone_id)}:{int(run_at.timestamp())}"),
                replace_existing=False,
                misfire_grace_time=120,
            )
            if getattr(self, 'has_volatile_jobstore', False):
                _kwargs['jobstore'] = 'volatile'
            self.scheduler.add_job(
                job_stop_zone,
                DateTrigger(run_date=run_at),
                **_kwargs,
            )
            self.active_zones[zone_id] = run_at
            logger.info(f"Автоостановка зоны {zone_id} запланирована на {run_at}")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка планирования автоостановки зоны {zone_id}: {e}")

    def schedule_zone_hard_stop(self, zone_id: int, run_at: datetime):
        """Жёсткий watchdog-стоп зоны на точное время run_at (доп. страховка)."""
        try:
            now = datetime.now()
            if run_at <= now:
                run_at = now + timedelta(seconds=1)
            _kwargs = dict(
                args=[zone_id],
                id=f"zone_hard_stop:{int(zone_id)}",
                replace_existing=True,
                misfire_grace_time=60,
                coalesce=False,
                max_instances=1,
            )
            if getattr(self, 'has_volatile_jobstore', False):
                _kwargs['jobstore'] = 'volatile'
            self.scheduler.add_job(
                job_stop_zone,
                DateTrigger(run_date=run_at),
                **_kwargs,
            )
            logger.info(f"Watchdog: zone {zone_id} hard-stop at {run_at}")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка планирования watchdog-стопа зоны {zone_id}: {e}")

    def schedule_zone_cap(self, zone_id: int, cap_minutes: int = 240):
        """Абсолютный лимит работы зоны: форс-стоп через cap_minutes от текущего момента."""
        try:
            run_at = datetime.now() + timedelta(minutes=int(cap_minutes))
            job_id = f"zone_cap_stop:{int(zone_id)}"
            _kwargs = dict(
                args=[zone_id],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=300,
                coalesce=False,
                max_instances=1,
            )
            if getattr(self, 'has_volatile_jobstore', False):
                _kwargs['jobstore'] = 'volatile'
            self.scheduler.add_job(job_stop_zone, DateTrigger(run_date=run_at), **_kwargs)
            logger.info(f"Zone cap: zone {zone_id} hard-stop at {run_at} (cap {cap_minutes}m)")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка планирования cap-стопа зоны {zone_id}: {e}")

    def cancel_zone_cap(self, zone_id: int):
        try:
            job_id = f"zone_cap_stop:{int(zone_id)}"
            try:
                self.scheduler.remove_job(job_id)
            except (ValueError, KeyError, RuntimeError) as e:
                logger.debug("Handled exception in cancel_zone_cap: %s", e)
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка отмены cap-стопа зоны {zone_id}: {e}")

    def cancel_zone_jobs(self, zone_id: int):
        """Отменяет все задачи автоостановки для зоны и убирает её из active_zones."""
        try:
            job_ids_to_remove = []
            for job in self.scheduler.get_jobs():
                if job.id.startswith(f"zone_stop:{int(zone_id)}:"):
                    job_ids_to_remove.append(job.id)
                if job.id == f"zone_hard_stop:{int(zone_id)}":
                    job_ids_to_remove.append(job.id)
            for job_id in job_ids_to_remove:
                try:
                    self.scheduler.remove_job(job_id)
                except (ValueError, KeyError, RuntimeError) as e:
                    logger.debug("Handled exception in cancel_zone_jobs: %s", e)
            self.active_zones.pop(int(zone_id), None)
            logger.info(f"Отменены задачи автоостановки для зоны {zone_id}")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка отмены задач зоны {zone_id}: {e}")

    def schedule_master_valve_cap(self, group_id: int, hours: int = 24):
        """Абсолютный лимит открытого мастер-клапана — закрыть через hours часов."""
        try:
            run_at = datetime.now() + timedelta(hours=int(hours))
            job_id = f"master_cap_close:{int(group_id)}"
            _kwargs = dict(
                args=[group_id],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=600,
                coalesce=False,
                max_instances=1,
            )
            if getattr(self, 'has_volatile_jobstore', False):
                _kwargs['jobstore'] = 'volatile'
            self.scheduler.add_job(job_close_master_valve, DateTrigger(run_date=run_at), **_kwargs)
            logger.info(f"Master valve cap: group {group_id} close at {run_at} (cap {hours}h)")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка планирования cap-закрытия мастер-клапана для группы {group_id}: {e}")

    def cancel_master_valve_cap(self, group_id: int):
        try:
            job_id = f"master_cap_close:{int(group_id)}"
            try:
                self.scheduler.remove_job(job_id)
            except (ValueError, KeyError, RuntimeError) as e:
                logger.debug("Handled exception in cancel_master_valve_cap: %s", e)
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка отмены cap-закрытия мастер-клапана для группы {group_id}: {e}")
