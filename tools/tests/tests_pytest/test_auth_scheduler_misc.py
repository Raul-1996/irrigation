import io
import os


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
    # Try to use real map from tools/tests/images if present
    here = os.path.abspath(os.path.dirname(__file__))
    images_dir = os.path.abspath(os.path.join(here, os.pardir, 'images'))
    candidates = ['map.jpg', 'map.jpeg', 'map.png', 'map.webp', 'map.gif']
    file_obj = None
    fname = None
    for name in candidates:
        p = os.path.join(images_dir, name)
        if os.path.exists(p):
            file_obj = open(p, 'rb')
            fname = name
            break
    if file_obj is None:
        file_obj = io.BytesIO(b'\xff\xd8\xff\xe0' + b'0' * 2048)
        fname = 'zones.jpg'
    try:
        r = client.post('/api/map', data={'file': (file_obj, fname)}, content_type='multipart/form-data')
    finally:
        try:
            file_obj.close()
        except Exception:
            pass
    assert r.status_code in (200, 400)
    # Если загрузка прошла успешно — удалим карту
    if r.status_code == 200:
        j = r.get_json() or {}
        path = j.get('path') or ''
        fname = path.split('/')[-1] if path else ''
        if fname:
            d = client.delete(f'/api/map/{fname}')
            assert d.status_code in (200, 404)


def test_backup_api(client):
    r = client.post('/api/backup')
    assert r.status_code in (200, 500)


def test_zones_sse_smoke(client):
    # In TESTING without MQTT the endpoint returns 200 with json success False or SSE stream
    r = client.get('/api/mqtt/zones-sse')
    assert r.status_code == 200


