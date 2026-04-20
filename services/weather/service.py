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
from typing import Any, Dict, Optional

from services.weather import cache as _cache
from services.weather.client import fetch_api as _fetch_api_impl
from services.weather.models import WeatherData

logger = logging.getLogger(__name__)


class WeatherService:
    """Fetches and caches weather data from Open-Meteo."""

    def __init__(self, db_path: str = 'irrigation.db') -> None:
        self.db_path = db_path

    def _get_location(self) -> Optional[Dict[str, float]]:
        """Get lat/lon from settings (thin delegate to ``cache.get_location``)."""
        return _cache.get_location(self.db_path)

    def _get_cached(self, lat: float, lon: float) -> Optional[WeatherData]:
        """Return cached weather data if still fresh (delegates to ``cache.read_fresh``)."""
        return _cache.read_fresh(self.db_path, lat, lon)

    def _save_cache(self, lat: float, lon: float, data: Dict[str, Any]) -> None:
        """Save weather data to cache (delegates to ``cache.save``)."""
        _cache.save(self.db_path, lat, lon, data)

    def _fetch_api(self, lat: float, lon: float) -> Optional[Dict[str, Any]]:
        """Fetch weather data from Open-Meteo API (delegates to ``client.fetch_api``).

        Kept as an instance method (rather than a free function call) so that
        test code can ``@patch('services.weather.WeatherService._fetch_api')``.
        """
        return _fetch_api_impl(lat, lon)

    def get_weather(self, force_refresh: bool = False) -> Optional[WeatherData]:
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

        lat = location['latitude']
        lon = location['longitude']

        if not force_refresh:
            cached = self._get_cached(lat, lon)
            if cached:
                return cached

        raw = self._fetch_api(lat, lon)
        if raw:
            raw['_fetched_at'] = time.time()
            self._save_cache(lat, lon, raw)
            return WeatherData(raw)

        # Fallback to stale cache if API fails
        stale = _cache.read_stale(self.db_path, lat, lon)
        if stale is not None:
            return stale

        return None

    def get_weather_summary(self) -> Dict[str, Any]:
        """Get weather summary for dashboard display."""
        weather = self.get_weather()
        if not weather:
            return {'available': False}

        # Local import avoids a circular dep at module load time
        # (adjustment imports singletons → singletons imports service).
        from services.weather.adjustment import WeatherAdjustment

        adj = WeatherAdjustment(self.db_path)
        coefficient = adj.get_coefficient()
        skip_info = adj.should_skip()

        return {
            'available': True,
            'temperature': weather.temperature,
            'humidity': weather.humidity,
            'precipitation': weather.precipitation,
            'wind_speed': weather.wind_speed,
            'precipitation_24h': weather.precipitation_24h,
            'precipitation_forecast_6h': weather.precipitation_forecast_6h,
            'daily_et0': weather.daily_et0,
            'coefficient': coefficient,
            'skip': skip_info.get('skip', False),
            'skip_reason': skip_info.get('reason', ''),
            'timestamp': weather.timestamp,
        }

    def get_weather_extended(self) -> Dict[str, Any]:
        """Get extended weather data for the new weather widget."""
        weather = self.get_weather()
        if not weather:
            return {'available': False}

        from services.weather_codes import get_weather_icon, get_weather_desc
        from services.weather.adjustment import WeatherAdjustment

        adj = WeatherAdjustment(self.db_path)
        coefficient = adj.get_coefficient()
        skip_info = adj.should_skip()
        factors = adj.get_factors_detail(weather)

        current = {
            'temperature': {'value': weather.temperature, 'source': 'api', 'unit': '°C'},
            'humidity': {'value': weather.humidity, 'source': 'api', 'unit': '%'},
            'rain': {'value': False, 'source': 'api'},
            'precipitation_mm': {'value': weather.precipitation, 'source': 'api', 'unit': 'мм'},
            'wind_speed': {'value': weather.wind_speed, 'source': 'api', 'unit': 'м/с'},
            'weather_code': weather.weather_code,
            'weather_icon': get_weather_icon(weather.weather_code),
            'weather_desc': get_weather_desc(weather.weather_code),
        }

        stats = {
            'precipitation_24h': weather.precipitation_24h,
            'precipitation_forecast_6h': weather.precipitation_forecast_6h,
            'daily_et0': weather.daily_et0,
        }

        adjustment = {
            'coefficient': coefficient,
            'skip': skip_info.get('skip', False),
            'skip_reason': skip_info.get('reason', ''),
            'skip_type': skip_info.get('details', {}).get('type'),
            'factors': factors,
        }

        forecast_24h = []
        for item in weather.hourly_forecast_24h:
            fc = dict(item)
            fc['icon'] = get_weather_icon(item.get('weather_code'))
            forecast_24h.append(fc)

        forecast_3d = []
        for item in weather.daily_forecast:
            fc = dict(item)
            fc['icon'] = get_weather_icon(item.get('weather_code'))
            forecast_3d.append(fc)

        astronomy = {
            'sunrise': weather.sunrise,
            'sunset': weather.sunset,
        }

        cache_age_sec = time.time() - weather.timestamp if weather.timestamp else 0

        result = {
            'available': True,
            'temperature': weather.temperature,
            'humidity': weather.humidity,
            'precipitation': weather.precipitation,
            'wind_speed': weather.wind_speed,
            'precipitation_24h': weather.precipitation_24h,
            'precipitation_forecast_6h': weather.precipitation_forecast_6h,
            'daily_et0': weather.daily_et0,
            'coefficient': coefficient,
            'skip': skip_info.get('skip', False),
            'skip_reason': skip_info.get('reason', ''),
            'timestamp': weather.timestamp,
            'current': current,
            'stats': stats,
            'adjustment': adjustment,
            'forecast_24h': forecast_24h,
            'forecast_3d': forecast_3d,
            'astronomy': astronomy,
            'cache_age_sec': round(cache_age_sec, 1),
        }

        return result
