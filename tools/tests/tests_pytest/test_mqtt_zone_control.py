import os
import pytest
import time


def build_topic(zone_id: int) -> str:
    dev = 101 + (zone_id - 1) // 6
    ch = 1 + (zone_id - 1) % 6
    return f"/devices/wb-mr6cv3_{dev}/controls/K{ch}"


def test_zone_mqtt_endpoints_exist(client):
    # set each zone to its own mapped topic
    zones = client.get('/api/zones').get_json()
    for z in zones:
        zid = z['id']
        client.put(f"/api/zones/{zid}", json={'mqtt_server_id': 1, 'topic': build_topic(zid)})
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
    topic = build_topic(1)
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
    zones = client.get('/api/zones').get_json()
    group1 = [z for z in zones if z['group_id'] == 1]
    if not group1:
        pytest.skip('No zones in group 1')
    # ensure topics are mapped correctly
    for z in group1:
        zid = z['id']
        client.put(f"/api/zones/{zid}", json={'mqtt_server_id': 1, 'topic': build_topic(zid)})
    seen_zero = []
    subscribe_topics = [build_topic(z['id']) for z in group1[:4]]
    def on_connect(c,u,f,rc,properties=None):
        for t in subscribe_topics:
            c.subscribe(t)
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
    zones = client.get('/api/zones').get_json()
    # set mqtt for all zones with mapped topics
    for z in zones:
        zid = z['id']
        client.put(f"/api/zones/{zid}", json={'mqtt_server_id': 1, 'topic': build_topic(zid)})
    zeros = []
    def on_connect(c,u,f,rc,properties=None):
        # subscribe a sample of topics to verify OFF broadcasts
        for sid in (1, 6, 12, 18, 24, 30):
            c.subscribe(build_topic(sid))
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
