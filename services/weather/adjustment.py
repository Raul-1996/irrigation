"""Weather-based watering adjustment — skip rules + coefficient calculation.

Single responsibility: given current weather (from ``WeatherService``) and
user-configurable thresholds (from the ``settings`` table), decide
(a) whether to skip watering entirely and (b) what multiplier (0–200%) to
apply to the base zone duration.

Algorithm: hybrid Zimmerman method (from OpenSprinkler) + ET₀ (FAO-56).

NOTE(wave4, CQ-015): direct ``sqlite3.connect(self.db_path, timeout=5)``
calls in ``_get_settings`` / ``_has_ms_threshold`` / ``log_adjustment`` are
preserved from the pre-split implementation. Migrating to
``db.SettingsRepository`` is tracked as follow-up (see
``irrigation-audit/findings/code-quality.md`` CQ-015).
"""
import logging
import sqlite3
from typing import Any, Dict, Optional, Tuple

from services.weather.singletons import get_weather_service

logger = logging.getLogger(__name__)


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

    def __init__(self, db_path: str = 'irrigation.db') -> None:
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
        # type: (Dict[str, Any], Optional[float]) -> Tuple[bool, str]
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

    def is_enabled(self) -> bool:
        """Check if weather adjustment is enabled."""
        return self._get_settings().get('enabled', False)

    def should_skip(self) -> Dict[str, Any]:
        """Determine if watering should be skipped entirely.

        Returns a dict with keys ``skip`` (bool), ``reason`` (human-readable
        string) and ``details`` (dict with ``type``/``value``/``threshold``).
        """
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

    def get_coefficient(self) -> int:
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

    def get_factors_detail(self, weather: Optional[Any] = None) -> Dict[str, Dict[str, Any]]:
        """Return per-factor breakdown for the weather widget.

        Args:
            weather: Optional pre-fetched ``WeatherData``; loaded on demand if ``None``.

        Returns:
            Mapping of factor name (``rain``/``freeze``/``wind``/``humidity``/``heat``)
            to a ``{'status', 'detail', 'enabled'}`` dict. ``status`` is one of
            ``'ok'`` / ``'warn'`` / ``'danger'``.
        """
        settings = self._get_settings()
        if weather is None:
            weather = self._get_weather()

        result = {}  # type: Dict[str, Dict[str, Any]]

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

    def adjust_duration(self, base_duration_min: int) -> int:
        """Adjust zone watering duration based on weather coefficient."""
        coeff = self.get_coefficient()
        adjusted = int(round(base_duration_min * coeff / 100.0))
        return max(1, adjusted) if adjusted > 0 else 0

    def log_adjustment(
        self,
        zone_id: int,
        original_duration: int,
        adjusted_duration: int,
        coefficient: int,
        skip: bool,
        reason: str = '',
    ) -> None:
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
