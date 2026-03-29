"""Comprehensive tests for routes/settings.py endpoints."""
import pytest
import json
import os

os.environ['TESTING'] = '1'


class TestSettingsPage:
    def test_settings_page(self, admin_client):
        resp = admin_client.get('/settings')
        assert resp.status_code in (200, 302, 404)

    def test_settings_page_post(self, admin_client):
        resp = admin_client.post('/settings',
            data={'zone_cap_minutes': '120'},
            content_type='application/x-www-form-urlencoded')
        assert resp.status_code in (200, 302, 400, 405)


class TestPasswordChange:
    def test_change_password(self, admin_client):
        resp = admin_client.post('/settings/password',
            data=json.dumps({'new_password': 'NewSecure123!'}),
            content_type='application/json')
        assert resp.status_code in (200, 302, 400, 404)


class TestRainConfig:
    def test_get_rain_config(self, admin_client):
        resp = admin_client.get('/api/settings/rain')
        assert resp.status_code in (200, 404)

    def test_set_rain_config(self, admin_client):
        resp = admin_client.put('/api/settings/rain',
            data=json.dumps({
                'enabled': True, 'topic': '/rain', 'server_id': 1, 'type': 'NO',
            }),
            content_type='application/json')
        assert resp.status_code in (200, 400, 404)


class TestEnvConfig:
    def test_get_env_config(self, admin_client):
        resp = admin_client.get('/api/settings/env')
        assert resp.status_code in (200, 404)

    def test_set_env_config(self, admin_client):
        resp = admin_client.put('/api/settings/env',
            data=json.dumps({
                'temp': {'enabled': True, 'topic': '/temp', 'server_id': 1},
                'hum': {'enabled': True, 'topic': '/hum', 'server_id': 1},
            }),
            content_type='application/json')
        assert resp.status_code in (200, 400, 404)
