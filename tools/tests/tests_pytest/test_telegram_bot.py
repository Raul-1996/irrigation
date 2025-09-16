import os
import json

def test_settings_telegram_endpoints(client):
    r = client.get('/api/settings/telegram')
    assert r.status_code in (200, 302)  # may redirect if not admin

    # PUT minimal update
    r2 = client.put('/api/settings/telegram', json={
        'telegram_webhook_secret_path': 'testsecret',
        'telegram_admin_chat_id': '12345'
    })
    assert r2.status_code in (200, 302)

    # Test endpoint
    r3 = client.post('/api/settings/telegram/test')
    assert r3.status_code in (200, 400)


def test_reports_api(client):
    r = client.get('/api/reports?period=today&format=brief')
    assert r.status_code in (200, 302)
    if r.status_code == 200:
        j = r.get_json()
        assert 'text' in j


def test_telegram_webhook_auth_flow(client):
    # webhook without secret
    u = {'message': {'chat': {'id': 42, 'username': 'tg', 'first_name': 'Test'}, 'text': '/start'}}
    r0 = client.post('/telegram/webhook/testsecret', data=json.dumps(u), content_type='application/json')
    # pre-set secret to pass secret check
    client.put('/api/settings/telegram', json={'telegram_webhook_secret_path': 'testsecret'})
    r1 = client.post('/telegram/webhook/testsecret', data=json.dumps(u), content_type='application/json')
    assert r1.status_code == 200

    # auth wrong password
    u2 = {'message': {'chat': {'id': 42}, 'text': '/auth wrong'}}
    r2 = client.post('/telegram/webhook/testsecret', data=json.dumps(u2), content_type='application/json')
    assert r2.status_code == 200
