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
try:
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
except Exception:
    SQLAlchemyJobStore = None
try:
    from apscheduler.jobstores.memory import MemoryJobStore
except Exception:
    MemoryJobStore = None
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

# Настройка логирования: по умолчанию WARNING; можно поднять через env SCHEDULER_LOG_LEVEL=INFO/DEBUG
level_name = os.getenv('SCHEDULER_LOG_LEVEL', 'WARNING').upper()
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


# === Module-level job callables for APScheduler persistence ===
def job_run_program(program_id: int, zones: list, program_name: str):
    try:
        from irrigation_scheduler import get_scheduler
        s = get_scheduler()
        if s is not None:
            s._run_program_threaded(int(program_id), [int(z) for z in zones], str(program_name))
    except Exception:
        pass


def job_run_group_sequence(group_id: int, zone_ids: list):
    try:
        from irrigation_scheduler import get_scheduler
        s = get_scheduler()
        if s is not None:
            s._run_group_sequence(int(group_id), [int(z) for z in zone_ids])
    except Exception:
        pass


def job_stop_zone(zone_id: int):
    try:
        from irrigation_scheduler import get_scheduler
        s = get_scheduler()
        if s is not None:
            s._stop_zone(int(zone_id))
    except Exception:
        pass


def job_close_master_valve(group_id: int):
    """Закрыть мастер-клапан у группы, если он включён."""
    try:
        from database import db
        from services.mqtt_pub import publish_mqtt_value
        g = next((gg for gg in (db.get_groups() or []) if int(gg.get('id')) == int(group_id)), None)
        if not g:
            return
        if int(g.get('use_master_valve') or 0) != 1:
            return
        topic = (g.get('master_mqtt_topic') or '').strip()
        sid = g.get('master_mqtt_server_id')
        if not topic or not sid:
            return
        server = db.get_mqtt_server(int(sid))
        if not server:
            return
        try:
            mode = (g.get('master_mode') or 'NC').strip().upper()
        except Exception:
            mode = 'NC'
        close_val = '1' if mode == 'NO' else '0'
        publish_mqtt_value(server, normalize_topic(topic), close_val, min_interval_sec=0.0, retain=True, meta={'cmd':'master_cap_close'})
        logger.info(f"Master valve cap close: group {group_id}")
    except Exception as e:
        logger.error(f"Ошибка cap-закрытия мастер-клапана для группы {group_id}: {e}")


def job_clear_expired_postpones():
    try:
        from irrigation_scheduler import get_scheduler
        s = get_scheduler()
        if s is not None:
            s.clear_expired_postpones()
    except Exception:
        pass

def job_dispatch_bot_subscriptions():
    try:
        from database import db
        from services.reports import build_report_text
        from services.telegram_bot import notifier
        now = datetime.now()
        due = db.get_due_bot_subscriptions(now)
        for sub in due:
            try:
                fmt = str(sub.get('format') or 'brief')
                ptype = str(sub.get('type') or 'daily')
                period = 'today' if ptype == 'daily' else '7'
                txt = build_report_text(period=period, fmt='brief' if fmt!='full' else 'full')
                chat_id = int(sub.get('chat_id'))
                if chat_id:
                    notifier.send_text(chat_id, txt)
            except Exception:
                pass
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
        # Инициализация APScheduler с SQLAlchemyJobStore (для персистентности), если доступен
        scheduler_kwargs = {}
        try:
            jobstores = {}
            if SQLAlchemyJobStore is not None:
                jobstores['default'] = SQLAlchemyJobStore(url=f'sqlite:///{self.db.db_path}')
            if MemoryJobStore is not None:
                jobstores['volatile'] = MemoryJobStore()  # для эфемерных задач
            if jobstores:
                scheduler_kwargs['jobstores'] = jobstores
        except Exception:
            # Не критично: работаем без персистентности
            pass
        self.scheduler = BackgroundScheduler(timezone=tz, **scheduler_kwargs) if tz else BackgroundScheduler(**scheduler_kwargs)
        # Флаги доступности jobstore-ов
        try:
            stores = getattr(self.scheduler, '_jobstores', {}) or {}
            self.has_default_jobstore = 'default' in stores
            self.has_volatile_jobstore = 'volatile' in stores
        except Exception:
            self.has_default_jobstore = False
            self.has_volatile_jobstore = False
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
                job_clear_expired_postpones,
                trigger=IntervalTrigger(minutes=1),
                id='postpone_sweeper',
                replace_existing=True,
                coalesce=False,
                max_instances=1,
                next_run_time=datetime.now()
            )
        except Exception as e:
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
        except Exception as e:
            logger.error(f"Не удалось добавить джоб bot_sub_dispatcher: {e}")

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
                    okv = False
                    try:
                        okv = self.db.update_zone_versioned(zone_id, {'state': 'on', 'watering_start_time': start_ts, 'watering_start_source': 'schedule', 'commanded_state': 'on'})
                    except Exception:
                        okv = False
                    if not okv:
                        self.db.update_zone(zone_id, {'state': 'on', 'watering_start_time': start_ts, 'watering_start_source': 'schedule', 'commanded_state': 'on'})
                    # Centralized start to ensure MV logic
                    try:
                        from services.zone_control import exclusive_start_zone as _start_central
                        _start_central(int(zone_id))
                    except Exception:
                        pass
                    end_time = datetime.now() + timedelta(minutes=duration)
                    self.active_zones[zone_id] = end_time
                    # write planned_end_time for watchdogs/diagnostics
                    try:
                        self.db.update_zone(zone_id, {'planned_end_time': end_time.strftime('%Y-%m-%d %H:%M:%S')})
                    except Exception:
                        pass
                    # Watchdog job
                    try:
                        self.schedule_zone_hard_stop(zone_id, end_time)
                    except Exception:
                        pass
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

                # Centralized stop to ensure MV delayed close
                try:
                    from services.zone_control import stop_zone as _stop_central
                    _stop_central(int(zone_id), reason='auto', force=False)
                except Exception:
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
                _kwargs = dict(
                    args=[program_id, zones, program_data['name']],
                    id=f"program:{program_id}:d{day}",
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

    def schedule_zone_stop(self, zone_id: int, duration_minutes: int, command_id: Optional[str] = None):
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
            # Стандартизованный ID (используем command_id при наличии)
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
        except Exception as e:
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
        except Exception as e:
            logger.error(f"Ошибка планирования watchdog-стопа зоны {zone_id}: {e}")

    def schedule_zone_cap(self, zone_id: int, cap_minutes: int = 240):
        """Абсолютный лимит работы зоны: форс-стоп через cap_minutes от текущего момента."""
        try:
            run_at = datetime.now() + timedelta(minutes=int(cap_minutes))
            # Уникальный job id для капа
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
        except Exception as e:
            logger.error(f"Ошибка планирования cap-стопа зоны {zone_id}: {e}")

    def cancel_zone_cap(self, zone_id: int):
        try:
            job_id = f"zone_cap_stop:{int(zone_id)}"
            try:
                self.scheduler.remove_job(job_id)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Ошибка отмены cap-стопа зоны {zone_id}: {e}")

    def schedule_master_valve_cap(self, group_id: int, hours: int = 24):
        """Абсолютный лимит открытого мастер-клапана — закрыть через hours часов.
        Перепланируется при повторных вызовах.
        """
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
        except Exception as e:
            logger.error(f"Ошибка планирования cap-закрытия мастер-клапана для группы {group_id}: {e}")

    def cancel_master_valve_cap(self, group_id: int):
        try:
            job_id = f"master_cap_close:{int(group_id)}"
            try:
                self.scheduler.remove_job(job_id)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Ошибка отмены cap-закрытия мастер-клапана для группы {group_id}: {e}")

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

            # Очистим старые джобы, связанные с этой группой (group_seq, zone_stop, zone_hard_stop)
            try:
                zone_ids = [int(z['id']) for z in group_zones]
                to_remove = []
                for job in self.scheduler.get_jobs():
                    jid = str(job.id)
                    if jid.startswith(f"group_seq:{int(group_id)}:"):
                        to_remove.append(jid)
                        continue
                    for zid in zone_ids:
                        if jid.startswith(f"zone_stop:{int(zid)}:") or jid == f"zone_hard_stop:{int(zid)}":
                            to_remove.append(jid)
                            break
                for jid in to_remove:
                    try:
                        self.scheduler.remove_job(jid)
                    except Exception:
                        pass
                for zid in zone_ids:
                    self.active_zones.pop(int(zid), None)
            except Exception:
                pass

            zone_ids = [z['id'] for z in group_zones]
            # Запускаем последовательность в отдельном джобе прямо сейчас
            _kwargs = dict(
                args=[group_id, zone_ids],
                id=f"group_seq:{group_id}:{int(datetime.now().timestamp())}",
                replace_existing=False,
                misfire_grace_time=120,
                coalesce=False,
                max_instances=1,
            )
            if getattr(self, 'has_volatile_jobstore', False):
                _kwargs['jobstore'] = 'volatile'
            self.scheduler.add_job(
                job_run_group_sequence,
                DateTrigger(run_date=datetime.now()),
                **_kwargs,
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
                try:
                    planned_end = (datetime.now() + timedelta(minutes=duration)).strftime('%Y-%m-%d %H:%M:%S')
                    self.db.update_zone(zone_id, {'state': 'on', 'watering_start_time': start_ts, 'planned_end_time': planned_end})
                except Exception:
                    self.db.update_zone(zone_id, {'state': 'on', 'watering_start_time': start_ts})
                try:
                    self.schedule_zone_hard_stop(int(zone_id), datetime.now() + timedelta(minutes=duration))
                except Exception:
                    pass
                # MQTT publish: pre-open master valve for the group (idempotent), then zone ON
                try:
                    # Pre-open MV if configured for this zone's group
                    try:
                        gid = int(zone.get('group_id') or 0)
                    except Exception:
                        gid = 0
                    if gid:
                        try:
                            groups = self.db.get_groups() or []
                            g = next((gg for gg in groups if int(gg.get('id')) == gid), None)
                        except Exception:
                            g = None
                        if g:
                            try:
                                use_mv = int(g.get('use_master_valve') or 0) == 1
                            except Exception:
                                use_mv = False
                            if use_mv:
                                mtopic = (g.get('master_mqtt_topic') or '').strip()
                                msid = g.get('master_mqtt_server_id')
                                if mtopic and msid:
                                    mserver = self.db.get_mqtt_server(int(msid))
                                    if mserver:
                                        try:
                                            mode = (g.get('master_mode') or 'NC').strip().upper()
                                        except Exception:
                                            mode = 'NC'
                                        from app import _publish_mqtt_value as _pub
                                        _pub(mserver, normalize_topic(mtopic), ('0' if mode == 'NO' else '1'), min_interval_sec=0.0)
                    # Publish zone ON
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
                try:
                    # очищаем planned_end_time у завершенной зоны
                    self.db.update_zone(zone_id, {'planned_end_time': None})
                except Exception:
                    pass
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
            # Ставим флаг отмены немедленно
            try:
                if group_id in self.group_cancel_events:
                    self.group_cancel_events[group_id].set()
            except Exception:
                pass

            # Немедленный OFF всем зонам группы через централизованный контроллер
            try:
                from services.zone_control import stop_all_in_group as _stop_all
                _stop_all(int(group_id), reason='group_cancel', force=True)
            except Exception:
                logger.exception('cancel_group_jobs: stop_all_in_group failed')

            # Получаем все зоны группы
            zones = self.db.get_zones()
            group_zones = [z for z in zones if z['group_id'] == group_id]
            
            # Отменяем задачи остановки зон
            for zone in group_zones:
                zone_id = zone['id']
                try:
                    # Единообразно снимаем все job’ы зоны (включая hard_stop)
                    self.cancel_zone_jobs(int(zone_id))
                except Exception:
                    pass
            
            # Отменяем задачи последовательного полива группы
            job_ids_to_remove = []
            for job in self.scheduler.get_jobs():
                jid = str(job.id)
                if jid.startswith(f"group_seq:{int(group_id)}:"):
                    job_ids_to_remove.append(job.id)
            
            for job_id in job_ids_to_remove:
                try:
                    self.scheduler.remove_job(job_id)
                except Exception:
                    pass

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
                if job.id.startswith(f"zone_stop:{int(zone_id)}:"):
                    job_ids_to_remove.append(job.id)
                if job.id == f"zone_hard_stop:{int(zone_id)}":
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
                    _kwargs = dict(
                        args=[int(p['id']), zones[start_idx:], str(p.get('name') or f'program_{p.get("id")}') + ' (recovered)'],
                        id=f"program_{int(p['id'])}_recover_{int(time.time())}",
                        replace_existing=False,
                        misfire_grace_time=300,
                        coalesce=False,
                        max_instances=1,
                    )
                    if getattr(self, 'has_volatile_jobstore', False):
                        _kwargs['jobstore'] = 'volatile'
                    self.scheduler.add_job(
                        job_run_program,
                        DateTrigger(run_date=datetime.now()),
                        **_kwargs,
                    )
                    logger.info(f"Recovery: программа {p['id']} — запущены оставшиеся зоны с индекса {start_idx}")
                except Exception as e:
                    logger.error(f"Ошибка recovery для программы {p.get('id')}: {e}")
        except Exception as e:
            logger.error(f"Ошибка recover_missed_runs: {e}")

    # === Boot-time remediation ===
    def cleanup_jobs_on_boot(self) -> None:
        try:
            job_ids_to_remove = []
            for job in self.scheduler.get_jobs():
                jid = str(job.id)
                if jid.startswith('zone_stop:') or jid.startswith('group_seq:'):
                    job_ids_to_remove.append(jid)
            for jid in job_ids_to_remove:
                try:
                    self.scheduler.remove_job(jid)
                except Exception:
                    pass
            logger.info(f"Boot cleanup: removed {len(job_ids_to_remove)} jobs")
        except Exception as e:
            logger.error(f"Boot cleanup failed: {e}")

    def stop_on_boot_active_zones(self) -> None:
        try:
            zones = self.db.get_zones()
            for z in zones:
                st = str(z.get('state') or '').lower()
                if st in ('starting', 'on', 'stopping'):
                    try:
                        from services.zone_control import stop_zone as _stop
                        _stop(int(z['id']), reason='recovery_boot', force=True)
                    except Exception:
                        pass
            logger.info("Boot remediation: active zones forced to OFF")
        except Exception as e:
            logger.error(f"Boot remediation failed: {e}")


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
        # Boot-time cleanup and stop lingering active zones
        try:
            scheduler.cleanup_jobs_on_boot()
        except Exception:
            pass
        try:
            scheduler.stop_on_boot_active_zones()
        except Exception:
            pass
    return scheduler


def get_scheduler() -> Optional[IrrigationScheduler]:
    return scheduler
