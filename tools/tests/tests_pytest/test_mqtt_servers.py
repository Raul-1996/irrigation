def test_mqtt_servers_crud(client):
    # initial list
    r_list = client.get('/api/mqtt/servers')
    assert r_list.status_code == 200
    data = r_list.get_json()
    assert data.get('success') is True

    # create
    payload = {
        'name': 'WB MQTT',
        'host': '127.0.0.1',
        'port': 1883,
        'username': 'user',
        'password': 'pass',
        'client_id': 'wb-test-1',
        'enabled': True
    }
    r_create = client.post('/api/mqtt/servers', json=payload)
    assert r_create.status_code in (201, 400)
    created = r_create.get_json()
    if r_create.status_code == 201:
        server_id = created['server']['id']
        # get
        r_get = client.get(f'/api/mqtt/servers/{server_id}')
        assert r_get.status_code == 200
        # update
        r_upd = client.put(f'/api/mqtt/servers/{server_id}', json={'name': 'WB MQTT Updated', 'host': 'mqtt.local', 'port': 1884})
        assert r_upd.status_code == 200
        # delete
        r_del = client.delete(f'/api/mqtt/servers/{server_id}')
        assert r_del.status_code in (204, 400)
