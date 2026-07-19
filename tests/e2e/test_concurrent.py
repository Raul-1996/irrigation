"""E2E tests: concurrent requests, race conditions (opt-in: pytest -m e2e).

Требуются переменные окружения WB_E2E_URL и WB_E2E_PASSWORD.
"""

import os
from concurrent.futures import Future, ThreadPoolExecutor, wait

import pytest

pytestmark = pytest.mark.e2e

CONTROLLER_URL = os.environ.get("WB_E2E_URL", "")
PASSWORD = os.environ.get("WB_E2E_PASSWORD", "")


@pytest.fixture
def live_session():
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


def _completed_results(futures: list[Future[int]], *, expected: int) -> list[int]:
    """Return every worker result or fail instead of accepting an empty subset."""
    done, pending = wait(futures, timeout=25)
    assert not pending, f"{len(pending)} of {expected} concurrent requests did not terminate"
    assert len(done) == expected
    return [future.result() for future in futures]


class TestConcurrentRequests:
    def test_concurrent_status_requests(self, live_session):
        """Multiple concurrent status requests should all succeed."""
        import httpx

        def fetch_status() -> int:
            try:
                with httpx.Client(base_url=CONTROLLER_URL, timeout=10) as client:
                    login = client.post("/api/login", json={"password": PASSWORD})
                    if login.status_code != 200:
                        raise AssertionError(f"concurrent login returned HTTP {login.status_code}")
                    return client.get("/api/status").status_code
            except httpx.RequestError as error:
                raise AssertionError(f"concurrent status request failed: {error}") from error

        executor = ThreadPoolExecutor(max_workers=5)
        try:
            futures = [executor.submit(fetch_status) for _ in range(5)]
            results = _completed_results(futures, expected=5)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        assert len(results) == 5
        assert results == [200] * 5

    def test_concurrent_zone_reads(self, live_session):
        """Concurrent zone reads should not cause conflicts."""
        import httpx

        def read_zones() -> int:
            try:
                with httpx.Client(base_url=CONTROLLER_URL, timeout=10) as client:
                    login = client.post("/api/login", json={"password": PASSWORD})
                    if login.status_code != 200:
                        raise AssertionError(f"concurrent login returned HTTP {login.status_code}")
                    return client.get("/api/zones").status_code
            except httpx.RequestError as error:
                raise AssertionError(f"concurrent zone request failed: {error}") from error

        executor = ThreadPoolExecutor(max_workers=10)
        try:
            futures = [executor.submit(read_zones) for _ in range(10)]
            results = _completed_results(futures, expected=10)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        assert len(results) == 10
        assert results == [200] * 10
