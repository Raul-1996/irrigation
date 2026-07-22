"""Tests for the production guest-control boundary.

Since TESTING=1 disables both middleware and @admin_required decorator,
we verify:
1. _is_status_action(): only fail-safe OFF actions remain public.
2. Production-like middleware rejects anonymous ON/resume actions.
"""

from unittest.mock import patch

import pytest


class TestIsStatusAction:
    """Test the _is_status_action() whitelist in app.py."""

    @pytest.fixture(autouse=True)
    def _import_fn(self, app):
        """Import _is_status_action from app module."""
        import app as app_mod

        self._is_status_action = app_mod._is_status_action

    # -- Allowed paths (fail-safe OFF actions) --

    def test_zone_mqtt_stop(self):
        assert self._is_status_action("/api/zones/42/mqtt/stop")

    def test_zone_stop(self):
        assert self._is_status_action("/api/zones/99/stop")

    def test_group_stop(self):
        assert self._is_status_action("/api/groups/3/stop")

    def test_emergency_stop(self):
        assert self._is_status_action("/api/emergency-stop")

    def test_postpone(self):
        assert self._is_status_action("/api/postpone")

    def test_status(self):
        assert self._is_status_action("/api/status")

    # -- Denied paths (admin only) --

    def test_mqtt_servers_denied(self):
        assert not self._is_status_action("/api/mqtt/servers")

    @pytest.mark.parametrize(
        "path",
        [
            "/api/zones/1/start",
            "/api/zones/1/mqtt/start",
            "/api/groups/1/start-from-first",
            "/api/groups/1/start-zone/2",
            "/api/groups/1/run-selected",
            "/api/groups/1/skip-current",
            "/api/groups/1/master-valve/open",
            "/api/groups/1/master-valve/close",
            "/api/emergency-resume",
        ],
    )
    def test_unsafe_physical_actions_denied(self, path):
        assert not self._is_status_action(path)

    def test_mqtt_server_by_id_denied(self):
        assert not self._is_status_action("/api/mqtt/servers/1")

    def test_mqtt_server_test_denied(self):
        assert not self._is_status_action("/api/mqtt/servers/1/test")

    def test_programs_denied(self):
        assert not self._is_status_action("/api/programs")

    def test_zones_crud_denied(self):
        assert not self._is_status_action("/api/zones")

    def test_next_watering_projection_is_not_anonymous(self):
        assert not self._is_status_action("/api/zones/next-watering-bulk")

    def test_groups_crud_denied(self):
        assert not self._is_status_action("/api/groups")

    def test_settings_denied(self):
        assert not self._is_status_action("/api/settings")

    def test_backup_denied(self):
        assert not self._is_status_action("/api/backup")

    # -- Edge cases --

    def test_zone_mqtt_start_no_traversal(self):
        """Path traversal should not match."""
        assert not self._is_status_action("/api/zones/1/mqtt/start/../../../admin")

    def test_zone_non_numeric_id(self):
        """Non-numeric zone id should not match."""
        assert not self._is_status_action("/api/zones/abc/mqtt/start")


class TestDecoratorPresence:
    """Verify @admin_required is removed from zone/group control and
    kept on MQTT CRUD endpoints."""

    def test_zone_watering_no_admin_required(self):
        """zones_watering_api.py should NOT import admin_required."""
        import routes.zones_watering_api as mod

        source_file = mod.__file__
        with open(source_file) as f:
            source = f.read()
        assert "admin_required" not in source, "zones_watering_api.py still references admin_required"

    def test_groups_api_no_admin_required(self):
        """groups_api.py should NOT import admin_required."""
        import routes.groups_api as mod

        source_file = mod.__file__
        with open(source_file) as f:
            source = f.read()
        assert "admin_required" not in source, "groups_api.py still references admin_required"

    def test_system_emergency_no_admin_required(self):
        """system_emergency_api.py should NOT import admin_required."""
        import routes.system_emergency_api as mod

        source_file = mod.__file__
        with open(source_file) as f:
            source = f.read()
        assert "admin_required" not in source, "system_emergency_api.py still references admin_required"

    def test_mqtt_api_has_admin_required(self):
        """mqtt_api.py MUST still have @admin_required."""
        import routes.mqtt_api as mod

        source_file = mod.__file__
        with open(source_file) as f:
            source = f.read()
        assert "admin_required" in source, "mqtt_api.py lost admin_required — MQTT CRUD must stay protected!"


class TestGuestEndpointAccess:
    """Integration: exercise the production auth middleware with a guest."""

    def test_guest_emergency_stop(self, guest_client, app):
        """Emergency OFF remains reachable without an authenticated session."""
        app.config["TESTING"] = False
        app.db.set_setting_value("password_must_change", "0")
        try:
            with (
                patch(
                    "services.zone_control.emergency_stop_all",
                    return_value={
                        "success": True,
                        "zones_failed": [],
                        "errors": [],
                        "masters_failed_publish": 0,
                        "zones_still_active_after_wait": 0,
                    },
                ),
                patch("routes.system_emergency_api.get_scheduler", return_value=None),
            ):
                resp = guest_client.post("/api/emergency-stop", content_type="application/json")
            assert resp.status_code == 503
            assert resp.get_json()["success"] is False
            assert resp.get_json()["physical_stop_confirmed"] is True
            assert resp.get_json()["sessions_quiesced"] is False
            assert app.config["EMERGENCY_STOP"] is True
        finally:
            app.config["EMERGENCY_STOP"] = False
            app.config["TESTING"] = True

    @pytest.mark.parametrize(
        "path",
        [
            "/api/zones/1/start",
            "/api/zones/1/mqtt/start",
            "/api/groups/1/start-from-first",
            "/api/groups/1/start-zone/2",
            "/api/groups/1/master-valve/open",
            "/api/emergency-resume",
        ],
    )
    def test_guest_cannot_issue_on_or_resume_actions(self, guest_client, app, path):
        app.config["TESTING"] = False
        app.db.set_setting_value("password_must_change", "0")
        try:
            resp = guest_client.post(path, content_type="application/json")
            assert resp.status_code == 401
            assert resp.get_json()["error_code"] == "UNAUTHENTICATED"
        finally:
            app.config["TESTING"] = True

    @pytest.mark.parametrize(
        "path",
        [
            "/api/zones/1/start",
            "/api/zones/1/mqtt/start",
            "/api/groups/1/start-from-first",
            "/api/groups/1/start-zone/2",
            "/api/groups/1/run-selected",
            "/api/groups/1/skip-current",
            "/api/groups/1/master-valve/open",
            "/api/groups/1/master-valve/close",
            "/api/emergency-resume",
        ],
    )
    def test_stale_admin_role_without_logged_in_cannot_control(self, app, path):
        stale_client = app.test_client()
        with stale_client.session_transaction() as sess:
            sess["logged_in"] = False
            sess["role"] = "admin"
        app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
        app.db.set_setting_value("password_must_change", "0")
        try:
            response = stale_client.post(path, content_type="application/json")
            assert response.status_code == 401
            assert response.get_json()["error_code"] == "UNAUTHENTICATED"
        finally:
            app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def test_logged_in_admin_can_resume_in_production_mode(self, admin_client, app):
        from services.api_rate_limiter import reset_all

        app.config.update(TESTING=False, WTF_CSRF_ENABLED=False, EMERGENCY_STOP=True)
        app.db.set_setting_value("password_must_change", "0")
        reset_all()
        try:
            response = admin_client.post("/api/emergency-resume", content_type="application/json")
            assert response.status_code == 200
            assert response.get_json()["success"] is True
        finally:
            app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, EMERGENCY_STOP=False)
            reset_all()
