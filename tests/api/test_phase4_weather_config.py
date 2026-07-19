"""API regression coverage for atomic weather/rain configuration."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch


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
            json={"enabled": True, "topic": "/rain", "server_id": 1, "type": "NO"},
        )

    assert response.status_code == 500
    assert app.db.get_setting_value("rain.enabled") == "0"
    monitor.start.assert_not_called()


def test_rain_config_change_restarts_runtime_monitor(admin_client) -> None:
    with patch("routes.system_config_api.rain_monitor", create=True) as monitor:
        response = admin_client.post(
            "/api/rain",
            json={"enabled": False, "topic": "", "server_id": None, "type": "NC"},
        )

    assert response.status_code == 200
    monitor.start.assert_called_once_with({"enabled": False, "topic": "", "server_id": None, "type": "NC"})


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
