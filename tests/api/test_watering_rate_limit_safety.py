"""Safety regression tests for direct zone MQTT rate limiting."""

from unittest.mock import patch

from services.api_rate_limiter import reset_all as reset_rate_limits


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
