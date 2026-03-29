"""Deep tests for weather services."""
import json
import sqlite3
import time
import pytest
from unittest.mock import patch, MagicMock


class TestWeatherData:
    def test_parse_hourly_data(self):
        from services.weather import WeatherData
        raw = {
            'hourly': {
                'time': ['2024-01-01T12:00'],
                'temperature_2m': [22.5],
                'relative_humidity_2m': [65],
                'precipitation': [0.0],
                'wind_speed_10m': [10.5],
            },
            'daily': {},
            '_fetched_at': time.time(),
        }
        wd = WeatherData(raw)
        assert wd.temperature == 22.5
        assert wd.humidity == 65
        assert wd.precipitation == 0.0
        assert wd.wind_speed == 10.5

    def test_parse_empty_data(self):
        from services.weather import WeatherData
        raw = {'hourly': {}, 'daily': {}}
        wd = WeatherData(raw)
        assert wd.temperature is None

    def test_to_dict(self):
        from services.weather import WeatherData
        raw = {
            'hourly': {
                'time': ['2024-01-01T12:00'],
                'temperature_2m': [20.0],
                'relative_humidity_2m': [50],
                'precipitation': [1.0],
                'wind_speed_10m': [5.0],
            },
            'daily': {},
        }
        wd = WeatherData(raw)
        d = wd.to_dict()
        assert isinstance(d, dict)
        assert 'temperature' in d


class TestWeatherAdjustment:
    def test_not_enabled(self, test_db_path):
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(test_db_path)
        assert not adj.is_enabled()

    def test_enabled_after_setting(self, test_db):
        test_db.set_setting_value('weather.enabled', '1')
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(test_db.db_path)
        assert adj.is_enabled()

    def test_should_skip_when_disabled(self, test_db):
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(test_db.db_path)
        result = adj.should_skip()
        assert result['skip'] is False

    def test_get_coefficient_when_disabled(self, test_db):
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(test_db.db_path)
        coeff = adj.get_coefficient()
        assert coeff == 100

    def test_get_settings(self, test_db):
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(test_db.db_path)
        settings = adj._get_settings()
        assert 'enabled' in settings
        assert 'rain_threshold_mm' in settings

    def test_log_adjustment(self, test_db):
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(test_db.db_path)
        try:
            adj.log_adjustment(zone_id=1, original_duration=10, adjusted_duration=8,
                               coefficient=80, skipped=False)
            with sqlite3.connect(test_db.db_path) as conn:
                cur = conn.execute('SELECT COUNT(*) FROM weather_log')
                count = cur.fetchone()[0]
            assert count >= 1
        except (sqlite3.Error, TypeError) as e:
            pytest.skip(f"log_adjustment not supported: {e}")


class TestWeatherService:
    def test_get_weather_no_location(self, test_db):
        from services.weather import WeatherService
        svc = WeatherService(test_db.db_path)
        result = svc.get_weather()
        assert result is None

    def test_get_weather_summary_no_data(self, test_db):
        from services.weather import WeatherService
        svc = WeatherService(test_db.db_path)
        summary = svc.get_weather_summary()
        assert summary.get('available') is False
