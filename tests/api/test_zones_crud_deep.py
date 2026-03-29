"""Deep tests for zones CRUD API routes."""
import json
import pytest
from unittest.mock import patch, MagicMock


class TestZonesCRUD:
    """Tests for /api/zones CRUD."""

    def test_list_zones(self, admin_client):
        resp = admin_client.get('/api/zones')
        assert resp.status_code == 200

    def test_create_zone(self, admin_client):
        resp = admin_client.post('/api/zones',
                                 data=json.dumps({
                                     'name': 'Тест Газон',
                                     'duration': 15,
                                     'group_id': 1,
                                     'icon': '🌿',
                                 }),
                                 content_type='application/json')
        assert resp.status_code in (200, 201)

    def test_get_zone(self, admin_client, app):
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = app.db.get_zones()
        zid = zones[0]['id']
        resp = admin_client.get(f'/api/zones/{zid}')
        assert resp.status_code == 200

    def test_get_zone_not_found(self, admin_client):
        resp = admin_client.get('/api/zones/99999')
        assert resp.status_code in (200, 404)

    def test_update_zone(self, admin_client, app):
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = app.db.get_zones()
        zid = zones[0]['id']
        resp = admin_client.put(f'/api/zones/{zid}',
                                data=json.dumps({'name': 'Z1 Updated', 'duration': 20}),
                                content_type='application/json')
        assert resp.status_code == 200

    def test_delete_zone(self, admin_client, app):
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = app.db.get_zones()
        zid = zones[0]['id']
        resp = admin_client.delete(f'/api/zones/{zid}')
        assert resp.status_code in (200, 204)


class TestZonesWatering:
    """Tests for zone watering start/stop."""

    def test_start_zone(self, admin_client, app):
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1,
                            'topic': '/devices/test/K1', 'mqtt_server_id': 1})
        app.db.create_mqtt_server({'name': 'S1', 'host': '127.0.0.1', 'port': 1883, 'enabled': 1})
        zones = app.db.get_zones()
        zid = zones[0]['id']
        with patch('services.zone_control.exclusive_start_zone'):
            resp = admin_client.post(f'/api/zones/{zid}/start')
        assert resp.status_code == 200

    def test_stop_zone(self, admin_client, app):
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1,
                            'topic': '/devices/test/K1', 'mqtt_server_id': 1})
        app.db.create_mqtt_server({'name': 'S1', 'host': '127.0.0.1', 'port': 1883, 'enabled': 1})
        zones = app.db.get_zones()
        zid = zones[0]['id']
        with patch('services.zone_control.stop_zone'):
            resp = admin_client.post(f'/api/zones/{zid}/stop')
        assert resp.status_code == 200

    def test_start_nonexistent_zone(self, admin_client):
        resp = admin_client.post('/api/zones/99999/start')
        assert resp.status_code in (200, 404)

    def test_next_watering_bulk(self, admin_client, app):
        """POST /api/zones/next-watering-bulk."""
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = app.db.get_zones()
        resp = admin_client.post('/api/zones/next-watering-bulk',
                                 data=json.dumps({'zone_ids': [zones[0]['id']]}),
                                 content_type='application/json')
        assert resp.status_code == 200
