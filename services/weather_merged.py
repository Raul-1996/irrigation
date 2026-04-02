"""Backward-compatibility stub — re-exports from consolidated services.weather module.

All functionality has been moved to services.weather.
This file exists only so that existing imports and test patches continue to work.
"""
from services.weather import (  # noqa: F401
    SENSOR_STALE_TIMEOUT,
    _merge_temperature,
    _merge_humidity,
    _merge_rain,
    _build_sensor_status,
    _build_forecast_24h,
    _build_forecast_3d,
    _build_astronomy,
    _get_weather_code,
    _get_rain_state,
    _get_env_state,
    _get_api_weather,
)

import time


def get_merged_weather(db_path):
    """Wrapper that calls module-level helpers so test patches on
    ``services.weather_merged._get_api_weather`` etc. are honoured."""
    now = time.time()

    api_weather = _get_api_weather(db_path)
    if api_weather is None:
        return {"available": False}

    env_state = _get_env_state(now)
    rain_state = _get_rain_state()

    temp_result = _merge_temperature(api_weather, env_state, now)
    hum_result = _merge_humidity(api_weather, env_state, now)
    rain_result = _merge_rain(api_weather, rain_state)

    wind_result = {
        "value": api_weather.wind_speed,
        "source": "api",
        "unit": "km/h",
    }

    precip_result = {
        "value": api_weather.precipitation,
        "source": "api",
        "unit": "mm",
    }

    forecast_24h = _build_forecast_24h(api_weather)
    forecast_3d = _build_forecast_3d(api_weather)
    astronomy = _build_astronomy(api_weather)
    sensors = _build_sensor_status(env_state, rain_state)
    cache_age = now - api_weather.timestamp if api_weather.timestamp else 0

    return {
        "available": True,
        "temperature": temp_result,
        "humidity": hum_result,
        "rain": rain_result,
        "wind_speed": wind_result,
        "precipitation_mm": precip_result,
        "precipitation_24h": api_weather.precipitation_24h or 0.0,
        "precipitation_forecast_6h": api_weather.precipitation_forecast_6h or 0.0,
        "daily_et0": api_weather.daily_et0,
        "weather_code": _get_weather_code(api_weather),
        "forecast_24h": forecast_24h,
        "forecast_3d": forecast_3d,
        "astronomy": astronomy,
        "sensors": sensors,
        "timestamp": now,
        "cache_age_sec": round(cache_age, 1),
    }


__all__ = [
    'SENSOR_STALE_TIMEOUT',
    'get_merged_weather',
    '_merge_temperature',
    '_merge_humidity',
    '_merge_rain',
    '_build_sensor_status',
    '_build_forecast_24h',
    '_build_forecast_3d',
    '_build_astronomy',
    '_get_weather_code',
    '_get_rain_state',
    '_get_env_state',
    '_get_api_weather',
]
