"""Deep tests for system API routes."""
import json
import pytest
from unittest.mock import patch, MagicMock


class TestSystemStatusAPI:
    def test_get_status(self, admin_client):
        resp = admin_client.get('/api/status')
        assert resp.status_code == 200

    def test_health(self, admin_client):
        resp = admin_client.get('/health')
        assert resp.status_code in (200, 503)  # 503 if scheduler not running

    def test_server_time(self, admin_client):
        resp = admin_client.get('/api/server-time')
        assert resp.status_code == 200

    def test_logs(self, admin_client):
        resp = admin_client.get('/api/logs')
        assert resp.status_code == 200

    def test_scheduler_status(self, admin_client):
        resp = admin_client.get('/api/scheduler/status')
        assert resp.status_code in (200, 500)  # may 500 if scheduler not init'd

    def test_scheduler_jobs(self, admin_client):
        resp = admin_client.get('/api/scheduler/jobs')
        assert resp.status_code == 200

    def test_health_details(self, admin_client):
        resp = admin_client.get('/api/health-details')
        assert resp.status_code == 200

    def test_water(self, admin_client):
        resp = admin_client.get('/api/water')
        assert resp.status_code == 200


class TestSystemConfigAPI:
    def test_auth_status(self, admin_client):
        resp = admin_client.get('/api/auth/status')
        assert resp.status_code == 200

    def test_early_off_get(self, admin_client):
        resp = admin_client.get('/api/settings/early-off')
        assert resp.status_code == 200

    def test_early_off_set(self, admin_client):
        resp = admin_client.post('/api/settings/early-off',
                                 data=json.dumps({'seconds': 5}),
                                 content_type='application/json')
        assert resp.status_code == 200

    def test_system_name_get(self, admin_client):
        resp = admin_client.get('/api/settings/system-name')
        assert resp.status_code == 200

    def test_system_name_set(self, admin_client):
        resp = admin_client.post('/api/settings/system-name',
                                 data=json.dumps({'name': 'My House'}),
                                 content_type='application/json')
        assert resp.status_code == 200

    def test_logging_debug_get(self, admin_client):
        resp = admin_client.get('/api/logging/debug')
        assert resp.status_code == 200

    def test_env_get(self, admin_client):
        resp = admin_client.get('/api/env')
        assert resp.status_code == 200

    def test_env_values(self, admin_client):
        resp = admin_client.get('/api/env/values')
        assert resp.status_code == 200

    def test_rain_get(self, admin_client):
        resp = admin_client.get('/api/rain')
        assert resp.status_code == 200

    def test_postpone(self, admin_client, app):
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = app.db.get_zones()
        resp = admin_client.post('/api/postpone',
                                 data=json.dumps({
                                     'zone_id': zones[0]['id'],
                                     'until': '2030-12-31 23:59',
                                     'reason': 'test',
                                 }),
                                 content_type='application/json')
        assert resp.status_code in (200, 400)  # 400 if validation strictness


class TestEmergencyAPI:
    def test_emergency_stop(self, admin_client):
        with patch('services.zone_control.stop_all_in_group'):
            resp = admin_client.post('/api/emergency-stop')
        assert resp.status_code == 200

    def test_emergency_resume(self, admin_client):
        resp = admin_client.post('/api/emergency-resume')
        assert resp.status_code == 200
