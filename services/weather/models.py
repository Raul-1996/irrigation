"""Domain models + module-level constants for the weather package.

Single responsibility: parse an Open-Meteo JSON response (``dict``) into the
``WeatherData`` structure consumed by downstream code (adjustment engine,
merge layer, HTTP views). No I/O, no DB, no HTTP — pure transformation.

Constants live here (rather than in a separate ``constants.py``) because the
parser and every other submodule consume the same values, and splitting a
ten-line constants module would add import churn without clarity.
"""

import logging
import math
import re
import time
from datetime import datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module constants (consumed by submodules; re-exported via package __init__)
# ---------------------------------------------------------------------------

# Open-Meteo API (free, no key required)
_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_CACHE_TTL_SEC = 30 * 60  # 30 minutes
_REQUEST_TIMEOUT = 10  # seconds

# Day-of-week names in Russian (Mon=0 ... Sun=6)
_DAY_NAMES_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Sensor data older than this is considered stale (for merged weather)
SENSOR_STALE_TIMEOUT = 600  # 10 minutes

_HH_MM_RE = re.compile(r"^\d{2}:\d{2}$")
_LOCAL_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$")


def _canonical_hh_mm(value: object) -> str | None:
    """Validate an API/relay time and return the only UI-safe representation."""
    if not isinstance(value, str):
        return None
    if _HH_MM_RE.fullmatch(value):
        formats = ("%H:%M",)
    elif _LOCAL_DATETIME_RE.fullmatch(value):
        formats = ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")
    else:
        return None
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return None


# ===================================================================
# WeatherData — parsed API response
# ===================================================================


class WeatherData:
    """Parsed weather data from Open-Meteo API."""

    def __init__(self, raw):
        # type: (Dict[str, Any]) -> None
        self.raw = raw
        now = time.time()
        self.timestamp = float(raw.get("_fetched_at", now))
        if not math.isfinite(self.timestamp) or self.timestamp > now:
            raise ValueError("weather fetched_at must not be in the future")
        self._parse()

    def _parse(self):
        """Extract current-hour values from hourly forecast data."""
        hourly = self.raw.get("hourly", {})
        daily = self.raw.get("daily", {})
        # Open-Meteo hourly.time is in the location's local timezone.
        # We must compute "now" in that same timezone, otherwise idx points to
        # the wrong hour and precipitation_24h sums the wrong window.
        utc_offset = self.raw.get("utc_offset_seconds")
        if utc_offset is not None:
            now = datetime.utcfromtimestamp(time.time() + int(utc_offset))
        else:
            logger.warning("WeatherData: utc_offset_seconds missing, falling back to server-local time")
            now = datetime.now()
        current_hour = now.strftime("%Y-%m-%dT%H:00")

        # Find current hour index
        times = hourly.get("time", [])
        idx = None
        for i, t in enumerate(times):
            if t == current_hour:
                idx = i
                break
        if idx is None and times:
            # Never reinterpret an arbitrary sample as "now".  This happens
            # with an expired relay/cache payload whose time range no longer
            # covers the controller's current hour.  Leaving idx=None makes the
            # payload unavailable to safety decisions instead of presenting
            # (usually midnight of an old day) as a live reading.
            logger.warning("WeatherData: current hour absent from payload")

        def _safe_get(data, key, index):
            try:
                arr = data.get(key, [])
                if arr and index is not None and index < len(arr):
                    val = arr[index]
                    number = float(val) if val is not None else None
                    return number if number is not None and math.isfinite(number) else None
            except (OverflowError, ValueError, TypeError, IndexError):
                pass
            return None

        def _safe_get_int(data, key, index):
            try:
                arr = data.get(key, [])
                if arr and index is not None and index < len(arr):
                    val = arr[index]
                    return int(val) if val is not None else None
            except (OverflowError, ValueError, TypeError, IndexError):
                pass
            return None

        self.temperature = _safe_get(hourly, "temperature_2m", idx)
        self.humidity = _safe_get(hourly, "relative_humidity_2m", idx)
        self.precipitation = _safe_get(hourly, "precipitation", idx)
        self.wind_speed = _safe_get(hourly, "wind_speed_10m", idx)
        self.et0_hourly = _safe_get(hourly, "et0_fao_evapotranspiration", idx)
        if self.humidity is not None and not 0.0 <= self.humidity <= 100.0:
            self.humidity = None
        for attribute in ("precipitation", "wind_speed", "et0_hourly"):
            value = getattr(self, attribute)
            if value is not None and value < 0:
                setattr(self, attribute, None)

        # Current weather code (WMO)
        self.weather_code = _safe_get_int(hourly, "weather_code", idx)

        # Daily values must be selected by the location-local date.  ``past_days``
        # deliberately puts yesterday at index 0, and a fresh cache may also be
        # parsed on the next side of midnight.
        today = now.strftime("%Y-%m-%d")
        daily_times = daily.get("time", [])
        daily_idx = next((i for i, value in enumerate(daily_times) if str(value) == today), None)

        self.daily_precipitation = _safe_get(daily, "precipitation_sum", daily_idx)
        self.daily_et0 = _safe_get(daily, "et0_fao_evapotranspiration", daily_idx)
        if self.daily_precipitation is not None and self.daily_precipitation < 0:
            self.daily_precipitation = None
        if self.daily_et0 is not None and self.daily_et0 < 0:
            self.daily_et0 = None

        # Daily temperature min/max (today)
        self.temperature_min = _safe_get(daily, "temperature_2m_min", daily_idx)
        self.temperature_max = _safe_get(daily, "temperature_2m_max", daily_idx)

        # Sunrise/sunset (today, as strings like "06:28")
        daily_sunrise = daily.get("sunrise", [])
        daily_sunset = daily.get("sunset", [])
        self.sunrise = None
        self.sunset = None
        if daily_idx is not None and daily_idx < len(daily_sunrise) and daily_sunrise[daily_idx]:
            self.sunrise = _canonical_hh_mm(daily_sunrise[daily_idx])
        if daily_idx is not None and daily_idx < len(daily_sunset) and daily_sunset[daily_idx]:
            self.sunset = _canonical_hh_mm(daily_sunset[daily_idx])

        # Calculate precipitation only when the payload covers every one of the
        # previous 24 contiguous hourly slots.  A partial relay/cache payload is
        # unavailable, never a misleading small "24h" value.
        self.precipitation_24h = None
        if idx is not None and idx >= 23:
            precip_arr = hourly.get("precipitation", [])
            start_idx = idx - 23
            window_times = times[start_idx : idx + 1]
            contiguous = len(window_times) == 24
            parsed_times = []
            if contiguous:
                for value in window_times:
                    try:
                        parsed_times.append(datetime.strptime(str(value), "%Y-%m-%dT%H:%M"))
                    except ValueError:
                        contiguous = False
                        break
            if contiguous:
                contiguous = all(
                    (parsed_times[i] - parsed_times[i - 1]).total_seconds() == 3600 for i in range(1, len(parsed_times))
                )
            values = []
            if contiguous and idx < len(precip_arr):
                for i in range(start_idx, idx + 1):
                    try:
                        value = float(precip_arr[i])
                        if not math.isfinite(value) or value < 0:
                            raise ValueError("invalid precipitation")
                        values.append(value)
                    except (IndexError, OverflowError, ValueError, TypeError):
                        values = []
                        break
            if len(values) == 24:
                total = sum(values)
                if math.isfinite(total):
                    self.precipitation_24h = total

        # Calculate precipitation forecast for next 6h
        self.precipitation_forecast_6h = None
        if idx is not None:
            precip_arr = hourly.get("precipitation", [])
            forecast_values = []
            for i in range(idx + 1, idx + 7):
                try:
                    val = precip_arr[i]
                    number = float(val)
                    if val is None or not math.isfinite(number) or number < 0:
                        raise ValueError("invalid precipitation forecast")
                    forecast_values.append(number)
                except (IndexError, OverflowError, ValueError, TypeError):
                    forecast_values = []
                    break
            if len(forecast_values) == 6:
                total = sum(forecast_values)
                if math.isfinite(total):
                    self.precipitation_forecast_6h = total

        # Hourly forecast for next 24h (every 4 hours, 6 points)
        self.hourly_forecast_24h = []  # type: List[Dict[str, Any]]
        if idx is not None:
            hour_times = hourly.get("time", [])
            temp_arr = hourly.get("temperature_2m", [])
            precip_arr = hourly.get("precipitation", [])
            wind_arr = hourly.get("wind_speed_10m", [])
            wcode_arr = hourly.get("weather_code", [])
            current_h = now.hour
            next_slot = current_h + (4 - current_h % 4) if current_h % 4 != 0 else current_h + 4
            for offset_h in range(0, 24, 4):
                target_h = next_slot + offset_h
                target_idx = idx + (target_h - current_h)
                if target_idx < 0 or target_idx >= len(hour_times):
                    continue
                hh_mm = _canonical_hh_mm(hour_times[target_idx])
                if hh_mm is None:
                    continue

                def _arr_val(arr, i, *, nonnegative=False):
                    try:
                        v = arr[i]
                        number = float(v) if v is not None else None
                        if number is None or not math.isfinite(number) or (nonnegative and number < 0):
                            return None
                        return number
                    except (IndexError, OverflowError, ValueError, TypeError):
                        return None

                def _arr_int(arr, i):
                    try:
                        v = arr[i]
                        return int(v) if v is not None else None
                    except (IndexError, ValueError, TypeError):
                        return None

                self.hourly_forecast_24h.append(
                    {
                        "time": hh_mm,
                        "temp": _arr_val(temp_arr, target_idx),
                        "precip": _arr_val(precip_arr, target_idx, nonnegative=True),
                        "wind": _arr_val(wind_arr, target_idx, nonnegative=True),
                        "weather_code": _arr_int(wcode_arr, target_idx),
                    }
                )
            self.hourly_forecast_24h = self.hourly_forecast_24h[:6]

        # Daily forecast (3 days)
        self.daily_forecast = []  # type: List[Dict[str, Any]]
        daily_tmin = daily.get("temperature_2m_min", [])
        daily_tmax = daily.get("temperature_2m_max", [])
        daily_psum = daily.get("precipitation_sum", [])
        daily_wcode = daily.get("weather_code", [])
        daily_sr = daily.get("sunrise", [])
        daily_ss = daily.get("sunset", [])

        daily_start = daily_idx if daily_idx is not None else len(daily_times)
        for di in range(daily_start, min(daily_start + 3, len(daily_times))):
            date_str = str(daily_times[di]) if di < len(daily_times) else ""
            day_name = ""
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                day_name = _DAY_NAMES_RU[dt.weekday()]
            except (ValueError, IndexError):
                pass

            def _dval(arr, i, *, nonnegative=False):
                try:
                    v = arr[i]
                    number = float(v) if v is not None else None
                    if number is None or not math.isfinite(number) or (nonnegative and number < 0):
                        return None
                    return number
                except (IndexError, OverflowError, ValueError, TypeError):
                    return None

            def _dint(arr, i):
                try:
                    v = arr[i]
                    return int(v) if v is not None else None
                except (IndexError, OverflowError, ValueError, TypeError):
                    return None

            sr_val = None
            ss_val = None
            if di < len(daily_sr) and daily_sr[di]:
                sr_val = _canonical_hh_mm(daily_sr[di])
            if di < len(daily_ss) and daily_ss[di]:
                ss_val = _canonical_hh_mm(daily_ss[di])

            self.daily_forecast.append(
                {
                    "date": date_str,
                    "day_name": day_name,
                    "temp_min": _dval(daily_tmin, di),
                    "temp_max": _dval(daily_tmax, di),
                    "precip_sum": _dval(daily_psum, di, nonnegative=True),
                    "weather_code": _dint(daily_wcode, di),
                    "sunrise": sr_val,
                    "sunset": ss_val,
                }
            )

        # Min temperature in next 6h (for freeze forecast check)
        self.min_temp_forecast_6h = None  # type: Optional[float]
        if idx is not None:
            temp_arr = hourly.get("temperature_2m", [])
            forecast_temperatures = []
            for i in range(idx, idx + 7):
                try:
                    v = temp_arr[i]
                    number = float(v)
                    if v is None or not math.isfinite(number):
                        raise ValueError("invalid temperature forecast")
                    forecast_temperatures.append(number)
                except (IndexError, OverflowError, ValueError, TypeError):
                    forecast_temperatures = []
                    break
            if len(forecast_temperatures) == 7:
                self.min_temp_forecast_6h = min(forecast_temperatures)

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return {
            "temperature": self.temperature,
            "humidity": self.humidity,
            "precipitation": self.precipitation,
            "wind_speed": self.wind_speed,
            "et0_hourly": self.et0_hourly,
            "daily_precipitation": self.daily_precipitation,
            "daily_et0": self.daily_et0,
            "precipitation_24h": self.precipitation_24h,
            "precipitation_forecast_6h": self.precipitation_forecast_6h,
            "timestamp": self.timestamp,
            "weather_code": self.weather_code,
            "temperature_min": self.temperature_min,
            "temperature_max": self.temperature_max,
            "sunrise": self.sunrise,
            "sunset": self.sunset,
            "hourly_forecast_24h": self.hourly_forecast_24h,
            "daily_forecast": self.daily_forecast,
            "min_temp_forecast_6h": self.min_temp_forecast_6h,
        }
