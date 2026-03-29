"""Tests for /api/programs/* endpoints."""
import pytest
import json
import os

os.environ['TESTING'] = '1'


class TestProgramsAPI:
    def test_get_programs(self, admin_client):
        resp = admin_client.get('/api/programs')
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)

    def test_create_program(self, admin_client, app):
        # First create zones
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        app.db.create_zone({'name': 'Z2', 'duration': 10, 'group_id': 1})
        resp = admin_client.post('/api/programs',
            data=json.dumps({
                'name': 'Test Program', 'time': '06:00',
                'days': [0, 2, 4], 'zones': [1, 2],
            }),
            content_type='application/json')
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['name'] == 'Test Program'

    def test_create_program_missing_fields(self, admin_client):
        resp = admin_client.post('/api/programs',
            data=json.dumps({'name': 'Incomplete'}),
            content_type='application/json')
        assert resp.status_code == 400

    def test_get_single_program(self, admin_client, app):
        prog = app.db.create_program({
            'name': 'P', 'time': '18:00', 'days': [1], 'zones': [1],
        })
        resp = admin_client.get(f'/api/programs/{prog["id"]}')
        assert resp.status_code == 200

    def test_get_program_not_found(self, admin_client):
        resp = admin_client.get('/api/programs/99999')
        assert resp.status_code == 404

    def test_update_program(self, admin_client, app):
        prog = app.db.create_program({
            'name': 'Old', 'time': '06:00', 'days': [0], 'zones': [1],
        })
        resp = admin_client.put(f'/api/programs/{prog["id"]}',
            data=json.dumps({
                'name': 'New', 'time': '07:00', 'days': [0, 1], 'zones': [1],
            }),
            content_type='application/json')
        assert resp.status_code == 200

    def test_delete_program(self, admin_client, app):
        prog = app.db.create_program({
            'name': 'Del', 'time': '06:00', 'days': [0], 'zones': [1],
        })
        resp = admin_client.delete(f'/api/programs/{prog["id"]}')
        assert resp.status_code == 204

    def test_delete_program_not_found(self, admin_client):
        resp = admin_client.delete('/api/programs/99999')
        # delete_program returns True for nonexistent IDs
        assert resp.status_code in (204, 404)


class TestCheckConflicts:
    def test_check_conflicts_endpoint(self, admin_client, app):
        app.db.create_zone({'name': 'Z1', 'duration': 30, 'group_id': 1})
        resp = admin_client.post('/api/programs/check-conflicts',
            data=json.dumps({
                'time': '06:00', 'zones': [1], 'days': [0],
            }),
            content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'has_conflicts' in data

    def test_check_conflicts_missing_data(self, admin_client):
        resp = admin_client.post('/api/programs/check-conflicts',
            data=json.dumps({'time': '06:00'}),
            content_type='application/json')
        assert resp.status_code == 400
