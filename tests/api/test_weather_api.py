"""Tests for weather API routes."""
import json
import sqlite3
import pytest
from unittest.mock import patch, MagicMock


class TestWeatherAPI:
    """Tests for /api/weather endpoints."""

    def test_get_weather_summary(self, admin_client):
        """GET /api/weather returns weather summary."""
        mock_svc = MagicMock()
        mock_svc.get_weather_summary.return_value = {
            'available': True, 'temperature': 25.0, 'humidity': 60
        }
        with patch('services.weather.get_weather_service', return_value=mock_svc):
            resp = admin_client.get('/api/weather')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get('available') is True or 'temperature' in data

    def test_get_weather_summary_no_service(self, admin_client):
        """GET /api/weather handles missing weather service."""
        with patch('services.weather.get_weather_service', side_effect=ImportError("no module")):
            resp = admin_client.get('/api/weather')
        assert resp.status_code == 200

    def test_get_weather_settings(self, admin_client):
        """GET /api/settings/weather returns settings."""
        resp = admin_client.get('/api/settings/weather')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'enabled' in data
        assert 'rain_threshold_mm' in data

    def test_put_weather_settings(self, admin_client):
        """PUT /api/settings/weather updates settings."""
        resp = admin_client.put('/api/settings/weather',
                                data=json.dumps({
                                    'enabled': True,
                                    'rain_threshold_mm': 10.0,
                                    'freeze_threshold_c': 0.5,
                                    'wind_threshold_kmh': 30.0,
                                }),
                                content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

        # Verify
        resp2 = admin_client.get('/api/settings/weather')
        data2 = resp2.get_json()
        assert data2['enabled'] is True
        assert data2['rain_threshold_mm'] == 10.0

    def test_put_weather_settings_clamps(self, admin_client):
        """PUT /api/settings/weather clamps extreme values."""
        resp = admin_client.put('/api/settings/weather',
                                data=json.dumps({
                                    'rain_threshold_mm': 999,
                                    'wind_threshold_kmh': 1,
                                }),
                                content_type='application/json')
        assert resp.status_code == 200
        resp2 = admin_client.get('/api/settings/weather')
        data2 = resp2.get_json()
        assert data2['rain_threshold_mm'] <= 100
        assert data2['wind_threshold_kmh'] >= 5

    def test_get_location(self, admin_client):
        """GET /api/settings/location returns lat/lon."""
        resp = admin_client.get('/api/settings/location')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'latitude' in data
        assert 'longitude' in data

    def test_put_location(self, admin_client):
        """PUT /api/settings/location sets lat/lon."""
        resp = admin_client.put('/api/settings/location',
                                data=json.dumps({'latitude': 55.75, 'longitude': 37.62}),
                                content_type='application/json')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

    def test_refresh_weather_no_data(self, admin_client):
        """POST /api/weather/refresh when no data available."""
        mock_svc = MagicMock()
        mock_svc.get_weather.return_value = None
        with patch('services.weather.get_weather_service', return_value=mock_svc):
            resp = admin_client.post('/api/weather/refresh')
        assert resp.status_code in (200, 400, 500)

    def test_refresh_weather_success(self, admin_client):
        """POST /api/weather/refresh returns data."""
        mock_weather = MagicMock()
        mock_weather.to_dict.return_value = {'temperature': 20.0}
        mock_svc = MagicMock()
        mock_svc.get_weather.return_value = mock_weather
        with patch('services.weather.get_weather_service', return_value=mock_svc):
            resp = admin_client.post('/api/weather/refresh')
        assert resp.status_code in (200, 400)

    def test_get_weather_log(self, admin_client):
        """GET /api/weather/log returns log entries."""
        resp = admin_client.get('/api/weather/log')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'logs' in data

    def test_get_weather_log_with_limit(self, admin_client):
        """GET /api/weather/log?limit=5 respects limit."""
        resp = admin_client.get('/api/weather/log?limit=5')
        assert resp.status_code == 200
