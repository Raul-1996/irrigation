import os
import time
import pytest
from app import app as flask_app


@pytest.mark.timeout(20)
def test_manual_stop_mid_zone(monkeypatch):
    os.environ['TESTING'] = '1'
    client = flask_app.test_client()

    r = client.get('/api/zones')
    assert r.status_code == 200
    zones = r.get_json() or []
    if not zones:
        pytest.skip('No zones configured')

    # pick first zone in group 1
    z = next((z for z in zones if z.get('group_id') == 1), None)
    if not z:
        pytest.skip('No zone in group 1')
    zid = z['id']

    # start zone
    r = client.post(f'/api/zones/{zid}/mqtt/start')
    assert r.status_code == 200
    data = r.get_json(); assert data and data.get('success')
    time.sleep(1)

    # stop zone manually
    r = client.post(f'/api/zones/{zid}/mqtt/stop')
    assert r.status_code == 200
    data = r.get_json(); assert data and data.get('success')

    # Ensure zone goes OFF quickly and no lingering jobs
    r = client.get('/api/zones')
    z2 = next((zz for zz in r.get_json() if zz['id'] == zid), None)
    assert z2 and z2.get('state') == 'off'


