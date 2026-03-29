"""Tests for /api/mqtt/* endpoints."""
import pytest
import json
import os

os.environ['TESTING'] = '1'


class TestMqttServersAPI:
    def test_list_servers(self, admin_client):
        resp = admin_client.get('/api/mqtt/servers')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert isinstance(data['servers'], list)

    def test_create_server(self, admin_client):
        resp = admin_client.post('/api/mqtt/servers',
            data=json.dumps({
                'name': 'API Server', 'host': '192.168.1.1', 'port': 1883,
            }),
            content_type='application/json')
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['success'] is True

    def test_get_server(self, admin_client, app):
        server = app.db.create_mqtt_server({
            'name': 'Get', 'host': '10.0.0.1', 'port': 1883,
        })
        resp = admin_client.get(f'/api/mqtt/servers/{server["id"]}')
        assert resp.status_code == 200

    def test_get_server_not_found(self, admin_client):
        resp = admin_client.get('/api/mqtt/servers/99999')
        assert resp.status_code == 404

    def test_update_server(self, admin_client, app):
        server = app.db.create_mqtt_server({
            'name': 'Old', 'host': 'h1', 'port': 1883,
        })
        resp = admin_client.put(f'/api/mqtt/servers/{server["id"]}',
            data=json.dumps({'name': 'Updated'}),
            content_type='application/json')
        assert resp.status_code == 200

    def test_delete_server(self, admin_client, app):
        server = app.db.create_mqtt_server({
            'name': 'Del', 'host': 'h1', 'port': 1883,
        })
        resp = admin_client.delete(f'/api/mqtt/servers/{server["id"]}')
        assert resp.status_code == 204


class TestMqttProbeStatus:
    def test_probe_server_not_found(self, admin_client):
        resp = admin_client.post('/api/mqtt/99999/probe',
            content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'items' in data

    def test_probe_existing_server(self, admin_client, app):
        server = app.db.create_mqtt_server({
            'name': 'Probe', 'host': '127.0.0.1', 'port': 1883,
        })
        resp = admin_client.post(f'/api/mqtt/{server["id"]}/probe',
            data=json.dumps({'filter': '#', 'duration': 1}),
            content_type='application/json')
        assert resp.status_code == 200

    def test_status_server_not_found(self, admin_client):
        resp = admin_client.get('/api/mqtt/99999/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['connected'] is False

    def test_status_existing_server(self, admin_client, app):
        server = app.db.create_mqtt_server({
            'name': 'Status', 'host': '127.0.0.1', 'port': 1883,
        })
        resp = admin_client.get(f'/api/mqtt/{server["id"]}/status')
        assert resp.status_code == 200
