"""Legacy auth API smoke tests — kept minimal post-#52.

The old single-password flow (POST /api/login {password}, /api/password) is
gone. The detailed B1-B14 coverage lives in tests/unit/test_auth_b_pillars.py
and tests/api/test_auth_in_app.py. This file just ensures the read-only
status endpoint still answers and the legacy /api/password is a 410.
"""

import json
import os

os.environ["TESTING"] = "1"


class TestAuthStatusAPI:
    def test_auth_status_admin(self, admin_client):
        resp = admin_client.get("/api/auth/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["authenticated"] is True


class TestLegacyPasswordEndpointGone:
    """B10: /api/password must answer 410 Gone for every method."""

    def test_post_gone(self, admin_client):
        resp = admin_client.post(
            "/api/password",
            data=json.dumps({"old_password": "1234", "new_password": "anythinglong"}),
            content_type="application/json",
        )
        assert resp.status_code == 410

    def test_get_gone(self, admin_client):
        resp = admin_client.get("/api/password")
        assert resp.status_code == 410
