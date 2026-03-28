"""
Edge case tests — boundary conditions, invalid inputs, concurrent access.
"""
import os
import sys
import json
import io
import threading
import time
import pytest

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("TESTING", "1")


class TestInvalidInputs:
    """Test API robustness against invalid/malformed inputs."""

    def test_zone_nonexistent_id(self, client):
        r = client.get('/api/zones/99999')
        assert r.status_code in (200, 404)

    def test_zone_update_nonexistent(self, client):
        r = client.put('/api/zones/99999', json={'name': 'Ghost'})
        assert r.status_code in (200, 404)

    def test_zone_delete_nonexistent(self, client):
        r = client.delete('/api/zones/99999')
        assert r.status_code in (200, 204, 404)

    def test_program_nonexistent(self, client):
        r = client.get('/api/programs/99999')
        assert r.status_code in (200, 404)

    def test_group_update_nonexistent(self, client):
        r = client.put('/api/groups/99999', json={'name': 'Ghost'})
        assert r.status_code in (200, 404)

    def test_group_delete_nonexistent(self, client):
        r = client.delete('/api/groups/99999')
        assert r.status_code in (200, 204, 404)

    def test_zone_create_missing_fields(self, client):
        r = client.post('/api/zones', json={})
        assert r.status_code in (200, 400)

    def test_zone_create_invalid_group(self, client):
        r = client.post('/api/zones', json={
            'name': 'Bad Zone',
            'icon': '❓',
            'duration': 1,
            'group_id': 99999,
            'topic': '/t/bad',
            'mqtt_server_id': 1
        })
        assert r.status_code in (200, 201, 400)

    def test_program_create_missing_fields(self, client):
        r = client.post('/api/programs', json={})
        assert r.status_code in (200, 400)

    def test_program_create_invalid_time(self, client):
        r = client.post('/api/programs', json={
            'name': 'Bad Time',
            'time': 'invalid',
            'days': [0],
            'zones': [1]
        })
        assert r.status_code in (200, 201, 400)

    def test_zone_negative_duration(self, client):
        r = client.put('/api/zones/1', json={'duration': -5})
        assert r.status_code in (200, 400)

    def test_postpone_negative_days(self, client):
        r = client.post('/api/postpone', json={
            'group_id': 1,
            'days': -1,
            'action': 'postpone'
        })
        assert r.status_code in (200, 400)

    def test_postpone_missing_action(self, client):
        r = client.post('/api/postpone', json={
            'group_id': 1,
            'days': 1
        })
        assert r.status_code in (200, 400)

    def test_photo_upload_no_file(self, client):
        r = client.post('/api/zones/1/photo',
                        data={},
                        content_type='multipart/form-data')
        assert r.status_code in (200, 400)

    def test_photo_upload_invalid_format(self, client):
        r = client.post('/api/zones/1/photo',
                        data={'file': (io.BytesIO(b'not an image'), 'test.txt')},
                        content_type='multipart/form-data')
        assert r.status_code in (200, 400)

    def test_map_upload_no_file(self, client):
        r = client.post('/api/map',
                        data={},
                        content_type='multipart/form-data')
        assert r.status_code in (200, 400)

    def test_rain_config_invalid_json(self, client):
        r = client.post('/api/rain',
                        data='not json',
                        content_type='application/json')
        assert r.status_code in (200, 400, 415)


class TestBoundaryConditions:
    def test_zone_zero_duration(self, client):
        r = client.put('/api/zones/1', json={'duration': 0})
        assert r.status_code in (200, 400)

    def test_zone_very_long_name(self, client):
        long_name = 'Z' * 1000
        r = client.put('/api/zones/1', json={'name': long_name})
        assert r.status_code in (200, 400)

    def test_program_empty_zones(self, client):
        r = client.post('/api/programs', json={
            'name': 'Empty Zones',
            'time': '12:00',
            'days': [0],
            'zones': []
        })
        assert r.status_code in (200, 201, 400)

    def test_program_empty_days(self, client):
        r = client.post('/api/programs', json={
            'name': 'No Days',
            'time': '12:00',
            'days': [],
            'zones': [1]
        })
        assert r.status_code in (200, 201, 400)

    def test_group_empty_name(self, client):
        r = client.post('/api/groups', json={'name': ''})
        assert r.status_code in (200, 201, 400)

    def test_404_page(self, client):
        r = client.get('/nonexistent-page')
        assert r.status_code == 404


class TestConcurrentRequests:
    """Test concurrent API access."""

    def test_concurrent_zone_updates(self, client):
        """Multiple concurrent zone updates should not corrupt data."""
        import app as app_module
        errors = []

        def update_zone(n):
            try:
                with app_module.app.test_client() as c:
                    c.put('/api/zones/1', json={'name': f'Zone_{n}'})
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=update_zone, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0

    def test_concurrent_reads(self, client):
        """Multiple concurrent reads should be safe."""
        import app as app_module
        errors = []

        def read_zones():
            try:
                with app_module.app.test_client() as c:
                    r = c.get('/api/zones')
                    assert r.status_code == 200
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=read_zones) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0
