"""Regression coverage for the documented merged-weather compatibility API."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from services.weather import get_merged_weather as package_get_merged_weather
from services.weather_merged import get_merged_weather as legacy_get_merged_weather


def _assert_isolated_import(script: str) -> None:
    project_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def _extended_payload() -> dict:
    return {
        "available": True,
        "current": {
            "temperature": {"value": 18.0, "source": "api", "unit": "°C"},
            "humidity": {"value": 55.0, "source": "api", "unit": "%"},
            "rain": {"value": False, "source": "api"},
            "wind_speed": {"value": 4.0, "source": "api", "unit": "м/с"},
            "precipitation_mm": {"value": 0.0, "source": "api", "unit": "мм"},
            "weather_code": 2,
        },
        "stats": {
            "precipitation_24h": 1.2,
            "precipitation_forecast_6h": 0.3,
            "daily_et0": 2.8,
        },
        "forecast_24h": [{"time": "16:00", "temp": 18}],
        "forecast_3d": [{"date": "2026-07-18", "temp_min": 10, "temp_max": 22}],
        "astronomy": {"sunrise": "05:30", "sunset": "20:15"},
        "timestamp": 1234.0,
        "cache_age_sec": 42.0,
    }


def _env_state(*, online: bool) -> dict:
    return {
        "temp_enabled": True,
        "temp_value": 23.5,
        "temp_last_rx": 1200.0,
        "temp_online": online,
        "hum_enabled": True,
        "hum_value": 70.0,
        "hum_last_rx": 1201.0,
        "hum_online": online,
    }


def test_package_and_legacy_module_export_same_stable_callable() -> None:
    assert package_get_merged_weather is legacy_get_merged_weather


def test_isolated_legacy_import_before_package_import() -> None:
    _assert_isolated_import(
        "from services.weather_merged import SENSOR_STALE_TIMEOUT, get_merged_weather as legacy; "
        "from services.weather import get_merged_weather as package; "
        "assert legacy is package; assert SENSOR_STALE_TIMEOUT == 600"
    )


def test_isolated_package_import_before_legacy_import() -> None:
    _assert_isolated_import(
        "from services.weather import get_merged_weather as package; "
        "from services.weather_merged import get_merged_weather as legacy; "
        "assert package is legacy"
    )


@patch("services.weather_merged.time.time", return_value=1300.0)
@patch("services.weather_merged._get_rain_state", return_value={"enabled": True, "is_rain": True})
@patch("services.weather_merged._get_env_state", return_value=_env_state(online=True))
@patch("services.weather_merged.get_weather_service")
def test_merged_weather_prefers_fresh_local_sensors(
    get_service,
    _get_env,
    _get_rain,
    _time,
) -> None:
    get_service.return_value.get_weather_extended.return_value = _extended_payload()

    result = legacy_get_merged_weather("irrigation.db")

    assert result["temperature"] == {"value": 23.5, "source": "local", "unit": "°C"}
    assert result["humidity"] == {"value": 70.0, "source": "local", "unit": "%"}
    assert result["rain"] == {"value": True, "source": "local"}
    assert result["wind_speed"]["source"] == "api"
    assert result["forecast_24h"] == [{"time": "16:00", "temp": 18}]
    assert result["sensors"]["rain"] == {
        "enabled": True,
        "online": True,
        "value": True,
        "state": "rain",
    }
    assert result["timestamp"] == 1300.0


@patch("services.weather_merged.time.time", return_value=1300.0)
@patch("services.weather_merged._get_rain_state", return_value={"enabled": True, "is_rain": None})
@patch("services.weather_merged._get_env_state", return_value=_env_state(online=False))
@patch("services.weather_merged.get_weather_service")
def test_merged_weather_keeps_enabled_offline_rain_unknown(
    get_service,
    _get_env,
    _get_rain,
    _time,
) -> None:
    get_service.return_value.get_weather_extended.return_value = _extended_payload()

    result = package_get_merged_weather("irrigation.db")

    assert result["temperature"] == {"value": 18.0, "source": "api_fallback", "unit": "°C"}
    assert result["humidity"] == {"value": 55.0, "source": "api_fallback", "unit": "%"}
    assert result["rain"] == {"value": None, "source": "local", "state": "offline"}
    assert result["sensors"]["temperature"]["online"] is False
    assert result["sensors"]["rain"]["online"] is False
    assert result["sensors"]["rain"]["state"] == "offline"


@patch("services.weather_merged.get_weather_service")
def test_merged_weather_preserves_unavailable_shape(get_service) -> None:
    get_service.return_value.get_weather_extended.return_value = {"available": False}

    assert legacy_get_merged_weather("irrigation.db") == {"available": False}
