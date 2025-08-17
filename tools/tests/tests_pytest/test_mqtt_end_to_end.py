import os
import time
import json
import threading
import queue
import pytest

try:
    import paho.mqtt.client as mqtt
except Exception:  # pragma: no cover
    mqtt = None


pytestmark = pytest.mark.skipif(mqtt is None, reason="paho-mqtt not installed")


class MqttSniffer:
    def __init__(self, host: str, port: int, topics: list[str]):
        self.host = host
        self.port = port
        self.topics = topics
        self.events = queue.Queue()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, cl, u, flags, rc, properties=None):
        for t in self.topics:
            try:
                options = mqtt.SubscribeOptions(qos=0, noLocal=False)
                cl.subscribe(t, options=options)
            except Exception:
                cl.subscribe(t, qos=0)

    def _on_message(self, cl, u, msg):
        try:
            topic = msg.topic
        except Exception:
            topic = getattr(msg, 'topic', '')
        try:
            payload = msg.payload.decode('utf-8', 'ignore').strip()
        except Exception:
            payload = str(msg.payload)
        self.events.put((str(topic if str(topic).startswith('/') else '/' + str(topic)), payload))

    def start(self):
        self.client.connect(self.host, self.port, 5)
        self.client.loop_start()

    def stop(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def drain(self):
        drained = []
        while True:
            try:
                drained.append(self.events.get_nowait())
            except queue.Empty:
                break
        return drained


def build_topic(zone_id: int) -> str:
    # mirror project mapping: zone i -> dev = 101 + (i-1)//6, ch = 1 + (i-1)%6
    dev = 101 + (zone_id - 1) // 6
    ch = 1 + (zone_id - 1) % 6
    return f"/devices/wb-mr6cv3_{dev}/controls/K{ch}"


def test_e2e_mqtt_commands(client):
    host = os.environ.get('TEST_MQTT_HOST', '127.0.0.1')
    port = int(os.environ.get('TEST_MQTT_PORT', '1883'))

    # Subscribe to all device topics: 101..105/K1..K6
    topics = [f"/devices/wb-mr6cv3_{dev}/controls/K{ch}" for dev in range(101, 106) for ch in range(1, 7)]
    sniffer = MqttSniffer(host, port, topics)
    sniffer.start()
    time.sleep(0.2)

    # Pick an existing MQTT server id
    servers = client.get('/api/mqtt/servers').get_json().get('servers', [])
    assert servers, 'No MQTT servers configured'
    server_id = servers[0]['id']

    # Normalize server ids and topics in API
    zones = client.get('/api/zones').get_json()
    for z in zones:
        zid = z['id']
        topic = build_topic(zid)
        client.put(f"/api/zones/{zid}", json={'mqtt_server_id': server_id, 'topic': topic})

    # 1) Manual zone start/stop via MQTT endpoints
    z2 = 2
    t2 = build_topic(z2)
    client.post(f"/api/zones/{z2}/mqtt/start")
    time.sleep(0.3)
    ev = sniffer.drain()
    assert (t2, '1') in ev, f"zone {z2} start missing in {ev}"
    client.post(f"/api/zones/{z2}/mqtt/stop")
    time.sleep(0.3)
    ev = sniffer.drain()
    assert (t2, '0') in ev, f"zone {z2} stop missing in {ev}"

    # 2) Exclusive group start: ON for chosen zone, OFF for others in group
    group_id = zones[0]['group_id']
    # choose deterministic first zone in group to avoid ambiguity
    group_zone_ids = sorted([z['id'] for z in zones if z['group_id'] == group_id])
    chosen = group_zone_ids[0]
    chosen_topic = build_topic(chosen)
    # ensure a few peers in same group
    peers = [zid for zid in group_zone_ids if zid != chosen][:3]
    client.post(f"/api/groups/{group_id}/start-zone/{chosen}")
    time.sleep(0.8)
    ev = sniffer.drain()
    # Allow that OFF may be published before or after ON due to concurrent publishes
    ons = [x for x in ev if x == (chosen_topic, '1')]
    assert ons, f"chosen ON missing in {ev}"
    for p in peers:
        offs = [x for x in ev if x == (build_topic(p), '0')]
        assert offs, f"peer OFF for {p} missing in {ev}"

    # 3) Group stop publishes OFF for all group zones
    client.post(f"/api/groups/{group_id}/stop")
    time.sleep(0.5)
    ev = sniffer.drain()
    for z in [zid for zid in [chosen] + peers]:
        assert (build_topic(z), '0') in ev

    # 4) Emergency stop publishes OFF for all zones
    client.post('/api/emergency-stop')
    time.sleep(0.5)
    ev = dict()
    for t, p in sniffer.drain():
        ev.setdefault(t, []).append(p)
    # check at least a representative sample
    sample = [1, 6, 12, 18, 24, 30]
    for sid in sample:
        assert '0' in ev.get(build_topic(sid), []), f"zone {sid} OFF missing in emergency"

    sniffer.stop()


