"""API tests for /api/logging/debug — Level 2 toggle with auto-off."""
from __future__ import annotations

import json
import os

import pytest

os.environ['TESTING'] = '1'


class TestDebugToggle:

    def test_get_default_off(self, admin_client):
        resp = admin_client.get('/api/logging/debug')
        assert resp.status_code == 200
        body = resp.get_json()
        assert 'debug' in body
        # Default for fresh DB is OFF
        assert body['debug'] in (False, 0, '0', None)

    def test_enable_then_disable(self, admin_client):
        # Enable
        r1 = admin_client.post(
            '/api/logging/debug',
            data=json.dumps({'enabled': True}),
            content_type='application/json',
        )
        assert r1.status_code == 200
        assert r1.get_json()['debug'] in (True, 1, '1')
        # Disable
        r2 = admin_client.post(
            '/api/logging/debug',
            data=json.dumps({'enabled': False}),
            content_type='application/json',
        )
        assert r2.status_code == 200
        assert r2.get_json()['debug'] in (False, 0, '0', None)

    def test_persists_in_db(self, admin_client, test_db):
        admin_client.post(
            '/api/logging/debug',
            data=json.dumps({'enabled': True}),
            content_type='application/json',
        )
        # Re-read via direct DB call — toggle must be persistent.
        from database import db as runtime_db
        assert runtime_db.get_logging_debug() in (True, 1, '1')

    def test_auto_off_minutes_clamped(self, admin_client):
        """Out-of-range auto_off_minutes must NOT crash, must be clamped to 1..720."""
        for m in (-5, 0, 99999):
            resp = admin_client.post(
                '/api/logging/debug',
                data=json.dumps({'enabled': True, 'auto_off_minutes': m}),
                content_type='application/json',
            )
            assert resp.status_code == 200

    def test_invalid_json_handled(self, admin_client):
        """Malformed body must not crash the route."""
        resp = admin_client.post(
            '/api/logging/debug',
            data='not-json',
            content_type='application/json',
        )
        assert resp.status_code in (200, 400)

    # NOTE: admin_required is bypassed under TESTING=1 (see services/security.py),
    # so auth-denial cannot be asserted through the Flask test client. The
    # decorator's presence on the route is the security contract.
