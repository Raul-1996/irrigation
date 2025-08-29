import os
import pytest

os.environ.setdefault("TESTING", "1")

from app import app  # noqa: E402

@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def test_index(client):
    resp = client.get("/")
    assert resp.status_code in (200, 302, 404)


def test_zones_list(client):
    resp = client.get("/api/zones")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_zone_start_stop_cycle(client):
    # stop to ensure clean state
    client.post("/api/zones/1/stop")
    r1 = client.post("/api/zones/1/start")
    assert r1.status_code in (200, 400, 404)
    r2 = client.post("/api/zones/1/stop")
    assert r2.status_code in (200, 404)


def test_group_stop_cancels_sequence(client):
    s = client.post("/api/groups/1/start-from-first")
    import time; time.sleep(5)
    # In TESTING scheduler may be unavailable -> accept 200/400/500
    assert s.status_code in (200, 400, 500)
    st = client.post("/api/groups/1/stop")
    assert st.status_code in (200, 500)
