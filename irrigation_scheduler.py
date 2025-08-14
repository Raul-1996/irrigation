#!/usr/bin/env python3
"""
Система планировщика полива WB-Irrigation
Реализует алгоритм последовательного запуска зон с APScheduler
"""

import threading
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import logging
from database import IrrigationDB
import json
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class IrrigationScheduler:
    """Планировщик полива с последовательным запуском зон"""

    def __init__(self, db: IrrigationDB):
        self.db = db
        self.scheduler = BackgroundScheduler()
        self.active_zones: Dict[int, datetime] = {}
        self.program_jobs: Dict[int, List[str]] = {}  # program_id -> list(job_id)
        self.is_running = False

    def start(self):
        if self.is_running:
            return
        self.scheduler.start()
        self.is_running = True
        logger.info("Планировщик полива (APScheduler) запущен")

    def stop(self):
        if not self.is_running:
            return
        self.scheduler.shutdown(wait=False)
        self.is_running = False
        logger.info("Планировщик полива остановлен")

    def _stop_zone(self, zone_id: int):
        try:
            self.db.update_zone(zone_id, {'state': 'off'})
            zone = self.db.get_zone(zone_id)
            if zone:
                self.db.add_log('zone_auto_stop', f'Зона {zone_id} ({zone["name"]}) автоматически остановлена')
            logger.info(f"Зона {zone_id} остановлена")
        except Exception as e:
            logger.error(f"Ошибка остановки зоны {zone_id}: {e}")

    def _run_program_threaded(self, program_id: int, zones: List[int], program_name: str):
        """Последовательный запуск зон в отдельном потоке, чтобы не блокировать APScheduler"""
        try:
            logger.info(f"Запуск программы {program_id} ({program_name})")
            for i, zone_id in enumerate(zones):
                zone = self.db.get_zone(zone_id)
                if not zone:
                    logger.warning(f"Зона {zone_id} не найдена")
                    continue

                # Проверяем отложенный полив
                postpone_until = zone.get('postpone_until')
                if postpone_until:
                    try:
                        postpone_dt = datetime.strptime(postpone_until, '%Y-%m-%d %H:%M')
                        if datetime.now() < postpone_dt:
                            logger.info(f"Зона {zone_id} отложена до {postpone_until}")
                            continue
                        else:
                            self.db.update_zone_postpone(zone_id, None)
                    except Exception:
                        # Если формат неожиданный — сбрасываем отложку
                        self.db.update_zone_postpone(zone_id, None)

                duration = int(zone['duration'])
                # Старт зоны
                try:
                    self.db.update_zone(zone_id, {'state': 'on'})
                    end_time = datetime.now() + timedelta(minutes=duration)
                    self.active_zones[zone_id] = end_time
                    self.db.add_log('zone_auto_start', json.dumps({
                        'zone_id': zone_id,
                        'zone_name': zone['name'],
                        'program_id': program_id,
                        'program_name': program_name,
                        'duration': duration,
                        'end_time': end_time.strftime('%Y-%m-%d %H:%M:%S')
                    }))
                except Exception as e:
                    logger.error(f"Ошибка запуска зоны {zone_id}: {e}")
                    continue

                # Ждем окончания текущей зоны, затем выключаем её
                try:
                    threading.Event().wait(duration * 60)
                finally:
                    self._stop_zone(zone_id)
                    self.active_zones.pop(zone_id, None)

            logger.info(f"Программа {program_id} ({program_name}) завершена")
        except Exception as e:
            logger.error(f"Ошибка в выполнении программы {program_id}: {e}")

    def schedule_program(self, program_id: int, program_data: Dict[str, Any]):
        try:
            time_str = program_data['time']  # 'HH:MM'
            hours, minutes = map(int, time_str.split(':'))
            days: List[int] = program_data['days']  # 0-6, где 0=Пн
            zones: List[int] = list(program_data['zones'])
            zones.sort()

            if not days or not zones:
                logger.warning(f"Программа {program_id} имеет пустые дни или зоны, пропуск")
                return

            # Удаляем прежние задания программы
            self.cancel_program(program_id)

            job_ids: List[str] = []
            # APScheduler CronTrigger: day_of_week принимает 0-6, 0=Monday
            for day in days:
                trigger = CronTrigger(day_of_week=day, hour=hours, minute=minutes)
                job = self.scheduler.add_job(
                    self._run_program_threaded,
                    trigger,
                    args=[program_id, zones, program_data['name']],
                    id=f"program_{program_id}_d{day}",
                    replace_existing=True,
                    misfire_grace_time=300,
                )
                job_ids.append(job.id)

            self.program_jobs[program_id] = job_ids
            logger.info(f"Программа {program_id} ({program_data['name']}) запланирована на дни {days} в {time_str}")
        except Exception as e:
            logger.error(f"Ошибка планирования программы {program_id}: {e}")

    def cancel_program(self, program_id: int):
        try:
            job_ids = self.program_jobs.get(program_id, [])
            for job_id in job_ids:
                try:
                    self.scheduler.remove_job(job_id)
                except Exception:
                    pass
            self.program_jobs[program_id] = []
            logger.info(f"Программа {program_id} отменена")
        except Exception as e:
            logger.error(f"Ошибка отмены программы {program_id}: {e}")

    def get_active_programs(self) -> Dict[int, Dict[str, Any]]:
        # Возвращаем список запланированных программ и их job_ids
        return {pid: {'job_ids': jobs} for pid, jobs in self.program_jobs.items()}

    def get_active_zones(self) -> Dict[int, datetime]:
        return self.active_zones.copy()

    def load_programs(self):
        try:
            programs = self.db.get_programs()
            for program in programs:
                self.schedule_program(program['id'], program)
            logger.info(f"Загружено {len(programs)} программ")
        except Exception as e:
            logger.error(f"Ошибка загрузки программ: {e}")


# Глобальный экземпляр планировщика
scheduler: Optional[IrrigationScheduler] = None


def init_scheduler(db: IrrigationDB):
    global scheduler
    if scheduler is None:
        scheduler = IrrigationScheduler(db)
        scheduler.start()
        scheduler.load_programs()
    return scheduler


def get_scheduler() -> Optional[IrrigationScheduler]:
    return scheduler
