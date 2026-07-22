"""Comprehensive tests for services/events.py."""

import os
from unittest.mock import MagicMock

os.environ["TESTING"] = "1"


class TestEventBus:
    def test_publish(self):
        from services.events import publish

        publish({"type": "test", "id": 1})  # should not crash

    def test_publish_dedup(self):
        from services import events

        old_dedup = events._DEDUP.copy()
        events._DEDUP.clear()
        try:
            cb = MagicMock()
            events.subscribe(cb)
            events.publish({"type": "test_dedup", "id": 42})
            events.publish({"type": "test_dedup", "id": 42})  # duplicate
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
            events._DEDUP[f"key_{i}"] = 0.0
        events._cleanup(0)
        assert len(events._DEDUP) == 0
        events._DEDUP = old

    def test_cleanup_expires_entries_after_ttl(self):
        from services import events

        old = events._DEDUP.copy()
        events._DEDUP.clear()
        try:
            events._DEDUP["stale_key"] = 0.0
            events._DEDUP["fresh_key"] = events._DEDUP_TTL + 1.0
            events._cleanup(events._DEDUP_TTL + 1.0)
            assert "stale_key" not in events._DEDUP
            assert "fresh_key" in events._DEDUP
        finally:
            events._DEDUP.clear()
            events._DEDUP.update(old)

    def test_republish_after_ttl_reaches_subscribers(self):
        """Повторный emergency_on после истечения TTL снова доходит до подписчиков."""
        from unittest.mock import patch

        from services import events

        old = events._DEDUP.copy()
        events._DEDUP.clear()
        try:
            cb = MagicMock()
            events.subscribe(cb)
            with patch("services.events.time.time", return_value=1000.0):
                events.publish({"type": "emergency_on", "by": "api"})
                events.publish({"type": "emergency_on", "by": "api"})  # в окне TTL — глушится
            assert cb.call_count == 1
            with patch("services.events.time.time", return_value=1000.0 + events._DEDUP_TTL + 1.0):
                events.publish({"type": "emergency_on", "by": "api"})
            assert cb.call_count == 2
        finally:
            events._DEDUP.clear()
            events._DEDUP.update(old)
            events._SUBS.pop()
