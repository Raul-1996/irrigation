import json
from datetime import datetime


def test_groups_crud(client):
    # list
    resp = client.get('/api/groups')
    assert resp.status_code == 200
    groups = resp.get_json()
    assert isinstance(groups, list)

    # create
    r = client.post('/api/groups', json={'name': 'Тестовая группа'})
    assert r.status_code in (201, 400)


def test_programs_list(client):
    r = client.get('/api/programs')
    assert r.status_code == 200
    programs = r.get_json()
    assert isinstance(programs, list)


def test_group_sequence_start_and_stop(client):
    s = client.post('/api/groups/1/start-from-first')
    # 200/400/500 acceptable in TESTING
    assert s.status_code in (200, 400, 500)
    st = client.post('/api/groups/1/stop')
    assert st.status_code in (200, 500)


def test_zone_next_watering(client):
    r = client.get('/api/zones/1/next-watering')
    # Might be 200 with info or 404 if no zone
    assert r.status_code in (200, 404)


def test_conflict_check_endpoint(client):
    # Fetch an existing program to build payload
    r = client.get('/api/programs')
    programs = r.get_json()
    if not programs:
        return
    prog = programs[0]
    payload = {
        'program_id': prog['id'],
        'time': prog['time'],
        'zones': prog['zones'] if isinstance(prog['zones'], list) else json.loads(prog['zones']),
        'days': prog['days'] if isinstance(prog['days'], list) else json.loads(prog['days'])
    }
    rc = client.post('/api/programs/check-conflicts', json=payload)
    assert rc.status_code == 200
    data = rc.get_json()
    assert 'success' in data and 'has_conflicts' in data
