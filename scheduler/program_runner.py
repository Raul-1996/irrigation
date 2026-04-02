#!/usr/bin/env python3
"""
Program runner mixin: _run_program_threaded, weather checks, group sequence cancel.
"""
import sqlite3
import json
import logging
import os
from datetime import datetime, timedelta
from typing import List, Dict, Any

from utils import normalize_topic

try:
    import paho.mqtt.client as mqtt
except ImportError as e:
    logging.getLogger(__name__).debug("paho.mqtt not available: %s", e)
    mqtt = None

logger = logging.getLogger(__name__)


class WeatherMixin:
    """Mixin for weather-related checks on IrrigationScheduler."""

    def _check_weather_skip(self, zone_id: int, program_id: int = 0) -> dict:
        """Check if watering should be skipped due to weather. Returns skip info dict."""
        try:
            from services.weather_adjustment import get_weather_adjustment
            adj = get_weather_adjustment(self.db.db_path)
            if not adj.is_enabled():
                return {'skip': False}
            skip_info = adj.should_skip()
            if skip_info.get('skip'):
                reason = skip_info.get('reason', 'weather')
                logger.info(f"Weather skip: zone={zone_id} program={program_id} reason={reason}")
                try:
                    self.db.add_log('weather_skip', json.dumps({
                        'zone_id': zone_id, 'program_id': program_id,
                        'reason': reason, 'details': skip_info.get('details', {}),
                    }))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    logger.debug("Weather skip log error: %s", e)
                try:
                    from services.telegram_bot import notifier
                    chat_id = self.db.get_setting_value('telegram_admin_chat_id')
                    if chat_id:
                        skip_type = skip_info.get('details', {}).get('type', 'weather')
                        emoji = {'rain': '🌧', 'rain_forecast': '🌧', 'freeze': '❄️', 'wind': '💨'}.get(skip_type, '⛅')
                        notifier.send_text(int(chat_id), f"{emoji} Полив пропущен: {reason}")
                except (ImportError, OSError, ValueError, TypeError) as e:
                    logger.debug("Weather skip telegram: %s", e)
                try:
                    adj.log_adjustment(zone_id, 0, 0, 0, True, reason)
                except (sqlite3.Error, OSError) as e:
                    logger.debug("Weather log error: %s", e)
            return skip_info
        except (ImportError, OSError, ValueError, TypeError) as e:
            logger.debug("Weather check error: %s", e)
            return {'skip': False}

    def _get_weather_adjusted_duration(self, zone_id: int, base_duration: int) -> int:
        """Get weather-adjusted zone duration."""
        try:
            from services.weather_adjustment import get_weather_adjustment
            adj = get_weather_adjustment(self.db.db_path)
            if not adj.is_enabled():
                return base_duration
            coeff = adj.get_coefficient()
            adjusted = int(round(base_duration * coeff / 100.0))
            adjusted = max(1, adjusted) if adjusted > 0 else base_duration
            if adjusted != base_duration:
                logger.info(f"Weather adjustment: zone={zone_id} base={base_duration}min adjusted={adjusted}min (coeff={coeff}%)")
                try:
                    adj.log_adjustment(zone_id, base_duration, adjusted, coeff, False)
                except (sqlite3.Error, OSError) as e:
                    logger.debug("Weather log error: %s", e)
            return adjusted
        except (ImportError, OSError, ValueError, TypeError) as e:
            logger.debug("Weather adjustment error: %s", e)
            return base_duration


class ProgramRunnerMixin(WeatherMixin):
    """Mixin for program execution on IrrigationScheduler."""

    def _run_program_threaded(self, program_id: int, zones: List[int], program_name: str):
        """Последовательный запуск зон в отдельном потоке, чтобы не блокировать APScheduler"""
        try:
            logger.info(f"Запуск программы {program_id} ({program_name})")
            try:
                self.db.add_log('program_start', json.dumps({'program_id': program_id, 'program_name': program_name}))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                logger.debug("Handled exception in _run_program_threaded: %s", e)

            # Weather check before program
            skip_info = self._check_weather_skip(zones[0] if zones else 0, program_id)
            if skip_info.get('skip'):
                logger.info(f"Программа {program_id} ({program_name}) пропущена из-за погоды: {skip_info.get('reason')}")
                try:
                    self.db.add_log('program_weather_skip', json.dumps({
                        'program_id': program_id, 'program_name': program_name,
                        'reason': skip_info.get('reason', ''),
                    }))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    logger.debug("Program weather skip log error: %s", e)
                return

            for i, zone_id in enumerate(zones):
                zone = self.db.get_zone(zone_id)
                if not zone:
                    logger.warning(f"Зона {zone_id} не найдена")
                    continue

                group_id = int(zone.get('group_id') or 0)
                try:
                    today = datetime.now().strftime('%Y-%m-%d')
                    from database import db as _db
                    if _db.is_program_run_cancelled_for_group(int(program_id), today, int(group_id)):
                        logger.info(f"Программа {program_id}: отменена для группы {group_id} на {today}, зона {zone_id} пропущена")
                        continue
                except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                    logger.debug("Handled exception in _run_program_threaded: %s", e)
                cancel_event = self.group_cancel_events.get(group_id)
                if cancel_event and cancel_event.is_set():
                    logger.info(f"Программа {program_id}: группа {group_id} отменена, зона {zone_id} пропущена")
                    continue

                # Проверяем отложенный полив
                postpone_until = zone.get('postpone_until')
                if postpone_until:
                    postpone_dt = self._parse_dt(postpone_until)
                    if postpone_dt is None or datetime.now() >= postpone_dt:
                        self.db.update_zone_postpone(zone_id, None, None)
                    else:
                        logger.info(f"Зона {zone_id} отложена до {postpone_until}")
                        continue

                duration = self._get_weather_adjusted_duration(zone_id, int(zone['duration']))

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
                                    from services.mqtt_pub import publish_mqtt_value as _pub
                                    _pub(server, t, '0', min_interval_sec=0.0, qos=2, retain=True)
                        except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                            logger.debug("Handled exception in line_383: %s", e)
                        try:
                            self.db.update_zone(int(gz['id']), {'state': 'off', 'watering_start_time': None})
                        except (sqlite3.Error, OSError) as e:
                            logger.debug("Handled exception in line_387: %s", e)
                except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                    logger.debug("Handled exception in line_389: %s", e)
                # Старт зоны
                try:
                    start_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    okv = False
                    try:
                        okv = self.db.update_zone_versioned(zone_id, {'state': 'on', 'watering_start_time': start_ts, 'watering_start_source': 'schedule', 'commanded_state': 'on'})
                    except (sqlite3.Error, OSError) as e:
                        logger.debug("Exception in line_397: %s", e)
                        okv = False
                    if not okv:
                        self.db.update_zone(zone_id, {'state': 'on', 'watering_start_time': start_ts, 'watering_start_source': 'schedule', 'commanded_state': 'on'})
                    try:
                        from services.zone_control import exclusive_start_zone as _start_central
                        _start_central(int(zone_id))
                    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                        logger.debug("Handled exception in line_406: %s", e)
                    end_time = datetime.now() + timedelta(minutes=duration)
                    self.active_zones[zone_id] = end_time
                    try:
                        planned_str = end_time.strftime('%Y-%m-%d %H:%M:%S')
                        self.db.update_zone(zone_id, {'planned_end_time': planned_str})
                    except (sqlite3.Error, OSError) as e:
                        logger.debug("Handled exception in line_413: %s", e)
                    try:
                        self.schedule_zone_hard_stop(zone_id, end_time)
                    except (ValueError, KeyError, RuntimeError) as e:
                        logger.debug("Handled exception in line_418: %s", e)
                    self.db.add_log('zone_auto_start', json.dumps({
                        'zone_id': zone_id,
                        'zone_name': zone['name'],
                        'program_id': program_id,
                        'program_name': program_name,
                        'duration': duration,
                        'end_time': end_time.strftime('%Y-%m-%d %H:%M:%S')
                    }))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    logger.error(f"Ошибка запуска зоны {zone_id}: {e}")
                    continue

                # Ждем окончания текущей зоны
                try:
                    from database import db as _db
                    early = int(_db.get_early_off_seconds())
                except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                    logger.debug("Exception in line_437: %s", e)
                    early = 3
                early = 0 if early < 0 else (15 if early > 15 else early)
                total_seconds = duration * 60
                if os.getenv('TESTING') == '1':
                    total_seconds = min(6, max(1, duration))
                    early = 0
                remaining = max(0, total_seconds - early)
                while remaining > 0:
                    cancel_event = self.group_cancel_events.get(group_id)
                    if cancel_event and cancel_event.is_set():
                        logger.info(f"Программа {program_id}: отмена группы {group_id}, досрочно останавливаем зону {zone_id}")
                        break
                    if self._shutdown_event.wait(timeout=1):
                        logger.info(f"Программа {program_id}: shutdown, досрочно останавливаем зону {zone_id}")
                        break
                    remaining -= 1

                # Centralized stop
                try:
                    from services.zone_control import stop_zone as _stop_central
                    _stop_central(int(zone_id), reason='auto', force=False)
                except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                    logger.debug("Exception in line_458: %s", e)
                    self._stop_zone(zone_id)
                self.active_zones.pop(zone_id, None)

                # Дождёмся оставшиеся ранние секунды
                if early > 0:
                    waited = 0
                    while waited < early:
                        cancel_event = self.group_cancel_events.get(group_id)
                        if cancel_event and cancel_event.is_set():
                            break
                        if self._shutdown_event.wait(timeout=1):
                            break
                        waited += 1

                cancel_event = self.group_cancel_events.get(group_id)
                if cancel_event and cancel_event.is_set():
                    logger.info(f"Программа {program_id}: отменена для группы {group_id}, продолжаем с другими группами (если есть)")
                    continue

            logger.info(f"Программа {program_id} ({program_name}) завершена")
            try:
                self.db.add_log('program_finish', json.dumps({'program_id': program_id, 'program_name': program_name}))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                logger.debug("Handled exception in line_482: %s", e)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.error(f"Ошибка в выполнении программы {program_id}: {e}")

    def cancel_group_jobs(self, group_id: int):
        """Отменяет все активные задачи планировщика для указанной группы"""
        try:
            try:
                if group_id in self.group_cancel_events:
                    self.group_cancel_events[group_id].set()
            except (KeyError, TypeError, ValueError) as e:
                logger.debug("Handled exception in cancel_group_jobs: %s", e)

            try:
                from services.zone_control import stop_all_in_group as _stop_all
                _stop_all(int(group_id), reason='group_cancel', force=True)
            except (sqlite3.Error, OSError, ValueError, TypeError):
                logger.exception('cancel_group_jobs: stop_all_in_group failed')

            zones = self.db.get_zones()
            group_zones = [z for z in zones if z['group_id'] == group_id]

            for zone in group_zones:
                zone_id = zone['id']
                try:
                    self.cancel_zone_jobs(int(zone_id))
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Handled exception in cancel_group_jobs: %s", e)

            job_ids_to_remove = []
            for job in self.scheduler.get_jobs():
                jid = str(job.id)
                if jid.startswith(f"group_seq:{int(group_id)}:"):
                    job_ids_to_remove.append(job.id)

            for job_id in job_ids_to_remove:
                try:
                    self.scheduler.remove_job(job_id)
                except (ValueError, KeyError, RuntimeError) as e:
                    logger.debug("Handled exception in line_980: %s", e)

            try:
                self.db.reschedule_group_to_next_program(group_id)
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in line_986: %s", e)

            logger.info(f"Отменены все задачи планировщика для группы {group_id}")
        except (sqlite3.Error, OSError) as e:
            logger.error(f"Ошибка отмены задач группы {group_id}: {e}")

    def start_group_sequence(self, group_id: int, override_duration: int = None):
        """Остановить все зоны группы и запустить последовательный полив всех зон по порядку."""
        try:
            from scheduler.jobs import job_run_group_sequence
            import threading
            from apscheduler.triggers.date import DateTrigger

            zones = self.db.get_zones()
            group_zones = sorted([z for z in zones if z['group_id'] == group_id], key=lambda x: x['id'])
            if not group_zones:
                logger.info(f"Группа {group_id}: нет зон для последовательного запуска")
                return False

            for z in group_zones:
                self.db.update_zone(z['id'], {'state': 'off', 'watering_start_time': None})

            try:
                start_base = datetime.now()
                cumulative = 0
                schedule_map: Dict[int, str] = {}
                for z in group_zones:
                    start_dt = start_base + timedelta(minutes=cumulative)
                    schedule_map[z['id']] = start_dt.strftime('%Y-%m-%d %H:%M:%S')
                    cumulative += override_duration if override_duration else int(z.get('duration') or 0)
                self.db.clear_group_scheduled_starts(group_id)
                self.db.set_group_scheduled_starts(group_id, schedule_map)
            except (sqlite3.Error, OSError) as e:
                logger.error(f"Ошибка расчета плановых стартов для группы {group_id}: {e}")

            cancel_event = threading.Event()
            self.group_cancel_events[group_id] = cancel_event

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
                    except (ValueError, KeyError, RuntimeError) as e:
                        logger.debug("Handled exception in line_743: %s", e)
                for zid in zone_ids:
                    self.active_zones.pop(int(zid), None)
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Handled exception in line_747: %s", e)

            zone_ids = [z['id'] for z in group_zones]
            if os.environ.get('TESTING') == '1':
                self._run_group_sequence(group_id, zone_ids, override_duration=override_duration)
            else:
                _kwargs = dict(
                    args=[group_id, zone_ids, override_duration],
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
                logger.debug("group-seq start group=%s zones=%s", group_id, zone_ids)
            except (OSError, ValueError) as e:
                logger.debug("Handled exception in line_770: %s", e)
            logger.info(f"Группа {group_id}: последовательный полив запущен для зон {zone_ids}")
            return True

        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка старта последовательного полива для группы {group_id}: {e}")
            return False

    def _run_group_sequence(self, group_id: int, zone_ids: List[int], override_duration: int = None):
        """Выполняет последовательный полив зон группы."""
        if os.environ.get('TESTING') == '1':
            logger.debug("TESTING mode: simplified _run_group_sequence for group %s", group_id)
            for zone_id in zone_ids:
                zone = self.db.get_zone(zone_id)
                if not zone:
                    continue
                duration = override_duration if override_duration else int(zone.get('duration') or 0)
                if duration <= 0:
                    continue
                start_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                planned_end = (datetime.now() + timedelta(minutes=duration)).strftime('%Y-%m-%d %H:%M:%S')
                self.db.update_zone(zone_id, {'state': 'on', 'watering_start_time': start_ts, 'planned_end_time': planned_end})
                break
            return
        try:
            # Weather check before group sequence
            skip_info = self._check_weather_skip(zone_ids[0] if zone_ids else 0, 0)
            if skip_info.get('skip'):
                logger.info(f"Группа {group_id}: последовательный полив пропущен из-за погоды: {skip_info.get('reason')}")
                try:
                    self.db.add_log('group_weather_skip', json.dumps({
                        'group_id': group_id, 'reason': skip_info.get('reason', ''),
                    }))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    logger.debug("Group weather skip log error: %s", e)
                return

            cancel_event = self.group_cancel_events.get(group_id)
            for zone_id in zone_ids:
                if cancel_event and cancel_event.is_set():
                    logger.info(f"Группа {group_id}: последовательный полив отменен перед запуском зоны {zone_id}")
                    break
                zone = self.db.get_zone(zone_id)
                if not zone:
                    logger.warning(f"Группа {group_id}: зона {zone_id} не найдена, пропуск")
                    continue

                base_dur = override_duration if override_duration else int(zone.get('duration') or 0)
                duration = self._get_weather_adjusted_duration(zone_id, base_dur)
                if duration <= 0:
                    logger.info(f"Группа {group_id}: зона {zone_id} имеет нулевую длительность, пропуск")
                    continue

                # Старт текущей зоны
                start_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                try:
                    planned_end = (datetime.now() + timedelta(minutes=duration)).strftime('%Y-%m-%d %H:%M:%S')
                    self.db.update_zone(zone_id, {'state': 'on', 'watering_start_time': start_ts, 'planned_end_time': planned_end})
                except (sqlite3.Error, OSError) as e:
                    logger.debug("Exception in _run_group_sequence: %s", e)
                    self.db.update_zone(zone_id, {'state': 'on', 'watering_start_time': start_ts})
                try:
                    self.schedule_zone_hard_stop(int(zone_id), datetime.now() + timedelta(minutes=duration))
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Handled exception in _run_group_sequence: %s", e)
                # MQTT publish
                try:
                    try:
                        gid = int(zone.get('group_id') or 0)
                    except (ValueError, TypeError, KeyError) as e:
                        logger.debug("Exception in line_813: %s", e)
                        gid = 0
                    if gid:
                        try:
                            groups = self.db.get_groups() or []
                            g = next((gg for gg in groups if int(gg.get('id')) == gid), None)
                        except (sqlite3.Error, OSError) as e:
                            logger.debug("Exception in line_820: %s", e)
                            g = None
                        if g:
                            try:
                                use_mv = int(g.get('use_master_valve') or 0) == 1
                            except (ValueError, TypeError, KeyError) as e:
                                logger.debug("Exception in line_826: %s", e)
                                use_mv = False
                            if use_mv:
                                mtopic = (g.get('master_mqtt_topic') or '').strip()
                                msid = g.get('master_mqtt_server_id')
                                if mtopic and msid:
                                    mserver = self.db.get_mqtt_server(int(msid))
                                    if mserver:
                                        try:
                                            mode = (g.get('master_mode') or 'NC').strip().upper()
                                        except (ValueError, TypeError, KeyError) as e:
                                            logger.debug("Exception in line_837: %s", e)
                                            mode = 'NC'
                                        from services.mqtt_pub import publish_mqtt_value as _pub
                                        _pub(mserver, normalize_topic(mtopic), ('0' if mode == 'NO' else '1'), min_interval_sec=0.0, qos=2, retain=True)
                    # Publish zone ON
                    topic = (zone.get('topic') or '').strip()
                    sid = zone.get('mqtt_server_id')
                    if mqtt and topic and sid:
                        t = normalize_topic(topic)
                        server = self.db.get_mqtt_server(int(sid))
                        if server:
                            from services.mqtt_pub import publish_mqtt_value as _pub
                            _pub(server, t, '1', min_interval_sec=0.0, qos=2, retain=True)
                except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                    logger.debug("Handled exception in line_851: %s", e)
                try:
                    self.db.add_log('group_seq_zone_start', json.dumps({
                        'group_id': group_id,
                        'zone_id': zone_id,
                        'zone_name': zone.get('name'),
                        'duration': duration
                    }))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    logger.debug("Handled exception in line_860: %s", e)

                # Ждем окончание полива зоны
                try:
                    from database import db as _db
                    early = int(_db.get_early_off_seconds())
                except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                    logger.debug("Exception in line_868: %s", e)
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
                            logger.debug("group-seq cancel tick group=%s zone=%s remaining=%s", group_id, zone_id, remaining)
                        except (OSError, ValueError) as e:
                            logger.debug("Handled exception in line_882: %s", e)
                        logger.info(f"Группа {group_id}: получена отмена, досрочно останавливаем зону {zone_id}")
                        break
                    if self._shutdown_event.wait(timeout=1):
                        logger.info(f"Группа {group_id}: shutdown, досрочно останавливаем зону {zone_id}")
                        break
                    remaining -= 1
                # Централизованный OFF
                try:
                    from services.zone_control import stop_zone as _stop_zone_central
                    _stop_zone_central(zone_id, reason='group_sequence')
                except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                    logger.debug("Exception in line_892: %s", e)
                    self._stop_zone(zone_id)
                self.active_zones.pop(zone_id, None)
                try:
                    self.db.update_zone(zone_id, {'planned_end_time': None})
                except (sqlite3.Error, OSError) as e:
                    logger.debug("Handled exception in line_899: %s", e)
                if early > 0 and not (cancel_event and cancel_event.is_set()):
                    self._shutdown_event.wait(timeout=early)
                if cancel_event and cancel_event.is_set():
                    break

            # По завершении очищаем плановые старты группы
            try:
                self.db.reschedule_group_to_next_program(group_id)
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in line_912: %s", e)

            try:
                self.db.add_log('group_seq_complete', json.dumps({'group_id': group_id, 'zones': zone_ids}))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                logger.debug("Handled exception in line_917: %s", e)
            logger.info(f"Группа {group_id}: последовательный полив завершен")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.error(f"Ошибка выполнения последовательного полива группы {group_id}: {e}")
        finally:
            try:
                ev = self.group_cancel_events.get(group_id)
                if ev:
                    ev.clear()
                self.group_cancel_events.pop(group_id, None)
            except (KeyError, TypeError, ValueError) as e:
                logger.debug("Handled exception in line_930: %s", e)

    def recover_missed_runs(self) -> None:
        """Догоняем пропущенный старт сегодняшней программы."""
        try:
            import time
            from apscheduler.triggers.date import DateTrigger
            from scheduler.jobs import job_run_program

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
                    if any((zones_by_id.get(zid) or {}).get('state') == 'on' for zid in zones):
                        continue
                    durations = [int((zones_by_id.get(zid) or {}).get('duration') or 0) for zid in zones]
                    total_min = sum(durations)
                    if now >= start_dt + timedelta(minutes=total_min):
                        continue
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
                except (ValueError, TypeError, KeyError) as e:
                    logger.error(f"Ошибка recovery для программы {p.get('id')}: {e}")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка recover_missed_runs: {e}")

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
                except (ValueError, KeyError, RuntimeError) as e:
                    logger.debug("Handled exception in cleanup_jobs_on_boot: %s", e)
            logger.info(f"Boot cleanup: removed {len(job_ids_to_remove)} jobs")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Boot cleanup failed: {e}")

    def stop_on_boot_active_zones(self) -> None:
        try:
            zones = self.db.get_zones()
            for z in zones:
                st = str(z.get('state') or '').lower()
                if st in ('starting', 'on', 'stopping', 'paused'):
                    try:
                        from services.zone_control import stop_zone as _stop
                        _stop(int(z['id']), reason='recovery_boot', force=True)
                    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                        logger.debug("Handled exception in stop_on_boot_active_zones: %s", e)
            logger.info("Boot remediation: active zones forced to OFF")
        except (sqlite3.Error, OSError, ValueError, TypeError) as e:
            logger.error(f"Boot remediation failed: {e}")
