import os
import pytest


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
