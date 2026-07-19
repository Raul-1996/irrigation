"""API regression coverage for atomic weather/rain configuration."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch


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


def test_live_weather_response_overlays_fresh_local_sources(admin_client) -> None:
    service_payload = {
        "available": True,
        "temperature": 18.0,
        "humidity": 40.0,
        "current": {
            "temperature": {"value": 18.0, "source": "api", "unit": "°C"},
            "humidity": {"value": 40.0, "source": "api", "unit": "%"},
            "rain": {"value": False, "source": "api"},
            "precipitation_mm": {"value": 0.0, "source": "api", "unit": "мм"},
        },
    }
    env = {
        "temp_enabled": True,
        "temp_value": 7.5,
        "temp_last_rx": 100.0,
        "temp_online": True,
        "hum_enabled": True,
        "hum_value": 82.0,
        "hum_last_rx": 100.0,
        "hum_online": True,
    }

    with (
        patch("services.weather.get_weather_service") as get_service,
        patch("services.weather_merged._get_env_state", return_value=env),
        patch("services.weather_merged._get_rain_state", return_value={"enabled": True, "is_rain": True}),
    ):
        get_service.return_value.get_weather_extended.return_value = service_payload
        response = admin_client.get("/api/weather")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["current"]["temperature"] == {"value": 7.5, "source": "local", "unit": "°C"}
    assert payload["current"]["humidity"] == {"value": 82.0, "source": "local", "unit": "%"}
    assert payload["current"]["rain"] == {"value": True, "source": "local"}
    assert payload["temperature"] == 7.5
    assert payload["humidity"] == 82.0
