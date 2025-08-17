def test_mqtt_servers_crud(client):
    # initial list
    r_list = client.get('/api/mqtt/servers')
    assert r_list.status_code == 200
    data = r_list.get_json()
    assert data.get('success') is True

    servers = data.get('servers') or []
    if servers:
        server_id = servers[0]['id']
    else:
        # create
        payload = {
            'name': 'local',
            'host': '127.0.0.1',
            'port': 1883,
            'enabled': True
        }
        r_create = client.post('/api/mqtt/servers', json=payload)
        assert r_create.status_code in (201, 400)
        if r_create.status_code == 201:
            server_id = r_create.get_json()['server']['id']
        else:
            # if 400, fetch again
            server_id = client.get('/api/mqtt/servers').get_json()['servers'][0]['id']

    # get
    r_get = client.get(f'/api/mqtt/servers/{server_id}')
    assert r_get.status_code == 200
    # update
    r_upd = client.put(f'/api/mqtt/servers/{server_id}', json={'name': 'local-upd', 'host': '127.0.0.1', 'port': 1883})
    assert r_upd.status_code == 200
    # delete
    r_del = client.delete(f'/api/mqtt/servers/{server_id}')
    assert r_del.status_code in (204, 400)
    # ensure one default exists for other tests
    client.post('/api/mqtt/servers', json={'name': 'local', 'host': '127.0.0.1', 'port': 1883})
