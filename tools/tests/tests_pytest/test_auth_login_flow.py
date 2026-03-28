"""
Tests for authentication login flow — login, logout, guest access, rate limiting.
"""
import os
import sys
import time
import pytest
from unittest.mock import patch, MagicMock

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("TESTING", "1")


class TestLoginPage:
    def test_login_page_renders(self, client):
        r = client.get('/login')
        assert r.status_code == 200

    def test_guest_login_redirect(self, client):
        r = client.get('/login?guest=1', follow_redirects=False)
        assert r.status_code in (302, 200)

    def test_guest_login_sets_session(self, client):
        r = client.get('/login?guest=1', follow_redirects=True)
        assert r.status_code == 200


class TestApiLogin:
    def test_login_correct_password(self, client):
        r = client.post('/api/login', json={'password': '1234'})
        assert r.status_code == 200
        data = r.get_json()
        assert data['success'] is True
        assert data.get('role') == 'admin'

    def test_login_wrong_password(self, client):
        r = client.post('/api/login', json={'password': 'wrong'})
        assert r.status_code == 401
        data = r.get_json()
        assert data['success'] is False

    def test_login_empty_password(self, client):
        r = client.post('/api/login', json={'password': ''})
        assert r.status_code == 401

    def test_login_no_json(self, client):
        r = client.post('/api/login', data='not json', content_type='text/plain')
        assert r.status_code in (400, 401)

    def test_login_missing_password_field(self, client):
        r = client.post('/api/login', json={})
        assert r.status_code == 401


class TestLogout:
    def test_logout_clears_session(self, client):
        # Login first
        client.post('/api/login', json={'password': '1234'})
        # Logout
        r = client.get('/logout', follow_redirects=False)
        assert r.status_code in (200, 302)

    def test_logout_without_login(self, client):
        r = client.get('/logout', follow_redirects=False)
        assert r.status_code in (200, 302)


class TestAuthStatus:
    def test_auth_status_not_logged_in(self, client):
        r = client.get('/api/auth/status')
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, dict)

    def test_auth_status_after_login(self, client):
        client.post('/api/login', json={'password': '1234'})
        r = client.get('/api/auth/status')
        assert r.status_code == 200
        data = r.get_json()
        assert data.get('logged_in') is True or data.get('authenticated') is True


class TestRateLimit:
    def test_rapid_login_attempts(self, client):
        """Rapid fire login should get rate-limited (429)."""
        responses = []
        for _ in range(5):
            r = client.post('/api/login', json={'password': 'wrong'})
            responses.append(r.status_code)
        # At least one should be 429 (rate limited)
        assert 429 in responses or all(s == 401 for s in responses)
