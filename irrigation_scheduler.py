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
from utils import normalize_topic
import json
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
import os
try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None
try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None

# Настройка логирования: по умолчанию INFO; можно задать через env SCHEDULER_LOG_LEVEL=WARNING
level_name = os.getenv('SCHEDULER_LOG_LEVEL', 'INFO').upper()
level = getattr(logging, level_name, logging.INFO)
logging.basicConfig(level=level)
logger = logging.getLogger(__name__)
try:
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler):
            h.setFormatter(fmt)
except Exception:
    pass
# Избегаем записи в stdout/stderr из потоков APScheduler при закрытии пайпов тест-раннером
logger.propagate = False
# Урезаем болтливость APScheduler, чтобы в тестах и проде не было лишних сообщений
try:
    aps_logger = logging.getLogger('apscheduler')
    aps_logger.setLevel(logging.ERROR)
except Exception:
    pass


class IrrigationScheduler:
    """Планировщик полива с последовательным запуском зон"""

    def __init__(self, db: IrrigationDB):
        self.db = db
        # Явно задаём таймзону для надёжности (иначе возможен UTC на некоторых системах)
        tz = None
        try:
            tzname = os.getenv('WB_TZ') or os.getenv('TZ')
            if not tzname:
                try:
                    with open('/etc/timezone', 'r') as f:
                        tzname = f.read().strip()
                except Exception:
                    tzname = None
            if ZoneInfo and tzname:
                tz = ZoneInfo(tzname)
        except Exception:
            tz = None
        self.scheduler = BackgroundScheduler(timezone=tz) if tz else BackgroundScheduler()
        self.active_zones: Dict[int, datetime] = {}
        self.program_jobs: Dict[int, List[str]] = {}  # program_id -> list(job_id)
        self.is_running = False
        self.group_cancel_events: Dict[int, threading.Event] = {}

    def start(self):
        if self.is_running:
            return
        self.scheduler.start()
        self.is_running = True
        try:
            logger.info("Планировщик полива (APScheduler) запущен, timezone=%s", str(getattr(self.scheduler, 'timezone', 'default')))
        except Exception:
            logger.info("Планировщик полива (APScheduler) запущен")
        # Плановый джоб: регулярная очистка истекших отложек
        try:
            self.schedule_postpone_sweeper()
        except Exception as e:
            logger.error(f"Не удалось запланировать очистку отложек: {e}")

    def stop(self):
        if not self.is_running:
            return
        self.scheduler.shutdown(wait=False)
        self.is_running = False
        logger.info("Планировщик полива остановлен")

    # --- Отложки: парсинг и фоновая очистка ---
    @staticmethod
    def _parse_dt(s: Optional[str]) -> Optional[datetime]:
        if not s:
            return None
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
        return None

    def clear_expired_postpones(self) -> None:
        """Сбрасывает отложенный полив для зон, у которых срок истек."""
        try:
            zones = self.db.get_zones()
            now = datetime.now()
            expired: List[int] = []
            for z in zones:
                pu = z.get('postpone_until')
                if not pu:
                    continue
                dt = self._parse_dt(pu)
                if dt is None or now >= dt:
                    expired.append(int(z['id']))
            for zone_id in expired:
                try:
                    self.db.update_zone_postpone(zone_id, None, None)
                    try:
                        self.db.add_log('postpone_expired', json.dumps({'zone': zone_id}))
                    except Exception:
                        pass
                except Exception as e:
                    logger.error(f"Не удалось сбросить отложку для зоны {zone_id}: {e}")
            if expired:
                logger.info(f"Сброшены истекшие отложки зон: {expired}")
        except Exception as e:
            logger.error(f"Ошибка очистки истекших отложек: {e}")

    def schedule_postpone_sweeper(self) -> None:
        """Планирует периодическую очистку истекших отложек (каждую минуту)."""
        try:
            # первая отработка — немедленно
            self.scheduler.add_job(
                self.clear_expired_postpones,
                trigger=IntervalTrigger(minutes=1),
                id='postpone_sweeper',
                replace_existing=True,
                coalesce=False,
                max_instances=1,
                next_run_time=datetime.now()
            )
        except Exception as e:
            logger.error(f"Не удалось добавить джоб postpone_sweeper: {e}")

    def _stop_zone(self, zone_id: int):
        try:
            # Фиксируем время последнего полива текущей зоны (время начала, если было)
            zone = self.db.get_zone(zone_id)
            last_time = None
            if zone and zone.get('watering_start_time'):
                last_time = zone['watering_start_time']
            # Централизованный OFF для автостопа
            try:
                from services.zone_control import stop_zone as _stop_zone_central
                _stop_zone_central(zone_id, reason='auto_stop')
            except Exception:
                # Fallback на локальный апдейт, если контроллер недоступен
                self.db.update_zone(zone_id, {
                    'state': 'off',
                    'watering_start_time': None,
                    'last_watering_time': last_time
                })
            try:
                from app import dlog
                dlog("auto-stop zone=%s", zone_id)
            except Exception:
                pass
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
            try:
                self.db.add_log('program_start', json.dumps({'program_id': program_id, 'program_name': program_name}))
            except Exception:
                pass
            for i, zone_id in enumerate(zones):
                zone = self.db.get_zone(zone_id)
                if not zone:
                    logger.warning(f"Зона {zone_id} не найдена")
                    continue

                # Если для группы зоны установлена отмена, пропускаем её
                group_id = int(zone.get('group_id') or 0)
                # Проверяем отмену текущего запуска программы для этой группы на сегодня
                try:
                    today = datetime.now().strftime('%Y-%m-%d')
                    from database import db as _db
                    if _db.is_program_run_cancelled_for_group(int(program_id), today, int(group_id)):
                        logger.info(f"Программа {program_id}: отменена для группы {group_id} на {today}, зона {zone_id} пропущена")
                        continue
                except Exception:
                    pass
                cancel_event = self.group_cancel_events.get(group_id)
                if cancel_event and cancel_event.is_set():
                    logger.info(f"Программа {program_id}: группа {group_id} отменена, зона {zone_id} пропущена")
                    continue

                # Проверяем отложенный полив
                postpone_until = zone.get('postpone_until')
                if postpone_until:
                    postpone_dt = self._parse_dt(postpone_until)
                    if postpone_dt is None or datetime.now() >= postpone_dt:
                        # истекло или непарсибельно — сбрасываем
                        self.db.update_zone_postpone(zone_id, None, None)
                    else:
                        logger.info(f"Зона {zone_id} отложена до {postpone_until}")
                        continue

                duration = int(zone['duration'])

                # БЕЗУСЛОВНО выключаем все зоны этой группы перед стартом текущей
                try:
                    all_zones = self.db.get_zones()
                    group_peers = [z for z in all_zones if z['group_id'] == group_id and int(z['id']) != int(zone_id)]
                    for gz in group_peers:
                        try:
                            topic = (gz.get('topic') or '').strip()
                            sid = gz.get('mqtt_server_id')
                            if mqtt and topic and sid:
                                t = normalize_topic(topic)
                                server = self.db.get_mqtt_server(int(sid))
                                if server:
                                    logger.debug(f"SCHED publish OFF peer zone={gz['id']} topic={t}")
                                    from app import _publish_mqtt_value as _pub
                                    _pub(server, t, '0', min_interval_sec=0.0)
                        except Exception:
                            pass
                        try:
                            self.db.update_zone(int(gz['id']), {'state': 'off', 'watering_start_time': None})
                        except Exception:
                            pass
                except Exception:
                    pass
                # Старт зоны: фиксируем время начала, чтобы таймер в UI работал
                try:
                    start_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    self.db.update_zone(zone_id, {'state': 'on', 'watering_start_time': start_ts, 'watering_start_source': 'schedule'})
                    # MQTT publish ON
                    try:
                        topic = (zone.get('topic') or '').strip()
                        sid = zone.get('mqtt_server_id')
                        if mqtt and topic and sid:
                            t = normalize_topic(topic)
                            server = self.db.get_mqtt_server(int(sid))
                            if server:
                                logger.debug(f"SCHED publish ON zone={zone_id} topic={t}")
                                from app import _publish_mqtt_value as _pub
                                _pub(server, t, '1', min_interval_sec=0.0)
                    except Exception:
                        pass
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

                # Ждем окончания текущей зоны с ранним выключением, проверяя отмену группы каждую секунду
                # Раннее выключение настраивается в settings (0..15 сек)
                try:
                    from database import db as _db
                    early = int(_db.get_early_off_seconds())
                except Exception:
                    early = 3
                early = 0 if early < 0 else (15 if early > 15 else early)
                total_seconds = duration * 60
                if os.getenv('TESTING') == '1':
                    total_seconds = min(6, max(1, duration))
                    early = 0  # в тестовом режиме не усложняем тайминги
                remaining = max(0, total_seconds - early)
                while remaining > 0:
                    cancel_event = self.group_cancel_events.get(group_id)
                    if cancel_event and cancel_event.is_set():
                        logger.info(f"Программа {program_id}: отмена группы {group_id}, досрочно останавливаем зону {zone_id}")
                        break
                    time.sleep(1)
                    remaining -= 1

                # Публикуем OFF и останавливаем зону
                try:
                    topic = (zone.get('topic') or '').strip()
                    sid = zone.get('mqtt_server_id')
                    if mqtt and topic and sid:
                        t = normalize_topic(topic)
                        server = self.db.get_mqtt_server(int(sid))
                        if server:
                            logger.debug(f"SCHED publish OFF zone={zone_id} topic={t}")
                            from app import _publish_mqtt_value as _pub
                            _pub(server, t, '0', min_interval_sec=0.0)
                except Exception:
                    pass
                self._stop_zone(zone_id)
                self.active_zones.pop(zone_id, None)

                # Дождёмся оставшиеся ранние секунды до «номинального» конца зоны, чтобы старт следующей был вовремя
                if early > 0:
                    waited = 0
                    while waited < early:
                        cancel_event = self.group_cancel_events.get(group_id)
                        if cancel_event and cancel_event.is_set():
                            break
                        time.sleep(1)
                        waited += 1

                # Если отмена — пропускаем оставшиеся зоны этой группы, но не мешаем другим группам
                cancel_event = self.group_cancel_events.get(group_id)
                if cancel_event and cancel_event.is_set():
                    logger.info(f"Программа {program_id}: отменена для группы {group_id}, продолжаем с другими группами (если есть)")
                    continue

            logger.info(f"Программа {program_id} ({program_name}) завершена")
            try:
                self.db.add_log('program_finish', json.dumps({'program_id': program_id, 'program_name': program_name}))
            except Exception:
                pass
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
                    misfire_grace_time=3600,
                    coalesce=False,
                    max_instances=1,
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
            # Раннее выключение: за N секунд до окончания (настраивается), по умолчанию 3
            try:
                from database import db as _db
                early = int(_db.get_early_off_seconds())
            except Exception:
                early = 3
            if early < 0:
                early = 0
            if early > 15:
                early = 15
            # В тестовом режиме ускоряем автостоп до секунд, чтобы не было «хвостов» после тестов
            if os.getenv('TESTING') == '1':
                total_seconds = min(6, max(1, int(duration_minutes)))
                early = 0
                run_at = datetime.now() + timedelta(seconds=total_seconds)
            else:
                run_at = datetime.now() + timedelta(minutes=int(duration_minutes)) - timedelta(seconds=early)
            # Гарантируем, что время в будущем (минимум +1 сек)
            now = datetime.now()
            if run_at <= now:
                run_at = now + timedelta(seconds=1)
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
                coalesce=False,
                max_instances=1,
            )
            try:
                from app import dlog
                dlog("group-seq start group=%s zones=%s", group_id, zone_ids)
            except Exception:
                pass
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
                # MQTT publish ON
                try:
                    topic = (zone.get('topic') or '').strip()
                    sid = zone.get('mqtt_server_id')
                    if mqtt and topic and sid:
                        t = normalize_topic(topic)
                        server = self.db.get_mqtt_server(int(sid))
                        if server:
                            from app import _publish_mqtt_value as _pub
                            _pub(server, t, '1', min_interval_sec=0.0)
                except Exception:
                    pass
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
                # Раннее выключение и выравнивание старта следующей зоны
                try:
                    from database import db as _db
                    early = int(_db.get_early_off_seconds())
                except Exception:
                    early = 3
                early = 0 if early < 0 else (15 if early > 15 else early)
                total_seconds = duration * 60
                if os.getenv('TESTING') == '1':
                    total_seconds = min(6, max(1, duration))
                    early = 0
                remaining = max(0, total_seconds - early)
                while remaining > 0:
                    if cancel_event and cancel_event.is_set():
                        try:
                            from app import dlog
                            dlog("group-seq cancel tick group=%s zone=%s remaining=%s", group_id, zone_id, remaining)
                        except Exception:
                            pass
                        logger.info(f"Группа {group_id}: получена отмена, досрочно останавливаем зону {zone_id}")
                        break
                    time.sleep(1)
                    remaining -= 1
                # Централизованный OFF и снятие активности
                try:
                    from services.zone_control import stop_zone as _stop_zone_central
                    _stop_zone_central(zone_id, reason='group_sequence')
                except Exception:
                    self._stop_zone(zone_id)
                self.active_zones.pop(zone_id, None)
                # Добираем ранние секунды, чтобы следующий старт был вовремя
                if early > 0 and not (cancel_event and cancel_event.is_set()):
                    time.sleep(early)
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

            # Ставим флаг отмены для уже запущенной последовательности (не очищаем здесь —
            # его очистит finally в _run_group_sequence, чтобы текущий поток гарантированно увидел отмену)
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

    def cancel_zone_jobs(self, zone_id: int):
        """Отменяет все задачи автоостановки для зоны и убирает её из active_zones."""
        try:
            job_ids_to_remove = []
            for job in self.scheduler.get_jobs():
                if job.id.startswith(f"zone_stop_{int(zone_id)}_"):
                    job_ids_to_remove.append(job.id)
            for job_id in job_ids_to_remove:
                try:
                    self.scheduler.remove_job(job_id)
                except Exception:
                    pass
            self.active_zones.pop(int(zone_id), None)
            logger.info(f"Отменены задачи автоостановки для зоны {zone_id}")
        except Exception as e:
            logger.error(f"Ошибка отмены задач зоны {zone_id}: {e}")

    def load_programs(self):
        try:
            programs = self.db.get_programs()
            for program in programs:
                self.schedule_program(program['id'], program)
            logger.info(f"Загружено {len(programs)} программ")
        except Exception as e:
            logger.error(f"Ошибка загрузки программ: {e}")

    def recover_missed_runs(self) -> None:
        """Догоняем пропущенный старт сегодняшней программы, если сервис перезапустился между стартом и окончанием."""
        try:
            programs = self.db.get_programs()
            now = datetime.now()
            zones_all = self.db.get_zones()
            zones_by_id = {int(z['id']): z for z in zones_all}
            for p in programs:
                try:
                    days = p.get('days') or []
                    if now.weekday() not in days:
                        continue
                    time_str = p.get('time') or '00:00'
                    hh, mm = map(int, time_str.split(':', 1))
                    start_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if now < start_dt:
                        continue
                    zones = sorted([int(z) for z in (p.get('zones') or [])])
                    if not zones:
                        continue
                    # Если какая-то зона уже включена — программа идёт
                    if any((zones_by_id.get(zid) or {}).get('state') == 'on' for zid in zones):
                        continue
                    durations = [int((zones_by_id.get(zid) or {}).get('duration') or 0) for zid in zones]
                    total_min = sum(durations)
                    if now >= start_dt + timedelta(minutes=total_min):
                        continue
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
                    if start_idx >= len(zones):
                        continue
                    # Запускаем остаток зон сейчас
                    self.scheduler.add_job(
                        self._run_program_threaded,
                        DateTrigger(run_date=datetime.now()),
                        args=[int(p['id']), zones[start_idx:], str(p.get('name') or f'program_{p.get("id")}') + ' (recovered)'],
                        id=f"program_{int(p['id'])}_recover_{int(time.time())}",
                        replace_existing=False,
                        misfire_grace_time=300,
                        coalesce=False,
                        max_instances=1,
                    )
                    logger.info(f"Recovery: программа {p['id']} — запущены оставшиеся зоны с индекса {start_idx}")
                except Exception as e:
                    logger.error(f"Ошибка recovery для программы {p.get('id')}: {e}")
        except Exception as e:
            logger.error(f"Ошибка recover_missed_runs: {e}")


# Глобальный экземпляр планировщика
scheduler: Optional[IrrigationScheduler] = None


def init_scheduler(db: IrrigationDB):
    global scheduler
    if scheduler is None:
        scheduler = IrrigationScheduler(db)
        scheduler.start()
        # Очистим истекшие отложки на старте
        try:
            scheduler.clear_expired_postpones()
        except Exception:
            pass
        scheduler.load_programs()
        # После загрузки программ попробуем сделать recovery пропущенных запусков
        try:
            # Локально объявим метод, чтобы не ломать существующие импорты, если его нет
            if hasattr(scheduler, 'recover_missed_runs'):
                scheduler.recover_missed_runs()
        except Exception:
            pass
    return scheduler


def get_scheduler() -> Optional[IrrigationScheduler]:
    return scheduler
