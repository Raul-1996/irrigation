"""Tests for config.py."""
import pytest


class TestConfig:
    def test_base_config(self):
        from config import Config
        assert Config.SECRET_KEY is not None

    def test_testing_config(self):
        from config import TestConfig
        assert TestConfig.TESTING is True
        assert TestConfig.WTF_CSRF_ENABLED is False

    def test_emergency_stop_default(self):
        from config import Config
        assert hasattr(Config, 'EMERGENCY_STOP')
