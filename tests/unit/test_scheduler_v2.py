"""Tests for Scheduler v2: new schedule types (interval, even-odd), extra_times, enabled field."""

import os
from datetime import datetime, timedelta

import pytest

os.environ["TESTING"] = "1"


def _cron_fields(trigger):
    """Return CronTrigger fields in a stable, semantic form."""
    return {field.name: str(field) for field in trigger.fields}


def _frozen_datetime(now: datetime):
    """Return a datetime subclass whose ``now()`` is deterministic."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return now
            return now.astimezone(tz)

    return FrozenDateTime


@pytest.fixture
def test_scheduler(test_db):
    """Create a test scheduler instance with test DB."""
    from irrigation_scheduler import IrrigationScheduler

    scheduler = IrrigationScheduler(test_db)
    scheduler.start()

    yield scheduler

    # Cleanup
    scheduler.stop()


class TestScheduleWeekdaysProgram:
    """Tests for standard weekdays schedule (existing functionality)."""

    def test_schedule_weekdays_program(self, test_scheduler):
        """Программа с schedule_type='weekdays' планируется корректно."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {
                "name": "Weekdays Test",
                "time": "06:00",
                "schedule_type": "weekdays",
                "days": [0, 2, 4],  # Пн, Ср, Пт
                "zones": [1],
                "enabled": 1,
            }
        )

        test_scheduler.schedule_program(prog["id"], prog)

        # Проверяем что job создан
        jobs = test_scheduler.scheduler.get_jobs()
        job_ids = [j.id for j in jobs]

        assert any(f"program:{prog['id']}:" in job_id for job_id in job_ids)

    def test_weekdays_program_has_correct_trigger(self, test_scheduler):
        """Weekdays программа имеет CronTrigger с правильными днями."""
        from apscheduler.triggers.cron import CronTrigger

        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {
                "name": "Weekdays",
                "time": "06:00",
                "schedule_type": "weekdays",
                "days": [0, 2, 4],
                "zones": [1],
                "enabled": 1,
            }
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f"program:{prog['id']}:" in j.id]

        jobs_by_id = {job.id: job for job in prog_jobs}
        expected_days = {f"program:{prog['id']}:main:d{day}": day for day in (0, 2, 4)}
        assert jobs_by_id.keys() == expected_days.keys()

        for job_id, day in expected_days.items():
            trigger = jobs_by_id[job_id].trigger
            assert isinstance(trigger, CronTrigger)
            fields = _cron_fields(trigger)
            assert fields["day_of_week"] == str(day)
            assert fields["hour"] == "6"
            assert fields["minute"] == "0"
            assert fields["second"] == "0"


class TestScheduleIntervalProgram:
    """Tests for schedule_type='interval' (every N days)."""

    def test_schedule_interval_program(self, test_scheduler):
        """Программа с schedule_type='interval' планируется с IntervalTrigger."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {
                "name": "Every 3 Days",
                "time": "06:00",
                "schedule_type": "interval",
                "interval_days": 3,
                "days": [],
                "zones": [1],
                "enabled": 1,
            }
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        job_ids = [j.id for j in jobs]

        assert any(f"program:{prog['id']}:" in job_id for job_id in job_ids)

    def test_interval_program_uses_interval_trigger(self, test_scheduler):
        """Interval программа использует IntervalTrigger."""
        from apscheduler.triggers.interval import IntervalTrigger

        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {
                "name": "Every 2 Days",
                "time": "06:00",
                "schedule_type": "interval",
                "interval_days": 2,
                "days": [],
                "zones": [1],
                "enabled": 1,
            }
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f"program:{prog['id']}:" in j.id]

        assert len(prog_jobs) > 0

        job = prog_jobs[0]
        assert isinstance(job.trigger, IntervalTrigger)

    @pytest.mark.parametrize(
        ("now", "expected_start"),
        [
            pytest.param(
                datetime(2030, 1, 15, 10, 0),
                datetime(2030, 1, 15, 14, 30),
                id="time-still-ahead",
            ),
            pytest.param(
                datetime(2030, 1, 15, 16, 0),
                datetime(2030, 1, 16, 14, 30),
                id="time-already-passed",
            ),
        ],
    )
    def test_interval_program_first_run_today(self, test_scheduler, monkeypatch, now, expected_start):
        """Interval starts at the nearest requested wall-clock time."""
        from apscheduler.triggers.interval import IntervalTrigger

        monkeypatch.setattr("irrigation_scheduler.datetime", _frozen_datetime(now))
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {
                "name": "Every 5 Days",
                "time": "14:30",
                "schedule_type": "interval",
                "interval_days": 5,
                "days": [],
                "zones": [1],
                "enabled": 1,
            }
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f"program:{prog['id']}:" in j.id]

        assert len(prog_jobs) == 1

        job = prog_jobs[0]
        assert isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval == timedelta(days=5)
        assert job.trigger.start_date.replace(tzinfo=None) == expected_start


class TestScheduleEvenOddProgram:
    """Tests for schedule_type='even-odd' (even/odd days of month)."""

    def test_schedule_even_odd_program_even(self, test_scheduler):
        """Программа с even_odd='even' планируется на чётные дни."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {
                "name": "Even Days",
                "time": "06:00",
                "schedule_type": "even-odd",
                "even_odd": "even",
                "days": [],
                "zones": [1],
                "enabled": 1,
            }
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        job_ids = [j.id for j in jobs]

        assert any(f"program:{prog['id']}:" in job_id for job_id in job_ids)

    def test_schedule_even_odd_program_odd(self, test_scheduler):
        """Программа с even_odd='odd' планируется на нечётные дни."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {
                "name": "Odd Days",
                "time": "06:00",
                "schedule_type": "even-odd",
                "even_odd": "odd",
                "days": [],
                "zones": [1],
                "enabled": 1,
            }
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        job_ids = [j.id for j in jobs]

        assert any(f"program:{prog['id']}:" in job_id for job_id in job_ids)

    @pytest.mark.parametrize(
        ("parity", "expected_days"),
        [
            pytest.param("even", ",".join(str(day) for day in range(2, 31, 2)), id="even"),
            pytest.param("odd", ",".join(str(day) for day in range(1, 32, 2)), id="odd"),
        ],
    )
    def test_even_odd_program_has_correct_cron_days(self, test_scheduler, parity, expected_days):
        """Even-odd CronTrigger preserves parity and requested start time."""
        from apscheduler.triggers.cron import CronTrigger

        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {
                "name": f"{parity.title()} Days",
                "time": "06:45",
                "schedule_type": "even-odd",
                "even_odd": parity,
                "days": [],
                "zones": [1],
                "enabled": 1,
            }
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f"program:{prog['id']}:" in j.id]

        assert len(prog_jobs) == 1

        job = prog_jobs[0]
        assert isinstance(job.trigger, CronTrigger)
        fields = _cron_fields(job.trigger)
        assert fields["day"] == expected_days
        assert fields["day_of_week"] == "*"
        assert fields["hour"] == "6"
        assert fields["minute"] == "45"
        assert fields["second"] == "0"


class TestExtraTimes:
    """Tests for extra_times field (multiple start times per day)."""

    def test_schedule_extra_times(self, test_scheduler):
        """Each weekday/extra-time pair gets the requested CronTrigger."""
        from apscheduler.triggers.cron import CronTrigger

        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {
                "name": "Triple Start",
                "time": "06:00",
                "extra_times": ["12:00", "18:00"],
                "days": [0, 2, 4],
                "zones": [1],
                "enabled": 1,
            }
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f"program:{prog['id']}:" in j.id]

        expected = {}
        for day in (0, 2, 4):
            expected[f"program:{prog['id']}:main:d{day}"] = (day, 6, 0)
            expected[f"program:{prog['id']}:extra:0:d{day}"] = (day, 12, 0)
            expected[f"program:{prog['id']}:extra:1:d{day}"] = (day, 18, 0)

        jobs_by_id = {job.id: job for job in prog_jobs}
        assert jobs_by_id.keys() == expected.keys()
        for job_id, (day, hour, minute) in expected.items():
            trigger = jobs_by_id[job_id].trigger
            assert isinstance(trigger, CronTrigger)
            fields = _cron_fields(trigger)
            assert fields["day_of_week"] == str(day)
            assert fields["hour"] == str(hour)
            assert fields["minute"] == str(minute)

    def test_extra_times_jobs_have_correct_ids(self, test_scheduler):
        """Jobs для extra_times имеют правильные ID (main, extra0, extra1, ...)."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {"name": "Multi-Start", "time": "06:00", "extra_times": ["18:00"], "days": [0], "zones": [1], "enabled": 1}
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        prog_job_ids = [j.id for j in jobs if f"program:{prog['id']}:" in j.id]

        assert f"program:{prog['id']}:main:d0" in prog_job_ids
        assert f"program:{prog['id']}:extra:0:d0" in prog_job_ids

    def test_extra_times_with_interval_schedule(self, test_scheduler):
        """extra_times работает с schedule_type='interval'."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {
                "name": "Interval Multi-Start",
                "time": "06:00",
                "extra_times": ["18:00"],
                "schedule_type": "interval",
                "interval_days": 2,
                "days": [],
                "zones": [1],
                "enabled": 1,
            }
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f"program:{prog['id']}:" in j.id]

        # Должно быть 2 job (main + extra0) для interval
        assert len(prog_jobs) == 2

    def test_empty_extra_times_creates_single_job(self, test_scheduler):
        """Программа с extra_times=[] создаёт только один job (main)."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {"name": "Single Start", "time": "06:00", "extra_times": [], "days": [0], "zones": [1], "enabled": 1}
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f"program:{prog['id']}:" in j.id]

        # Только main job
        assert len(prog_jobs) == 1


class TestEnabledField:
    """Tests for enabled field (skip disabled programs)."""

    def test_skip_disabled_program(self, test_scheduler):
        """Программа с enabled=0 не планируется."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {"name": "Disabled", "time": "06:00", "days": [0, 1, 2], "zones": [1], "enabled": 0}
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f"program:{prog['id']}:" in j.id]

        # Не должно быть jobs для выключенной программы
        assert len(prog_jobs) == 0

    def test_enabled_default_true_schedules_program(self, test_scheduler):
        """Программа без явного enabled (дефолт=1) планируется."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {
                "name": "Default Enabled",
                "time": "06:00",
                "days": [0],
                "zones": [1],
                # enabled не указан, дефолт 1
            }
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f"program:{prog['id']}:" in j.id]

        assert len(prog_jobs) > 0

    def test_enable_disable_reschedules(self, test_scheduler):
        """При toggle enabled → пересчёт расписания (добавление/удаление jobs)."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {"name": "Toggle Test", "time": "06:00", "days": [0], "zones": [1], "enabled": 1}
        )

        # Планируем
        test_scheduler.schedule_program(prog["id"], prog)

        jobs_before = [j for j in test_scheduler.scheduler.get_jobs() if f"program:{prog['id']}:" in j.id]
        assert len(jobs_before) > 0

        # Выключаем
        prog["enabled"] = 0
        test_scheduler.schedule_program(prog["id"], prog)

        jobs_after_disable = [j for j in test_scheduler.scheduler.get_jobs() if f"program:{prog['id']}:" in j.id]
        assert len(jobs_after_disable) == 0

        # Включаем обратно
        prog["enabled"] = 1
        test_scheduler.schedule_program(prog["id"], prog)

        jobs_after_enable = [j for j in test_scheduler.scheduler.get_jobs() if f"program:{prog['id']}:" in j.id]
        assert len(jobs_after_enable) > 0


class TestSmartTypeWeatherAdjustment:
    """Smart has no proven semantics and must not alias time-based."""

    def test_smart_type_weather_adjustment(self, test_scheduler):
        """Программа с type='smart' отклоняется без scheduler jobs."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {"name": "Smart Program", "time": "06:00", "type": "smart", "days": [0], "zones": [1], "enabled": 1}
        )

        assert test_scheduler.schedule_program(prog["id"], prog) is False

        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f"program:{prog['id']}:" in j.id]

        assert prog_jobs == []

    def test_time_based_uses_standard_weather(self, test_scheduler):
        """Программа с type='time-based' использует стандартную погодокоррекцию."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {
                "name": "Time-Based Program",
                "time": "06:00",
                "type": "time-based",
                "days": [0],
                "zones": [1],
                "enabled": 1,
            }
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f"program:{prog['id']}:" in j.id]

        assert len(prog_jobs) > 0


class TestSchedulerIntegration:
    """Integration tests for scheduler with v2 features."""

    def test_full_v2_program_scheduling(self, test_scheduler):
        """Полная программа v2 (все поля) планируется корректно."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        test_scheduler.db.create_zone({"name": "Z2", "duration": 15, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {
                "name": "Full v2 Program",
                "time": "06:00",
                "extra_times": ["18:00"],
                "type": "time-based",
                "schedule_type": "interval",
                "interval_days": 2,
                "color": "#9c27b0",
                "enabled": 1,
                "days": [],
                "zones": [1, 2],
            }
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f"program:{prog['id']}:" in j.id]

        # Должно быть 2 job (main + extra0) для interval с extra_times
        assert len(prog_jobs) == 2

        # Проверяем что оба job имеют IntervalTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        for job in prog_jobs:
            assert isinstance(job.trigger, IntervalTrigger)

    def test_reschedule_program_on_update(self, test_scheduler):
        """При обновлении программы jobs пересоздаются."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {"name": "Test", "time": "06:00", "days": [0], "zones": [1], "enabled": 1}
        )

        # Планируем
        test_scheduler.schedule_program(prog["id"], prog)

        jobs_before = [j for j in test_scheduler.scheduler.get_jobs() if f"program:{prog['id']}:" in j.id]
        assert len(jobs_before) > 0

        # Обновляем время и schedule_type
        updated_prog = test_scheduler.db.update_program(
            prog["id"], {"time": "18:00", "schedule_type": "interval", "interval_days": 3, "days": []}
        )

        # Пере-планируем
        test_scheduler.schedule_program(prog["id"], updated_prog)

        jobs_after = [j for j in test_scheduler.scheduler.get_jobs() if f"program:{prog['id']}:" in j.id]
        assert len(jobs_after) > 0

        # Проверяем что trigger изменился
        from apscheduler.triggers.interval import IntervalTrigger

        job = jobs_after[0]
        assert isinstance(job.trigger, IntervalTrigger)

    def test_cancel_program_removes_all_jobs(self, test_scheduler):
        """При удалении программы все её jobs удаляются (включая extra_times)."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        prog = test_scheduler.db.create_program(
            {
                "name": "To Delete",
                "time": "06:00",
                "extra_times": ["12:00", "18:00"],
                "days": [0],
                "zones": [1],
                "enabled": 1,
            }
        )

        # Планируем
        test_scheduler.schedule_program(prog["id"], prog)

        jobs_before = [j for j in test_scheduler.scheduler.get_jobs() if f"program:{prog['id']}:" in j.id]
        assert len(jobs_before) == 3  # main + 2 extra

        # Отменяем
        test_scheduler.cancel_program(prog["id"])

        jobs_after = [j for j in test_scheduler.scheduler.get_jobs() if f"program:{prog['id']}:" in j.id]
        assert len(jobs_after) == 0


class TestBackwardCompatibilityScheduler:
    """Tests ensuring old programs still schedule correctly."""

    def test_old_program_schedules_as_weekdays(self, test_scheduler):
        """Старая программа (без новых полей) планируется как weekdays."""
        test_scheduler.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        # Создаём программу в старом формате
        prog = test_scheduler.db.create_program(
            {
                "name": "Legacy",
                "time": "06:00",
                "days": [0, 2, 4],
                "zones": [1],
                # Новые поля не указаны
            }
        )

        test_scheduler.schedule_program(prog["id"], prog)

        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f"program:{prog['id']}:" in j.id]

        assert len(prog_jobs) > 0

        # Проверяем что это CronTrigger (как для weekdays)
        from apscheduler.triggers.cron import CronTrigger

        job = prog_jobs[0]
        assert isinstance(job.trigger, CronTrigger)
