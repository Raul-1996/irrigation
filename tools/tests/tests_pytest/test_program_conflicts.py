import json

from database import db


def test_program_conflicts_endpoint(client):
    progs = db.get_programs() or []
    zones = db.get_zones() or []
    if not zones:
        return
    # Build a payload for existing or synthetic program
    if progs:
        p = progs[0]
        payload = {
            'program_id': p['id'],
            'time': p['time'],
            'zones': p['zones'] if isinstance(p['zones'], list) else json.loads(p['zones']),
            'days': p['days'] if isinstance(p['days'], list) else json.loads(p['days'])
        }
    else:
        zid = zones[0]['id']
        payload = {'program_id': None, 'time': '06:00', 'zones': [zid], 'days': [0]}
    r = client.post('/api/programs/check-conflicts', json=payload)
    assert r.status_code == 200
    data = r.get_json()
    assert 'success' in data and 'has_conflicts' in data

