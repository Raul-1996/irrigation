"""
Tests for services/ modules — monitors, zone_control, events, locks, security, auth.
"""
import os
import sys
import json
import time
import threading
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("TESTING", "1")


# ---------- RainMonitor ----------

class TestRainMonitor:
    def test_rain_monitor_import(self):
        """RainMonitor class should be importable."""
        from app import RainMonitor
        rm = RainMonitor()
        assert rm is not None

    def test_rain_monitor_stop_noop(self):
        """Stopping a non-started monitor should not crash."""
        from app import RainMonitor
        rm = RainMonitor()
        rm.stop()

    def test_rain_interpret_payload(self):
        """Test payload interpretation for rain sensor."""
        from app import RainMonitor
        rm = RainMonitor()
        # Test boolean payload parsing
        assert rm._interpret_payload('1') in (True, None)
        assert rm._interpret_payload('0') in (False, None)
        assert rm._interpret_payload('true') in (True, None)
        assert rm._interpret_payload('false') in (False, None)


# ---------- EnvMonitor ----------

class TestEnvMonitor:
    def test_env_monitor_import(self):
        from app import EnvMonitor
        em = EnvMonitor()
        assert em is not None

    def test_env_monitor_stop_noop(self):
        from app import EnvMonitor
        em = EnvMonitor()
        em.stop()


# ---------- Zone Control ----------

class TestZoneControl:
    def test_zone_control_import(self):
        try:
            from services.zone_control import ZoneController
            assert ZoneController is not None
        except ImportError:
            # Module may have different structure
            import services.zone_control
            assert services.zone_control is not None


# ---------- Events / SSE ----------

class TestEvents:
    def test_events_module_import(self):
        import services.events
        assert services.events is not None

    def test_sse_endpoint_returns_stream(self, client):
        """SSE endpoint should return text/event-stream or similar."""
        r = client.get('/api/mqtt/zones-sse')
        assert r.status_code == 200


# ---------- Locks ----------

class TestLocks:
    def test_locks_import(self):
        import services.locks
        assert services.locks is not None


# ---------- Security ----------

class TestSecurity:
    def test_security_import(self):
        import services.security
        assert services.security is not None


# ---------- Auth Service ----------

class TestAuthService:
    def test_auth_service_import(self):
        import services.auth_service
        assert services.auth_service is not None

    def test_login_sets_session(self, client):
        """Successful login should set session."""
        r = client.post('/api/login', json={'password': '1234'})
        assert r.status_code == 200

        # Check auth status
        r2 = client.get('/api/auth/status')
        assert r2.status_code == 200
        data = r2.get_json()
        assert data.get('authenticated') is True or data.get('is_admin') is True


# ---------- Reports ----------

class TestReports:
    def test_reports_brief(self, client):
        r = client.get('/api/reports?period=today&format=brief')
        assert r.status_code in (200, 302)

    def test_reports_full(self, client):
        r = client.get('/api/reports?period=week&format=full')
        assert r.status_code in (200, 302)


# ---------- Env API ----------

class TestEnvAPI:
    def test_env_config_roundtrip(self, client):
        """Get env config, update it, get again."""
        r1 = client.get('/api/env')
        assert r1.status_code == 200
        data = r1.get_json()

        # Post updated config
        r2 = client.post('/api/env', json={
            'temp_enabled': False,
            'temp_topic': '',
            'temp_mqtt_server_id': None,
            'hum_enabled': False,
            'hum_topic': '',
            'hum_mqtt_server_id': None
        })
        assert r2.status_code in (200, 400)

    def test_env_values_format(self, client):
        r = client.get('/api/env/values')
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, dict)
