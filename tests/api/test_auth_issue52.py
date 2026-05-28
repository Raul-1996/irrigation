"""Integration tests for Issue #52: in-app login replaces basic auth.

Covered scenarios:
    - users-table seed (admin + Poliv exist after migration)
    - username+password login (success / wrong username / wrong password)
    - back-compat: password-only login still resolves to 'admin'
    - escalate flow (viewer → admin via /api/login/escalate)
    - escalate rejects non-admin credentials
    - rate-limit Tier 1 (5 IP fails → 429)
    - per-username throttle persists across IPs
    - successful login resets the rate-limit bucket
    - admin user CRUD (create / change role / deactivate / change password)
    - last-admin protection (cannot demote / deactivate the only admin)
    - /api/account/password (viewer changes own password)
    - long-lived session: session.permanent=True after login
"""

import json
import os

os.environ["TESTING"] = "1"


def _login(client, username, password):
    return client.post(
        "/api/login",
        data=json.dumps({"username": username, "password": password}),
        content_type="application/json",
    )


def _escalate(client, username, password):
    return client.post(
        "/api/login/escalate",
        data=json.dumps({"username": username, "password": password}),
        content_type="application/json",
    )


# ── seed ───────────────────────────────────────────────────────────────────
class TestUsersSeed:
    def test_admin_user_seeded(self, app):
        from services import users_service

        u = users_service.get_by_username("admin")
        assert u is not None
        assert u.role == "admin"
        assert u.is_active is True

    def test_poliv_viewer_seeded(self, app):
        from services import users_service

        u = users_service.get_by_username("Poliv")
        assert u is not None
        assert u.role == "viewer"
        assert u.is_active is True


# ── username + password login ──────────────────────────────────────────────
class TestUsernamePasswordLogin:
    def test_admin_login_success(self, client):
        r = _login(client, "admin", "1234")
        assert r.status_code == 200
        j = r.get_json()
        assert j["success"] is True
        assert j["role"] == "admin"
        assert j["username"] == "admin"

    def test_viewer_login_success(self, client):
        r = _login(client, "Poliv", "Poliv")
        assert r.status_code == 200
        j = r.get_json()
        assert j["success"] is True
        assert j["role"] == "viewer"
        assert j["username"] == "Poliv"

    def test_login_wrong_password(self, client):
        r = _login(client, "admin", "definitely-wrong")
        assert r.status_code == 401
        assert r.get_json()["success"] is False

    def test_login_unknown_username(self, client):
        r = _login(client, "nobody", "whatever")
        assert r.status_code == 401
        assert r.get_json()["success"] is False

    def test_password_only_back_compat(self, client):
        """Legacy clients that POST only {password} resolve to 'admin'."""
        r = client.post(
            "/api/login",
            data=json.dumps({"password": "1234"}),
            content_type="application/json",
        )
        assert r.status_code == 200
        assert r.get_json()["role"] == "admin"


# ── escalate ───────────────────────────────────────────────────────────────
class TestEscalate:
    def test_viewer_can_escalate_with_admin_creds(self, client):
        # 1. Login as viewer
        r = _login(client, "Poliv", "Poliv")
        assert r.status_code == 200
        # 2. Escalate with admin creds
        r = _escalate(client, "admin", "1234")
        assert r.status_code == 200
        j = r.get_json()
        assert j["success"] is True
        assert j["role"] == "admin"
        # 3. Session role is now admin
        with client.session_transaction() as sess:
            assert sess["role"] == "admin"
            assert sess["username"] == "admin"

    def test_escalate_rejects_viewer_creds(self, client):
        _login(client, "Poliv", "Poliv")
        r = _escalate(client, "Poliv", "Poliv")
        assert r.status_code == 401
        assert r.get_json()["success"] is False

    def test_escalate_rejects_wrong_password(self, client):
        _login(client, "Poliv", "Poliv")
        r = _escalate(client, "admin", "nope")
        assert r.status_code == 401


# ── rate limit ─────────────────────────────────────────────────────────────
class TestRateLimit:
    def test_tier1_locks_after_5_ip_failures(self, client):
        from constants import LOGIN_TIER1_MAX

        # LOGIN_TIER1_MAX failed attempts → next one returns 429.
        for _ in range(LOGIN_TIER1_MAX):
            r = _login(client, "admin", "wrong")
            assert r.status_code == 401
        r = _login(client, "admin", "wrong")
        assert r.status_code == 429

    def test_successful_login_resets_bucket(self, client):
        from constants import LOGIN_TIER1_MAX

        # 1. Fail almost up to the limit.
        for _ in range(LOGIN_TIER1_MAX - 1):
            _login(client, "admin", "wrong")
        # 2. Successful login → bucket should be cleared.
        r = _login(client, "admin", "1234")
        assert r.status_code == 200
        # 3. We can now fail (LOGIN_TIER1_MAX - 1) more times without 429.
        #    If the bucket hadn't been reset, the very next failure would 429.
        for _ in range(LOGIN_TIER1_MAX - 1):
            r = _login(client, "admin", "wrong")
            assert r.status_code == 401

    def test_per_username_throttle(self, client):
        from constants import LOGIN_USERNAME_MAX
        from services.rate_limiter import login_limiter

        # Drive enough username-failures to trigger the per-username lockout
        # without hitting Tier-1 by interleaving usernames? No: per-username
        # throttle is independent — feed it directly via the limiter.
        for _ in range(LOGIN_USERNAME_MAX):
            login_limiter.record_failure("9.9.9.9", username="Poliv")
        allowed, retry_after = login_limiter.check("8.8.8.8", username="Poliv")
        assert allowed is False
        assert retry_after > 0


# ── admin user CRUD ────────────────────────────────────────────────────────
class TestAdminUserCRUD:
    def test_list_users(self, admin_client):
        r = admin_client.get("/api/admin/users")
        assert r.status_code == 200
        j = r.get_json()
        usernames = {u["username"] for u in j["users"]}
        assert {"admin", "Poliv"} <= usernames

    def test_create_user(self, admin_client):
        r = admin_client.post(
            "/api/admin/users",
            data=json.dumps({"username": "alice", "password": "alice-pw-123", "role": "viewer"}),
            content_type="application/json",
        )
        assert r.status_code == 200
        j = r.get_json()
        assert j["success"] is True
        assert j["user"]["username"] == "alice"
        assert j["user"]["role"] == "viewer"
        # password_hash must never leak.
        assert "password_hash" not in j["user"]

    def test_create_user_rejects_short_password(self, admin_client):
        r = admin_client.post(
            "/api/admin/users",
            data=json.dumps({"username": "bob", "password": "x", "role": "viewer"}),
            content_type="application/json",
        )
        assert r.status_code == 400

    def test_create_user_rejects_invalid_role(self, admin_client):
        r = admin_client.post(
            "/api/admin/users",
            data=json.dumps({"username": "carol", "password": "carol-pw-12", "role": "root"}),
            content_type="application/json",
        )
        assert r.status_code == 400

    def test_change_role(self, admin_client):
        from services import users_service

        # Create a viewer, promote them.
        users_service.create_user("dave", "dave-pw-12", "viewer")
        u = users_service.get_by_username("dave")
        r = admin_client.post(
            f"/api/admin/users/{u.id}/role",
            data=json.dumps({"role": "admin"}),
            content_type="application/json",
        )
        assert r.status_code == 200
        assert users_service.get_by_id(u.id).role == "admin"

    def test_deactivate_user(self, admin_client):
        from services import users_service

        users_service.create_user("eve", "eve-pw-12", "viewer")
        u = users_service.get_by_username("eve")
        r = admin_client.post(f"/api/admin/users/{u.id}/deactivate")
        assert r.status_code == 200
        assert users_service.get_by_id(u.id).is_active is False
        # Activate again
        r = admin_client.post(f"/api/admin/users/{u.id}/activate")
        assert r.status_code == 200
        assert users_service.get_by_id(u.id).is_active is True

    def test_admin_can_change_user_password(self, admin_client):
        from services import users_service

        users_service.create_user("frank", "frank-pw-12", "viewer")
        u = users_service.get_by_username("frank")
        r = admin_client.post(
            f"/api/admin/users/{u.id}/password",
            data=json.dumps({"new_password": "frank-new-pw-12"}),
            content_type="application/json",
        )
        assert r.status_code == 200
        # New password works for login.
        client = admin_client  # same app
        r = _login(client, "frank", "frank-new-pw-12")
        assert r.status_code == 200


# ── last-admin protection ──────────────────────────────────────────────────
class TestLastAdminProtection:
    def test_cannot_demote_last_admin(self, admin_client):
        from services import users_service

        admin = users_service.get_by_username("admin")
        r = admin_client.post(
            f"/api/admin/users/{admin.id}/role",
            data=json.dumps({"role": "viewer"}),
            content_type="application/json",
        )
        assert r.status_code == 400
        # Still admin.
        assert users_service.get_by_id(admin.id).role == "admin"

    def test_cannot_deactivate_last_admin(self, admin_client):
        from services import users_service

        admin = users_service.get_by_username("admin")
        r = admin_client.post(f"/api/admin/users/{admin.id}/deactivate")
        assert r.status_code == 400
        assert users_service.get_by_id(admin.id).is_active is True

    def test_can_demote_when_two_admins_present(self, admin_client):
        from services import users_service

        users_service.create_user("admin2", "admin2-pw-12", "admin")
        admin = users_service.get_by_username("admin")
        r = admin_client.post(
            f"/api/admin/users/{admin.id}/role",
            data=json.dumps({"role": "viewer"}),
            content_type="application/json",
        )
        assert r.status_code == 200


# ── self-service /api/account/password ─────────────────────────────────────
class TestAccountPasswordChange:
    def test_viewer_changes_own_password(self, client):
        # Login as Poliv.
        r = _login(client, "Poliv", "Poliv")
        assert r.status_code == 200

        # Change own password.
        r = client.post(
            "/api/account/password",
            data=json.dumps({"old_password": "Poliv", "new_password": "Poliv-new-pw-12"}),
            content_type="application/json",
        )
        assert r.status_code == 200
        assert r.get_json()["success"] is True

        # Old password no longer works.
        r = _login(client, "Poliv", "Poliv")
        assert r.status_code == 401
        # New password works.
        r = _login(client, "Poliv", "Poliv-new-pw-12")
        assert r.status_code == 200

    def test_wrong_old_password_rejected(self, client):
        _login(client, "Poliv", "Poliv")
        r = client.post(
            "/api/account/password",
            data=json.dumps({"old_password": "WRONG", "new_password": "Poliv-new-pw-12"}),
            content_type="application/json",
        )
        assert r.status_code == 400
        assert r.get_json()["success"] is False


# ── long-lived session ─────────────────────────────────────────────────────
class TestLongLivedSession:
    def test_session_is_permanent_after_login(self, client):
        r = _login(client, "admin", "1234")
        assert r.status_code == 200
        with client.session_transaction() as sess:
            # Flask exposes the permanent flag as a dict key when set.
            assert sess.permanent is True
            assert sess.get("logged_in") is True
            assert sess.get("user_id") is not None
            assert sess.get("username") == "admin"
            assert sess.get("role") == "admin"

    def test_app_has_long_session_lifetime(self, app):
        from datetime import timedelta

        lt = app.config.get("PERMANENT_SESSION_LIFETIME")
        # 365 days configured in app.py per spec.
        assert lt == timedelta(days=365)
