"""Tests for Programs v2: new fields (type, schedule_type, interval_days, even_odd, color, enabled, extra_times).

TDD approach: tests written BEFORE implementation.
All tests use @pytest.mark.xfail for not-yet-implemented features.
"""
import pytest
import os

os.environ['TESTING'] = '1'


class TestProgramType:
    """Tests for program type field: time-based / smart."""

    @pytest.mark.xfail(reason="Not yet implemented: type field")
    def test_create_program_with_type_time_based(self, test_db):
        """Программа с type='time-based' создаётся и возвращает type."""
        prog = test_db.create_program({
            'name': 'Time-Based Program',
            'time': '06:00',
            'days': [0, 2, 4],
            'zones': [1],
            'type': 'time-based'
        })
        
        assert prog is not None
        assert prog['type'] == 'time-based'

    @pytest.mark.xfail(reason="Not yet implemented: type field")
    def test_create_program_with_type_smart(self, test_db):
        """Программа с type='smart' создаётся и возвращает type."""
        prog = test_db.create_program({
            'name': 'Smart Program',
            'time': '06:00',
            'days': [0, 2, 4],
            'zones': [1],
            'type': 'smart'
        })
        
        assert prog is not None
        assert prog['type'] == 'smart'

    @pytest.mark.xfail(reason="Not yet implemented: type field default")
    def test_create_program_without_type_defaults_to_time_based(self, test_db):
        """Программа без type получает дефолт 'time-based'."""
        prog = test_db.create_program({
            'name': 'Default Type',
            'time': '06:00',
            'days': [0],
            'zones': [1]
        })
        
        assert prog is not None
        assert prog['type'] == 'time-based'

    @pytest.mark.xfail(reason="Not yet implemented: type field update")
    def test_update_program_type(self, test_db):
        """Обновление type программы."""
        prog = test_db.create_program({
            'name': 'Test',
            'time': '06:00',
            'days': [0],
            'zones': [1],
            'type': 'time-based'
        })
        
        updated = test_db.update_program(prog['id'], {'type': 'smart'})
        
        assert updated is not None
        assert updated['type'] == 'smart'


class TestScheduleTypeWeekdays:
    """Tests for schedule_type='weekdays' (standard days of week)."""

    @pytest.mark.xfail(reason="Not yet implemented: schedule_type field")
    def test_create_program_with_schedule_type_weekdays(self, test_db):
        """Программа с schedule_type='weekdays' и days=[0,2,4]."""
        prog = test_db.create_program({
            'name': 'Weekdays Program',
            'time': '06:00',
            'schedule_type': 'weekdays',
            'days': [0, 2, 4],  # Пн, Ср, Пт
            'zones': [1]
        })
        
        assert prog is not None
        assert prog['schedule_type'] == 'weekdays'
        assert prog['days'] == [0, 2, 4]

    @pytest.mark.xfail(reason="Not yet implemented: schedule_type default")
    def test_create_program_without_schedule_type_defaults_to_weekdays(self, test_db):
        """Программа без schedule_type получает дефолт 'weekdays'."""
        prog = test_db.create_program({
            'name': 'Default Schedule',
            'time': '06:00',
            'days': [0, 1, 2],
            'zones': [1]
        })
        
        assert prog is not None
        assert prog['schedule_type'] == 'weekdays'


class TestScheduleTypeEvenOdd:
    """Tests for schedule_type='even-odd' (even/odd days of month)."""

    @pytest.mark.xfail(reason="Not yet implemented: even-odd schedule")
    def test_create_program_with_schedule_type_even_odd_even(self, test_db):
        """Программа с schedule_type='even-odd' + even_odd='even'."""
        prog = test_db.create_program({
            'name': 'Even Days',
            'time': '06:00',
            'schedule_type': 'even-odd',
            'even_odd': 'even',
            'days': [],  # days не используются для even-odd
            'zones': [1]
        })
        
        assert prog is not None
        assert prog['schedule_type'] == 'even-odd'
        assert prog['even_odd'] == 'even'

    @pytest.mark.xfail(reason="Not yet implemented: even-odd schedule")
    def test_create_program_with_schedule_type_even_odd_odd(self, test_db):
        """Программа с schedule_type='even-odd' + even_odd='odd'."""
        prog = test_db.create_program({
            'name': 'Odd Days',
            'time': '06:00',
            'schedule_type': 'even-odd',
            'even_odd': 'odd',
            'days': [],
            'zones': [1]
        })
        
        assert prog is not None
        assert prog['schedule_type'] == 'even-odd'
        assert prog['even_odd'] == 'odd'

    @pytest.mark.xfail(reason="Not yet implemented: even-odd update")
    def test_update_program_schedule_type_to_even_odd(self, test_db):
        """Смена расписания с weekdays на even-odd."""
        prog = test_db.create_program({
            'name': 'Test',
            'time': '06:00',
            'schedule_type': 'weekdays',
            'days': [0, 1, 2],
            'zones': [1]
        })
        
        updated = test_db.update_program(prog['id'], {
            'schedule_type': 'even-odd',
            'even_odd': 'even',
            'days': []
        })
        
        assert updated is not None
        assert updated['schedule_type'] == 'even-odd'
        assert updated['even_odd'] == 'even'


class TestScheduleTypeInterval:
    """Tests for schedule_type='interval' (every N days)."""

    @pytest.mark.xfail(reason="Not yet implemented: interval schedule")
    def test_create_program_with_schedule_type_interval(self, test_db):
        """Программа с schedule_type='interval' + interval_days=3."""
        prog = test_db.create_program({
            'name': 'Every 3 Days',
            'time': '06:00',
            'schedule_type': 'interval',
            'interval_days': 3,
            'days': [],  # days не используются для interval
            'zones': [1]
        })
        
        assert prog is not None
        assert prog['schedule_type'] == 'interval'
        assert prog['interval_days'] == 3

    @pytest.mark.xfail(reason="Not yet implemented: interval schedule")
    def test_create_program_with_interval_days_1(self, test_db):
        """Программа с interval_days=1 (каждый день)."""
        prog = test_db.create_program({
            'name': 'Every Day',
            'time': '06:00',
            'schedule_type': 'interval',
            'interval_days': 1,
            'days': [],
            'zones': [1]
        })
        
        assert prog is not None
        assert prog['interval_days'] == 1

    @pytest.mark.xfail(reason="Not yet implemented: interval schedule")
    def test_update_program_schedule_type_to_interval(self, test_db):
        """Смена расписания на interval."""
        prog = test_db.create_program({
            'name': 'Test',
            'time': '06:00',
            'schedule_type': 'weekdays',
            'days': [0, 1, 2],
            'zones': [1]
        })
        
        updated = test_db.update_program(prog['id'], {
            'schedule_type': 'interval',
            'interval_days': 5,
            'days': []
        })
        
        assert updated is not None
        assert updated['schedule_type'] == 'interval'
        assert updated['interval_days'] == 5


class TestProgramColor:
    """Tests for program color field."""

    @pytest.mark.xfail(reason="Not yet implemented: color field")
    def test_create_program_with_color(self, test_db):
        """Программа с color создаётся корректно."""
        prog = test_db.create_program({
            'name': 'Blue Program',
            'time': '06:00',
            'days': [0],
            'zones': [1],
            'color': '#42a5f5'
        })
        
        assert prog is not None
        assert prog['color'] == '#42a5f5'

    @pytest.mark.xfail(reason="Not yet implemented: color default")
    def test_create_program_without_color_defaults(self, test_db):
        """Программа без color получает дефолтный цвет."""
        prog = test_db.create_program({
            'name': 'Default Color',
            'time': '06:00',
            'days': [0],
            'zones': [1]
        })
        
        assert prog is not None
        assert 'color' in prog
        assert prog['color'] == '#42a5f5'  # дефолтный синий

    @pytest.mark.xfail(reason="Not yet implemented: color update")
    def test_update_program_color(self, test_db):
        """Обновление цвета программы."""
        prog = test_db.create_program({
            'name': 'Test',
            'time': '06:00',
            'days': [0],
            'zones': [1],
            'color': '#42a5f5'
        })
        
        updated = test_db.update_program(prog['id'], {'color': '#66bb6a'})
        
        assert updated is not None
        assert updated['color'] == '#66bb6a'


class TestProgramEnabled:
    """Tests for enabled field (program on/off)."""

    @pytest.mark.xfail(reason="Not yet implemented: enabled field")
    def test_create_program_with_enabled_true(self, test_db):
        """Программа с enabled=1 создаётся."""
        prog = test_db.create_program({
            'name': 'Enabled Program',
            'time': '06:00',
            'days': [0],
            'zones': [1],
            'enabled': 1
        })
        
        assert prog is not None
        assert prog['enabled'] == 1

    @pytest.mark.xfail(reason="Not yet implemented: enabled field")
    def test_create_program_with_enabled_false(self, test_db):
        """Программа с enabled=0 создаётся."""
        prog = test_db.create_program({
            'name': 'Disabled Program',
            'time': '06:00',
            'days': [0],
            'zones': [1],
            'enabled': 0
        })
        
        assert prog is not None
        assert prog['enabled'] == 0

    @pytest.mark.xfail(reason="Not yet implemented: enabled default")
    def test_create_program_without_enabled_defaults_to_true(self, test_db):
        """Программа без enabled получает дефолт 1 (включена)."""
        prog = test_db.create_program({
            'name': 'Default Enabled',
            'time': '06:00',
            'days': [0],
            'zones': [1]
        })
        
        assert prog is not None
        assert prog['enabled'] == 1

    @pytest.mark.xfail(reason="Not yet implemented: enabled update")
    def test_update_program_enabled(self, test_db):
        """Обновление enabled программы."""
        prog = test_db.create_program({
            'name': 'Test',
            'time': '06:00',
            'days': [0],
            'zones': [1],
            'enabled': 1
        })
        
        updated = test_db.update_program(prog['id'], {'enabled': 0})
        
        assert updated is not None
        assert updated['enabled'] == 0
        
        # Toggle обратно
        updated2 = test_db.update_program(prog['id'], {'enabled': 1})
        assert updated2['enabled'] == 1


class TestExtraTimes:
    """Tests for extra_times field (multiple start times)."""

    @pytest.mark.xfail(reason="Not yet implemented: extra_times field")
    def test_create_program_with_extra_times(self, test_db):
        """Программа с extra_times создаётся."""
        prog = test_db.create_program({
            'name': 'Multi-Start',
            'time': '06:00',
            'extra_times': ['12:00', '18:00'],
            'days': [0, 2, 4],
            'zones': [1]
        })
        
        assert prog is not None
        assert prog['extra_times'] == ['12:00', '18:00']

    @pytest.mark.xfail(reason="Not yet implemented: extra_times default")
    def test_create_program_without_extra_times_defaults_to_empty(self, test_db):
        """Программа без extra_times получает пустой массив."""
        prog = test_db.create_program({
            'name': 'Single Start',
            'time': '06:00',
            'days': [0],
            'zones': [1]
        })
        
        assert prog is not None
        assert 'extra_times' in prog
        assert prog['extra_times'] == []

    @pytest.mark.xfail(reason="Not yet implemented: extra_times update")
    def test_update_program_extra_times(self, test_db):
        """Обновление extra_times программы."""
        prog = test_db.create_program({
            'name': 'Test',
            'time': '06:00',
            'extra_times': [],
            'days': [0],
            'zones': [1]
        })
        
        updated = test_db.update_program(prog['id'], {
            'extra_times': ['10:00', '14:00', '18:00']
        })
        
        assert updated is not None
        assert len(updated['extra_times']) == 3
        assert '10:00' in updated['extra_times']

    @pytest.mark.xfail(reason="Not yet implemented: extra_times clear")
    def test_clear_extra_times(self, test_db):
        """Очистка extra_times (установка в пустой массив)."""
        prog = test_db.create_program({
            'name': 'Test',
            'time': '06:00',
            'extra_times': ['12:00', '18:00'],
            'days': [0],
            'zones': [1]
        })
        
        updated = test_db.update_program(prog['id'], {'extra_times': []})
        
        assert updated is not None
        assert updated['extra_times'] == []


class TestGetProgramsReturnsNewFields:
    """Tests ensuring get_program(s) returns all new fields."""

    @pytest.mark.xfail(reason="Not yet implemented: new fields in get_programs")
    def test_get_programs_returns_new_fields(self, test_db):
        """get_programs() возвращает все новые поля v2."""
        prog = test_db.create_program({
            'name': 'Full v2 Program',
            'time': '06:00',
            'type': 'smart',
            'schedule_type': 'interval',
            'interval_days': 2,
            'color': '#ffa726',
            'enabled': 1,
            'extra_times': ['18:00'],
            'days': [],
            'zones': [1]
        })
        
        all_progs = test_db.get_programs()
        found = next((p for p in all_progs if p['id'] == prog['id']), None)
        
        assert found is not None
        assert found['type'] == 'smart'
        assert found['schedule_type'] == 'interval'
        assert found['interval_days'] == 2
        assert found['color'] == '#ffa726'
        assert found['enabled'] == 1
        assert found['extra_times'] == ['18:00']

    @pytest.mark.xfail(reason="Not yet implemented: new fields in get_program")
    def test_get_program_by_id_returns_new_fields(self, test_db):
        """get_program(id) возвращает все новые поля v2."""
        prog = test_db.create_program({
            'name': 'v2 Program',
            'time': '06:00',
            'type': 'time-based',
            'schedule_type': 'even-odd',
            'even_odd': 'even',
            'color': '#ef5350',
            'enabled': 0,
            'extra_times': [],
            'days': [],
            'zones': [1]
        })
        
        fetched = test_db.get_program(prog['id'])
        
        assert fetched is not None
        assert fetched['type'] == 'time-based'
        assert fetched['schedule_type'] == 'even-odd'
        assert fetched['even_odd'] == 'even'
        assert fetched['color'] == '#ef5350'
        assert fetched['enabled'] == 0


class TestDefaultValuesForExistingPrograms:
    """Tests ensuring existing programs get correct defaults after migration."""

    @pytest.mark.xfail(reason="Not yet implemented: migration defaults")
    def test_existing_program_gets_default_values(self, test_db):
        """Существующая программа (без новых полей) получает дефолты после миграции.
        
        Симулируем старую программу через прямой SQL INSERT без новых полей,
        затем проверяем что get_program возвращает дефолтные значения.
        """
        import sqlite3
        import json
        
        # Прямой INSERT в старом формате (без новых полей)
        with sqlite3.connect(test_db.db_path) as conn:
            cursor = conn.execute('''
                INSERT INTO programs (name, time, days, zones)
                VALUES (?, ?, ?, ?)
            ''', ('Legacy Program', '06:00', json.dumps([0, 2, 4]), json.dumps([1])))
            legacy_id = cursor.lastrowid
            conn.commit()
        
        # Теперь читаем через репозиторий
        prog = test_db.get_program(legacy_id)
        
        assert prog is not None
        assert prog['type'] == 'time-based'
        assert prog['schedule_type'] == 'weekdays'
        assert prog['color'] == '#42a5f5'
        assert prog['enabled'] == 1
        assert prog['extra_times'] == []
        assert prog['interval_days'] is None
        assert prog['even_odd'] is None


class TestDuplicateProgram:
    """Tests for duplicate_program method (if added to repository)."""

    @pytest.mark.xfail(reason="Not yet implemented: duplicate_program method")
    def test_duplicate_program(self, test_db):
        """Дублирование программы создаёт копию со всеми полями."""
        original = test_db.create_program({
            'name': 'Original',
            'time': '06:00',
            'type': 'smart',
            'schedule_type': 'interval',
            'interval_days': 3,
            'color': '#9c27b0',
            'enabled': 1,
            'extra_times': ['18:00'],
            'days': [],
            'zones': [1, 2]
        })
        
        # Предполагаем метод duplicate_program(program_id)
        duplicate = test_db.programs.duplicate_program(original['id'])
        
        assert duplicate is not None
        assert duplicate['id'] != original['id']
        assert duplicate['name'] == 'Original (копия)'
        assert duplicate['time'] == original['time']
        assert duplicate['type'] == original['type']
        assert duplicate['schedule_type'] == original['schedule_type']
        assert duplicate['interval_days'] == original['interval_days']
        assert duplicate['color'] == original['color']
        assert duplicate['enabled'] == original['enabled']
        assert duplicate['extra_times'] == original['extra_times']
        assert duplicate['zones'] == original['zones']

    @pytest.mark.xfail(reason="Not yet implemented: duplicate_program not found")
    def test_duplicate_program_not_found(self, test_db):
        """Дублирование несуществующей программы возвращает None."""
        duplicate = test_db.programs.duplicate_program(99999)
        assert duplicate is None


class TestBackwardCompatibility:
    """Tests ensuring old programs continue to work."""

    @pytest.mark.xfail(reason="Not yet implemented: backward compatibility")
    def test_old_program_format_still_works(self, test_db):
        """Старая программа (без новых полей v2) создаётся и работает."""
        prog = test_db.create_program({
            'name': 'Legacy Program',
            'time': '06:00',
            'days': [0, 2, 4],
            'zones': [1, 2]
            # Новые поля не указаны
        })
        
        assert prog is not None
        assert prog['name'] == 'Legacy Program'
        assert prog['time'] == '06:00'
        assert prog['days'] == [0, 2, 4]
        assert prog['zones'] == [1, 2]
        
        # Новые поля должны быть с дефолтами
        assert prog['type'] == 'time-based'
        assert prog['schedule_type'] == 'weekdays'
        assert prog['enabled'] == 1
        assert prog['extra_times'] == []

    @pytest.mark.xfail(reason="Not yet implemented: backward compatibility update")
    def test_update_old_program_with_new_fields(self, test_db):
        """Обновление старой программы новыми полями работает."""
        prog = test_db.create_program({
            'name': 'Old',
            'time': '06:00',
            'days': [0],
            'zones': [1]
        })
        
        updated = test_db.update_program(prog['id'], {
            'type': 'smart',
            'color': '#ff5722'
        })
        
        assert updated is not None
        assert updated['type'] == 'smart'
        assert updated['color'] == '#ff5722'
        # Старые поля не трогались
        assert updated['name'] == 'Old'
        assert updated['time'] == '06:00'
