#!/usr/bin/env python3
"""
Система планировщика полива WB-Irrigation
Реализует алгоритм последовательного запуска зон с APScheduler
"""

import threading
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import logging
from database import IrrigationDB
import json
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

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
        self.group_cancel_events: Dict[int, threading.Event] = {}

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
            # Фиксируем время последнего полива текущей зоны (время начала, если было)
            zone = self.db.get_zone(zone_id)
            last_time = None
            if zone and zone.get('watering_start_time'):
                last_time = zone['watering_start_time']
            self.db.update_zone(zone_id, {
                'state': 'off',
                'watering_start_time': None,
                'last_watering_time': last_time
            })
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

                # Если для группы зоны установлена отмена, пропускаем её
                group_id = int(zone.get('group_id') or 0)
                cancel_event = self.group_cancel_events.get(group_id)
                if cancel_event and cancel_event.is_set():
                    logger.info(f"Программа {program_id}: группа {group_id} отменена, зона {zone_id} пропущена")
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
                # Старт зоны: фиксируем время начала, чтобы таймер в UI работал
                try:
                    start_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    self.db.update_zone(zone_id, {'state': 'on', 'watering_start_time': start_ts})
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

                # Ждем окончания текущей зоны, проверяя отмену группы каждую секунду
                remaining = duration * 60
                while remaining > 0:
                    cancel_event = self.group_cancel_events.get(group_id)
                    if cancel_event and cancel_event.is_set():
                        logger.info(f"Программа {program_id}: отмена группы {group_id}, досрочно останавливаем зону {zone_id}")
                        break
                    time.sleep(1)
                    remaining -= 1

                # Останавливаем зону и очищаем активность
                self._stop_zone(zone_id)
                self.active_zones.pop(zone_id, None)

                # Если отмена — пропускаем оставшиеся зоны этой группы, но не мешаем другим группам
                cancel_event = self.group_cancel_events.get(group_id)
                if cancel_event and cancel_event.is_set():
                    logger.info(f"Программа {program_id}: отменена для группы {group_id}, продолжаем с другими группами (если есть)")
                    continue

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

            # Предварительно рассчитанные плановые старты зон в рамках программы (на каждый день одинаковый порядок)
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
                # Записываем плановые старты для всех затронутых зон (без привязки к группе)
                # Программы могут включать зоны из разных групп — пишем напрямую по zone_id
                for zid, ts in schedule_map.items():
                    self.db.update_zone(zid, {'scheduled_start_time': ts})
            except Exception as e:
                logger.error(f"Ошибка расчета плановых стартов для программы {program_id}: {e}")

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

    def schedule_zone_stop(self, zone_id: int, duration_minutes: int):
        """Запланировать автоматическую остановку зоны через duration_minutes минут (для ручных запусков)."""
        try:
            if duration_minutes is None:
                return
            run_at = datetime.now() + timedelta(minutes=int(duration_minutes))
            self.scheduler.add_job(
                self._stop_zone,
                DateTrigger(run_date=run_at),
                args=[zone_id],
                id=f"zone_stop_{zone_id}_{int(run_at.timestamp())}",
                replace_existing=False,
                misfire_grace_time=120,
            )
            self.active_zones[zone_id] = run_at
            logger.info(f"Автоостановка зоны {zone_id} запланирована на {run_at}")
        except Exception as e:
            logger.error(f"Ошибка планирования автоостановки зоны {zone_id}: {e}")

    # ===== Ручной последовательный запуск всех зон в группе =====
    def start_group_sequence(self, group_id: int):
        """Остановить все зоны группы и запустить последовательный полив всех зон по порядку."""
        try:
            zones = self.db.get_zones()
            group_zones = sorted([z for z in zones if z['group_id'] == group_id], key=lambda x: x['id'])
            if not group_zones:
                logger.info(f"Группа {group_id}: нет зон для последовательного запуска")
                return False

            # Останавливаем все зоны в группе перед запуском
            for z in group_zones:
                self.db.update_zone(z['id'], {'state': 'off', 'watering_start_time': None})

            # Считаем и записываем плановые времена стартов для зон группы
            try:
                start_base = datetime.now()
                cumulative = 0
                schedule_map: Dict[int, str] = {}
                for z in group_zones:
                    start_dt = start_base + timedelta(minutes=cumulative)
                    schedule_map[z['id']] = start_dt.strftime('%Y-%m-%d %H:%M:%S')
                    cumulative += int(z.get('duration') or 0)
                # Очистим предыдущие плановые старты и запишем новые
                self.db.clear_group_scheduled_starts(group_id)
                self.db.set_group_scheduled_starts(group_id, schedule_map)
            except Exception as e:
                logger.error(f"Ошибка расчета плановых стартов для группы {group_id}: {e}")

            # Готовим флаг отмены для этой группы
            cancel_event = threading.Event()
            self.group_cancel_events[group_id] = cancel_event

            zone_ids = [z['id'] for z in group_zones]
            # Запускаем последовательность в отдельном джобе прямо сейчас
            self.scheduler.add_job(
                self._run_group_sequence,
                DateTrigger(run_date=datetime.now()),
                args=[group_id, zone_ids],
                id=f"group_seq_{group_id}_{int(datetime.now().timestamp())}",
                replace_existing=False,
                misfire_grace_time=120,
            )
            logger.info(f"Группа {group_id}: последовательный полив запущен для зон {zone_ids}")
            return True
        except Exception as e:
            logger.error(f"Ошибка старта последовательного полива для группы {group_id}: {e}")
            return False

    def _run_group_sequence(self, group_id: int, zone_ids: List[int]):
        """Выполняет последовательный полив зон группы. Выполняется в пуле потоков APScheduler."""
        try:
            cancel_event = self.group_cancel_events.get(group_id)
            for zone_id in zone_ids:
                if cancel_event and cancel_event.is_set():
                    logger.info(f"Группа {group_id}: последовательный полив отменен перед запуском зоны {zone_id}")
                    break
                zone = self.db.get_zone(zone_id)
                if not zone:
                    logger.warning(f"Группа {group_id}: зона {zone_id} не найдена, пропуск")
                    continue

                duration = int(zone.get('duration') or 0)
                if duration <= 0:
                    logger.info(f"Группа {group_id}: зона {zone_id} имеет нулевую длительность, пропуск")
                    continue

                # Старт текущей зоны
                start_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.db.update_zone(zone_id, {'state': 'on', 'watering_start_time': start_ts})
                try:
                    self.db.add_log('group_seq_zone_start', json.dumps({
                        'group_id': group_id,
                        'zone_id': zone_id,
                        'zone_name': zone.get('name'),
                        'duration': duration
                    }))
                except Exception:
                    pass

                # Ждем окончание полива зоны, проверяя флаг отмены каждую секунду
                remaining = duration * 60
                while remaining > 0:
                    if cancel_event and cancel_event.is_set():
                        logger.info(f"Группа {group_id}: получена отмена, досрочно останавливаем зону {zone_id}")
                        break
                    time.sleep(1)
                    remaining -= 1
                # Останавливаем зону (независимо от причины выхода)
                self._stop_zone(zone_id)
                # Если отменено — выходим из последовательности
                if cancel_event and cancel_event.is_set():
                    break

            # По завершении очищаем плановые старты группы
            try:
                # Перестраиваем расписание группы на ближайшее будущее
                self.db.reschedule_group_to_next_program(group_id)
            except Exception:
                pass

            try:
                self.db.add_log('group_seq_complete', json.dumps({'group_id': group_id, 'zones': zone_ids}))
            except Exception:
                pass
            logger.info(f"Группа {group_id}: последовательный полив завершен")
        except Exception as e:
            logger.error(f"Ошибка выполнения последовательного полива группы {group_id}: {e}")
        finally:
            # Снимаем флаг отмены и очищаем событие
            try:
                ev = self.group_cancel_events.get(group_id)
                if ev:
                    ev.clear()
                # Опционально удаляем, чтобы не копилось
                self.group_cancel_events.pop(group_id, None)
            except Exception:
                pass

    def get_active_programs(self) -> Dict[int, Dict[str, Any]]:
        # Возвращаем список запланированных программ и их job_ids
        return {pid: {'job_ids': jobs} for pid, jobs in self.program_jobs.items()}

    def get_active_zones(self) -> Dict[int, datetime]:
        return self.active_zones.copy()
    
    def cancel_group_jobs(self, group_id: int):
        """Отменяет все активные задачи планировщика для указанной группы"""
        try:
            # Получаем все зоны группы
            zones = self.db.get_zones()
            group_zones = [z for z in zones if z['group_id'] == group_id]
            
            # Отменяем задачи остановки зон
            for zone in group_zones:
                zone_id = zone['id']
                # Удаляем задачи остановки зон
                job_ids_to_remove = []
                for job in self.scheduler.get_jobs():
                    if job.id.startswith(f"zone_stop_{zone_id}_"):
                        job_ids_to_remove.append(job.id)
                
                for job_id in job_ids_to_remove:
                    try:
                        self.scheduler.remove_job(job_id)
                    except Exception:
                        pass
                
                # Удаляем из активных зон
                self.active_zones.pop(zone_id, None)
            
            # Отменяем задачи последовательного полива группы
            job_ids_to_remove = []
            for job in self.scheduler.get_jobs():
                if job.id.startswith(f"group_seq_{group_id}_"):
                    job_ids_to_remove.append(job.id)
            
            for job_id in job_ids_to_remove:
                try:
                    self.scheduler.remove_job(job_id)
                except Exception:
                    pass

            # Ставим флаг отмены для уже запущенной последовательности
            if group_id in self.group_cancel_events:
                self.group_cancel_events[group_id].set()

            # Перестраиваем расписание группы на ближайшую программу
            try:
                self.db.reschedule_group_to_next_program(group_id)
            except Exception:
                pass
            
            logger.info(f"Отменены все задачи планировщика для группы {group_id}")
        except Exception as e:
            logger.error(f"Ошибка отмены задач группы {group_id}: {e}")

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
