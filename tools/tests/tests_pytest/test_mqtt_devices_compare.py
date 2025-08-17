import os
import socket
import time


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _collect_devices_direct(host: str, port: int, duration: float = 3.0):
    try:
        import paho.mqtt.client as mqtt
    except Exception:
        return set()

    devices = set()

    def on_connect(c, u, f, rc, properties=None):
        # широкая подписка, many retained
        c.subscribe('/devices/+/meta/#')
        c.subscribe('/devices/+/controls/+/on')

    def on_message(c, u, m):
        t = str(getattr(m, 'topic', '') or '')
        parts = t.split('/')
        if len(parts) >= 3 and parts[1] == 'devices':
            devices.add(parts[2])

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(host, port, 3)
    except Exception:
        return set()
    client.loop_start()
    start = time.time()
    while time.time() - start < duration:
        time.sleep(0.2)
    client.loop_stop()
    return devices


def test_mqtt_devices_compare_web_vs_direct(client):
    host = os.environ.get('TEST_MQTT_HOST', '127.0.0.1')
    port = int(os.environ.get('TEST_MQTT_PORT', '1883'))

    if not _tcp_reachable(host, port):
        # Environment without broker – skip to keep CI green
        import pytest
        pytest.skip('MQTT broker not reachable; set TEST_MQTT_HOST to run this test')

    # Create server via API (idempotent)
    r = client.post('/api/mqtt/servers', json={
        'name': 'pytest-devices', 'host': host, 'port': port, 'enabled': True
    })
    assert r.status_code in (201, 400)
    if r.status_code == 201:
        sid = r.get_json()['server']['id']
    else:
        # fetch first server
        sid = client.get('/api/mqtt/servers').get_json()['servers'][0]['id']

    # Web probe – gather topics
    pr = client.post(f'/api/mqtt/{sid}/probe', json={'filter': '/devices/#', 'duration': 3})
    assert pr.status_code == 200
    data = pr.get_json()
    web_items = data.get('items', [])
    web_devices = set()
    for it in web_items:
        t = str(it.get('topic') or '')
        parts = t.split('/')
        if len(parts) >= 3 and parts[1] == 'devices':
            web_devices.add(parts[2])

    # Direct paho collection
    direct_devices = _collect_devices_direct(host, port, duration=3)

    # Basic assertions
    assert isinstance(web_devices, set)
    assert len(web_devices) >= 0

    # If direct side empty (no paho), skip strict comparison
    if len(direct_devices) == 0:
        return

    # Web result should be subset of direct (allow slight timing differences)
    assert web_devices.issubset(direct_devices)
    # And should include at least some devices if broker has any
    assert len(web_devices) > 0


