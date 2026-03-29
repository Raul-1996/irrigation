"""Weather service — Open-Meteo API integration with SQLite caching.

Provides current weather data (temperature, humidity, precipitation, wind, ET₀)
for weather-dependent irrigation adjustments.
"""
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Open-Meteo API (free, no key required)
_OPEN_METEO_URL = 'https://api.open-meteo.com/v1/forecast'
_CACHE_TTL_SEC = 30 * 60  # 30 minutes
_REQUEST_TIMEOUT = 10  # seconds


class WeatherData:
    """Parsed weather data from Open-Meteo API."""

    def __init__(self, raw: Dict[str, Any]):
        self.raw = raw
        self.timestamp = raw.get('_fetched_at', time.time())
        self._parse()

    def _parse(self):
        """Extract current-hour values from hourly forecast data."""
        hourly = self.raw.get('hourly', {})
        daily = self.raw.get('daily', {})
        now = datetime.now()
        current_hour = now.strftime('%Y-%m-%dT%H:00')

        # Find current hour index
        times = hourly.get('time', [])
        idx = None
        for i, t in enumerate(times):
            if t == current_hour:
                idx = i
                break
        if idx is None and times:
            # Fallback: closest hour
            idx = 0

        def _safe_get(data, key, index):
            try:
                arr = data.get(key, [])
                if arr and index is not None and index < len(arr):
                    val = arr[index]
                    return float(val) if val is not None else None
            except (ValueError, TypeError, IndexError):
                pass
            return None

        self.temperature = _safe_get(hourly, 'temperature_2m', idx)
        self.humidity = _safe_get(hourly, 'relative_humidity_2m', idx)
        self.precipitation = _safe_get(hourly, 'precipitation', idx)
        self.wind_speed = _safe_get(hourly, 'wind_speed_10m', idx)
        self.et0_hourly = _safe_get(hourly, 'et0_fao_evapotranspiration', idx)

        # Daily values (today = index 0)
        self.daily_precipitation = _safe_get(daily, 'precipitation_sum', 0)
        self.daily_et0 = _safe_get(daily, 'et0_fao_evapotranspiration', 0)

        # Calculate precipitation sum for past 24h from hourly data
        self.precipitation_24h = 0.0
        if idx is not None:
            precip_arr = hourly.get('precipitation', [])
            start_idx = max(0, idx - 23)
            for i in range(start_idx, idx + 1):
                try:
                    val = precip_arr[i]
                    if val is not None:
                        self.precipitation_24h += float(val)
                except (IndexError, ValueError, TypeError):
                    pass

        # Calculate precipitation forecast for next 6h
        self.precipitation_forecast_6h = 0.0
        if idx is not None:
            precip_arr = hourly.get('precipitation', [])
            end_idx = min(len(precip_arr), idx + 7)
            for i in range(idx + 1, end_idx):
                try:
                    val = precip_arr[i]
                    if val is not None:
                        self.precipitation_forecast_6h += float(val)
                except (IndexError, ValueError, TypeError):
                    pass

    def to_dict(self) -> Dict[str, Any]:
        return {
            'temperature': self.temperature,
            'humidity': self.humidity,
            'precipitation': self.precipitation,
            'wind_speed': self.wind_speed,
            'et0_hourly': self.et0_hourly,
            'daily_precipitation': self.daily_precipitation,
            'daily_et0': self.daily_et0,
            'precipitation_24h': self.precipitation_24h,
            'precipitation_forecast_6h': self.precipitation_forecast_6h,
            'timestamp': self.timestamp,
        }


class WeatherService:
    """Fetches and caches weather data from Open-Meteo."""

    def __init__(self, db_path: str = 'irrigation.db'):
        self.db_path = db_path

    def _get_location(self) -> Optional[Dict[str, float]]:
        """Get lat/lon from settings."""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute("SELECT value FROM settings WHERE key = 'weather.latitude'")
                lat_row = cur.fetchone()
                cur = conn.execute("SELECT value FROM settings WHERE key = 'weather.longitude'")
                lon_row = cur.fetchone()
                if lat_row and lon_row and lat_row['value'] and lon_row['value']:
                    return {
                        'latitude': float(lat_row['value']),
                        'longitude': float(lon_row['value']),
                    }
        except (sqlite3.Error, ValueError, TypeError) as e:
            logger.debug("Weather location read error: %s", e)
        return None

    def _get_cached(self, lat: float, lon: float) -> Optional[WeatherData]:
        """Return cached weather data if still fresh."""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    'SELECT data, fetched_at FROM weather_cache '
                    'WHERE latitude = ? AND longitude = ? '
                    'ORDER BY fetched_at DESC LIMIT 1',
                    (round(lat, 4), round(lon, 4)),
                )
                row = cur.fetchone()
                if row:
                    fetched_at = float(row['fetched_at'])
                    if time.time() - fetched_at < _CACHE_TTL_SEC:
                        data = json.loads(row['data'])
                        data['_fetched_at'] = fetched_at
                        return WeatherData(data)
        except (sqlite3.Error, json.JSONDecodeError, ValueError, TypeError) as e:
            logger.debug("Weather cache read error: %s", e)
        return None

    def _save_cache(self, lat: float, lon: float, data: Dict[str, Any]) -> None:
        """Save weather data to cache."""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                now = time.time()
                conn.execute(
                    'INSERT OR REPLACE INTO weather_cache '
                    '(latitude, longitude, data, fetched_at) VALUES (?, ?, ?, ?)',
                    (round(lat, 4), round(lon, 4), json.dumps(data), now),
                )
                # Clean old entries
                conn.execute(
                    'DELETE FROM weather_cache WHERE fetched_at < ?',
                    (now - _CACHE_TTL_SEC * 4,),
                )
                conn.commit()
        except (sqlite3.Error, json.JSONDecodeError) as e:
            logger.debug("Weather cache write error: %s", e)

    def _fetch_api(self, lat: float, lon: float) -> Optional[Dict[str, Any]]:
        """Fetch weather data from Open-Meteo API."""
        try:
            import requests
        except ImportError:
            try:
                import urllib.request
                import urllib.parse
                params = urllib.parse.urlencode({
                    'latitude': lat,
                    'longitude': lon,
                    'hourly': 'temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,et0_fao_evapotranspiration',
                    'daily': 'precipitation_sum,et0_fao_evapotranspiration',
                    'timezone': 'auto',
                    'forecast_days': 2,
                })
                url = f'{_OPEN_METEO_URL}?{params}'
                req = urllib.request.Request(url, headers={'User-Agent': 'WB-Irrigation/2.0'})
                with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                    return json.loads(resp.read().decode('utf-8'))
            except Exception as e:
                logger.warning("Weather API fetch (urllib) failed: %s", e)
                return None

        try:
            resp = requests.get(
                _OPEN_METEO_URL,
                params={
                    'latitude': lat,
                    'longitude': lon,
                    'hourly': 'temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,et0_fao_evapotranspiration',
                    'daily': 'precipitation_sum,et0_fao_evapotranspiration',
                    'timezone': 'auto',
                    'forecast_days': 2,
                },
                timeout=_REQUEST_TIMEOUT,
                headers={'User-Agent': 'WB-Irrigation/2.0'},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning("Weather API fetch failed: %s", e)
            return None

    def get_weather(self, force_refresh: bool = False) -> Optional[WeatherData]:
        """Get current weather data (cached or fresh).

        Returns None if location not configured or API unavailable.
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
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    'SELECT data, fetched_at FROM weather_cache '
                    'WHERE latitude = ? AND longitude = ? '
                    'ORDER BY fetched_at DESC LIMIT 1',
                    (round(lat, 4), round(lon, 4)),
                )
                row = cur.fetchone()
                if row:
                    data = json.loads(row['data'])
                    data['_fetched_at'] = float(row['fetched_at'])
                    logger.info("Weather: using stale cache (API unavailable)")
                    return WeatherData(data)
        except (sqlite3.Error, json.JSONDecodeError) as e:
            logger.debug("Weather stale cache read error: %s", e)

        return None

    def get_weather_summary(self) -> Dict[str, Any]:
        """Get weather summary for dashboard display."""
        weather = self.get_weather()
        if not weather:
            return {'available': False}

        from services.weather_adjustment import WeatherAdjustment
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


# Module-level singleton
_weather_service: Optional[WeatherService] = None


def get_weather_service(db_path: str = 'irrigation.db') -> WeatherService:
    """Get or create the weather service singleton."""
    global _weather_service
    if _weather_service is None:
        _weather_service = WeatherService(db_path)
    return _weather_service
