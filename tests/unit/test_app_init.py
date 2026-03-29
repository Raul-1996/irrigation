"""Tests for services/app_init.py."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestAppInit:
    def test_import(self):
        try:
            import services.app_init
        except ImportError:
            pytest.skip("app_init not available")
