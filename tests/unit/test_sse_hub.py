"""Tests for SSE hub: events, MQTT→SSE fan-out."""

import os
import queue
import time

os.environ["TESTING"] = "1"


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


class TestReloadHub:
    def test_reload_noop_when_not_started(self):
        from services import sse_hub

        old = sse_hub._SSE_HUB_STARTED
        sse_hub._SSE_HUB_STARTED = False
        try:
            sse_hub.reload_hub()
            assert sse_hub._SSE_HUB_STARTED is False
        finally:
            sse_hub._SSE_HUB_STARTED = old

    def test_reload_stops_old_clients_and_restarts(self):
        from unittest.mock import MagicMock

        from services import sse_hub

        old_started = sse_hub._SSE_HUB_STARTED
        old_cfg = sse_hub._app_config
        old_mqtt = sse_hub._mqtt
        fake_client = MagicMock()
        sse_hub._SSE_HUB_MQTT[1] = fake_client
        sse_hub._SSE_HUB_STARTED = True
        sse_hub._app_config = {"TESTING": True}
        sse_hub._mqtt = MagicMock()
        try:
            sse_hub.reload_hub()
            fake_client.loop_stop.assert_called_once()
            fake_client.disconnect.assert_called_once()
            assert 1 not in sse_hub._SSE_HUB_MQTT
            # Hub restarted (TESTING short-circuit sets the flag back)
            assert sse_hub._SSE_HUB_STARTED is True
        finally:
            sse_hub._SSE_HUB_MQTT.pop(1, None)
            sse_hub._SSE_HUB_STARTED = old_started
            sse_hub._app_config = old_cfg
            sse_hub._mqtt = old_mqtt
