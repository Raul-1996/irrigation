"""Performance tests: all endpoints < 500ms."""
import pytest
import time
import os

os.environ['TESTING'] = '1'

pytestmark = pytest.mark.slow


class TestResponseTimes:
    def _measure(self, client, method, path, **kwargs):
        t0 = time.time()
        if method == 'GET':
            resp = client.get(path, **kwargs)
        else:
            resp = client.post(path, **kwargs)
        elapsed_ms = (time.time() - t0) * 1000
        return resp, elapsed_ms

    def test_zones_list_under_500ms(self, admin_client):
        resp, ms = self._measure(admin_client, 'GET', '/api/zones')
        assert resp.status_code == 200
        assert ms < 500, f"GET /api/zones took {ms:.0f}ms"

    def test_status_under_500ms(self, admin_client):
        resp, ms = self._measure(admin_client, 'GET', '/api/status')
        assert resp.status_code == 200
        assert ms < 500, f"GET /api/status took {ms:.0f}ms"

    def test_groups_under_500ms(self, admin_client):
        resp, ms = self._measure(admin_client, 'GET', '/api/groups')
        assert resp.status_code == 200
        assert ms < 500, f"GET /api/groups took {ms:.0f}ms"

    def test_programs_under_500ms(self, admin_client):
        resp, ms = self._measure(admin_client, 'GET', '/api/programs')
        assert resp.status_code == 200
        assert ms < 500, f"GET /api/programs took {ms:.0f}ms"

    def test_health_under_500ms(self, admin_client):
        resp, ms = self._measure(admin_client, 'GET', '/health')
        assert resp.status_code in (200, 503)
        assert ms < 500, f"GET /health took {ms:.0f}ms"

    def test_logs_under_500ms(self, admin_client):
        resp, ms = self._measure(admin_client, 'GET', '/api/logs')
        assert resp.status_code == 200
        assert ms < 500, f"GET /api/logs took {ms:.0f}ms"

    def test_server_time_under_500ms(self, admin_client):
        resp, ms = self._measure(admin_client, 'GET', '/api/server-time')
        assert resp.status_code == 200
        assert ms < 500, f"GET /api/server-time took {ms:.0f}ms"

    def test_mqtt_servers_under_500ms(self, admin_client):
        resp, ms = self._measure(admin_client, 'GET', '/api/mqtt/servers')
        assert resp.status_code == 200
        assert ms < 500, f"GET /api/mqtt/servers took {ms:.0f}ms"

    def test_env_values_under_500ms(self, admin_client):
        resp, ms = self._measure(admin_client, 'GET', '/api/env/values')
        assert resp.status_code == 200
        assert ms < 500, f"GET /api/env/values took {ms:.0f}ms"

    def test_auth_status_under_500ms(self, admin_client):
        resp, ms = self._measure(admin_client, 'GET', '/api/auth/status')
        assert resp.status_code == 200
        assert ms < 500, f"GET /api/auth/status took {ms:.0f}ms"
