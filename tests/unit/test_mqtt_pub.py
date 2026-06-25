"""Tests for MQTT publish service: publish, retry, debounce, dual-topic, QoS."""

import os
import time
from unittest.mock import MagicMock, patch

os.environ["TESTING"] = "1"


class TestPublishMqttValue:
    def test_publish_basic(self):
        """Basic publish should succeed."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result

        with (
            patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mock_client),
            patch("services.mqtt_pub._TOPIC_LAST_SEND", {}),
            patch("services.mqtt_pub._db", None),
        ):
            from services.mqtt_pub import publish_mqtt_value

            result = publish_mqtt_value(
                {"id": 1, "host": "127.0.0.1", "port": 1883}, "/test/topic", "1", min_interval_sec=0
            )
            assert result is True
            assert mock_client.publish.called

    def test_publish_debounce(self):
        """Same value within debounce window should be skipped."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result

        # Pre-fill last send with current time
        key = (1, "/test/topic")
        last_send = {key: ("1", time.time())}

        with (
            patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mock_client),
            patch("services.mqtt_pub._TOPIC_LAST_SEND", last_send),
            patch("services.mqtt_pub._db", None),
        ):
            from services.mqtt_pub import publish_mqtt_value

            result = publish_mqtt_value({"id": 1}, "/test/topic", "1", min_interval_sec=10)
            # Should return True (skipped as duplicate)
            assert result is True

    def test_publish_no_debounce_on_different_value(self):
        """Different value should not be debounced."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result

        key = (1, "/test/topic")
        last_send = {key: ("0", time.time())}

        with (
            patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mock_client),
            patch("services.mqtt_pub._TOPIC_LAST_SEND", last_send),
            patch("services.mqtt_pub._db", None),
        ):
            from services.mqtt_pub import publish_mqtt_value

            result = publish_mqtt_value({"id": 1}, "/test/topic", "1", min_interval_sec=10)
            assert result is True
            assert mock_client.publish.called

    def test_publish_client_unavailable(self):
        """Should return False when MQTT client is unavailable."""
        with (
            patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=None),
            patch("services.mqtt_pub._TOPIC_LAST_SEND", {}),
            patch("services.mqtt_pub._db", None),
        ):
            from services.mqtt_pub import publish_mqtt_value

            result = publish_mqtt_value({"id": 1}, "/test/topic", "1", min_interval_sec=0)
            assert result is False

    def test_publish_dual_topic(self):
        """Should publish to both base topic and /on topic."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result

        with (
            patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mock_client),
            patch("services.mqtt_pub._TOPIC_LAST_SEND", {}),
            patch("services.mqtt_pub._db", None),
        ):
            from services.mqtt_pub import publish_mqtt_value

            result = publish_mqtt_value({"id": 1}, "/test/topic", "1", min_interval_sec=0)
            assert result is True
            # Should have published to both /test/topic and /test/topic/on
            topics_published = [call[0][0] for call in mock_client.publish.call_args_list]
            assert "/test/topic" in topics_published
            assert "/test/topic/on" in topics_published

    def test_publish_qos_2(self):
        """QoS 2 publish should wait for acknowledgement."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_result.wait_for_publish = MagicMock()
        mock_client.publish.return_value = mock_result

        with (
            patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mock_client),
            patch("services.mqtt_pub._TOPIC_LAST_SEND", {}),
            patch("services.mqtt_pub._db", None),
        ):
            from services.mqtt_pub import publish_mqtt_value

            result = publish_mqtt_value({"id": 1}, "/test/topic", "1", min_interval_sec=0, qos=2)
            assert result is True


class TestMqttClientRecovery:
    """mqtt-client-recovery: a wedged client is invalidated and recreated."""

    def test_publish_recovers_wedged_client(self):
        """Delivery failure triggers invalidate + recreate + successful retry."""
        stuck = MagicMock(name="stuck")
        fresh = MagicMock(name="fresh")
        invalidated = []

        with (
            patch(
                "services.mqtt_pub.get_or_create_mqtt_client",
                side_effect=[stuck, fresh, fresh, fresh, fresh],
            ),
            # base topic: first attempt fails (wedged), retry after recreate succeeds; /on succeeds
            patch("services.mqtt_pub._publish_with_retries", side_effect=[False, True, True, True]),
            patch("services.mqtt_pub._invalidate_client", side_effect=lambda sid: invalidated.append(sid)),
            patch("services.mqtt_pub._TOPIC_LAST_SEND", {}),
            patch("services.mqtt_pub._db", None),
        ):
            from services.mqtt_pub import publish_mqtt_value

            result = publish_mqtt_value({"id": 1}, "/test/topic", "1", min_interval_sec=0, qos=2)
            assert result is True
            # client for server id=1 was invalidated exactly once during recovery
            assert invalidated == [1]

    def test_publish_no_recovery_when_healthy(self):
        """A healthy client must not be invalidated."""
        ok = MagicMock(name="ok")
        invalidated = []

        with (
            patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=ok),
            patch("services.mqtt_pub._publish_with_retries", return_value=True),
            patch("services.mqtt_pub._invalidate_client", side_effect=lambda sid: invalidated.append(sid)),
            patch("services.mqtt_pub._TOPIC_LAST_SEND", {}),
            patch("services.mqtt_pub._db", None),
        ):
            from services.mqtt_pub import publish_mqtt_value

            result = publish_mqtt_value({"id": 1}, "/test/topic", "1", min_interval_sec=0, qos=2)
            assert result is True
            assert invalidated == []

    def test_invalidate_client_pops_and_tears_down(self):
        """_invalidate_client removes the cached client and tears it down."""
        cl = MagicMock()
        cache = {1: cl}
        with patch("services.mqtt_pub._MQTT_CLIENTS", cache):
            from services.mqtt_pub import _invalidate_client

            _invalidate_client(1)
            assert 1 not in cache
            cl.loop_stop.assert_called_once()
            cl.disconnect.assert_called_once()

    def test_publish_with_retries_detects_wedged_window(self):
        """Wedged inflight window: publish() rc=0, wait_for_publish() returns
        silently (no exception), but is_published() is False → must return
        False so the caller's recovery kicks in. Reproduces the prod bug."""
        info = MagicMock()
        info.rc = 0
        info.wait_for_publish.return_value = None  # silent timeout, no raise
        info.is_published.return_value = False  # message never actually sent
        cl = MagicMock()
        cl.publish.return_value = info

        with patch("services.mqtt_pub.time.sleep"):  # skip backoff delays
            from services.mqtt_pub import _publish_with_retries

            assert _publish_with_retries(cl, "/test/topic", "1", 2, True) is False

    def test_publish_with_retries_succeeds_when_delivered(self):
        """is_published() True → genuine delivery → return True (no false negatives)."""
        info = MagicMock()
        info.rc = 0
        info.wait_for_publish.return_value = None
        info.is_published.return_value = True
        cl = MagicMock()
        cl.publish.return_value = info

        from services.mqtt_pub import _publish_with_retries

        assert _publish_with_retries(cl, "/test/topic", "1", 2, True) is True
