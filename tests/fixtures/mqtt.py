"""MQTT fixtures: mock client for unit tests, real broker for integration."""
import pytest
from unittest.mock import MagicMock, patch


class MockMQTTClient:
    """A simple mock MQTT client that tracks published messages."""

    def __init__(self):
        self.published = []
        self.subscribed = []
        self.connected = False
        self._on_message = None
        self._on_connect = None

    def connect(self, host, port, keepalive=60):
        self.connected = True
        self.host = host
        self.port = port

    def disconnect(self):
        self.connected = False

    def publish(self, topic, payload=None, qos=0, retain=False):
        result = MagicMock()
        result.rc = 0
        result.wait_for_publish = MagicMock()
        self.published.append({
            'topic': topic,
            'payload': payload,
            'qos': qos,
            'retain': retain,
        })
        return result

    def subscribe(self, topic, qos=0):
        self.subscribed.append({'topic': topic, 'qos': qos})

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def username_pw_set(self, username, password=None):
        self.username = username
        self.password = password

    def reconnect(self):
        pass

    def reconnect_delay_set(self, min_delay=1, max_delay=5):
        pass

    def max_inflight_messages_set(self, count):
        pass

    def tls_set(self, **kwargs):
        pass

    def tls_insecure_set(self, value):
        pass

    @property
    def on_message(self):
        return self._on_message

    @on_message.setter
    def on_message(self, func):
        self._on_message = func

    @property
    def on_connect(self):
        return self._on_connect

    @on_connect.setter
    def on_connect(self, func):
        self._on_connect = func

    @property
    def on_disconnect(self):
        return self._on_disconnect

    @on_disconnect.setter
    def on_disconnect(self, func):
        self._on_disconnect = func


@pytest.fixture
def mock_mqtt_client():
    """Return a fresh MockMQTTClient."""
    return MockMQTTClient()


@pytest.fixture
def mock_mqtt_module(mock_mqtt_client):
    """Patch paho.mqtt.client.Client to return mock_mqtt_client."""
    mock_module = MagicMock()
    mock_module.Client.return_value = mock_mqtt_client

    # Provide CallbackAPIVersion
    class FakeAPIVersion:
        VERSION2 = 2

    mock_module.CallbackAPIVersion = FakeAPIVersion
    return mock_module


@pytest.fixture
def patched_mqtt(mock_mqtt_module):
    """Patch 'paho.mqtt.client' globally for unit tests."""
    with patch.dict('sys.modules', {'paho.mqtt.client': mock_mqtt_module, 'paho': MagicMock(), 'paho.mqtt': MagicMock()}):
        yield mock_mqtt_module
