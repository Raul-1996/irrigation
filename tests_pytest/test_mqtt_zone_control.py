import os
import pytest
import time


def test_zone_mqtt_endpoints_exist(client):
    # set all zones to same mqtt topic to simplify manual testing
    topic = '/devices/wb-mr6cv3_50/controls/K2'
    zones = client.get('/api/zones').get_json()
    for z in zones:
        client.put(f"/api/zones/{z['id']}", json={'mqtt_server_id': 1, 'topic': topic})
    # endpoints should exist and return 200/400 depending on broker
    r1 = client.post('/api/zones/1/mqtt/start')
    assert r1.status_code in (200, 400, 500)
    r2 = client.post('/api/zones/1/mqtt/stop')
    assert r2.status_code in (200, 400, 500)


@pytest.mark.skipif(not os.environ.get('TEST_MQTT_HOST'), reason='External MQTT not configured')
def test_zone_mqtt_topic_toggles_when_called(client):
    import paho.mqtt.client as mqtt
    host = os.environ.get('TEST_MQTT_HOST', '127.0.0.1')
    port = int(os.environ.get('TEST_MQTT_PORT', '1883'))
    topic = '/devices/wb-mr6cv3_50/controls/K2'
    # set zone 1 config
    client.put('/api/zones/1', json={'mqtt_server_id': 1, 'topic': topic})
    seen = []
    def on_connect(c,u,f,rc,properties=None):
        c.subscribe(topic)
    def on_message(c,u,m):
        try:
            seen.append(m.payload.decode('utf-8','ignore'))
        except Exception:
            seen.append(str(m.payload))
    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    cl.on_connect = on_connect
    cl.on_message = on_message
    cl.connect(host, port, 5)
    cl.loop_start()
    time.sleep(0.5)
    client.post('/api/zones/1/mqtt/start')
    t0 = time.time()
    while time.time()-t0 < 5 and '1' not in seen:
        time.sleep(0.2)
    client.post('/api/zones/1/mqtt/stop')
    t1 = time.time()
    while time.time()-t1 < 5 and '0' not in seen:
        time.sleep(0.2)
    cl.loop_stop()
    assert '1' in seen and '0' in seen


@pytest.mark.skipif(not os.environ.get('TEST_MQTT_HOST'), reason='External MQTT not configured')
def test_group_stop_publishes_zero_to_all_group_topics(client):
    import paho.mqtt.client as mqtt
    import json
    host = os.environ.get('TEST_MQTT_HOST', '127.0.0.1')
    port = int(os.environ.get('TEST_MQTT_PORT', '1883'))
    topic = '/devices/wb-mr6cv3_50/controls/K2'
    # assign same topic to all zones of group 1
    zones = client.get('/api/zones').get_json()
    group1 = [z for z in zones if z['group_id'] == 1]
    if not group1:
        pytest.skip('No zones in group 1')
    for z in group1:
        client.put(f"/api/zones/{z['id']}", json={'mqtt_server_id': 1, 'topic': topic})
    seen_zero = []
    def on_connect(c,u,f,rc,properties=None):
        c.subscribe(topic)
    def on_message(c,u,m):
        try:
            if m.payload.decode('utf-8','ignore') == '0':
                seen_zero.append(1)
        except Exception:
            pass
    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    cl.on_connect = on_connect
    cl.on_message = on_message
    cl.connect(host, port, 5)
    cl.loop_start()
    time.sleep(0.5)
    # call stop group
    r = client.post('/api/groups/1/stop')
    assert r.status_code in (200, 400, 500)
    t0 = time.time()
    # expect at least one '0' publish on shared topic
    while time.time() - t0 < 5 and not seen_zero:
        time.sleep(0.2)
    cl.loop_stop()
    assert seen_zero, 'Did not observe MQTT 0 for group stop'


@pytest.mark.skipif(not os.environ.get('TEST_MQTT_HOST'), reason='External MQTT not configured')
def test_emergency_stop_publishes_zero_for_all_zones(client):
    import paho.mqtt.client as mqtt
    host = os.environ.get('TEST_MQTT_HOST', '127.0.0.1')
    port = int(os.environ.get('TEST_MQTT_PORT', '1883'))
    topic = '/devices/wb-mr6cv3_50/controls/K2'
    zones = client.get('/api/zones').get_json()
    # set mqtt for all zones
    for z in zones:
        client.put(f"/api/zones/{z['id']}", json={'mqtt_server_id': 1, 'topic': topic})
    zeros = []
    def on_connect(c,u,f,rc,properties=None):
        c.subscribe(topic)
    def on_message(c,u,m):
        if m.payload.decode('utf-8','ignore') == '0':
            zeros.append(1)
    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    cl.on_connect = on_connect
    cl.on_message = on_message
    cl.connect(host, port, 5)
    cl.loop_start()
    time.sleep(0.5)
    r = client.post('/api/emergency-stop')
    assert r.status_code in (200, 400, 500)
    t0 = time.time()
    while time.time()-t0 < 5 and not zeros:
        time.sleep(0.2)
    cl.loop_stop()
    assert zeros, 'Did not observe MQTT 0 for emergency stop'
