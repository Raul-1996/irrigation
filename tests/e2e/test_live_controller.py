"""E2E tests against a live controller (opt-in: pytest -m e2e).

Требуются переменные окружения WB_E2E_URL и WB_E2E_PASSWORD.
"""

import os

import pytest

pytestmark = pytest.mark.e2e

CONTROLLER_URL = os.environ.get("WB_E2E_URL", "")
PASSWORD = os.environ.get("WB_E2E_PASSWORD", "")


@pytest.fixture
def live_session():
    """Create an authenticated session to the live controller."""
    if not CONTROLLER_URL or not PASSWORD:
        pytest.fail("e2e tests require WB_E2E_URL and WB_E2E_PASSWORD environment variables")
    try:
        import httpx
    except ImportError as error:
        pytest.fail(f"e2e tests require httpx: {error}", pytrace=False)

    client = httpx.Client(base_url=CONTROLLER_URL, timeout=10)
    try:
        try:
            resp = client.post("/api/login", json={"password": PASSWORD})
        except httpx.RequestError as error:
            pytest.fail(f"Cannot connect to opted-in controller: {error}", pytrace=False)
        if resp.status_code != 200:
            pytest.fail(
                f"Cannot login to opted-in controller: HTTP {resp.status_code}",
                pytrace=False,
            )

        yield client
    finally:
        client.close()


class TestLiveController:
    def test_health(self, live_session):
        resp = live_session.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_status(self, live_session):
        resp = live_session.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "groups" in data

    def test_get_zones(self, live_session):
        resp = live_session.get("/api/zones")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_get_programs(self, live_session):
        resp = live_session.get("/api/programs")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_groups(self, live_session):
        resp = live_session.get("/api/groups")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_mqtt_servers(self, live_session):
        resp = live_session.get("/api/mqtt/servers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_server_time(self, live_session):
        resp = live_session.get("/api/server-time")
        assert resp.status_code == 200
        data = resp.json()
        assert "now_iso" in data

    def test_auth_status(self, live_session):
        resp = live_session.get("/api/auth/status")
        assert resp.status_code == 200

    def test_scheduler_status(self, live_session):
        resp = live_session.get("/api/scheduler/status")
        assert resp.status_code == 200
        assert resp.json()["is_running"] is True

    def test_water_data(self, live_session):
        resp = live_session.get("/api/water")
        assert resp.status_code == 200

    def test_logs(self, live_session):
        resp = live_session.get("/api/logs")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
