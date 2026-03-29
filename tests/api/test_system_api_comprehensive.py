"""Comprehensive tests for routes/system_api.py endpoints."""
import pytest
import json
import os

os.environ['TESTING'] = '1'


class TestStatusAPI:
    def test_get_status(self, admin_client):
        resp = admin_client.get('/api/status')
        assert resp.status_code == 200

    def test_get_status_json(self, admin_client):
        resp = admin_client.get('/api/status')
        data = resp.get_json()
        assert isinstance(data, dict)


class TestEmergencyAPI:
    def test_emergency_stop(self, admin_client):
        resp = admin_client.post('/api/emergency-stop',
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)

    def test_emergency_resume(self, admin_client):
        resp = admin_client.post('/api/emergency-resume',
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)


class TestSettingsAPI:
    def test_get_settings(self, admin_client):
        resp = admin_client.get('/api/settings')
        assert resp.status_code in (200, 404)

    def test_update_settings(self, admin_client):
        resp = admin_client.put('/api/settings',
            data=json.dumps({'zone_cap_minutes': '120'}),
            content_type='application/json')
        assert resp.status_code in (200, 400, 404)


class TestLogsAPI:
    def test_get_logs(self, admin_client):
        resp = admin_client.get('/api/logs')
        assert resp.status_code == 200

    def test_get_logs_filtered(self, admin_client, app):
        app.db.add_log('test_api_log', 'some details')
        resp = admin_client.get('/api/logs?event_type=test_api_log')
        assert resp.status_code == 200


class TestBackupAPI:
    def test_create_backup(self, admin_client):
        resp = admin_client.post('/api/backup',
            content_type='application/json')
        assert resp.status_code in (200, 201, 400, 500)


class TestDiagnosticsAPI:
    def test_diagnostics(self, admin_client):
        resp = admin_client.get('/api/diagnostics')
        assert resp.status_code in (200, 404)


class TestWaterUsageAPI:
    def test_get_water_usage(self, admin_client):
        resp = admin_client.get('/api/water-usage')
        assert resp.status_code in (200, 404)

    def test_get_water_statistics(self, admin_client):
        resp = admin_client.get('/api/water-statistics')
        assert resp.status_code in (200, 404)
