import os
import time
import json
import pytest

BASE_URL = os.environ.get('WB_BASE_URL', 'http://127.0.0.1:8080')
TEMP_TOPIC = '/devices/wb-msw-v4_107/controls/Temperature'
HUM_TOPIC = '/devices/wb-msw-v4_107/controls/Humidity'


def _http_get(path):
    import urllib.request
    with urllib.request.urlopen(BASE_URL + path, timeout=5) as r:
        return json.loads(r.read().decode('utf-8'))


def _http_post(path, payload):
    import urllib.request
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(BASE_URL + path, data=data, headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode('utf-8'))


def _publish_retained(temp: int, hum: int):
    try:
        import paho.mqtt.client as mqtt
    except Exception:
        pytest.skip('paho-mqtt not installed in test env')
    # detect server from API
    servers = _http_get('/api/mqtt/servers').get('servers', [])
    enabled = [s for s in servers if int(s.get('enabled') or 0) == 1]
    if not enabled:
        pytest.skip('No enabled MQTT server configured')
    s = enabled[0]
    host = s.get('host') or '127.0.0.1'
    port = int(s.get('port') or 1883)
    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if s.get('username'):
        cl.username_pw_set(s.get('username'), s.get('password') or None)
    cl.connect(host, port, 5)
    cl.publish(TEMP_TOPIC, payload=str(temp), qos=0, retain=True)
    cl.publish(HUM_TOPIC, payload=str(hum), qos=0, retain=True)
    cl.disconnect()


@pytest.mark.timeout(15)
def test_env_values_end_to_end():
    # 1) Ensure env sensors enabled and configured
    servers = _http_get('/api/mqtt/servers').get('servers', [])
    enabled = [s for s in servers if int(s.get('enabled') or 0) == 1]
    if not enabled:
        pytest.skip('No enabled MQTT server configured')
    sid = int(enabled[0]['id'])
    cfg_resp = _http_post('/api/env', {
        'temp': {'enabled': True, 'topic': TEMP_TOPIC, 'server_id': sid},
        'hum': {'enabled': True, 'topic': HUM_TOPIC, 'server_id': sid},
    })
    assert cfg_resp.get('success') is True

    # 2) Publish retained values
    try:
        _publish_retained(21, 55)
    except pytest.skip.Exception:
        raise
    except Exception as e:
        pytest.skip(f'MQTT publish failed or unavailable: {e}')

    # 3) Poll /api/env (values) until numbers appear
    found = False
    for _ in range(20):
        env = _http_get('/api/env')
        if env.get('success'):
            values = env.get('values') or {}
            t = values.get('temp')
            h = values.get('hum')
            if t not in (None, 'нет данных') and h not in (None, 'нет данных'):
                found = True
                break
        time.sleep(0.5)
    assert found, 'Env values did not appear in time'


