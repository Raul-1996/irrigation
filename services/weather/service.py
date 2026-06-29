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
from services.weather.models import WeatherData

logger = logging.getLogger(__name__)


def _relay_payload_is_current(raw: dict[str, Any]) -> bool:
    """True if a relay payload's hourly forecast still covers the current hour.

    A stale relay file (the Action stopped committing) comes back as HTTP 200
    with old content — not a network error — so without this check
    ``WeatherData._parse`` would silently fall back to ``idx=0`` (midnight of the
    file's first day): ~zero ``precipitation_24h`` and a wrong freeze window on a
    live irrigation controller. We mirror the parser's own "current hour" math
    and reject the payload when that hour is absent, so the caller fails closed.
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
                    # stale-cache fallback + api-down alert, instead of letting
                    # the parser silently use idx=0 (midnight of an old day).
                    logger.error(
                        "weather relay payload is stale (no current-hour entry); treating as fetch failure"
                    )
                    return None
                return raw
            logger.error(
                "weather.source_mode=relay but OPEN_METEO_RELAY_URL not set; "
                "falling back to a direct Open-Meteo call"
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

    def get_weather(self, force_refresh: bool = False) -> WeatherData | None:
        """Get current weather data (cached or fresh).

        Order of fallback:
            1. Fresh cache (age < ``_CACHE_TTL_SEC``), unless ``force_refresh``.
            2. Live API call via ``_fetch_api``.
            3. Stale cache (any age) — degraded-mode fallback.
            4. ``None``.
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

        raw = self._fetch_api(lat, lon)
        if raw:
            raw["_fetched_at"] = time.time()
            self._save_cache(lat, lon, raw)
            return WeatherData(raw)

        # Fallback to stale cache if API fails
        stale = _cache.read_stale(self.db_path, lat, lon)
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
        coefficient = adj.get_coefficient()
        skip_info = adj.should_skip()

        return {
            "available": True,
            "temperature": weather.temperature,
            "humidity": weather.humidity,
            "precipitation": weather.precipitation,
            "wind_speed": weather.wind_speed,
            "precipitation_24h": weather.precipitation_24h,
            "precipitation_forecast_6h": weather.precipitation_forecast_6h,
            "daily_et0": weather.daily_et0,
            "coefficient": coefficient,
            "skip": skip_info.get("skip", False),
            "skip_reason": skip_info.get("reason", ""),
            "timestamp": weather.timestamp,
        }

    def get_weather_extended(self) -> dict[str, Any]:
        """Get extended weather data for the new weather widget."""
        weather = self.get_weather()
        if not weather:
            return {"available": False}

        from services.weather.adjustment import WeatherAdjustment
        from services.weather_codes import get_weather_desc, get_weather_icon

        adj = WeatherAdjustment(self.db_path)
        coefficient = adj.get_coefficient()
        skip_info = adj.should_skip()
        factors = adj.get_factors_detail(weather)

        current = {
            "temperature": {"value": weather.temperature, "source": "api", "unit": "°C"},
            "humidity": {"value": weather.humidity, "source": "api", "unit": "%"},
            "rain": {"value": False, "source": "api"},
            "precipitation_mm": {"value": weather.precipitation, "source": "api", "unit": "мм"},
            "wind_speed": {"value": weather.wind_speed, "source": "api", "unit": "м/с"},
            "weather_code": weather.weather_code,
            "weather_icon": get_weather_icon(weather.weather_code),
            "weather_desc": get_weather_desc(weather.weather_code),
        }

        stats = {
            "precipitation_24h": weather.precipitation_24h,
            "precipitation_forecast_6h": weather.precipitation_forecast_6h,
            "daily_et0": weather.daily_et0,
        }

        # H2 "second opinion": expose BOTH coefficients (legacy is cheap to
        # compute from current weather, balance is a cached read), tagging which
        # one is actually applied. The balance value is surfaced whenever the
        # nightly job has computed it at least once — even in shadow (flag off) —
        # so the operator can compare without balance steering watering. In live
        # mode (flag on + fresh) balance is applied and legacy is the 2nd opinion.
        coefficient_legacy = coefficient  # == get_coefficient() above
        balance_on = adj._balance_enabled()
        balance_fresh = balance_on and adj._balance_coef_fresh()
        from services.weather.balance import has_computed, read_cached_coef

        coefficient_balance = read_cached_coef(self.db_path) if has_computed(self.db_path) else None
        if balance_fresh:
            mode = "balance"
            coefficient_applied = coefficient_balance
        elif coefficient_balance is not None:
            # Computed but not live (flag off, or flag on yet cache stale):
            # legacy is applied, balance is shown only as the second opinion.
            mode = "shadow"
            coefficient_applied = coefficient_legacy
        else:
            mode = "legacy"
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
            "temperature": weather.temperature,
            "humidity": weather.humidity,
            "precipitation": weather.precipitation,
            "wind_speed": weather.wind_speed,
            "precipitation_24h": weather.precipitation_24h,
            "precipitation_forecast_6h": weather.precipitation_forecast_6h,
            "daily_et0": weather.daily_et0,
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
