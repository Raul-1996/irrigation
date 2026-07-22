"""WeatherService — orchestrator on top of client.py + cache.py.

Single responsibility: coordinate the fresh-cache / API-fetch / stale-cache
fallback chain and build the summary and extended dashboard payloads used
by the Flask views. Does not itself talk to the network or SQLite — delegates
to ``services.weather.client`` and ``services.weather.cache``.

``_fetch_api``, ``_get_cached``, ``_save_cache`` and ``_get_location`` remain
on this class as thin delegating methods because existing tests patch them
via ``@patch('services.weather.WeatherService._fetch_api')`` and we committed
(Wave 4) to zero behavioural changes.
"""

import logging
import time
from datetime import datetime
from typing import Any

from services.weather import cache as _cache
from services.weather.client import fetch_api as _fetch_api_impl
from services.weather.client import fetch_relay as _fetch_relay_impl
from services.weather.models import _CACHE_TTL_SEC, WeatherData

logger = logging.getLogger(__name__)

# A short upstream outage may use the last forecast, but day-old rain/freeze
# data is unsafe.  Keep the degraded window explicit and testable.
_STALE_CACHE_MAX_AGE_SEC = 6 * 60 * 60


def _relay_payload_is_current(raw: dict[str, Any]) -> bool:
    """True if a relay payload's hourly forecast still covers the current hour.

    A stale relay file (the Action stopped committing) comes back as HTTP 200
    with old content — not a network error.  Reject it before parsing when the
    controller's current hour is absent, so the caller uses only a bounded
    local cache fallback.
    """
    hourly = raw.get("hourly") or {}
    times = hourly.get("time") or []
    offset = raw.get("utc_offset_seconds")
    if not times or offset is None:
        return False
    now_local = datetime.utcfromtimestamp(time.time() + int(offset))
    return now_local.strftime("%Y-%m-%dT%H:00") in times


class WeatherService:
    """Fetches and caches weather data from Open-Meteo."""

    def __init__(self, db_path: str = "irrigation.db") -> None:
        self.db_path = db_path

    def _get_location(self) -> dict[str, float] | None:
        """Get lat/lon from settings (thin delegate to ``cache.get_location``)."""
        return _cache.get_location(self.db_path)

    def _get_cached(self, lat: float, lon: float) -> WeatherData | None:
        """Return cached weather data if still fresh (delegates to ``cache.read_fresh``)."""
        return _cache.read_fresh(self.db_path, lat, lon)

    def _save_cache(self, lat: float, lon: float, data: dict[str, Any]) -> None:
        """Save weather data to cache (delegates to ``cache.save``)."""
        _cache.save(self.db_path, lat, lon, data)

    def _fetch_api(self, lat: float, lon: float) -> dict[str, Any] | None:
        """Fetch weather data — direct Open-Meteo or via the GitHub relay.

        Routing depends on the live ``weather.source_mode`` setting:
            * ``direct`` (default) → ``client.fetch_api`` (Open-Meteo).
            * ``relay``            → ``client.fetch_relay`` (a GitHub file
              pre-fetched by an Action), for sites where Open-Meteo is
              network-blocked. Requires ``OPEN_METEO_RELAY_URL``;
              ``OPEN_METEO_RELAY_TOKEN`` is only needed for a private relay
              repo (empty for a public one). If the URL is missing we log and
              fall back to a direct call.

        Kept as an instance method (rather than a free function call) so that
        test code can ``@patch('services.weather.WeatherService._fetch_api')``.
        """
        if self._get_source_mode() == "relay":
            from config import Config

            url = Config.OPEN_METEO_RELAY_URL
            if url:
                # token may be "" for a public relay repo (raw URL, no auth)
                raw = _fetch_relay_impl(url, Config.OPEN_METEO_RELAY_TOKEN)
                if raw is not None and not _relay_payload_is_current(raw):
                    # Relay served 200 but its forecast no longer covers "now"
                    # (Action stopped updating). Fail CLOSED: return None →
                    # bounded stale-cache fallback + api-down alert, instead of
                    # presenting an old forecast as current weather.
                    logger.error("weather relay payload is stale (no current-hour entry); treating as fetch failure")
                    return None
                return raw
            logger.error(
                "weather.source_mode=relay but OPEN_METEO_RELAY_URL not set; falling back to a direct Open-Meteo call"
            )
        return _fetch_api_impl(lat, lon)

    def _get_source_mode(self) -> str:
        """Read the live weather source mode (``direct`` | ``relay``).

        Defaults to ``direct`` when unset or invalid, so a fresh DB and any
        unexpected value both behave like the historical direct-only path.
        """
        from db.settings import SettingsRepository

        val = SettingsRepository(self.db_path).get_setting_value("weather.source_mode")
        return val if val in ("direct", "relay") else "direct"

    def get_weather(self, force_refresh: bool = False, cache_only: bool = False) -> WeatherData | None:
        """Get current weather data (cached or fresh).

        Order of fallback:
            1. Fresh cache (age < ``_CACHE_TTL_SEC``), unless ``force_refresh``.
            2. Live API call via ``_fetch_api``.
            3. Stale cache (at most six hours old) — degraded-mode fallback after a failed
               live request only.
            4. ``None``.

        ``cache_only=True`` never touches the network and never trusts data
        older than the normal cache TTL.  This is used for display-only
        decisions on hot paths like /api/status; an arbitrarily stale rain or
        freeze forecast must not diverge from the live-capable scheduler.
        """
        location = self._get_location()
        if not location:
            logger.debug("Weather: location not configured")
            return None

        lat = location["latitude"]
        lon = location["longitude"]

        if not force_refresh:
            cached = self._get_cached(lat, lon)
            if cached:
                return cached

        if cache_only:
            return _cache.read_stale(
                self.db_path,
                lat,
                lon,
                max_age_sec=_CACHE_TTL_SEC,
            )

        raw = self._fetch_api(lat, lon)
        if raw:
            raw["_fetched_at"] = time.time()
            self._save_cache(lat, lon, raw)
            return WeatherData(raw)

        # Fallback to stale cache if API fails
        stale = _cache.read_stale(
            self.db_path,
            lat,
            lon,
            max_age_sec=_STALE_CACHE_MAX_AGE_SEC,
        )
        if stale is not None:
            return stale

        return None

    def get_weather_summary(self) -> dict[str, Any]:
        """Get weather summary for dashboard display."""
        weather = self.get_weather()
        if not weather:
            return {"available": False}

        # Local import avoids a circular dep at module load time
        # (adjustment imports singletons → singletons imports service).
        from services.weather.adjustment import WeatherAdjustment

        adj = WeatherAdjustment(self.db_path)
        effective_weather = adj._select_input_source(weather)
        coefficient = adj.get_coefficient(weather=effective_weather)
        skip_info = adj.should_skip(weather=effective_weather)

        return {
            "available": True,
            "temperature": effective_weather.temperature,
            "humidity": effective_weather.humidity,
            "precipitation": effective_weather.precipitation,
            "wind_speed": effective_weather.wind_speed,
            "precipitation_24h": effective_weather.precipitation_24h,
            "precipitation_forecast_6h": effective_weather.precipitation_forecast_6h,
            "daily_et0": effective_weather.daily_et0,
            "coefficient": coefficient,
            "skip": skip_info.get("skip", False),
            "skip_reason": skip_info.get("reason", ""),
            "timestamp": effective_weather.timestamp,
        }

    def get_weather_extended(self) -> dict[str, Any]:
        """Get extended weather data for the new weather widget."""
        weather = self.get_weather()
        if not weather:
            return {"available": False}

        from services.weather.adjustment import WeatherAdjustment
        from services.weather_codes import get_weather_desc, get_weather_icon

        adj = WeatherAdjustment(self.db_path)
        effective_weather = adj._select_input_source(weather)
        coefficient = adj.get_coefficient(weather=effective_weather)
        skip_info = adj.should_skip(weather=effective_weather)
        factors = adj.get_factors_detail(effective_weather)

        current = {
            "temperature": {
                "value": effective_weather.temperature,
                "source": getattr(effective_weather, "temperature_source", "api"),
                "unit": "°C",
            },
            "humidity": {
                "value": effective_weather.humidity,
                "source": getattr(effective_weather, "humidity_source", "api"),
                "unit": "%",
            },
            "rain": {"value": False, "source": "api"},
            "precipitation_mm": {"value": effective_weather.precipitation, "source": "api", "unit": "мм"},
            "wind_speed": {"value": effective_weather.wind_speed, "source": "api", "unit": "м/с"},
            "weather_code": effective_weather.weather_code,
            "weather_icon": get_weather_icon(effective_weather.weather_code),
            "weather_desc": get_weather_desc(effective_weather.weather_code),
        }

        stats = {
            "precipitation_24h": effective_weather.precipitation_24h,
            "precipitation_forecast_6h": effective_weather.precipitation_forecast_6h,
            "daily_et0": effective_weather.daily_et0,
        }

        # H2 is intentionally shadow-only (PR-060): expose its cached diagnostic
        # value as a second opinion, but always report H1 as the applied value.
        coefficient_legacy = coefficient
        from services.weather.balance import has_computed, read_cached_coef

        balance_diagnostic = adj.get_balance_diagnostic_status()
        coefficient_balance = (
            read_cached_coef(self.db_path)
            if balance_diagnostic["status"] != "unavailable" and has_computed(self.db_path)
            else None
        )
        mode = "shadow" if coefficient_balance is not None else "legacy"
        coefficient_applied = coefficient_legacy

        adjustment = {
            "coefficient": coefficient,
            "skip": skip_info.get("skip", False),
            "skip_reason": skip_info.get("reason", ""),
            "skip_type": skip_info.get("details", {}).get("type"),
            "factors": factors,
            "mode": mode,
            "coefficient_applied": coefficient_applied,
            "coefficient_legacy": coefficient_legacy,
            "coefficient_balance": coefficient_balance,
            "balance_enabled": adj._balance_enabled(),
            "balance_active": False,
            "balance_status": balance_diagnostic["status"],
            "balance_last_recalc_date": balance_diagnostic["last_recalc_date"],
            "balance_age_days": balance_diagnostic["age_days"],
            "balance_stale": balance_diagnostic["stale"],
            "balance_fresh": balance_diagnostic["fresh"],
        }

        forecast_24h = []
        for item in weather.hourly_forecast_24h:
            fc = dict(item)
            fc["icon"] = get_weather_icon(item.get("weather_code"))
            forecast_24h.append(fc)

        forecast_3d = []
        for item in weather.daily_forecast:
            fc = dict(item)
            fc["icon"] = get_weather_icon(item.get("weather_code"))
            forecast_3d.append(fc)

        astronomy = {
            "sunrise": weather.sunrise,
            "sunset": weather.sunset,
        }

        cache_age_sec = time.time() - weather.timestamp if weather.timestamp else 0

        result = {
            "available": True,
            "temperature": effective_weather.temperature,
            "humidity": effective_weather.humidity,
            "precipitation": effective_weather.precipitation,
            "wind_speed": effective_weather.wind_speed,
            "precipitation_24h": effective_weather.precipitation_24h,
            "precipitation_forecast_6h": effective_weather.precipitation_forecast_6h,
            "daily_et0": effective_weather.daily_et0,
            "coefficient": coefficient,
            "skip": skip_info.get("skip", False),
            "skip_reason": skip_info.get("reason", ""),
            "timestamp": weather.timestamp,
            "current": current,
            "stats": stats,
            "adjustment": adjustment,
            "forecast_24h": forecast_24h,
            "forecast_3d": forecast_3d,
            "astronomy": astronomy,
            "cache_age_sec": round(cache_age_sec, 1),
        }

        return result
