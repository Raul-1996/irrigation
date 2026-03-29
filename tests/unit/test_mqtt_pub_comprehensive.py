"""Comprehensive tests for services/mqtt_pub.py."""
import pytest
import os
import time
from unittest.mock import patch, MagicMock

os.environ['TESTING'] = '1'


class TestGetOrCreateMqttClient:
    def test_create_client(self):
        mock_mqtt = MagicMock()
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        mock_mqtt.CallbackAPIVersion.VERSION2 = 2

        with patch('services.mqtt_pub.mqtt', mock_mqtt), \
             patch('services.mqtt_pub._MQTT_CLIENTS', {}), \
             patch('services.mqtt_pub._MQTT_CLIENTS_LOCK'):
            from services.mqtt_pub import get_or_create_mqtt_client
            result = get_or_create_mqtt_client({'id': 1, 'host': '127.0.0.1', 'port': 1883})
            assert result is not None

    def test_cached_client(self):
        mock_client = MagicMock()
        with patch('services.mqtt_pub._MQTT_CLIENTS', {1: mock_client}):
            from services.mqtt_pub import get_or_create_mqtt_client
            result = get_or_create_mqtt_client({'id': 1})
            assert result is mock_client

    def test_no_mqtt_module(self):
        with patch('services.mqtt_pub.mqtt', None):
            from services.mqtt_pub import get_or_create_mqtt_client
            result = get_or_create_mqtt_client({'id': 1})
            assert result is None

    def test_create_with_auth(self):
        mock_mqtt = MagicMock()
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client
        mock_mqtt.CallbackAPIVersion.VERSION2 = 2

        with patch('services.mqtt_pub.mqtt', mock_mqtt), \
             patch('services.mqtt_pub._MQTT_CLIENTS', {}), \
             patch('services.mqtt_pub._MQTT_CLIENTS_LOCK'):
            from services.mqtt_pub import get_or_create_mqtt_client
            result = get_or_create_mqtt_client({
                'id': 2, 'host': '10.0.0.1', 'port': 1883,
                'username': 'user', 'password': 'pass',
            })
            assert result is not None
            mock_client.username_pw_set.assert_called_once()

    def test_connection_failure(self):
        mock_mqtt = MagicMock()
        mock_client = MagicMock()
        mock_client.connect.side_effect = ConnectionError("refused")
        mock_mqtt.Client.return_value = mock_client
        mock_mqtt.CallbackAPIVersion.VERSION2 = 2

        with patch('services.mqtt_pub.mqtt', mock_mqtt), \
             patch('services.mqtt_pub._MQTT_CLIENTS', {}), \
             patch('services.mqtt_pub._MQTT_CLIENTS_LOCK'):
            from services.mqtt_pub import get_or_create_mqtt_client
            result = get_or_create_mqtt_client({'id': 3, 'host': 'bad', 'port': 1883})
            assert result is None


class TestPublishMqttValue:
    def test_publish_with_meta(self):
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result

        with patch('services.mqtt_pub.get_or_create_mqtt_client', return_value=mock_client), \
             patch('services.mqtt_pub._TOPIC_LAST_SEND', {}), \
             patch('services.mqtt_pub._db', None):
            from services.mqtt_pub import publish_mqtt_value
            result = publish_mqtt_value(
                {'id': 1}, '/test/topic', '1', min_interval_sec=0,
                meta={'cmd': 'start', 'ver': '1'}
            )
            assert result is True

    def test_publish_with_retain(self):
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result

        with patch('services.mqtt_pub.get_or_create_mqtt_client', return_value=mock_client), \
             patch('services.mqtt_pub._TOPIC_LAST_SEND', {}), \
             patch('services.mqtt_pub._db', None):
            from services.mqtt_pub import publish_mqtt_value
            result = publish_mqtt_value(
                {'id': 1}, '/test/topic', '1', min_interval_sec=0, retain=True
            )
            assert result is True

    def test_publish_server_cache(self):
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result
        mock_db = MagicMock()
        mock_db.get_mqtt_server.return_value = {'id': 1, 'host': '127.0.0.1', 'port': 1883}

        with patch('services.mqtt_pub.get_or_create_mqtt_client', return_value=mock_client), \
             patch('services.mqtt_pub._TOPIC_LAST_SEND', {}), \
             patch('services.mqtt_pub._db', mock_db), \
             patch('services.mqtt_pub._SERVER_CACHE', {}):
            from services.mqtt_pub import publish_mqtt_value
            result = publish_mqtt_value(
                {'id': 1}, '/test/topic', '1', min_interval_sec=0
            )
            assert result is True

    def test_publish_retry_on_failure(self):
        mock_client = MagicMock()
        mock_result_fail = MagicMock()
        mock_result_fail.rc = 4  # error
        mock_result_ok = MagicMock()
        mock_result_ok.rc = 0
        mock_client.publish.side_effect = [mock_result_fail, mock_result_ok, mock_result_ok]

        with patch('services.mqtt_pub.get_or_create_mqtt_client', return_value=mock_client), \
             patch('services.mqtt_pub._TOPIC_LAST_SEND', {}), \
             patch('services.mqtt_pub._db', None):
            from services.mqtt_pub import publish_mqtt_value
            result = publish_mqtt_value(
                {'id': 1}, '/test/topic', '1', min_interval_sec=0
            )
            assert result is True

    def test_publish_qos1_wait_for_publish(self):
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
                {'id': 1}, '/test/topic', '1', min_interval_sec=0, qos=1
            )
            assert result is True
            mock_result.wait_for_publish.assert_called()


class TestShutdownMqttClients:
    def test_shutdown(self):
        mock_client = MagicMock()
        clients = {1: mock_client}
        with patch('services.mqtt_pub._MQTT_CLIENTS', clients):
            from services.mqtt_pub import _shutdown_mqtt_clients
            _shutdown_mqtt_clients()
            mock_client.loop_stop.assert_called()
            mock_client.disconnect.assert_called()
