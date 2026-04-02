"""Tests: guest (no admin) can control zones/groups/emergency but NOT MQTT CRUD.

Since TESTING=1 disables both middleware and @admin_required decorator,
we verify:
1. Structure: that @admin_required is absent from zone control endpoints
   and present on MQTT CRUD endpoints.
2. _is_status_action(): that the path matcher correctly allows/denies paths.
"""
import pytest
import re


class TestIsStatusAction:
    """Test the _is_status_action() whitelist in app.py."""

    @pytest.fixture(autouse=True)
    def _import_fn(self, app):
        """Import _is_status_action from app module."""
        import app as app_mod
        self._is_status_action = app_mod._is_status_action

    # -- Allowed paths (guest can access) --

    def test_zone_mqtt_start(self):
        assert self._is_status_action('/api/zones/1/mqtt/start')

    def test_zone_mqtt_stop(self):
        assert self._is_status_action('/api/zones/42/mqtt/stop')

    def test_zone_start(self):
        assert self._is_status_action('/api/zones/7/start')

    def test_zone_stop(self):
        assert self._is_status_action('/api/zones/99/stop')

    def test_group_stop(self):
        assert self._is_status_action('/api/groups/3/stop')

    def test_group_start_from_first(self):
        assert self._is_status_action('/api/groups/5/start-from-first')

    def test_group_master_valve_open(self):
        assert self._is_status_action('/api/groups/2/master-valve/open')

    def test_group_master_valve_close(self):
        assert self._is_status_action('/api/groups/2/master-valve/close')

    def test_group_start_zone(self):
        assert self._is_status_action('/api/groups/1/start-zone/5')

    def test_emergency_stop(self):
        assert self._is_status_action('/api/emergency-stop')

    def test_emergency_resume(self):
        assert self._is_status_action('/api/emergency-resume')

    def test_postpone(self):
        assert self._is_status_action('/api/postpone')

    def test_status(self):
        assert self._is_status_action('/api/status')

    # -- Denied paths (admin only) --

    def test_mqtt_servers_denied(self):
        assert not self._is_status_action('/api/mqtt/servers')

    def test_mqtt_server_by_id_denied(self):
        assert not self._is_status_action('/api/mqtt/servers/1')

    def test_mqtt_server_test_denied(self):
        assert not self._is_status_action('/api/mqtt/servers/1/test')

    def test_programs_denied(self):
        assert not self._is_status_action('/api/programs')

    def test_zones_crud_denied(self):
        assert not self._is_status_action('/api/zones')

    def test_groups_crud_denied(self):
        assert not self._is_status_action('/api/groups')

    def test_settings_denied(self):
        assert not self._is_status_action('/api/settings')

    def test_backup_denied(self):
        assert not self._is_status_action('/api/backup')

    # -- Edge cases --

    def test_zone_mqtt_start_no_traversal(self):
        """Path traversal should not match."""
        assert not self._is_status_action('/api/zones/1/mqtt/start/../../../admin')

    def test_zone_non_numeric_id(self):
        """Non-numeric zone id should not match."""
        assert not self._is_status_action('/api/zones/abc/mqtt/start')


class TestDecoratorPresence:
    """Verify @admin_required is removed from zone/group control and
    kept on MQTT CRUD endpoints."""

    def test_zone_watering_no_admin_required(self):
        """zones_watering_api.py should NOT import admin_required."""
        import routes.zones_watering_api as mod
        source_file = mod.__file__
        with open(source_file) as f:
            source = f.read()
        assert 'admin_required' not in source, \
            "zones_watering_api.py still references admin_required"

    def test_groups_api_no_admin_required(self):
        """groups_api.py should NOT import admin_required."""
        import routes.groups_api as mod
        source_file = mod.__file__
        with open(source_file) as f:
            source = f.read()
        assert 'admin_required' not in source, \
            "groups_api.py still references admin_required"

    def test_system_emergency_no_admin_required(self):
        """system_emergency_api.py should NOT import admin_required."""
        import routes.system_emergency_api as mod
        source_file = mod.__file__
        with open(source_file) as f:
            source = f.read()
        assert 'admin_required' not in source, \
            "system_emergency_api.py still references admin_required"

    def test_mqtt_api_has_admin_required(self):
        """mqtt_api.py MUST still have @admin_required."""
        import routes.mqtt_api as mod
        source_file = mod.__file__
        with open(source_file) as f:
            source = f.read()
        assert 'admin_required' in source, \
            "mqtt_api.py lost admin_required — MQTT CRUD must stay protected!"


class TestGuestEndpointAccess:
    """Integration: guest client hits zone control endpoints (TESTING mode)."""

    def test_guest_emergency_stop(self, guest_client, app):
        """Guest can POST /api/emergency-stop."""
        resp = guest_client.post('/api/emergency-stop',
                                content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

    def test_guest_emergency_resume(self, guest_client, app):
        """Guest can POST /api/emergency-resume."""
        resp = guest_client.post('/api/emergency-resume',
                                content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
