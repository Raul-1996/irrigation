"""Comprehensive tests for services/sse_hub.py."""
import pytest
import os
import json
import queue
import time
from unittest.mock import patch, MagicMock

os.environ['TESTING'] = '1'


class TestSSEHubInit:
    def test_init(self):
        from services import sse_hub
        mock_db = MagicMock()
        mock_mqtt = MagicMock()
        mock_config = {'TESTING': True}
        sse_hub.init(
            db=mock_db,
            mqtt_module=mock_mqtt,
            app_config=mock_config,
            publish_mqtt_value=MagicMock(),
            normalize_topic=MagicMock(),
            get_scheduler=MagicMock(),
        )
        assert sse_hub._db is mock_db


class TestBroadcast:
    def test_broadcast_to_clients(self):
        from services import sse_hub
        q = queue.Queue(maxsize=100)
        old = sse_hub._SSE_HUB_CLIENTS
        sse_hub._SSE_HUB_CLIENTS = [q]
        try:
            sse_hub.broadcast('{"test": true}')
            assert not q.empty()
            data = q.get_nowait()
            assert data == '{"test": true}'
        finally:
            sse_hub._SSE_HUB_CLIENTS = old

    def test_broadcast_no_clients(self):
        from services import sse_hub
        old = sse_hub._SSE_HUB_CLIENTS
        sse_hub._SSE_HUB_CLIENTS = []
        try:
            sse_hub.broadcast('{"test": true}')  # should not crash
        finally:
            sse_hub._SSE_HUB_CLIENTS = old

    def test_broadcast_full_queue(self):
        from services import sse_hub
        q = queue.Queue(maxsize=1)
        q.put('old')
        old = sse_hub._SSE_HUB_CLIENTS
        sse_hub._SSE_HUB_CLIENTS = [q]
        try:
            sse_hub.broadcast('new')  # should not crash on full queue
        finally:
            sse_hub._SSE_HUB_CLIENTS = old


class TestMarkZoneStopped:
    def test_mark_stopped(self):
        from services import sse_hub
        sse_hub.mark_zone_stopped(1)
        assert sse_hub.recently_stopped(1) is True

    def test_recently_stopped_false(self):
        from services import sse_hub
        assert sse_hub.recently_stopped(9999) is False

    def test_recently_stopped_expired(self):
        from services import sse_hub
        with patch('services.sse_hub._LAST_MANUAL_STOP', {1: time.time() - 10}):
            assert sse_hub.recently_stopped(1, window_sec=5) is False

    def test_recently_stopped_within_window(self):
        from services import sse_hub
        sse_hub.mark_zone_stopped(2)
        assert sse_hub.recently_stopped(2, window_sec=10) is True


class TestGetMetaBuffer:
    def test_empty_buffer(self):
        from services import sse_hub
        from collections import deque
        old = sse_hub._SSE_META_BUFFER
        sse_hub._SSE_META_BUFFER = deque(maxlen=100)
        try:
            result = sse_hub.get_meta_buffer()
            assert result == []
        finally:
            sse_hub._SSE_META_BUFFER = old

    def test_buffer_with_data(self):
        from services import sse_hub
        from collections import deque
        old = sse_hub._SSE_META_BUFFER
        sse_hub._SSE_META_BUFFER = deque([{'topic': '/t', 'payload': 'x'}], maxlen=100)
        try:
            result = sse_hub.get_meta_buffer()
            assert len(result) == 1
        finally:
            sse_hub._SSE_META_BUFFER = old


class TestRegisterUnregister:
    def test_register_client(self):
        from services import sse_hub
        old = list(sse_hub._SSE_HUB_CLIENTS)
        try:
            q = sse_hub.register_client()
            assert q in sse_hub._SSE_HUB_CLIENTS
        finally:
            sse_hub._SSE_HUB_CLIENTS = old

    def test_unregister_client(self):
        from services import sse_hub
        q = queue.Queue()
        sse_hub._SSE_HUB_CLIENTS.append(q)
        sse_hub.unregister_client(q)
        assert q not in sse_hub._SSE_HUB_CLIENTS

    def test_unregister_nonexistent(self):
        from services import sse_hub
        q = queue.Queue()
        sse_hub.unregister_client(q)  # should not crash


class TestEnsureHubStarted:
    def test_ensure_hub_in_testing_mode(self):
        from services import sse_hub
        old_started = sse_hub._SSE_HUB_STARTED
        sse_hub._SSE_HUB_STARTED = False
        sse_hub._app_config = {'TESTING': True}
        sse_hub._mqtt = MagicMock()
        try:
            sse_hub.ensure_hub_started()
            assert sse_hub._SSE_HUB_STARTED is True
        finally:
            sse_hub._SSE_HUB_STARTED = old_started

    def test_ensure_hub_no_mqtt(self):
        from services import sse_hub
        old_started = sse_hub._SSE_HUB_STARTED
        old_mqtt = sse_hub._mqtt
        sse_hub._SSE_HUB_STARTED = False
        sse_hub._mqtt = None
        try:
            sse_hub.ensure_hub_started()
        finally:
            sse_hub._SSE_HUB_STARTED = old_started
            sse_hub._mqtt = old_mqtt

    def test_ensure_hub_already_started(self):
        from services import sse_hub
        old = sse_hub._SSE_HUB_STARTED
        sse_hub._SSE_HUB_STARTED = True
        sse_hub._mqtt = MagicMock()
        try:
            sse_hub.ensure_hub_started()  # should return immediately
        finally:
            sse_hub._SSE_HUB_STARTED = old


class TestRebuildSubscriptions:
    def test_rebuild(self, test_db):
        from services import sse_hub
        old_db = sse_hub._db
        sse_hub._db = test_db
        try:
            srv = test_db.create_mqtt_server({'name': 'T', 'host': '127.0.0.1', 'port': 1883})
            test_db.create_zone({
                'name': 'Z1', 'duration': 10, 'group_id': 1,
                'topic': '/test/z1', 'mqtt_server_id': srv['id'],
            })
            zone_topics, mv_topics = sse_hub._rebuild_subscriptions()
            assert isinstance(zone_topics, dict)
            assert isinstance(mv_topics, dict)
        finally:
            sse_hub._db = old_db
