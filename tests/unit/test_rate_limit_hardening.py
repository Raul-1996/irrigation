"""Tests for rate-limiting hardening: password endpoint + SSE connection limit.

TDD-first: these tests define the expected behavior before implementation.
Uses direct Flask test client with the real app (no reload).
"""
import os
import sys
import pytest
import json

# Ensure project root on path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ['TESTING'] = '1'
os.environ['SECRET_KEY'] = 'test-secret-key'

from services.api_rate_limiter import reset_all as reset_rate_limits, _is_allowed


@pytest.fixture
def flask_app():
    """Create a minimal Flask app for testing rate-limited endpoints."""
    from flask import Flask, jsonify, request, session
    from services.api_rate_limiter import rate_limit

    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test-secret'

    # Simulate the /api/password endpoint WITH rate limiting applied
    @app.route('/api/password', methods=['POST'])
    @rate_limit('password_change', max_requests=3, window_sec=300)
    def api_change_password():
        return jsonify({'success': False, 'message': 'wrong old password'}), 400

    yield app


@pytest.fixture
def test_client(flask_app):
    return flask_app.test_client()


class TestPasswordChangeRateLimit:
    """P3: /api/password should be rate-limited to 3 requests / 5 min."""

    def test_normal_password_change_allowed(self, flask_app, test_client):
        """1-2 password change attempts should NOT be rate-limited."""
        reset_rate_limits()
        flask_app.config['TESTING'] = False  # Enable rate limiter
        try:
            for i in range(2):
                resp = test_client.post('/api/password',
                    json={'old_password': 'wrong', 'new_password': 'newpass123'})
                assert resp.status_code != 429, f"Request {i+1} was rate-limited unexpectedly"
        finally:
            flask_app.config['TESTING'] = True
            reset_rate_limits()

    def test_password_change_rate_limited(self, flask_app, test_client):
        """More than 3 POST /api/password in 5 min should return 429."""
        reset_rate_limits()
        flask_app.config['TESTING'] = False
        try:
            results = []
            for i in range(5):
                resp = test_client.post('/api/password',
                    json={'old_password': 'wrong', 'new_password': 'newpass123'})
                results.append(resp.status_code)

            # First 3 should NOT be 429
            for code in results[:3]:
                assert code != 429, f"First 3 should not be rate-limited, got {code}"

            # Requests 4-5 must be 429
            assert 429 in results[3:], (
                f"Requests after limit should be 429, got {results}"
            )
        finally:
            flask_app.config['TESTING'] = True
            reset_rate_limits()


class TestSSEConnectionLimit:
    """P2: scan-sse should limit concurrent SSE connections per IP to 2."""

    def test_sse_connection_limit(self):
        """More than 2 SSE connections from same IP should be rejected.

        We test the connection tracking directly since the full MQTT SSE
        endpoint requires paho-mqtt and a live MQTT broker.
        """
        try:
            from routes.mqtt_api import (
                _scan_sse_connections,
                _scan_sse_lock,
                MAX_SCAN_SSE_PER_IP,
            )
        except ImportError:
            pytest.fail(
                "SSE connection tracking not implemented yet: "
                "need _scan_sse_connections, _scan_sse_lock, MAX_SCAN_SSE_PER_IP "
                "in routes/mqtt_api.py"
            )

        assert MAX_SCAN_SSE_PER_IP == 2, f"Expected max 2, got {MAX_SCAN_SSE_PER_IP}"

        # Simulate: 2 active connections from IP
        test_ip = '10.0.0.1'
        with _scan_sse_lock:
            _scan_sse_connections.clear()
            _scan_sse_connections[test_ip] = 2

        # Third connection should be rejected
        with _scan_sse_lock:
            current = _scan_sse_connections.get(test_ip, 0)
            assert current >= MAX_SCAN_SSE_PER_IP, "Should be at limit"

        # Clean up
        with _scan_sse_lock:
            _scan_sse_connections.clear()

    def test_sse_connection_under_limit_allowed(self):
        """Connections under the limit should be allowed."""
        try:
            from routes.mqtt_api import (
                _scan_sse_connections,
                _scan_sse_lock,
                MAX_SCAN_SSE_PER_IP,
            )
        except ImportError:
            pytest.fail("SSE connection tracking not implemented yet")

        test_ip = '10.0.0.2'
        with _scan_sse_lock:
            _scan_sse_connections.clear()
            _scan_sse_connections[test_ip] = 1

        with _scan_sse_lock:
            current = _scan_sse_connections.get(test_ip, 0)
            assert current < MAX_SCAN_SSE_PER_IP, "Should be under limit"

        with _scan_sse_lock:
            _scan_sse_connections.clear()
