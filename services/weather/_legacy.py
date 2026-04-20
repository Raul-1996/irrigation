"""Wave-4 transition module — thin aggregator during the weather/ split.

This module used to be ``services/weather.py`` (1404 LOC). Submodules have
been carved out progressively across commits c1-c3; this file currently
re-exports the already-extracted symbols and hosts what has not yet been
relocated. It will disappear entirely in commit c4.
"""
import json
import logging
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

# Re-export constants + WeatherData (now in services/weather/models.py).
from services.weather.models import (  # noqa: F401
    WeatherData,
    SENSOR_STALE_TIMEOUT,
    _OPEN_METEO_URL,
    _CACHE_TTL_SEC,
    _REQUEST_TIMEOUT,
    _DAY_NAMES_RU,
)
# Re-export HTTP client + cache helpers (now in client.py / cache.py).
from services.weather.client import fetch_api as _fetch_api_impl  # noqa: F401
from services.weather import cache as _cache  # noqa: F401

logger = logging.getLogger(__name__)


# ===================================================================
# WeatherAdjustment — watering coefficient & skip logic
# ===================================================================

class WeatherAdjustment:
    """Calculate watering adjustment based on weather conditions.

    Hybrid approach: Zimmerman method (simple, proven from OpenSprinkler) + ET₀.
    Calculates a watering coefficient (0-200%) and skip conditions (rain, freeze, wind).
    """

    # Default thresholds
    DEFAULT_RAIN_THRESHOLD_MM = 5.0
    DEFAULT_FREEZE_THRESHOLD_C = 2.0
    DEFAULT_WIND_THRESHOLD_KMH = 25.0     # legacy
    DEFAULT_WIND_THRESHOLD_MS = 7.0       # ~25 km/h
    DEFAULT_HUMIDITY_THRESHOLD_PCT = 80.0
    DEFAULT_HUMIDITY_REDUCTION_PCT = 30
    DEFAULT_BASELINE_TEMP_C = 25.0
    DEFAULT_BASELINE_HUM_PCT = 50.0

    def __init__(self, db_path='irrigation.db'):
        # type: (str) -> None
        self.db_path = db_path

    def _get_settings(self):
        # type: () -> Dict[str, Any]
        """Load weather adjustment settings from DB."""
        defaults = {
            'enabled': False,
            'rain_threshold_mm': self.DEFAULT_RAIN_THRESHOLD_MM,
            'freeze_threshold_c': self.DEFAULT_FREEZE_THRESHOLD_C,
            'wind_threshold_kmh': self.DEFAULT_WIND_THRESHOLD_KMH,
            'wind_threshold_ms': self.DEFAULT_WIND_THRESHOLD_MS,
            'humidity_threshold_pct': self.DEFAULT_HUMIDITY_THRESHOLD_PCT,
            'humidity_reduction_pct': self.DEFAULT_HUMIDITY_REDUCTION_PCT,
            'factor_rain': True,
            'factor_freeze': True,
            'factor_wind': True,
            'factor_humidity': True,
            'factor_heat': True,
        }
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                keys = [
                    'weather.enabled',
                    'weather.rain_threshold_mm',
                    'weather.freeze_threshold_c',
                    'weather.wind_threshold_kmh',
                    'weather.wind_threshold_ms',
                    'weather.humidity_threshold_pct',
                    'weather.humidity_reduction_pct',
                    'weather.factor.rain',
                    'weather.factor.freeze',
                    'weather.factor.wind',
                    'weather.factor.humidity',
                    'weather.factor.heat',
                ]
                for key in keys:
                    cur = conn.execute('SELECT value FROM settings WHERE key = ?', (key,))
                    row = cur.fetchone()
                    if row and row['value'] is not None:
                        val = row['value']
                        if key == 'weather.enabled':
                            defaults['enabled'] = str(val) in ('1', 'true', 'True')
                        elif key.startswith('weather.factor.'):
                            factor_name = key.replace('weather.factor.', '')
                            defaults['factor_' + factor_name] = str(val) in ('1', 'true', 'True')
                        else:
                            short_key = key.replace('weather.', '')
                            try:
                                defaults[short_key] = float(val)
                            except (ValueError, TypeError):
                                pass
        except (sqlite3.Error, OSError) as e:
            logger.debug("Weather settings read error: %s", e)
        return defaults

    def _get_weather(self):
        """Get current weather data."""
        try:
            svc = get_weather_service(self.db_path)
            return svc.get_weather()
        except (ImportError, OSError) as e:
            logger.debug("Weather data unavailable: %s", e)
            return None

    def _has_ms_threshold(self):
        # type: () -> bool
        """Check if weather.wind_threshold_ms is explicitly set in DB."""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cur = conn.execute("SELECT value FROM settings WHERE key = 'weather.wind_threshold_ms'")
                row = cur.fetchone()
                return row is not None and row[0] is not None
        except (sqlite3.Error, ValueError, TypeError):
            return False

    def _get_wind_threshold_ms(self, settings):
        # type: (Dict[str, Any]) -> float
        """Get wind threshold in m/s."""
        if self._has_ms_threshold():
            ms_val = settings.get('wind_threshold_ms', self.DEFAULT_WIND_THRESHOLD_MS)
            return float(ms_val)
        kmh_val = settings.get('wind_threshold_kmh', self.DEFAULT_WIND_THRESHOLD_KMH)
        return round(float(kmh_val) / 3.6, 1)

    def _get_wind_check(self, settings, wind_value):
        # type: (Dict[str, Any], Optional[float]) -> tuple
        """Check if wind exceeds threshold."""
        if wind_value is None:
            return (False, '')
        if self._has_ms_threshold():
            threshold = float(settings.get('wind_threshold_ms', self.DEFAULT_WIND_THRESHOLD_MS))
            exceeds = wind_value > threshold
            detail = '%.1f м/с > %.1f м/с' % (wind_value, threshold) if exceeds else '%.1f м/с < %.1f м/с' % (wind_value, threshold)
            return (exceeds, 'wind_skip: %.1f м/с (порог %.1f м/с)' % (wind_value, threshold) if exceeds else detail)
        else:
            threshold = float(settings.get('wind_threshold_kmh', self.DEFAULT_WIND_THRESHOLD_KMH))
            exceeds = wind_value > threshold
            detail = '%.1f км/ч > %.0f км/ч' % (wind_value, threshold) if exceeds else '%.1f км/ч < %.0f км/ч' % (wind_value, threshold)
            return (exceeds, 'wind_skip: %.1f км/ч (порог %.0f км/ч)' % (wind_value, threshold) if exceeds else detail)

    def is_enabled(self):
        # type: () -> bool
        """Check if weather adjustment is enabled."""
        return self._get_settings().get('enabled', False)

    def should_skip(self):
        # type: () -> Dict[str, Any]
        """Determine if watering should be skipped entirely."""
        result = {'skip': False, 'reason': '', 'details': {}}
        settings = self._get_settings()
        if not settings.get('enabled'):
            return result

        weather = self._get_weather()
        if not weather:
            result['details']['api_unavailable'] = True
            return result

        # Rain skip
        if settings.get('factor_rain', True):
            rain_threshold = settings.get('rain_threshold_mm', self.DEFAULT_RAIN_THRESHOLD_MM)
            rain_24h = weather.precipitation_24h or 0.0
            rain_forecast = weather.precipitation_forecast_6h or 0.0

            if rain_24h > rain_threshold:
                result['skip'] = True
                result['reason'] = 'rain_skip: %.1fmm за 24ч (порог %.0fmm)' % (rain_24h, rain_threshold)
                result['details'] = {'type': 'rain', 'value': rain_24h, 'threshold': rain_threshold}
                return result

            if rain_forecast > rain_threshold:
                result['skip'] = True
                result['reason'] = 'rain_forecast_skip: прогноз %.1fmm за 6ч (порог %.0fmm)' % (rain_forecast, rain_threshold)
                result['details'] = {'type': 'rain_forecast', 'value': rain_forecast, 'threshold': rain_threshold}
                return result

        # Freeze skip
        if settings.get('factor_freeze', True):
            freeze_threshold = settings.get('freeze_threshold_c', self.DEFAULT_FREEZE_THRESHOLD_C)
            temp = weather.temperature

            if temp is not None and temp < freeze_threshold:
                result['skip'] = True
                result['reason'] = 'freeze_skip: %.1f°C (порог %.0f°C)' % (temp, freeze_threshold)
                result['details'] = {'type': 'freeze', 'value': temp, 'threshold': freeze_threshold}
                return result

            min_temp_6h = getattr(weather, 'min_temp_forecast_6h', None)
            if min_temp_6h is not None and isinstance(min_temp_6h, (int, float)) and min_temp_6h < freeze_threshold:
                result['skip'] = True
                result['reason'] = 'freeze_forecast_skip: прогноз мин %.1f°C за 6ч (порог %.0f°C)' % (min_temp_6h, freeze_threshold)
                result['details'] = {'type': 'freeze_forecast', 'value': min_temp_6h, 'threshold': freeze_threshold}
                return result

        # Wind postpone
        if settings.get('factor_wind', True):
            wind = weather.wind_speed
            exceeds, reason_str = self._get_wind_check(settings, wind)
            if exceeds:
                result['skip'] = True
                result['reason'] = reason_str
                threshold = self._get_wind_threshold_ms(settings) if self._has_ms_threshold() else settings.get('wind_threshold_kmh', self.DEFAULT_WIND_THRESHOLD_KMH)
                result['details'] = {'type': 'wind', 'value': wind, 'threshold': threshold}
                return result

        return result

    def get_coefficient(self):
        # type: () -> int
        """Calculate watering adjustment coefficient (0-200%)."""
        settings = self._get_settings()
        if not settings.get('enabled'):
            return 100

        weather = self._get_weather()
        if not weather:
            return 100

        base = 100

        # Temperature factor (Zimmerman-style)
        temp = weather.temperature
        temp_factor = 1.0
        if temp is not None and settings.get('factor_heat', True):
            if temp > 35:
                temp_factor = 1.5
            elif temp > 30:
                temp_factor = 1.25
            elif temp > 25:
                temp_factor = 1.1
            elif temp < 5:
                temp_factor = 0.3
            elif temp < 10:
                temp_factor = 0.5
            elif temp < 15:
                temp_factor = 0.7
            elif temp < 20:
                temp_factor = 0.85

        # Humidity factor (Zimmerman-style)
        hum = weather.humidity
        humidity_factor = 1.0
        if hum is not None:
            if hum > 90:
                humidity_factor = 0.5
            elif hum > 80:
                humidity_factor = 0.7
            elif hum > 70:
                humidity_factor = 0.85
            elif hum < 30:
                humidity_factor = 1.2
            elif hum < 40:
                humidity_factor = 1.1

        # Additional humidity threshold reduction
        if hum is not None and settings.get('factor_humidity', True):
            hum_threshold = settings.get('humidity_threshold_pct', self.DEFAULT_HUMIDITY_THRESHOLD_PCT)
            hum_reduction = settings.get('humidity_reduction_pct', self.DEFAULT_HUMIDITY_REDUCTION_PCT)
            if hum > hum_threshold:
                humidity_factor = humidity_factor * (1.0 - hum_reduction / 100.0)

        # Rain factor
        rain_24h = weather.precipitation_24h or 0.0
        rain_factor = 1.0
        rain_threshold = settings.get('rain_threshold_mm', self.DEFAULT_RAIN_THRESHOLD_MM)
        if rain_24h > 0 and settings.get('factor_rain', True):
            ratio = rain_24h / rain_threshold
            if ratio >= 1.0:
                rain_factor = 0.0
            else:
                rain_factor = max(0.3, 1.0 - ratio * 0.7)

        # Wind factor
        wind = weather.wind_speed
        wind_factor = 1.0
        if wind is not None and settings.get('factor_wind', True):
            if self._has_ms_threshold():
                if wind > 4.2:
                    wind_factor = 1.1
                elif wind > 2.8:
                    wind_factor = 1.05
            else:
                if wind > 15:
                    wind_factor = 1.1
                elif wind > 10:
                    wind_factor = 1.05

        # ET₀ factor
        et0_factor = 1.0
        daily_et0 = weather.daily_et0
        if daily_et0 is not None:
            ref_et0 = 4.5
            if daily_et0 > 0:
                et0_ratio = daily_et0 / ref_et0
                et0_factor = 0.5 + 0.5 * min(2.0, et0_ratio)

        coefficient = base * temp_factor * humidity_factor * rain_factor * wind_factor * et0_factor
        result = max(0, min(200, int(round(coefficient))))
        return result

    def get_factors_detail(self, weather=None):
        # type: (Any) -> Dict[str, Dict[str, str]]
        """Return per-factor breakdown for the weather widget."""
        settings = self._get_settings()
        if weather is None:
            weather = self._get_weather()

        result = {}  # type: Dict[str, Dict[str, str]]

        # Rain factor
        rain_enabled = settings.get('factor_rain', True)
        rain_threshold = settings.get('rain_threshold_mm', self.DEFAULT_RAIN_THRESHOLD_MM)
        rain_24h = 0.0
        rain_forecast = 0.0
        if weather:
            rain_24h = weather.precipitation_24h or 0.0
            rain_forecast = weather.precipitation_forecast_6h or 0.0

        rain_status = 'ok'
        rain_detail = '%.1f мм < %.0f мм' % (rain_24h, rain_threshold)
        if rain_24h > rain_threshold:
            rain_status = 'danger'
            rain_detail = '%.1f мм > %.0f мм (skip)' % (rain_24h, rain_threshold)
        elif rain_24h > rain_threshold * 0.5:
            rain_status = 'warn'
            rain_detail = '%.1f мм (прогноз +%.1f мм)' % (rain_24h, rain_forecast)

        result['rain'] = {'status': rain_status, 'detail': rain_detail, 'enabled': rain_enabled}

        # Freeze factor
        freeze_enabled = settings.get('factor_freeze', True)
        freeze_threshold = settings.get('freeze_threshold_c', self.DEFAULT_FREEZE_THRESHOLD_C)
        temp = weather.temperature if weather else None
        _raw_min_6h = getattr(weather, 'min_temp_forecast_6h', None) if weather else None
        min_temp_6h = _raw_min_6h if isinstance(_raw_min_6h, (int, float)) else None

        freeze_status = 'ok'
        if temp is not None:
            if temp < freeze_threshold:
                freeze_status = 'danger'
                freeze_detail = '%.1f°C < %.0f°C (skip)' % (temp, freeze_threshold)
            elif min_temp_6h is not None and min_temp_6h < freeze_threshold:
                freeze_status = 'danger'
                freeze_detail = 'прогноз мин %.1f°C за 6ч (skip)' % min_temp_6h
            elif min_temp_6h is not None and min_temp_6h < freeze_threshold + 3:
                freeze_status = 'warn'
                freeze_detail = 'мин %.1f°C за 6ч (близко к порогу)' % min_temp_6h
            else:
                if min_temp_6h is not None:
                    freeze_detail = 'мин +%.1f°C за 6ч' % min_temp_6h
                else:
                    freeze_detail = '+%.1f°C — норма' % temp
        else:
            freeze_detail = 'нет данных'

        result['freeze'] = {'status': freeze_status, 'detail': freeze_detail, 'enabled': freeze_enabled}

        # Wind factor
        wind_enabled = settings.get('factor_wind', True)
        wind = weather.wind_speed if weather else None
        use_ms = self._has_ms_threshold()

        wind_status = 'ok'
        if wind is not None:
            if use_ms:
                wind_thr = self._get_wind_threshold_ms(settings)
                unit = 'м/с'
            else:
                wind_thr = float(settings.get('wind_threshold_kmh', self.DEFAULT_WIND_THRESHOLD_KMH))
                unit = 'км/ч'
            if wind > wind_thr:
                wind_status = 'danger'
                wind_detail = '%.1f %s > %.1f %s (skip)' % (wind, unit, wind_thr, unit)
            elif wind > wind_thr * 0.7:
                wind_status = 'warn'
                wind_detail = '%.1f %s (близко к порогу)' % (wind, unit)
            else:
                wind_detail = '%.1f %s < %.1f %s' % (wind, unit, wind_thr, unit)
        else:
            wind_detail = 'нет данных'

        result['wind'] = {'status': wind_status, 'detail': wind_detail, 'enabled': wind_enabled}

        # Humidity factor
        hum_enabled = settings.get('factor_humidity', True)
        hum_threshold = settings.get('humidity_threshold_pct', self.DEFAULT_HUMIDITY_THRESHOLD_PCT)
        hum = weather.humidity if weather else None

        hum_status = 'ok'
        if hum is not None:
            if hum > hum_threshold:
                hum_status = 'warn'
                hum_detail = '%.0f%% > %.0f%% (коэфф. снижен)' % (hum, hum_threshold)
            else:
                hum_detail = '%.0f%% < %.0f%%' % (hum, hum_threshold)
        else:
            hum_detail = 'нет данных'

        result['humidity'] = {'status': hum_status, 'detail': hum_detail, 'enabled': hum_enabled}

        # Heat factor
        heat_enabled = settings.get('factor_heat', True)
        heat_status = 'ok'
        if temp is not None:
            if temp > 35:
                heat_status = 'danger'
                heat_detail = '+%.0f°C — жара (коэфф. ×1.5)' % temp
            elif temp > 30:
                heat_status = 'warn'
                heat_detail = '+%.0f°C — жарко (коэфф. ×1.25)' % temp
            elif temp > 25:
                heat_status = 'ok'
                heat_detail = '+%.0f°C — тепло' % temp
            else:
                heat_detail = '+%.0f°C — норма' % temp
        else:
            heat_detail = 'нет данных'

        result['heat'] = {'status': heat_status, 'detail': heat_detail, 'enabled': heat_enabled}

        return result

    def adjust_duration(self, base_duration_min):
        # type: (int) -> int
        """Adjust zone watering duration based on weather coefficient."""
        coeff = self.get_coefficient()
        adjusted = int(round(base_duration_min * coeff / 100.0))
        return max(1, adjusted) if adjusted > 0 else 0

    def log_adjustment(self, zone_id, original_duration,
                       adjusted_duration, coefficient,
                       skip, reason=''):
        # type: (int, int, int, int, bool, str) -> None
        """Log weather adjustment to weather_log table."""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute(
                    'INSERT INTO weather_log '
                    '(zone_id, original_duration, adjusted_duration, coefficient, '
                    'skipped, skip_reason, weather_data, created_at) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, datetime("now"))',
                    (zone_id, original_duration, adjusted_duration, coefficient,
                     1 if skip else 0, reason, '{}'),
                )
                conn.commit()
        except (sqlite3.Error, OSError) as e:
            logger.debug("Weather log write error: %s", e)


# ===================================================================
# WeatherService — API integration + caching
# ===================================================================

class WeatherService:
    """Fetches and caches weather data from Open-Meteo."""

    def __init__(self, db_path='irrigation.db'):
        # type: (str) -> None
        self.db_path = db_path

    def _get_location(self):
        # type: () -> Optional[Dict[str, float]]
        """Get lat/lon from settings (thin delegate to ``cache.get_location``)."""
        return _cache.get_location(self.db_path)

    def _get_cached(self, lat, lon):
        # type: (float, float) -> Optional[WeatherData]
        """Return cached weather data if still fresh (delegates to ``cache.read_fresh``)."""
        return _cache.read_fresh(self.db_path, lat, lon)

    def _save_cache(self, lat, lon, data):
        # type: (float, float, Dict[str, Any]) -> None
        """Save weather data to cache (delegates to ``cache.save``)."""
        _cache.save(self.db_path, lat, lon, data)

    def _fetch_api(self, lat, lon):
        # type: (float, float) -> Optional[Dict[str, Any]]
        """Fetch weather data from Open-Meteo API (delegates to ``client.fetch_api``).

        Kept as an instance method (rather than a free function call) so that
        test code can ``@patch('services.weather.WeatherService._fetch_api')``.
        """
        return _fetch_api_impl(lat, lon)

    def get_weather(self, force_refresh=False):
        # type: (bool) -> Optional[WeatherData]
        """Get current weather data (cached or fresh).

        Order of fallback:
            1. Fresh cache (age < _CACHE_TTL_SEC), unless ``force_refresh``.
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

    def get_weather_summary(self):
        # type: () -> Dict[str, Any]
        """Get weather summary for dashboard display."""
        weather = self.get_weather()
        if not weather:
            return {'available': False}

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
        """Get extended weather data for the new weather widget."""
        weather = self.get_weather()
        if not weather:
            return {'available': False}

        from services.weather_codes import get_weather_icon, get_weather_desc

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


# ===================================================================
# Merged Weather — combines local MQTT sensors with Open-Meteo API
# ===================================================================

def get_merged_weather(db_path):
    # type: (str) -> Dict[str, Any]
    """Merge local sensor data with Open-Meteo API data.

    Local sensors (EnvMonitor temp/hum, RainMonitor) take priority when
    they are enabled and have fresh data (< SENSOR_STALE_TIMEOUT seconds old).
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


# ===================================================================
# Module-level singletons
# ===================================================================

_weather_service = None  # type: Optional[WeatherService]
_adjustment = None  # type: Optional[WeatherAdjustment]


def get_weather_service(db_path='irrigation.db'):
    # type: (str) -> WeatherService
    """Get or create the weather service singleton."""
    global _weather_service
    if _weather_service is None:
        _weather_service = WeatherService(db_path)
    return _weather_service


def get_weather_adjustment(db_path='irrigation.db'):
    # type: (str) -> WeatherAdjustment
    """Get or create the weather adjustment singleton."""
    global _adjustment
    if _adjustment is None:
        _adjustment = WeatherAdjustment(db_path)
    return _adjustment