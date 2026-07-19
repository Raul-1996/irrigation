"""Regression tests for the Phase 4 weather safety review."""

from __future__ import annotations

import json
import sqlite3
import ssl
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from services.monitors.rain_monitor import RainMonitor
from services.weather.adjustment import WeatherAdjustment
from services.weather.models import WeatherData
from services.weather.service import WeatherService


def _weather(**overrides):
    values = {
        "temperature": 25.0,
        "humidity": 50.0,
        "precipitation": 0.0,
        "precipitation_24h": 0.0,
        "precipitation_forecast_6h": 0.0,
        "wind_speed": 0.0,
        "daily_et0": 4.5,
        "min_temp_forecast_6h": 25.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _enable_weather(test_db) -> WeatherAdjustment:
    assert test_db.set_setting_value("weather.enabled", "1")
    return WeatherAdjustment(test_db.db_path)


def test_hard_mismatch_never_discards_the_colder_local_reading(test_db) -> None:
    """A disagreement may flag a sensor, but must not hide a freeze reading."""
    adj = _enable_weather(test_db)
    api = _weather(temperature=8.0, humidity=45.0)
    local = {
        "temp_enabled": True,
        "temp_value": -3.0,
        "temp_last_rx": 100.0,
        "temp_online": True,
        "hum_enabled": True,
        "hum_value": 70.0,
        "hum_last_rx": 100.0,
        "hum_online": True,
    }

    with patch("services.weather.merge._get_env_state", return_value=local):
        verdict = adj.evaluate_sensor_source(api)

    assert verdict["mismatch"]["level"] == "hard"
    assert verdict["temperature"] == -3.0
    assert verdict["temp_source"] == "local"


def test_open_meteo_request_includes_previous_day_for_rolling_24h() -> None:
    from services.weather import client

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"hourly": {}, "daily": {}}

    with patch("requests.get", return_value=response) as get:
        client.fetch_api(55.7, 37.6)

    assert get.call_args.kwargs["params"]["past_days"] == 1


def test_missing_current_hour_is_not_mapped_to_first_payload_sample() -> None:
    raw = {
        "utc_offset_seconds": 0,
        "hourly": {
            "time": ["2001-01-01T00:00"],
            "temperature_2m": [41.0],
            "relative_humidity_2m": [5.0],
            "precipitation": [99.0],
            "wind_speed_10m": [20.0],
        },
        "daily": {"time": ["2001-01-01"], "precipitation_sum": [99.0]},
    }

    weather = WeatherData(raw)

    assert weather.temperature is None
    assert weather.humidity is None
    assert weather.precipitation_24h == 0.0
    assert weather.daily_precipitation is None
    assert weather.daily_forecast == []


def test_daily_fields_follow_location_date_across_midnight() -> None:
    now = datetime(2026, 7, 19, 0, 10, tzinfo=UTC)
    epoch = now.timestamp()
    raw = {
        "utc_offset_seconds": 0,
        "hourly": {
            "time": ["2026-07-19T00:00"],
            "temperature_2m": [12.0],
            "relative_humidity_2m": [60.0],
            "precipitation": [0.0],
            "wind_speed_10m": [1.0],
        },
        "daily": {
            "time": ["2026-07-18", "2026-07-19", "2026-07-20"],
            "precipitation_sum": [90.0, 1.5, 2.5],
            "et0_fao_evapotranspiration": [9.0, 3.0, 4.0],
            "temperature_2m_min": [-20.0, 7.0, 8.0],
            "temperature_2m_max": [40.0, 18.0, 19.0],
            "weather_code": [99, 2, 3],
            "sunrise": ["2026-07-18T01:00", "2026-07-19T04:30", "2026-07-20T04:31"],
            "sunset": ["2026-07-18T23:00", "2026-07-19T20:30", "2026-07-20T20:29"],
        },
    }

    with patch("services.weather.models.time.time", return_value=epoch):
        weather = WeatherData(raw)

    assert weather.daily_precipitation == 1.5
    assert weather.daily_et0 == 3.0
    assert weather.temperature_min == 7.0
    assert weather.sunrise == "04:30"
    assert [item["date"] for item in weather.daily_forecast] == ["2026-07-19", "2026-07-20"]


def test_api_failure_rejects_cache_older_than_degraded_mode_bound(test_db) -> None:
    assert test_db.set_setting_value("weather.latitude", "55.7")
    assert test_db.set_setting_value("weather.longitude", "37.6")
    now = datetime.now(tz=UTC)
    times = [(now + timedelta(hours=offset)).strftime("%Y-%m-%dT%H:00") for offset in range(-23, 25)]
    raw = {
        "utc_offset_seconds": 0,
        "hourly": {
            "time": times,
            "temperature_2m": [20.0] * len(times),
            "relative_humidity_2m": [50.0] * len(times),
            "precipitation": [0.0] * len(times),
            "wind_speed_10m": [1.0] * len(times),
        },
        "daily": {"time": [now.date().isoformat()]},
    }
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute(
            "INSERT INTO weather_cache(latitude, longitude, data, fetched_at) VALUES (?, ?, ?, ?)",
            (55.7, 37.6, json.dumps(raw), now.timestamp() - 7 * 3600),
        )
        conn.commit()

    svc = WeatherService(test_db.db_path)
    with patch.object(svc, "_fetch_api", return_value=None):
        assert svc.get_weather(force_refresh=True) is None


@pytest.mark.parametrize(
    ("weather", "expected_type"),
    [
        (_weather(precipitation_24h=5.0), "rain"),
        (_weather(temperature=2.0, min_temp_forecast_6h=2.0), "freeze"),
        (_weather(wind_speed=7.0), "wind"),
    ],
)
def test_safety_threshold_equality_skips_and_returns_zero(test_db, weather, expected_type) -> None:
    adj = _enable_weather(test_db)

    with patch.object(adj, "_get_weather", return_value=weather):
        skip = adj.should_skip()
        coefficient = adj.get_coefficient()

    assert skip["skip"] is True
    assert skip["details"]["type"] == expected_type
    assert coefficient == 0


def test_disabling_humidity_factor_removes_all_humidity_adjustment(test_db) -> None:
    adj = _enable_weather(test_db)
    assert test_db.set_setting_value("weather.factor.humidity", "0")
    weather = _weather(humidity=95.0)

    with patch.object(adj, "_get_weather", return_value=weather):
        assert adj.get_coefficient() == 100
        assert adj.get_factors_detail(weather)["humidity"]["detail"] == "фактор отключён"


def test_zero_rain_threshold_does_not_skip_when_it_is_dry(test_db) -> None:
    adj = _enable_weather(test_db)
    assert test_db.set_setting_value("weather.rain_threshold_mm", "0")
    weather = _weather(precipitation_24h=0.0, precipitation_forecast_6h=0.0)

    with patch.object(adj, "_get_weather", return_value=weather):
        assert adj.should_skip()["skip"] is False
        assert adj.get_coefficient() == 100


def test_rain_monitor_start_replaces_or_disables_existing_client() -> None:
    monitor = RainMonitor()
    old_client = MagicMock()
    monitor.client = old_client

    with patch.object(monitor, "_ensure_client") as ensure:
        monitor.start({"enabled": False, "topic": "", "server_id": None, "type": "NO"})

    old_client.loop_stop.assert_called_once_with()
    old_client.disconnect.assert_called_once_with()
    ensure.assert_not_called()
    assert monitor.client is None
    assert monitor._cfg == {"enabled": False, "topic": "", "server_id": None, "type": "NO"}


def test_rain_monitor_restart_stays_fail_closed_until_new_sensor_value() -> None:
    monitor = RainMonitor()
    monitor.client = MagicMock()
    monitor.is_rain = True

    with (
        patch.object(monitor, "_ensure_client") as ensure,
        patch.object(monitor, "_on_rain_stop") as clear_rain,
    ):
        monitor.start({"enabled": True, "topic": "/rain", "server_id": 1, "type": "NO"})

    ensure.assert_called_once_with()
    clear_rain.assert_not_called()
    assert monitor.is_rain is True


def test_rain_stop_does_not_erase_manual_program_cancellations() -> None:
    monitor = RainMonitor()
    fake_db = MagicMock()
    fake_db.get_groups.return_value = [{"id": 1}]
    fake_db.get_group_use_rain.return_value = True
    fake_db.get_zones.return_value = []

    with patch("services.monitors.rain_monitor.db", fake_db):
        monitor._on_rain_stop()

    fake_db.clear_program_cancellations_for_group_on_date.assert_not_called()


def _mqtt_module(client):
    return SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=object()),
        Client=MagicMock(return_value=client),
    )


def _tls_server(**overrides):
    server = {
        "id": 1,
        "host": "mqtt.example",
        "port": 8883,
        "username": "rain",
        "password": "secret",
        "tls_enabled": 1,
        "tls_ca_path": "/etc/ssl/rain-ca.pem",
        "tls_cert_path": None,
        "tls_key_path": None,
        "tls_insecure": 0,
        "tls_version": "TLSV1.2",
    }
    server.update(overrides)
    return server


def test_rain_monitor_configures_broker_tls_before_connect() -> None:
    monitor = RainMonitor()
    monitor.server_id = 1
    monitor.topic = "/rain"
    client = MagicMock()
    fake_db = MagicMock()
    fake_db.get_mqtt_server.return_value = _tls_server(tls_insecure=1)

    with (
        patch("services.monitors.rain_monitor.db", fake_db),
        patch("services.monitors.rain_monitor.mqtt", _mqtt_module(client)),
    ):
        monitor._ensure_client()

    client.tls_set.assert_called_once_with(
        ca_certs="/etc/ssl/rain-ca.pem",
        certfile=None,
        keyfile=None,
        tls_version=ssl.PROTOCOL_TLSv1_2,
    )
    client.tls_insecure_set.assert_called_once_with(True)
    client.connect.assert_called_once_with("mqtt.example", 8883, 30)
    method_names = [method_call[0] for method_call in client.method_calls]
    assert method_names.index("tls_set") < method_names.index("connect")


def test_rain_monitor_tls_failure_never_falls_back_to_plaintext() -> None:
    monitor = RainMonitor()
    monitor.server_id = 1
    monitor.topic = "/rain"
    client = MagicMock()
    client.tls_set.side_effect = OSError("bad CA")
    fake_db = MagicMock()
    fake_db.get_mqtt_server.return_value = _tls_server()

    with (
        patch("services.monitors.rain_monitor.db", fake_db),
        patch("services.monitors.rain_monitor.mqtt", _mqtt_module(client)),
    ):
        monitor._ensure_client()

    client.connect.assert_not_called()
    client.loop_start.assert_not_called()
    assert monitor.client is None


def test_rain_monitor_rejects_unknown_tls_version_before_connect() -> None:
    monitor = RainMonitor()
    monitor.server_id = 1
    monitor.topic = "/rain"
    client = MagicMock()
    fake_db = MagicMock()
    fake_db.get_mqtt_server.return_value = _tls_server(tls_version="SSLv3")

    with (
        patch("services.monitors.rain_monitor.db", fake_db),
        patch("services.monitors.rain_monitor.mqtt", _mqtt_module(client)),
    ):
        monitor._ensure_client()

    client.tls_set.assert_not_called()
    client.connect.assert_not_called()
