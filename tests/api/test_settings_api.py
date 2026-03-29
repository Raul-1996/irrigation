"""Tests for settings API: early-off, weather, location."""
import pytest
import json
import os

os.environ['TESTING'] = '1'


class TestEarlyOffAPI:
    def test_get_early_off(self, admin_client):
        resp = admin_client.get('/api/settings/early-off')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'seconds' in data

    def test_set_early_off(self, admin_client):
        resp = admin_client.post('/api/settings/early-off',
            data=json.dumps({'seconds': 5}),
            content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

    def test_set_early_off_out_of_range(self, admin_client):
        resp = admin_client.post('/api/settings/early-off',
            data=json.dumps({'seconds': 99}),
            content_type='application/json')
        assert resp.status_code == 400


class TestSystemNameAPI:
    def test_get_system_name(self, admin_client):
        resp = admin_client.get('/api/settings/system-name')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'name' in data

    def test_set_system_name(self, admin_client):
        resp = admin_client.post('/api/settings/system-name',
            data=json.dumps({'name': 'Test System'}),
            content_type='application/json')
        assert resp.status_code == 200


class TestRainConfigAPI:
    def test_get_rain_config(self, admin_client):
        resp = admin_client.get('/api/rain')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

    def test_set_rain_config(self, admin_client):
        resp = admin_client.post('/api/rain',
            data=json.dumps({
                'enabled': False,
                'topic': '',
                'type': 'NO',
            }),
            content_type='application/json')
        assert resp.status_code == 200


class TestEnvConfigAPI:
    def test_get_env_config(self, admin_client):
        resp = admin_client.get('/api/env')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

    def test_get_env_values(self, admin_client):
        resp = admin_client.get('/api/env/values')
        assert resp.status_code == 200

    def test_set_env_config(self, admin_client):
        resp = admin_client.post('/api/env',
            data=json.dumps({
                'temp': {'enabled': False, 'topic': '', 'server_id': None},
                'hum': {'enabled': False, 'topic': '', 'server_id': None},
            }),
            content_type='application/json')
        assert resp.status_code == 200


class TestLoggingDebugAPI:
    def test_get_debug(self, admin_client):
        resp = admin_client.get('/api/logging/debug')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'debug' in data

    def test_set_debug(self, admin_client):
        resp = admin_client.post('/api/logging/debug',
            data=json.dumps({'enabled': True}),
            content_type='application/json')
        assert resp.status_code == 200
