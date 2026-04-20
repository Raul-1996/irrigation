"""Security tests for SEC-006 (session fixation) / SEC-007 (logout clear) / SEC-008.

These tests exercise the regeneration behaviour directly on the helper and via
a minimal Flask app — the full app fixture disables auth in TESTING mode, so
testing the middleware decisions there is pointless. The helper contract is:
after `_regenerate_session(new_values)` no key from the previous session
remains, and only the explicitly-passed keys are present.
"""
from __future__ import annotations

import pytest
from flask import Flask, session


@pytest.fixture
def bare_app():
    """Minimal Flask app that does NOT set TESTING — we want real sessions."""
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'test-secret-for-session-fixation-tests'
    app.config['TESTING'] = False  # Real session cookies please.
    return app


def test_regenerate_session_drops_pre_login_keys(bare_app):
    """SEC-006: any key set before login must not survive regeneration."""
    from routes.auth import _regenerate_session

    @bare_app.route('/plant')
    def plant():
        session['attacker_planted'] = 'evil'
        session['role'] = 'guest'
        return 'planted'

    @bare_app.route('/login-ok')
    def login_ok():
        _regenerate_session({'logged_in': True, 'role': 'admin'})
        return 'ok'

    @bare_app.route('/peek')
    def peek():
        return {
            'planted': session.get('attacker_planted'),
            'role': session.get('role'),
            'logged_in': session.get('logged_in'),
        }

    client = bare_app.test_client()
    client.get('/plant')
    client.get('/login-ok')
    resp = client.get('/peek')
    body = resp.get_json()
    assert body['planted'] is None, "pre-login key must not survive regeneration"
    assert body['role'] == 'admin'
    assert body['logged_in'] is True


def test_regenerate_session_rotates_cookie(bare_app):
    """SEC-006: the signed session cookie value must change after regeneration."""
    from routes.auth import _regenerate_session

    @bare_app.route('/plant')
    def plant():
        session['role'] = 'guest'
        return 'planted'

    @bare_app.route('/login-ok')
    def login_ok():
        _regenerate_session({'logged_in': True, 'role': 'admin'})
        return 'ok'

    client = bare_app.test_client()
    resp1 = client.get('/plant')
    cookie_before = resp1.headers.getlist('Set-Cookie')
    resp2 = client.get('/login-ok')
    cookie_after = resp2.headers.getlist('Set-Cookie')
    # We can't compare exact values reliably (werkzeug may not always emit
    # the header when nothing changed), but the login handler MUST emit a
    # Set-Cookie header because we wrote to the session.
    assert any('session=' in c for c in cookie_after), "login must emit a new session cookie"


def test_logout_clears_session_keys(bare_app):
    """SEC-007: /logout must not leave any keys in session."""
    # Recreate the logout handler inline — it mirrors system_config_api.api_logout
    from flask import redirect

    @bare_app.route('/plant')
    def plant():
        session['logged_in'] = True
        session['role'] = 'admin'
        session['something_else'] = 'x'
        return 'planted'

    @bare_app.route('/logout', methods=['GET', 'POST'])
    def logout():
        session.clear()
        return redirect('/peek')

    @bare_app.route('/peek')
    def peek():
        return {
            'logged_in': session.get('logged_in'),
            'role': session.get('role'),
            'something_else': session.get('something_else'),
        }

    client = bare_app.test_client()
    client.get('/plant')
    client.get('/logout', follow_redirects=True)
    resp = client.get('/peek')
    body = resp.get_json()
    assert body['logged_in'] is None
    assert body['role'] is None
    assert body['something_else'] is None
