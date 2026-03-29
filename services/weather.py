"""Weather service — Open-Meteo API integration with SQLite caching.

Provides current weather data (temperature, humidity, precipitation, wind, ET₀)
for weather-dependent irrigation adjustments.

Extended in v2: forecast_days=3, weather_code, sunrise/sunset, temp_min/max,
hourly 24h forecast (every 4h), daily 3-day forecast, wind in m/s.
"""
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Open-Meteo API (free, no key required)
_OPEN_METEO_URL = 'https://api.open-meteo.com/v1/forecast'
_CACHE_TTL_SEC = 30 * 60  # 30 minutes
_REQUEST_TIMEOUT = 10  # seconds

# Day-of-week names in Russian (Mon=0 ... Sun=6)
_DAY_NAMES_RU = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']


class WeatherData:
    """Parsed weather data from Open-Meteo API."""

    def __init__(self, raw):
        # type: (Dict[str, Any]) -> None
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

        def _safe_get_int(data, key, index):
            try:
                arr = data.get(key, [])
                if arr and index is not None and index < len(arr):
                    val = arr[index]
                    return int(val) if val is not None else None
            except (ValueError, TypeError, IndexError):
                pass
            return None

        self.temperature = _safe_get(hourly, 'temperature_2m', idx)
        self.humidity = _safe_get(hourly, 'relative_humidity_2m', idx)
        self.precipitation = _safe_get(hourly, 'precipitation', idx)
        self.wind_speed = _safe_get(hourly, 'wind_speed_10m', idx)
        self.et0_hourly = _safe_get(hourly, 'et0_fao_evapotranspiration', idx)

        # NEW: current weather code (WMO)
        self.weather_code = _safe_get_int(hourly, 'weather_code', idx)

        # Daily values (today = index 0)
        self.daily_precipitation = _safe_get(daily, 'precipitation_sum', 0)
        self.daily_et0 = _safe_get(daily, 'et0_fao_evapotranspiration', 0)

        # NEW: daily temperature min/max (today)
        self.temperature_min = _safe_get(daily, 'temperature_2m_min', 0)
        self.temperature_max = _safe_get(daily, 'temperature_2m_max', 0)

        # NEW: sunrise/sunset (today, as strings like "2026-03-29T06:28")
        daily_sunrise = daily.get('sunrise', [])
        daily_sunset = daily.get('sunset', [])
        self.sunrise = None
        self.sunset = None
        if daily_sunrise and len(daily_sunrise) > 0 and daily_sunrise[0]:
            try:
                # Extract HH:MM from ISO datetime
                self.sunrise = str(daily_sunrise[0]).split('T')[1][:5] if 'T' in str(daily_sunrise[0]) else str(daily_sunrise[0])
            except (IndexError, ValueError):
                pass
        if daily_sunset and len(daily_sunset) > 0 and daily_sunset[0]:
            try:
                self.sunset = str(daily_sunset[0]).split('T')[1][:5] if 'T' in str(daily_sunset[0]) else str(daily_sunset[0])
            except (IndexError, ValueError):
                pass

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

        # NEW: hourly forecast for next 24h (every 4 hours, 6 points)
        self.hourly_forecast_24h = []  # type: List[Dict[str, Any]]
        if idx is not None:
            hour_times = hourly.get('time', [])
            temp_arr = hourly.get('temperature_2m', [])
            precip_arr = hourly.get('precipitation', [])
            wind_arr = hourly.get('wind_speed_10m', [])
            wcode_arr = hourly.get('weather_code', [])
            # Every 4 hours starting from next even 4h slot
            current_h = now.hour
            # Find next slot: round up to nearest 4
            next_slot = current_h + (4 - current_h % 4) if current_h % 4 != 0 else current_h + 4
            for offset_h in range(0, 24, 4):
                target_h = next_slot + offset_h
                target_idx = idx + (target_h - current_h)
                if target_idx < 0 or target_idx >= len(hour_times):
                    continue
                try:
                    t_str = hour_times[target_idx]
                    hh_mm = t_str.split('T')[1][:5] if 'T' in t_str else t_str
                except (IndexError, ValueError):
                    hh_mm = ''

                def _arr_val(arr, i):
                    try:
                        v = arr[i]
                        return float(v) if v is not None else None
                    except (IndexError, ValueError, TypeError):
                        return None

                def _arr_int(arr, i):
                    try:
                        v = arr[i]
                        return int(v) if v is not None else None
                    except (IndexError, ValueError, TypeError):
                        return None

                self.hourly_forecast_24h.append({
                    'time': hh_mm,
                    'temp': _arr_val(temp_arr, target_idx),
                    'precip': _arr_val(precip_arr, target_idx),
                    'wind': _arr_val(wind_arr, target_idx),
                    'weather_code': _arr_int(wcode_arr, target_idx),
                })
            # Ensure max 6 points
            self.hourly_forecast_24h = self.hourly_forecast_24h[:6]

        # NEW: daily forecast (3 days)
        self.daily_forecast = []  # type: List[Dict[str, Any]]
        daily_times = daily.get('time', [])
        daily_tmin = daily.get('temperature_2m_min', [])
        daily_tmax = daily.get('temperature_2m_max', [])
        daily_psum = daily.get('precipitation_sum', [])
        daily_wcode = daily.get('weather_code', [])
        daily_sr = daily.get('sunrise', [])
        daily_ss = daily.get('sunset', [])

        for di in range(min(3, len(daily_times))):
            date_str = str(daily_times[di]) if di < len(daily_times) else ''
            # Parse day name
            day_name = ''
            try:
                dt = datetime.strptime(date_str, '%Y-%m-%d')
                day_name = _DAY_NAMES_RU[dt.weekday()]
            except (ValueError, IndexError):
                pass

            def _dval(arr, i):
                try:
                    v = arr[i]
                    return float(v) if v is not None else None
                except (IndexError, ValueError, TypeError):
                    return None

            def _dint(arr, i):
                try:
                    v = arr[i]
                    return int(v) if v is not None else None
                except (IndexError, ValueError, TypeError):
                    return None

            sr_val = None
            ss_val = None
            if di < len(daily_sr) and daily_sr[di]:
                try:
                    sr_val = str(daily_sr[di]).split('T')[1][:5] if 'T' in str(daily_sr[di]) else str(daily_sr[di])
                except (IndexError, ValueError):
                    pass
            if di < len(daily_ss) and daily_ss[di]:
                try:
                    ss_val = str(daily_ss[di]).split('T')[1][:5] if 'T' in str(daily_ss[di]) else str(daily_ss[di])
                except (IndexError, ValueError):
                    pass

            self.daily_forecast.append({
                'date': date_str,
                'day_name': day_name,
                'temp_min': _dval(daily_tmin, di),
                'temp_max': _dval(daily_tmax, di),
                'precip_sum': _dval(daily_psum, di),
                'weather_code': _dint(daily_wcode, di),
                'sunrise': sr_val,
                'sunset': ss_val,
            })

        # NEW: min temperature in next 6h (for freeze forecast check)
        self.min_temp_forecast_6h = None  # type: Optional[float]
        if idx is not None:
            temp_arr = hourly.get('temperature_2m', [])
            end_idx_6h = min(len(temp_arr), idx + 7)
            min_t = None
            for i in range(idx, end_idx_6h):
                try:
                    v = temp_arr[i]
                    if v is not None:
                        fv = float(v)
                        if min_t is None or fv < min_t:
                            min_t = fv
                except (IndexError, ValueError, TypeError):
                    pass
            self.min_temp_forecast_6h = min_t

    def to_dict(self):
        # type: () -> Dict[str, Any]
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
            # NEW fields
            'weather_code': self.weather_code,
            'temperature_min': self.temperature_min,
            'temperature_max': self.temperature_max,
            'sunrise': self.sunrise,
            'sunset': self.sunset,
            'hourly_forecast_24h': self.hourly_forecast_24h,
            'daily_forecast': self.daily_forecast,
            'min_temp_forecast_6h': self.min_temp_forecast_6h,
        }


class WeatherService:
    """Fetches and caches weather data from Open-Meteo."""

    def __init__(self, db_path='irrigation.db'):
        # type: (str) -> None
        self.db_path = db_path

    def _get_location(self):
        # type: () -> Optional[Dict[str, float]]
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

    def _get_cached(self, lat, lon):
        # type: (float, float) -> Optional[WeatherData]
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

    def _save_cache(self, lat, lon, data):
        # type: (float, float, Dict[str, Any]) -> None
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

    def _fetch_api(self, lat, lon):
        # type: (float, float) -> Optional[Dict[str, Any]]
        """Fetch weather data from Open-Meteo API.

        Extended in v2: forecast_days=3, weather_code, sunrise/sunset,
        temp_min/max, wind_speed_unit=ms.
        """
        # Build params for v2 extended request
        hourly_params = ','.join([
            'temperature_2m',
            'relative_humidity_2m',
            'precipitation',
            'wind_speed_10m',
            'et0_fao_evapotranspiration',
            'weather_code',
        ])
        daily_params = ','.join([
            'precipitation_sum',
            'et0_fao_evapotranspiration',
            'temperature_2m_max',
            'temperature_2m_min',
            'weather_code',
            'sunrise',
            'sunset',
        ])

        try:
            import requests
        except ImportError:
            try:
                import urllib.request
                import urllib.parse
                params = urllib.parse.urlencode({
                    'latitude': lat,
                    'longitude': lon,
                    'hourly': hourly_params,
                    'daily': daily_params,
                    'timezone': 'auto',
                    'forecast_days': 3,
                    'wind_speed_unit': 'ms',
                })
                url = '%s?%s' % (_OPEN_METEO_URL, params)
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
                    'hourly': hourly_params,
                    'daily': daily_params,
                    'timezone': 'auto',
                    'forecast_days': 3,
                    'wind_speed_unit': 'ms',
                },
                timeout=_REQUEST_TIMEOUT,
                headers={'User-Agent': 'WB-Irrigation/2.0'},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning("Weather API fetch failed: %s", e)
            return None

    def get_weather(self, force_refresh=False):
        # type: (bool) -> Optional[WeatherData]
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

    def get_weather_summary(self):
        # type: () -> Dict[str, Any]
        """Get weather summary for dashboard display.

        Returns the original flat format for backward compatibility.
        For extended data, use get_weather_extended().
        """
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

    def get_weather_extended(self):
        # type: () -> Dict[str, Any]
        """Get extended weather data for the new weather widget.

        Returns both backward-compatible flat fields AND new structured data:
        current, forecast_24h, forecast_3d, astronomy, factors, etc.
        """
        weather = self.get_weather()
        if not weather:
            return {'available': False}

        from services.weather_adjustment import WeatherAdjustment
        from services.weather_codes import get_weather_icon, get_weather_desc

        adj = WeatherAdjustment(self.db_path)
        coefficient = adj.get_coefficient()
        skip_info = adj.should_skip()
        factors = adj.get_factors_detail(weather)

        # Build current section
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

        # Stats
        stats = {
            'precipitation_24h': weather.precipitation_24h,
            'precipitation_forecast_6h': weather.precipitation_forecast_6h,
            'daily_et0': weather.daily_et0,
        }

        # Adjustment
        adjustment = {
            'coefficient': coefficient,
            'skip': skip_info.get('skip', False),
            'skip_reason': skip_info.get('reason', ''),
            'skip_type': skip_info.get('details', {}).get('type'),
            'factors': factors,
        }

        # Forecast 24h with icons
        forecast_24h = []
        for item in weather.hourly_forecast_24h:
            fc = dict(item)
            fc['icon'] = get_weather_icon(item.get('weather_code'))
            forecast_24h.append(fc)

        # Forecast 3d with icons
        forecast_3d = []
        for item in weather.daily_forecast:
            fc = dict(item)
            fc['icon'] = get_weather_icon(item.get('weather_code'))
            forecast_3d.append(fc)

        # Astronomy
        astronomy = {
            'sunrise': weather.sunrise,
            'sunset': weather.sunset,
        }

        # Cache age
        cache_age_sec = time.time() - weather.timestamp if weather.timestamp else 0

        # Build response: backward-compatible flat fields + new structured data
        result = {
            'available': True,
            # Backward-compatible flat fields
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
            # New structured fields
            'current': current,
            'stats': stats,
            'adjustment': adjustment,
            'forecast_24h': forecast_24h,
            'forecast_3d': forecast_3d,
            'astronomy': astronomy,
            'cache_age_sec': round(cache_age_sec, 1),
        }

        return result


# Module-level singleton
_weather_service = None  # type: Optional[WeatherService]


def get_weather_service(db_path='irrigation.db'):
    # type: (str) -> WeatherService
    """Get or create the weather service singleton."""
    global _weather_service
    if _weather_service is None:
        _weather_service = WeatherService(db_path)
    return _weather_service
