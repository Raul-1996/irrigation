"""Tests for MQTT publish service: publish, retry, debounce, dual-topic, QoS."""
import pytest
import os
import time
from unittest.mock import patch, MagicMock

os.environ['TESTING'] = '1'


class TestPublishMqttValue:
    def test_publish_basic(self):
        """Basic publish should succeed."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result

        with patch('services.mqtt_pub.get_or_create_mqtt_client', return_value=mock_client), \
             patch('services.mqtt_pub._TOPIC_LAST_SEND', {}), \
             patch('services.mqtt_pub._db', None):
            from services.mqtt_pub import publish_mqtt_value
            result = publish_mqtt_value(
                {'id': 1, 'host': '127.0.0.1', 'port': 1883},
                '/test/topic', '1', min_interval_sec=0
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
        key = (1, '/test/topic')
        last_send = {key: ('1', time.time())}

        with patch('services.mqtt_pub.get_or_create_mqtt_client', return_value=mock_client), \
             patch('services.mqtt_pub._TOPIC_LAST_SEND', last_send), \
             patch('services.mqtt_pub._db', None):
            from services.mqtt_pub import publish_mqtt_value
            result = publish_mqtt_value(
                {'id': 1}, '/test/topic', '1', min_interval_sec=10
            )
            # Should return True (skipped as duplicate)
            assert result is True

    def test_publish_no_debounce_on_different_value(self):
        """Different value should not be debounced."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result

        key = (1, '/test/topic')
        last_send = {key: ('0', time.time())}

        with patch('services.mqtt_pub.get_or_create_mqtt_client', return_value=mock_client), \
             patch('services.mqtt_pub._TOPIC_LAST_SEND', last_send), \
             patch('services.mqtt_pub._db', None):
            from services.mqtt_pub import publish_mqtt_value
            result = publish_mqtt_value(
                {'id': 1}, '/test/topic', '1', min_interval_sec=10
            )
            assert result is True
            assert mock_client.publish.called

    def test_publish_client_unavailable(self):
        """Should return False when MQTT client is unavailable."""
        with patch('services.mqtt_pub.get_or_create_mqtt_client', return_value=None), \
             patch('services.mqtt_pub._TOPIC_LAST_SEND', {}), \
             patch('services.mqtt_pub._db', None):
            from services.mqtt_pub import publish_mqtt_value
            result = publish_mqtt_value(
                {'id': 1}, '/test/topic', '1', min_interval_sec=0
            )
            assert result is False

    def test_publish_dual_topic(self):
        """Should publish to both base topic and /on topic."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result

        with patch('services.mqtt_pub.get_or_create_mqtt_client', return_value=mock_client), \
             patch('services.mqtt_pub._TOPIC_LAST_SEND', {}), \
             patch('services.mqtt_pub._db', None):
            from services.mqtt_pub import publish_mqtt_value
            result = publish_mqtt_value(
                {'id': 1}, '/test/topic', '1', min_interval_sec=0
            )
            assert result is True
            # Should have published to both /test/topic and /test/topic/on
            topics_published = [call[0][0] for call in mock_client.publish.call_args_list]
            assert '/test/topic' in topics_published
            assert '/test/topic/on' in topics_published

    def test_publish_qos_2(self):
        """QoS 2 publish should wait for acknowledgement."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_result.wait_for_publish = MagicMock()
        mock_client.publish.return_value = mock_result

        with patch('services.mqtt_pub.get_or_create_mqtt_client', return_value=mock_client), \
             patch('services.mqtt_pub._TOPIC_LAST_SEND', {}), \
             patch('services.mqtt_pub._db', None):
            from services.mqtt_pub import publish_mqtt_value
            result = publish_mqtt_value(
                {'id': 1}, '/test/topic', '1', min_interval_sec=0, qos=2
            )
            assert result is True
