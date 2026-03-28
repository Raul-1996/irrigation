"""Facade for the irrigation database — backward-compatible wrapper.

All business logic lives in db/ submodules.  This file exposes the original
IrrigationDB class with the same public API so that every existing call like
``db.get_zones()`` or ``db.create_program(...)`` keeps working unchanged.
"""

import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from db.zones import ZoneRepository
from db.programs import ProgramRepository
from db.groups import GroupRepository
from db.mqtt import MqttRepository
from db.settings import SettingsRepository
from db.telegram import TelegramRepository
from db.logs import LogRepository
from db.migrations import MigrationRunner

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
try:
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler):
            h.setFormatter(fmt)
except (TypeError, ValueError, AttributeError) as _fmt_err:
    logger.debug("log formatter setup: %s", _fmt_err)
# В тестах отключаем распространение в root, чтобы не писать в закрытый stdout из фоновых ��отоков
logger.propagate = False


class IrrigationDB:
    """Backward-compatible facade over decomposed db/ repositories."""

    def __init__(self, db_path: str = 'irrigation.db'):
        self.db_path = db_path
        self.backup_dir = 'backups'

        # Repositories
        self.zones = ZoneRepository(db_path)
        self.programs = ProgramRepository(db_path)
        self.groups = GroupRepository(db_path)
        self.mqtt = MqttRepository(db_path)
        self.settings = SettingsRepository(db_path)
        self.telegram = TelegramRepository(db_path)
        self.logs = LogRepository(db_path, self.backup_dir)

        # Init schema + migrations
        self._migrations = MigrationRunner(db_path)
        self.init_database()

    def init_database(self):
        """Initialize database schema and run all migrations."""
        self._migrations.init_database()

    # =====================================================================
    # Proxy methods — backward compatibility
    # =====================================================================

    # --- Zones ---
    def get_zones(self, **kw) -> List[Dict[str, Any]]:
        return self.zones.get_zones(**kw)

    def get_zone(self, zone_id: int) -> Optional[Dict[str, Any]]:
        return self.zones.get_zone(zone_id)

    def create_zone(self, zone_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self.zones.create_zone(zone_data)

    def update_zone(self, zone_id: int, zone_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self.zones.update_zone(zone_id, zone_data)

    def update_zone_versioned(self, zone_id: int, updates: Dict[str, Any]) -> bool:
        return self.zones.update_zone_versioned(zone_id, updates)

    def bulk_update_zones(self, updates: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.zones.bulk_update_zones(updates)

    def bulk_upsert_zones(self, zones: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.zones.bulk_upsert_zones(zones)

    def delete_zone(self, zone_id: int) -> bool:
        return self.zones.delete_zone(zone_id)

    def get_zones_by_group(self, group_id: int) -> List[Dict[str, Any]]:
        return self.zones.get_zones_by_group(group_id)

    def clear_group_scheduled_starts(self, group_id: int) -> None:
        return self.zones.clear_group_scheduled_starts(group_id)

    def set_group_scheduled_starts(self, group_id: int, schedule: Dict[int, str]) -> None:
        return self.zones.set_group_scheduled_starts(group_id, schedule)

    def clear_scheduled_for_zone_group_peers(self, zone_id: int, group_id: int) -> None:
        return self.zones.clear_scheduled_for_zone_group_peers(zone_id, group_id)

    def update_zone_postpone(self, zone_id: int, postpone_until: str = None, reason: str = None) -> bool:
        return self.zones.update_zone_postpone(zone_id, postpone_until, reason)

    def update_zone_photo(self, zone_id: int, photo_path: Optional[str]) -> bool:
        return self.zones.update_zone_photo(zone_id, photo_path)

    def get_zone_duration(self, zone_id: int) -> int:
        return self.zones.get_zone_duration(zone_id)

    def create_zone_run(self, zone_id, group_id, start_utc, start_monotonic,
                        start_raw_pulses, pulse_liters_at_start, base_m3_at_start=None):
        return self.zones.create_zone_run(zone_id, group_id, start_utc, start_monotonic,
                                          start_raw_pulses, pulse_liters_at_start, base_m3_at_start)

    def get_open_zone_run(self, zone_id: int):
        return self.zones.get_open_zone_run(zone_id)

    def finish_zone_run(self, run_id, end_utc, end_monotonic, end_raw_pulses, total_liters, avg_flow_lpm, status='ok'):
        return self.zones.finish_zone_run(run_id, end_utc, end_monotonic, end_raw_pulses, total_liters, avg_flow_lpm, status)

    def compute_next_run_for_zone(self, zone_id: int) -> Optional[str]:
        return self.zones.compute_next_run_for_zone(zone_id, programs_getter=self.programs.get_programs)

    def reschedule_group_to_next_program(self, group_id: int) -> None:
        return self.zones.reschedule_group_to_next_program(group_id, programs_getter=self.programs.get_programs)

    # --- Programs ---
    def get_programs(self) -> List[Dict[str, Any]]:
        return self.programs.get_programs()

    def get_program(self, program_id: int) -> Optional[Dict[str, Any]]:
        return self.programs.get_program(program_id)

    def create_program(self, program_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self.programs.create_program(program_data)

    def update_program(self, program_id: int, program_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self.programs.update_program(program_id, program_data)

    def delete_program(self, program_id: int) -> bool:
        return self.programs.delete_program(program_id)

    def check_program_conflicts(self, program_id=None, time=None, zones=None, days=None):
        return self.programs.check_program_conflicts(program_id, time, zones, days)

    def cancel_program_run_for_group(self, program_id: int, run_date: str, group_id: int) -> bool:
        return self.programs.cancel_program_run_for_group(program_id, run_date, group_id)

    def is_program_run_cancelled_for_group(self, program_id: int, run_date: str, group_id: int) -> bool:
        return self.programs.is_program_run_cancelled_for_group(program_id, run_date, group_id)

    def clear_program_cancellations_for_group_on_date(self, group_id: int, run_date: str) -> bool:
        return self.programs.clear_program_cancellations_for_group_on_date(group_id, run_date)

    # --- Groups ---
    def get_groups(self) -> List[Dict[str, Any]]:
        return self.groups.get_groups()

    def create_group(self, name: str) -> Optional[Dict[str, Any]]:
        return self.groups.create_group(name)

    def delete_group(self, group_id: int) -> bool:
        return self.groups.delete_group(group_id)

    def update_group(self, group_id: int, name: str) -> bool:
        return self.groups.update_group(group_id, name)

    def update_group_fields(self, group_id: int, updates: Dict[str, Any]) -> bool:
        return self.groups.update_group_fields(group_id, updates)

    def get_group_use_rain(self, group_id: int) -> bool:
        return self.groups.get_group_use_rain(group_id)

    def set_group_use_rain(self, group_id: int, enabled: bool) -> bool:
        return self.groups.set_group_use_rain(group_id, enabled)

    def list_groups_min(self) -> List[Dict[str, Any]]:
        return self.groups.list_groups_min()

    def list_zones_by_group_min(self, group_id: int) -> List[Dict[str, Any]]:
        return self.groups.list_zones_by_group_min(group_id)

    # --- MQTT ---
    def get_mqtt_servers(self) -> List[Dict[str, Any]]:
        return self.mqtt.get_mqtt_servers()

    def get_mqtt_server(self, server_id: int) -> Optional[Dict[str, Any]]:
        return self.mqtt.get_mqtt_server(server_id)

    def create_mqtt_server(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self.mqtt.create_mqtt_server(data)

    def update_mqtt_server(self, server_id: int, data: Dict[str, Any]) -> bool:
        return self.mqtt.update_mqtt_server(server_id, data)

    def delete_mqtt_server(self, server_id: int) -> bool:
        return self.mqtt.delete_mqtt_server(server_id)

    @staticmethod
    def _decrypt_mqtt_password(server: Dict[str, Any]) -> Dict[str, Any]:
        return MqttRepository._decrypt_mqtt_password(server)

    # --- Settings ---
    def get_setting_value(self, key: str) -> Optional[str]:
        return self.settings.get_setting_value(key)

    def set_setting_value(self, key: str, value: Optional[str]) -> bool:
        return self.settings.set_setting_value(key, value)

    def ensure_password_change_required(self) -> None:
        return self.settings.ensure_password_change_required()

    def get_logging_debug(self) -> bool:
        return self.settings.get_logging_debug()

    def set_logging_debug(self, enabled: bool) -> bool:
        return self.settings.set_logging_debug(enabled)

    def get_rain_config(self) -> Dict[str, Any]:
        return self.settings.get_rain_config()

    def set_rain_config(self, cfg: Dict[str, Any]) -> bool:
        return self.settings.set_rain_config(cfg)

    def get_master_config(self) -> Dict[str, Any]:
        return self.settings.get_master_config()

    def set_master_config(self, cfg: Dict[str, Any]) -> bool:
        return self.settings.set_master_config(cfg)

    def get_env_config(self) -> Dict[str, Any]:
        return self.settings.get_env_config()

    def set_env_config(self, cfg: Dict[str, Any]) -> bool:
        return self.settings.set_env_config(cfg)

    def get_password_hash(self) -> Optional[str]:
        return self.settings.get_password_hash()

    def set_password(self, new_password: str) -> bool:
        return self.settings.set_password(new_password)

    def get_early_off_seconds(self) -> int:
        return self.settings.get_early_off_seconds()

    def set_early_off_seconds(self, seconds: int) -> bool:
        return self.settings.set_early_off_seconds(seconds)

    # --- Telegram ---
    def get_bot_user_by_chat(self, chat_id: int):
        return self.telegram.get_bot_user_by_chat(chat_id)

    def upsert_bot_user(self, chat_id, username, first_name):
        return self.telegram.upsert_bot_user(chat_id, username, first_name)

    def set_bot_user_authorized(self, chat_id, role='user'):
        return self.telegram.set_bot_user_authorized(chat_id, role)

    def inc_bot_user_failed(self, chat_id):
        return self.telegram.inc_bot_user_failed(chat_id)

    def lock_bot_user_until(self, chat_id, until_iso):
        return self.telegram.lock_bot_user_until(chat_id, until_iso)

    def set_bot_fsm(self, chat_id, state, data):
        return self.telegram.set_bot_fsm(chat_id, state, data)

    def get_bot_fsm(self, chat_id):
        return self.telegram.get_bot_fsm(chat_id)

    def is_new_idempotency_token(self, token, chat_id, action, ttl_seconds=600):
        return self.telegram.is_new_idempotency_token(token, chat_id, action, ttl_seconds)

    def get_bot_user_notif_settings(self, chat_id):
        return self.telegram.get_bot_user_notif_settings(chat_id)

    def set_bot_user_notif_toggle(self, chat_id, key, enabled):
        return self.telegram.set_bot_user_notif_toggle(chat_id, key, enabled)

    def get_due_bot_subscriptions(self, now_local):
        return self.telegram.get_due_bot_subscriptions(now_local)

    def create_or_update_subscription(self, user_id, sub_type, fmt, time_local, dow_mask, enabled=True):
        return self.telegram.create_or_update_subscription(user_id, sub_type, fmt, time_local, dow_mask, enabled)

    # --- Logs ---
    def get_logs(self, event_type=None, from_date=None, to_date=None):
        return self.logs.get_logs(event_type, from_date, to_date)

    def add_log(self, log_type, details=None):
        return self.logs.add_log(log_type, details)

    def get_water_usage(self, days=7, zone_id=None):
        return self.logs.get_water_usage(days, zone_id)

    def add_water_usage(self, zone_id, liters):
        return self.logs.add_water_usage(zone_id, liters)

    def get_water_statistics(self, days=30):
        return self.logs.get_water_statistics(days)

    def create_backup(self):
        return self.logs.create_backup()


# Глобальный экземпляр базы данных
db = IrrigationDB()
