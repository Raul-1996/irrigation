"""API-level tests for /api/audit/* endpoints.

Covers:
  * POST /api/audit/ui   — public, CSRF-exempt, records click events.
  * GET  /api/audit       — admin-only, paginated, filterable.
  * GET  /api/audit/types — admin-only, distinct action_types.
  * Auth: viewer/guest must NOT see admin-only routes.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ['TESTING'] = '1'


class TestAuditUIEvent:
    """POST /api/audit/ui — semi-public, used by static/js/audit.js.

    OQ3 contract: anonymous callers may ONLY emit ``login_attempt``.
    Every other action_type returns 403 to prevent audit-spam DoS from
    an unauth scraper.  Authenticated callers (any logged-in role) may
    emit any action_type, subject to server-side redaction.
    """

    def test_anonymous_login_attempt_allowed(self, client):
        """Anonymous client CAN emit login_attempt — used by login page."""
        resp = client.post(
            '/api/audit/ui',
            data=json.dumps({
                'action': 'login_attempt',
                'target': 'auth:login',
            }),
            content_type='application/json',
        )
        assert resp.status_code in (200, 204)

    def test_anonymous_other_action_rejected(self, client):
        """Anonymous client MUST be rejected for non-login_attempt actions."""
        resp = client.post(
            '/api/audit/ui',
            data=json.dumps({
                'action': 'zone_start_click',
                'target': 'zone:5',
            }),
            content_type='application/json',
        )
        assert resp.status_code == 403

    def test_records_click(self, admin_client):
        """Authenticated client can emit any action."""
        resp = admin_client.post(
            '/api/audit/ui',
            data=json.dumps({
                'action': 'zone_start_click',
                'target': 'zone:5',
                'context': {'reason': 'manual'}
            }),
            content_type='application/json',
        )
        # Endpoint returns 204 on success
        assert resp.status_code in (200, 204)

    def test_handles_empty_body(self, admin_client):
        """An empty JSON body must NOT crash; defaults to 'ui_event_unknown'."""
        resp = admin_client.post('/api/audit/ui',
                                 data='{}', content_type='application/json')
        assert resp.status_code in (200, 204)

    def test_strips_password_from_context(self, client, admin_client):
        """Sensitive keys in `context` must be redacted before storage.

        Uses ``login_attempt`` (anonymous-allowed) so the test exercises
        BOTH the OQ3 gate and the redaction path.
        """
        resp = client.post(
            '/api/audit/ui',
            data=json.dumps({
                'action': 'login_attempt',
                'context': {'username': 'u', 'password': 'p4ssw0rd'}
            }),
            content_type='application/json',
        )
        assert resp.status_code in (200, 204)
        # Read back via admin GET — password must not appear in raw payload
        list_resp = admin_client.get('/api/audit?limit=20')
        assert list_resp.status_code == 200
        body = list_resp.get_json()
        rows = body.get('rows', [])
        login_rows = [r for r in rows if r.get('action_type') == 'login_attempt']
        assert login_rows, "login_attempt row should exist"
        payload = login_rows[0].get('payload_json') or ''
        assert 'p4ssw0rd' not in payload
        assert '"password": "***"' in payload

    def test_action_capped_to_64_chars(self, admin_client):
        long_action = 'x' * 200
        admin_client.post(
            '/api/audit/ui',
            data=json.dumps({'action': long_action}),
            content_type='application/json',
        )
        list_resp = admin_client.get('/api/audit?limit=5')
        rows = list_resp.get_json().get('rows', [])
        for r in rows:
            assert len(r.get('action_type') or '') <= 65  # 64 + '…'


class TestAuditList:
    """GET /api/audit — admin-only paginated list.

    Note: under TESTING=1 the admin_required decorator is bypassed (see
    services/security.py), so we can't assert auth denial through the test
    client. Decorator presence on the route is the security contract.
    """

    def test_admin_returns_rows(self, admin_client):
        # Seed one row via UI endpoint (admin_client is authenticated).
        admin_client.post(
            '/api/audit/ui',
            data=json.dumps({'action': 'seed_row', 'target': 'unit:1'}),
            content_type='application/json',
        )
        resp = admin_client.get('/api/audit?limit=10')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['success'] is True
        assert isinstance(body['rows'], list)
        assert isinstance(body['total'], int)
        assert body['limit'] == 10

    def test_filter_by_action_type(self, admin_client):
        for action in ('alpha_click', 'beta_click', 'alpha_click'):
            admin_client.post(
                '/api/audit/ui',
                data=json.dumps({'action': action}),
                content_type='application/json',
            )
        resp = admin_client.get('/api/audit?action_type=alpha_click&limit=20')
        assert resp.status_code == 200
        rows = resp.get_json().get('rows', [])
        assert all(r['action_type'] == 'alpha_click' for r in rows)
        assert len(rows) >= 2

    def test_pagination_clamps(self, admin_client):
        """limit > 500 must be clamped to 500."""
        resp = admin_client.get('/api/audit?limit=99999')
        assert resp.status_code == 200
        assert resp.get_json()['limit'] == 500

    def test_substring_filter_q(self, admin_client):
        admin_client.post(
            '/api/audit/ui',
            data=json.dumps({'action': 'unique_substring_xyz'}),
            content_type='application/json',
        )
        resp = admin_client.get('/api/audit?q=substring_xyz&limit=20')
        assert resp.status_code == 200
        rows = resp.get_json().get('rows', [])
        assert any('substring_xyz' in r.get('action_type', '') for r in rows)


class TestAuditTypes:
    """GET /api/audit/types — admin-only distinct action_types."""

    def test_returns_list(self, admin_client):
        admin_client.post(
            '/api/audit/ui',
            data=json.dumps({'action': 'type_test_action'}),
            content_type='application/json',
        )
        resp = admin_client.get('/api/audit/types')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['success'] is True
        assert isinstance(body['types'], list)
        assert 'type_test_action' in body['types']
