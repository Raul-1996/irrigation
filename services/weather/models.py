"""Domain models + module-level constants for the weather package.

Single responsibility: parse an Open-Meteo JSON response (``dict``) into the
``WeatherData`` structure consumed by downstream code (adjustment engine,
merge layer, HTTP views). No I/O, no DB, no HTTP — pure transformation.

Constants live here (rather than in a separate ``constants.py``) because the
parser and every other submodule consume the same values, and splitting a
ten-line constants module would add import churn without clarity.
"""
import logging
from datetime import datetime
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module constants (consumed by submodules; re-exported via package __init__)
# ---------------------------------------------------------------------------

# Open-Meteo API (free, no key required)
_OPEN_METEO_URL = 'https://api.open-meteo.com/v1/forecast'
_CACHE_TTL_SEC = 30 * 60  # 30 minutes
_REQUEST_TIMEOUT = 10  # seconds

# Day-of-week names in Russian (Mon=0 ... Sun=6)
_DAY_NAMES_RU = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']

# Sensor data older than this is considered stale (for merged weather)
SENSOR_STALE_TIMEOUT = 600  # 10 minutes


# ===================================================================
# WeatherData — parsed API response
# ===================================================================

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
        # Open-Meteo hourly.time is in the location's local timezone.
        # We must compute "now" in that same timezone, otherwise idx points to
        # the wrong hour and precipitation_24h sums the wrong window.
        utc_offset = self.raw.get('utc_offset_seconds')
        if utc_offset is not None:
            now = datetime.utcfromtimestamp(time.time() + int(utc_offset))
        else:
            logger.warning("WeatherData: utc_offset_seconds missing, falling back to server-local time")
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

        # Current weather code (WMO)
        self.weather_code = _safe_get_int(hourly, 'weather_code', idx)

        # Daily values (today = index 0)
        self.daily_precipitation = _safe_get(daily, 'precipitation_sum', 0)
        self.daily_et0 = _safe_get(daily, 'et0_fao_evapotranspiration', 0)

        # Daily temperature min/max (today)
        self.temperature_min = _safe_get(daily, 'temperature_2m_min', 0)
        self.temperature_max = _safe_get(daily, 'temperature_2m_max', 0)

        # Sunrise/sunset (today, as strings like "06:28")
        daily_sunrise = daily.get('sunrise', [])
        daily_sunset = daily.get('sunset', [])
        self.sunrise = None
        self.sunset = None
        if daily_sunrise and len(daily_sunrise) > 0 and daily_sunrise[0]:
            try:
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

        # Hourly forecast for next 24h (every 4 hours, 6 points)
        self.hourly_forecast_24h = []  # type: List[Dict[str, Any]]
        if idx is not None:
            hour_times = hourly.get('time', [])
            temp_arr = hourly.get('temperature_2m', [])
            precip_arr = hourly.get('precipitation', [])
            wind_arr = hourly.get('wind_speed_10m', [])
            wcode_arr = hourly.get('weather_code', [])
            current_h = now.hour
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
            self.hourly_forecast_24h = self.hourly_forecast_24h[:6]

        # Daily forecast (3 days)
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

        # Min temperature in next 6h (for freeze forecast check)
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
            'weather_code': self.weather_code,
            'temperature_min': self.temperature_min,
            'temperature_max': self.temperature_max,
            'sunrise': self.sunrise,
            'sunset': self.sunset,
            'hourly_forecast_24h': self.hourly_forecast_24h,
            'daily_forecast': self.daily_forecast,
            'min_temp_forecast_6h': self.min_temp_forecast_6h,
        }
