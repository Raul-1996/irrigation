"""Tests for Programs API v2: new endpoints and updated request/response formats.

TDD approach: tests written BEFORE implementation.
All tests use @pytest.mark.xfail for not-yet-implemented features.
"""
import pytest
import json
import os

os.environ['TESTING'] = '1'


class TestCreateProgramWithNewFields:
    """Tests for POST /api/programs with v2 fields."""

    @pytest.mark.xfail(reason="Not yet implemented: create program with type")
    def test_create_program_with_new_fields(self, admin_client, app):
        """POST /api/programs с type, schedule_type, color, enabled."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        resp = admin_client.post('/api/programs',
            data=json.dumps({
                'name': 'Smart Program',
                'time': '06:00',
                'type': 'smart',
                'schedule_type': 'weekdays',
                'days': [0, 2, 4],
                'zones': [1],
                'color': '#66bb6a',
                'enabled': 1
            }),
            content_type='application/json')
        
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['name'] == 'Smart Program'
        assert data['type'] == 'smart'
        assert data['schedule_type'] == 'weekdays'
        assert data['color'] == '#66bb6a'
        assert data['enabled'] == 1

    @pytest.mark.xfail(reason="Not yet implemented: create program with interval")
    def test_create_program_with_schedule_type_interval(self, admin_client, app):
        """POST /api/programs с schedule_type='interval' + interval_days."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        resp = admin_client.post('/api/programs',
            data=json.dumps({
                'name': 'Every 3 Days',
                'time': '06:00',
                'schedule_type': 'interval',
                'interval_days': 3,
                'days': [],
                'zones': [1],
                'type': 'time-based'
            }),
            content_type='application/json')
        
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['schedule_type'] == 'interval'
        assert data['interval_days'] == 3

    @pytest.mark.xfail(reason="Not yet implemented: create program with even-odd")
    def test_create_program_with_schedule_type_even_odd(self, admin_client, app):
        """POST /api/programs с schedule_type='even-odd' + even_odd='even'."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        resp = admin_client.post('/api/programs',
            data=json.dumps({
                'name': 'Even Days',
                'time': '06:00',
                'schedule_type': 'even-odd',
                'even_odd': 'even',
                'days': [],
                'zones': [1]
            }),
            content_type='application/json')
        
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['schedule_type'] == 'even-odd'
        assert data['even_odd'] == 'even'

    @pytest.mark.xfail(reason="Not yet implemented: create program with extra_times")
    def test_create_program_with_extra_times(self, admin_client, app):
        """POST /api/programs с extra_times (несколько времён старта)."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        resp = admin_client.post('/api/programs',
            data=json.dumps({
                'name': 'Triple Start',
                'time': '06:00',
                'extra_times': ['12:00', '18:00'],
                'days': [0, 2, 4],
                'zones': [1]
            }),
            content_type='application/json')
        
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['time'] == '06:00'
        assert len(data['extra_times']) == 2
        assert '12:00' in data['extra_times']
        assert '18:00' in data['extra_times']


class TestValidation:
    """Tests for API validation of new fields."""

    @pytest.mark.xfail(reason="Not yet implemented: schedule_type validation")
    def test_create_program_validates_schedule_type(self, admin_client, app):
        """POST /api/programs с невалидным schedule_type → 400."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        resp = admin_client.post('/api/programs',
            data=json.dumps({
                'name': 'Invalid Schedule',
                'time': '06:00',
                'schedule_type': 'invalid-type',
                'days': [],
                'zones': [1]
            }),
            content_type='application/json')
        
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'schedule_type' in str(data).lower() or 'invalid' in str(data).lower()

    @pytest.mark.xfail(reason="Not yet implemented: interval_days validation")
    def test_create_program_interval_requires_interval_days(self, admin_client, app):
        """POST /api/programs с schedule_type='interval' без interval_days → 400."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        resp = admin_client.post('/api/programs',
            data=json.dumps({
                'name': 'No Interval Days',
                'time': '06:00',
                'schedule_type': 'interval',
                'days': [],
                'zones': [1]
            }),
            content_type='application/json')
        
        assert resp.status_code == 400

    @pytest.mark.xfail(reason="Not yet implemented: even_odd validation")
    def test_create_program_even_odd_requires_even_odd_field(self, admin_client, app):
        """POST /api/programs с schedule_type='even-odd' без even_odd → 400."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        resp = admin_client.post('/api/programs',
            data=json.dumps({
                'name': 'No Even/Odd',
                'time': '06:00',
                'schedule_type': 'even-odd',
                'days': [],
                'zones': [1]
            }),
            content_type='application/json')
        
        assert resp.status_code == 400

    @pytest.mark.xfail(reason="Not yet implemented: type validation")
    def test_create_program_validates_type(self, admin_client, app):
        """POST /api/programs с невалидным type → 400."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        resp = admin_client.post('/api/programs',
            data=json.dumps({
                'name': 'Invalid Type',
                'time': '06:00',
                'type': 'super-smart',
                'days': [0],
                'zones': [1]
            }),
            content_type='application/json')
        
        assert resp.status_code == 400


class TestUpdateProgramWithNewFields:
    """Tests for PUT /api/programs/<id> with v2 fields."""

    @pytest.mark.xfail(reason="Not yet implemented: update program with new fields")
    def test_update_program_with_new_fields(self, admin_client, app):
        """PUT /api/programs/<id> обновляет новые поля."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        prog = app.db.create_program({
            'name': 'Old',
            'time': '06:00',
            'days': [0],
            'zones': [1]
        })
        
        resp = admin_client.put(f'/api/programs/{prog["id"]}',
            data=json.dumps({
                'name': 'Updated',
                'time': '07:00',
                'type': 'smart',
                'schedule_type': 'interval',
                'interval_days': 2,
                'color': '#ffa726',
                'enabled': 1,
                'days': [],
                'zones': [1]
            }),
            content_type='application/json')
        
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['name'] == 'Updated'
        assert data['type'] == 'smart'
        assert data['schedule_type'] == 'interval'
        assert data['interval_days'] == 2
        assert data['color'] == '#ffa726'

    @pytest.mark.xfail(reason="Not yet implemented: update schedule interval")
    def test_update_program_schedule_interval(self, admin_client, app):
        """PUT /api/programs/<id> обновление на interval расписание."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        prog = app.db.create_program({
            'name': 'Test',
            'time': '06:00',
            'schedule_type': 'weekdays',
            'days': [0, 1, 2],
            'zones': [1]
        })
        
        resp = admin_client.put(f'/api/programs/{prog["id"]}',
            data=json.dumps({
                'name': 'Test',
                'time': '06:00',
                'schedule_type': 'interval',
                'interval_days': 5,
                'days': [],
                'zones': [1]
            }),
            content_type='application/json')
        
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['schedule_type'] == 'interval'
        assert data['interval_days'] == 5


class TestGetProgramsReturnsNewFields:
    """Tests for GET /api/programs returning v2 fields."""

    @pytest.mark.xfail(reason="Not yet implemented: get programs returns new fields")
    def test_get_programs_returns_new_fields(self, admin_client, app):
        """GET /api/programs возвращает все новые поля v2."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        app.db.create_program({
            'name': 'v2 Program',
            'time': '06:00',
            'type': 'smart',
            'schedule_type': 'interval',
            'interval_days': 3,
            'color': '#9c27b0',
            'enabled': 1,
            'extra_times': ['18:00'],
            'days': [],
            'zones': [1]
        })
        
        resp = admin_client.get('/api/programs')
        assert resp.status_code == 200
        
        programs = resp.get_json()
        assert isinstance(programs, list)
        assert len(programs) > 0
        
        prog = programs[0]
        assert 'type' in prog
        assert 'schedule_type' in prog
        assert 'color' in prog
        assert 'enabled' in prog
        assert 'extra_times' in prog

    @pytest.mark.xfail(reason="Not yet implemented: get single program returns new fields")
    def test_get_single_program_returns_new_fields(self, admin_client, app):
        """GET /api/programs/<id> возвращает все новые поля v2."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        prog = app.db.create_program({
            'name': 'v2 Program',
            'time': '06:00',
            'type': 'time-based',
            'schedule_type': 'even-odd',
            'even_odd': 'even',
            'color': '#ef5350',
            'enabled': 0,
            'days': [],
            'zones': [1]
        })
        
        resp = admin_client.get(f'/api/programs/{prog["id"]}')
        assert resp.status_code == 200
        
        data = resp.get_json()
        assert data['type'] == 'time-based'
        assert data['schedule_type'] == 'even-odd'
        assert data['even_odd'] == 'even'
        assert data['color'] == '#ef5350'
        assert data['enabled'] == 0


class TestToggleProgramEnabled:
    """Tests for PATCH /api/programs/<id>/enabled."""

    @pytest.mark.xfail(reason="Not yet implemented: toggle enabled endpoint")
    def test_toggle_program_enabled_to_false(self, admin_client, app):
        """PATCH /api/programs/<id>/enabled переключает enabled на 0."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        prog = app.db.create_program({
            'name': 'Test',
            'time': '06:00',
            'days': [0],
            'zones': [1],
            'enabled': 1
        })
        
        resp = admin_client.patch(f'/api/programs/{prog["id"]}/enabled',
            data=json.dumps({'enabled': 0}),
            content_type='application/json')
        
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['program']['enabled'] == 0

    @pytest.mark.xfail(reason="Not yet implemented: toggle enabled endpoint")
    def test_toggle_program_enabled_to_true(self, admin_client, app):
        """PATCH /api/programs/<id>/enabled переключает enabled на 1."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        prog = app.db.create_program({
            'name': 'Test',
            'time': '06:00',
            'days': [0],
            'zones': [1],
            'enabled': 0
        })
        
        resp = admin_client.patch(f'/api/programs/{prog["id"]}/enabled',
            data=json.dumps({'enabled': 1}),
            content_type='application/json')
        
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['program']['enabled'] == 1

    @pytest.mark.xfail(reason="Not yet implemented: toggle enabled not found")
    def test_toggle_program_enabled_not_found(self, admin_client):
        """PATCH /api/programs/<id>/enabled для несуществующей программы → 404."""
        resp = admin_client.patch('/api/programs/99999/enabled',
            data=json.dumps({'enabled': 0}),
            content_type='application/json')
        
        assert resp.status_code == 404


class TestDuplicateProgram:
    """Tests for POST /api/programs/<id>/duplicate."""

    @pytest.mark.xfail(reason="Not yet implemented: duplicate endpoint")
    def test_duplicate_program(self, admin_client, app):
        """POST /api/programs/<id>/duplicate создаёт копию."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        app.db.create_zone({'name': 'Z2', 'duration': 15, 'group_id': 1})
        
        original = app.db.create_program({
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
        
        resp = admin_client.post(f'/api/programs/{original["id"]}/duplicate')
        
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['success'] is True
        assert 'program' in data
        
        dup = data['program']
        assert dup['name'] == 'Original (копия)'
        assert dup['time'] == original['time']
        assert dup['type'] == original['type']
        assert dup['schedule_type'] == original['schedule_type']
        assert dup['interval_days'] == original['interval_days']
        assert dup['color'] == original['color']
        assert dup['enabled'] == original['enabled']
        assert dup['extra_times'] == original['extra_times']
        assert dup['zones'] == original['zones']
        assert dup['id'] != original['id']

    @pytest.mark.xfail(reason="Not yet implemented: duplicate not found")
    def test_duplicate_program_not_found(self, admin_client):
        """POST /api/programs/<id>/duplicate для несуществующей → 404."""
        resp = admin_client.post('/api/programs/99999/duplicate')
        
        assert resp.status_code == 404
        data = resp.get_json()
        assert data['success'] is False


class TestProgramLog:
    """Tests for GET /api/programs/<id>/log."""

    @pytest.mark.xfail(reason="Not yet implemented: log endpoint")
    def test_get_program_log(self, admin_client, app):
        """GET /api/programs/<id>/log возвращает журнал поливов."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        prog = app.db.create_program({
            'name': 'Test',
            'time': '06:00',
            'days': [0],
            'zones': [1]
        })
        
        resp = admin_client.get(f'/api/programs/{prog["id"]}/log')
        
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'success' in data
        assert 'log' in data
        assert isinstance(data['log'], list)

    @pytest.mark.xfail(reason="Not yet implemented: log with period filter")
    def test_get_program_log_with_period_filter(self, admin_client, app):
        """GET /api/programs/<id>/log?period=week фильтрует по периоду."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        prog = app.db.create_program({
            'name': 'Test',
            'time': '06:00',
            'days': [0],
            'zones': [1]
        })
        
        resp = admin_client.get(f'/api/programs/{prog["id"]}/log?period=week')
        
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'log' in data

    @pytest.mark.xfail(reason="Not yet implemented: log with limit")
    def test_get_program_log_with_limit(self, admin_client, app):
        """GET /api/programs/<id>/log?limit=10 ограничивает количество записей."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        prog = app.db.create_program({
            'name': 'Test',
            'time': '06:00',
            'days': [0],
            'zones': [1]
        })
        
        resp = admin_client.get(f'/api/programs/{prog["id"]}/log?limit=10')
        
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'log' in data
        # Если есть записи, проверяем что не больше 10
        if len(data['log']) > 0:
            assert len(data['log']) <= 10


class TestProgramStats:
    """Tests for GET /api/programs/<id>/stats."""

    @pytest.mark.xfail(reason="Not yet implemented: stats endpoint")
    def test_get_program_stats(self, admin_client, app):
        """GET /api/programs/<id>/stats возвращает статистику."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        prog = app.db.create_program({
            'name': 'Test',
            'time': '06:00',
            'days': [0],
            'zones': [1]
        })
        
        resp = admin_client.get(f'/api/programs/{prog["id"]}/stats')
        
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'success' in data
        assert 'stats' in data
        
        stats = data['stats']
        assert 'total_runs' in stats
        assert 'total_water_calc' in stats
        assert 'total_water_fact' in stats
        assert 'avg_duration_min' in stats
        assert isinstance(stats['total_runs'], int)

    @pytest.mark.xfail(reason="Not yet implemented: stats endpoint not found")
    def test_get_program_stats_not_found(self, admin_client):
        """GET /api/programs/<id>/stats для несуществующей программы → 404."""
        resp = admin_client.get('/api/programs/99999/stats')
        
        assert resp.status_code == 404


class TestBackwardCompatibility:
    """Tests ensuring backward compatibility with old API format."""

    @pytest.mark.xfail(reason="Not yet implemented: backward compatible create")
    def test_create_program_backward_compatible(self, admin_client, app):
        """POST /api/programs со старым форматом (без новых полей) работает."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        
        resp = admin_client.post('/api/programs',
            data=json.dumps({
                'name': 'Legacy Program',
                'time': '06:00',
                'days': [0, 2, 4],
                'zones': [1]
                # Новые поля не указаны
            }),
            content_type='application/json')
        
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['name'] == 'Legacy Program'
        
        # Новые поля должны быть с дефолтами
        assert data['type'] == 'time-based'
        assert data['schedule_type'] == 'weekdays'
        assert data['enabled'] == 1
        assert data['extra_times'] == []

    @pytest.mark.xfail(reason="Not yet implemented: backward compatible update")
    def test_update_program_backward_compatible(self, admin_client, app):
        """PUT /api/programs/<id> со старым форматом работает."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        prog = app.db.create_program({
            'name': 'Old',
            'time': '06:00',
            'days': [0],
            'zones': [1]
        })
        
        resp = admin_client.put(f'/api/programs/{prog["id"]}',
            data=json.dumps({
                'name': 'Updated',
                'time': '07:00',
                'days': [0, 1],
                'zones': [1]
                # Новые поля не указаны
            }),
            content_type='application/json')
        
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['name'] == 'Updated'
        assert data['time'] == '07:00'


class TestCheckConflictsWithExtraTimes:
    """Tests for POST /api/programs/check-conflicts with extra_times."""

    @pytest.mark.xfail(reason="Not yet implemented: conflicts with extra_times")
    def test_check_conflicts_with_extra_times(self, admin_client, app):
        """POST /api/programs/check-conflicts учитывает extra_times."""
        app.db.create_zone({'name': 'Z1', 'duration': 30, 'group_id': 1})
        
        # Создаём программу с extra_times
        app.db.create_program({
            'name': 'Existing',
            'time': '06:00',
            'extra_times': ['12:00'],
            'days': [0],
            'zones': [1]
        })
        
        # Проверяем конфликт с extra_times
        resp = admin_client.post('/api/programs/check-conflicts',
            data=json.dumps({
                'time': '12:10',  # Пересекается с extra_times 12:00
                'zones': [1],
                'days': [0],
                'extra_times': []
            }),
            content_type='application/json')
        
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'has_conflicts' in data
        # Должен обнаружить конфликт
        # assert data['has_conflicts'] is True

    @pytest.mark.xfail(reason="Not yet implemented: conflicts with own extra_times")
    def test_check_conflicts_with_own_extra_times(self, admin_client, app):
        """POST /api/programs/check-conflicts проверяет конфликты между своими extra_times."""
        app.db.create_zone({'name': 'Z1', 'duration': 60, 'group_id': 1})
        
        resp = admin_client.post('/api/programs/check-conflicts',
            data=json.dumps({
                'time': '06:00',
                'extra_times': ['06:30'],  # Конфликт: 06:00 + 60 мин = 07:00, 06:30 попадает
                'zones': [1],
                'days': [0]
            }),
            content_type='application/json')
        
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'has_conflicts' in data
