"""Integration tests with REAL MQTT broker on 10.2.5.244:1883."""
import pytest
import time

pytestmark = pytest.mark.mqtt_real

MQTT_HOST = '10.2.5.244'
MQTT_PORT = 1883


@pytest.fixture
def mqtt_client():
    """Create a real MQTT client connected to the broker."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        pytest.skip("paho-mqtt not installed")
    
    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    try:
        cl.connect(MQTT_HOST, MQTT_PORT, 10)
        cl.loop_start()
    except Exception as e:
        pytest.skip(f"Cannot connect to MQTT broker: {e}")
    
    yield cl
    
    cl.loop_stop()
    cl.disconnect()


class TestMQTTRealBroker:
    def test_connect(self, mqtt_client):
        """Should be able to connect to the real broker."""
        assert mqtt_client is not None

    def test_publish_and_subscribe(self, mqtt_client):
        """Should be able to publish and receive messages."""
        received = []
        
        def on_message(client, userdata, msg):
            received.append(msg.payload.decode())
        
        mqtt_client.on_message = on_message
        test_topic = '/test/irrigation_pytest_' + str(int(time.time()))
        mqtt_client.subscribe(test_topic)
        time.sleep(0.5)
        
        mqtt_client.publish(test_topic, '1', qos=1)
        time.sleep(1.0)
        
        assert len(received) > 0
        assert '1' in received

    def test_retained_message(self, mqtt_client):
        """Should be able to publish and receive retained messages."""
        received = []
        test_topic = '/test/irrigation_retained_' + str(int(time.time()))
        
        # Publish retained
        mqtt_client.publish(test_topic, 'retained_value', qos=1, retain=True)
        time.sleep(0.5)
        
        def on_message(client, userdata, msg):
            received.append(msg.payload.decode())
        
        mqtt_client.on_message = on_message
        mqtt_client.subscribe(test_topic)
        time.sleep(1.0)
        
        assert len(received) > 0
        assert 'retained_value' in received
        
        # Cleanup: clear retained
        mqtt_client.publish(test_topic, '', retain=True)

    def test_qos_2_delivery(self, mqtt_client):
        """QoS 2 message should be delivered exactly once."""
        received = []
        test_topic = '/test/irrigation_qos2_' + str(int(time.time()))
        
        def on_message(client, userdata, msg):
            received.append(msg.payload.decode())
        
        mqtt_client.on_message = on_message
        mqtt_client.subscribe(test_topic, qos=2)
        time.sleep(0.5)
        
        result = mqtt_client.publish(test_topic, 'qos2_test', qos=2)
        result.wait_for_publish(timeout=5)
        time.sleep(1.0)
        
        assert len(received) == 1
        assert received[0] == 'qos2_test'
