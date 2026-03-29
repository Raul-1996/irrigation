"""Comprehensive tests for irrigation_scheduler.py — targeting 90%+ coverage."""
import pytest
import os
import time
import json
import threading
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, PropertyMock

os.environ['TESTING'] = '1'


@pytest.fixture
def scheduler_instance(test_db):
    """Create a fresh IrrigationScheduler with test DB."""
    # Reset global scheduler
    import irrigation_scheduler as mod
    mod.scheduler = None
    sched = mod.IrrigationScheduler(test_db)
    yield sched
    try:
        sched.stop()
    except Exception:
        pass
    mod.scheduler = None


@pytest.fixture
def started_scheduler(scheduler_instance):
    """Scheduler that's already started."""
    scheduler_instance.start()
    yield scheduler_instance


@pytest.fixture
def db_with_zones(test_db):
    """DB pre-populated with zones and programs."""
    test_db.create_zone({'name': 'Zone 1', 'duration': 10, 'group_id': 1, 'topic': '/test/z1'})
    test_db.create_zone({'name': 'Zone 2', 'duration': 15, 'group_id': 1, 'topic': '/test/z2'})
    test_db.create_zone({'name': 'Zone 3', 'duration': 5, 'group_id': 2, 'topic': '/test/z3'})
    return test_db


class TestIrrigationSchedulerInit:
    def test_init_creates_scheduler(self, test_db):
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        assert sched.db is test_db
        assert sched.is_running is False
        assert sched.active_zones == {}
        assert sched.program_jobs == {}

    def test_start_sets_running(self, scheduler_instance):
        scheduler_instance.start()
        assert scheduler_instance.is_running is True

    def test_start_idempotent(self, started_scheduler):
        started_scheduler.start()  # should not crash
        assert started_scheduler.is_running is True

    def test_stop(self, started_scheduler):
        started_scheduler.stop()
        assert started_scheduler.is_running is False

    def test_stop_idempotent(self, scheduler_instance):
        scheduler_instance.stop()  # not started
        assert scheduler_instance.is_running is False


class TestParseDt:
    def test_parse_full_datetime(self):
        from irrigation_scheduler import IrrigationScheduler
        dt = IrrigationScheduler._parse_dt('2026-01-15 10:30:00')
        assert dt == datetime(2026, 1, 15, 10, 30, 0)

    def test_parse_short_datetime(self):
        from irrigation_scheduler import IrrigationScheduler
        dt = IrrigationScheduler._parse_dt('2026-01-15 10:30')
        assert dt == datetime(2026, 1, 15, 10, 30)

    def test_parse_none(self):
        from irrigation_scheduler import IrrigationScheduler
        assert IrrigationScheduler._parse_dt(None) is None

    def test_parse_empty(self):
        from irrigation_scheduler import IrrigationScheduler
        assert IrrigationScheduler._parse_dt('') is None

    def test_parse_invalid(self):
        from irrigation_scheduler import IrrigationScheduler
        assert IrrigationScheduler._parse_dt('not-a-date') is None


class TestClearExpiredPostpones:
    def test_no_postponed_zones(self, started_scheduler, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        started_scheduler.clear_expired_postpones()  # should not crash

    def test_expired_postpone_cleared(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        past = (datetime.now() - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
        test_db.update_zone_postpone(z['id'], past, 'test')
        started_scheduler.clear_expired_postpones()
        zone = test_db.get_zone(z['id'])
        assert zone.get('postpone_until') is None

    def test_future_postpone_not_cleared(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        future = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
        test_db.update_zone_postpone(z['id'], future, 'test')
        started_scheduler.clear_expired_postpones()
        zone = test_db.get_zone(z['id'])
        assert zone.get('postpone_until') is not None


class TestScheduleProgram:
    def test_schedule_program(self, started_scheduler, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        test_db.create_zone({'name': 'Z2', 'duration': 15, 'group_id': 1})
        started_scheduler.schedule_program(1, {
            'name': 'Morning', 'time': '06:00',
            'days': [0, 2, 4], 'zones': [1, 2],
        })
        assert 1 in started_scheduler.program_jobs
        assert len(started_scheduler.program_jobs[1]) == 3  # 3 days

    def test_schedule_program_empty_days(self, started_scheduler, test_db):
        started_scheduler.schedule_program(1, {
            'name': 'Empty', 'time': '06:00',
            'days': [], 'zones': [1],
        })
        # Should not create jobs
        assert started_scheduler.program_jobs.get(1, []) == []

    def test_schedule_program_empty_zones(self, started_scheduler, test_db):
        started_scheduler.schedule_program(1, {
            'name': 'Empty', 'time': '06:00',
            'days': [0], 'zones': [],
        })
        assert started_scheduler.program_jobs.get(1, []) == []


class TestCancelProgram:
    def test_cancel_existing(self, started_scheduler, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        started_scheduler.schedule_program(1, {
            'name': 'Test', 'time': '06:00',
            'days': [0], 'zones': [1],
        })
        started_scheduler.cancel_program(1)
        assert started_scheduler.program_jobs[1] == []

    def test_cancel_nonexistent(self, started_scheduler):
        started_scheduler.cancel_program(999)  # should not crash


class TestScheduleZoneStop:
    def test_schedule_stop(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        started_scheduler.schedule_zone_stop(z['id'], 5)
        assert z['id'] in started_scheduler.active_zones

    def test_schedule_stop_none_duration(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        started_scheduler.schedule_zone_stop(z['id'], None)
        assert z['id'] not in started_scheduler.active_zones


class TestScheduleZoneHardStop:
    def test_hard_stop(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        run_at = datetime.now() + timedelta(minutes=5)
        started_scheduler.schedule_zone_hard_stop(z['id'], run_at)
        # Should not crash

    def test_hard_stop_past_time(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        run_at = datetime.now() - timedelta(minutes=5)
        started_scheduler.schedule_zone_hard_stop(z['id'], run_at)
        # Should adjust to now + 1 sec


class TestScheduleZoneCap:
    def test_zone_cap(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        started_scheduler.schedule_zone_cap(z['id'], cap_minutes=120)
        # Should not crash

    def test_cancel_zone_cap(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        started_scheduler.schedule_zone_cap(z['id'], cap_minutes=120)
        started_scheduler.cancel_zone_cap(z['id'])  # Should not crash

    def test_cancel_zone_cap_nonexistent(self, started_scheduler):
        started_scheduler.cancel_zone_cap(999)  # Should not crash


class TestMasterValveCap:
    def test_schedule_master_valve_cap(self, started_scheduler):
        started_scheduler.schedule_master_valve_cap(1, hours=24)

    def test_cancel_master_valve_cap(self, started_scheduler):
        started_scheduler.schedule_master_valve_cap(1, hours=24)
        started_scheduler.cancel_master_valve_cap(1)

    def test_cancel_master_valve_cap_nonexistent(self, started_scheduler):
        started_scheduler.cancel_master_valve_cap(999)


class TestStartGroupSequence:
    def test_start_group_sequence(self, started_scheduler, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 2, 'group_id': 1, 'topic': '/t/1'})
        test_db.create_zone({'name': 'Z2', 'duration': 3, 'group_id': 1, 'topic': '/t/2'})
        result = started_scheduler.start_group_sequence(1)
        assert result is True

    def test_start_group_no_zones(self, started_scheduler, test_db):
        result = started_scheduler.start_group_sequence(999)
        assert result is False

    def test_run_group_sequence_testing_mode(self, started_scheduler, test_db):
        z1 = test_db.create_zone({'name': 'Z1', 'duration': 2, 'group_id': 1, 'topic': '/t/1'})
        z2 = test_db.create_zone({'name': 'Z2', 'duration': 3, 'group_id': 1, 'topic': '/t/2'})
        started_scheduler._run_group_sequence(1, [z1['id'], z2['id']])
        # In TESTING mode, only first zone gets ON
        z = test_db.get_zone(z1['id'])
        assert z['state'] == 'on'


class TestCancelGroupJobs:
    def test_cancel_group_jobs(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1, 'topic': '/t/1'})
        test_db.update_zone(z['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})
        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            started_scheduler.cancel_group_jobs(1)
        # Zone should be stopped
        z_after = test_db.get_zone(z['id'])
        assert z_after['state'] == 'off'


class TestCancelZoneJobs:
    def test_cancel_zone_jobs(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        started_scheduler.schedule_zone_stop(z['id'], 5)
        started_scheduler.cancel_zone_jobs(z['id'])
        assert z['id'] not in started_scheduler.active_zones


class TestGetters:
    def test_get_active_programs(self, started_scheduler, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        started_scheduler.schedule_program(1, {
            'name': 'Test', 'time': '06:00', 'days': [0], 'zones': [1],
        })
        result = started_scheduler.get_active_programs()
        assert 1 in result
        assert 'job_ids' in result[1]

    def test_get_active_zones(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        started_scheduler.schedule_zone_stop(z['id'], 5)
        result = started_scheduler.get_active_zones()
        assert z['id'] in result


class TestLoadPrograms:
    def test_load_programs(self, started_scheduler, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        test_db.create_program({
            'name': 'Morning', 'time': '06:00',
            'days': [0, 2], 'zones': [1],
        })
        started_scheduler.load_programs()
        # Should have scheduled the program


class TestRecoverMissedRuns:
    def test_recover_no_programs(self, started_scheduler, test_db):
        started_scheduler.recover_missed_runs()  # should not crash

    def test_recover_wrong_day(self, started_scheduler, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        # Create program for a day that's not today
        today = datetime.now().weekday()
        other_day = (today + 1) % 7
        test_db.create_program({
            'name': 'Test', 'time': '06:00',
            'days': [other_day], 'zones': [1],
        })
        started_scheduler.recover_missed_runs()  # should skip


class TestCleanupJobsOnBoot:
    def test_cleanup(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        started_scheduler.schedule_zone_stop(z['id'], 5)
        started_scheduler.cleanup_jobs_on_boot()
        # zone_stop jobs should be removed


class TestStopOnBootActiveZones:
    def test_stop_active_zones(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1, 'topic': '/t/1'})
        test_db.update_zone(z['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})
        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            started_scheduler.stop_on_boot_active_zones()
        z_after = test_db.get_zone(z['id'])
        assert z_after['state'] == 'off'


class TestStopZoneInternal:
    def test_stop_zone(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1, 'topic': '/t/1'})
        test_db.update_zone(z['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})
        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            started_scheduler._stop_zone(z['id'])

    def test_stop_nonexistent_zone(self, started_scheduler, test_db):
        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            started_scheduler._stop_zone(9999)  # Should not crash


class TestRunProgramThreaded:
    def test_run_program_basic(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 1, 'group_id': 1, 'topic': '/t/1'})
        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            started_scheduler._run_program_threaded(1, [z['id']], 'Test Program')

    def test_run_program_nonexistent_zone(self, started_scheduler, test_db):
        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            started_scheduler._run_program_threaded(1, [9999], 'Test')

    def test_run_program_postponed_zone(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 1, 'group_id': 1, 'topic': '/t/1'})
        future = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
        test_db.update_zone_postpone(z['id'], future, 'test')
        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            started_scheduler._run_program_threaded(1, [z['id']], 'Test')

    def test_run_program_cancelled_group(self, started_scheduler, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 1, 'group_id': 1, 'topic': '/t/1'})
        cancel_event = threading.Event()
        cancel_event.set()
        started_scheduler.group_cancel_events[1] = cancel_event
        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            started_scheduler._run_program_threaded(1, [z['id']], 'Test')


class TestModuleLevelJobs:
    def test_job_run_program(self, test_db):
        import irrigation_scheduler as mod
        old = mod.scheduler
        mock_sched = MagicMock()
        mod.scheduler = mock_sched
        try:
            mod.job_run_program(1, [1, 2], 'Test')
            mock_sched._run_program_threaded.assert_called_once()
        finally:
            mod.scheduler = old

    def test_job_run_program_no_scheduler(self):
        import irrigation_scheduler as mod
        old = mod.scheduler
        mod.scheduler = None
        try:
            mod.job_run_program(1, [1], 'Test')  # Should not crash
        finally:
            mod.scheduler = old

    def test_job_run_group_sequence(self, test_db):
        import irrigation_scheduler as mod
        old = mod.scheduler
        mock_sched = MagicMock()
        mod.scheduler = mock_sched
        try:
            mod.job_run_group_sequence(1, [1, 2])
            mock_sched._run_group_sequence.assert_called_once()
        finally:
            mod.scheduler = old

    def test_job_stop_zone(self, test_db):
        import irrigation_scheduler as mod
        old = mod.scheduler
        mock_sched = MagicMock()
        mod.scheduler = mock_sched
        try:
            mod.job_stop_zone(1)
            mock_sched._stop_zone.assert_called_once_with(1)
        finally:
            mod.scheduler = old

    def test_job_clear_expired_postpones(self, test_db):
        import irrigation_scheduler as mod
        old = mod.scheduler
        mock_sched = MagicMock()
        mod.scheduler = mock_sched
        try:
            mod.job_clear_expired_postpones()
            mock_sched.clear_expired_postpones.assert_called_once()
        finally:
            mod.scheduler = old


class TestJobCloseMasterValve:
    def test_close_master_valve_no_group(self, test_db):
        from irrigation_scheduler import job_close_master_valve
        with patch('database.db', test_db):
            job_close_master_valve(999)  # No group exists, should not crash

    def test_close_master_valve_no_mv(self, test_db):
        from irrigation_scheduler import job_close_master_valve
        test_db.create_group('Test Group')
        with patch('database.db', test_db):
            job_close_master_valve(1)  # Group doesn't use MV, should skip


class TestInitScheduler:
    def test_init_scheduler(self, test_db):
        import irrigation_scheduler as mod
        mod.scheduler = None
        sched = mod.init_scheduler(test_db)
        assert sched is not None
        assert sched.is_running is True
        sched.stop()
        mod.scheduler = None

    def test_get_scheduler(self):
        import irrigation_scheduler as mod
        result = mod.get_scheduler()
        # May return None or existing instance


class TestSchedulePostponeSweeper:
    def test_schedule_sweeper(self, started_scheduler):
        started_scheduler.schedule_postpone_sweeper()
        # Should not crash, jobs added
