"""E2E tests: concurrent requests, race conditions."""
import pytest
import os
import threading

pytestmark = pytest.mark.e2e

CONTROLLER_URL = 'http://10.2.5.244:8080'
PASSWORD = '1234'


@pytest.fixture
def live_session():
    try:
        import httpx
    except ImportError:
        pytest.skip("httpx not installed")
    
    client = httpx.Client(base_url=CONTROLLER_URL, timeout=10)
    try:
        resp = client.post('/api/login', json={'password': PASSWORD})
        if resp.status_code != 200:
            pytest.skip(f"Cannot login: {resp.status_code}")
    except Exception as e:
        pytest.skip(f"Cannot connect: {e}")
    
    yield client
    client.close()


class TestConcurrentRequests:
    def test_concurrent_status_requests(self, live_session):
        """Multiple concurrent status requests should all succeed."""
        import httpx
        results = []
        errors = []

        def fetch_status():
            try:
                client = httpx.Client(base_url=CONTROLLER_URL, timeout=10)
                client.post('/api/login', json={'password': PASSWORD})
                resp = client.get('/api/status')
                results.append(resp.status_code)
                client.close()
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=fetch_status) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert len(errors) == 0, f"Errors: {errors}"
        assert all(r == 200 for r in results), f"Results: {results}"

    def test_concurrent_zone_reads(self, live_session):
        """Concurrent zone reads should not cause conflicts."""
        import httpx
        results = []

        def read_zones():
            try:
                client = httpx.Client(base_url=CONTROLLER_URL, timeout=10)
                client.post('/api/login', json={'password': PASSWORD})
                resp = client.get('/api/zones')
                results.append(resp.status_code)
                client.close()
            except Exception:
                results.append(500)

        threads = [threading.Thread(target=read_zones) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert all(r == 200 for r in results)
