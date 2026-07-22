"""Weather service package — Open-Meteo integration and adjustment engine.

Public API (stable — callers MUST use these names):
    WeatherData           — parsed API response (dataclass-like container)
    WeatherService        — HTTP client + SQLite cache orchestrator
    WeatherAdjustment     — Zimmerman + ET₀ watering-coefficient engine
    get_weather_service() — module-level singleton accessor
    get_weather_adjustment() — module-level singleton accessor
    get_merged_weather()  — local-sensor-first extended weather payload
    SENSOR_STALE_TIMEOUT  — staleness threshold for local sensor data (seconds)

Package layout (Wave 4 refactor):
    models.py     — WeatherData parser + module constants
    client.py     — Open-Meteo HTTP fetch (requests + urllib fallback)
    cache.py      — SQLite weather_cache read/write/stale-fallback
    service.py    — WeatherService orchestrator
    adjustment.py — WeatherAdjustment (Zimmerman + ET₀ + skip rules)
    merge.py      — _get_env_state local-sensor snapshot helper
    singletons.py — process-wide service/adjustment cache
"""

from services.weather.adjustment import WeatherAdjustment
from services.weather.models import (
    SENSOR_STALE_TIMEOUT,
    WeatherData,
)
from services.weather.service import WeatherService
from services.weather.singletons import (
    get_weather_adjustment,
    get_weather_service,
)
from services.weather_merged import get_merged_weather

__all__ = [
    "SENSOR_STALE_TIMEOUT",
    "WeatherAdjustment",
    "WeatherData",
    "WeatherService",
    "get_merged_weather",
    "get_weather_adjustment",
    "get_weather_service",
]
