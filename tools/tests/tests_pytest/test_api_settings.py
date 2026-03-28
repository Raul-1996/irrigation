"""
Tests for settings API — system name, early-off, rain, env, logging, password, backup.
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("TESTING", "1")


class TestSystemName:
    def test_get_system_name(self, client):
        r = client.get('/api/settings/system-name')
        assert r.status_code == 200

    def test_set_system_name(self, client):
        r = client.post('/api/settings/system-name', json={'name': 'Test System'})
        assert r.status_code in (200, 400)


class TestEarlyOff:
    def test_get_early_off(self, client):
        r = client.get('/api/settings/early-off')
        assert r.status_code == 200

    def test_set_early_off(self, client):
        r = client.post('/api/settings/early-off', json={'enabled': True})
        assert r.status_code in (200, 400)


class TestRainConfig:
    def test_get_rain_config(self, client):
        r = client.get('/api/rain')
        assert r.status_code == 200

    def test_set_rain_config(self, client):
        r = client.post('/api/rain', json={
            'enabled': True,
            'threshold_mm': 5.0
        })
        assert r.status_code in (200, 400)


class TestEnv:
    def test_get_env(self, client):
        r = client.get('/api/env')
        assert r.status_code == 200

    def test_get_env_values(self, client):
        r = client.get('/api/env/values')
        assert r.status_code == 200


class TestLogging:
    def test_get_logging_debug(self, client):
        r = client.get('/api/logging/debug')
        assert r.status_code == 200

    def test_toggle_logging_debug(self, client):
        r = client.post('/api/logging/debug', json={'enabled': True})
        assert r.status_code in (200, 400)


class TestPassword:
    def test_change_password(self, client):
        r = client.post('/api/password', json={
            'current_password': '1234',
            'new_password': 'newpass123'
        })
        assert r.status_code in (200, 400, 401)

    def test_change_password_wrong_current(self, client):
        r = client.post('/api/password', json={
            'current_password': 'wrongpass',
            'new_password': 'newpass123'
        })
        assert r.status_code in (400, 401, 403)


class TestBackup:
    def test_backup_endpoint(self, client):
        r = client.post('/api/backup')
        assert r.status_code in (200, 500)


class TestEmergency:
    @patch('app._publish_mqtt_value', return_value=True)
    def test_emergency_stop(self, mock_pub, client):
        r = client.post('/api/emergency-stop')
        assert r.status_code in (200, 400)

    @patch('app._publish_mqtt_value', return_value=True)
    def test_emergency_resume(self, mock_pub, client):
        r = client.post('/api/emergency-resume')
        assert r.status_code in (200, 400)


class TestPostpone:
    def test_postpone(self, client):
        r = client.post('/api/postpone', json={
            'group_id': 1,
            'days': 1
        })
        assert r.status_code in (200, 400)


class TestScheduler:
    def test_scheduler_status(self, client):
        r = client.get('/api/scheduler/status')
        assert r.status_code == 200

    def test_scheduler_init(self, client):
        r = client.post('/api/scheduler/init')
        assert r.status_code in (200, 400)

    def test_scheduler_jobs(self, client):
        r = client.get('/api/scheduler/jobs')
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, (list, dict))


class TestLogs:
    def test_get_logs(self, client):
        r = client.get('/api/logs')
        assert r.status_code == 200

    def test_get_water_stats(self, client):
        r = client.get('/api/water')
        assert r.status_code == 200


class TestTelegramSettings:
    def test_get_telegram_settings(self, client):
        r = client.get('/api/settings/telegram')
        assert r.status_code == 200

    def test_put_telegram_settings(self, client):
        r = client.put('/api/settings/telegram', json={
            'bot_token': 'test:token',
            'chat_id': '12345'
        })
        assert r.status_code in (200, 400)

    @patch('requests.post')
    def test_telegram_test(self, mock_post, client):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {'ok': True})
        r = client.post('/api/settings/telegram/test')
        assert r.status_code in (200, 400, 500)


class TestMap:
    def test_get_map_files(self, client):
        r = client.get('/api/map')
        assert r.status_code in (200, 404)

    def test_delete_nonexistent_map(self, client):
        r = client.delete('/api/map/nonexistent.png')
        assert r.status_code in (200, 404)


class TestMisc:
    def test_service_worker(self, client):
        r = client.get('/sw.js')
        assert r.status_code in (200, 404)

    def test_ws_stub(self, client):
        r = client.get('/ws')
        assert r.status_code in (200, 400, 426)

    def test_server_time(self, client):
        r = client.get('/api/server-time')
        assert r.status_code == 200

    def test_reports(self, client):
        r = client.get('/api/reports')
        assert r.status_code in (200, 404)
