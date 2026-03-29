"""Coverage boost: unit tests for services and core modules."""
import json
import sqlite3
import time
import threading
import pytest
from unittest.mock import patch, MagicMock
from database import IrrigationDB


class TestDatabaseFacade:
    """Tests for database.py facade methods."""

    def test_get_zones_empty(self, test_db):
        assert test_db.get_zones() == []

    def test_get_zone_nonexistent(self, test_db):
        assert test_db.get_zone(99999) is None

    def test_get_groups(self, test_db):
        groups = test_db.get_groups()
        assert isinstance(groups, list)

    def test_get_programs_empty(self, test_db):
        assert test_db.get_programs() == []

    def test_get_mqtt_servers_empty(self, test_db):
        servers = test_db.get_mqtt_servers()
        assert isinstance(servers, list)

    def test_get_mqtt_server_nonexistent(self, test_db):
        assert test_db.get_mqtt_server(99999) is None

    def test_get_setting_value(self, test_db):
        test_db.set_setting_value('test_key', 'test_value')
        assert test_db.get_setting_value('test_key') == 'test_value'

    def test_get_setting_value_missing(self, test_db):
        val = test_db.get_setting_value('nonexistent_key')
        assert val is None

    def test_add_log(self, test_db):
        test_db.add_log('test_type', 'test details')
        logs = test_db.get_logs()
        assert len(logs) >= 1

    def test_get_logs_with_type(self, test_db):
        test_db.add_log('specific_type', 'details')
        logs = test_db.get_logs(event_type='specific_type')
        assert isinstance(logs, list)

    def test_get_early_off_seconds(self, test_db):
        val = test_db.get_early_off_seconds()
        assert isinstance(val, (int, float))

    def test_get_rain_config(self, test_db):
        cfg = test_db.get_rain_config()
        assert isinstance(cfg, dict)

    def test_get_env_config(self, test_db):
        cfg = test_db.get_env_config()
        assert isinstance(cfg, dict)

    def test_reschedule_group_to_next_program(self, test_db):
        test_db.reschedule_group_to_next_program(1)  # Should not crash


class TestSchedulerModule:
    """Tests for irrigation_scheduler module-level functions."""

    def test_get_scheduler_before_init(self):
        from irrigation_scheduler import get_scheduler
        # May return None or an instance depending on test order
        result = get_scheduler()
        assert result is None or hasattr(result, 'start')

    def test_irrigation_scheduler_init(self, test_db):
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        assert sched.db == test_db
        assert not sched.is_running
        assert not sched._shutdown_event.is_set()

    def test_parse_dt(self, test_db):
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        assert sched._parse_dt('2024-01-01 12:00:00') is not None
        assert sched._parse_dt('2024-01-01 12:00') is not None
        assert sched._parse_dt(None) is None
        assert sched._parse_dt('invalid') is None
        assert sched._parse_dt('') is None

    def test_get_active_zones(self, test_db):
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        assert sched.get_active_zones() == {}

    def test_get_active_programs(self, test_db):
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        assert sched.get_active_programs() == {}

    def test_schedule_master_valve_cap(self, test_db):
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        sched.start()
        try:
            sched.schedule_master_valve_cap(1, hours=1)
            sched.cancel_master_valve_cap(1)
        finally:
            sched.stop()

    def test_cancel_group_jobs(self, test_db):
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        sched.start()
        try:
            sched.cancel_group_jobs(1)
        finally:
            sched.stop()

    def test_cleanup_jobs_on_boot(self, test_db):
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        sched.start()
        try:
            sched.cleanup_jobs_on_boot()
        finally:
            sched.stop()

    def test_stop_on_boot_active_zones(self, test_db):
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        sched.start()
        try:
            sched.stop_on_boot_active_zones()
        finally:
            sched.stop()

    def test_recover_missed_runs(self, test_db):
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        sched.start()
        try:
            sched.recover_missed_runs()
        finally:
            sched.stop()

    def test_check_weather_skip_disabled(self, test_db):
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        result = sched._check_weather_skip(1, 1)
        assert result.get('skip') is False

    def test_get_weather_adjusted_duration_disabled(self, test_db):
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        assert sched._get_weather_adjusted_duration(1, 10) == 10

    def test_start_group_sequence_no_zones(self, test_db):
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        sched.start()
        try:
            result = sched.start_group_sequence(999)
            assert result is False
        finally:
            sched.stop()

    def test_start_group_sequence_with_zones(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 1, 'group_id': 1})
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        sched.start()
        try:
            result = sched.start_group_sequence(1)
            assert result is True
        finally:
            sched.stop()


class TestEventsModule:
    """Tests for services/events.py."""

    def test_publish_subscribe(self):
        from services import events
        received = []
        events.subscribe(lambda ev: received.append(ev))
        events.publish({'type': 'test', 'id': 'cov1'})
        assert len(received) >= 1


class TestHelpersModule:
    """Tests for services/helpers.py."""

    def test_api_error(self, app):
        with app.app_context():
            from services.helpers import api_error
            resp = api_error('TEST_ERR', 'test error message', 400)
            assert resp is not None

    def test_api_soft(self, app):
        with app.app_context():
            from services.helpers import api_soft
            resp = api_soft('SOFT_ERR', 'test warning')
            assert resp is not None


class TestLocksModule:
    """Tests for services/locks.py."""

    def test_snapshot_all_locks(self):
        from services.locks import snapshot_all_locks
        result = snapshot_all_locks()
        assert isinstance(result, dict)


class TestSecurityModule:
    """Tests for services/security.py."""

    def test_admin_required_import(self):
        from services.security import admin_required
        assert callable(admin_required)


class TestRateLimiter:
    """Tests for services/rate_limiter.py."""

    def test_rate_limiter(self):
        from services.rate_limiter import LoginRateLimiter
        rl = LoginRateLimiter()
        # Should not be locked initially
        ok, _ = rl.check('192.168.99.99')
        assert ok is True


class TestConstants:
    """Tests for constants.py."""

    def test_mqtt_cache_ttl(self):
        from constants import MQTT_CACHE_TTL_SEC
        assert isinstance(MQTT_CACHE_TTL_SEC, (int, float))
