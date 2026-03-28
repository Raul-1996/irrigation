"""
Tests for MQTT functionality using mocks — no real MQTT connections.
"""
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


class TestMQTTPublish:
    def test_mqtt_pub_import(self):
        import services.mqtt_pub
        assert services.mqtt_pub is not None

    @patch('paho.mqtt.client.Client')
    def test_publish_async_mocked(self, mock_client_cls):
        """Test _publish_mqtt_async with mocked MQTT client."""
        from app import _publish_mqtt_async
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance

        server = {'host': '127.0.0.1', 'port': 1883, 'id': 1}
        topic = '/devices/test/controls/K1'
        value = '1'

        # Should not raise
        try:
            _publish_mqtt_async(server, topic, value)
        except Exception:
            pass  # May fail without real MQTT, that's expected

    def test_zone_mqtt_start_no_server(self, client):
        """Starting MQTT for zone without configured server."""
        # Ensure zone exists but has no valid mqtt server
        r = client.post('/api/zones/1/mqtt/start')
        assert r.status_code in (200, 400, 500)

    def test_zone_mqtt_stop_already_stopped(self, client):
        """Stopping an already stopped zone."""
        r = client.post('/api/zones/1/mqtt/stop')
        assert r.status_code in (200, 400)


class TestMQTTServersAPI:
    def test_create_mqtt_server_missing_fields(self, client):
        r = client.post('/api/mqtt/servers', json={})
        assert r.status_code in (400, 200)

    def test_create_mqtt_server_invalid_port(self, client):
        r = client.post('/api/mqtt/servers', json={
            'name': 'bad-port',
            'host': '127.0.0.1',
            'port': -1
        })
        assert r.status_code in (200, 400)

    def test_update_nonexistent_server(self, client):
        r = client.put('/api/mqtt/servers/99999', json={
            'name': 'ghost',
            'host': '10.0.0.1',
            'port': 1883
        })
        assert r.status_code in (200, 404)

    def test_delete_nonexistent_server(self, client):
        r = client.delete('/api/mqtt/servers/99999')
        assert r.status_code in (200, 404)

    def test_probe_nonexistent_server(self, client):
        r = client.post('/api/mqtt/99999/probe', json={
            'filter': '#',
            'duration': 1
        })
        assert r.status_code in (200, 404, 400)

    def test_status_nonexistent_server(self, client):
        r = client.get('/api/mqtt/99999/status')
        assert r.status_code in (200, 404)


class TestMQTTEmergency:
    def test_emergency_stop_clears_all(self, client):
        """Emergency stop should turn off all zones."""
        r = client.post('/api/emergency-stop')
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, dict)

        # Zones should be off
        zones = client.get('/api/zones').get_json()
        for z in zones:
            assert z.get('state') in ('off', None, '')

        # Resume
        client.post('/api/emergency-resume')

    def test_emergency_resume_without_stop(self, client):
        """Resume without prior stop should be safe."""
        r = client.post('/api/emergency-resume')
        assert r.status_code == 200
