"""Weather service package — Open-Meteo integration, adjustment engine, merge.

Public API (stable — callers MUST use these names):
    WeatherData           — parsed API response (dataclass-like container)
    WeatherService        — HTTP client + SQLite cache orchestrator
    WeatherAdjustment     — Zimmerman + ET₀ watering-coefficient engine
    get_weather_service() — module-level singleton accessor
    get_weather_adjustment() — module-level singleton accessor
    get_merged_weather()  — merges local MQTT sensors with API data
    SENSOR_STALE_TIMEOUT  — staleness threshold for local sensor data (seconds)

Package layout (v2, Wave 4 refactor — in progress):
    _legacy.py    — original monolithic implementation (being split)
    models.py     — WeatherData parser + module constants            [planned]
    client.py     — Open-Meteo HTTP fetch                            [planned]
    cache.py      — SQLite weather_cache read/write                  [planned]
    service.py    — WeatherService orchestrator                      [planned]
    adjustment.py — WeatherAdjustment (Zimmerman + ET₀)              [planned]
    merge.py      — get_merged_weather + _merge_*/_build_*/_get_*    [planned]
    singletons.py — process-wide service/adjustment cache            [planned]

Private helpers (``_merge_*`` / ``_build_*`` / ``_get_*``) are re-exported
for backward compatibility with ``services.weather_merged`` shim and
``tests/unit/test_weather_*`` which patch them at the package level.
"""
from services.weather._legacy import (  # noqa: F401
    WeatherData,
    WeatherService,
    WeatherAdjustment,
    get_weather_service,
    get_weather_adjustment,
    get_merged_weather,
    SENSOR_STALE_TIMEOUT,
)
# Private helpers re-exported for backward compatibility
# (services.weather_merged shim + test patches target these names).
from services.weather._legacy import (  # noqa: F401
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

__all__ = [
    'WeatherData',
    'WeatherService',
    'WeatherAdjustment',
    'get_weather_service',
    'get_weather_adjustment',
    'get_merged_weather',
    'SENSOR_STALE_TIMEOUT',
]
