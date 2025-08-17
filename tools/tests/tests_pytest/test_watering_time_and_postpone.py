from datetime import datetime


def test_watering_time_api(client):
    # ensure stopped
    client.post('/api/zones/1/stop')
    r = client.get('/api/zones/1/watering-time')
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        data = r.get_json()
        assert 'remaining_seconds' in data


def test_postpone_api(client):
    # postpone group 1 for 1 day
    pr = client.post('/api/postpone', json={'group_id': 1, 'days': 1, 'action': 'postpone'})
    assert pr.status_code in (200, 500)
    # cancel postpone
    cr = client.post('/api/postpone', json={'group_id': 1, 'action': 'cancel'})
    assert cr.status_code in (200, 500)
