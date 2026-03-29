"""Deep tests for MQTT API routes."""
import json
import pytest
from unittest.mock import patch, MagicMock


class TestMQTTServersAPI:
    """Tests for /api/mqtt/servers endpoints."""

    def test_list_mqtt_servers(self, admin_client):
        resp = admin_client.get('/api/mqtt/servers')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'servers' in data

    def test_create_mqtt_server(self, admin_client):
        resp = admin_client.post('/api/mqtt/servers',
                                 data=json.dumps({
                                     'name': 'Test MQTT',
                                     'host': '192.168.1.100',
                                     'port': 1883,
                                 }),
                                 content_type='application/json')
        assert resp.status_code in (200, 201)

    def test_update_mqtt_server(self, admin_client, app):
        app.db.create_mqtt_server({'name': 'S1', 'host': '1.2.3.4', 'port': 1883, 'enabled': 1})
        servers = app.db.get_mqtt_servers()
        if servers:
            sid = servers[0]['id']
            resp = admin_client.put(f'/api/mqtt/servers/{sid}',
                                    data=json.dumps({'name': 'S1 Updated', 'host': '5.6.7.8'}),
                                    content_type='application/json')
            assert resp.status_code == 200

    def test_delete_mqtt_server(self, admin_client, app):
        app.db.create_mqtt_server({'name': 'S2', 'host': '1.2.3.4', 'port': 1883, 'enabled': 1})
        servers = app.db.get_mqtt_servers()
        if servers:
            sid = servers[0]['id']
            resp = admin_client.delete(f'/api/mqtt/servers/{sid}')
            assert resp.status_code in (200, 204)


class TestMQTTZoneControl:
    """Tests for MQTT zone start/stop."""

    def test_mqtt_start_zone(self, admin_client, app):
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1,
                            'topic': '/devices/test/K1', 'mqtt_server_id': 1})
        app.db.create_mqtt_server({'name': 'S1', 'host': '127.0.0.1', 'port': 1883, 'enabled': 1})
        zones = app.db.get_zones()
        zid = zones[0]['id']
        with patch('services.mqtt_pub.publish_mqtt_value', return_value=True):
            resp = admin_client.post(f'/api/zones/{zid}/mqtt/start')
        assert resp.status_code == 200

    def test_mqtt_stop_zone(self, admin_client, app):
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1,
                            'topic': '/devices/test/K1', 'mqtt_server_id': 1})
        app.db.create_mqtt_server({'name': 'S1', 'host': '127.0.0.1', 'port': 1883, 'enabled': 1})
        zones = app.db.get_zones()
        zid = zones[0]['id']
        with patch('services.mqtt_pub.publish_mqtt_value', return_value=True):
            resp = admin_client.post(f'/api/zones/{zid}/mqtt/stop')
        assert resp.status_code == 200
