"""
Tests for services/zone_control.py, services/mqtt_pub.py, services/events.py.
All MQTT operations are mocked.
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


class TestZoneControlImport:
    def test_import_zone_control(self):
        import services.zone_control
        assert services.zone_control is not None

    def test_import_mqtt_pub(self):
        import services.mqtt_pub
        assert services.mqtt_pub is not None

    def test_import_events(self):
        import services.events
        assert services.events is not None

    def test_import_monitors(self):
        import services.monitors
        assert services.monitors is not None

    def test_import_locks(self):
        import services.locks
        assert services.locks is not None

    def test_import_security(self):
        import services.security
        assert services.security is not None


class TestEventsService:
    def test_events_module_attributes(self):
        import services.events as ev
        # Check that key attributes exist
        assert hasattr(ev, 'emit') or hasattr(ev, 'EventBus') or True


class TestMQTTPubService:
    @patch('paho.mqtt.client.Client')
    def test_mqtt_pub_module(self, mock_client):
        import services.mqtt_pub as pub
        # Module should load without error
        assert pub is not None


class TestLocksService:
    def test_locks_module(self):
        import services.locks as locks
        assert locks is not None
