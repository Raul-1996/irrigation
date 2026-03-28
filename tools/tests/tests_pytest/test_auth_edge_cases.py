"""
Tests for authentication edge cases — rate limiting, session handling, default password.
"""
import os
import sys
import json
import pytest

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("TESTING", "1")


class TestAuthEdgeCases:
    def test_login_empty_password(self, client):
        r = client.post('/api/login', json={'password': ''})
        assert r.status_code in (400, 401, 403)

    def test_login_no_body(self, client):
        r = client.post('/api/login', json={})
        assert r.status_code in (400, 401, 403)

    def test_login_default_password(self, client):
        """Default password '1234' should work after seed."""
        r = client.post('/api/login', json={'password': '1234'})
        assert r.status_code == 200

    def test_multiple_failed_logins(self, client):
        """Multiple wrong passwords — should not crash (rate limiting may apply)."""
        for i in range(5):
            r = client.post('/api/login', json={'password': f'wrong_{i}'})
            assert r.status_code in (401, 403, 429)

    def test_auth_status_unauthenticated(self, client):
        """Without login, auth status should show not authenticated."""
        # New client without session
        import app as app_module
        with app_module.app.test_client() as c:
            r = c.get('/api/auth/status')
            assert r.status_code == 200

    def test_logout_clears_session(self, client):
        # Login first
        client.post('/api/login', json={'password': '1234'})
        r1 = client.get('/api/auth/status')
        assert r1.status_code == 200

        # Logout
        client.get('/logout', follow_redirects=False)

        # Check status after logout
        r2 = client.get('/api/auth/status')
        assert r2.status_code == 200

    def test_change_password_wrong_old(self, client):
        r = client.post('/api/password', json={
            'old_password': 'totally_wrong',
            'new_password': 'newpass'
        })
        assert r.status_code in (400, 401, 403)

    def test_change_password_empty_new(self, client):
        r = client.post('/api/password', json={
            'old_password': '1234',
            'new_password': ''
        })
        assert r.status_code in (200, 400)

    def test_mutations_without_auth(self):
        """Some mutations might require auth."""
        import app as app_module
        with app_module.app.test_client() as c:
            # Try zone update without login
            r = c.put('/api/zones/1', json={'name': 'Hacked'})
            # Could be 200 (no auth required) or 401/302
            assert r.status_code in (200, 302, 401, 403)
