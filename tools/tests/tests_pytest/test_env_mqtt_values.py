"""Tests for environment MQTT values using Flask test client."""
import os
import json
import pytest


@pytest.mark.timeout(10)
def test_env_values_config(client):
    """Test env config endpoint via test client."""
    # Get current env config
    r = client.get('/api/env')
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, dict)


@pytest.mark.timeout(10)
def test_env_values_set_config(client):
    """Test setting env config."""
    servers = client.get('/api/mqtt/servers').get_json()
    server_list = servers.get('servers', [])
    enabled = [s for s in server_list if int(s.get('enabled') or 0) == 1]
    if not enabled:
        pytest.skip('No enabled MQTT server configured')
    sid = int(enabled[0]['id'])

    cfg_resp = client.post('/api/env', json={
        'temp': {'enabled': True, 'topic': '/devices/wb-msw-v4_107/controls/Temperature', 'server_id': sid},
        'hum': {'enabled': True, 'topic': '/devices/wb-msw-v4_107/controls/Humidity', 'server_id': sid},
    })
    assert cfg_resp.status_code == 200
    data = cfg_resp.get_json()
    assert isinstance(data, dict)
