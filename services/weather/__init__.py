"""Weather service package — Open-Meteo integration, adjustment engine, merge.

Public API (stable — callers MUST use these names):
    WeatherData           — parsed API response (dataclass-like container)
    WeatherService        — HTTP client + SQLite cache orchestrator
    WeatherAdjustment     — Zimmerman + ET₀ watering-coefficient engine
    get_weather_service() — module-level singleton accessor
    get_weather_adjustment() — module-level singleton accessor
    get_merged_weather()  — merges local MQTT sensors with API data
    SENSOR_STALE_TIMEOUT  — staleness threshold for local sensor data (seconds)

Package layout (Wave 4 refactor):
    models.py     — WeatherData parser + module constants
    client.py     — Open-Meteo HTTP fetch (requests + urllib fallback)
    cache.py      — SQLite weather_cache read/write/stale-fallback
    service.py    — WeatherService orchestrator
    adjustment.py — WeatherAdjustment (Zimmerman + ET₀ + skip rules)
    merge.py      — get_merged_weather + _merge_*/_build_*/_get_* helpers
    singletons.py — process-wide service/adjustment cache

Private helpers (``_merge_*`` / ``_build_*`` / ``_get_*``) are re-exported
from ``merge`` for backward compatibility with the ``services.weather_merged``
shim and the ``tests/unit/test_weather_*`` test suite which patches these
names at the package level.
"""
from services.weather.models import (  # noqa: F401
    WeatherData,
    SENSOR_STALE_TIMEOUT,
)
from services.weather.service import WeatherService  # noqa: F401
from services.weather.adjustment import WeatherAdjustment  # noqa: F401
from services.weather.singletons import (  # noqa: F401
    get_weather_service,
    get_weather_adjustment,
)
from services.weather.merge import (  # noqa: F401
    get_merged_weather,
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
