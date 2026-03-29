"""Weather-based watering adjustment engine.

Hybrid approach: Zimmerman method (simple, proven from OpenSprinkler) + ET₀ from Open-Meteo.
Calculates a watering coefficient (0-200%) and skip conditions (rain, freeze, wind).
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
    DEFAULT_WIND_THRESHOLD_KMH = 25.0     # km/h → postpone
    DEFAULT_BASELINE_TEMP_C = 25.0        # Reference temperature for Zimmerman
    DEFAULT_BASELINE_HUM_PCT = 50.0       # Reference humidity for Zimmerman

    def __init__(self, db_path: str = 'irrigation.db'):
        self.db_path = db_path

    def _get_settings(self) -> Dict[str, Any]:
        """Load weather adjustment settings from DB."""
        defaults = {
            'enabled': False,
            'rain_threshold_mm': self.DEFAULT_RAIN_THRESHOLD_MM,
            'freeze_threshold_c': self.DEFAULT_FREEZE_THRESHOLD_C,
            'wind_threshold_kmh': self.DEFAULT_WIND_THRESHOLD_KMH,
        }
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                keys = [
                    'weather.enabled',
                    'weather.rain_threshold_mm',
                    'weather.freeze_threshold_c',
                    'weather.wind_threshold_kmh',
                ]
                for key in keys:
                    cur = conn.execute('SELECT value FROM settings WHERE key = ?', (key,))
                    row = cur.fetchone()
                    if row and row['value'] is not None:
                        short_key = key.replace('weather.', '')
                        val = row['value']
                        if short_key == 'enabled':
                            defaults[short_key] = str(val) in ('1', 'true', 'True')
                        else:
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

    def is_enabled(self) -> bool:
        """Check if weather adjustment is enabled."""
        return self._get_settings().get('enabled', False)

    def should_skip(self) -> Dict[str, Any]:
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
        rain_threshold = settings.get('rain_threshold_mm', self.DEFAULT_RAIN_THRESHOLD_MM)
        rain_24h = weather.precipitation_24h or 0.0
        rain_forecast = weather.precipitation_forecast_6h or 0.0

        if rain_24h > rain_threshold:
            result['skip'] = True
            result['reason'] = f'rain_skip: {rain_24h:.1f}mm за 24ч (порог {rain_threshold:.0f}mm)'
            result['details'] = {'type': 'rain', 'value': rain_24h, 'threshold': rain_threshold}
            return result

        if rain_forecast > rain_threshold:
            result['skip'] = True
            result['reason'] = f'rain_forecast_skip: прогноз {rain_forecast:.1f}mm за 6ч (порог {rain_threshold:.0f}mm)'
            result['details'] = {'type': 'rain_forecast', 'value': rain_forecast, 'threshold': rain_threshold}
            return result

        # Freeze skip: temp < threshold
        freeze_threshold = settings.get('freeze_threshold_c', self.DEFAULT_FREEZE_THRESHOLD_C)
        temp = weather.temperature
        if temp is not None and temp < freeze_threshold:
            result['skip'] = True
            result['reason'] = f'freeze_skip: {temp:.1f}°C (порог {freeze_threshold:.0f}°C)'
            result['details'] = {'type': 'freeze', 'value': temp, 'threshold': freeze_threshold}
            return result

        # Wind postpone: wind > threshold
        wind_threshold = settings.get('wind_threshold_kmh', self.DEFAULT_WIND_THRESHOLD_KMH)
        wind = weather.wind_speed
        if wind is not None and wind > wind_threshold:
            result['skip'] = True
            result['reason'] = f'wind_skip: {wind:.1f} км/ч (порог {wind_threshold:.0f} км/ч)'
            result['details'] = {'type': 'wind', 'value': wind, 'threshold': wind_threshold}
            return result

        return result

    def get_coefficient(self) -> int:
        """Calculate watering adjustment coefficient (0-200%).

        Uses hybrid Zimmerman + ET₀ approach:
        - Temperature factor: hotter → more water
        - Humidity factor: more humid → less water
        - Rain factor: recent rain → less water
        - ET₀ factor: higher evapotranspiration → more water (if available)
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
        if temp is not None:
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

        # --- Humidity factor ---
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

        # --- Rain factor ---
        rain_24h = weather.precipitation_24h or 0.0
        rain_factor = 1.0
        rain_threshold = settings.get('rain_threshold_mm', self.DEFAULT_RAIN_THRESHOLD_MM)
        if rain_24h > 0:
            # Proportional reduction based on rain amount
            ratio = rain_24h / rain_threshold
            if ratio >= 1.0:
                rain_factor = 0.0  # Should be caught by should_skip()
            else:
                rain_factor = max(0.3, 1.0 - ratio * 0.7)

        # --- Wind factor ---
        wind = weather.wind_speed
        wind_factor = 1.0
        if wind is not None:
            # Moderate wind increases evaporation slightly
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

    def adjust_duration(self, base_duration_min: int) -> int:
        """Adjust zone watering duration based on weather coefficient.

        Args:
            base_duration_min: Original duration in minutes

        Returns:
            Adjusted duration in minutes (minimum 1 minute if not skipped)
        """
        coeff = self.get_coefficient()
        adjusted = int(round(base_duration_min * coeff / 100.0))
        return max(1, adjusted) if adjusted > 0 else 0

    def log_adjustment(self, zone_id: int, original_duration: int,
                       adjusted_duration: int, coefficient: int,
                       skip: bool, reason: str = '') -> None:
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
_adjustment: Optional[WeatherAdjustment] = None


def get_weather_adjustment(db_path: str = 'irrigation.db') -> WeatherAdjustment:
    """Get or create the weather adjustment singleton."""
    global _adjustment
    if _adjustment is None:
        _adjustment = WeatherAdjustment(db_path)
    return _adjustment
