import time
import json

def test_logs_update_after_zone_actions(client):
    # 1) Read logs before
    r = client.get('/api/logs')
    assert r.status_code == 200
    before = r.get_json()
    before_count = len(before)
    before_max_id = max((row.get('id') or 0) for row in before) if before else 0

    # 2) Start/stop first two zones via API
    # Ensure admin session for /logs page access
    client.post('/api/login', json={'password': '1234'})
    for zid in (1, 2):
        # start
        rs = client.post(f'/api/zones/{zid}/mqtt/start', json={})
        assert rs.status_code in (200, 202)
        # slight wait to let DB write
        time.sleep(0.1)
        # stop
        rt = client.post(f'/api/zones/{zid}/mqtt/stop', json={})
        assert rt.status_code in (200, 202)
        time.sleep(0.1)

    # 3) Read logs after
    r2 = client.get('/api/logs')
    assert r2.status_code == 200
    after = r2.get_json()
    after_count = len(after)
    # New rows by id
    new_rows = [row for row in after if (row.get('id') or 0) > before_max_id]

    # Expect at least a couple of new records
    assert after_count - before_count >= 2 or len(new_rows) >= 2

    # Recent records must include zone_start/zone_stop
    recent_types = {item.get('type') for item in (new_rows or after[:20])}
    assert {'zone_start', 'zone_stop'} & recent_types

    # 4) Check that the logs page is accessible
    html = client.get('/logs')
    assert html.status_code == 200
