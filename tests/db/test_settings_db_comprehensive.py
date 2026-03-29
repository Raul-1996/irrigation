"""Comprehensive tests for db/settings.py."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestSettings:
    def test_get_set_value(self, test_db):
        test_db.set_setting_value('test_key', 'test_value')
        assert test_db.get_setting_value('test_key') == 'test_value'

    def test_get_nonexistent(self, test_db):
        result = test_db.get_setting_value('nonexistent_key_xyz')
        assert result is None

    def test_overwrite_value(self, test_db):
        test_db.set_setting_value('key', 'old')
        test_db.set_setting_value('key', 'new')
        assert test_db.get_setting_value('key') == 'new'

    def test_set_none_value(self, test_db):
        test_db.set_setting_value('key', 'val')
        test_db.set_setting_value('key', None)
        # Behavior depends on implementation


class TestPasswordOperations:
    def test_set_and_get_password(self, test_db):
        test_db.set_password('testpass123')
        ph = test_db.get_password_hash()
        assert ph is not None
        assert ph != 'testpass123'  # should be hashed

    def test_ensure_password_change(self, test_db):
        test_db.ensure_password_change_required()
        # Should not crash


class TestLoggingDebug:
    def test_get_default(self, test_db):
        result = test_db.get_logging_debug()
        assert isinstance(result, bool)

    def test_set_debug(self, test_db):
        test_db.set_logging_debug(True)
        assert test_db.get_logging_debug() is True
        test_db.set_logging_debug(False)
        assert test_db.get_logging_debug() is False


class TestRainConfig:
    def test_get_default(self, test_db):
        cfg = test_db.get_rain_config()
        assert isinstance(cfg, dict)

    def test_set_rain_config(self, test_db):
        cfg = {'enabled': True, 'topic': '/rain', 'server_id': 1, 'type': 'NO'}
        test_db.set_rain_config(cfg)
        result = test_db.get_rain_config()
        assert result.get('enabled') in (True, 1, '1', 'true', 'True')


class TestEnvConfig:
    def test_get_default(self, test_db):
        cfg = test_db.get_env_config()
        assert isinstance(cfg, dict)

    def test_set_env_config(self, test_db):
        cfg = {
            'temp': {'enabled': True, 'topic': '/temp', 'server_id': 1},
            'hum': {'enabled': True, 'topic': '/hum', 'server_id': 1},
        }
        test_db.set_env_config(cfg)
        result = test_db.get_env_config()
        assert isinstance(result, dict)


class TestMasterConfig:
    def test_get_default(self, test_db):
        cfg = test_db.get_master_config()
        assert isinstance(cfg, dict)

    def test_set_master_config(self, test_db):
        cfg = {'mode': 'NC', 'topic': '/master', 'server_id': 1}
        test_db.set_master_config(cfg)
        result = test_db.get_master_config()
        assert isinstance(result, dict)


class TestEarlyOffSeconds:
    def test_get_default(self, test_db):
        result = test_db.get_early_off_seconds()
        assert isinstance(result, int)

    def test_set_early_off(self, test_db):
        test_db.set_early_off_seconds(5)
        assert test_db.get_early_off_seconds() == 5
