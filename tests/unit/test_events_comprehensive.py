"""Comprehensive tests for services/events.py."""
import pytest
import os
from unittest.mock import MagicMock

os.environ['TESTING'] = '1'


class TestEventBus:
    def test_publish(self):
        from services.events import publish
        publish({'type': 'test', 'id': 1})  # should not crash

    def test_publish_dedup(self):
        from services import events
        old_dedup = events._DEDUP.copy()
        events._DEDUP.clear()
        try:
            cb = MagicMock()
            events.subscribe(cb)
            events.publish({'type': 'test_dedup', 'id': 42})
            events.publish({'type': 'test_dedup', 'id': 42})  # duplicate
            assert cb.call_count == 1
        finally:
            events._DEDUP = old_dedup
            events._SUBS.pop()

    def test_subscribe(self):
        from services.events import subscribe
        cb = MagicMock()
        subscribe(cb)
        from services import events
        assert cb in events._SUBS
        events._SUBS.remove(cb)

    def test_cleanup_large_dedup(self):
        from services import events
        # Fill dedup beyond max
        old = events._DEDUP.copy()
        for i in range(5000):
            events._DEDUP.add(f'key_{i}')
        events._cleanup(0)
        assert len(events._DEDUP) == 0
        events._DEDUP = old
