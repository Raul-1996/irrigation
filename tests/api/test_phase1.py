"""Tests for Phase 1 fixes: rate limiter, CSP header, QoS 1."""
import os
import queue
import pytest
from unittest.mock import patch, MagicMock


# ── CSP Header Tests ───────────────────────────────────────────────────────

class TestCSPHeader:
    """Content-Security-Policy header is present on responses."""

    def test_csp_header_on_api_response(self, admin_client):
        """CSP header should be set on API responses."""
        resp = admin_client.get('/api/zones')
        assert resp.status_code == 200
        csp = resp.headers.get('Content-Security-Policy')
        assert csp is not None, "Content-Security-Policy header missing"
        assert "default-src 'self'" in csp
        assert "script-src 'self' 'unsafe-inline'" in csp
        assert "style-src 'self' 'unsafe-inline'" in csp
        assert "img-src 'self' data:" in csp
        assert "connect-src 'self'" in csp

    def test_csp_header_on_page_response(self, client):
        """CSP header should be set on HTML page responses too."""
        resp = client.get('/')
        csp = resp.headers.get('Content-Security-Policy')
        assert csp is not None, "Content-Security-Policy header missing on page"

    def test_x_content_type_options_still_present(self, client):
        """Other security headers should still be present."""
        resp = client.get('/api/zones')
        assert resp.headers.get('X-Content-Type-Options') == 'nosniff'
        assert resp.headers.get('X-Frame-Options') == 'SAMEORIGIN'


# ── Rate Limiter Tests ─────────────────────────────────────────────────────

class TestAPIRateLimiter:
    """Test the api_rate_limiter module directly."""

    def test_allows_requests_within_limit(self):
        """Requests within the limit should be allowed."""
        from services.api_rate_limiter import _is_allowed, reset_all
        reset_all()
        for i in range(5):
            allowed, retry = _is_allowed('10.0.0.1', 'test_group', 5, 60)
            assert allowed, f"Request {i+1} should be allowed"
            assert retry == 0

    def test_blocks_over_limit(self):
        """Requests over the limit should be blocked."""
        from services.api_rate_limiter import _is_allowed, reset_all
        reset_all()
        # Use up the limit
        for _ in range(10):
            _is_allowed('10.0.0.2', 'test_block', 10, 60)
        # Next one should be blocked
        allowed, retry = _is_allowed('10.0.0.2', 'test_block', 10, 60)
        assert not allowed
        assert retry > 0

    def test_different_ips_independent(self):
        """Different IPs should have independent limits."""
        from services.api_rate_limiter import _is_allowed, reset_all
        reset_all()
        # Max out IP A
        for _ in range(3):
            _is_allowed('10.0.0.3', 'test_ip', 3, 60)
        # IP A blocked
        allowed_a, _ = _is_allowed('10.0.0.3', 'test_ip', 3, 60)
        assert not allowed_a
        # IP B should still be allowed
        allowed_b, _ = _is_allowed('10.0.0.4', 'test_ip', 3, 60)
        assert allowed_b

    def test_different_groups_independent(self):
        """Different groups should have independent limits."""
        from services.api_rate_limiter import _is_allowed, reset_all
        reset_all()
        for _ in range(3):
            _is_allowed('10.0.0.5', 'group_a', 3, 60)
        allowed_a, _ = _is_allowed('10.0.0.5', 'group_a', 3, 60)
        assert not allowed_a
        allowed_b, _ = _is_allowed('10.0.0.5', 'group_b', 3, 60)
        assert allowed_b

    def test_reset_clears_state(self):
        """reset_all should clear all rate limit state."""
        from services.api_rate_limiter import _is_allowed, reset_all
        for _ in range(5):
            _is_allowed('10.0.0.6', 'test_reset', 5, 60)
        allowed, _ = _is_allowed('10.0.0.6', 'test_reset', 5, 60)
        assert not allowed
        reset_all()
        allowed, _ = _is_allowed('10.0.0.6', 'test_reset', 5, 60)
        assert allowed

    def test_decorator_skips_in_testing(self, admin_client):
        """Rate limit decorator should skip when TESTING=True."""
        # In test mode, rate limiting is disabled — we should be able
        # to make many requests without 429
        for _ in range(50):
            resp = admin_client.get('/api/zones')
            assert resp.status_code == 200


# ── SSE QoS Tests ──────────────────────────────────────────────────────────

class TestSSEQoS:
    """Test that SSE hub subscribes with QoS 1."""

    def test_subscribe_called_with_qos_1(self):
        """ensure_hub_started should subscribe to topics with qos=1."""
        from services import sse_hub
        import importlib
        # Reset hub state
        sse_hub._SSE_HUB_STARTED = False
        sse_hub._SSE_HUB_MQTT = {}

        mock_client = MagicMock()
        mock_client.connect = MagicMock()
        mock_client.subscribe = MagicMock()
        mock_client.loop_start = MagicMock()

        mock_mqtt_mod = MagicMock()
        mock_mqtt_mod.Client.return_value = mock_client
        mock_mqtt_mod.CallbackAPIVersion.VERSION2 = 2

        mock_db = MagicMock()
        mock_db.get_zones.return_value = [
            {'id': 1, 'mqtt_server_id': 1, 'topic': '/test/zone1'},
        ]
        mock_db.get_groups.return_value = []
        mock_db.get_mqtt_server.return_value = {
            'id': 1, 'host': '127.0.0.1', 'port': 1883,
            'username': '', 'password': '', 'client_id': 'test',
        }

        # Init with mocks
        sse_hub.init(
            db=mock_db,
            mqtt_module=mock_mqtt_mod,
            app_config={'TESTING': False},
            publish_mqtt_value=MagicMock(),
            normalize_topic=lambda t: t if t.startswith('/') else '/' + t,
            get_scheduler=MagicMock(),
        )

        sse_hub.ensure_hub_started()

        # Verify subscribe was called with qos=1
        subscribe_calls = mock_client.subscribe.call_args_list
        assert len(subscribe_calls) > 0, "subscribe() was never called"
        for call in subscribe_calls:
            args, kwargs = call
            # subscribe(topic, qos=1) — qos can be positional or keyword
            if len(args) >= 2:
                assert args[1] == 1, f"Expected qos=1, got qos={args[1]}"
            elif 'qos' in kwargs:
                assert kwargs['qos'] == 1, f"Expected qos=1, got qos={kwargs['qos']}"

        # Clean up
        sse_hub._SSE_HUB_STARTED = False
        sse_hub._SSE_HUB_MQTT = {}
