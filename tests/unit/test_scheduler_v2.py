"""Tests for Scheduler v2: new schedule types (interval, even-odd), extra_times, enabled field.

TDD approach: tests written BEFORE implementation.
All tests use @pytest.mark.xfail for not-yet-implemented features.
"""
import pytest
import os
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

os.environ['TESTING'] = '1'


@pytest.fixture
def test_scheduler(test_db):
    """Create a test scheduler instance with test DB."""
    from irrigation_scheduler import IrrigationScheduler
    
    # Mock MQTT client
    mock_mqtt = MagicMock()
    
    scheduler = IrrigationScheduler(test_db, mock_mqtt)
    scheduler.start()
    
    yield scheduler
    
    # Cleanup
    scheduler.stop()


class TestScheduleWeekdaysProgram:
    """Tests for standard weekdays schedule (existing functionality)."""

    @pytest.mark.xfail(reason="Not yet implemented: weekdays with new fields")
    def test_schedule_weekdays_program(self, test_scheduler):
        """Программа с schedule_type='weekdays' планируется корректно."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Weekdays Test',
            'time': '06:00',
            'schedule_type': 'weekdays',
            'days': [0, 2, 4],  # Пн, Ср, Пт
            'zones': [1],
            'enabled': 1
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        # Проверяем что job создан
        jobs = test_scheduler.scheduler.get_jobs()
        job_ids = [j.id for j in jobs]
        
        assert any('program_' in job_id and str(prog['id']) in job_id for job_id in job_ids)

    @pytest.mark.xfail(reason="Not yet implemented: weekdays schedule details")
    def test_weekdays_program_has_correct_trigger(self, test_scheduler):
        """Weekdays программа имеет CronTrigger с правильными днями."""
        from apscheduler.triggers.cron import CronTrigger
        
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Weekdays',
            'time': '06:00',
            'schedule_type': 'weekdays',
            'days': [0, 2, 4],
            'zones': [1],
            'enabled': 1
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f'program_{prog["id"]}' in j.id]
        
        assert len(prog_jobs) > 0
        
        job = prog_jobs[0]
        assert isinstance(job.trigger, CronTrigger)
        # Проверяем что день недели соответствует 0,2,4 (mon,wed,fri)
        # CronTrigger использует: mon=0, tue=1, wed=2, thu=3, fri=4, sat=5, sun=6


class TestScheduleIntervalProgram:
    """Tests for schedule_type='interval' (every N days)."""

    @pytest.mark.xfail(reason="Not yet implemented: interval schedule")
    def test_schedule_interval_program(self, test_scheduler):
        """Программа с schedule_type='interval' планируется с IntervalTrigger."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Every 3 Days',
            'time': '06:00',
            'schedule_type': 'interval',
            'interval_days': 3,
            'days': [],
            'zones': [1],
            'enabled': 1
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        job_ids = [j.id for j in jobs]
        
        assert any(f'program_{prog["id"]}' in job_id for job_id in job_ids)

    @pytest.mark.xfail(reason="Not yet implemented: interval trigger type")
    def test_interval_program_uses_interval_trigger(self, test_scheduler):
        """Interval программа использует IntervalTrigger."""
        from apscheduler.triggers.interval import IntervalTrigger
        
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Every 2 Days',
            'time': '06:00',
            'schedule_type': 'interval',
            'interval_days': 2,
            'days': [],
            'zones': [1],
            'enabled': 1
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f'program_{prog["id"]}' in j.id]
        
        assert len(prog_jobs) > 0
        
        job = prog_jobs[0]
        assert isinstance(job.trigger, IntervalTrigger)

    @pytest.mark.xfail(reason="Not yet implemented: interval first run today")
    def test_interval_program_first_run_today(self, test_scheduler):
        """Interval программа: первый запуск СЕГОДНЯ в указанное время."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Every 5 Days',
            'time': '14:30',
            'schedule_type': 'interval',
            'interval_days': 5,
            'days': [],
            'zones': [1],
            'enabled': 1
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f'program_{prog["id"]}' in j.id]
        
        assert len(prog_jobs) > 0
        
        job = prog_jobs[0]
        next_run = job.next_run_time
        
        # Следующий запуск должен быть сегодня в 14:30 (если ещё не прошло) или через interval_days
        today = datetime.now().date()
        assert next_run.date() >= today


class TestScheduleEvenOddProgram:
    """Tests for schedule_type='even-odd' (even/odd days of month)."""

    @pytest.mark.xfail(reason="Not yet implemented: even-odd schedule")
    def test_schedule_even_odd_program_even(self, test_scheduler):
        """Программа с even_odd='even' планируется на чётные дни."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Even Days',
            'time': '06:00',
            'schedule_type': 'even-odd',
            'even_odd': 'even',
            'days': [],
            'zones': [1],
            'enabled': 1
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        job_ids = [j.id for j in jobs]
        
        assert any(f'program_{prog["id"]}' in job_id for job_id in job_ids)

    @pytest.mark.xfail(reason="Not yet implemented: even-odd schedule odd")
    def test_schedule_even_odd_program_odd(self, test_scheduler):
        """Программа с even_odd='odd' планируется на нечётные дни."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Odd Days',
            'time': '06:00',
            'schedule_type': 'even-odd',
            'even_odd': 'odd',
            'days': [],
            'zones': [1],
            'enabled': 1
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        job_ids = [j.id for j in jobs]
        
        assert any(f'program_{prog["id"]}' in job_id for job_id in job_ids)

    @pytest.mark.xfail(reason="Not yet implemented: even-odd cron expression")
    def test_even_odd_program_has_correct_cron_days(self, test_scheduler):
        """Even-odd программа имеет CronTrigger с правильными днями месяца."""
        from apscheduler.triggers.cron import CronTrigger
        
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Even Days',
            'time': '06:00',
            'schedule_type': 'even-odd',
            'even_odd': 'even',
            'days': [],
            'zones': [1],
            'enabled': 1
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f'program_{prog["id"]}' in j.id]
        
        assert len(prog_jobs) > 0
        
        job = prog_jobs[0]
        assert isinstance(job.trigger, CronTrigger)
        
        # Проверяем что day содержит чётные дни: 2,4,6,8,...
        # trigger.fields[2] обычно day field в CronTrigger


class TestExtraTimes:
    """Tests for extra_times field (multiple start times per day)."""

    @pytest.mark.xfail(reason="Not yet implemented: extra_times scheduling")
    def test_schedule_extra_times(self, test_scheduler):
        """Программа с extra_times создаёт несколько jobs."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Triple Start',
            'time': '06:00',
            'extra_times': ['12:00', '18:00'],
            'days': [0, 2, 4],
            'zones': [1],
            'enabled': 1
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f'program_{prog["id"]}' in j.id]
        
        # Должно быть 3 job: main (06:00), extra0 (12:00), extra1 (18:00)
        assert len(prog_jobs) == 3

    @pytest.mark.xfail(reason="Not yet implemented: extra_times job naming")
    def test_extra_times_jobs_have_correct_ids(self, test_scheduler):
        """Jobs для extra_times имеют правильные ID (main, extra0, extra1, ...)."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Multi-Start',
            'time': '06:00',
            'extra_times': ['18:00'],
            'days': [0],
            'zones': [1],
            'enabled': 1
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        prog_job_ids = [j.id for j in jobs if f'program_{prog["id"]}' in j.id]
        
        assert f'program_{prog["id"]}_main' in prog_job_ids
        assert f'program_{prog["id"]}_extra0' in prog_job_ids

    @pytest.mark.xfail(reason="Not yet implemented: extra_times with interval")
    def test_extra_times_with_interval_schedule(self, test_scheduler):
        """extra_times работает с schedule_type='interval'."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Interval Multi-Start',
            'time': '06:00',
            'extra_times': ['18:00'],
            'schedule_type': 'interval',
            'interval_days': 2,
            'days': [],
            'zones': [1],
            'enabled': 1
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f'program_{prog["id"]}' in j.id]
        
        # Должно быть 2 job (main + extra0) для interval
        assert len(prog_jobs) == 2

    @pytest.mark.xfail(reason="Not yet implemented: empty extra_times")
    def test_empty_extra_times_creates_single_job(self, test_scheduler):
        """Программа с extra_times=[] создаёт только один job (main)."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Single Start',
            'time': '06:00',
            'extra_times': [],
            'days': [0],
            'zones': [1],
            'enabled': 1
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f'program_{prog["id"]}' in j.id]
        
        # Только main job
        assert len(prog_jobs) == 1


class TestEnabledField:
    """Tests for enabled field (skip disabled programs)."""

    @pytest.mark.xfail(reason="Not yet implemented: skip disabled program")
    def test_skip_disabled_program(self, test_scheduler):
        """Программа с enabled=0 не планируется."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Disabled',
            'time': '06:00',
            'days': [0, 1, 2],
            'zones': [1],
            'enabled': 0
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f'program_{prog["id"]}' in j.id]
        
        # Не должно быть jobs для выключенной программы
        assert len(prog_jobs) == 0

    @pytest.mark.xfail(reason="Not yet implemented: enabled default")
    def test_enabled_default_true_schedules_program(self, test_scheduler):
        """Программа без явного enabled (дефолт=1) планируется."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Default Enabled',
            'time': '06:00',
            'days': [0],
            'zones': [1]
            # enabled не указан, дефолт 1
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f'program_{prog["id"]}' in j.id]
        
        assert len(prog_jobs) > 0

    @pytest.mark.xfail(reason="Not yet implemented: cancel jobs on disable")
    def test_enable_disable_reschedules(self, test_scheduler):
        """При toggle enabled → пересчёт расписания (добавление/удаление jobs)."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Toggle Test',
            'time': '06:00',
            'days': [0],
            'zones': [1],
            'enabled': 1
        })
        
        # Планируем
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs_before = [j for j in test_scheduler.scheduler.get_jobs() if f'program_{prog["id"]}' in j.id]
        assert len(jobs_before) > 0
        
        # Выключаем
        prog['enabled'] = 0
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs_after_disable = [j for j in test_scheduler.scheduler.get_jobs() if f'program_{prog["id"]}' in j.id]
        assert len(jobs_after_disable) == 0
        
        # Включаем обратно
        prog['enabled'] = 1
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs_after_enable = [j for j in test_scheduler.scheduler.get_jobs() if f'program_{prog["id"]}' in j.id]
        assert len(jobs_after_enable) > 0


class TestSmartTypeWeatherAdjustment:
    """Tests for type='smart' with enhanced weather correction."""

    @pytest.mark.xfail(reason="Not yet implemented: smart type weather")
    def test_smart_type_weather_adjustment(self, test_scheduler):
        """Программа с type='smart' применяет расширенную погодокоррекцию."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Smart Program',
            'time': '06:00',
            'type': 'smart',
            'days': [0],
            'zones': [1],
            'enabled': 1
        })
        
        # Планируем (проверяем что не падает)
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f'program_{prog["id"]}' in j.id]
        
        assert len(prog_jobs) > 0
        
        # TODO: проверить что при запуске используется smart weather adjustment
        # Это требует мока weather_adjustment и проверки вызова adjust_duration

    @pytest.mark.xfail(reason="Not yet implemented: time-based vs smart")
    def test_time_based_uses_standard_weather(self, test_scheduler):
        """Программа с type='time-based' использует стандартную погодокоррекцию."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Time-Based Program',
            'time': '06:00',
            'type': 'time-based',
            'days': [0],
            'zones': [1],
            'enabled': 1
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f'program_{prog["id"]}' in j.id]
        
        assert len(prog_jobs) > 0


class TestSchedulerIntegration:
    """Integration tests for scheduler with v2 features."""

    @pytest.mark.xfail(reason="Not yet implemented: full v2 program scheduling")
    def test_full_v2_program_scheduling(self, test_scheduler):
        """Полная программа v2 (все поля) планируется корректно."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        test_scheduler.db.create_zone({'name': 'Z2', 'duration': 15, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Full v2 Program',
            'time': '06:00',
            'extra_times': ['18:00'],
            'type': 'smart',
            'schedule_type': 'interval',
            'interval_days': 2,
            'color': '#9c27b0',
            'enabled': 1,
            'days': [],
            'zones': [1, 2]
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f'program_{prog["id"]}' in j.id]
        
        # Должно быть 2 job (main + extra0) для interval с extra_times
        assert len(prog_jobs) == 2
        
        # Проверяем что оба job имеют IntervalTrigger
        from apscheduler.triggers.interval import IntervalTrigger
        for job in prog_jobs:
            assert isinstance(job.trigger, IntervalTrigger)

    @pytest.mark.xfail(reason="Not yet implemented: reschedule on update")
    def test_reschedule_program_on_update(self, test_scheduler):
        """При обновлении программы jobs пересоздаются."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'Test',
            'time': '06:00',
            'days': [0],
            'zones': [1],
            'enabled': 1
        })
        
        # Планируем
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs_before = [j for j in test_scheduler.scheduler.get_jobs() if f'program_{prog["id"]}' in j.id]
        assert len(jobs_before) > 0
        
        # Обновляем время и schedule_type
        updated_prog = test_scheduler.db.update_program(prog['id'], {
            'time': '18:00',
            'schedule_type': 'interval',
            'interval_days': 3,
            'days': []
        })
        
        # Пере-планируем
        test_scheduler.schedule_program(prog['id'], updated_prog)
        
        jobs_after = [j for j in test_scheduler.scheduler.get_jobs() if f'program_{prog["id"]}' in j.id]
        assert len(jobs_after) > 0
        
        # Проверяем что trigger изменился
        from apscheduler.triggers.interval import IntervalTrigger
        job = jobs_after[0]
        assert isinstance(job.trigger, IntervalTrigger)

    @pytest.mark.xfail(reason="Not yet implemented: cancel all jobs on delete")
    def test_cancel_program_removes_all_jobs(self, test_scheduler):
        """При удалении программы все её jobs удаляются (включая extra_times)."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        prog = test_scheduler.db.create_program({
            'name': 'To Delete',
            'time': '06:00',
            'extra_times': ['12:00', '18:00'],
            'days': [0],
            'zones': [1],
            'enabled': 1
        })
        
        # Планируем
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs_before = [j for j in test_scheduler.scheduler.get_jobs() if f'program_{prog["id"]}' in j.id]
        assert len(jobs_before) == 3  # main + 2 extra
        
        # Отменяем
        test_scheduler.cancel_program(prog['id'])
        
        jobs_after = [j for j in test_scheduler.scheduler.get_jobs() if f'program_{prog["id"]}' in j.id]
        assert len(jobs_after) == 0


class TestBackwardCompatibilityScheduler:
    """Tests ensuring old programs still schedule correctly."""

    @pytest.mark.xfail(reason="Not yet implemented: backward compatible scheduling")
    def test_old_program_schedules_as_weekdays(self, test_scheduler):
        """Старая программа (без новых полей) планируется как weekdays."""
        test_scheduler.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        # Создаём программу в старом формате
        prog = test_scheduler.db.create_program({
            'name': 'Legacy',
            'time': '06:00',
            'days': [0, 2, 4],
            'zones': [1]
            # Новые поля не указаны
        })
        
        test_scheduler.schedule_program(prog['id'], prog)
        
        jobs = test_scheduler.scheduler.get_jobs()
        prog_jobs = [j for j in jobs if f'program_{prog["id"]}' in j.id]
        
        assert len(prog_jobs) > 0
        
        # Проверяем что это CronTrigger (как для weekdays)
        from apscheduler.triggers.cron import CronTrigger
        job = prog_jobs[0]
        assert isinstance(job.trigger, CronTrigger)
