"""API regression coverage for atomic weather/rain configuration."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest


def _create_enabled_broker(app, *, enabled: bool = True) -> int:
    server = app.db.create_mqtt_server(
        {
            "name": "rain-test",
            "host": "127.0.0.1",
            "port": 1883,
            "enabled": enabled,
        }
    )
    assert server is not None
    return int(server["id"])


def test_weather_validation_happens_before_any_setting_is_written(admin_client, app) -> None:
    assert app.db.set_setting_value("weather.enabled", "0")

    response = admin_client.put(
        "/api/settings/weather",
        json={"enabled": True, "rain_threshold_mm": "not-a-number"},
    )

    assert response.status_code == 400
    assert app.db.get_setting_value("weather.enabled") == "0"


def test_weather_database_failure_rolls_back_the_whole_batch(admin_client, app) -> None:
    assert app.db.set_setting_value("weather.enabled", "0")
    with sqlite3.connect(app.db.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_freeze_setting
            BEFORE INSERT ON settings
            WHEN NEW.key = 'weather.freeze_threshold_c'
            BEGIN
                SELECT RAISE(ABORT, 'simulated write failure');
            END
            """
        )
        conn.commit()

    response = admin_client.put(
        "/api/settings/weather",
        json={"enabled": True, "freeze_threshold_c": 1.0},
    )

    assert response.status_code == 500
    assert app.db.get_setting_value("weather.enabled") == "0"


def test_location_validates_both_coordinates_before_commit(admin_client, app) -> None:
    assert app.db.set_setting_value("weather.latitude", "55.0")
    assert app.db.set_setting_value("weather.longitude", "37.0")

    response = admin_client.put(
        "/api/settings/location",
        json={"latitude": 12.0, "longitude": 999.0},
    )

    assert response.status_code == 400
    assert app.db.get_setting_value("weather.latitude") == "55.0"
    assert app.db.get_setting_value("weather.longitude") == "37.0"


def test_rain_config_database_failure_rolls_back_and_does_not_restart(admin_client, app) -> None:
    assert app.db.set_setting_value("rain.enabled", "0")
    server_id = _create_enabled_broker(app)
    with sqlite3.connect(app.db.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_rain_topic
            BEFORE INSERT ON settings
            WHEN NEW.key = 'rain.topic'
            BEGIN
                SELECT RAISE(ABORT, 'simulated write failure');
            END
            """
        )
        conn.commit()

    with patch("routes.system_config_api.rain_monitor", create=True) as monitor:
        response = admin_client.post(
            "/api/rain",
            json={"enabled": True, "topic": "/rain", "server_id": server_id, "type": "NO"},
        )

    assert response.status_code == 500
    assert app.db.get_setting_value("rain.enabled") == "0"
    monitor.reconfigure.assert_not_called()


def test_rain_config_change_restarts_runtime_monitor(admin_client) -> None:
    with patch("routes.system_config_api.rain_monitor", create=True) as monitor:
        monitor.reconfigure.return_value = True
        response = admin_client.post(
            "/api/rain",
            json={"enabled": False, "topic": "", "server_id": None, "type": "NC"},
        )

    assert response.status_code == 200
    monitor.reconfigure.assert_called_once_with({"enabled": False, "topic": "", "server_id": None, "type": "NC"})


def test_rain_config_rejects_non_string_or_wildcard_topics_before_write(admin_client, app) -> None:
    assert app.db.set_setting_value("rain.topic", "/old")
    server_id = _create_enabled_broker(app)
    with patch("routes.system_config_api.rain_monitor", create=True) as monitor:
        for topic in (7, "/rain/+", "/rain/#", "bad\x00topic"):
            response = admin_client.post(
                "/api/rain",
                json={"enabled": True, "topic": topic, "server_id": server_id, "type": "NO"},
            )
            assert response.status_code == 400

    assert app.db.get_setting_value("rain.topic") == "/old"
    monitor.reconfigure.assert_not_called()


def test_rain_config_rejects_missing_or_disabled_broker(admin_client, app) -> None:
    disabled_id = _create_enabled_broker(app, enabled=False)
    with patch("routes.system_config_api.rain_monitor", create=True) as monitor:
        missing = admin_client.post(
            "/api/rain",
            json={"enabled": True, "topic": "/rain", "server_id": disabled_id + 10_000, "type": "NO"},
        )
        disabled = admin_client.post(
            "/api/rain",
            json={"enabled": True, "topic": "/rain", "server_id": disabled_id, "type": "NO"},
        )

    assert missing.status_code == 400
    assert disabled.status_code == 400
    monitor.reconfigure.assert_not_called()


def test_rain_runtime_failure_rolls_back_settings_and_group_flags(admin_client, app) -> None:
    server_id = _create_enabled_broker(app)
    assert app.db.set_setting_value("rain.enabled", "0")
    assert app.db.set_setting_value("rain.topic", "/old")
    assert app.db.set_setting_value("rain.type", "NC")
    assert app.db.set_group_use_rain(1, False)

    with patch("routes.system_config_api.rain_monitor", create=True) as monitor:
        monitor.reconfigure.return_value = False
        response = admin_client.post(
            "/api/rain",
            json={"enabled": True, "topic": "/new", "server_id": server_id, "type": "NO"},
        )

    assert response.status_code == 503
    assert response.get_json()["rollback_success"] is True
    assert app.db.get_setting_value("rain.enabled") == "0"
    assert app.db.get_setting_value("rain.topic") == "/old"
    assert app.db.get_setting_value("rain.type") == "NC"
    assert app.db.get_setting_value("rain.server_id") is None
    assert app.db.get_group_use_rain(1) is False


def test_rain_runtime_failure_never_erases_concurrent_group_enable(admin_client, app) -> None:
    server_id = _create_enabled_broker(app)
    assert app.db.set_rain_config({"enabled": False, "topic": "/old", "server_id": None, "type": "NC"})
    assert app.db.set_group_use_rain(1, False)

    def concurrent_enable(_config):
        assert app.db.set_group_use_rain(1, True)
        return False

    with patch("routes.system_config_api.rain_monitor", create=True) as monitor:
        monitor.reconfigure.side_effect = concurrent_enable
        response = admin_client.post(
            "/api/rain",
            json={"enabled": True, "topic": "/new", "server_id": server_id, "type": "NO"},
        )

    assert response.status_code == 503
    assert response.get_json()["rollback_success"] is False
    assert app.db.get_group_use_rain(1) is True
    # Exact rollback is all-or-none: the concurrent writer changed only the
    # provenance timestamp while preserving value=1, and still must win.
    assert app.db.get_setting_value("rain.enabled") == "1"


def test_global_rain_enable_returns_authoritative_group_flags(admin_client, app) -> None:
    server_id = _create_enabled_broker(app)
    assert app.db.set_group_use_rain(1, False)

    with patch("routes.system_config_api.rain_monitor", create=True) as monitor:
        monitor.reconfigure.return_value = True
        response = admin_client.post(
            "/api/rain",
            json={"enabled": True, "topic": "/rain", "server_id": server_id, "type": "NO"},
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["config"] == {
        "enabled": True,
        "topic": "/rain",
        "server_id": server_id,
        "type": "NO",
    }
    flags = {row["id"]: row["use_rain_sensor"] for row in payload["groups"]}
    assert flags[1] is True
    assert app.db.get_group_use_rain(1) is True


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf"), 10**400])
def test_weather_api_rejects_nonfinite_stale_fallback_days(admin_client, app, invalid) -> None:
    assert app.db.set_setting_value("weather.balance.stale_fallback_days", "2")

    response = admin_client.put(
        "/api/settings/weather",
        json={"balance": {"stale_fallback_days": invalid}},
    )

    assert response.status_code == 400
    assert app.db.get_setting_value("weather.balance.stale_fallback_days") == "2"


def test_live_weather_response_preserves_one_canonical_effective_snapshot(admin_client) -> None:
    service_payload = {
        "available": True,
        "temperature": 0.0,
        "humidity": 40.0,
        "current": {
            "temperature": {"value": 0.0, "source": "local", "unit": "°C"},
            "humidity": {"value": 40.0, "source": "local", "unit": "%"},
            "rain": {"value": False, "source": "api"},
            "precipitation_mm": {"value": 0.0, "source": "api", "unit": "мм"},
        },
        "adjustment": {
            "skip": True,
            "skip_reason": "freeze_skip: 0.0°C",
            "factors": {"freeze": {"status": "danger", "enabled": True}},
        },
    }
    env = {
        "temp_enabled": True,
        "temp_value": 30.0,
        "temp_last_rx": 100.0,
        "temp_online": True,
        "hum_enabled": True,
        "hum_value": 70.0,
        "hum_last_rx": 100.0,
        "hum_online": True,
    }

    with (
        patch("services.weather.get_weather_service") as get_service,
        patch("services.weather_merged._get_env_state", return_value=env) as get_env,
        patch("services.weather_merged._get_rain_state", return_value={"enabled": True, "is_rain": True}),
    ):
        get_service.return_value.get_weather_extended.return_value = service_payload
        response = admin_client.get("/api/weather")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["current"]["temperature"] == {"value": 0.0, "source": "local", "unit": "°C"}
    assert payload["current"]["humidity"] == {"value": 40.0, "source": "local", "unit": "%"}
    assert payload["current"]["rain"] == {"value": True, "source": "local"}
    assert payload["temperature"] == 0.0
    assert payload["humidity"] == 40.0
    assert payload["adjustment"]["skip_reason"] == "freeze_skip: 0.0°C"
    get_env.assert_not_called()
