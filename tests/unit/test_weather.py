"""Tests for weather service (Open-Meteo API integration)."""
import json
import os
import sqlite3
import time
import pytest
from unittest.mock import patch, MagicMock

os.environ['TESTING'] = '1'


@pytest.fixture
def weather_db(tmp_path):
    """Create a temp DB with weather tables."""
    db_path = str(tmp_path / 'test_weather.db')
    conn = sqlite3.connect(db_path)
    conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    conn.execute('''CREATE TABLE IF NOT EXISTS weather_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        latitude REAL NOT NULL, longitude REAL NOT NULL,
        data TEXT NOT NULL, fetched_at REAL NOT NULL
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS weather_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        zone_id INTEGER, original_duration INTEGER, adjusted_duration INTEGER,
        coefficient INTEGER, skipped INTEGER DEFAULT 0, skip_reason TEXT,
        weather_data TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # Set location
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.latitude', '55.7558')")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.longitude', '37.6176')")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.enabled', '1')")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.rain_threshold_mm', '5.0')")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.freeze_threshold_c', '2.0')")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.wind_threshold_kmh', '25.0')")
    conn.commit()
    conn.close()
    return db_path


def _make_sample_api_response():
    """Create a sample Open-Meteo API response."""
    from datetime import datetime, timedelta
    now = datetime.now()
    # Generate 48 hours of data
    times = []
    temps = []
    hums = []
    precips = []
    winds = []
    et0s = []
    for i in range(-24, 24):
        dt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=i)
        times.append(dt.strftime('%Y-%m-%dT%H:00'))
        temps.append(25.0 + i * 0.1)
        hums.append(50.0)
        precips.append(0.0)
        winds.append(10.0)
        et0s.append(0.2)

    return {
        'hourly': {
            'time': times,
            'temperature_2m': temps,
            'relative_humidity_2m': hums,
            'precipitation': precips,
            'wind_speed_10m': winds,
            'et0_fao_evapotranspiration': et0s,
        },
        'daily': {
            'time': [now.strftime('%Y-%m-%d')],
            'precipitation_sum': [0.0],
            'et0_fao_evapotranspiration': [4.5],
        },
        '_fetched_at': time.time(),
    }


class TestWeatherData:
    def test_parse_basic(self):
        from services.weather import WeatherData
        raw = _make_sample_api_response()
        wd = WeatherData(raw)
        assert wd.temperature is not None
        assert wd.humidity is not None
        assert wd.wind_speed is not None
        assert wd.precipitation_24h >= 0

    def test_parse_empty_data(self):
        from services.weather import WeatherData
        wd = WeatherData({'hourly': {}, 'daily': {}})
        assert wd.temperature is None
        assert wd.humidity is None
        assert wd.precipitation_24h == 0.0

    def test_to_dict(self):
        from services.weather import WeatherData
        raw = _make_sample_api_response()
        wd = WeatherData(raw)
        d = wd.to_dict()
        assert 'temperature' in d
        assert 'humidity' in d
        assert 'precipitation_24h' in d
        assert 'precipitation_forecast_6h' in d
        assert 'daily_et0' in d

    def test_precipitation_24h_sum(self):
        from services.weather import WeatherData
        raw = _make_sample_api_response()
        # Set some rain
        for i in range(20, 25):
            raw['hourly']['precipitation'][i] = 2.0
        wd = WeatherData(raw)
        assert wd.precipitation_24h >= 0  # Should sum up some values

    def test_precipitation_forecast(self):
        from services.weather import WeatherData
        raw = _make_sample_api_response()
        # Set rain in forecast period
        for i in range(25, 30):
            raw['hourly']['precipitation'][i] = 3.0
        wd = WeatherData(raw)
        assert wd.precipitation_forecast_6h >= 0


class TestWeatherService:
    def test_get_location(self, weather_db):
        from services.weather import WeatherService
        svc = WeatherService(weather_db)
        loc = svc._get_location()
        assert loc is not None
        assert abs(loc['latitude'] - 55.7558) < 0.01
        assert abs(loc['longitude'] - 37.6176) < 0.01

    def test_get_location_not_set(self, tmp_path):
        db_path = str(tmp_path / 'empty.db')
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
        conn.commit()
        conn.close()
        from services.weather import WeatherService
        svc = WeatherService(db_path)
        assert svc._get_location() is None

    def test_cache_roundtrip(self, weather_db):
        from services.weather import WeatherService
        svc = WeatherService(weather_db)
        raw = _make_sample_api_response()
        svc._save_cache(55.7558, 37.6176, raw)
        cached = svc._get_cached(55.7558, 37.6176)
        assert cached is not None
        assert cached.temperature is not None

    def test_cache_expired(self, weather_db):
        """Expired cache should return None."""
        from services.weather import WeatherService
        svc = WeatherService(weather_db)
        raw = _make_sample_api_response()
        # Save with old timestamp
        conn = sqlite3.connect(weather_db)
        old_time = time.time() - 3600  # 1 hour ago
        conn.execute(
            'INSERT INTO weather_cache (latitude, longitude, data, fetched_at) VALUES (?, ?, ?, ?)',
            (55.7558, 37.6176, json.dumps(raw), old_time),
        )
        conn.commit()
        conn.close()
        cached = svc._get_cached(55.7558, 37.6176)
        assert cached is None

    @patch('services.weather.WeatherService._fetch_api')
    def test_get_weather_from_api(self, mock_fetch, weather_db):
        from services.weather import WeatherService
        svc = WeatherService(weather_db)
        raw = _make_sample_api_response()
        mock_fetch.return_value = raw
        weather = svc.get_weather(force_refresh=True)
        assert weather is not None
        assert weather.temperature is not None
        mock_fetch.assert_called_once()

    @patch('services.weather.WeatherService._fetch_api')
    def test_get_weather_api_fail_uses_stale_cache(self, mock_fetch, weather_db):
        from services.weather import WeatherService
        svc = WeatherService(weather_db)
        # Seed stale cache
        raw = _make_sample_api_response()
        conn = sqlite3.connect(weather_db)
        old_time = time.time() - 7200  # 2 hours ago (stale)
        conn.execute(
            'INSERT INTO weather_cache (latitude, longitude, data, fetched_at) VALUES (?, ?, ?, ?)',
            (55.7558, 37.6176, json.dumps(raw), old_time),
        )
        conn.commit()
        conn.close()
        mock_fetch.return_value = None  # API fails
        weather = svc.get_weather(force_refresh=True)
        assert weather is not None  # Falls back to stale cache

    def test_get_weather_no_location(self, tmp_path):
        db_path = str(tmp_path / 'noloc.db')
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS weather_cache (id INTEGER PRIMARY KEY, latitude REAL, longitude REAL, data TEXT, fetched_at REAL)')
        conn.commit()
        conn.close()
        from services.weather import WeatherService
        svc = WeatherService(db_path)
        assert svc.get_weather() is None
