"""Tests for SSE hub: events, MQTT→SSE fan-out."""
import pytest
import os
import time
import queue

os.environ['TESTING'] = '1'


class TestSSEHub:
    def test_register_unregister_client(self):
        from services import sse_hub
        q = sse_hub.register_client()
        assert isinstance(q, queue.Queue)
        sse_hub.unregister_client(q)
        # Should not be in clients list
        assert q not in sse_hub._SSE_HUB_CLIENTS

    def test_broadcast(self):
        from services import sse_hub
        q = sse_hub.register_client()
        try:
            sse_hub.broadcast('{"test": true}')
            msg = q.get_nowait()
            assert msg == '{"test": true}'
        finally:
            sse_hub.unregister_client(q)

    def test_broadcast_to_multiple_clients(self):
        from services import sse_hub
        q1 = sse_hub.register_client()
        q2 = sse_hub.register_client()
        try:
            sse_hub.broadcast('{"hello": 1}')
            assert q1.get_nowait() == '{"hello": 1}'
            assert q2.get_nowait() == '{"hello": 1}'
        finally:
            sse_hub.unregister_client(q1)
            sse_hub.unregister_client(q2)

    def test_mark_zone_stopped_and_recently_stopped(self):
        from services import sse_hub
        sse_hub.mark_zone_stopped(42)
        assert sse_hub.recently_stopped(42, window_sec=5) is True

    def test_recently_stopped_expired(self):
        from services import sse_hub
        sse_hub._LAST_MANUAL_STOP[99] = time.time() - 10
        assert sse_hub.recently_stopped(99, window_sec=5) is False

    def test_recently_stopped_unknown_zone(self):
        from services import sse_hub
        assert sse_hub.recently_stopped(999999, window_sec=5) is False

    def test_unregister_nonexistent_client(self):
        """Unregistering a client not in list should not raise."""
        from services import sse_hub
        q = queue.Queue()
        sse_hub.unregister_client(q)  # Should not raise

    def test_get_meta_buffer(self):
        from services import sse_hub
        buf = sse_hub.get_meta_buffer()
        assert isinstance(buf, list)
