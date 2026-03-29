"""Weather API routes for WB-Irrigation.

Endpoints:
- GET  /api/weather          — current weather summary
- GET  /api/settings/weather — weather adjustment settings
- PUT  /api/settings/weather — update weather adjustment settings
- GET  /api/settings/location — get location (lat/lon)
- PUT  /api/settings/location — set location (lat/lon)
- POST /api/weather/refresh  — force refresh weather data
- GET  /api/weather/log      — weather adjustment log
"""
import sqlite3
import logging
import json

from flask import Blueprint, jsonify, request
from database import db
from services.security import admin_required

logger = logging.getLogger(__name__)

weather_api_bp = Blueprint('weather_api_bp', __name__)


@weather_api_bp.route('/api/weather', methods=['GET'])
def api_get_weather():
    """Get current weather summary for dashboard."""
    try:
        from services.weather import get_weather_service
        svc = get_weather_service(db.db_path)
        summary = svc.get_weather_summary()
        return jsonify(summary)
    except (ImportError, OSError, ValueError) as e:
        logger.debug("Weather summary error: %s", e)
        return jsonify({'available': False, 'error': str(e)})


@weather_api_bp.route('/api/settings/weather', methods=['GET'])
@admin_required
def api_get_weather_settings():
    """Get weather adjustment settings."""
    try:
        return jsonify({
            'enabled': str(db.get_setting_value('weather.enabled') or '0') in ('1', 'true', 'True'),
            'rain_threshold_mm': float(db.get_setting_value('weather.rain_threshold_mm') or 5.0),
            'freeze_threshold_c': float(db.get_setting_value('weather.freeze_threshold_c') or 2.0),
            'wind_threshold_kmh': float(db.get_setting_value('weather.wind_threshold_kmh') or 25.0),
        })
    except (sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("Weather settings read error: %s", e)
        return jsonify({'error': str(e)}), 500


@weather_api_bp.route('/api/settings/weather', methods=['PUT'])
@admin_required
def api_put_weather_settings():
    """Update weather adjustment settings."""
    try:
        data = request.get_json() or {}
        ok = True
        if 'enabled' in data:
            ok &= db.set_setting_value('weather.enabled', '1' if data['enabled'] else '0')
        if 'rain_threshold_mm' in data:
            val = float(data['rain_threshold_mm'])
            ok &= db.set_setting_value('weather.rain_threshold_mm', str(max(0, min(100, val))))
        if 'freeze_threshold_c' in data:
            val = float(data['freeze_threshold_c'])
            ok &= db.set_setting_value('weather.freeze_threshold_c', str(max(-10, min(10, val))))
        if 'wind_threshold_kmh' in data:
            val = float(data['wind_threshold_kmh'])
            ok &= db.set_setting_value('weather.wind_threshold_kmh', str(max(5, min(100, val))))
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
            ok &= db.set_setting_value('weather.latitude', str(float(lat)))
        if lon is not None:
            ok &= db.set_setting_value('weather.longitude', str(float(lon)))
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
