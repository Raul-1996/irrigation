import os
import json
import time


def test_mqtt_status_probe_quick(client):
    # Create server from env or default localhost
    host = os.environ.get('TEST_MQTT_HOST', '127.0.0.1')
    port = int(os.environ.get('TEST_MQTT_PORT', '1883'))
    r = client.post('/api/mqtt/servers', json={
        'name': 'pytest', 'host': host, 'port': port, 'enabled': True
    })
    assert r.status_code in (201, 400)
    if r.status_code == 201:
        sid = r.get_json()['server']['id']
        # status shouldn't take long
        st = client.get(f'/api/mqtt/{sid}/status')
        assert st.status_code == 200
        # probe with short duration; should return JSON and not hang
        pr = client.post(f'/api/mqtt/{sid}/probe', json={'filter': '#', 'duration': 1})
        assert pr.status_code == 200
        data = pr.get_json()
        assert 'success' in data

