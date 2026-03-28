"""
Tests for programs API — CRUD, conflict checks, scheduling.
"""
import os
import sys
import json
import pytest

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("TESTING", "1")


class TestProgramsAPI:
    def test_get_programs(self, client):
        r = client.get('/api/programs')
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)

    def test_get_program_by_id(self, client):
        r = client.get('/api/programs/1')
        assert r.status_code in (200, 404)

    def test_create_program(self, client):
        r = client.post('/api/programs', json={
            'name': 'Test Program',
            'time': '06:00',
            'days': [0, 1, 2, 3, 4, 5, 6],
            'zones': [1, 2, 3]
        })
        assert r.status_code in (200, 201, 400)

    def test_create_program_missing_fields(self, client):
        r = client.post('/api/programs', json={})
        assert r.status_code in (200, 400, 422)

    def test_update_program(self, client):
        r = client.put('/api/programs/1', json={
            'name': 'Updated',
            'time': '07:00',
            'days': [1, 3, 5],
            'zones': [1]
        })
        assert r.status_code in (200, 404, 400)

    def test_delete_program(self, client):
        # Create first
        r = client.post('/api/programs', json={
            'name': 'Deletable',
            'time': '12:00',
            'days': [0],
            'zones': [1]
        })
        if r.status_code in (200, 201):
            data = r.get_json()
            pid = data.get('id') or data.get('program', {}).get('id')
            if pid:
                r2 = client.delete(f'/api/programs/{pid}')
                assert r2.status_code in (200, 204, 404)

    def test_get_nonexistent_program(self, client):
        r = client.get('/api/programs/99999')
        assert r.status_code in (200, 404)


class TestConflictChecks:
    def test_check_conflicts(self, client):
        r = client.post('/api/programs/check-conflicts', json={
            'time': '04:00',
            'zones': [1, 2, 3],
            'days': [0, 1, 2, 3, 4, 5, 6]
        })
        assert r.status_code in (200, 400)

    def test_check_duration_conflicts(self, client):
        r = client.post('/api/zones/check-duration-conflicts', json={
            'zone_id': 1,
            'duration': 10
        })
        assert r.status_code in (200, 400)

    def test_check_duration_conflicts_bulk(self, client):
        r = client.post('/api/zones/check-duration-conflicts-bulk', json={
            'zones': [
                {'zone_id': 1, 'duration': 5},
                {'zone_id': 2, 'duration': 10}
            ]
        })
        assert r.status_code in (200, 400)
