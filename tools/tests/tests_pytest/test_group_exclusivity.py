from database import db


def test_group_exclusive_start_zone(client):
    zones = db.get_zones()
    assert zones, 'No zones'
    # choose a group with >=2 zones if possible
    by_group = {}
    for z in zones:
        by_group.setdefault(z['group_id'], []).append(z)
    gid, group_zones = next(iter(by_group.items()))
    if len(group_zones) < 2:
        # create one more in same group
        db.create_zone({'name': 'peer', 'icon': '🌿', 'duration': 1, 'group_id': gid})
        group_zones = db.get_zones_by_group(gid)
    # turn all off
    for z in db.get_zones_by_group(gid):
        db.update_zone(z['id'], {'state': 'off', 'watering_start_time': None})
    a = group_zones[0]['id']
    b = group_zones[1]['id']
    # start zone a exclusively via API
    client.post(f'/api/groups/{gid}/start-zone/{a}')
    import time; time.sleep(5)
    # Fetch states
    z_a = db.get_zone(a); z_b = db.get_zone(b)
    assert z_a['state'] == 'on'
    assert z_b['state'] == 'off'

