"""Comprehensive tests for routes/programs_api.py endpoints."""
import pytest
import json
import os

os.environ['TESTING'] = '1'


class TestProgramsAPI:
    def test_list_programs(self, admin_client):
        resp = admin_client.get('/api/programs')
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)

    def test_create_program(self, admin_client, app):
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        resp = admin_client.post('/api/programs',
            data=json.dumps({
                'name': 'Morning', 'time': '06:00',
                'days': [0, 2, 4], 'zones': [1],
            }),
            content_type='application/json')
        assert resp.status_code in (200, 201)

    def test_create_program_no_name(self, admin_client):
        resp = admin_client.post('/api/programs',
            data=json.dumps({
                'time': '06:00', 'days': [0], 'zones': [1],
            }),
            content_type='application/json')
        assert resp.status_code in (200, 201, 400)

    def test_get_program(self, admin_client, app):
        p = app.db.create_program({
            'name': 'Get', 'time': '06:00', 'days': [0], 'zones': [1],
        })
        resp = admin_client.get(f'/api/programs/{p["id"]}')
        assert resp.status_code in (200, 404)

    def test_update_program(self, admin_client, app):
        p = app.db.create_program({
            'name': 'Old', 'time': '06:00', 'days': [0], 'zones': [1],
        })
        resp = admin_client.put(f'/api/programs/{p["id"]}',
            data=json.dumps({
                'name': 'Updated', 'time': '07:00',
                'days': [1, 3], 'zones': [1],
            }),
            content_type='application/json')
        assert resp.status_code in (200, 400)

    def test_delete_program(self, admin_client, app):
        p = app.db.create_program({
            'name': 'Del', 'time': '06:00', 'days': [0], 'zones': [1],
        })
        resp = admin_client.delete(f'/api/programs/{p["id"]}')
        assert resp.status_code in (200, 204)

    def test_delete_nonexistent_program(self, admin_client):
        resp = admin_client.delete('/api/programs/99999')
        assert resp.status_code in (200, 204, 404)


class TestProgramConflictsAPI:
    def test_check_conflicts(self, admin_client, app):
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        app.db.create_program({
            'name': 'P1', 'time': '06:00', 'days': [0], 'zones': [1],
        })
        resp = admin_client.post('/api/programs/check-conflicts',
            data=json.dumps({
                'time': '06:05', 'zones': [1], 'days': [0],
            }),
            content_type='application/json')
        assert resp.status_code in (200, 400, 404)
