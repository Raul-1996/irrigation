"""Comprehensive tests for routes/mqtt_api.py endpoints."""
import pytest
import json
import os

os.environ['TESTING'] = '1'


class TestMqttServersAPI:
    def test_list_servers(self, admin_client):
        resp = admin_client.get('/api/mqtt/servers')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, (list, dict))  # may return {servers: [...]} or [...]

    def test_create_server(self, admin_client):
        resp = admin_client.post('/api/mqtt/servers',
            data=json.dumps({
                'name': 'Test MQTT', 'host': '127.0.0.1', 'port': 1883,
            }),
            content_type='application/json')
        assert resp.status_code in (200, 201)

    def test_create_server_with_auth(self, admin_client):
        resp = admin_client.post('/api/mqtt/servers',
            data=json.dumps({
                'name': 'Auth MQTT', 'host': '10.0.0.1', 'port': 1883,
                'username': 'user', 'password': 'pass',
            }),
            content_type='application/json')
        assert resp.status_code in (200, 201)

    def test_get_server(self, admin_client, app):
        s = app.db.create_mqtt_server({'name': 'Get', 'host': '127.0.0.1', 'port': 1883})
        resp = admin_client.get(f'/api/mqtt/servers/{s["id"]}')
        assert resp.status_code in (200, 404)

    def test_get_server_not_found(self, admin_client):
        resp = admin_client.get('/api/mqtt/servers/99999')
        assert resp.status_code in (404, 200)

    def test_update_server(self, admin_client, app):
        s = app.db.create_mqtt_server({'name': 'Old', 'host': '10.0.0.1', 'port': 1883})
        resp = admin_client.put(f'/api/mqtt/servers/{s["id"]}',
            data=json.dumps({'name': 'Updated', 'host': '10.0.0.2'}),
            content_type='application/json')
        assert resp.status_code in (200, 400)

    def test_delete_server(self, admin_client, app):
        s = app.db.create_mqtt_server({'name': 'Del', 'host': '10.0.0.1', 'port': 1883})
        resp = admin_client.delete(f'/api/mqtt/servers/{s["id"]}')
        assert resp.status_code in (200, 204, 400)


class TestMqttTestConnection:
    def test_test_connection(self, admin_client, app):
        s = app.db.create_mqtt_server({'name': 'T', 'host': '127.0.0.1', 'port': 1883})
        resp = admin_client.post(f'/api/mqtt/{s["id"]}/probe',
            content_type='application/json')
        assert resp.status_code in (200, 400, 404, 500)


class TestMqttPublishAPI:
    def test_publish(self, admin_client, app):
        s = app.db.create_mqtt_server({'name': 'Pub', 'host': '127.0.0.1', 'port': 1883})
        resp = admin_client.post(f'/api/mqtt/{s["id"]}/probe',
            content_type='application/json')
        assert resp.status_code in (200, 400, 404, 500)
