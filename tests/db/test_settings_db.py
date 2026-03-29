"""Tests for settings DB: settings, password, migrations."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestSettings:
    def test_get_set_setting(self, test_db):
        test_db.set_setting_value('test_key', 'test_value')
        assert test_db.get_setting_value('test_key') == 'test_value'

    def test_get_nonexistent_setting(self, test_db):
        result = test_db.get_setting_value('nonexistent_key_12345')
        assert result is None

    def test_overwrite_setting(self, test_db):
        test_db.set_setting_value('k', 'v1')
        test_db.set_setting_value('k', 'v2')
        assert test_db.get_setting_value('k') == 'v2'

    def test_set_none_value(self, test_db):
        test_db.set_setting_value('nullable', None)
        assert test_db.get_setting_value('nullable') is None


class TestPassword:
    def test_default_password(self, test_db):
        """Default password should be set."""
        h = test_db.get_password_hash()
        assert h is not None

    def test_set_password(self, test_db):
        test_db.set_password('new_secure_password')
        h = test_db.get_password_hash()
        assert h is not None
        from werkzeug.security import check_password_hash
        assert check_password_hash(h, 'new_secure_password')

    def test_set_password_changes_hash(self, test_db):
        old_hash = test_db.get_password_hash()
        test_db.set_password('another_password_99')
        new_hash = test_db.get_password_hash()
        assert old_hash != new_hash


class TestLoggingDebug:
    def test_get_set_debug(self, test_db):
        test_db.set_logging_debug(True)
        assert test_db.get_logging_debug() is True

    def test_disable_debug(self, test_db):
        test_db.set_logging_debug(True)
        test_db.set_logging_debug(False)
        assert test_db.get_logging_debug() is False


class TestRainConfig:
    def test_get_default_rain_config(self, test_db):
        cfg = test_db.get_rain_config()
        assert isinstance(cfg, dict)

    def test_set_rain_config(self, test_db):
        cfg = {'enabled': True, 'topic': '/rain/sensor', 'type': 'NO', 'server_id': 1}
        test_db.set_rain_config(cfg)
        stored = test_db.get_rain_config()
        assert stored.get('enabled') is True or stored.get('enabled') == 'True'


class TestEarlyOff:
    def test_get_default_early_off(self, test_db):
        val = test_db.get_early_off_seconds()
        assert isinstance(val, int)

    def test_set_early_off(self, test_db):
        test_db.set_early_off_seconds(5)
        assert test_db.get_early_off_seconds() == 5
