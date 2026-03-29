"""Tests for scheduler Event.wait (interruptible sleep)."""
import threading
import time
import pytest
from unittest.mock import patch, MagicMock
from database import IrrigationDB
from irrigation_scheduler import IrrigationScheduler


class TestSchedulerShutdownEvent:
    """Test that _shutdown_event is properly used for interruptible waits."""

    @pytest.fixture
    def scheduler(self, test_db):
        """Create a scheduler instance."""
        sched = IrrigationScheduler(test_db)
        yield sched
        if sched.is_running:
            sched.stop()

    def test_shutdown_event_exists(self, scheduler):
        """Scheduler should have a _shutdown_event."""
        assert hasattr(scheduler, '_shutdown_event')
        assert isinstance(scheduler._shutdown_event, threading.Event)

    def test_shutdown_event_not_set_initially(self, scheduler):
        """_shutdown_event should not be set at creation."""
        assert not scheduler._shutdown_event.is_set()

    def test_stop_sets_shutdown_event(self, scheduler):
        """stop() should set _shutdown_event."""
        scheduler.start()
        scheduler.stop()
        assert scheduler._shutdown_event.is_set()

    def test_shutdown_event_interrupts_wait(self, scheduler):
        """Setting _shutdown_event should interrupt Event.wait() calls."""
        result = {'interrupted': False}

        def waiter():
            if scheduler._shutdown_event.wait(timeout=30):
                result['interrupted'] = True

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)  # Let thread start
        scheduler._shutdown_event.set()
        t.join(timeout=2)
        assert result['interrupted'] is True

    def test_start_stop_cycle(self, scheduler):
        """Multiple start/stop cycles should work."""
        scheduler.start()
        assert scheduler.is_running
        scheduler.stop()
        assert not scheduler.is_running
        assert scheduler._shutdown_event.is_set()


class TestSchedulerClearPostpones:
    """Test clear_expired_postpones."""

    def test_clear_expired_postpones_empty(self, test_db):
        """Should handle no zones gracefully."""
        sched = IrrigationScheduler(test_db)
        sched.clear_expired_postpones()  # Should not raise

    def test_clear_expired_postpones_with_expired(self, test_db):
        """Should clear zones with expired postpone_until."""
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        zid = zones[0]['id']
        # Set postpone in the past
        test_db.update_zone(zid, {'postpone_until': '2020-01-01 00:00:00', 'postpone_reason': 'test'})

        sched = IrrigationScheduler(test_db)
        sched.clear_expired_postpones()

        zone = test_db.get_zone(zid)
        assert zone.get('postpone_until') is None

    def test_clear_postpones_future_not_cleared(self, test_db):
        """Should NOT clear zones with future postpone_until."""
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        zid = zones[0]['id']
        test_db.update_zone(zid, {'postpone_until': '2099-01-01 00:00:00', 'postpone_reason': 'test'})

        sched = IrrigationScheduler(test_db)
        sched.clear_expired_postpones()

        zone = test_db.get_zone(zid)
        assert zone.get('postpone_until') is not None


class TestSchedulerPrograms:
    """Test program scheduling."""

    def test_schedule_program(self, test_db):
        """Should schedule a program."""
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        sched = IrrigationScheduler(test_db)
        sched.start()
        try:
            sched.schedule_program(1, {
                'name': 'Morning',
                'time': '06:00',
                'days': [0, 2, 4],
                'zones': [zones[0]['id']],
            })
            assert 1 in sched.program_jobs
            assert len(sched.program_jobs[1]) == 3  # 3 days
        finally:
            sched.stop()

    def test_cancel_program(self, test_db):
        """Should cancel a scheduled program."""
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        sched = IrrigationScheduler(test_db)
        sched.start()
        try:
            sched.schedule_program(1, {
                'name': 'Morning',
                'time': '06:00',
                'days': [0],
                'zones': [zones[0]['id']],
            })
            sched.cancel_program(1)
            assert sched.program_jobs.get(1) == []
        finally:
            sched.stop()

    def test_load_programs(self, test_db):
        """Should load all programs from DB."""
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        test_db.create_program({
            'name': 'Morning',
            'time': '06:00',
            'days': [0, 2, 4],
            'zones': [zones[0]['id']],
        })
        sched = IrrigationScheduler(test_db)
        sched.start()
        try:
            sched.load_programs()
            assert len(sched.program_jobs) > 0
        finally:
            sched.stop()


class TestSchedulerZoneStop:
    """Test zone auto-stop scheduling."""

    def test_schedule_zone_stop(self, test_db):
        """Should schedule a zone auto-stop."""
        sched = IrrigationScheduler(test_db)
        sched.start()
        try:
            sched.schedule_zone_stop(1, 10)
            assert 1 in sched.active_zones
        finally:
            sched.stop()

    def test_cancel_zone_jobs(self, test_db):
        """Should cancel zone jobs and remove from active_zones."""
        sched = IrrigationScheduler(test_db)
        sched.start()
        try:
            sched.schedule_zone_stop(1, 10)
            sched.cancel_zone_jobs(1)
            assert 1 not in sched.active_zones
        finally:
            sched.stop()

    def test_schedule_zone_cap(self, test_db):
        """Should schedule zone cap stop."""
        sched = IrrigationScheduler(test_db)
        sched.start()
        try:
            sched.schedule_zone_cap(1, 60)
            # Should not crash and should have a job
        finally:
            sched.stop()

    def test_cancel_zone_cap(self, test_db):
        """Should cancel zone cap without error."""
        sched = IrrigationScheduler(test_db)
        sched.start()
        try:
            sched.cancel_zone_cap(1)  # Even if no cap scheduled
        finally:
            sched.stop()
