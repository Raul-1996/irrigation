"""Tests for SSE hub hardening: client limits, dead detection, timeouts."""
import pytest
import os
import queue
import time

os.environ['TESTING'] = '1'


class TestSSEClientLimit:
    """MAX_SSE_CLIENTS enforcement."""

    def setup_method(self):
        from services import sse_hub
        # Clean slate
        with sse_hub._SSE_HUB_LOCK:
            sse_hub._SSE_HUB_CLIENTS.clear()

    def teardown_method(self):
        from services import sse_hub
        with sse_hub._SSE_HUB_LOCK:
            sse_hub._SSE_HUB_CLIENTS.clear()

    def test_register_respects_max_limit(self):
        """Register 25 clients → only MAX_SSE_CLIENTS remain."""
        from services import sse_hub
        queues = []
        for _ in range(25):
            queues.append(sse_hub.register_client())

        with sse_hub._SSE_HUB_LOCK:
            assert len(sse_hub._SSE_HUB_CLIENTS) == sse_hub.MAX_SSE_CLIENTS

    def test_oldest_evicted_on_limit(self):
        """When limit hit, oldest client gets None sentinel."""
        from services import sse_hub
        first = sse_hub.register_client()
        for _ in range(sse_hub.MAX_SSE_CLIENTS):
            sse_hub.register_client()

        # first should no longer be in clients
        with sse_hub._SSE_HUB_LOCK:
            assert first not in sse_hub._SSE_HUB_CLIENTS

        # first should have received None sentinel
        sentinel = first.get_nowait()
        assert sentinel is None

    def test_queue_maxsize_reduced(self):
        """New client queues should have maxsize=100."""
        from services import sse_hub
        q = sse_hub.register_client()
        assert q.maxsize == 100
        sse_hub.unregister_client(q)


class TestDeadClientDetection:
    """broadcast() removes clients with full queues."""

    def setup_method(self):
        from services import sse_hub
        with sse_hub._SSE_HUB_LOCK:
            sse_hub._SSE_HUB_CLIENTS.clear()

    def teardown_method(self):
        from services import sse_hub
        with sse_hub._SSE_HUB_LOCK:
            sse_hub._SSE_HUB_CLIENTS.clear()

    def test_full_queue_removed_on_broadcast(self):
        """Client with full queue is removed during broadcast."""
        from services import sse_hub
        alive = sse_hub.register_client()
        dead = sse_hub.register_client()

        # Fill dead queue to capacity
        for i in range(dead.maxsize):
            dead.put_nowait(f'{{"fill": {i}}}')

        sse_hub.broadcast('{"test": 1}')

        with sse_hub._SSE_HUB_LOCK:
            assert dead not in sse_hub._SSE_HUB_CLIENTS
            assert alive in sse_hub._SSE_HUB_CLIENTS

        # alive should have received the broadcast
        msg = alive.get_nowait()
        assert msg == '{"test": 1}'
        sse_hub.unregister_client(alive)

    def test_healthy_clients_survive_broadcast(self):
        """Healthy clients are not removed."""
        from services import sse_hub
        q1 = sse_hub.register_client()
        q2 = sse_hub.register_client()
        sse_hub.broadcast('{"ok": true}')
        with sse_hub._SSE_HUB_LOCK:
            assert len(sse_hub._SSE_HUB_CLIENTS) == 2
        sse_hub.unregister_client(q1)
        sse_hub.unregister_client(q2)


class TestSentinelHandling:
    """None sentinel terminates the generator cleanly."""

    def setup_method(self):
        from services import sse_hub
        with sse_hub._SSE_HUB_LOCK:
            sse_hub._SSE_HUB_CLIENTS.clear()

    def teardown_method(self):
        from services import sse_hub
        with sse_hub._SSE_HUB_LOCK:
            sse_hub._SSE_HUB_CLIENTS.clear()

    def test_sentinel_stops_generator(self):
        """Generator breaks on None sentinel in queue."""
        from services import sse_hub
        q = sse_hub.register_client()
        # Put some data then sentinel
        q.put_nowait('{"data": 1}')
        q.put_nowait(None)

        # Simulate generator logic
        results = []
        while True:
            try:
                data = q.get_nowait()
                if data is None:
                    break
                results.append(data)
            except queue.Empty:
                break

        assert len(results) == 1
        assert results[0] == '{"data": 1}'
        sse_hub.unregister_client(q)


class TestInternalBroadcastDeadDetection:
    """Dead client detection in internal _on_message broadcast paths."""

    def setup_method(self):
        from services import sse_hub
        with sse_hub._SSE_HUB_LOCK:
            sse_hub._SSE_HUB_CLIENTS.clear()

    def teardown_method(self):
        from services import sse_hub
        with sse_hub._SSE_HUB_LOCK:
            sse_hub._SSE_HUB_CLIENTS.clear()

    def test_multiple_dead_clients_removed(self):
        """Multiple dead clients are all removed in one broadcast."""
        from services import sse_hub
        alive = sse_hub.register_client()
        deads = []
        for _ in range(5):
            d = sse_hub.register_client()
            for i in range(d.maxsize):
                d.put_nowait(f'{{"fill": {i}}}')
            deads.append(d)

        sse_hub.broadcast('{"test": true}')

        with sse_hub._SSE_HUB_LOCK:
            assert len(sse_hub._SSE_HUB_CLIENTS) == 1
            assert alive in sse_hub._SSE_HUB_CLIENTS
        sse_hub.unregister_client(alive)
