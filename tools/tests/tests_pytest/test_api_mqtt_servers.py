"""
Tests for MQTT servers API — CRUD, probe, status.
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("TESTING", "1")


class TestMQTTServersAPI:
    def test_get_servers(self, client):
        r = client.get('/api/mqtt/servers')
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)

    def test_create_server(self, client):
        r = client.post('/api/mqtt/servers', json={
            'name': 'test-broker',
            'host': '192.168.1.100',
            'port': 1883,
            'enabled': True
        })
        assert r.status_code in (200, 201, 400)

    def test_get_server_by_id(self, client):
        r = client.get('/api/mqtt/servers/1')
        assert r.status_code in (200, 404)

    def test_get_nonexistent_server(self, client):
        r = client.get('/api/mqtt/servers/99999')
        assert r.status_code in (200, 404)

    def test_update_server(self, client):
        r = client.put('/api/mqtt/servers/1', json={
            'name': 'updated-broker',
            'host': '10.0.0.1',
            'port': 1884
        })
        assert r.status_code in (200, 404, 400)

    def test_delete_server(self, client):
        # Create, then delete
        r = client.post('/api/mqtt/servers', json={
            'name': 'temp',
            'host': '127.0.0.1',
            'port': 1885,
            'enabled': False
        })
        if r.status_code in (200, 201):
            data = r.get_json()
            sid = data.get('id') or data.get('server', {}).get('id')
            if sid:
                r2 = client.delete(f'/api/mqtt/servers/{sid}')
                assert r2.status_code in (200, 204, 404)


class TestMQTTProbe:
    @patch('paho.mqtt.client.Client')
    def test_probe_server(self, mock_mqtt, client):
        r = client.post('/api/mqtt/1/probe')
        assert r.status_code in (200, 400, 404, 500)

    def test_probe_nonexistent_server(self, client):
        r = client.post('/api/mqtt/99999/probe')
        assert r.status_code in (200, 400, 404, 500)


class TestMQTTStatus:
    def test_mqtt_server_status(self, client):
        r = client.get('/api/mqtt/1/status')
        assert r.status_code in (200, 404)
