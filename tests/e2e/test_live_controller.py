"""E2E tests against the live controller at http://10.2.5.244:8080."""
import pytest
import os

pytestmark = pytest.mark.e2e

CONTROLLER_URL = 'http://10.2.5.244:8080'
PASSWORD = '1234'


@pytest.fixture
def live_session():
    """Create an authenticated session to the live controller."""
    try:
        import httpx
    except ImportError:
        pytest.skip("httpx not installed")
    
    client = httpx.Client(base_url=CONTROLLER_URL, timeout=10)
    
    # Login
    try:
        resp = client.post('/api/login', json={'password': PASSWORD})
        if resp.status_code != 200:
            pytest.skip(f"Cannot login to controller: {resp.status_code}")
    except Exception as e:
        pytest.skip(f"Cannot connect to controller: {e}")
    
    yield client
    client.close()


class TestLiveController:
    def test_health(self, live_session):
        resp = live_session.get('/health')
        assert resp.status_code in (200, 503)
        data = resp.json()
        assert 'ok' in data

    def test_status(self, live_session):
        resp = live_session.get('/api/status')
        assert resp.status_code == 200
        data = resp.json()
        assert 'groups' in data

    def test_get_zones(self, live_session):
        resp = live_session.get('/api/zones')
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_get_programs(self, live_session):
        resp = live_session.get('/api/programs')
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_groups(self, live_session):
        resp = live_session.get('/api/groups')
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_mqtt_servers(self, live_session):
        resp = live_session.get('/api/mqtt/servers')
        assert resp.status_code == 200
        data = resp.json()
        assert data['success'] is True

    def test_server_time(self, live_session):
        resp = live_session.get('/api/server-time')
        assert resp.status_code == 200
        data = resp.json()
        assert 'now_iso' in data

    def test_auth_status(self, live_session):
        resp = live_session.get('/api/auth/status')
        assert resp.status_code == 200

    def test_scheduler_status(self, live_session):
        resp = live_session.get('/api/scheduler/status')
        assert resp.status_code in (200, 500)

    def test_water_data(self, live_session):
        resp = live_session.get('/api/water')
        assert resp.status_code == 200

    def test_logs(self, live_session):
        resp = live_session.get('/api/logs')
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
