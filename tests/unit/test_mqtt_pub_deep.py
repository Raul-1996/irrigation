"""Deep tests for services/mqtt_pub.py."""
import pytest
from unittest.mock import patch, MagicMock


class TestPublishMqttValue:
    """Tests for publish_mqtt_value function."""

    def test_publish_returns_true_on_success(self):
        """Should return True when publish succeeds."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result

        with patch('services.mqtt_pub.get_or_create_mqtt_client', return_value=mock_client), \
             patch('services.mqtt_pub._db', None):
            from services.mqtt_pub import publish_mqtt_value
            result = publish_mqtt_value(
                {'id': 1, 'host': '127.0.0.1', 'port': 1883},
                '/devices/test/K1', '1',
                min_interval_sec=0.0
            )
        assert result is True

    def test_publish_returns_false_no_client(self):
        """Should return False when no MQTT client available."""
        with patch('services.mqtt_pub.get_or_create_mqtt_client', return_value=None), \
             patch('services.mqtt_pub._db', None):
            from services.mqtt_pub import publish_mqtt_value
            result = publish_mqtt_value(
                {'id': 1, 'host': '127.0.0.1', 'port': 1883},
                '/devices/test/K1', '1',
                min_interval_sec=0.0
            )
        assert result is False

    def test_publish_skips_duplicate(self):
        """Should skip duplicate publishes within min_interval_sec."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result

        with patch('services.mqtt_pub.get_or_create_mqtt_client', return_value=mock_client), \
             patch('services.mqtt_pub._db', None):
            from services.mqtt_pub import publish_mqtt_value
            # First call
            publish_mqtt_value(
                {'id': 99, 'host': '127.0.0.1', 'port': 1883},
                '/devices/dedup_test/K1', '1',
                min_interval_sec=60.0  # long interval
            )
            # Second call with same value should be skipped
            result = publish_mqtt_value(
                {'id': 99, 'host': '127.0.0.1', 'port': 1883},
                '/devices/dedup_test/K1', '1',
                min_interval_sec=60.0
            )
        assert result is True  # Returns True (skip = success)

    def test_publish_qos2_with_wait(self):
        """Should call wait_for_publish for QoS >= 1."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result

        with patch('services.mqtt_pub.get_or_create_mqtt_client', return_value=mock_client), \
             patch('services.mqtt_pub._db', None):
            from services.mqtt_pub import publish_mqtt_value
            result = publish_mqtt_value(
                {'id': 98, 'host': '127.0.0.1', 'port': 1883},
                '/devices/qos_test/K1', '1',
                min_interval_sec=0.0, qos=2
            )
        assert result is True
        mock_result.wait_for_publish.assert_called()


class TestGetOrCreateMqttClient:
    """Tests for get_or_create_mqtt_client."""

    def test_returns_none_without_paho(self):
        """Should return None if paho is not available."""
        with patch('services.mqtt_pub.mqtt', None):
            from services.mqtt_pub import get_or_create_mqtt_client
            result = get_or_create_mqtt_client({'id': 1, 'host': '127.0.0.1'})
        assert result is None
