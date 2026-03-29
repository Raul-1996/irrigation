"""Tests for config.py."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestConfig:
    def test_config_class(self):
        from config import Config
        assert Config.SECRET_KEY is not None
        assert Config.WTF_CSRF_ENABLED is True

    def test_test_config(self):
        from config import TestConfig
        assert TestConfig.TESTING is True
        assert TestConfig.WTF_CSRF_ENABLED is False

    def test_load_or_generate_secret(self):
        from config import _load_or_generate_secret
        key = _load_or_generate_secret()
        assert key is not None
        assert len(key) > 0

    def test_load_from_env(self):
        from config import _load_or_generate_secret
        os.environ['SECRET_KEY'] = 'my-custom-secret-key'
        try:
            key = _load_or_generate_secret()
            assert key == 'my-custom-secret-key'
        finally:
            os.environ['SECRET_KEY'] = 'test-secret-key-for-testing-only'

    def test_ignore_default_secret(self):
        from config import _load_or_generate_secret
        os.environ['SECRET_KEY'] = 'wb-irrigation-secret'
        try:
            key = _load_or_generate_secret()
            assert key != 'wb-irrigation-secret'  # should not use old default
        finally:
            os.environ['SECRET_KEY'] = 'test-secret-key-for-testing-only'
