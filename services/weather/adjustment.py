"""Weather-based watering adjustment — skip rules + coefficient calculation.

Single responsibility: given current weather (from ``WeatherService``) and
user-configurable thresholds (from the ``settings`` table), decide
(a) whether to skip watering entirely and (b) what multiplier (0–200%) to
apply to the base zone duration.

Algorithm: hybrid Zimmerman method (from OpenSprinkler) + ET₀ (FAO-56).

NOTE(wave4, CQ-015): direct ``sqlite3.connect(self.db_path, timeout=5)``
calls in ``_get_settings`` / ``_has_ms_threshold`` / ``log_adjustment`` are
preserved from the pre-split implementation. A future repository migration
should remain separate from the import-cycle cleanup.
"""

import contextlib
import copy
import json
import logging
import math
import sqlite3
import time
from typing import Any

from services.weather.singletons import get_weather_service

logger = logging.getLogger(__name__)
_WEATHER_UNSET = object()


class WeatherAdjustment:
    """Calculate watering adjustment based on weather conditions.

    Hybrid approach: Zimmerman method (simple, proven from OpenSprinkler) + ET₀.
    Calculates a watering coefficient (0-200%) and skip conditions (rain, freeze, wind).
    """

    # Default thresholds
    DEFAULT_RAIN_THRESHOLD_MM = 5.0
    DEFAULT_FREEZE_THRESHOLD_C = 2.0
    DEFAULT_WIND_THRESHOLD_KMH = 25.0  # legacy
    DEFAULT_WIND_THRESHOLD_MS = 7.0  # ~25 km/h
    DEFAULT_HUMIDITY_THRESHOLD_PCT = 80.0
    DEFAULT_HUMIDITY_REDUCTION_PCT = 30
    DEFAULT_BASELINE_TEMP_C = 25.0
    DEFAULT_BASELINE_HUM_PCT = 50.0

    # Local WB-MSW sensor sanity bounds + mismatch thresholds vs Open-Meteo.
    # Local temp/hum take priority over the API forecast when present, sane
    # and fresh; a temperature gap beyond the hard threshold flags a faulty
    # sensor.  The colder source is retained for freeze safety while humidity
    # falls back to Open-Meteo.
    SENSOR_TEMP_MIN_C = -50.0
    SENSOR_TEMP_MAX_C = 60.0
    SENSOR_HUM_MIN_PCT = 0.0
    SENSOR_HUM_MAX_PCT = 100.0
    DEFAULT_SENSOR_MISMATCH_SOFT_C = 5.0
    DEFAULT_SENSOR_MISMATCH_HARD_C = 10.0

    def __init__(self, db_path: str = "irrigation.db") -> None:
        self.db_path = db_path

    def _get_settings(self):
        # type: () -> Dict[str, Any]
        """Load weather adjustment settings from DB."""
        defaults = {
            "enabled": False,
            "rain_threshold_mm": self.DEFAULT_RAIN_THRESHOLD_MM,
            "freeze_threshold_c": self.DEFAULT_FREEZE_THRESHOLD_C,
            "wind_threshold_kmh": self.DEFAULT_WIND_THRESHOLD_KMH,
            "wind_threshold_ms": self.DEFAULT_WIND_THRESHOLD_MS,
            "humidity_threshold_pct": self.DEFAULT_HUMIDITY_THRESHOLD_PCT,
            "humidity_reduction_pct": self.DEFAULT_HUMIDITY_REDUCTION_PCT,
            "factor_rain": True,
            "factor_freeze": True,
            "factor_wind": True,
            "factor_humidity": True,
            "factor_heat": True,
            "sensor_mismatch_soft_c": self.DEFAULT_SENSOR_MISMATCH_SOFT_C,
            "sensor_mismatch_hard_c": self.DEFAULT_SENSOR_MISMATCH_HARD_C,
        }
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                keys = [
                    "weather.enabled",
                    "weather.rain_threshold_mm",
                    "weather.freeze_threshold_c",
                    "weather.wind_threshold_kmh",
                    "weather.wind_threshold_ms",
                    "weather.humidity_threshold_pct",
                    "weather.humidity_reduction_pct",
                    "weather.factor.rain",
                    "weather.factor.freeze",
                    "weather.factor.wind",
                    "weather.factor.humidity",
                    "weather.factor.heat",
                    "weather.sensor_mismatch_soft_c",
                    "weather.sensor_mismatch_hard_c",
                ]
                for key in keys:
                    cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
                    row = cur.fetchone()
                    if row and row["value"] is not None:
                        val = row["value"]
                        if key == "weather.enabled":
                            defaults["enabled"] = str(val) in ("1", "true", "True")
                        elif key.startswith("weather.factor."):
                            factor_name = key.replace("weather.factor.", "")
                            defaults["factor_" + factor_name] = str(val) in ("1", "true", "True")
                        else:
                            short_key = key.replace("weather.", "")
                            try:
                                defaults[short_key] = float(val)
                            except (OverflowError, ValueError, TypeError):
                                defaults[short_key] = None
        except (sqlite3.Error, OSError) as e:
            logger.debug("Weather settings read error: %s", e)
        return defaults

    def _get_weather(self, cache_only: bool = False):
        """Get current weather data."""
        try:
            svc = get_weather_service(self.db_path)
            weather = svc.get_weather(cache_only=cache_only)
            if weather is None:
                if not cache_only:
                    self._maybe_alert_api_down("weather=None")
                return None
            try:
                ts = getattr(weather, "timestamp", None)
                if ts:
                    age = time.time() - float(ts)
                    if age > 7200 and not cache_only:
                        self._maybe_alert_api_down(f"cache stale {int(age / 60)}min")
            except (OverflowError, TypeError, ValueError):
                pass
            return self._select_input_source(weather)
        except (ImportError, OSError) as e:
            logger.debug("Weather data unavailable: %s", e)
            return None

    @staticmethod
    def _finite_number(
        value: Any,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float | None:
        """Return a finite constrained float or ``None`` for unsafe input."""
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        if minimum is not None and number < minimum:
            return None
        if maximum is not None and number > maximum:
            return None
        return number

    @classmethod
    def _sane(cls, value: Any, lo: float, hi: float) -> bool:
        """True if ``value`` is a finite number within ``[lo, hi]``."""
        return cls._finite_number(value, minimum=lo, maximum=hi) is not None

    def _validated_safety_inputs(
        self,
        weather: Any,
        settings: dict[str, Any],
    ) -> tuple[dict[str, float | bool] | None, str | None]:
        """Validate every value participating in hard rain/freeze/wind safety."""
        source_error = getattr(weather, "safety_invalid_field", None)
        if isinstance(source_error, str) and source_error:
            return None, source_error
        use_ms = self._has_ms_threshold()
        threshold_key = "wind_threshold_ms" if use_ms else "wind_threshold_kmh"
        defaults = {
            "rain_threshold_mm": self.DEFAULT_RAIN_THRESHOLD_MM,
            "freeze_threshold_c": self.DEFAULT_FREEZE_THRESHOLD_C,
            "wind_threshold_ms": self.DEFAULT_WIND_THRESHOLD_MS,
            "wind_threshold_kmh": self.DEFAULT_WIND_THRESHOLD_KMH,
        }
        specifications = (
            ("rain_threshold_mm", settings.get("rain_threshold_mm", defaults["rain_threshold_mm"]), 0.0),
            ("freeze_threshold_c", settings.get("freeze_threshold_c", defaults["freeze_threshold_c"]), None),
            (threshold_key, settings.get(threshold_key, defaults[threshold_key]), 0.0),
            (
                "sensor_mismatch_soft_c",
                settings.get("sensor_mismatch_soft_c", self.DEFAULT_SENSOR_MISMATCH_SOFT_C),
                0.0,
            ),
            (
                "sensor_mismatch_hard_c",
                settings.get("sensor_mismatch_hard_c", self.DEFAULT_SENSOR_MISMATCH_HARD_C),
                0.0,
            ),
            ("precipitation_24h", getattr(weather, "precipitation_24h", None), 0.0),
            ("precipitation_forecast_6h", getattr(weather, "precipitation_forecast_6h", None), 0.0),
            ("temperature", getattr(weather, "temperature", None), None),
            ("min_temp_forecast_6h", getattr(weather, "min_temp_forecast_6h", None), None),
            ("wind_speed", getattr(weather, "wind_speed", None), 0.0),
        )
        validated: dict[str, float | bool] = {"wind_uses_ms": use_ms}
        for field, raw_value, minimum in specifications:
            value = self._finite_number(raw_value, minimum=minimum)
            if value is None:
                return None, field
            validated[field] = value
        if validated["sensor_mismatch_hard_c"] < validated["sensor_mismatch_soft_c"]:
            return None, "sensor_mismatch_window"
        validated["wind_threshold"] = validated[threshold_key]
        return validated, None

    @staticmethod
    def _unavailable_decision(
        *,
        field: str | None = None,
        cache_only: bool = False,
        api_unavailable: bool = False,
    ) -> dict[str, Any]:
        reason = "weather_unavailable" if field is None else f"weather_unavailable: {field}"
        details: dict[str, Any] = {
            "type": "weather_unavailable",
            "api_unavailable": api_unavailable,
        }
        if field is not None:
            details["field"] = field
        if cache_only:
            details["unknown"] = True
            details["display_only"] = True
        return {"skip": not cache_only, "reason": reason, "details": details}

    def evaluate_sensor_source(self, api_weather: Any) -> dict[str, Any]:
        """Choose temp/hum input source (local sensor priority) + detect mismatch.

        The local WB-MSW sensor wins over the Open-Meteo forecast when its
        reading is present, physically sane and fresh (handled by
        ``_get_env_state``). A temperature gap vs the API beyond the *hard*
        threshold is treated as a faulty sensor and a ``mismatch`` of level
        ``'hard'`` is returned. Temperature keeps the colder of local/API so
        the mismatch cannot mask a freeze; humidity falls back to Open-Meteo.
        A gap beyond the *soft* threshold keeps the local value but flags
        ``'soft'`` for the UI. Precipitation/wind/ET₀ are untouched (API only).

        Returns ``{temperature, humidity, temp_source, hum_source, mismatch}``
        where ``*_source`` is ``'local'`` / ``'api'`` / ``'api_fallback'`` and
        ``mismatch`` is ``None`` or ``{'level', 'local', 'api', 'delta'}``.
        """
        from services.weather.merge import _get_env_state

        settings = self._get_settings()
        soft = self._finite_number(
            settings.get("sensor_mismatch_soft_c", self.DEFAULT_SENSOR_MISMATCH_SOFT_C),
            minimum=0.0,
        )
        hard = self._finite_number(
            settings.get("sensor_mismatch_hard_c", self.DEFAULT_SENSOR_MISMATCH_HARD_C),
            minimum=0.0,
        )
        invalid_mismatch_window = soft is None or hard is None or hard < soft
        if invalid_mismatch_window:
            soft = self.DEFAULT_SENSOR_MISMATCH_SOFT_C
            hard = self.DEFAULT_SENSOR_MISMATCH_HARD_C

        env = _get_env_state(time.time())
        api_t = getattr(api_weather, "temperature", None)
        api_h = getattr(api_weather, "humidity", None)

        mismatch = None

        # Temperature: local sensor priority, with mismatch detection vs API.
        temp_value: Any = api_t
        temp_source = "api_fallback" if (env["temp_enabled"] and not env["temp_online"]) else "api"
        if env["temp_online"] and self._sane(env["temp_value"], self.SENSOR_TEMP_MIN_C, self.SENSOR_TEMP_MAX_C):
            local_t = float(env["temp_value"])
            if self._sane(api_t, self.SENSOR_TEMP_MIN_C, self.SENSOR_TEMP_MAX_C):
                delta = abs(local_t - float(api_t))
                if delta > hard:
                    mismatch = {"level": "hard", "local": local_t, "api": float(api_t), "delta": round(delta, 1)}
                    # A hard mismatch still must not discard the colder input
                    # before the freeze gate.  Fall back only when the API is
                    # the safer (colder) source; retain a colder local reading.
                    if local_t <= float(api_t):
                        temp_value, temp_source = local_t, "local"
                    else:
                        temp_value, temp_source = float(api_t), "api_fallback"
                else:
                    if delta > soft:
                        mismatch = {"level": "soft", "local": local_t, "api": float(api_t), "delta": round(delta, 1)}
                    temp_value, temp_source = local_t, "local"
            else:
                temp_value, temp_source = local_t, "local"

        # Humidity: local priority, but a hard temp mismatch distrusts the whole
        # sensor module → use API humidity too.
        hum_value: Any = api_h
        hum_source = "api_fallback" if (env["hum_enabled"] and not env["hum_online"]) else "api"
        hard_mismatch = mismatch is not None and mismatch["level"] == "hard"
        if hard_mismatch:
            # a hard temp mismatch distrusts the whole sensor module → deliberate
            # API fallback for humidity too, even though it may be online/sane.
            hum_value, hum_source = api_h, "api_fallback"
        elif env["hum_online"] and self._sane(env["hum_value"], self.SENSOR_HUM_MIN_PCT, self.SENSOR_HUM_MAX_PCT):
            hum_value, hum_source = float(env["hum_value"]), "local"

        return {
            "temperature": temp_value,
            "humidity": hum_value,
            "temp_source": temp_source,
            "hum_source": hum_source,
            "mismatch": mismatch,
            "safety_invalid_field": "sensor_mismatch_window" if invalid_mismatch_window else None,
        }

    def _apply_source(self, weather: Any, verdict: dict[str, Any]) -> Any:
        """Return a shallow copy of ``weather`` with temp/hum from ``verdict``.

        ``min_temp_forecast_6h`` and all other fields stay as the API provided
        them, so freeze protection still triggers on the *minimum* of the local
        sensor and the forecast.
        """
        eff = copy.copy(weather)
        eff.temperature = verdict["temperature"]
        eff.humidity = verdict["humidity"]
        eff.temperature_source = verdict.get("temp_source", "api")
        eff.humidity_source = verdict.get("hum_source", "api")
        eff.sensor_mismatch = verdict.get("mismatch")
        eff.safety_invalid_field = verdict.get("safety_invalid_field")
        # Keep provenance next to the values that flow into safety and decision
        # logging.  Open-Meteo remains the only source for all fields except the
        # two values that may be overlaid by the local WB-MSW sensor.
        eff.data_sources = {
            "temperature": eff.temperature_source,
            "humidity": eff.humidity_source,
            "precipitation_24h": "api",
            "precipitation_forecast_6h": "api",
            "wind_speed": "api",
            "daily_et0": "api",
            "min_temp_forecast_6h": "api",
        }
        return eff

    def _select_input_source(self, weather: Any) -> Any:
        """Wrap source selection so it never breaks weather retrieval."""
        try:
            verdict = self.evaluate_sensor_source(weather)
            return self._apply_source(weather, verdict)
        except (ImportError, OSError, AttributeError, ValueError, TypeError, KeyError) as e:
            logger.debug("Input source selection failed, using API data: %s", e)
            return weather

    def get_sensor_mismatch(self) -> dict[str, Any] | None:
        """Cache-only temp mismatch check for the status banner (no network).

        Reads only the fresh weather cache (never triggers an API fetch) so it
        is cheap enough for the frequently-polled ``/api/status`` endpoint.
        Returns the ``mismatch`` dict (or ``None``).
        """
        if not self.is_enabled():
            return None
        try:
            svc = get_weather_service(self.db_path)
            loc = svc._get_location()
            if not loc:
                return None
            cached = svc._get_cached(loc["latitude"], loc["longitude"])
            if cached is None:
                return None
            return self.evaluate_sensor_source(cached).get("mismatch")
        except (ImportError, OSError, AttributeError, ValueError, TypeError, KeyError) as e:
            logger.debug("sensor mismatch check failed: %s", e)
            return None

    def _check_safety_skip(self, weather, settings):
        # type: (Any, Dict[str, Any]) -> bool
        """Threshold-only safety check (ignores factor_* flags).

        Mirrors should_skip() conditions but without the user-facing
        ``factor_rain``/``factor_freeze``/``factor_wind`` toggles. Used by
        ``get_coefficient`` to force coef=0 on dangerous conditions even when
        the corresponding factor is disabled (e.g. 50mm rain + factor_rain=off
        must still skip — flags are UX, thresholds are hard safety).
        """
        if weather is None:
            return True
        safety, error = self._validated_safety_inputs(weather, settings)
        if error is not None or safety is None:
            return True

        rain_threshold = float(safety["rain_threshold_mm"])
        rain_24h = float(safety["precipitation_24h"])
        rain_forecast = float(safety["precipitation_forecast_6h"])
        if self._rain_reaches_threshold(rain_24h, rain_threshold) or self._rain_reaches_threshold(
            rain_forecast, rain_threshold
        ):
            return True

        freeze_threshold = float(safety["freeze_threshold_c"])
        temp = float(safety["temperature"])
        if temp <= freeze_threshold:
            return True
        min_temp_6h = float(safety["min_temp_forecast_6h"])
        if min_temp_6h <= freeze_threshold:
            return True

        return float(safety["wind_speed"]) >= float(safety["wind_threshold"])

    @staticmethod
    def _rain_reaches_threshold(value: float, threshold: float) -> bool:
        """Inclusive rain threshold without treating dry zero as a hit at 0 mm."""
        return value > 0 if threshold <= 0 else value >= threshold

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
            ms_val = settings.get("wind_threshold_ms", self.DEFAULT_WIND_THRESHOLD_MS)
            value = self._finite_number(ms_val, minimum=0.0)
            return value if value is not None else float("nan")
        kmh_val = settings.get("wind_threshold_kmh", self.DEFAULT_WIND_THRESHOLD_KMH)
        value = self._finite_number(kmh_val, minimum=0.0)
        return round(value / 3.6, 1) if value is not None else float("nan")

    def _get_wind_check(self, settings, wind_value):
        # type: (Dict[str, Any], Optional[float]) -> Tuple[bool, str]
        """Check if wind exceeds threshold."""
        wind = self._finite_number(wind_value, minimum=0.0)
        if wind is None:
            return (True, "weather_unavailable: wind_speed")
        if self._has_ms_threshold():
            threshold = self._finite_number(
                settings.get("wind_threshold_ms", self.DEFAULT_WIND_THRESHOLD_MS),
                minimum=0.0,
            )
            if threshold is None:
                return (True, "weather_unavailable: wind_threshold_ms")
            exceeds = wind >= threshold
            detail = f"{wind:.1f} м/с ≥ {threshold:.1f} м/с" if exceeds else f"{wind:.1f} м/с < {threshold:.1f} м/с"
            return (exceeds, f"wind_skip: {wind:.1f} м/с (порог {threshold:.1f} м/с)" if exceeds else detail)

        threshold = self._finite_number(
            settings.get("wind_threshold_kmh", self.DEFAULT_WIND_THRESHOLD_KMH),
            minimum=0.0,
        )
        if threshold is None:
            return (True, "weather_unavailable: wind_threshold_kmh")
        exceeds = wind >= threshold
        detail = f"{wind:.1f} км/ч ≥ {threshold:.0f} км/ч" if exceeds else f"{wind:.1f} км/ч < {threshold:.0f} км/ч"
        return (exceeds, f"wind_skip: {wind:.1f} км/ч (порог {threshold:.0f} км/ч)" if exceeds else detail)

    def is_enabled(self) -> bool:
        """Check if weather adjustment is enabled."""
        return self._get_settings().get("enabled", False)

    def should_skip(self, cache_only: bool = False, *, weather: Any = _WEATHER_UNSET) -> dict[str, Any]:
        """Determine if watering should be skipped entirely.

        Returns a dict with keys ``skip`` (bool), ``reason`` (human-readable
        string) and ``details`` (dict with ``type``/``value``/``threshold``).

        ``cache_only=True`` decides по кэшу погоды без сетевого запроса —
        для hot-path'ов отображения (поллинг /api/status). A cache miss is
        returned as non-suppressing unknown state; live scheduler calls remain
        fail-closed.
        """
        result = {"skip": False, "reason": "", "details": {}}
        settings = self._get_settings()
        if not settings.get("enabled"):
            return result

        if weather is _WEATHER_UNSET:
            weather = self._get_weather(cache_only=cache_only)
        if not weather:
            return self._unavailable_decision(cache_only=cache_only, api_unavailable=not cache_only)

        safety, error = self._validated_safety_inputs(weather, settings)
        if error is not None or safety is None:
            return self._unavailable_decision(field=error, cache_only=cache_only)

        # Rain/freeze/wind thresholds are hard safety. The factor toggles only
        # disable their soft coefficient contribution and UI control state.
        rain_threshold = float(safety["rain_threshold_mm"])
        rain_24h = float(safety["precipitation_24h"])
        rain_forecast = float(safety["precipitation_forecast_6h"])

        if self._rain_reaches_threshold(rain_24h, rain_threshold):
            result["skip"] = True
            result["reason"] = f"rain_skip: {rain_24h:.1f}mm за 24ч (порог {rain_threshold:.0f}mm)"
            result["details"] = {"type": "rain", "value": rain_24h, "threshold": rain_threshold}
            return result

        if self._rain_reaches_threshold(rain_forecast, rain_threshold):
            result["skip"] = True
            result["reason"] = f"rain_forecast_skip: прогноз {rain_forecast:.1f}mm за 6ч (порог {rain_threshold:.0f}mm)"
            result["details"] = {"type": "rain_forecast", "value": rain_forecast, "threshold": rain_threshold}
            return result

        freeze_threshold = float(safety["freeze_threshold_c"])
        temp = float(safety["temperature"])

        if temp <= freeze_threshold:
            result["skip"] = True
            result["reason"] = f"freeze_skip: {temp:.1f}°C (порог {freeze_threshold:.0f}°C)"
            result["details"] = {"type": "freeze", "value": temp, "threshold": freeze_threshold}
            return result

        min_temp_6h = float(safety["min_temp_forecast_6h"])
        if min_temp_6h <= freeze_threshold:
            result["skip"] = True
            result["reason"] = (
                f"freeze_forecast_skip: прогноз мин {min_temp_6h:.1f}°C за 6ч (порог {freeze_threshold:.0f}°C)"
            )
            result["details"] = {"type": "freeze_forecast", "value": min_temp_6h, "threshold": freeze_threshold}
            return result

        wind = float(safety["wind_speed"])
        wind_threshold = float(safety["wind_threshold"])
        if wind >= wind_threshold:
            unit = "м/с" if safety["wind_uses_ms"] else "км/ч"
            result["skip"] = True
            result["reason"] = f"wind_skip: {wind:.1f} {unit} (порог {wind_threshold:.1f} {unit})"
            result["details"] = {"type": "wind", "value": wind, "threshold": wind_threshold}
            return result

        return result

    def get_coefficient(self, *, weather: Any = _WEATHER_UNSET) -> int:
        """Calculate the H1 watering adjustment coefficient (0-200%)."""
        settings = self._get_settings()
        if not settings.get("enabled"):
            return 100

        if weather is _WEATHER_UNSET:
            weather = self._get_weather()
        if not weather:
            return 0

        # Hard safety: thresholds always force skip even if factor_* flags are off
        if self._check_safety_skip(weather, settings):
            return 0
        safety, error = self._validated_safety_inputs(weather, settings)
        if error is not None or safety is None:
            return 0

        base = 100

        # Temperature factor (Zimmerman-style)
        temp = float(safety["temperature"])
        temp_factor = 1.0
        if settings.get("factor_heat", True):
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
        hum = self._finite_number(getattr(weather, "humidity", None), minimum=0.0, maximum=100.0)
        humidity_factor = 1.0
        if hum is not None and settings.get("factor_humidity", True):
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
        if hum is not None and settings.get("factor_humidity", True):
            hum_threshold = self._finite_number(
                settings.get("humidity_threshold_pct", self.DEFAULT_HUMIDITY_THRESHOLD_PCT),
                minimum=0.0,
                maximum=100.0,
            )
            hum_reduction = self._finite_number(
                settings.get("humidity_reduction_pct", self.DEFAULT_HUMIDITY_REDUCTION_PCT),
                minimum=0.0,
                maximum=100.0,
            )
            if hum_threshold is None or hum_reduction is None:
                return 0
            if hum > hum_threshold:
                humidity_factor = humidity_factor * (1.0 - hum_reduction / 100.0)

        # Rain factor
        rain_24h = float(safety["precipitation_24h"])
        rain_factor = 1.0
        rain_threshold = float(safety["rain_threshold_mm"])
        if rain_24h > 0 and rain_threshold > 0 and settings.get("factor_rain", True):
            ratio = rain_24h / rain_threshold
            if ratio >= 1.0:
                rain_factor = 0.0
            else:
                rain_factor = max(0.3, 1.0 - ratio * 0.7)

        # Wind factor
        wind = float(safety["wind_speed"])
        wind_factor = 1.0
        if settings.get("factor_wind", True):
            if bool(safety["wind_uses_ms"]):
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
        daily_et0 = self._finite_number(getattr(weather, "daily_et0", None), minimum=0.0)
        if daily_et0 is not None:
            ref_et0 = 4.5
            if daily_et0 > 0:
                et0_ratio = daily_et0 / ref_et0
                et0_factor = 0.5 + 0.5 * min(2.0, et0_ratio)

        coefficient = base * temp_factor * humidity_factor * rain_factor * wind_factor * et0_factor
        if not math.isfinite(coefficient):
            return 0
        result = max(0, min(200, round(coefficient)))
        return result

    # --- H2 water-balance integration -----------------------------------
    # H2 is intentionally shadow-only (PR-060).  These helpers expose diagnostic
    # state to the UI/nightly job; the scheduler always calls the H1 method above.

    def _balance_enabled(self) -> bool:
        """True if the H2 shadow/diagnostic calculation flag is set."""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cur = conn.execute("SELECT value FROM settings WHERE key = 'weather.balance.enabled'")
                row = cur.fetchone()
                return row is not None and str(row[0]) in ("1", "true", "True")
        except (sqlite3.Error, OSError):
            return False

    def _balance_coef_fresh(self) -> bool:
        """True if the cached diagnostic balance coefficient is fresh."""
        return self.get_balance_diagnostic_status()["status"] == "fresh"

    def get_balance_diagnostic_status(self) -> dict[str, Any]:
        """Return canonical H2 shadow freshness metadata for the UI.

        H2 never steers watering.  Future/invalid dates are surfaced explicitly
        and cannot be mistaken for a fresh live coefficient.
        """
        result: dict[str, Any] = {
            "status": "unavailable",
            "last_recalc_date": None,
            "age_days": None,
            "stale": False,
            "fresh": False,
        }
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute("SELECT value FROM settings WHERE key = 'weather.balance.last_recalc_date'")
                row = cur.fetchone()
                if not row or not row["value"]:
                    return result
                cur = conn.execute("SELECT value FROM settings WHERE key = 'weather.balance.stale_fallback_days'")
                srow = cur.fetchone()
                stale_days = 2
                if srow and srow["value"] is not None:
                    raw_stale_days = float(srow["value"])
                    if (
                        not math.isfinite(raw_stale_days)
                        or not raw_stale_days.is_integer()
                        or not 1 <= raw_stale_days <= 14
                    ):
                        logger.error(
                            "invalid weather.balance.stale_fallback_days=%r",
                            srow["value"],
                        )
                        return result
                    stale_days = int(raw_stale_days)
                from datetime import date, datetime

                raw_date = str(row["value"])
                last = datetime.strptime(raw_date, "%Y-%m-%d").date()
                age_days = (date.today() - last).days
                status = "future" if age_days < 0 else "fresh" if age_days <= stale_days else "stale"
                return {
                    "status": status,
                    "last_recalc_date": raw_date,
                    "age_days": age_days,
                    "stale": status == "stale",
                    "fresh": status == "fresh",
                }
        except (sqlite3.Error, OSError, OverflowError, ValueError, TypeError) as e:
            logger.debug("balance freshness check failed: %s", e)
            return result

    def get_effective_coefficient(self) -> int:
        """Compatibility alias; H2 remains diagnostic and never steers watering."""
        return self.get_coefficient()

    def get_factors_detail(self, weather: Any | None = None) -> dict[str, dict[str, Any]]:
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
        rain_enabled = settings.get("factor_rain", True)
        rain_threshold = self._finite_number(
            settings.get("rain_threshold_mm", self.DEFAULT_RAIN_THRESHOLD_MM),
            minimum=0.0,
        )
        rain_24h = None
        rain_forecast = None
        if weather:
            rain_24h = self._finite_number(getattr(weather, "precipitation_24h", None), minimum=0.0)
            rain_forecast = self._finite_number(
                getattr(weather, "precipitation_forecast_6h", None),
                minimum=0.0,
            )

        rain_status = "ok"
        if rain_threshold is None or rain_24h is None or rain_forecast is None:
            rain_status = "danger"
            rain_detail = "нет достоверных данных дождя за 24ч/прогноза 6ч/порога (skip)"
        elif self._rain_reaches_threshold(rain_24h, rain_threshold):
            rain_status = "danger"
            rain_detail = f"{rain_24h:.1f} мм ≥ {rain_threshold:.0f} мм (skip)"
        elif self._rain_reaches_threshold(rain_forecast, rain_threshold):
            rain_status = "danger"
            rain_detail = f"прогноз {rain_forecast:.1f} мм за 6ч ≥ {rain_threshold:.0f} мм (skip)"
        elif max(rain_24h, rain_forecast) > rain_threshold * 0.5:
            rain_status = "warn"
            rain_detail = f"{rain_24h:.1f} мм (прогноз +{rain_forecast:.1f} мм)"
        else:
            rain_detail = f"{rain_24h:.1f} мм < {rain_threshold:.0f} мм"

        result["rain"] = {"status": rain_status, "detail": rain_detail, "enabled": rain_enabled}

        # Freeze factor
        freeze_enabled = settings.get("factor_freeze", True)
        freeze_threshold = self._finite_number(settings.get("freeze_threshold_c", self.DEFAULT_FREEZE_THRESHOLD_C))
        temp = self._finite_number(getattr(weather, "temperature", None)) if weather else None
        _raw_min_6h = getattr(weather, "min_temp_forecast_6h", None) if weather else None
        min_temp_6h = self._finite_number(_raw_min_6h)

        freeze_status = "ok"
        if freeze_threshold is None or temp is None or min_temp_6h is None:
            freeze_status = "danger"
            freeze_detail = "нет достоверных данных температуры/порога (skip)"
        elif temp <= freeze_threshold:
            freeze_status = "danger"
            freeze_detail = f"{temp:.1f}°C ≤ {freeze_threshold:.0f}°C (skip)"
        elif min_temp_6h <= freeze_threshold:
            freeze_status = "danger"
            freeze_detail = f"прогноз мин {min_temp_6h:.1f}°C за 6ч (skip)"
        elif min_temp_6h < freeze_threshold + 3:
            freeze_status = "warn"
            freeze_detail = f"мин {min_temp_6h:.1f}°C за 6ч (близко к порогу)"
        else:
            freeze_detail = f"мин +{min_temp_6h:.1f}°C за 6ч"

        result["freeze"] = {"status": freeze_status, "detail": freeze_detail, "enabled": freeze_enabled}

        # Wind factor
        wind_enabled = settings.get("factor_wind", True)
        wind = self._finite_number(getattr(weather, "wind_speed", None), minimum=0.0) if weather else None
        use_ms = self._has_ms_threshold()

        wind_status = "ok"
        if wind is not None:
            if use_ms:
                wind_thr = self._get_wind_threshold_ms(settings)
                unit = "м/с"
            else:
                raw_wind_threshold = settings.get("wind_threshold_kmh", self.DEFAULT_WIND_THRESHOLD_KMH)
                wind_thr = self._finite_number(raw_wind_threshold, minimum=0.0)
                unit = "км/ч"
            if wind_thr is None or not math.isfinite(wind_thr):
                wind_status = "danger"
                wind_detail = "нет достоверного порога ветра (skip)"
            elif wind >= wind_thr:
                wind_status = "danger"
                wind_detail = f"{wind:.1f} {unit} ≥ {wind_thr:.1f} {unit} (skip)"
            elif wind > wind_thr * 0.7:
                wind_status = "warn"
                wind_detail = f"{wind:.1f} {unit} (близко к порогу)"
            else:
                wind_detail = f"{wind:.1f} {unit} < {wind_thr:.1f} {unit}"
        else:
            wind_status = "danger"
            wind_detail = "нет достоверных данных ветра (skip)"

        result["wind"] = {"status": wind_status, "detail": wind_detail, "enabled": wind_enabled}

        # Humidity factor
        hum_enabled = settings.get("factor_humidity", True)
        hum_threshold = self._finite_number(
            settings.get("humidity_threshold_pct", self.DEFAULT_HUMIDITY_THRESHOLD_PCT),
            minimum=0.0,
            maximum=100.0,
        )
        hum = self._finite_number(getattr(weather, "humidity", None), minimum=0.0, maximum=100.0) if weather else None

        hum_status = "ok"
        if not hum_enabled:
            hum_detail = "фактор отключён"
        elif hum is not None and hum_threshold is not None:
            if hum > hum_threshold:
                hum_status = "warn"
                hum_detail = f"{hum:.0f}% > {hum_threshold:.0f}% (коэфф. снижен)"
            else:
                hum_detail = f"{hum:.0f}% < {hum_threshold:.0f}%"
        else:
            hum_detail = "нет данных"

        result["humidity"] = {"status": hum_status, "detail": hum_detail, "enabled": hum_enabled}

        # Heat factor
        heat_enabled = settings.get("factor_heat", True)
        heat_status = "ok"
        if not heat_enabled:
            heat_detail = "фактор отключён"
        elif temp is not None:
            if temp > 35:
                heat_status = "danger"
                heat_detail = f"+{temp:.0f}°C — жара (коэфф. ×1.5)"
            elif temp > 30:
                heat_status = "warn"
                heat_detail = f"+{temp:.0f}°C — жарко (коэфф. ×1.25)"
            elif temp > 25:
                heat_status = "ok"
                heat_detail = f"+{temp:.0f}°C — тепло"
            else:
                heat_detail = f"+{temp:.0f}°C — норма"
        else:
            heat_detail = "нет данных"

        result["heat"] = {"status": heat_status, "detail": heat_detail, "enabled": heat_enabled}

        return result

    def adjust_duration(self, base_duration_min: int) -> int:
        """Adjust zone watering duration based on weather coefficient."""
        coeff = self.get_coefficient()
        adjusted = round(base_duration_min * coeff / 100.0)
        return max(1, adjusted) if adjusted > 0 else 0

    def log_adjustment(
        self,
        zone_id: int,
        original_duration: int,
        adjusted_duration: int,
        coefficient: int,
        skip: bool,
        reason: str = "",
        weather_snapshot: dict | None = None,
    ) -> None:
        """Log weather adjustment to weather_log table."""
        if weather_snapshot is None:
            w = self._get_weather()
            weather_snapshot = w.to_dict() if w is not None else {}
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute(
                    "INSERT INTO weather_log "
                    "(zone_id, original_duration, adjusted_duration, coefficient, "
                    "skipped, skip_reason, weather_data, created_at) "
                    'VALUES (?, ?, ?, ?, ?, ?, ?, datetime("now"))',
                    (
                        zone_id,
                        original_duration,
                        adjusted_duration,
                        coefficient,
                        1 if skip else 0,
                        reason,
                        json.dumps(weather_snapshot or {}),
                    ),
                )
                conn.commit()
        except (sqlite3.Error, OSError) as e:
            logger.debug("Weather log write error: %s", e)

    def log_decision(self, weather, coefficient, skip, reason, mode="auto") -> None:
        """Записать decision в weather_decisions для UI history."""
        snapshot_missing = weather is None
        if snapshot_missing:
            decision = "skip"
            coefficient = 0
            reason = reason or "weather_unavailable"
            mode = "unknown"
        elif skip:
            decision = "skip"
        elif coefficient == 100:
            decision = "water"
        else:
            decision = "adjust"

        now = time.localtime()
        date_str = time.strftime("%Y-%m-%d", now)
        time_str = time.strftime("%H:%M:%S", now)

        def _safe(attr):
            try:
                v = getattr(weather, attr, None)
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        allowed_sources = {"api", "api_fallback", "local", "unknown"}

        def _source(attr: str) -> str:
            if snapshot_missing:
                return "unknown"
            sources = getattr(weather, "data_sources", None)
            value = sources.get(attr) if isinstance(sources, dict) else None
            return value if value in allowed_sources else "api"

        data_sources = {
            "temperature": _source("temperature"),
            "humidity": _source("humidity"),
            "precipitation_24h": _source("precipitation_24h"),
            "precipitation_forecast_6h": _source("precipitation_forecast_6h"),
            "wind_speed": _source("wind_speed"),
            "daily_et0": _source("daily_et0"),
            "min_temp_forecast_6h": _source("min_temp_forecast_6h"),
        }

        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute(
                    "INSERT INTO weather_decisions "
                    "(date, time, temperature, humidity, precipitation_24h, wind_speed, "
                    "coefficient, decision, reason, mode, data_sources, user_override) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        date_str,
                        time_str,
                        _safe("temperature"),
                        _safe("humidity"),
                        _safe("precipitation_24h"),
                        _safe("wind_speed"),
                        int(coefficient),
                        decision,
                        reason or "",
                        mode,
                        json.dumps(data_sources),
                        0,
                    ),
                )
                conn.commit()
        except (sqlite3.Error, OSError) as e:
            logger.debug("Weather decision log error: %s", e)

    def _get_admin_chat_id(self) -> str | None:
        """Read telegram_admin_chat_id from settings (no self.db here)."""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cur = conn.execute(
                    "SELECT value FROM settings WHERE key = ?",
                    ("telegram_admin_chat_id",),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return str(row[0])
        except (sqlite3.Error, OSError) as e:
            logger.debug("admin chat_id read error: %s", e)
        return None

    def _should_alert_now(self) -> bool:
        """Throttle: 1 alert / 30 min via weather.last_alert_at setting."""
        try:
            now = time.time()
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cur = conn.execute("SELECT value FROM settings WHERE key = 'weather.last_alert_at'")
                row = cur.fetchone()
                if row and row[0]:
                    try:
                        last = float(row[0])
                        if now - last < 1800:
                            return False
                    except (TypeError, ValueError):
                        pass
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES ('weather.last_alert_at', ?)",
                    (str(now),),
                )
                conn.commit()
            return True
        except (sqlite3.Error, OSError) as e:
            logger.debug("alert throttle error: %s", e)
            return False

    def _send_telegram_alert(self, text: str) -> None:
        """Send Telegram alert to admin chat (best-effort)."""
        try:
            from services.telegram_bot import notifier

            chat_id = self._get_admin_chat_id()
            if chat_id:
                notifier.send_text(int(chat_id), text)
        except (ImportError, OSError, ValueError, TypeError) as e:
            logger.debug("Weather alert telegram: %s", e)

    def _maybe_alert_api_down(self, reason: str) -> None:
        """Send API-down alert if throttle allows."""
        if not self._should_alert_now():
            return
        self._send_telegram_alert(f"⚠️ Weather API недоступно: {reason}")
