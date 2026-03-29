"""Tests for auth, roles, CSRF, session management."""
import pytest
import os
from unittest.mock import patch, MagicMock
from flask import Flask

os.environ['TESTING'] = '1'


class TestAuthService:
    def test_verify_password_correct(self, test_db):
        """Correct password should return (True, 'admin')."""
        # Default password is '1234'
        with patch('services.auth_service.db', test_db):
            from services.auth_service import verify_password
            success, role = verify_password('1234')
            assert success is True
            assert role == 'admin'

    def test_verify_password_wrong(self, test_db):
        """Wrong password should return (False, 'guest')."""
        with patch('services.auth_service.db', test_db):
            from services.auth_service import verify_password
            success, role = verify_password('wrong-password')
            assert success is False
            assert role == 'guest'

    def test_verify_password_empty(self, test_db):
        """Empty password should fail."""
        with patch('services.auth_service.db', test_db):
            from services.auth_service import verify_password
            success, role = verify_password('')
            assert success is False
            assert role == 'guest'


class TestSecurityDecorators:
    def test_admin_required_in_testing(self):
        """admin_required should allow access in TESTING mode."""
        from services.security import admin_required

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test'

        @app.route('/test')
        @admin_required
        def test_view():
            return 'ok'

        with app.test_client() as client:
            resp = client.get('/test')
            assert resp.status_code == 200

    def test_role_required_in_testing(self):
        """role_required should allow access in TESTING mode."""
        from services.security import role_required

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test'

        @app.route('/test')
        @role_required('admin')
        def test_view():
            return 'ok'

        with app.test_client() as client:
            resp = client.get('/test')
            assert resp.status_code == 200
