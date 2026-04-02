#!/usr/bin/env python3
"""
Schedule calculation mixin: program scheduling, cancel, load_programs.
"""
import sqlite3
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from scheduler.jobs import job_run_program

logger = logging.getLogger(__name__)


class ScheduleCalcMixin:
    """Mixin for program scheduling on IrrigationScheduler."""

    def schedule_program(self, program_id: int, program_data: Dict[str, Any]):
        try:
            if not program_data.get('enabled', True):
                logger.info(f"Программа {program_id} выключена (enabled=0), отменяем расписание")
                self.cancel_program(program_id)
                return

            time_str = program_data['time']
            hours, minutes = map(int, time_str.split(':'))
            zones: List[int] = list(program_data['zones'])
            zones.sort()

            if not zones:
                logger.warning(f"Программа {program_id} имеет пустые зоны, пропуск")
                return

            schedule_type = program_data.get('schedule_type', 'weekdays')
            days: List[int] = program_data.get('days', [])

            if schedule_type == 'weekdays' and not days:
                logger.warning(f"Программа {program_id} имеет schedule_type=weekdays но пустые дни, пропуск")
                return

            self.cancel_program(program_id)

            # Предварительно рассчитанные плановые старты зон
            try:
                now = datetime.now()
                cumulative = 0
                schedule_map: Dict[int, str] = {}
                for zid in zones:
                    zone = self.db.get_zone(zid)
                    if not zone:
                        continue
                    start_dt = datetime(now.year, now.month, now.day, hours, minutes) + timedelta(minutes=cumulative)
                    schedule_map[zid] = start_dt.strftime('%Y-%m-%d %H:%M:%S')
                    cumulative += int(zone.get('duration') or 0)
                for zid, ts in schedule_map.items():
                    self.db.update_zone(zid, {'scheduled_start_time': ts})
            except (sqlite3.Error, OSError) as e:
                logger.error(f"Ошибка расчета плановых стартов для программы {program_id}: {e}")

            job_ids: List[str] = []

            self._schedule_single_time(program_id, program_data, time_str, 'main', job_ids)

            extra_times = program_data.get('extra_times', [])
            if isinstance(extra_times, str):
                try:
                    extra_times = json.loads(extra_times)
                except (json.JSONDecodeError, TypeError):
                    extra_times = []

            for idx, extra_time in enumerate(extra_times):
                self._schedule_single_time(program_id, program_data, extra_time, f'extra:{idx}', job_ids)

            self.program_jobs[program_id] = job_ids
            logger.info(f"Программа {program_id} ({program_data['name']}) запланирована: {schedule_type}, {len(job_ids)} jobs")
        except (sqlite3.Error, OSError, KeyError, ValueError) as e:
            logger.error(f"Ошибка планирования программы {program_id}: {e}")

    def _schedule_single_time(self, program_id: int, program_data: Dict[str, Any], time_str: str, suffix: str, job_ids: List[str]):
        """Создать jobs для одного времени старта (main или extra_times)."""
        try:
            hours, minutes = map(int, time_str.split(':'))
            zones: List[int] = list(program_data['zones'])
            zones.sort()
            schedule_type = program_data.get('schedule_type', 'weekdays')
            days: List[int] = program_data.get('days', [])

            if schedule_type == 'weekdays':
                for day in days:
                    trigger = CronTrigger(day_of_week=day, hour=hours, minute=minutes)
                    _kwargs = dict(
                        args=[program_id, zones, program_data['name']],
                        id=f"program:{program_id}:{suffix}:d{day}",
                        replace_existing=True,
                        misfire_grace_time=3600,
                        coalesce=False,
                        max_instances=1,
                    )
                    if getattr(self, 'has_default_jobstore', False):
                        _kwargs['jobstore'] = 'default'
                    job = self.scheduler.add_job(
                        job_run_program,
                        trigger,
                        **_kwargs,
                    )
                    job_ids.append(job.id)

            elif schedule_type == 'interval':
                interval_days = int(program_data.get('interval_days', 1))
                now = datetime.now()
                start_date = datetime(now.year, now.month, now.day, hours, minutes, 0, 0)
                if start_date <= now:
                    start_date += timedelta(days=1)

                trigger = IntervalTrigger(days=interval_days, start_date=start_date)
                _kwargs = dict(
                    args=[program_id, zones, program_data['name']],
                    id=f"program:{program_id}:{suffix}",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=False,
                    max_instances=1,
                )
                if getattr(self, 'has_default_jobstore', False):
                    _kwargs['jobstore'] = 'default'
                job = self.scheduler.add_job(
                    job_run_program,
                    trigger,
                    **_kwargs,
                )
                job_ids.append(job.id)

            elif schedule_type == 'even-odd':
                even_odd = program_data.get('even_odd', 'even')
                if even_odd == 'even':
                    day_str = '2,4,6,8,10,12,14,16,18,20,22,24,26,28,30'
                else:
                    day_str = '1,3,5,7,9,11,13,15,17,19,21,23,25,27,29,31'

                trigger = CronTrigger(day=day_str, hour=hours, minute=minutes)
                _kwargs = dict(
                    args=[program_id, zones, program_data['name']],
                    id=f"program:{program_id}:{suffix}",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=False,
                    max_instances=1,
                )
                if getattr(self, 'has_default_jobstore', False):
                    _kwargs['jobstore'] = 'default'
                job = self.scheduler.add_job(
                    job_run_program,
                    trigger,
                    **_kwargs,
                )
                job_ids.append(job.id)

        except (ValueError, KeyError, TypeError) as e:
            logger.error(f"Ошибка планирования времени {time_str} для программы {program_id}: {e}")

    def cancel_program(self, program_id: int):
        try:
            job_ids = self.program_jobs.get(program_id, [])
            for job_id in job_ids:
                try:
                    self.scheduler.remove_job(job_id)
                except (ValueError, KeyError, RuntimeError) as e:
                    logger.debug("Handled exception in cancel_program: %s", e)
            self.program_jobs[program_id] = []
            logger.info(f"Программа {program_id} отменена")
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Ошибка отмены программы {program_id}: {e}")

    def load_programs(self):
        try:
            programs = self.db.get_programs()
            for program in programs:
                self.schedule_program(program['id'], program)
            logger.info(f"Загружено {len(programs)} программ")
        except (sqlite3.Error, OSError) as e:
            logger.error(f"Ошибка загрузки программ: {e}")
