import os
import time
import pytest
from app import app as flask_app


@pytest.mark.timeout(15)
def test_group_cancel_immediate_off(monkeypatch):
    os.environ['TESTING'] = '1'
    client = flask_app.test_client()

    # Ensure zones exist
    r = client.get('/api/zones')
    assert r.status_code == 200
    zones = r.get_json() or []
    if not zones:
        pytest.skip('No zones configured')

    # Start group sequence for group 1
    t0 = time.time()
    r = client.post('/api/groups/1/start-from-first')
    assert r.status_code == 200
    data = r.get_json(); assert data and data.get('success')

    # Wait a moment to let first zone switch ON (test env can be slower)
    time.sleep(0.5)
    r = client.get('/api/zones')
    zlist = r.get_json() or []
    had_on = any(z.get('group_id') == 1 and z.get('state') == 'on' for z in zlist)
    assert had_on, 'no ON zone found after group start'
    start_to_on_s = time.time() - t0
    assert start_to_on_s <= 3.0, f"Group first zone ON took too long: {start_to_on_s:.3f}s"

    # Stop group and expect immediate OFF (<= 3s)
    t1 = time.time()
    r = client.post('/api/groups/1/stop')
    assert r.status_code == 200
    data = r.get_json(); assert data and data.get('success')

    # Re-read zones, expect all in group off (without waiting 20s)
    r = client.get('/api/zones')
    zlist2 = r.get_json() or []
    assert all(z.get('state') == 'off' for z in zlist2 if z.get('group_id') == 1)
    stop_elapsed = time.time() - t1
    assert stop_elapsed <= 3.0, f"Group OFF propagation exceeded 3s: {stop_elapsed:.3f}s"

    # Ensure there are no leftover stop jobs for zones in group 1
    r = client.get('/api/health-details')
    assert r.status_code == 200
    hd = r.get_json() or {}
    jobs = [j.get('id') for j in (hd.get('jobs') or [])]
    group_zone_ids = [int(z['id']) for z in zlist2 if z.get('group_id') == 1]
    for zid in group_zone_ids:
        assert not any(str(jid).startswith(f"zone_stop:{zid}:") for jid in jobs), f"leftover zone_stop for Z{zid}"
        assert f"zone_hard_stop:{zid}" not in jobs, f"leftover zone_hard_stop for Z{zid}"


