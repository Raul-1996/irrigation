"""Merged weather data — combine local MQTT sensors with Open-Meteo API.

Single responsibility: produce a unified ``weather`` payload for the
``/api/weather-merged`` endpoint that prefers local ``EnvMonitor`` (temp,
hum) and ``RainMonitor`` readings when they are enabled and fresh
(<``SENSOR_STALE_TIMEOUT``), falling back to API values annotated with a
``source`` of ``"api"`` or ``"api_fallback"`` (the latter means the local
sensor exists but is offline).

All ``_merge_*`` / ``_build_*`` / ``_get_*`` helpers remain module-level
(not class methods) because ``services.weather_merged`` (the legacy shim)
and ``tests/unit/test_weather_merged.py`` patch them by fully-qualified
name at the package level.
"""
from datetime import datetime
import logging
import time
from typing import Any, Dict, List, Optional

from services.weather.models import SENSOR_STALE_TIMEOUT
from services.weather.singletons import get_weather_service

logger = logging.getLogger(__name__)


def get_merged_weather(db_path: str) -> Dict[str, Any]:
    """Merge local sensor data with Open-Meteo API data.

    Local sensors (EnvMonitor temp/hum, RainMonitor) take priority when
    they are enabled and have fresh data (< ``SENSOR_STALE_TIMEOUT`` seconds old).
    Otherwise, API data is used with appropriate source annotation.
    """
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


# ---------------------------------------------------------------------------
# Merged weather internal helpers
# ---------------------------------------------------------------------------

def _get_api_weather(db_path):
    # type: (str) -> Optional[Any]
    """Get weather data from the WeatherService."""
    try:
        svc = get_weather_service(db_path)
        return svc.get_weather()
    except (ImportError, OSError, Exception) as e:
        logger.warning("Failed to get API weather: %s", e)
        return None


def _get_env_state(now):
    # type: (float) -> Dict[str, Any]
    """Get current EnvMonitor state."""
    try:
        from services.monitors import env_monitor
        cfg = env_monitor.cfg or {}
        temp_cfg = cfg.get("temp") or {}
        hum_cfg = cfg.get("hum") or {}

        return {
            "temp_enabled": bool(temp_cfg.get("enabled")),
            "temp_value": env_monitor.temp_value,
            "temp_last_rx": env_monitor.last_temp_rx_ts,
            "temp_online": (
                bool(temp_cfg.get("enabled"))
                and env_monitor.last_temp_rx_ts > 0
                and (now - env_monitor.last_temp_rx_ts) < SENSOR_STALE_TIMEOUT
            ),
            "hum_enabled": bool(hum_cfg.get("enabled")),
            "hum_value": env_monitor.hum_value,
            "hum_last_rx": env_monitor.last_hum_rx_ts,
            "hum_online": (
                bool(hum_cfg.get("enabled"))
                and env_monitor.last_hum_rx_ts > 0
                and (now - env_monitor.last_hum_rx_ts) < SENSOR_STALE_TIMEOUT
            ),
        }
    except (ImportError, Exception) as e:
        logger.debug("EnvMonitor state unavailable: %s", e)
        return {
            "temp_enabled": False, "temp_value": None, "temp_last_rx": 0,
            "temp_online": False,
            "hum_enabled": False, "hum_value": None, "hum_last_rx": 0,
            "hum_online": False,
        }


def _get_rain_state():
    # type: () -> Dict[str, Any]
    """Get current RainMonitor state."""
    try:
        from services.monitors import rain_monitor
        cfg = rain_monitor._cfg or {}
        return {
            "enabled": bool(cfg.get("enabled", False)),
            "is_rain": rain_monitor.is_rain,
        }
    except (ImportError, Exception) as e:
        logger.debug("RainMonitor state unavailable: %s", e)
        return {"enabled": False, "is_rain": None}


def _merge_temperature(api_weather, env_state, now):
    # type: (Any, Dict[str, Any], float) -> Dict[str, Any]
    """Merge temperature: local sensor priority over API."""
    if env_state["temp_online"] and env_state["temp_value"] is not None:
        return {
            "value": env_state["temp_value"],
            "source": "local",
            "unit": "°C",
        }

    source = "api"
    if env_state["temp_enabled"] and not env_state["temp_online"]:
        source = "api_fallback"

    return {
        "value": api_weather.temperature,
        "source": source,
        "unit": "°C",
    }


def _merge_humidity(api_weather, env_state, now):
    # type: (Any, Dict[str, Any], float) -> Dict[str, Any]
    """Merge humidity: local sensor priority over API."""
    if env_state["hum_online"] and env_state["hum_value"] is not None:
        return {
            "value": env_state["hum_value"],
            "source": "local",
            "unit": "%",
        }

    source = "api"
    if env_state["hum_enabled"] and not env_state["hum_online"]:
        source = "api_fallback"

    return {
        "value": api_weather.humidity,
        "source": source,
        "unit": "%",
    }


def _merge_rain(api_weather, rain_state):
    # type: (Any, Dict[str, Any]) -> Dict[str, Any]
    """Merge rain: local sensor priority over API."""
    if rain_state["enabled"] and rain_state["is_rain"] is not None:
        return {
            "value": rain_state["is_rain"],
            "source": "local",
        }

    api_rain = False
    if api_weather.precipitation is not None and api_weather.precipitation > 0:
        api_rain = True

    source = "api"
    if rain_state["enabled"] and rain_state["is_rain"] is None:
        source = "api_fallback"

    return {
        "value": api_rain,
        "source": source,
    }


def _get_weather_code(api_weather):
    # type: (Any) -> Optional[int]
    """Extract current weather code from API data."""
    try:
        raw = api_weather.raw or {}
        hourly = raw.get("hourly", {})
        codes = hourly.get("weather_code", [])
        times = hourly.get("time", [])
        if not codes or not times:
            return None

        current_hour = datetime.now().strftime("%Y-%m-%dT%H:00")
        for i, t in enumerate(times):
            if t == current_hour and i < len(codes):
                val = codes[i]
                return int(val) if val is not None else None

        return int(codes[0]) if codes[0] is not None else None
    except (ValueError, TypeError, IndexError, AttributeError) as e:
        logger.debug("Weather code extraction error: %s", e)
        return None


def _build_forecast_24h(api_weather):
    # type: (Any) -> List[Dict[str, Any]]
    """Build 24h forecast: 6 points, every 4 hours."""
    result = []  # type: List[Dict[str, Any]]
    try:
        raw = api_weather.raw or {}
        hourly = raw.get("hourly", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        precips = hourly.get("precipitation", [])
        winds = hourly.get("wind_speed_10m", [])
        codes = hourly.get("weather_code", [])

        if not times:
            return result

        now_str = datetime.now().strftime("%Y-%m-%dT%H:00")

        start_idx = 0
        for i, t in enumerate(times):
            if t >= now_str:
                start_idx = i
                break

        count = 0
        for i in range(start_idx, len(times), 4):
            if count >= 6:
                break
            entry = {
                "time": times[i][11:16] if len(times[i]) >= 16 else times[i],
            }
            if i < len(temps) and temps[i] is not None:
                entry["temp"] = round(float(temps[i]))
            else:
                entry["temp"] = None
            if i < len(precips) and precips[i] is not None:
                entry["precip"] = round(float(precips[i]), 1)
            else:
                entry["precip"] = 0.0
            if i < len(winds) and winds[i] is not None:
                entry["wind"] = round(float(winds[i]), 1)
            else:
                entry["wind"] = None
            if i < len(codes) and codes[i] is not None:
                entry["weather_code"] = int(codes[i])
            else:
                entry["weather_code"] = None
            result.append(entry)
            count += 1
    except (ValueError, TypeError, IndexError, AttributeError) as e:
        logger.debug("Forecast 24h build error: %s", e)

    return result


def _build_forecast_3d(api_weather):
    # type: (Any) -> List[Dict[str, Any]]
    """Build 3-day daily forecast."""
    result = []  # type: List[Dict[str, Any]]
    try:
        raw = api_weather.raw or {}
        daily = raw.get("daily", {})
        dates = daily.get("time", [])
        precip_sums = daily.get("precipitation_sum", [])
        et0s = daily.get("et0_fao_evapotranspiration", [])
        temp_maxs = daily.get("temperature_2m_max", [])
        temp_mins = daily.get("temperature_2m_min", [])
        codes = daily.get("weather_code", [])

        for i in range(min(3, len(dates))):
            entry = {
                "date": dates[i] if i < len(dates) else None,
            }
            if i < len(temp_mins) and temp_mins[i] is not None:
                entry["temp_min"] = round(float(temp_mins[i]))
            else:
                entry["temp_min"] = None
            if i < len(temp_maxs) and temp_maxs[i] is not None:
                entry["temp_max"] = round(float(temp_maxs[i]))
            else:
                entry["temp_max"] = None
            if i < len(precip_sums) and precip_sums[i] is not None:
                entry["precip_sum"] = round(float(precip_sums[i]), 1)
            else:
                entry["precip_sum"] = 0.0
            if i < len(codes) and codes[i] is not None:
                entry["weather_code"] = int(codes[i])
            else:
                entry["weather_code"] = None
            if i < len(et0s) and et0s[i] is not None:
                entry["et0"] = round(float(et0s[i]), 1)
            else:
                entry["et0"] = None
            result.append(entry)
    except (ValueError, TypeError, IndexError, AttributeError) as e:
        logger.debug("Forecast 3d build error: %s", e)

    return result


def _build_astronomy(api_weather):
    # type: (Any) -> Dict[str, Optional[str]]
    """Extract sunrise/sunset from daily data."""
    try:
        raw = api_weather.raw or {}
        daily = raw.get("daily", {})
        sunrises = daily.get("sunrise", [])
        sunsets = daily.get("sunset", [])

        sunrise = None  # type: Optional[str]
        sunset = None   # type: Optional[str]

        if sunrises and sunrises[0]:
            sr = str(sunrises[0])
            if "T" in sr:
                sunrise = sr.split("T")[1][:5]
            else:
                sunrise = sr

        if sunsets and sunsets[0]:
            ss = str(sunsets[0])
            if "T" in ss:
                sunset = ss.split("T")[1][:5]
            else:
                sunset = ss

        return {"sunrise": sunrise, "sunset": sunset}
    except (ValueError, TypeError, IndexError, AttributeError) as e:
        logger.debug("Astronomy extraction error: %s", e)
        return {"sunrise": None, "sunset": None}


def _build_sensor_status(env_state, rain_state):
    # type: (Dict[str, Any], Dict[str, Any]) -> Dict[str, Any]
    """Build sensor status summary."""
    return {
        "temperature": {
            "enabled": env_state.get("temp_enabled", False),
            "online": env_state.get("temp_online", False),
            "last_rx": env_state.get("temp_last_rx", 0),
        },
        "humidity": {
            "enabled": env_state.get("hum_enabled", False),
            "online": env_state.get("hum_online", False),
            "last_rx": env_state.get("hum_last_rx", 0),
        },
        "rain": {
            "enabled": rain_state.get("enabled", False),
            "value": rain_state.get("is_rain"),
        },
    }
