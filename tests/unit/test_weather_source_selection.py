"""Tests for weather input-source selection (Горизонт 1).

Covers ``WeatherAdjustment.evaluate_sensor_source`` / ``_apply_source`` /
``_select_input_source``: local WB-MSW sensor priority over Open-Meteo,
sanity + freshness gating, and temp mismatch detection (soft/hard) with
Open-Meteo fallback on hard mismatch.
"""

import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ["TESTING"] = "1"

from services.weather_adjustment import WeatherAdjustment

SETTINGS = {"sensor_mismatch_soft_c": 5.0, "sensor_mismatch_hard_c": 10.0}


def _env(
    temp_enabled=True,
    temp_value=23.0,
    temp_online=True,
    hum_enabled=True,
    hum_value=55.0,
    hum_online=True,
):
    """Build an env state dict as returned by merge._get_env_state."""
    return {
        "temp_enabled": temp_enabled,
        "temp_value": temp_value,
        "temp_last_rx": 0.0,
        "temp_online": temp_online,
        "hum_enabled": hum_enabled,
        "hum_value": hum_value,
        "hum_last_rx": 0.0,
        "hum_online": hum_online,
    }


def _api(temperature=24.0, humidity=50.0, min_temp_forecast_6h=24.0):
    return SimpleNamespace(
        temperature=temperature,
        humidity=humidity,
        min_temp_forecast_6h=min_temp_forecast_6h,
    )


def _adj():
    return WeatherAdjustment("/tmp/source_sel_test.db")


def _evaluate(adj, env, api):
    with (
        patch.object(WeatherAdjustment, "_get_settings", return_value=dict(SETTINGS)),
        patch("services.weather.merge._get_env_state", return_value=env),
    ):
        return adj.evaluate_sensor_source(api)


class TestSourceSelection:
    def test_local_priority_when_fresh_and_close(self):
        """Fresh sane sensor close to API → use local, no mismatch."""
        v = _evaluate(_adj(), _env(temp_value=23, hum_value=55), _api(temperature=24))
        assert v["temp_source"] == "local"
        assert v["temperature"] == 23
        assert v["hum_source"] == "local"
        assert v["humidity"] == 55
        assert v["mismatch"] is None

    def test_api_fallback_when_sensor_offline(self):
        """Enabled but stale sensor → api_fallback with API value."""
        v = _evaluate(_adj(), _env(temp_online=False, hum_online=False), _api(temperature=24, humidity=50))
        assert v["temp_source"] == "api_fallback"
        assert v["temperature"] == 24
        assert v["hum_source"] == "api_fallback"
        assert v["humidity"] == 50

    def test_api_when_local_value_none(self):
        """Online sensor but no value yet → API."""
        v = _evaluate(_adj(), _env(temp_value=None), _api(temperature=24))
        assert v["temp_source"] == "api"
        assert v["temperature"] == 24

    def test_insane_local_value_rejected(self):
        """Out-of-range sensor reading is ignored → API."""
        v = _evaluate(_adj(), _env(temp_value=999.0), _api(temperature=24))
        assert v["temp_source"] == "api"
        assert v["temperature"] == 24

    def test_soft_mismatch_keeps_local(self):
        """5 < delta <= 10 → keep local but flag soft."""
        v = _evaluate(_adj(), _env(temp_value=20.0), _api(temperature=13.0))  # delta 7
        assert v["temp_source"] == "local"
        assert v["temperature"] == 20.0
        assert v["mismatch"]["level"] == "soft"
        assert v["mismatch"]["delta"] == 7.0

    def test_hard_mismatch_uses_colder_api_and_falls_back_for_humidity(self):
        """delta > 10 uses the colder API temp and API humidity."""
        v = _evaluate(_adj(), _env(temp_value=30.0, hum_value=80.0), _api(temperature=15.0, humidity=50.0))
        assert v["temp_source"] == "api_fallback"
        assert v["temperature"] == 15.0
        assert v["mismatch"]["level"] == "hard"
        assert v["mismatch"]["delta"] == 15.0
        # hard temp mismatch distrusts the whole sensor module
        assert v["hum_source"] == "api_fallback"
        assert v["humidity"] == 50.0

    def test_humidity_local_priority_independent(self):
        """No temp issue → humidity uses local sensor."""
        v = _evaluate(_adj(), _env(temp_value=24.0, hum_value=70.0), _api(temperature=24.0, humidity=40.0))
        assert v["hum_source"] == "local"
        assert v["humidity"] == 70

    def test_insane_humidity_rejected(self):
        v = _evaluate(_adj(), _env(hum_value=150.0), _api(humidity=45.0))
        assert v["hum_source"] == "api"
        assert v["humidity"] == 45.0

    def test_local_used_when_api_temp_missing(self):
        """API temp None/garbage → can't compare; valid local is used, no mismatch (M1)."""
        v = _evaluate(_adj(), _env(temp_value=22.0), _api(temperature=None))
        assert v["temp_source"] == "local"
        assert v["temperature"] == 22.0
        assert v["mismatch"] is None


class TestApplySource:
    def test_apply_overrides_temp_hum_preserves_forecast(self):
        """_apply_source swaps temp/hum but keeps min_temp_forecast_6h (freeze-by-min)."""
        weather = _api(temperature=20.0, humidity=50.0, min_temp_forecast_6h=-1.0)
        adj = _adj()
        eff = adj._apply_source(weather, {"temperature": 21.0, "humidity": 60.0})
        assert eff.temperature == 21.0
        assert eff.humidity == 60.0
        # forecast minimum stays from the API → freeze still triggers on it
        assert eff.min_temp_forecast_6h == -1.0
        # original object not mutated
        assert weather.temperature == 20.0

    def test_select_input_source_replaces_local(self):
        """End-to-end: fresh local sensor flows through into the weather view."""
        weather = _api(temperature=24.0, humidity=50.0, min_temp_forecast_6h=24.0)
        adj = _adj()
        with (
            patch.object(WeatherAdjustment, "_get_settings", return_value=dict(SETTINGS)),
            patch("services.weather.merge._get_env_state", return_value=_env(temp_value=21.0, hum_value=58.0)),
        ):
            eff = adj._select_input_source(weather)
        assert eff.temperature == 21.0
        assert eff.humidity == 58
        assert eff.min_temp_forecast_6h == 24.0
