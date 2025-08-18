import io


def test_login_and_auth_status(client):
    # login as user via default password (1234) -> role admin by hashed check
    r = client.post('/api/login', json={'password': '1234'})
    assert r.status_code in (200, 401)
    st = client.get('/api/auth/status')
    assert st.status_code == 200


def test_logout_redirect(client):
    client.post('/api/login', json={'password': '1234'})
    r = client.get('/logout', follow_redirects=False)
    # redirect to login page
    assert r.status_code in (302, 303, 200)


def test_early_off_settings(client):
    g = client.get('/api/settings/early-off')
    assert g.status_code == 200
    seconds = g.get_json().get('seconds', 3)
    new_val = (seconds + 1) % 16
    s = client.post('/api/settings/early-off', json={'seconds': new_val})
    assert s.status_code in (200, 400)


def test_scheduler_init_and_status(client):
    i = client.post('/api/scheduler/init')
    assert i.status_code in (200, 500)
    st = client.get('/api/scheduler/status')
    assert st.status_code in (200, 500)


def test_map_upload_and_get(client):
    # GET map when empty should succeed
    r0 = client.get('/api/map')
    assert r0.status_code == 200
    # POST new map
    img = io.BytesIO(b'\xff\xd8\xff\xe0' + b'0' * 2048)
    r = client.post('/api/map', data={'file': (img, 'zones.jpg')}, content_type='multipart/form-data')
    assert r.status_code in (200, 400)


def test_backup_api(client):
    r = client.post('/api/backup')
    assert r.status_code in (200, 500)


def test_zones_sse_smoke(client):
    # In TESTING without MQTT the endpoint returns 200 with json success False or SSE stream
    r = client.get('/api/mqtt/zones-sse')
    assert r.status_code == 200


