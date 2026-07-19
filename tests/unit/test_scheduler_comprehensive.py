"""Comprehensive tests for irrigation_scheduler.py — targeting 90%+ coverage."""

import contextlib
import os
import threading
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

os.environ["TESTING"] = "1"


@pytest.fixture
def scheduler_instance(test_db):
    """Create a fresh IrrigationScheduler with test DB."""
    # Reset global scheduler
    import irrigation_scheduler as mod

    mod.scheduler = None
    sched = mod.IrrigationScheduler(test_db)
    yield sched
    with contextlib.suppress(RuntimeError, ValueError):
        sched.stop()
    mod.scheduler = None


@pytest.fixture
def started_scheduler(scheduler_instance):
    """Scheduler that's already started."""
    scheduler_instance.start()
    yield scheduler_instance


@pytest.fixture
def db_with_zones(test_db):
    """DB pre-populated with zones and programs."""
    test_db.create_zone({"name": "Zone 1", "duration": 10, "group_id": 1, "topic": "/test/z1"})
    test_db.create_zone({"name": "Zone 2", "duration": 15, "group_id": 1, "topic": "/test/z2"})
    test_db.create_zone({"name": "Zone 3", "duration": 5, "group_id": 2, "topic": "/test/z3"})
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

        dt = IrrigationScheduler._parse_dt("2026-01-15 10:30:00")
        assert dt == datetime(2026, 1, 15, 10, 30, 0)

    def test_parse_short_datetime(self):
        from irrigation_scheduler import IrrigationScheduler

        dt = IrrigationScheduler._parse_dt("2026-01-15 10:30")
        assert dt == datetime(2026, 1, 15, 10, 30)

    def test_parse_none(self):
        from irrigation_scheduler import IrrigationScheduler

        assert IrrigationScheduler._parse_dt(None) is None

    def test_parse_empty(self):
        from irrigation_scheduler import IrrigationScheduler

        assert IrrigationScheduler._parse_dt("") is None

    def test_parse_invalid(self):
        from irrigation_scheduler import IrrigationScheduler

        assert IrrigationScheduler._parse_dt("not-a-date") is None


class TestClearExpiredPostpones:
    def test_no_postponed_zones(self, started_scheduler, test_db):
        test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        started_scheduler.clear_expired_postpones()  # should not crash

    def test_expired_postpone_cleared(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone_postpone(z["id"], past, "test")
        started_scheduler.clear_expired_postpones()
        zone = test_db.get_zone(z["id"])
        assert zone.get("postpone_until") is None

    def test_future_postpone_not_cleared(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone_postpone(z["id"], future, "test")
        started_scheduler.clear_expired_postpones()
        zone = test_db.get_zone(z["id"])
        assert zone.get("postpone_until") is not None


class TestScheduleProgram:
    def test_schedule_program(self, started_scheduler, test_db):
        test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        test_db.create_zone({"name": "Z2", "duration": 15, "group_id": 1})
        started_scheduler.schedule_program(
            1,
            {
                "name": "Morning",
                "time": "06:00",
                "days": [0, 2, 4],
                "zones": [1, 2],
            },
        )
        assert 1 in started_scheduler.program_jobs
        assert len(started_scheduler.program_jobs[1]) == 3  # 3 days

    def test_schedule_program_empty_days(self, started_scheduler, test_db):
        started_scheduler.schedule_program(
            1,
            {
                "name": "Empty",
                "time": "06:00",
                "days": [],
                "zones": [1],
            },
        )
        # Should not create jobs
        assert started_scheduler.program_jobs.get(1, []) == []

    def test_schedule_program_empty_zones(self, started_scheduler, test_db):
        started_scheduler.schedule_program(
            1,
            {
                "name": "Empty",
                "time": "06:00",
                "days": [0],
                "zones": [],
            },
        )
        assert started_scheduler.program_jobs.get(1, []) == []


class TestCancelProgram:
    def test_cancel_existing(self, started_scheduler, test_db):
        test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        started_scheduler.schedule_program(
            1,
            {
                "name": "Test",
                "time": "06:00",
                "days": [0],
                "zones": [1],
            },
        )
        started_scheduler.cancel_program(1)
        assert started_scheduler.program_jobs[1] == []

    def test_cancel_nonexistent(self, started_scheduler):
        started_scheduler.cancel_program(999)  # should not crash


class TestScheduleZoneStop:
    def test_schedule_stop(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        started_scheduler.schedule_zone_stop(z["id"], 5)
        assert z["id"] in started_scheduler.active_zones

    def test_schedule_stop_none_duration(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        started_scheduler.schedule_zone_stop(z["id"], None)
        assert z["id"] not in started_scheduler.active_zones


class TestScheduleZoneHardStop:
    def test_hard_stop(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        run_at = datetime.now() + timedelta(minutes=5)
        started_scheduler.schedule_zone_hard_stop(z["id"], run_at)
        # Should not crash

    def test_hard_stop_past_time(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        run_at = datetime.now() - timedelta(minutes=5)
        started_scheduler.schedule_zone_hard_stop(z["id"], run_at)
        # Should adjust to now + 1 sec


class TestScheduleZoneCap:
    def test_zone_cap(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        started_scheduler.schedule_zone_cap(z["id"], cap_minutes=120)
        # Should not crash

    def test_cancel_zone_cap(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        started_scheduler.schedule_zone_cap(z["id"], cap_minutes=120)
        started_scheduler.cancel_zone_cap(z["id"])  # Should not crash

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
        test_db.create_zone({"name": "Z1", "duration": 2, "group_id": 1, "topic": "/t/1"})
        test_db.create_zone({"name": "Z2", "duration": 3, "group_id": 1, "topic": "/t/2"})
        with patch("services.zone_control.db", test_db):
            result = started_scheduler.start_group_sequence(1)
        assert result is True

    def test_start_group_no_zones(self, started_scheduler, test_db):
        result = started_scheduler.start_group_sequence(999)
        assert result is False

    def test_run_group_sequence_testing_mode(self, started_scheduler, test_db):
        z1 = test_db.create_zone({"name": "Z1", "duration": 2, "group_id": 1, "topic": "/t/1"})
        z2 = test_db.create_zone({"name": "Z2", "duration": 3, "group_id": 1, "topic": "/t/2"})
        started_scheduler._run_group_sequence(1, [z1["id"], z2["id"]])
        # In TESTING mode, only first zone gets ON
        z = test_db.get_zone(z1["id"])
        assert z["state"] == "on"

    def test_start_group_sequence_percent_signature_back_compat(self, started_scheduler, test_db):
        """Issue #12 — adding `override_percent` kwarg must not break legacy callers.

        Calling `start_group_sequence(gid)` with no kwargs uses the zones'
        own durations (helper's third branch). Result must match what the
        method returned before #12 — True, with the first zone marked ON.
        """
        group = test_db.create_group("Back-compat sequence group")
        z1 = test_db.create_zone({"name": "BC1", "duration": 4, "group_id": group["id"], "topic": "/t/bc1"})
        test_db.create_zone({"name": "BC2", "duration": 6, "group_id": group["id"], "topic": "/t/bc2"})
        with patch("services.zone_control.db", test_db):
            result = started_scheduler.start_group_sequence(group["id"])
        assert result is True
        # First zone goes ON in TESTING mode (legacy assertion still holds).
        assert test_db.get_zone(z1["id"])["state"] == "on"


class TestCancelGroupJobs:
    def test_cancel_group_jobs(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1, "topic": "/t/1"})
        test_db.update_zone(z["id"], {"state": "on", "watering_start_time": "2026-01-01 10:00:00"})
        with (
            patch("services.zone_control.db", test_db),
            patch("services.zone_control.publish_mqtt_value", return_value=True),
            patch("services.zone_control.water_monitor"),
            patch("services.zone_control.state_verifier"),
        ):
            started_scheduler.cancel_group_jobs(1)
        # Zone should be stopped
        z_after = test_db.get_zone(z["id"])
        assert z_after["state"] == "off"


class TestCancelZoneJobs:
    def test_cancel_zone_jobs(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        started_scheduler.schedule_zone_stop(z["id"], 5)
        started_scheduler.cancel_zone_jobs(z["id"])
        assert z["id"] not in started_scheduler.active_zones


class TestGetters:
    def test_get_active_programs(self, started_scheduler, test_db):
        test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        started_scheduler.schedule_program(
            1,
            {
                "name": "Test",
                "time": "06:00",
                "days": [0],
                "zones": [1],
            },
        )
        result = started_scheduler.get_active_programs()
        assert 1 in result
        assert "job_ids" in result[1]

    def test_get_active_zones(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        started_scheduler.schedule_zone_stop(z["id"], 5)
        result = started_scheduler.get_active_zones()
        assert z["id"] in result


class TestLoadPrograms:
    def test_load_programs(self, started_scheduler, test_db):
        test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        test_db.create_program(
            {
                "name": "Morning",
                "time": "06:00",
                "days": [0, 2],
                "zones": [1],
            }
        )
        started_scheduler.load_programs()
        # Should have scheduled the program


def _frozen_datetime(now: datetime):
    """Return a datetime subclass whose ``now()`` is deterministic."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return now
            return now.astimezone(tz)

    return FrozenDateTime


def _recent_time_today(now: datetime, minutes_ago=10):
    """HH:MM ``minutes_ago`` before ``now``, without crossing midnight."""
    start = now - timedelta(minutes=minutes_ago)
    if start.date() != now.date():
        start = now.replace(hour=0, minute=0)
    return start.strftime("%H:%M")


def _recover_jobs_at(scheduler, now: datetime):
    """Run recovery at ``now`` and return the mocked scheduler add call."""
    with (
        patch("irrigation_scheduler.datetime", _frozen_datetime(now)),
        patch.object(scheduler.scheduler, "add_job") as mock_add,
    ):
        scheduler.recover_missed_runs()
    return mock_add


class TestRecoverMissedRuns:
    def test_recover_no_programs(self, started_scheduler, test_db):
        now = datetime(2026, 7, 17, 12, 0)
        with patch("irrigation_scheduler.datetime", _frozen_datetime(now)):
            started_scheduler.recover_missed_runs()  # should not crash

    def test_recover_wrong_day(self, started_scheduler, test_db):
        now = datetime(2026, 7, 17, 12, 0)  # Friday
        test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        # Create program for a day that's not today
        today = now.weekday()
        other_day = (today + 1) % 7
        test_db.create_program(
            {
                "name": "Test",
                "time": "06:00",
                "days": [other_day],
                "zones": [1],
            }
        )
        with patch("irrigation_scheduler.datetime", _frozen_datetime(now)):
            started_scheduler.recover_missed_runs()  # should skip

    def test_recover_skips_disabled_program(self, started_scheduler, test_db):
        now = datetime(2026, 7, 17, 12, 0)
        z = test_db.create_zone({"name": "Z1", "duration": 30, "group_id": 1})
        test_db.create_program(
            {
                "name": "Disabled",
                "time": _recent_time_today(now),
                "days": [0, 1, 2, 3, 4, 5, 6],
                "zones": [z["id"]],
                "enabled": False,
            }
        )
        mock_add = _recover_jobs_at(started_scheduler, now)
        assert not mock_add.called

    def test_recover_skips_interval_program(self, started_scheduler, test_db):
        # IntervalTrigger пере-якорится при каждом старте сервиса: после
        # рестарта нельзя отличить прерванный запуск от дня, когда полив
        # не планировался, поэтому interval-программы не восстанавливаются.
        now = datetime(2026, 7, 17, 12, 0)
        z = test_db.create_zone({"name": "Z1", "duration": 30, "group_id": 1})
        test_db.create_program(
            {
                "name": "Interval",
                "time": _recent_time_today(now),
                "days": [],
                "zones": [z["id"]],
                "schedule_type": "interval",
                "interval_days": 2,
            }
        )
        mock_add = _recover_jobs_at(started_scheduler, now)
        assert not mock_add.called

    @pytest.mark.parametrize(
        ("now", "should_recover"),
        [
            pytest.param(datetime(2026, 7, 17, 12, 0), True, id="odd-date"),
            pytest.param(datetime(2026, 7, 18, 12, 0), False, id="even-date"),
        ],
    )
    def test_recover_even_odd_null_means_odd(self, started_scheduler, test_db, now, should_recover):
        # NULL в even_odd планировщик трактует как нечётные дни —
        # восстановление обязано совпадать с ним.
        z = test_db.create_zone({"name": "Z1", "duration": 30, "group_id": 1})
        test_db.create_program(
            {
                "name": "EvenOddNull",
                "time": _recent_time_today(now),
                "days": [],
                "zones": [z["id"]],
                "schedule_type": "even-odd",
                "even_odd": None,
            }
        )
        mock_add = _recover_jobs_at(started_scheduler, now)
        assert mock_add.called is should_recover

    @pytest.mark.parametrize(
        "now",
        [
            pytest.param(datetime(2026, 7, 17, 12, 0), id="odd-date"),
            pytest.param(datetime(2026, 7, 18, 12, 0), id="even-date"),
        ],
    )
    def test_recover_even_odd_program_matching_parity(self, started_scheduler, test_db, now):
        z = test_db.create_zone({"name": "Z1", "duration": 30, "group_id": 1})
        parity = "even" if now.day % 2 == 0 else "odd"
        test_db.create_program(
            {
                "name": "EvenOdd",
                "time": _recent_time_today(now),
                "days": [],
                "zones": [z["id"]],
                "schedule_type": "even-odd",
                "even_odd": parity,
            }
        )
        mock_add = _recover_jobs_at(started_scheduler, now)
        assert mock_add.called

    @pytest.mark.parametrize(
        "now",
        [
            pytest.param(datetime(2026, 7, 17, 12, 0), id="odd-date"),
            pytest.param(datetime(2026, 7, 18, 12, 0), id="even-date"),
        ],
    )
    def test_recover_even_odd_program_wrong_parity(self, started_scheduler, test_db, now):
        z = test_db.create_zone({"name": "Z1", "duration": 30, "group_id": 1})
        wrong_parity = "odd" if now.day % 2 == 0 else "even"
        test_db.create_program(
            {
                "name": "EvenOddWrong",
                "time": _recent_time_today(now),
                "days": [],
                "zones": [z["id"]],
                "schedule_type": "even-odd",
                "even_odd": wrong_parity,
            }
        )
        mock_add = _recover_jobs_at(started_scheduler, now)
        assert not mock_add.called

    def test_recover_extra_time_window(self, started_scheduler, test_db):
        # Main time is outside the execution window, extra time is inside it.
        z = test_db.create_zone({"name": "Z1", "duration": 30, "group_id": 1})
        now = datetime(2026, 7, 17, 12, 0)
        main_t = (now + timedelta(hours=4)).strftime("%H:%M")
        test_db.create_program(
            {
                "name": "ExtraTimes",
                "time": main_t,
                "days": [0, 1, 2, 3, 4, 5, 6],
                "zones": [z["id"]],
                "extra_times": [_recent_time_today(now)],
            }
        )
        mock_add = _recover_jobs_at(started_scheduler, now)
        assert mock_add.called
        kwargs = mock_add.call_args.kwargs
        assert "_recover_" in kwargs["id"]


class TestCleanupJobsOnBoot:
    def test_cleanup(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        started_scheduler.schedule_zone_stop(z["id"], 5)
        started_scheduler.cleanup_jobs_on_boot()
        # zone_stop jobs should be removed


class TestStopOnBootActiveZones:
    def test_stop_active_zones(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1, "topic": "/t/1"})
        test_db.update_zone(z["id"], {"state": "on", "watering_start_time": "2026-01-01 10:00:00"})
        with (
            patch("services.zone_control.db", test_db),
            patch("services.zone_control.publish_mqtt_value", return_value=True),
            patch("services.zone_control.water_monitor"),
            patch("services.zone_control.state_verifier"),
        ):
            started_scheduler.stop_on_boot_active_zones()
        z_after = test_db.get_zone(z["id"])
        assert z_after["state"] == "off"


class TestStopZoneInternal:
    def test_stop_zone(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1, "topic": "/t/1"})
        test_db.update_zone(z["id"], {"state": "on", "watering_start_time": "2026-01-01 10:00:00"})
        with (
            patch("services.zone_control.db", test_db),
            patch("services.zone_control.publish_mqtt_value", return_value=True),
            patch("services.zone_control.water_monitor"),
            patch("services.zone_control.state_verifier"),
        ):
            started_scheduler._stop_zone(z["id"])

    def test_stop_nonexistent_zone(self, started_scheduler, test_db):
        with (
            patch("services.zone_control.db", test_db),
            patch("services.zone_control.publish_mqtt_value", return_value=True),
            patch("services.zone_control.water_monitor"),
            patch("services.zone_control.state_verifier"),
        ):
            started_scheduler._stop_zone(9999)  # Should not crash


class TestRunProgramThreaded:
    def test_run_program_basic(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 1, "group_id": 1, "topic": "/t/1"})
        with (
            patch("services.zone_control.db", test_db),
            patch("services.zone_control.publish_mqtt_value", return_value=True),
            patch("services.zone_control.water_monitor"),
            patch("services.zone_control.state_verifier"),
        ):
            started_scheduler._run_program_threaded(1, [z["id"]], "Test Program")

    def test_run_program_nonexistent_zone(self, started_scheduler, test_db):
        with (
            patch("services.zone_control.db", test_db),
            patch("services.zone_control.publish_mqtt_value", return_value=True),
            patch("services.zone_control.water_monitor"),
            patch("services.zone_control.state_verifier"),
        ):
            started_scheduler._run_program_threaded(1, [9999], "Test")

    def test_run_program_postponed_zone(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 1, "group_id": 1, "topic": "/t/1"})
        future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone_postpone(z["id"], future, "test")
        with (
            patch("services.zone_control.db", test_db),
            patch("services.zone_control.publish_mqtt_value", return_value=True),
            patch("services.zone_control.water_monitor"),
            patch("services.zone_control.state_verifier"),
        ):
            started_scheduler._run_program_threaded(1, [z["id"]], "Test")

    def test_run_program_cancelled_group(self, started_scheduler, test_db):
        z = test_db.create_zone({"name": "Z1", "duration": 1, "group_id": 1, "topic": "/t/1"})
        cancel_event = threading.Event()
        cancel_event.set()
        started_scheduler.group_cancel_events[1] = cancel_event
        with (
            patch("services.zone_control.db", test_db),
            patch("services.zone_control.publish_mqtt_value", return_value=True),
            patch("services.zone_control.water_monitor"),
            patch("services.zone_control.state_verifier"),
        ):
            started_scheduler._run_program_threaded(1, [z["id"]], "Test")


class TestModuleLevelJobs:
    def test_job_run_program(self, test_db):
        import irrigation_scheduler as mod

        old = mod.scheduler
        mock_sched = MagicMock()
        mod.scheduler = mock_sched
        try:
            mod.job_run_program(1, [1, 2], "Test")
            mock_sched._run_program_threaded.assert_called_once()
        finally:
            mod.scheduler = old

    def test_job_run_program_no_scheduler(self):
        import irrigation_scheduler as mod

        old = mod.scheduler
        mod.scheduler = None
        try:
            mod.job_run_program(1, [1], "Test")  # Should not crash
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

        with patch("database.db", test_db):
            job_close_master_valve(999)  # No group exists, should not crash

    def test_close_master_valve_no_mv(self, test_db):
        from irrigation_scheduler import job_close_master_valve

        test_db.create_group("Test Group")
        with patch("database.db", test_db):
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
