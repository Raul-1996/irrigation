"""Comprehensive tests for services/helpers.py."""
import pytest
import os
from flask import Flask

os.environ['TESTING'] = '1'


class TestApiError:
    def test_basic_error(self):
        from services.helpers import api_error
        app = Flask(__name__)
        with app.app_context():
            resp, status = api_error('ERR_TEST', 'Test error')
            assert status == 400
            data = resp.get_json()
            assert data['success'] is False
            assert data['error_code'] == 'ERR_TEST'

    def test_custom_status(self):
        from services.helpers import api_error
        app = Flask(__name__)
        with app.app_context():
            _, status = api_error('ERR', 'msg', status=404)
            assert status == 404

    def test_extra_fields(self):
        from services.helpers import api_error
        app = Flask(__name__)
        with app.app_context():
            resp, _ = api_error('ERR', 'msg', extra={'detail': 'info'})
            data = resp.get_json()
            assert data.get('detail') == 'info'


class TestApiSoft:
    def test_soft_200(self):
        from services.helpers import api_soft
        app = Flask(__name__)
        with app.app_context():
            resp, status = api_soft('WARN', 'soft warning')
            assert status == 200
            data = resp.get_json()
            assert data['success'] is False


class TestParseDt:
    def test_parse_full(self):
        from services.helpers import parse_dt
        from datetime import datetime
        dt = parse_dt('2026-01-15 10:30:00')
        assert dt == datetime(2026, 1, 15, 10, 30, 0)

    def test_parse_short(self):
        from services.helpers import parse_dt
        dt = parse_dt('2026-01-15 10:30')
        assert dt is not None

    def test_parse_none(self):
        from services.helpers import parse_dt
        assert parse_dt(None) is None

    def test_parse_empty(self):
        from services.helpers import parse_dt
        assert parse_dt('') is None

    def test_parse_invalid(self):
        from services.helpers import parse_dt
        assert parse_dt('not-a-date') is None


class TestMediaConstants:
    def test_constants_exist(self):
        from services.helpers import UPLOAD_FOLDER, ALLOWED_EXTENSIONS, MAX_FILE_SIZE
        assert UPLOAD_FOLDER is not None
        assert 'png' in ALLOWED_EXTENSIONS
        assert MAX_FILE_SIZE > 0
