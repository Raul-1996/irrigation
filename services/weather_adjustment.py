"""Weather-based watering adjustment engine.

Hybrid approach: Zimmerman method (simple, proven from OpenSprinkler) + ET₀ from Open-Meteo.
Calculates a watering coefficient (0-200%) and skip conditions (rain, freeze, wind).

Extended in v2: humidity factor, freeze forecast 6h, wind in m/s,
per-factor toggles, get_factors_detail() for widget.
"""
import logging
import sqlite3
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class WeatherAdjustment:
    """Calculate watering adjustment based on weather conditions."""

    # Default thresholds
    DEFAULT_RAIN_THRESHOLD_MM = 5.0       # mm in 24h or forecast 6h → skip
    DEFAULT_FREEZE_THRESHOLD_C = 2.0      # °C → skip
    DEFAULT_WIND_THRESHOLD_KMH = 25.0     # km/h → postpone (legacy)
    DEFAULT_WIND_THRESHOLD_MS = 7.0       # m/s → postpone (new, ~25 km/h)
    DEFAULT_HUMIDITY_THRESHOLD_PCT = 80.0  # % → reduce coefficient
    DEFAULT_HUMIDITY_REDUCTION_PCT = 30    # % reduction when humidity above threshold
    DEFAULT_BASELINE_TEMP_C = 25.0        # Reference temperature for Zimmerman
    DEFAULT_BASELINE_HUM_PCT = 50.0       # Reference humidity for Zimmerman

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
            # Per-factor toggles (default: all enabled)
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
            from services.weather import get_weather_service
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
        """Get wind threshold in m/s.

        If weather.wind_threshold_ms is explicitly in DB, use it.
        Otherwise convert from weather.wind_threshold_kmh.
        """
        if self._has_ms_threshold():
            ms_val = settings.get('wind_threshold_ms', self.DEFAULT_WIND_THRESHOLD_MS)
            return float(ms_val)
        # Fallback: convert from km/h for backward compat
        kmh_val = settings.get('wind_threshold_kmh', self.DEFAULT_WIND_THRESHOLD_KMH)
        return round(float(kmh_val) / 3.6, 1)

    def _get_wind_check(self, settings, wind_value):
        # type: (Dict[str, Any], Optional[float]) -> tuple
        """Check if wind exceeds threshold.

        Returns (exceeds: bool, detail_str: str).
        When wind_threshold_ms is in DB, compares in m/s.
        Otherwise compares in km/h (backward compat with old data).
        """
        if wind_value is None:
            return (False, '')
        if self._has_ms_threshold():
            threshold = float(settings.get('wind_threshold_ms', self.DEFAULT_WIND_THRESHOLD_MS))
            exceeds = wind_value > threshold
            detail = '%.1f м/с > %.1f м/с' % (wind_value, threshold) if exceeds else '%.1f м/с < %.1f м/с' % (wind_value, threshold)
            return (exceeds, 'wind_skip: %.1f м/с (порог %.1f м/с)' % (wind_value, threshold) if exceeds else detail)
        else:
            # Legacy km/h comparison
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
        """Determine if watering should be skipped entirely.

        Returns:
            dict with 'skip' (bool), 'reason' (str), 'details' (dict)
        """
        result = {'skip': False, 'reason': '', 'details': {}}
        settings = self._get_settings()
        if not settings.get('enabled'):
            return result

        weather = self._get_weather()
        if not weather:
            # API unavailable — don't skip, water normally
            result['details']['api_unavailable'] = True
            return result

        # Rain skip: 24h actual > threshold OR 6h forecast > threshold
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

        # Freeze skip: temp < threshold OR forecast min temp 6h < threshold
        if settings.get('factor_freeze', True):
            freeze_threshold = settings.get('freeze_threshold_c', self.DEFAULT_FREEZE_THRESHOLD_C)
            temp = weather.temperature

            if temp is not None and temp < freeze_threshold:
                result['skip'] = True
                result['reason'] = 'freeze_skip: %.1f°C (порог %.0f°C)' % (temp, freeze_threshold)
                result['details'] = {'type': 'freeze', 'value': temp, 'threshold': freeze_threshold}
                return result

            # NEW: check forecast min temperature for next 6h
            min_temp_6h = getattr(weather, 'min_temp_forecast_6h', None)
            if min_temp_6h is not None and isinstance(min_temp_6h, (int, float)) and min_temp_6h < freeze_threshold:
                result['skip'] = True
                result['reason'] = 'freeze_forecast_skip: прогноз мин %.1f°C за 6ч (порог %.0f°C)' % (min_temp_6h, freeze_threshold)
                result['details'] = {'type': 'freeze_forecast', 'value': min_temp_6h, 'threshold': freeze_threshold}
                return result

        # Wind postpone: wind > threshold
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
        """Calculate watering adjustment coefficient (0-200%).

        Uses hybrid Zimmerman + ET₀ approach:
        - Temperature factor: hotter → more water
        - Humidity factor: more humid → less water
        - Rain factor: recent rain → less water
        - Wind factor: moderate wind increases evaporation
        - ET₀ factor: higher evapotranspiration → more water (if available)
        - Humidity threshold factor: high humidity → reduce coefficient
        """
        settings = self._get_settings()
        if not settings.get('enabled'):
            return 100  # No adjustment

        weather = self._get_weather()
        if not weather:
            return 100  # API unavailable — water normally

        # Base coefficient
        base = 100

        # --- Temperature factor (Zimmerman-style) ---
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

        # --- Humidity factor (Zimmerman-style) ---
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

        # NEW: additional humidity threshold reduction
        if hum is not None and settings.get('factor_humidity', True):
            hum_threshold = settings.get('humidity_threshold_pct', self.DEFAULT_HUMIDITY_THRESHOLD_PCT)
            hum_reduction = settings.get('humidity_reduction_pct', self.DEFAULT_HUMIDITY_REDUCTION_PCT)
            if hum > hum_threshold:
                # Apply additional reduction as a multiplier
                humidity_factor = humidity_factor * (1.0 - hum_reduction / 100.0)

        # --- Rain factor ---
        rain_24h = weather.precipitation_24h or 0.0
        rain_factor = 1.0
        rain_threshold = settings.get('rain_threshold_mm', self.DEFAULT_RAIN_THRESHOLD_MM)
        if rain_24h > 0 and settings.get('factor_rain', True):
            # Proportional reduction based on rain amount
            ratio = rain_24h / rain_threshold
            if ratio >= 1.0:
                rain_factor = 0.0  # Should be caught by should_skip()
            else:
                rain_factor = max(0.3, 1.0 - ratio * 0.7)

        # --- Wind factor ---
        wind = weather.wind_speed
        wind_factor = 1.0
        if wind is not None and settings.get('factor_wind', True):
            # Moderate wind increases evaporation slightly
            if self._has_ms_threshold():
                # Wind in m/s: 4.2 m/s ≈ 15 km/h, 2.8 m/s ≈ 10 km/h
                if wind > 4.2:
                    wind_factor = 1.1
                elif wind > 2.8:
                    wind_factor = 1.05
            else:
                # Legacy: wind in km/h
                if wind > 15:
                    wind_factor = 1.1
                elif wind > 10:
                    wind_factor = 1.05

        # --- ET₀ factor (scientific adjustment if available) ---
        et0_factor = 1.0
        daily_et0 = weather.daily_et0
        if daily_et0 is not None:
            # Reference ET₀ ≈ 4-5 mm/day for moderate climate
            # Higher ET₀ means more evaporation → need more water
            ref_et0 = 4.5
            if daily_et0 > 0:
                et0_ratio = daily_et0 / ref_et0
                # Blend ET₀ with Zimmerman (50/50 weight)
                et0_factor = 0.5 + 0.5 * min(2.0, et0_ratio)

        # Combine factors
        coefficient = base * temp_factor * humidity_factor * rain_factor * wind_factor * et0_factor

        # Clamp to 0-200%
        result = max(0, min(200, int(round(coefficient))))
        return result

    def get_factors_detail(self, weather=None):
        # type: (Any) -> Dict[str, Dict[str, str]]
        """Return per-factor breakdown for the weather widget.

        Each factor has:
        - status: 'ok' | 'warn' | 'danger'
        - detail: human-readable description (Russian)
        - enabled: whether the factor is active

        Args:
            weather: Optional WeatherData instance. If None, fetches fresh data.
        """
        settings = self._get_settings()
        if weather is None:
            weather = self._get_weather()

        result = {}  # type: Dict[str, Dict[str, str]]

        # --- Rain factor ---
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

        # --- Freeze factor ---
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

        # --- Wind factor ---
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

        # --- Humidity factor ---
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

        # --- Heat factor ---
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
        """Adjust zone watering duration based on weather coefficient.

        Args:
            base_duration_min: Original duration in minutes

        Returns:
            Adjusted duration in minutes (minimum 1 minute if not skipped)
        """
        coeff = self.get_coefficient()
        adjusted = int(round(base_duration_min * coeff / 100.0))
        return max(1, adjusted) if adjusted > 0 else 0

    def log_adjustment(self, zone_id, original_duration,
                       adjusted_duration, coefficient,
                       skip, reason=''):
        # type: (int, int, int, int, bool, str) -> None
        """Log weather adjustment to weather_log table."""
        try:
            import json
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


# Module-level singleton
_adjustment = None  # type: Optional[WeatherAdjustment]


def get_weather_adjustment(db_path='irrigation.db'):
    # type: (str) -> WeatherAdjustment
    """Get or create the weather adjustment singleton."""
    global _adjustment
    if _adjustment is None:
        _adjustment = WeatherAdjustment(db_path)
    return _adjustment
