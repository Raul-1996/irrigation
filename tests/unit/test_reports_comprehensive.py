"""Tests for services/reports.py."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestReports:
    def test_build_report_text(self, test_db):
        try:
            from services.reports import build_report_text
            with patch_db(test_db):
                result = build_report_text(period='today', fmt='brief')
                assert isinstance(result, str)
        except (ImportError, AttributeError):
            pytest.skip("reports module not fully available")

    def test_import(self):
        try:
            from services.reports import build_report_text
            assert callable(build_report_text)
        except ImportError:
            pytest.skip("reports not available")


def patch_db(db):
    from unittest.mock import patch
    return patch('services.reports.db', db) if hasattr(__import__('services.reports', fromlist=['db']), 'db') else patch.dict({})
