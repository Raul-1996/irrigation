"""Tests for Merged Weather Data (services/weather_merged.py).

Covers: sensor priority, stale fallback, source annotation, offline handling.
Uses mocks to isolate from real MQTT monitors and weather API.
Python 3.9 compatible.
"""
import os
import time
import pytest
from unittest.mock import patch, MagicMock

os.environ['TESTING'] = '1'

from services.weather_merged import (
    SENSOR_STALE_TIMEOUT,
    get_merged_weather,
    _merge_temperature,
    _merge_humidity,
    _merge_rain,
    _build_sensor_status,
    _build_forecast_24h,
    _build_forecast_3d,
    _build_astronomy,
    _get_weather_code,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_api_weather(**overrides):
    """Create a mock WeatherData object."""
    w = MagicMock()
    w.temperature = overrides.get("temperature", 22.0)
    w.humidity = overrides.get("humidity", 65)
    w.precipitation = overrides.get("precipitation", 0.0)
    w.wind_speed = overrides.get("wind_speed", 5.0)
    w.precipitation_24h = overrides.get("precipitation_24h", 1.2)
    w.precipitation_forecast_6h = overrides.get("precipitation_forecast_6h", 0.5)
    w.daily_et0 = overrides.get("daily_et0", 4.2)
    w.timestamp = overrides.get("timestamp", time.time() - 120)
    w.raw = overrides.get("raw", {})
    return w


def _make_env_state(
    temp_enabled=True,
    temp_value=23.5,
    temp_last_rx=None,
    temp_online=True,
    hum_enabled=True,
    hum_value=70,
    hum_last_rx=None,
    hum_online=True,
):
    """Create an env state dict as returned by _get_env_state."""
    now = time.time()
    if temp_last_rx is None:
        temp_last_rx = now - 30 if temp_online else now - 700
    if hum_last_rx is None:
        hum_last_rx = now - 30 if hum_online else now - 700
    return {
        "temp_enabled": temp_enabled,
        "temp_value": temp_value,
        "temp_last_rx": temp_last_rx,
        "temp_online": temp_online,
        "hum_enabled": hum_enabled,
        "hum_value": hum_value,
        "hum_last_rx": hum_last_rx,
        "hum_online": hum_online,
    }


def _make_rain_state(enabled=True, is_rain=False):
    return {"enabled": enabled, "is_rain": is_rain}


# ---------------------------------------------------------------------------
# _merge_temperature
# ---------------------------------------------------------------------------

class TestMergeTemperature:
    def test_local_priority_fresh(self):
        """Local sensor online and fresh -> source='local'."""
        api = _make_api_weather(temperature=20.0)
        env = _make_env_state(temp_online=True, temp_value=23.5)
        result = _merge_temperature(api, env, time.time())
        assert result["value"] == 23.5
        assert result["source"] == "local"
        assert result["unit"] == "°C"

    def test_api_fallback_stale(self):
        """Sensor enabled but stale -> source='api_fallback'."""
        api = _make_api_weather(temperature=20.0)
        env = _make_env_state(
            temp_enabled=True,
            temp_online=False,
            temp_value=23.5,
            temp_last_rx=time.time() - 700,
        )
        result = _merge_temperature(api, env, time.time())
        assert result["value"] == 20.0
        assert result["source"] == "api_fallback"

    def test_api_when_disabled(self):
        """Sensor disabled -> source='api'."""
        api = _make_api_weather(temperature=20.0)
        env = _make_env_state(temp_enabled=False, temp_online=False)
        result = _merge_temperature(api, env, time.time())
        assert result["value"] == 20.0
        assert result["source"] == "api"

    def test_local_none_value_falls_through(self):
        """Sensor online but value is None -> use API."""
        api = _make_api_weather(temperature=20.0)
        env = _make_env_state(temp_online=True, temp_value=None)
        result = _merge_temperature(api, env, time.time())
        assert result["value"] == 20.0
        # source depends on enabled state
        assert result["source"] in ("api", "api_fallback")


# ---------------------------------------------------------------------------
# _merge_humidity
# ---------------------------------------------------------------------------

class TestMergeHumidity:
    def test_local_priority(self):
        api = _make_api_weather(humidity=55)
        env = _make_env_state(hum_online=True, hum_value=70)
        result = _merge_humidity(api, env, time.time())
        assert result["value"] == 70
        assert result["source"] == "local"

    def test_api_fallback_stale(self):
        api = _make_api_weather(humidity=55)
        env = _make_env_state(hum_enabled=True, hum_online=False)
        result = _merge_humidity(api, env, time.time())
        assert result["value"] == 55
        assert result["source"] == "api_fallback"

    def test_api_when_disabled(self):
        api = _make_api_weather(humidity=55)
        env = _make_env_state(hum_enabled=False, hum_online=False)
        result = _merge_humidity(api, env, time.time())
        assert result["value"] == 55
        assert result["source"] == "api"


# ---------------------------------------------------------------------------
# _merge_rain
# ---------------------------------------------------------------------------

class TestMergeRain:
    def test_local_rain_true(self):
        api = _make_api_weather(precipitation=0.0)
        rain = _make_rain_state(enabled=True, is_rain=True)
        result = _merge_rain(api, rain)
        assert result["value"] is True
        assert result["source"] == "local"

    def test_local_rain_false(self):
        api = _make_api_weather(precipitation=5.0)
        rain = _make_rain_state(enabled=True, is_rain=False)
        result = _merge_rain(api, rain)
        assert result["value"] is False
        assert result["source"] == "local"

    def test_api_when_rain_disabled(self):
        api = _make_api_weather(precipitation=2.0)
        rain = _make_rain_state(enabled=False, is_rain=None)
        result = _merge_rain(api, rain)
        assert result["value"] is True  # precipitation > 0
        assert result["source"] == "api"

    def test_api_fallback_rain_none(self):
        """Rain sensor enabled but value is None -> api_fallback."""
        api = _make_api_weather(precipitation=0.0)
        rain = _make_rain_state(enabled=True, is_rain=None)
        result = _merge_rain(api, rain)
        assert result["value"] is False
        assert result["source"] == "api_fallback"

    def test_api_no_precipitation(self):
        api = _make_api_weather(precipitation=0.0)
        rain = _make_rain_state(enabled=False)
        result = _merge_rain(api, rain)
        assert result["value"] is False


# ---------------------------------------------------------------------------
# Wind (always API)
# ---------------------------------------------------------------------------

class TestWindAlwaysApi:
    @patch("services.weather_merged._get_api_weather")
    @patch("services.weather_merged._get_env_state")
    @patch("services.weather_merged._get_rain_state")
    def test_wind_source_api(self, mock_rain, mock_env, mock_api):
        mock_api.return_value = _make_api_weather(wind_speed=7.2)
        mock_env.return_value = _make_env_state()
        mock_rain.return_value = _make_rain_state()
        result = get_merged_weather("/tmp/test.db")
        assert result["available"] is True
        assert result["wind_speed"]["source"] == "api"
        assert result["wind_speed"]["value"] == 7.2


# ---------------------------------------------------------------------------
# Full get_merged_weather
# ---------------------------------------------------------------------------

class TestGetMergedWeather:
    @patch("services.weather_merged._get_api_weather")
    @patch("services.weather_merged._get_env_state")
    @patch("services.weather_merged._get_rain_state")
    def test_available_true(self, mock_rain, mock_env, mock_api):
        mock_api.return_value = _make_api_weather()
        mock_env.return_value = _make_env_state()
        mock_rain.return_value = _make_rain_state()
        result = get_merged_weather("/tmp/test.db")
        assert result["available"] is True
        assert "temperature" in result
        assert "humidity" in result
        assert "rain" in result
        assert "wind_speed" in result
        assert "precipitation_mm" in result
        assert "timestamp" in result
        assert "cache_age_sec" in result

    @patch("services.weather_merged._get_api_weather")
    def test_api_unavailable(self, mock_api):
        mock_api.return_value = None
        result = get_merged_weather("/tmp/test.db")
        assert result["available"] is False

    @patch("services.weather_merged._get_api_weather")
    @patch("services.weather_merged._get_env_state")
    @patch("services.weather_merged._get_rain_state")
    def test_precipitation_fields(self, mock_rain, mock_env, mock_api):
        mock_api.return_value = _make_api_weather(
            precipitation_24h=3.5,
            precipitation_forecast_6h=1.0,
            daily_et0=5.0,
        )
        mock_env.return_value = _make_env_state()
        mock_rain.return_value = _make_rain_state()
        result = get_merged_weather("/tmp/test.db")
        assert result["precipitation_24h"] == 3.5
        assert result["precipitation_forecast_6h"] == 1.0
        assert result["daily_et0"] == 5.0

    @patch("services.weather_merged._get_api_weather")
    @patch("services.weather_merged._get_env_state")
    @patch("services.weather_merged._get_rain_state")
    def test_local_temp_in_full_result(self, mock_rain, mock_env, mock_api):
        mock_api.return_value = _make_api_weather(temperature=18.0)
        mock_env.return_value = _make_env_state(temp_online=True, temp_value=25.0)
        mock_rain.return_value = _make_rain_state()
        result = get_merged_weather("/tmp/test.db")
        assert result["temperature"]["value"] == 25.0
        assert result["temperature"]["source"] == "local"


# ---------------------------------------------------------------------------
# Sensor status
# ---------------------------------------------------------------------------

class TestBuildSensorStatus:
    def test_all_online(self):
        env = _make_env_state(temp_online=True, hum_online=True)
        rain = _make_rain_state(enabled=True, is_rain=False)
        result = _build_sensor_status(env, rain)
        assert result["temperature"]["online"] is True
        assert result["humidity"]["online"] is True
        assert result["rain"]["enabled"] is True

    def test_all_offline(self):
        env = _make_env_state(
            temp_enabled=False, temp_online=False,
            hum_enabled=False, hum_online=False,
        )
        rain = _make_rain_state(enabled=False)
        result = _build_sensor_status(env, rain)
        assert result["temperature"]["online"] is False
        assert result["humidity"]["online"] is False
        assert result["rain"]["enabled"] is False


# ---------------------------------------------------------------------------
# Forecast builders
# ---------------------------------------------------------------------------

class TestForecast24h:
    def test_empty_raw(self):
        api = _make_api_weather(raw={})
        result = _build_forecast_24h(api)
        assert result == []

    def test_no_hourly(self):
        api = _make_api_weather(raw={"hourly": {}})
        result = _build_forecast_24h(api)
        assert result == []

    def test_with_data(self):
        """Build forecast from hourly data."""
        from datetime import datetime, timedelta
        now = datetime.now()
        times = []
        temps = []
        precips = []
        winds = []
        codes = []
        for i in range(48):
            t = now + timedelta(hours=i)
            times.append(t.strftime("%Y-%m-%dT%H:00"))
            temps.append(20.0 + i * 0.1)
            precips.append(0.0)
            winds.append(3.0)
            codes.append(2)

        raw = {
            "hourly": {
                "time": times,
                "temperature_2m": temps,
                "precipitation": precips,
                "wind_speed_10m": winds,
                "weather_code": codes,
            }
        }
        api = _make_api_weather(raw=raw)
        result = _build_forecast_24h(api)
        assert len(result) <= 6
        for entry in result:
            assert "time" in entry
            assert "temp" in entry
            assert "precip" in entry


class TestForecast3d:
    def test_empty_raw(self):
        api = _make_api_weather(raw={})
        result = _build_forecast_3d(api)
        assert result == []

    def test_with_data(self):
        raw = {
            "daily": {
                "time": ["2026-03-29", "2026-03-30", "2026-03-31"],
                "precipitation_sum": [1.2, 0.0, 3.5],
                "et0_fao_evapotranspiration": [3.0, 4.0, 2.5],
                "temperature_2m_max": [10, 12, 8],
                "temperature_2m_min": [2, 3, 1],
                "weather_code": [2, 0, 63],
            }
        }
        api = _make_api_weather(raw=raw)
        result = _build_forecast_3d(api)
        assert len(result) == 3
        assert result[0]["date"] == "2026-03-29"
        assert result[0]["temp_max"] == 10
        assert result[0]["temp_min"] == 2
        assert result[0]["precip_sum"] == 1.2
        assert result[2]["weather_code"] == 63


# ---------------------------------------------------------------------------
# Astronomy
# ---------------------------------------------------------------------------

class TestAstronomy:
    def test_empty_raw(self):
        api = _make_api_weather(raw={})
        result = _build_astronomy(api)
        assert result["sunrise"] is None
        assert result["sunset"] is None

    def test_with_data(self):
        raw = {
            "daily": {
                "sunrise": ["2026-03-29T06:28"],
                "sunset": ["2026-03-29T19:15"],
            }
        }
        api = _make_api_weather(raw=raw)
        result = _build_astronomy(api)
        assert result["sunrise"] == "06:28"
        assert result["sunset"] == "19:15"

    def test_no_t_separator(self):
        raw = {
            "daily": {
                "sunrise": ["06:28"],
                "sunset": ["19:15"],
            }
        }
        api = _make_api_weather(raw=raw)
        result = _build_astronomy(api)
        assert result["sunrise"] == "06:28"
        assert result["sunset"] == "19:15"


# ---------------------------------------------------------------------------
# Weather code extraction
# ---------------------------------------------------------------------------

class TestGetWeatherCode:
    def test_no_raw(self):
        api = _make_api_weather(raw={})
        result = _get_weather_code(api)
        assert result is None

    def test_with_code(self):
        from datetime import datetime
        current_hour = datetime.now().strftime("%Y-%m-%dT%H:00")
        raw = {
            "hourly": {
                "time": [current_hour],
                "weather_code": [61],
            }
        }
        api = _make_api_weather(raw=raw)
        result = _get_weather_code(api)
        assert result == 61

    def test_fallback_to_first(self):
        """If current hour not found, returns first entry."""
        raw = {
            "hourly": {
                "time": ["2020-01-01T00:00"],
                "weather_code": [3],
            }
        }
        api = _make_api_weather(raw=raw)
        result = _get_weather_code(api)
        assert result == 3

    def test_none_code(self):
        """None weather_code entry -> None."""
        raw = {
            "hourly": {
                "time": ["2020-01-01T00:00"],
                "weather_code": [None],
            }
        }
        api = _make_api_weather(raw=raw)
        result = _get_weather_code(api)
        assert result is None


# ---------------------------------------------------------------------------
# SENSOR_STALE_TIMEOUT constant
# ---------------------------------------------------------------------------

class TestConstants:
    def test_stale_timeout(self):
        assert SENSOR_STALE_TIMEOUT == 600
