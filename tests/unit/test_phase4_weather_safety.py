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


def test_hard_freeze_safety_ignores_the_factor_toggle(test_db) -> None:
    adj = _enable_weather(test_db)
    assert test_db.set_setting_value("weather.factor.freeze", "0")

    with patch.object(adj, "_get_weather", return_value=_weather(temperature=-3.0)):
        assert adj.get_coefficient() == 0


@pytest.mark.parametrize(
    ("weather", "factor", "expected_text"),
    [
        (_weather(precipitation_forecast_6h=5.0), "rain", "прогноз"),
        (_weather(temperature=None, min_temp_forecast_6h=2.0), "freeze", "прогноз"),
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
        raw_sources = conn.execute(
            "SELECT data_sources FROM weather_decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
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
            "SELECT decision, coefficient, reason, data_sources "
            "FROM weather_decisions ORDER BY id DESC LIMIT 1"
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
    assert monitor.is_rain is True


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
        patch("services.zone_control.stop_all_in_group", return_value=True),
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
    scheduler.cancel_group_jobs.return_value = {"success": True, "stopped": [1], "unresolved": []}
    assert test_db.set_group_use_rain(1, True)

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
