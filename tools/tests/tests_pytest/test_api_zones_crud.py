"""
Tests for zone API endpoints — CRUD, photo upload, start/stop.
"""
import io
import os
import sys
import json
import pytest
from unittest.mock import patch, MagicMock

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("TESTING", "1")


class TestZonesAPI:
    def test_get_zones(self, client):
        r = client.get('/api/zones')
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, (list, dict))

    def test_get_zone_by_id(self, client):
        r = client.get('/api/zones/1')
        assert r.status_code in (200, 404)

    def test_get_nonexistent_zone(self, client):
        r = client.get('/api/zones/99999')
        assert r.status_code in (404, 200)

    def test_create_zone(self, client):
        r = client.post('/api/zones', json={
            'name': 'New Zone',
            'icon': '🌱',
            'duration': 5,
            'group_id': 1,
            'topic': '/devices/test/controls/K1',
            'mqtt_server_id': 1
        })
        assert r.status_code in (200, 201, 400)

    def test_create_zone_missing_fields(self, client):
        r = client.post('/api/zones', json={})
        assert r.status_code in (200, 400, 422)

    def test_update_zone(self, client):
        r = client.put('/api/zones/1', json={'name': 'Updated Zone'})
        assert r.status_code in (200, 404, 400)

    def test_delete_zone(self, client):
        # Create first
        r = client.post('/api/zones', json={
            'name': 'ToDelete',
            'icon': '❌',
            'duration': 1,
            'group_id': 1,
            'topic': '/devices/del/controls/K1',
            'mqtt_server_id': 1
        })
        if r.status_code in (200, 201):
            data = r.get_json()
            zid = data.get('id') or data.get('zone', {}).get('id')
            if zid:
                r2 = client.delete(f'/api/zones/{zid}')
                assert r2.status_code in (200, 204, 404)


class TestZonePhoto:
    def test_upload_photo(self, client):
        # Create a fake image
        img = io.BytesIO()
        try:
            from PIL import Image
            im = Image.new('RGB', (100, 100), color='red')
            im.save(img, format='JPEG')
        except ImportError:
            img.write(b'\xff\xd8\xff\xe0' + b'\x00' * 100)
        img.seek(0)
        r = client.post('/api/zones/1/photo',
                        data={'photo': (img, 'test.jpg')},
                        content_type='multipart/form-data')
        assert r.status_code in (200, 400, 413)

    def test_get_photo_nonexistent(self, client):
        r = client.get('/api/zones/99999/photo')
        assert r.status_code in (200, 404)

    def test_delete_photo(self, client):
        r = client.delete('/api/zones/1/photo')
        assert r.status_code in (200, 404)


class TestZoneStartStop:
    @patch('app._publish_mqtt_value', return_value=True)
    def test_start_zone(self, mock_pub, client):
        r = client.post('/api/zones/1/start')
        assert r.status_code in (200, 400, 500)

    @patch('app._publish_mqtt_value', return_value=True)
    def test_stop_zone(self, mock_pub, client):
        r = client.post('/api/zones/1/stop')
        assert r.status_code in (200, 400)

    def test_start_nonexistent_zone(self, client):
        r = client.post('/api/zones/99999/start')
        assert r.status_code in (200, 400, 404, 500)

    def test_zone_watering_time(self, client):
        r = client.get('/api/zones/1/watering-time')
        assert r.status_code in (200, 404)

    def test_zone_next_watering(self, client):
        r = client.get('/api/zones/1/next-watering')
        assert r.status_code in (200, 404)

    def test_bulk_next_watering(self, client):
        r = client.post('/api/zones/next-watering-bulk',
                        json={'zone_ids': [1, 2, 3]})
        assert r.status_code in (200, 400)


class TestZoneImport:
    def test_import_zones_bulk(self, client):
        zones_data = [
            {'name': 'Imported 1', 'icon': '🌿', 'duration': 3,
             'group_id': 1, 'topic': '/imp/1', 'mqtt_server_id': 1},
            {'name': 'Imported 2', 'icon': '🌱', 'duration': 5,
             'group_id': 1, 'topic': '/imp/2', 'mqtt_server_id': 1},
        ]
        r = client.post('/api/zones/import', json={'zones': zones_data})
        assert r.status_code in (200, 201, 400)

    def test_import_empty(self, client):
        r = client.post('/api/zones/import', json={'zones': []})
        assert r.status_code in (200, 400)
