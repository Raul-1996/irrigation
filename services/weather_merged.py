"""Compatibility adapter for the documented merged-weather public API.

The canonical weather package now builds forecasts and astronomy through
``WeatherService.get_weather_extended``.  This module keeps the historical
``services.weather_merged`` import path and adds only the local-sensor merge
that is not part of that service payload.
"""

import copy
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from services.weather.models import SENSOR_STALE_TIMEOUT

logger = logging.getLogger(__name__)


def __getattr__(name: str):
    """Resolve package constants lazily so legacy-first imports cannot cycle."""
    if name == "SENSOR_STALE_TIMEOUT":
        from services.weather.models import SENSOR_STALE_TIMEOUT

        return SENSOR_STALE_TIMEOUT
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_weather_service(db_path: str):
    """Resolve the canonical singleton lazily to keep legacy-first imports safe."""
    from services.weather.singletons import get_weather_service as get_service

    return get_service(db_path)


def _get_env_state(now: float) -> dict[str, Any]:
    """Delegate to the canonical sensor snapshot helper after import completes."""
    from services.weather.merge import _get_env_state as get_state

    return get_state(now)


def _get_rain_state() -> dict[str, Any]:
    """Return the optional RainMonitor state without making it a dependency."""
    try:
        from services.monitors import rain_monitor

        config = rain_monitor._cfg or {}
        state = rain_monitor.get_sensor_state()
        return {
            "enabled": bool(config.get("enabled", False)) or state != "disabled",
            "is_rain": rain_monitor.is_rain,
            "online": bool(rain_monitor.sensor_online),
            "state": state,
        }
    except Exception as exc:
        logger.debug("RainMonitor state unavailable: %s", exc)
        return {"enabled": True, "is_rain": None, "online": False, "state": "offline"}


def _api_field(current: dict[str, Any], name: str, *, unit: str | None = None) -> dict[str, Any]:
    """Copy a field from the canonical payload and normalize its source."""
    field = dict(current.get(name) or {})
    field["source"] = "api"
    if unit is not None:
        field.setdefault("unit", unit)
    return field


def _merge_env_field(
    api_field: dict[str, Any],
    *,
    enabled: bool,
    online: bool,
    local_value: Any,
) -> dict[str, Any]:
    """Prefer a usable local reading and annotate API fallback otherwise."""
    if online and local_value is not None:
        return {
            "value": local_value,
            "source": "local",
            "unit": api_field.get("unit"),
        }

    result = dict(api_field)
    result["source"] = "api_fallback" if enabled else "api"
    return result


def _api_rain(precipitation: Any) -> bool:
    try:
        return precipitation is not None and float(precipitation) > 0
    except (TypeError, ValueError):
        return False


def merge_weather_response(extended: dict[str, Any]) -> dict[str, Any]:
    """Add rain health without replacing the canonical effective snapshot.

    ``WeatherService.get_weather_extended`` has already selected one local/API
    temperature and humidity snapshot and used it for values, factors, and the
    skip decision. Reading those sensors again here could pair a new 30°C value
    with an older freeze decision. Their canonical fields are therefore
    immutable here; only the independent rain-monitor status is appended.
    """
    if not extended.get("available"):
        return extended

    result = copy.deepcopy(extended)
    current = result.setdefault("current", {})
    temperature = dict(current.get("temperature") or {})
    humidity = dict(current.get("humidity") or {})
    result["temperature"] = temperature.get("value")
    result["humidity"] = humidity.get("value")

    rain_state = _get_rain_state()
    rain_enabled = bool(rain_state.get("enabled"))
    rain_value = rain_state.get("is_rain")
    rain_online = rain_enabled and bool(rain_state.get("online", rain_value is not None))
    rain_state_name = str(
        rain_state.get("state") or ("rain" if rain_online and rain_value else "dry" if rain_online else "offline")
    )
    if rain_enabled and rain_online and rain_value is not None:
        current["rain"] = {"value": bool(rain_value), "source": "local"}
    elif rain_enabled:
        current["rain"] = {
            "value": None,
            "source": "local",
            "state": rain_state_name,
        }
    else:
        precipitation = (current.get("precipitation_mm") or {}).get("value")
        current["rain"] = {
            "value": _api_rain(precipitation),
            "source": "api",
        }

    sensors = result.setdefault("sensors", {})
    for name, field in (("temperature", temperature), ("humidity", humidity)):
        source = str(field.get("source") or "api")
        sensors.setdefault(
            name,
            {
                "enabled": source in {"local", "api_fallback"},
                "online": source == "local",
                "last_rx": None,
            },
        )
    sensors["rain"] = {
        "enabled": rain_enabled,
        "online": rain_online,
        "value": rain_value,
        "state": rain_state_name if rain_enabled else "disabled",
    }
    return result


def get_merged_weather(db_path: str) -> dict[str, Any]:
    """Return the stable local-sensor-first weather payload.

    Forecast parsing, astronomy, weather codes, and cache age are delegated to
    the live ``WeatherService`` implementation.  Fresh local temperature,
    humidity, and rain observations override API values; enabled but offline
    sensors are explicitly marked ``api_fallback``.
    """
    try:
        extended = get_weather_service(db_path).get_weather_extended()
    except Exception as exc:
        logger.warning("Failed to get extended weather: %s", exc)
        return {"available": False}

    if not extended.get("available"):
        return {"available": False}

    now = time.time()
    current = extended.get("current") or {}
    stats = extended.get("stats") or {}
    env_state = _get_env_state(now)
    rain_state = _get_rain_state()

    temperature_api = _api_field(current, "temperature", unit="°C")
    humidity_api = _api_field(current, "humidity", unit="%")
    temperature = _merge_env_field(
        temperature_api,
        enabled=bool(env_state.get("temp_enabled")),
        online=bool(env_state.get("temp_online")),
        local_value=env_state.get("temp_value"),
    )
    humidity = _merge_env_field(
        humidity_api,
        enabled=bool(env_state.get("hum_enabled")),
        online=bool(env_state.get("hum_online")),
        local_value=env_state.get("hum_value"),
    )

    rain_enabled = bool(rain_state.get("enabled"))
    rain_value = rain_state.get("is_rain")
    rain_online = rain_enabled and bool(rain_state.get("online", rain_value is not None))
    rain_state_name = str(
        rain_state.get("state") or ("rain" if rain_online and rain_value else "dry" if rain_online else "offline")
    )
    if rain_enabled and rain_online and rain_value is not None:
        rain = {"value": bool(rain_value), "source": "local"}
    elif rain_enabled:
        rain = {
            "value": None,
            "source": "local",
            "state": rain_state_name,
        }
    else:
        precipitation = (current.get("precipitation_mm") or {}).get("value")
        rain = {
            "value": _api_rain(precipitation),
            "source": "api",
        }

    sensors = {
        "temperature": {
            "enabled": bool(env_state.get("temp_enabled")),
            "online": bool(env_state.get("temp_online")),
            "last_rx": env_state.get("temp_last_rx", 0),
        },
        "humidity": {
            "enabled": bool(env_state.get("hum_enabled")),
            "online": bool(env_state.get("hum_online")),
            "last_rx": env_state.get("hum_last_rx", 0),
        },
        "rain": {
            "enabled": rain_enabled,
            "online": rain_online,
            "value": rain_value,
            "state": rain_state_name if rain_enabled else "disabled",
        },
    }

    return {
        "available": True,
        "temperature": temperature,
        "humidity": humidity,
        "rain": rain,
        "wind_speed": _api_field(current, "wind_speed"),
        "precipitation_mm": _api_field(current, "precipitation_mm"),
        "precipitation_24h": stats.get("precipitation_24h", 0.0),
        "precipitation_forecast_6h": stats.get("precipitation_forecast_6h", 0.0),
        "daily_et0": stats.get("daily_et0"),
        "weather_code": current.get("weather_code"),
        "forecast_24h": extended.get("forecast_24h") or [],
        "forecast_3d": extended.get("forecast_3d") or [],
        "astronomy": extended.get("astronomy") or {"sunrise": None, "sunset": None},
        "sensors": sensors,
        "timestamp": now,
        "cache_age_sec": extended.get("cache_age_sec", 0.0),
    }


__all__ = ["SENSOR_STALE_TIMEOUT", "get_merged_weather", "merge_weather_response"]
