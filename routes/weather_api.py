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
import sqlite3
import logging
import json
import time

from flask import Blueprint, jsonify, request
from database import db
from services.security import admin_required

logger = logging.getLogger(__name__)

weather_api_bp = Blueprint('weather_api_bp', __name__)


@weather_api_bp.route('/api/weather', methods=['GET'])
def api_get_weather():
    """Get current weather summary for dashboard.

    Returns extended format with backward-compatible flat fields
    plus new structured data (current, forecast_24h, forecast_3d,
    astronomy, adjustment with factors).
    """
    try:
        from services.weather import get_weather_service
        svc = get_weather_service(db.db_path)
        # Use extended format that includes both old flat fields and new structured data
        try:
            extended = svc.get_weather_extended()
            return jsonify(extended)
        except (AttributeError, TypeError):
            # Fallback to legacy summary for backward compat
            summary = svc.get_weather_summary()
            return jsonify(summary)
    except (ImportError, OSError, ValueError) as e:
        logger.debug("Weather summary error: %s", e)
        return jsonify({'available': False, 'error': str(e)})


@weather_api_bp.route('/api/weather/decisions', methods=['GET'])
def api_get_weather_decisions():
    """Get weather decision history.

    Query params:
    - days (int, default 7): how many days back
    - limit (int, default 50): max records to return
    """
    try:
        days = min(90, max(1, int(request.args.get('days', 7))))
        limit = min(200, max(1, int(request.args.get('limit', 50))))

        with sqlite3.connect(db.db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row

            # Get decisions
            cur = conn.execute(
                'SELECT * FROM weather_decisions '
                'WHERE created_at >= datetime("now", ?) '
                'ORDER BY created_at DESC LIMIT ?',
                ('-%d days' % days, limit),
            )
            rows = cur.fetchall()
            decisions = []
            for r in rows:
                d = dict(r)
                # Parse data_sources JSON
                try:
                    d['data_sources'] = json.loads(d.get('data_sources', '{}') or '{}')
                except (json.JSONDecodeError, TypeError):
                    d['data_sources'] = {}
                decisions.append(d)

            # Stats
            cur2 = conn.execute(
                'SELECT '
                '  COUNT(CASE WHEN decision IN ("skip", "stop") THEN 1 END) as skips, '
                '  COALESCE(AVG(coefficient), 100) as avg_coeff '
                'FROM weather_decisions '
                'WHERE created_at >= datetime("now", ?)',
                ('-%d days' % days,),
            )
            stats_row = cur2.fetchone()
            skips_count = int(stats_row['skips']) if stats_row else 0
            avg_coeff = int(round(float(stats_row['avg_coeff']))) if stats_row else 100
            water_saved = max(0, 100 - avg_coeff)

            return jsonify({
                'decisions': decisions,
                'total': len(decisions),
                'stats': {
                    'skips_%dd' % days: skips_count,
                    'avg_coefficient_%dd' % days: avg_coeff,
                    'water_saved_pct': water_saved,
                },
            })

    except sqlite3.OperationalError as e:
        # Table might not exist yet (migration not run)
        err_str = str(e)
        if 'no such table' in err_str:
            return jsonify({'decisions': [], 'total': 0, 'stats': {}})
        logger.debug("Weather decisions read error: %s", e)
        return jsonify({'decisions': [], 'total': 0, 'stats': {}})
    except (sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("Weather decisions read error: %s", e)
        return jsonify({'decisions': [], 'total': 0, 'stats': {}})


@weather_api_bp.route('/api/settings/weather', methods=['GET'])
@admin_required
def api_get_weather_settings():
    """Get weather adjustment settings (extended in v2)."""
    try:
        # Helper to read a setting with default
        def _get(key, default, as_type='float'):
            val = db.get_setting_value(key)
            if val is None:
                return default
            if as_type == 'bool':
                return str(val) in ('1', 'true', 'True')
            if as_type == 'float':
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return default
            if as_type == 'int':
                try:
                    return int(float(val))
                except (ValueError, TypeError):
                    return default
            return val

        return jsonify({
            'enabled': _get('weather.enabled', False, 'bool'),
            'rain_threshold_mm': _get('weather.rain_threshold_mm', 5.0),
            'freeze_threshold_c': _get('weather.freeze_threshold_c', 2.0),
            # Legacy field for backward compat
            'wind_threshold_kmh': _get('weather.wind_threshold_kmh', 25.0),
            # NEW fields
            'wind_threshold_ms': _get('weather.wind_threshold_ms', 7.0),
            'humidity_threshold_pct': _get('weather.humidity_threshold_pct', 80.0),
            'humidity_reduction_pct': _get('weather.humidity_reduction_pct', 30, 'int'),
            'factors': {
                'rain': _get('weather.factor.rain', True, 'bool'),
                'freeze': _get('weather.factor.freeze', True, 'bool'),
                'wind': _get('weather.factor.wind', True, 'bool'),
                'humidity': _get('weather.factor.humidity', True, 'bool'),
                'heat': _get('weather.factor.heat', True, 'bool'),
            },
        })
    except (sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("Weather settings read error: %s", e)
        return jsonify({'error': str(e)}), 500


@weather_api_bp.route('/api/settings/weather', methods=['PUT'])
@admin_required
def api_put_weather_settings():
    """Update weather adjustment settings (extended in v2)."""
    try:
        data = request.get_json() or {}
        ok = True

        if 'enabled' in data:
            ok = ok and db.set_setting_value('weather.enabled', '1' if data['enabled'] else '0')
        if 'rain_threshold_mm' in data:
            val = float(data['rain_threshold_mm'])
            ok = ok and db.set_setting_value('weather.rain_threshold_mm', str(max(0, min(100, val))))
        if 'freeze_threshold_c' in data:
            val = float(data['freeze_threshold_c'])
            ok = ok and db.set_setting_value('weather.freeze_threshold_c', str(max(-10, min(10, val))))

        # Legacy wind field
        if 'wind_threshold_kmh' in data:
            val = float(data['wind_threshold_kmh'])
            ok = ok and db.set_setting_value('weather.wind_threshold_kmh', str(max(5, min(100, val))))

        # NEW: wind in m/s
        if 'wind_threshold_ms' in data:
            val = float(data['wind_threshold_ms'])
            ok = ok and db.set_setting_value('weather.wind_threshold_ms', str(max(1.0, min(30.0, val))))

        # NEW: humidity threshold
        if 'humidity_threshold_pct' in data:
            val = float(data['humidity_threshold_pct'])
            ok = ok and db.set_setting_value('weather.humidity_threshold_pct', str(max(50, min(100, val))))

        # NEW: humidity reduction percentage
        if 'humidity_reduction_pct' in data:
            val = int(float(data['humidity_reduction_pct']))
            ok = ok and db.set_setting_value('weather.humidity_reduction_pct', str(max(10, min(50, val))))

        # NEW: per-factor toggles
        factors = data.get('factors')
        if factors and isinstance(factors, dict):
            for factor_name in ('rain', 'freeze', 'wind', 'humidity', 'heat'):
                if factor_name in factors:
                    key = 'weather.factor.%s' % factor_name
                    ok = ok and db.set_setting_value(key, '1' if factors[factor_name] else '0')

        return jsonify({'success': bool(ok)})
    except (sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("Weather settings write error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@weather_api_bp.route('/api/settings/location', methods=['GET'])
@admin_required
def api_get_location():
    """Get configured location (lat/lon)."""
    try:
        lat = db.get_setting_value('weather.latitude')
        lon = db.get_setting_value('weather.longitude')
        return jsonify({
            'latitude': float(lat) if lat else None,
            'longitude': float(lon) if lon else None,
        })
    except (sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("Location read error: %s", e)
        return jsonify({'latitude': None, 'longitude': None})


@weather_api_bp.route('/api/settings/location', methods=['PUT'])
@admin_required
def api_put_location():
    """Set location (lat/lon)."""
    try:
        data = request.get_json() or {}
        lat = data.get('latitude')
        lon = data.get('longitude')
        ok = True
        if lat is not None:
            ok = ok and db.set_setting_value('weather.latitude', str(float(lat)))
        if lon is not None:
            ok = ok and db.set_setting_value('weather.longitude', str(float(lon)))
        return jsonify({'success': bool(ok)})
    except (sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("Location write error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@weather_api_bp.route('/api/weather/refresh', methods=['POST'])
@admin_required
def api_refresh_weather():
    """Force refresh weather data from API."""
    try:
        from services.weather import get_weather_service
        svc = get_weather_service(db.db_path)
        weather = svc.get_weather(force_refresh=True)
        if weather:
            return jsonify({'success': True, 'data': weather.to_dict()})
        return jsonify({'success': False, 'message': 'Не удалось получить данные. Проверьте координаты.'}), 400
    except (ImportError, OSError, ValueError) as e:
        logger.debug("Weather refresh error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@weather_api_bp.route('/api/weather/log', methods=['GET'])
@admin_required
def api_get_weather_log():
    """Get weather adjustment log (last 50 entries)."""
    try:
        limit = min(100, max(1, int(request.args.get('limit', 50))))
        with sqlite3.connect(db.db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                'SELECT * FROM weather_log ORDER BY created_at DESC LIMIT ?',
                (limit,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            return jsonify({'logs': rows})
    except (sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("Weather log read error: %s", e)
        return jsonify({'logs': []})
