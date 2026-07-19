"""Weather API routes for WB-Irrigation.

Endpoints:
- GET  /api/weather          — current weather summary (extended in v2)
- GET  /api/weather/decisions — weather decision history (NEW in v2)
- GET  /api/settings/weather — weather adjustment settings
- PUT  /api/settings/weather — update weather adjustment settings (extended in v2)
- GET  /api/settings/location — get location (lat/lon)
- PUT  /api/settings/location — set location (lat/lon)
- POST /api/weather/refresh  — force refresh weather data
- GET  /api/weather/log      — weather adjustment log
"""

import json
import logging
import math
import sqlite3
from typing import Any

from flask import Blueprint, jsonify, request

from database import db
from services.audit import audit_log
from services.security import admin_required

logger = logging.getLogger(__name__)

weather_api_bp = Blueprint("weather_api_bp", __name__)


def _setting_updates_atomically(updates: list[tuple[str, str | None]]) -> None:
    """Persist a configuration batch in one SQLite transaction."""
    with db.settings._connect() as conn:
        for key, value in updates:
            if value is None:
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                    (key, value),
                )
        conn.commit()


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a finite number") from error
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean")
    return value


@weather_api_bp.route("/api/weather", methods=["GET"])
def api_get_weather():
    """Get current weather summary for dashboard.

    Returns extended format with backward-compatible flat fields
    plus new structured data (current, forecast_24h, forecast_3d,
    astronomy, adjustment with factors).
    """
    try:
        from services.weather import get_weather_service
        from services.weather_merged import merge_weather_response

        svc = get_weather_service(db.db_path)
        # Use extended format that includes both old flat fields and new structured data
        try:
            extended = svc.get_weather_extended()
            return jsonify(merge_weather_response(extended))
        except (AttributeError, TypeError):
            # Fallback to legacy summary for backward compat
            summary = svc.get_weather_summary()
            return jsonify(summary)
    except (ImportError, OSError, ValueError) as e:
        logger.debug("Weather summary error: %s", e)
        return jsonify({"available": False, "error": str(e)})


@weather_api_bp.route("/api/weather/decisions", methods=["GET"])
def api_get_weather_decisions():
    """Get weather decision history.

    Query params:
    - days (int, default 7): how many days back
    - limit (int, default 50): max records to return
    """
    try:
        # SEC-015: guarantee int / range before building SQL modifier. If
        # `days` ever escapes the clamp in a future refactor, the sqlite
        # parameter binding will still reject it because we pass it as a
        # bound parameter — never concatenated into the SQL string.
        days = min(90, max(1, int(request.args.get("days", 7))))
        limit = min(200, max(1, int(request.args.get("limit", 50))))
        # Build the SQLite date-modifier string from the validated int.
        # `'-N days'` with N strictly int is safe; we still pass it as a
        # bound parameter (not an f-string into the query).
        days_modifier = "-%d days" % days

        with db.logs._connect() as conn:
            # Get decisions
            cur = conn.execute(
                "SELECT * FROM weather_decisions "
                'WHERE created_at >= datetime("now", ?) '
                "ORDER BY created_at DESC LIMIT ?",
                (days_modifier, limit),
            )
            rows = cur.fetchall()
            decisions = []
            for r in rows:
                d = dict(r)
                # Parse data_sources JSON
                try:
                    d["data_sources"] = json.loads(d.get("data_sources", "{}") or "{}")
                except (json.JSONDecodeError, TypeError):
                    d["data_sources"] = {}
                decisions.append(d)

            # Stats
            cur2 = conn.execute(
                "SELECT "
                '  COUNT(CASE WHEN decision IN ("skip", "stop") THEN 1 END) as skips, '
                "  COALESCE(AVG(coefficient), 100) as avg_coeff "
                "FROM weather_decisions "
                'WHERE created_at >= datetime("now", ?)',
                (days_modifier,),
            )
            stats_row = cur2.fetchone()
            skips_count = int(stats_row["skips"]) if stats_row else 0
            avg_coeff = round(float(stats_row["avg_coeff"])) if stats_row else 100
            water_saved = max(0, 100 - avg_coeff)

            return jsonify(
                {
                    "decisions": decisions,
                    "total": len(decisions),
                    "stats": {
                        "skips_%dd" % days: skips_count,
                        "avg_coefficient_%dd" % days: avg_coeff,
                        "water_saved_pct": water_saved,
                    },
                }
            )

    except sqlite3.OperationalError as e:
        # Table might not exist yet (migration not run)
        err_str = str(e)
        if "no such table" in err_str:
            return jsonify({"decisions": [], "total": 0, "stats": {}})
        logger.debug("Weather decisions read error: %s", e)
        return jsonify({"decisions": [], "total": 0, "stats": {}})
    except (sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("Weather decisions read error: %s", e)
        return jsonify({"decisions": [], "total": 0, "stats": {}})


@weather_api_bp.route("/api/settings/weather", methods=["GET"])
@admin_required
def api_get_weather_settings():
    """Get weather adjustment settings (extended in v2)."""
    try:
        # Helper to read a setting with default
        def _get(key, default, as_type="float"):
            val = db.get_setting_value(key)
            if val is None:
                return default
            if as_type == "bool":
                return str(val) in ("1", "true", "True")
            if as_type == "float":
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return default
            if as_type == "int":
                try:
                    return int(float(val))
                except (ValueError, TypeError):
                    return default
            return val

        return jsonify(
            {
                "enabled": _get("weather.enabled", False, "bool"),
                # Data source channel: "direct" (Open-Meteo) | "relay" (GitHub file)
                "source_mode": _get("weather.source_mode", "direct", "str"),
                "rain_threshold_mm": _get("weather.rain_threshold_mm", 5.0),
                "freeze_threshold_c": _get("weather.freeze_threshold_c", 2.0),
                # Legacy field for backward compat
                "wind_threshold_kmh": _get("weather.wind_threshold_kmh", 25.0),
                # NEW fields
                "wind_threshold_ms": _get("weather.wind_threshold_ms", 7.0),
                "humidity_threshold_pct": _get("weather.humidity_threshold_pct", 80.0),
                "humidity_reduction_pct": _get("weather.humidity_reduction_pct", 30, "int"),
                "factors": {
                    "rain": _get("weather.factor.rain", True, "bool"),
                    "freeze": _get("weather.factor.freeze", True, "bool"),
                    "wind": _get("weather.factor.wind", True, "bool"),
                    "humidity": _get("weather.factor.humidity", True, "bool"),
                    "heat": _get("weather.factor.heat", True, "bool"),
                },
                # H2 virtual water balance (mode switch + tuning)
                "balance": {
                    "enabled": _get("weather.balance.enabled", False, "bool"),
                    "window_days": _get("weather.balance.window_days", 3, "int"),
                    "norm_window_days": _get("weather.balance.norm_window_days", 30, "int"),
                    "coef_min": _get("weather.balance.coef_min", 50, "int"),
                    "coef_max": _get("weather.balance.coef_max", 150, "int"),
                    "intercept_mm": _get("weather.balance.intercept_mm", 4.0),
                    "stale_fallback_days": _get("weather.balance.stale_fallback_days", 2, "int"),
                },
            }
        )
    except (sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("Weather settings read error: %s", e)
        return jsonify({"error": str(e)}), 500


@weather_api_bp.route("/api/settings/weather", methods=["PUT"])
@admin_required
@audit_log("weather_settings_save", target_extractor=lambda *a, **kw: "weather_settings")
def api_put_weather_settings():
    """Update weather adjustment settings (extended in v2)."""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("request body must be an object")
        updates: list[tuple[str, str | None]] = []

        if "enabled" in data:
            enabled = _boolean(data["enabled"], "enabled")
            updates.append(("weather.enabled", "1" if enabled else "0"))
        if "source_mode" in data:
            mode = str(data["source_mode"]).strip().lower()
            if mode not in ("direct", "relay"):
                raise ValueError("source_mode must be direct or relay")
            updates.append(("weather.source_mode", mode))
        if "rain_threshold_mm" in data:
            val = _finite_number(data["rain_threshold_mm"], "rain_threshold_mm")
            updates.append(("weather.rain_threshold_mm", str(max(0, min(100, val)))))
        if "freeze_threshold_c" in data:
            val = _finite_number(data["freeze_threshold_c"], "freeze_threshold_c")
            updates.append(("weather.freeze_threshold_c", str(max(-10, min(10, val)))))

        # Legacy wind field
        if "wind_threshold_kmh" in data:
            val = _finite_number(data["wind_threshold_kmh"], "wind_threshold_kmh")
            updates.append(("weather.wind_threshold_kmh", str(max(5, min(100, val)))))

        # NEW: wind in m/s
        if "wind_threshold_ms" in data:
            val = _finite_number(data["wind_threshold_ms"], "wind_threshold_ms")
            updates.append(("weather.wind_threshold_ms", str(max(1.0, min(30.0, val)))))

        # NEW: humidity threshold
        if "humidity_threshold_pct" in data:
            val = _finite_number(data["humidity_threshold_pct"], "humidity_threshold_pct")
            updates.append(("weather.humidity_threshold_pct", str(max(50, min(100, val)))))

        # NEW: humidity reduction percentage
        if "humidity_reduction_pct" in data:
            val = int(_finite_number(data["humidity_reduction_pct"], "humidity_reduction_pct"))
            updates.append(("weather.humidity_reduction_pct", str(max(10, min(50, val)))))

        # NEW: per-factor toggles
        factors = data.get("factors")
        if factors is not None:
            if not isinstance(factors, dict):
                raise ValueError("factors must be an object")
            for factor_name in ("rain", "freeze", "wind", "humidity", "heat"):
                if factor_name in factors:
                    key = f"weather.factor.{factor_name}"
                    enabled = _boolean(factors[factor_name], f"factors.{factor_name}")
                    updates.append((key, "1" if enabled else "0"))

        # H2 virtual water balance settings (mode switch + tuning, clamped)
        balance = data.get("balance")
        if balance is not None:
            if not isinstance(balance, dict):
                raise ValueError("balance must be an object")
            if "enabled" in balance:
                enabled = _boolean(balance["enabled"], "balance.enabled")
                updates.append(("weather.balance.enabled", "1" if enabled else "0"))
            if "window_days" in balance:
                val = int(_finite_number(balance["window_days"], "balance.window_days"))
                updates.append(("weather.balance.window_days", str(max(1, min(14, val)))))
            if "norm_window_days" in balance:
                val = int(_finite_number(balance["norm_window_days"], "balance.norm_window_days"))
                updates.append(("weather.balance.norm_window_days", str(max(7, min(90, val)))))
            if "coef_min" in balance:
                val = int(_finite_number(balance["coef_min"], "balance.coef_min"))
                updates.append(("weather.balance.coef_min", str(max(0, min(100, val)))))
            if "coef_max" in balance:
                val = int(_finite_number(balance["coef_max"], "balance.coef_max"))
                updates.append(("weather.balance.coef_max", str(max(100, min(300, val)))))
            if "intercept_mm" in balance:
                val = _finite_number(balance["intercept_mm"], "balance.intercept_mm")
                updates.append(("weather.balance.intercept_mm", str(max(0.0, min(20.0, val)))))
            if "stale_fallback_days" in balance:
                val = int(_finite_number(balance["stale_fallback_days"], "balance.stale_fallback_days"))
                updates.append(("weather.balance.stale_fallback_days", str(max(1, min(14, val)))))

        _setting_updates_atomically(updates)
        return jsonify({"success": True})
    except (ValueError, TypeError) as e:
        logger.debug("Weather settings validation error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 400
    except sqlite3.Error as e:
        logger.debug("Weather settings write error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@weather_api_bp.route("/api/settings/location", methods=["GET"])
@admin_required
def api_get_location():
    """Get configured location (lat/lon)."""
    try:
        lat = db.get_setting_value("weather.latitude")
        lon = db.get_setting_value("weather.longitude")
        return jsonify(
            {
                "latitude": float(lat) if lat else None,
                "longitude": float(lon) if lon else None,
            }
        )
    except (sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("Location read error: %s", e)
        return jsonify({"latitude": None, "longitude": None})


@weather_api_bp.route("/api/settings/location", methods=["PUT"])
@admin_required
@audit_log("weather_location_save", target_extractor=lambda *a, **kw: "weather_location")
def api_put_location():
    """Set location (lat/lon)."""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("request body must be an object")
        if "latitude" not in data or "longitude" not in data:
            raise ValueError("latitude and longitude are required")
        lat = _finite_number(data["latitude"], "latitude")
        lon = _finite_number(data["longitude"], "longitude")
        if not -90.0 <= lat <= 90.0:
            raise ValueError("latitude must be within -90..90")
        if not -180.0 <= lon <= 180.0:
            raise ValueError("longitude must be within -180..180")
        _setting_updates_atomically(
            [
                ("weather.latitude", str(lat)),
                ("weather.longitude", str(lon)),
            ]
        )
        return jsonify({"success": True})
    except (ValueError, TypeError) as e:
        logger.debug("Location validation error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 400
    except sqlite3.Error as e:
        logger.debug("Location write error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@weather_api_bp.route("/api/weather/refresh", methods=["POST"])
@admin_required
@audit_log("weather_refresh", target_extractor=lambda *a, **kw: "weather")
def api_refresh_weather():
    """Force refresh weather data from API."""
    try:
        from services.weather import get_weather_service

        svc = get_weather_service(db.db_path)
        weather = svc.get_weather(force_refresh=True)
        if weather:
            return jsonify({"success": True, "data": weather.to_dict()})
        return jsonify({"success": False, "message": "Не удалось получить данные. Проверьте координаты."}), 400
    except (ImportError, OSError, ValueError) as e:
        logger.debug("Weather refresh error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@weather_api_bp.route("/api/weather/log", methods=["GET"])
@admin_required
def api_get_weather_log():
    """Get weather adjustment log (last 50 entries)."""
    try:
        limit = min(100, max(1, int(request.args.get("limit", 50))))
        with db.logs._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM weather_log ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            return jsonify({"logs": rows})
    except (sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("Weather log read error: %s", e)
        return jsonify({"logs": []})


@weather_api_bp.route("/api/weather/balance/log", methods=["GET"])
@admin_required
def api_get_weather_balance_log():
    """Get H2 water-balance audit log (last N entries) for shadow-mode review."""
    try:
        limit = min(100, max(1, int(request.args.get("limit", 50))))
        with db.logs._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM weather_balance_log ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            return jsonify({"logs": rows})
    except sqlite3.OperationalError as e:
        # Table may not exist yet (migration not run on this DB).
        if "no such table" in str(e):
            return jsonify({"logs": []})
        logger.debug("Weather balance log read error: %s", e)
        return jsonify({"logs": []})
    except (sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("Weather balance log read error: %s", e)
        return jsonify({"logs": []})


@weather_api_bp.route("/api/weather/balance/recalc", methods=["POST"])
@admin_required
@audit_log("weather_balance_recalc", target_extractor=lambda *a, **kw: "weather_balance")
def api_recalc_weather_balance():
    """Manually trigger an H2 water-balance recalculation (admin).

    Bypasses the same-day idempotency by clearing ``last_recalc_date`` first, so
    an operator can force a fresh pull/recompute on demand.
    """
    try:
        from services.weather.balance import recalc_balance

        db.set_setting_value("weather.balance.last_recalc_date", None)
        result = recalc_balance(db.db_path)
        if result is not None:
            return jsonify({"success": True, "result": result})
        return jsonify({"success": False, "message": "Пересчёт не выполнен (нет данных/локации)."}), 400
    except (ImportError, sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.debug("Weather balance recalc error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500
