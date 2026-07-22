"""Safety regression tests for direct zone MQTT rate limiting."""

from unittest.mock import patch

import pytest

from services.api_rate_limiter import reset_all as reset_rate_limits
from tests.safety_contracts import complete_group_stop_scheduler


def test_start_limit_never_blocks_explicit_zone_stop(admin_client, app):
    """A saturated start bucket must not turn the zone's OFF button into 429."""
    zone = app.db.create_zone({"name": "Rate limit safety", "duration": 10, "group_id": 1})
    app.db.set_setting_value("password_must_change", "0")

    reset_rate_limits()
    app.config["TESTING"] = False
    try:
        missing_zone_id = 999_999
        for _ in range(10):
            response = admin_client.post(f"/api/zones/{missing_zone_id}/mqtt/start")
            assert response.status_code == 404

        blocked_start = admin_client.post(f"/api/zones/{missing_zone_id}/mqtt/start")
        assert blocked_start.status_code == 429

        with patch("services.zone_control.stop_zone", return_value=True) as stop_zone:
            stop_response = admin_client.post(f"/api/zones/{zone['id']}/mqtt/stop")

        assert stop_response.status_code == 200
        assert stop_response.get_json()["success"] is True
        stop_zone.assert_called_once_with(zone["id"], reason="manual", force=True)
    finally:
        app.config["TESTING"] = True
        reset_rate_limits()


@pytest.mark.parametrize("stop_kind", ["legacy_zone", "group"])
def test_general_mutation_limit_never_blocks_fail_safe_stop(admin_client, app, stop_kind):
    """Exhausting general mutations must not suppress any explicit OFF path."""
    group = app.db.create_group(f"General rate OFF {stop_kind}")
    zone = app.db.create_zone({"name": "Rate limited OFF", "duration": 10, "group_id": group["id"]})
    app.db.set_setting_value("password_must_change", "0")

    reset_rate_limits()
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
    try:
        for index in range(30):
            response = admin_client.post(f"/api/zones/{900_000 + index}/start")
            assert response.status_code == 404

        blocked = admin_client.post("/api/zones/999999/start")
        assert blocked.status_code == 429

        if stop_kind == "legacy_zone":
            with (
                patch("routes.zones_watering_api.get_scheduler", return_value=None),
                patch("services.zone_control.stop_zone", return_value=True) as controller,
            ):
                stop_response = admin_client.post(f"/api/zones/{zone['id']}/stop")
            controller.assert_called_once_with(zone["id"], reason="manual", force=True)
        else:
            scheduler = complete_group_stop_scheduler(app.db)
            with patch("routes.groups_api.get_scheduler", return_value=scheduler):
                stop_response = admin_client.post(f"/api/groups/{group['id']}/stop")
            scheduler.cancel_group_jobs.assert_called_once_with(group["id"])

        assert stop_response.status_code == 200
        assert stop_response.get_json()["success"] is True
    finally:
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        reset_rate_limits()
