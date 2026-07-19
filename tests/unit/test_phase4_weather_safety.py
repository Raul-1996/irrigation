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


def _scheduler_stop_result(
    group_id: int,
    *,
    stopped: list[int],
    unresolved: list[int] | None = None,
    unverified: list[int] | None = None,
    aggregate_valid: bool = True,
    retry_scheduled: bool = False,
) -> dict:
    unresolved = list(unresolved or [])
    unverified = list(unverified or [])
    return {
        "success": aggregate_valid and not unresolved and not unverified,
        "group_id": int(group_id),
        "aggregate_valid": aggregate_valid,
        "stopped": list(stopped),
        "unresolved": unresolved,
        "unverified_zone_ids": unverified,
        "retry_scheduled": retry_scheduled,
    }


def _core_stop_result(
    group_id: int,
    *,
    stopped: list[int],
    unresolved: list[int] | None = None,
    retry_scheduled: bool = False,
) -> dict:
    unresolved = list(unresolved or [])
    return {
        "success": not unresolved,
        "group_id": int(group_id),
        "stopped": list(stopped),
        "unresolved": unresolved,
        "retry_scheduled": retry_scheduled,
    }


def _raw_weather_window(
    now: datetime,
    *,
    current_temperature: object = 20.0,
    future_temperatures: list[object] | None = None,
    future_precipitation: list[object] | None = None,
) -> dict:
    offsets = list(range(-23, 7))
    times = [(now + timedelta(hours=offset)).strftime("%Y-%m-%dT%H:00") for offset in offsets]
    temperatures: list[object] = [20.0] * len(offsets)
    precipitation: list[object] = [0.0] * len(offsets)
    current_index = 23
    temperatures[current_index] = current_temperature
    if future_temperatures is not None:
        temperatures[current_index + 1 : current_index + 7] = future_temperatures
    if future_precipitation is not None:
        precipitation[current_index + 1 : current_index + 7] = future_precipitation
    return {
        "_fetched_at": now.timestamp(),
        "utc_offset_seconds": 0,
        "hourly": {
            "time": times,
            "temperature_2m": temperatures,
            "relative_humidity_2m": [50.0] * len(offsets),
            "precipitation": precipitation,
            "wind_speed_10m": [1.0] * len(offsets),
            "weather_code": [0] * len(offsets),
        },
        "daily": {"time": [now.date().isoformat()], "et0_fao_evapotranspiration": [4.5]},
    }


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


def test_colder_local_hard_mismatch_reaches_the_freeze_gate(test_db) -> None:
    """Source selection must carry the colder local value into hard safety."""
    adj = _enable_weather(test_db)
    api = _weather(temperature=8.0, humidity=45.0)
    api.timestamp = datetime.now(tz=UTC).timestamp()
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
    service = MagicMock()
    service.get_weather.return_value = api

    with (
        patch("services.weather.adjustment.get_weather_service", return_value=service),
        patch("services.weather.merge._get_env_state", return_value=local),
    ):
        skip = adj.should_skip()
        coefficient = adj.get_coefficient()

    assert skip["skip"] is True
    assert skip["details"]["type"] == "freeze"
    assert skip["details"]["value"] == -3.0
    assert coefficient == 0


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
    assert weather.precipitation_24h is None
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


def test_future_cache_timestamp_is_rejected(test_db) -> None:
    assert test_db.set_setting_value("weather.latitude", "55.7")
    assert test_db.set_setting_value("weather.longitude", "37.6")
    now = datetime.now(tz=UTC)
    raw = {
        "utc_offset_seconds": 0,
        "hourly": {"time": [now.strftime("%Y-%m-%dT%H:00")]},
        "daily": {"time": [now.date().isoformat()]},
    }
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute(
            "INSERT INTO weather_cache(latitude, longitude, data, fetched_at) VALUES (?, ?, ?, ?)",
            (55.7, 37.6, json.dumps(raw), now.timestamp() + 60),
        )
        conn.commit()

    assert WeatherService(test_db.db_path).get_weather(cache_only=True) is None


def test_partial_hourly_history_is_not_labeled_as_24h_precipitation() -> None:
    now = datetime(2026, 7, 19, 10, 10, tzinfo=UTC)
    times = [(now + timedelta(hours=offset)).strftime("%Y-%m-%dT%H:00") for offset in range(-5, 7)]
    raw = {
        "utc_offset_seconds": 0,
        "hourly": {
            "time": times,
            "temperature_2m": [20.0] * len(times),
            "relative_humidity_2m": [50.0] * len(times),
            "precipitation": [1.0] * len(times),
            "wind_speed_10m": [1.0] * len(times),
        },
        "daily": {"time": [now.date().isoformat()]},
    }

    with patch("services.weather.models.time.time", return_value=now.timestamp()):
        weather = WeatherData(raw)

    assert weather.precipitation_24h is None


def test_incomplete_24h_rain_history_is_skip_safe(test_db) -> None:
    adj = _enable_weather(test_db)
    weather = _weather(precipitation_24h=None)

    with patch.object(adj, "_get_weather", return_value=weather):
        skip = adj.should_skip()
        coefficient = adj.get_coefficient()
        detail = adj.get_factors_detail(weather)["rain"]

    assert skip["skip"] is True
    assert skip["details"] == {
        "type": "weather_unavailable",
        "field": "precipitation_24h",
        "api_unavailable": False,
    }
    assert coefficient == 0
    assert detail["status"] == "danger"
    assert "24ч" in detail["detail"]


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


def test_weather_payload_labels_water_balance_as_shadow_only(test_db) -> None:
    _enable_weather(test_db)
    assert test_db.set_setting_value("weather.balance.enabled", "1")
    assert test_db.set_setting_value("weather.balance.last_recalc_date", datetime.now().date().isoformat())
    assert test_db.set_setting_value("weather.balance.coef_cached", "130")
    weather = _weather()
    weather.timestamp = datetime.now(tz=UTC).timestamp()
    weather.weather_code = 0
    weather.hourly_forecast_24h = []
    weather.daily_forecast = []
    weather.sunrise = None
    weather.sunset = None
    service = WeatherService(test_db.db_path)
    local_disabled = {
        "temp_enabled": False,
        "temp_value": None,
        "temp_last_rx": None,
        "temp_online": False,
        "hum_enabled": False,
        "hum_value": None,
        "hum_last_rx": None,
        "hum_online": False,
    }

    with (
        patch.object(service, "get_weather", return_value=weather),
        patch("services.weather.adjustment.get_weather_service", return_value=service),
        patch("services.weather.merge._get_env_state", return_value=local_disabled),
    ):
        adjustment = service.get_weather_extended()["adjustment"]

    assert adjustment["mode"] == "shadow"
    assert adjustment["coefficient"] == 100
    assert adjustment["coefficient_applied"] == 100
    assert adjustment["coefficient_legacy"] == 100
    assert adjustment["coefficient_balance"] == 130
    assert adjustment["balance_status"] == "fresh"
    assert adjustment["balance_last_recalc_date"] == datetime.now().date().isoformat()
    assert adjustment["balance_age_days"] == 0
    assert adjustment["balance_stale"] is False
    assert adjustment["balance_active"] is False


@pytest.mark.parametrize(
    ("offset_days", "expected_status", "expected_stale"),
    [(-5, "stale", True), (1, "future", False)],
)
def test_h2_diagnostic_date_reports_stale_and_future_truthfully(
    test_db, offset_days, expected_status, expected_stale
) -> None:
    adj = _enable_weather(test_db)
    recalc_date = (datetime.now().date() + timedelta(days=offset_days)).isoformat()
    assert test_db.set_setting_value("weather.balance.last_recalc_date", recalc_date)

    status = adj.get_balance_diagnostic_status()

    assert status["status"] == expected_status
    assert status["last_recalc_date"] == recalc_date
    assert status["age_days"] == -offset_days
    assert status["stale"] is expected_stale


@pytest.mark.parametrize("invalid", ["nan", "inf", "-inf", "1e999"])
def test_h2_diagnostic_rejects_nonfinite_or_overflow_stale_days(test_db, invalid) -> None:
    adj = _enable_weather(test_db)
    assert test_db.set_setting_value("weather.balance.last_recalc_date", datetime.now().date().isoformat())
    assert test_db.set_setting_value("weather.balance.stale_fallback_days", invalid)

    status = adj.get_balance_diagnostic_status()

    assert status == {
        "status": "unavailable",
        "last_recalc_date": None,
        "age_days": None,
        "stale": False,
        "fresh": False,
    }


def test_hard_freeze_safety_ignores_the_factor_toggle(test_db) -> None:
    adj = _enable_weather(test_db)
    assert test_db.set_setting_value("weather.factor.freeze", "0")

    with patch.object(adj, "_get_weather", return_value=_weather(temperature=-3.0)):
        assert adj.get_coefficient() == 0


@pytest.mark.parametrize(
    ("weather", "factor", "expected_text"),
    [
        (_weather(precipitation_forecast_6h=5.0), "rain", "прогноз"),
        (_weather(temperature=10.0, min_temp_forecast_6h=2.0), "freeze", "прогноз"),
    ],
)
def test_factor_detail_matches_forecast_skip_truth(test_db, weather, factor, expected_text) -> None:
    adj = _enable_weather(test_db)

    with patch.object(adj, "_get_weather", return_value=weather):
        skip = adj.should_skip()
        detail = adj.get_factors_detail(weather)[factor]

    assert skip["skip"] is True
    assert detail["status"] == "danger"
    assert expected_text in detail["detail"]


def test_factor_detail_uses_effective_local_temperature(test_db) -> None:
    _enable_weather(test_db)
    weather = _weather(temperature=8.0, min_temp_forecast_6h=6.0)
    weather.timestamp = datetime.now(tz=UTC).timestamp()
    weather.weather_code = 0
    weather.hourly_forecast_24h = []
    weather.daily_forecast = []
    weather.sunrise = None
    weather.sunset = None
    local = {
        "temp_enabled": True,
        "temp_value": -3.0,
        "temp_last_rx": 100.0,
        "temp_online": True,
        "hum_enabled": False,
        "hum_value": None,
        "hum_last_rx": None,
        "hum_online": False,
    }
    service = WeatherService(test_db.db_path)

    with (
        patch.object(service, "get_weather", return_value=weather),
        patch("services.weather.adjustment.get_weather_service", return_value=service),
        patch("services.weather.merge._get_env_state", return_value=local),
    ):
        payload = service.get_weather_extended()

    assert payload["adjustment"]["skip"] is True
    assert payload["adjustment"]["factors"]["freeze"]["status"] == "danger"
    assert "-3.0" in payload["adjustment"]["factors"]["freeze"]["detail"]
    assert payload["current"]["temperature"] == {"value": -3.0, "source": "local", "unit": "°C"}


def test_decision_history_preserves_per_field_source_provenance(test_db) -> None:
    adj = _enable_weather(test_db)
    api = _weather(temperature=8.0, humidity=45.0)
    selected = adj._apply_source(
        api,
        {
            "temperature": -3.0,
            "humidity": 45.0,
            "temp_source": "local",
            "hum_source": "api_fallback",
            "mismatch": {"level": "hard"},
        },
    )

    adj.log_decision(selected, 0, True, "freeze")

    with sqlite3.connect(test_db.db_path) as conn:
        raw_sources = conn.execute("SELECT data_sources FROM weather_decisions ORDER BY id DESC LIMIT 1").fetchone()[0]
    sources = json.loads(raw_sources)
    assert sources["temperature"] == "local"
    assert sources["humidity"] == "api_fallback"
    assert sources["precipitation_24h"] == "api"
    assert sources["precipitation_forecast_6h"] == "api"
    assert sources["wind_speed"] == "api"
    assert sources["daily_et0"] == "api"


def test_missing_weather_snapshot_is_logged_as_unknown_and_skip_safe(test_db) -> None:
    adj = _enable_weather(test_db)

    with patch.object(adj, "_get_weather", return_value=None):
        skip = adj.should_skip()
        coefficient = adj.get_coefficient()
    adj.log_decision(None, 100, False, "")

    assert skip["skip"] is True
    assert skip["details"]["type"] == "weather_unavailable"
    assert coefficient == 0
    with sqlite3.connect(test_db.db_path) as conn:
        decision, coefficient, reason, raw_sources = conn.execute(
            "SELECT decision, coefficient, reason, data_sources FROM weather_decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert decision == "skip"
    assert coefficient == 0
    assert reason == "weather_unavailable"
    assert set(json.loads(raw_sources).values()) == {"unknown"}


def test_zero_rain_threshold_does_not_skip_when_it_is_dry(test_db) -> None:
    adj = _enable_weather(test_db)
    assert test_db.set_setting_value("weather.rain_threshold_mm", "0")
    weather = _weather(precipitation_24h=0.0, precipitation_forecast_6h=0.0)

    with patch.object(adj, "_get_weather", return_value=weather):
        assert adj.should_skip()["skip"] is False
        assert adj.get_coefficient() == 100


def test_relay_time_fields_are_strictly_canonicalized_before_ui_output() -> None:
    now = datetime(2026, 7, 19, 10, 10, tzinfo=UTC)
    raw = {
        "utc_offset_seconds": 0,
        "hourly": {
            "time": [
                "2026-07-19T10:00",
                "2026-07-19T11:00",
                '<img src=x onerror="alert(1)">',
                "2026-07-19T13:00",
                "2026-07-19T14:00",
            ],
            "temperature_2m": [20.0] * 5,
            "relative_humidity_2m": [50.0] * 5,
            "precipitation": [0.0] * 5,
            "wind_speed_10m": [1.0] * 5,
        },
        "daily": {
            "time": ["2026-07-19"],
            "sunrise": ['<img src=x onerror="alert(2)">'],
            "sunset": ["2026-07-19T20:30"],
        },
    }

    with patch("services.weather.models.time.time", return_value=now.timestamp()):
        weather = WeatherData(raw)

    assert weather.sunrise is None
    assert weather.sunset == "20:30"
    assert weather.hourly_forecast_24h == []
    assert weather.daily_forecast[0]["sunrise"] is None
    assert weather.daily_forecast[0]["sunset"] == "20:30"


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


def _ready_mqtt_client(*, suback_codes=None):
    client = MagicMock()
    client.connect.return_value = 0
    client.connect_async.return_value = 0
    codes = [0] if suback_codes is None else suback_codes

    def subscribe(topic, qos=0):
        client.on_subscribe(client, None, 41, codes, None)
        return 0, 41

    client.subscribe.side_effect = subscribe
    client.loop_start.side_effect = lambda: client.on_connect(client, None, None, 0, None)
    return client


def test_rain_monitor_swaps_only_after_successful_connack_and_suback() -> None:
    monitor = RainMonitor()
    old_client = MagicMock()
    monitor.client = old_client
    monitor._cfg = {"enabled": True, "topic": "/old", "server_id": 1, "type": "NO"}
    monitor.is_rain = True
    staged_client = _ready_mqtt_client()
    fake_db = MagicMock()
    fake_db.get_mqtt_server.return_value = _tls_server(tls_enabled=0, enabled=1)

    with (
        patch("services.monitors.rain_monitor.db", fake_db),
        patch("services.monitors.rain_monitor.mqtt", _mqtt_module(staged_client)),
    ):
        applied = monitor.reconfigure({"enabled": True, "topic": "/rain", "server_id": 1, "type": "NO"})

    assert applied is True
    assert monitor.client is staged_client
    assert monitor.topic == "/rain"
    assert monitor._cfg == {"enabled": True, "topic": "/rain", "server_id": 1, "type": "NO"}
    staged_client.subscribe.assert_called_once_with("/rain", qos=0)
    old_client.loop_stop.assert_called_once_with()
    old_client.disconnect.assert_called_once_with()
    assert monitor.sensor_online is True
    assert monitor.is_rain is None
    assert monitor.is_group_blocked(1) is True


def test_failed_suback_preserves_previous_rain_generation() -> None:
    monitor = RainMonitor()
    old_client = MagicMock()
    old_cfg = {"enabled": True, "topic": "/old", "server_id": 1, "type": "NO"}
    monitor.client = old_client
    monitor.topic = "/old"
    monitor.server_id = 1
    monitor._cfg = old_cfg
    monitor._generation = 7
    staged_client = _ready_mqtt_client(suback_codes=[128])
    fake_db = MagicMock()
    fake_db.get_mqtt_server.return_value = _tls_server(tls_enabled=0, enabled=1)

    with (
        patch("services.monitors.rain_monitor.db", fake_db),
        patch("services.monitors.rain_monitor.mqtt", _mqtt_module(staged_client)),
    ):
        applied = monitor.reconfigure({"enabled": True, "topic": "/new", "server_id": 2, "type": "NC"})

    assert applied is False
    assert monitor.client is old_client
    assert monitor._cfg == old_cfg
    assert monitor.topic == "/old"
    assert monitor.server_id == 1
    assert monitor._generation == 7
    old_client.loop_stop.assert_not_called()
    old_client.disconnect.assert_not_called()
    staged_client.loop_stop.assert_called_once_with()
    staged_client.disconnect.assert_called_once_with()


def test_retired_rain_generation_cannot_deliver_payloads() -> None:
    monitor = RainMonitor()
    first_client = _ready_mqtt_client()
    second_client = _ready_mqtt_client()
    mqtt_module = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=object()),
        Client=MagicMock(side_effect=[first_client, second_client]),
    )
    fake_db = MagicMock()
    fake_db.get_mqtt_server.return_value = _tls_server(tls_enabled=0, enabled=1)
    config = {"enabled": True, "topic": "/rain", "server_id": 1, "type": "NO"}

    with (
        patch("services.monitors.rain_monitor.db", fake_db),
        patch("services.monitors.rain_monitor.mqtt", mqtt_module),
    ):
        assert monitor.reconfigure(config) is True
        assert monitor.reconfigure({**config, "topic": "/rain/new"}) is True
        with patch.object(monitor, "_handle_payload") as handle:
            first_client.on_message(first_client, None, SimpleNamespace(payload=b"1"))

    handle.assert_not_called()


def test_rain_stop_does_not_erase_manual_program_cancellations() -> None:
    monitor = RainMonitor()
    fake_db = MagicMock()
    fake_db.get_groups.return_value = [{"id": 1}]
    fake_db.get_group_use_rain.return_value = True
    fake_db.get_zones.return_value = []

    with patch("services.monitors.rain_monitor.db", fake_db):
        monitor._on_rain_stop()

    fake_db.clear_program_cancellations_for_group_on_date.assert_not_called()


def test_rain_postpone_preserves_manual_owner_and_protects_unset_sibling(test_db) -> None:
    monitor = RainMonitor()
    group_id = 1
    manual = test_db.create_zone({"name": "manual", "duration": 10, "group_id": group_id})
    unset = test_db.create_zone({"name": "unset", "duration": 10, "group_id": group_id})
    assert test_db.set_group_use_rain(group_id, True)
    assert test_db.update_zone_postpone(manual["id"], "2099-12-31 23:59:59", "manual")

    with (
        patch("services.monitors.rain_monitor.db", test_db),
        patch(
            "services.zone_control.stop_all_in_group",
            return_value=_core_stop_result(1, stopped=[manual["id"], unset["id"]]),
        ),
        patch("irrigation_scheduler.get_scheduler", return_value=None),
    ):
        monitor._on_rain_start()

    manual_after = test_db.get_zone(manual["id"])
    unset_after = test_db.get_zone(unset["id"])
    assert manual_after["postpone_until"] == "2099-12-31 23:59:59"
    assert manual_after["postpone_reason"] == "manual"
    assert unset_after["postpone_until"] is not None
    assert unset_after["postpone_reason"] == "rain"

    with patch("services.monitors.rain_monitor.db", test_db):
        assert monitor._on_rain_stop() is True

    manual_after = test_db.get_zone(manual["id"])
    unset_after = test_db.get_zone(unset["id"])
    assert manual_after["postpone_until"] == "2099-12-31 23:59:59"
    assert manual_after["postpone_reason"] == "manual"
    assert unset_after["postpone_until"] is None
    assert unset_after["postpone_reason"] is None


def test_rain_gate_persists_until_explicit_successful_dry(test_db) -> None:
    monitor = RainMonitor()
    assert test_db.set_group_use_rain(1, True)
    with (
        patch("services.monitors.rain_monitor.db", test_db),
        patch("services.monitors.rain_monitor._apply_rain_postpone_deadline", return_value=True),
        patch.object(monitor, "_cancel_and_stop_group", return_value=True),
    ):
        assert monitor._on_rain_start() is True
        assert monitor.is_group_blocked(1) is True
        with patch("services.monitors.rain_monitor._clear_rain_postpone", return_value=False):
            assert monitor._on_rain_stop() is False
        assert monitor.is_group_blocked(1) is True
        with patch("services.monitors.rain_monitor._clear_rain_postpone", return_value=True):
            assert monitor._on_rain_stop() is True
        assert monitor.is_group_blocked(1) is False


def test_persisted_rain_gate_survives_monitor_restart_and_expired_deadline(test_db) -> None:
    assert test_db.set_group_use_rain(1, True)
    zone = test_db.create_zone({"name": "expired-rain", "duration": 10, "group_id": 1})
    assert test_db.update_zone_postpone(zone["id"], "2001-01-01 23:59:59", "rain")
    assert test_db.set_setting_value("rain.active", "1")

    restarted = RainMonitor()
    with patch("services.monitors.rain_monitor.db", test_db):
        assert restarted.is_group_blocked(1) is True
        assert restarted.is_rain is True


def test_rain_group_read_failure_is_fail_closed() -> None:
    monitor = RainMonitor()
    with (
        patch("services.monitors.rain_monitor._strict_target_groups", side_effect=sqlite3.Error("read failed")),
        patch.object(monitor, "_persist_rain_active", return_value=False),
    ):
        assert monitor._on_rain_start() is False
        assert monitor.is_group_blocked(42) is True


def test_rain_start_uses_structured_scheduler_cancellation_before_fallback_stop(test_db) -> None:
    monitor = RainMonitor()
    scheduler = MagicMock()
    assert test_db.set_group_use_rain(1, True)
    zone = test_db.create_zone({"name": "rain-stop", "duration": 10, "group_id": 1})
    scheduler.cancel_group_jobs.return_value = _scheduler_stop_result(1, stopped=[zone["id"]])

    with (
        patch("services.monitors.rain_monitor.db", test_db),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch("services.zone_control.stop_all_in_group") as fallback_stop,
        patch("services.monitors.rain_monitor._apply_rain_postpone_deadline", return_value=True),
    ):
        assert monitor._on_rain_start() is True

    scheduler.cancel_group_jobs.assert_called_once_with(1, master_close_immediately=True)
    fallback_stop.assert_not_called()


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
        "enabled": 1,
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
    client = _ready_mqtt_client()
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
    client.connect_async.assert_called_once_with("mqtt.example", 8883, 30)
    method_names = [method_call[0] for method_call in client.method_calls]
    assert method_names.index("tls_set") < method_names.index("connect_async")


def test_rain_monitor_tls_failure_never_falls_back_to_plaintext() -> None:
    monitor = RainMonitor()
    monitor.server_id = 1
    monitor.topic = "/rain"
    client = _ready_mqtt_client()
    client.tls_set.side_effect = OSError("bad CA")
    fake_db = MagicMock()
    fake_db.get_mqtt_server.return_value = _tls_server()

    with (
        patch("services.monitors.rain_monitor.db", fake_db),
        patch("services.monitors.rain_monitor.mqtt", _mqtt_module(client)),
    ):
        monitor._ensure_client()

    client.connect_async.assert_not_called()
    client.connect.assert_not_called()
    client.loop_start.assert_not_called()
    assert monitor.client is None


def test_rain_monitor_rejects_unknown_tls_version_before_connect() -> None:
    monitor = RainMonitor()
    monitor.server_id = 1
    monitor.topic = "/rain"
    client = _ready_mqtt_client()
    fake_db = MagicMock()
    fake_db.get_mqtt_server.return_value = _tls_server(tls_version="SSLv3")

    with (
        patch("services.monitors.rain_monitor.db", fake_db),
        patch("services.monitors.rain_monitor.mqtt", _mqtt_module(client)),
    ):
        monitor._ensure_client()

    client.tls_set.assert_not_called()
    client.connect_async.assert_not_called()
    client.connect.assert_not_called()


def test_candidate_disconnect_between_final_validation_and_swap_preserves_live_generation() -> None:
    monitor = RainMonitor()
    monitor._persisted_state_loaded = True
    old_client = MagicMock()
    monitor.client = old_client
    monitor._cfg = {"enabled": True, "topic": "/old", "server_id": 1, "type": "NO"}
    monitor.topic = "/old"
    monitor.server_id = 1
    monitor._generation = 4
    candidate = _ready_mqtt_client()
    server = _tls_server(tls_enabled=0, port=1883)
    fake_db = MagicMock()
    fake_db.get_mqtt_server.return_value = server
    requested = (True, "/new", "NO", 1)

    def disconnect_before_swap():
        candidate.on_disconnect(candidate, None, None, 0, None)
        return [1]

    with (
        patch("services.monitors.rain_monitor.db", fake_db),
        patch("services.monitors.rain_monitor.mqtt", _mqtt_module(candidate)),
        patch("services.monitors.rain_monitor._strict_persisted_rain_fingerprint", return_value=requested),
        patch("services.monitors.rain_monitor._strict_target_groups", side_effect=disconnect_before_swap),
    ):
        assert monitor.reconfigure({"enabled": True, "topic": "/new", "server_id": 1, "type": "NO"}) is False

    assert monitor.client is old_client
    assert monitor.topic == "/old"
    assert monitor._generation == 4
    old_client.disconnect.assert_not_called()
    candidate.disconnect.assert_called_once_with()


def test_rain_reconfigure_final_persisted_cas_rejects_stale_candidate() -> None:
    monitor = RainMonitor()
    monitor._persisted_state_loaded = True
    old_client = MagicMock()
    monitor.client = old_client
    monitor._cfg = {"enabled": True, "topic": "/old", "server_id": 1, "type": "NO"}
    monitor.topic = "/old"
    monitor.server_id = 1
    candidate = _ready_mqtt_client()
    server = _tls_server(tls_enabled=0, port=1883)
    fake_db = MagicMock()
    fake_db.get_mqtt_server.return_value = server
    requested = (True, "/new", "NO", 1)
    superseding = (True, "/newer", "NO", 1)

    with (
        patch("services.monitors.rain_monitor.db", fake_db),
        patch("services.monitors.rain_monitor.mqtt", _mqtt_module(candidate)),
        patch(
            "services.monitors.rain_monitor._strict_persisted_rain_fingerprint",
            side_effect=[requested, superseding],
        ),
    ):
        assert monitor.reconfigure({"enabled": True, "topic": "/new", "server_id": 1, "type": "NO"}) is False

    assert monitor.client is old_client
    assert monitor.topic == "/old"
    old_client.disconnect.assert_not_called()
    candidate.disconnect.assert_called_once_with()


def test_public_rain_config_lock_is_process_wide_and_reentrant() -> None:
    from services.monitors import rain_config_transaction_lock

    first = rain_config_transaction_lock()
    second = rain_config_transaction_lock()
    assert first is second
    assert first.acquire(blocking=False) is True
    try:
        assert first.acquire(blocking=False) is True
        first.release()
    finally:
        first.release()


def test_rain_gate_requires_suback_and_fresh_payload_after_reconnect(test_db) -> None:
    server = test_db.create_mqtt_server({"name": "rain", "host": "127.0.0.1", "port": 1883, "enabled": 1})
    assert server is not None
    config = {"enabled": True, "topic": "/rain", "server_id": int(server["id"]), "type": "NO"}
    assert test_db.set_rain_config(config)
    assert test_db.set_group_use_rain(1, True)
    client = _ready_mqtt_client()
    monitor = RainMonitor()

    with (
        patch("services.monitors.rain_monitor.db", test_db),
        patch("services.monitors.rain_monitor.mqtt", _mqtt_module(client)),
    ):
        assert monitor.reconfigure(config) is True
        assert monitor.sensor_online is True
        assert monitor.is_rain is None
        assert monitor.is_group_blocked(1) is True

        client.on_disconnect(client, None, None, 0, None)
        assert monitor.sensor_online is False
        assert monitor.is_rain is None
        assert monitor.is_group_blocked(1) is True

        client.on_connect(client, None, None, 0, None)
        assert monitor.sensor_online is True
        assert monitor.is_rain is None
        assert monitor.is_group_blocked(1) is True

        client.on_message(client, None, SimpleNamespace(payload=b"0"))
        assert monitor.sensor_online is True
        assert monitor.is_rain is False
        assert monitor.is_group_blocked(1) is False


def test_explicit_rain_disable_atomically_clears_gate_truth_and_owned_postpone(test_db) -> None:
    rain_zone = test_db.create_zone({"name": "rain", "duration": 10, "group_id": 1})
    manual_zone = test_db.create_zone({"name": "manual", "duration": 10, "group_id": 1})
    assert test_db.update_zone_postpone(rain_zone["id"], "2099-12-31 23:59:59", "rain")
    assert test_db.update_zone_postpone(manual_zone["id"], "2099-12-31 23:59:59", "manual")
    assert test_db.set_setting_value("rain.active", "1")
    disabled = {"enabled": False, "topic": "", "server_id": None, "type": "NO"}
    assert test_db.set_rain_config(disabled)
    monitor = RainMonitor()
    monitor.client = MagicMock()
    monitor._cfg = {"enabled": True, "topic": "/rain", "server_id": 1, "type": "NO"}
    monitor.is_rain = True
    monitor._blocked_groups = {1}

    with patch("services.monitors.rain_monitor.db", test_db):
        assert monitor.reconfigure(disabled) is True

    assert test_db.get_setting_value("rain.active") == "0"
    assert test_db.get_zone(rain_zone["id"])["postpone_reason"] is None
    assert test_db.get_zone(manual_zone["id"])["postpone_reason"] == "manual"
    assert monitor.client is None
    assert monitor.is_rain is None
    assert monitor.sensor_online is False
    assert monitor.is_group_blocked(1) is False


def test_failed_atomic_disable_cleanup_preserves_old_runtime_and_gate(test_db) -> None:
    zone = test_db.create_zone({"name": "rain", "duration": 10, "group_id": 1})
    assert test_db.update_zone_postpone(zone["id"], "2099-12-31 23:59:59", "rain")
    assert test_db.set_setting_value("rain.active", "1")
    disabled = {"enabled": False, "topic": "", "server_id": None, "type": "NO"}
    assert test_db.set_rain_config(disabled)
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_rain_cleanup
            BEFORE UPDATE ON zones
            WHEN OLD.postpone_reason = 'rain'
            BEGIN
                SELECT RAISE(ABORT, 'simulated cleanup failure');
            END
            """
        )
        conn.commit()

    monitor = RainMonitor()
    old_client = MagicMock()
    monitor.client = old_client
    monitor._cfg = {"enabled": True, "topic": "/rain", "server_id": 1, "type": "NO"}
    monitor.is_rain = True
    monitor._blocked_groups = {1}

    with patch("services.monitors.rain_monitor.db", test_db):
        assert monitor.reconfigure(disabled) is False

    assert monitor.client is old_client
    assert monitor.is_rain is True
    assert test_db.get_setting_value("rain.active") == "1"
    assert test_db.get_zone(zone["id"])["postpone_reason"] == "rain"
    old_client.disconnect.assert_not_called()


@pytest.mark.parametrize(
    "invalid_result",
    [None, True, {}, {"success": True}, {"success": True, "unresolved": [7]}],
)
def test_rain_stop_rejects_non_structured_or_incomplete_acknowledgement(invalid_result) -> None:
    monitor = RainMonitor()
    scheduler = MagicMock()
    scheduler.cancel_group_jobs.return_value = invalid_result

    with (
        patch("services.monitors.rain_monitor._strict_group_zone_ids", return_value={1}),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch("services.zone_control.stop_all_in_group", return_value=invalid_result),
    ):
        assert monitor._cancel_and_stop_group(1) is False


@pytest.mark.parametrize(
    "malformation",
    [
        "minimal",
        "wrong_group",
        "aggregate_false",
        "unverified",
        "overlap",
        "duplicate",
        "foreign",
        "missing",
        "claimed_retry",
        "bad_id_type",
        "extra_key",
    ],
)
def test_rain_stop_rejects_malformed_scheduler_aggregate(malformation) -> None:
    expected_zone_id = 11
    result = _scheduler_stop_result(1, stopped=[expected_zone_id])
    if malformation == "minimal":
        result = {"success": True, "unresolved": []}
    elif malformation == "wrong_group":
        result["group_id"] = 2
    elif malformation == "aggregate_false":
        result["aggregate_valid"] = False
    elif malformation == "unverified":
        result.update({"stopped": [], "unverified_zone_ids": [expected_zone_id]})
    elif malformation == "overlap":
        result["unresolved"] = [expected_zone_id]
    elif malformation == "duplicate":
        result["stopped"] = [expected_zone_id, expected_zone_id]
    elif malformation == "foreign":
        result["stopped"] = [expected_zone_id, 12]
    elif malformation == "missing":
        result["stopped"] = []
    elif malformation == "claimed_retry":
        result["retry_scheduled"] = True
    elif malformation == "bad_id_type":
        result["stopped"] = [str(expected_zone_id)]
    elif malformation == "extra_key":
        result["legacy"] = None

    scheduler = MagicMock()
    scheduler.cancel_group_jobs.return_value = result
    fallback_result = _core_stop_result(1, stopped=[], unresolved=[expected_zone_id])
    with (
        patch("services.monitors.rain_monitor._strict_group_zone_ids", return_value={expected_zone_id}),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch("services.zone_control.stop_all_in_group", return_value=fallback_result) as fallback,
    ):
        assert RainMonitor._cancel_and_stop_group(1) is False

    fallback.assert_called_once_with(
        1,
        reason="rain",
        force=True,
        master_close_immediately=True,
        require_observed_confirmation=True,
    )


@pytest.mark.parametrize(
    "malformation",
    [
        "minimal",
        "wrong_group",
        "overlap",
        "duplicate",
        "foreign",
        "missing",
        "claimed_retry",
        "bad_success_type",
        "bad_partition_type",
        "extra_key",
    ],
)
def test_rain_stop_rejects_malformed_core_fallback_aggregate(malformation) -> None:
    expected_zone_id = 11
    result = _core_stop_result(1, stopped=[expected_zone_id])
    if malformation == "minimal":
        result = {"success": True, "unresolved": []}
    elif malformation == "wrong_group":
        result["group_id"] = 2
    elif malformation == "overlap":
        result["unresolved"] = [expected_zone_id]
    elif malformation == "duplicate":
        result["stopped"] = [expected_zone_id, expected_zone_id]
    elif malformation == "foreign":
        result["stopped"] = [expected_zone_id, 12]
    elif malformation == "missing":
        result["stopped"] = []
    elif malformation == "claimed_retry":
        result["retry_scheduled"] = True
    elif malformation == "bad_success_type":
        result["success"] = 1
    elif malformation == "bad_partition_type":
        result["stopped"] = (expected_zone_id,)
    elif malformation == "extra_key":
        result["legacy"] = None

    with (
        patch("services.monitors.rain_monitor._strict_group_zone_ids", return_value={expected_zone_id}),
        patch("irrigation_scheduler.get_scheduler", return_value=None),
        patch("services.zone_control.stop_all_in_group", return_value=result),
    ):
        assert RainMonitor._cancel_and_stop_group(1) is False


def test_rain_stop_accepts_exact_core_fallback_with_observed_confirmation() -> None:
    expected_zone_id = 11
    with (
        patch("services.monitors.rain_monitor._strict_group_zone_ids", return_value={expected_zone_id}),
        patch("irrigation_scheduler.get_scheduler", return_value=None),
        patch(
            "services.zone_control.stop_all_in_group",
            return_value=_core_stop_result(1, stopped=[expected_zone_id]),
        ) as fallback,
    ):
        assert RainMonitor._cancel_and_stop_group(1) is True

    fallback.assert_called_once_with(
        1,
        reason="rain",
        force=True,
        master_close_immediately=True,
        require_observed_confirmation=True,
    )


def test_rain_stop_does_not_accept_broker_ack_without_fresh_observed_off() -> None:
    expected_zone_id = 11
    unresolved = _core_stop_result(1, stopped=[], unresolved=[expected_zone_id])
    with (
        patch("services.monitors.rain_monitor._strict_group_zone_ids", return_value={expected_zone_id}),
        patch("irrigation_scheduler.get_scheduler", return_value=None),
        patch("services.zone_control.stop_all_in_group", return_value=unresolved) as fallback,
    ):
        assert RainMonitor._cancel_and_stop_group(1) is False

    assert fallback.call_args.kwargs["require_observed_confirmation"] is True


def test_rain_stop_snapshot_failure_is_fail_closed_before_any_attempt() -> None:
    scheduler = MagicMock()
    with (
        patch(
            "services.monitors.rain_monitor._strict_group_zone_ids",
            side_effect=sqlite3.OperationalError("snapshot failed"),
        ),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch("services.zone_control.stop_all_in_group") as fallback,
    ):
        assert RainMonitor._cancel_and_stop_group(1) is False

    scheduler.cancel_group_jobs.assert_not_called()
    fallback.assert_not_called()


def test_failed_physical_rain_stop_is_retried_on_repeated_payload() -> None:
    expected_zone_id = 11
    monitor = RainMonitor()
    monitor._persisted_state_loaded = True
    monitor._cfg = {"enabled": True, "topic": "/rain", "server_id": 1, "type": "NO"}
    scheduler = MagicMock()
    scheduler.cancel_group_jobs.return_value = _scheduler_stop_result(
        1,
        stopped=[],
        unresolved=[expected_zone_id],
        retry_scheduled=True,
    )
    fallback_result = _core_stop_result(1, stopped=[], unresolved=[expected_zone_id])

    with (
        patch("services.monitors.rain_monitor._strict_target_groups", return_value=[1]),
        patch("services.monitors.rain_monitor._strict_group_zone_ids", return_value={expected_zone_id}),
        patch.object(monitor, "_persist_rain_active", return_value=True),
        patch("services.monitors.rain_monitor._apply_rain_postpone_deadline", return_value=True),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch("services.zone_control.stop_all_in_group", return_value=fallback_result) as fallback,
    ):
        assert monitor._handle_payload("1") is False
        assert monitor._handle_payload("1") is False

    assert scheduler.cancel_group_jobs.call_count == 2
    assert fallback.call_count == 2
    assert monitor._last_sensor_value is None


def test_failed_rain_enforcement_does_not_deduplicate_same_payload() -> None:
    monitor = RainMonitor()
    monitor._cfg = {"enabled": True, "topic": "/rain", "server_id": 1, "type": "NO"}

    with patch.object(monitor, "_on_rain_start", return_value=False) as start:
        assert monitor._handle_payload("1") is False
        assert monitor._handle_payload("1") is False

    assert start.call_count == 2
    assert monitor._last_sensor_value is None


def test_corrupt_persisted_rain_state_is_unknown_and_recovers_on_fresh_dry(test_db) -> None:
    assert test_db.set_group_use_rain(1, True)
    server = test_db.create_mqtt_server({"name": "rain-corrupt-state", "host": "127.0.0.1", "port": 1883, "enabled": 1})
    assert server is not None
    assert test_db.set_rain_config({"enabled": True, "topic": "/rain", "server_id": int(server["id"]), "type": "NO"})
    assert test_db.set_setting_value("rain.active", "corrupt")
    monitor = RainMonitor()

    with patch("services.monitors.rain_monitor.db", test_db):
        assert monitor.is_group_blocked(1) is True
        assert monitor.is_rain is None
        assert monitor._handle_payload("0") is True

    assert test_db.get_setting_value("rain.active") == "0"
    assert monitor.is_rain is False
    assert monitor.is_group_blocked(1) is False


def test_enforce_group_claims_rain_postpone_and_stops_immediately(test_db) -> None:
    zone = test_db.create_zone({"name": "newly-protected", "duration": 10, "group_id": 1})
    assert test_db.set_group_use_rain(1, True)
    monitor = RainMonitor()
    monitor._persisted_state_loaded = True
    monitor._cfg = {"enabled": True, "topic": "/rain", "server_id": 1, "type": "NO"}
    monitor.is_rain = True
    scheduler = MagicMock()
    scheduler.cancel_group_jobs.return_value = _scheduler_stop_result(1, stopped=[zone["id"]])

    with (
        patch("services.monitors.rain_monitor.db", test_db),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        assert monitor.enforce_group(1) is True

    assert monitor.is_group_blocked(1) is True
    assert test_db.get_zone(zone["id"])["postpone_reason"] == "rain"
    scheduler.cancel_group_jobs.assert_called_once_with(1, master_close_immediately=True)


def test_unknown_rain_state_stops_protected_group_and_keeps_gate_closed(test_db) -> None:
    assert test_db.set_group_use_rain(1, True)
    zone = test_db.create_zone({"name": "unknown-rain", "duration": 10, "group_id": 1})
    monitor = RainMonitor()
    monitor._persisted_state_loaded = True
    monitor._cfg = {"enabled": True, "topic": "/rain", "server_id": 1, "type": "NO"}
    monitor.sensor_online = True
    monitor.is_rain = False
    scheduler = MagicMock()
    scheduler.cancel_group_jobs.return_value = _scheduler_stop_result(1, stopped=[zone["id"]])

    with (
        patch("services.monitors.rain_monitor.db", test_db),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        monitor._enter_unknown_state(online=False)

    assert monitor.sensor_online is False
    assert monitor.is_rain is None
    assert monitor.is_group_blocked(1) is True
    scheduler.cancel_group_jobs.assert_called_once_with(1, master_close_immediately=True)


@pytest.mark.parametrize(
    ("factor_key", "weather", "factor_name"),
    [
        ("weather.factor.rain", _weather(precipitation_24h=9.0), "rain"),
        ("weather.factor.freeze", _weather(temperature=-3.0), "freeze"),
        ("weather.factor.wind", _weather(wind_speed=8.0), "wind"),
    ],
)
def test_hard_safety_is_not_disableable_by_soft_factor_toggle(test_db, factor_key, weather, factor_name) -> None:
    adj = _enable_weather(test_db)
    assert test_db.set_setting_value(factor_key, "0")

    skip = adj.should_skip(weather=weather)
    coefficient = adj.get_coefficient(weather=weather)
    factor = adj.get_factors_detail(weather)[factor_name]

    assert skip["skip"] is True
    assert coefficient == 0
    assert factor["enabled"] is False
    assert factor["status"] == "danger"


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("temperature", float("nan")),
        ("temperature", float("inf")),
        ("temperature", "1e999"),
        ("precipitation_24h", float("nan")),
        ("precipitation_24h", float("inf")),
        ("precipitation_24h", -0.1),
        ("precipitation_forecast_6h", float("nan")),
        ("precipitation_forecast_6h", float("inf")),
        ("precipitation_forecast_6h", -0.1),
        ("min_temp_forecast_6h", float("nan")),
        ("min_temp_forecast_6h", float("inf")),
        ("wind_speed", float("nan")),
        ("wind_speed", float("inf")),
        ("wind_speed", -0.1),
    ],
)
def test_nonfinite_or_negative_safety_input_is_fail_closed_unavailable(test_db, field, invalid) -> None:
    adj = _enable_weather(test_db)
    weather = _weather(**{field: invalid})

    decision = adj.should_skip(weather=weather)

    assert decision["skip"] is True
    assert decision["details"]["type"] == "weather_unavailable"
    assert decision["details"]["field"] == field
    assert adj.get_coefficient(weather=weather) == 0


@pytest.mark.parametrize(
    ("setting", "invalid", "weather"),
    [
        ("weather.rain_threshold_mm", "nan", _weather(precipitation_24h=50.0)),
        ("weather.rain_threshold_mm", "inf", _weather(precipitation_24h=50.0)),
        ("weather.freeze_threshold_c", "-inf", _weather(temperature=-5.0)),
        ("weather.wind_threshold_ms", "1e999", _weather(wind_speed=50.0)),
    ],
)
def test_nonfinite_database_safety_threshold_cannot_bypass_skip(test_db, setting, invalid, weather) -> None:
    adj = _enable_weather(test_db)
    assert test_db.set_setting_value(setting, invalid)

    decision = adj.should_skip(weather=weather)

    assert decision["skip"] is True
    assert decision["details"]["type"] == "weather_unavailable"
    assert adj.get_coefficient(weather=weather) == 0


@pytest.mark.parametrize(
    ("soft", "hard", "expected_field"),
    [("nan", "10", "sensor_mismatch_soft_c"), ("10", "5", "sensor_mismatch_window")],
)
def test_invalid_sensor_mismatch_window_is_fail_closed(test_db, soft, hard, expected_field) -> None:
    adj = _enable_weather(test_db)
    assert test_db.set_setting_value("weather.sensor_mismatch_soft_c", soft)
    assert test_db.set_setting_value("weather.sensor_mismatch_hard_c", hard)

    decision = adj.should_skip(weather=_weather())

    assert decision["skip"] is True
    assert decision["details"]["field"] == expected_field
    assert adj.get_coefficient(weather=_weather()) == 0


def test_nan_current_temperature_with_freezing_forecast_stays_fail_closed(test_db) -> None:
    adj = _enable_weather(test_db)
    weather = _weather(temperature=float("nan"), min_temp_forecast_6h=-5.0)

    decision = adj.should_skip(weather=weather)

    assert decision["skip"] is True
    assert decision["details"] == {
        "type": "weather_unavailable",
        "api_unavailable": False,
        "field": "temperature",
    }
    assert adj.get_coefficient(weather=weather) == 0


@pytest.mark.parametrize("invalid", [-1.0, float("nan"), float("inf"), "1e999"])
def test_invalid_precipitation_forecast_member_invalidates_the_whole_window(test_db, invalid) -> None:
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    raw = _raw_weather_window(now, future_precipitation=[invalid, 0.0, 0.0, 0.0, 0.0, 0.0])
    with patch("services.weather.models.time.time", return_value=now.timestamp()):
        weather = WeatherData(raw)

    assert weather.precipitation_forecast_6h is None
    decision = _enable_weather(test_db).should_skip(weather=weather)
    assert decision["skip"] is True
    assert decision["details"]["field"] == "precipitation_forecast_6h"


def test_invalid_temperature_member_invalidates_freeze_window(test_db) -> None:
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    raw = _raw_weather_window(
        now,
        current_temperature=float("nan"),
        future_temperatures=[-5.0, 10.0, 11.0, 12.0, 13.0, 14.0],
    )
    with patch("services.weather.models.time.time", return_value=now.timestamp()):
        weather = WeatherData(raw)

    assert weather.temperature is None
    assert weather.min_temp_forecast_6h is None
    decision = _enable_weather(test_db).should_skip(weather=weather)
    assert decision["skip"] is True
    assert decision["details"]["type"] == "weather_unavailable"


def test_cache_only_miss_is_non_suppressing_unknown_and_does_not_alert(test_db) -> None:
    from services.next_watering import _weather_duration_coefficient, weather_skip_today

    adj = _enable_weather(test_db)
    service = MagicMock()
    service.get_weather.return_value = None
    with (
        patch("services.weather.adjustment.get_weather_service", return_value=service),
        patch.object(adj, "_maybe_alert_api_down") as alert,
    ):
        decision = adj.should_skip(cache_only=True)
        with patch("services.weather_adjustment.get_weather_adjustment", return_value=adj):
            assert weather_skip_today() is False

    assert decision["skip"] is False
    assert decision["details"]["unknown"] is True
    assert decision["details"]["display_only"] is True
    assert service.get_weather.call_count == 2
    assert all(item.kwargs == {"cache_only": True} for item in service.get_weather.call_args_list)
    alert.assert_not_called()

    with (
        patch("services.next_watering.db", test_db),
        patch("services.weather.service.WeatherService.get_weather", return_value=None) as cache_read,
    ):
        assert _weather_duration_coefficient() == 100
    cache_read.assert_called_once_with(cache_only=True)


def test_disabled_heat_factor_is_neutral_even_during_extreme_heat(test_db) -> None:
    adj = _enable_weather(test_db)
    assert test_db.set_setting_value("weather.factor.heat", "0")

    heat = adj.get_factors_detail(_weather(temperature=40.0))["heat"]

    assert heat == {"status": "ok", "detail": "фактор отключён", "enabled": False}


def test_extended_weather_decisions_share_one_effective_snapshot(test_db) -> None:
    raw = _weather()
    raw.timestamp = datetime.now(tz=UTC).timestamp()
    raw.weather_code = 0
    raw.hourly_forecast_24h = []
    raw.daily_forecast = []
    raw.sunrise = None
    raw.sunset = None
    effective = _weather(temperature=-4.0)
    effective.timestamp = raw.timestamp
    effective.weather_code = raw.weather_code
    effective.hourly_forecast_24h = raw.hourly_forecast_24h
    effective.daily_forecast = raw.daily_forecast
    effective.sunrise = raw.sunrise
    effective.sunset = raw.sunset
    effective.temperature_source = "local"
    effective.humidity_source = "api"
    service = WeatherService(test_db.db_path)

    with (
        patch.object(service, "get_weather", return_value=raw) as get_weather,
        patch.object(WeatherAdjustment, "_select_input_source", autospec=True, return_value=effective) as select,
        patch.object(WeatherAdjustment, "get_coefficient", autospec=True, return_value=0) as coefficient,
        patch.object(
            WeatherAdjustment,
            "should_skip",
            autospec=True,
            return_value={"skip": True, "reason": "freeze", "details": {"type": "freeze"}},
        ) as should_skip,
        patch.object(WeatherAdjustment, "get_factors_detail", autospec=True, return_value={}) as factors,
    ):
        payload = service.get_weather_extended()

    get_weather.assert_called_once_with()
    select.assert_called_once()
    assert coefficient.call_args.kwargs["weather"] is effective
    assert should_skip.call_args.kwargs["weather"] is effective
    assert factors.call_args.args[1] is effective
    assert payload["temperature"] == -4.0
    assert payload["adjustment"]["coefficient"] == 0
    assert payload["adjustment"]["skip"] is True
