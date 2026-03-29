"""Tests for weather adjustment engine (rain/freeze/wind skip, coefficients)."""
import json
import os
import sqlite3
import time
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

os.environ['TESTING'] = '1'


@pytest.fixture
def adj_db(tmp_path):
    """Create a temp DB with weather settings."""
    db_path = str(tmp_path / 'test_adj.db')
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
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.enabled', '1')")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.latitude', '55.7558')")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.longitude', '37.6176')")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.rain_threshold_mm', '5.0')")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.freeze_threshold_c', '2.0')")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.wind_threshold_kmh', '25.0')")
    conn.commit()
    conn.close()
    return db_path


def _mock_weather(temperature=25.0, humidity=50.0, precipitation_24h=0.0,
                  precipitation_forecast_6h=0.0, wind_speed=10.0, daily_et0=4.5):
    """Create a mock WeatherData object."""
    mock = MagicMock()
    mock.temperature = temperature
    mock.humidity = humidity
    mock.precipitation_24h = precipitation_24h
    mock.precipitation_forecast_6h = precipitation_forecast_6h
    mock.wind_speed = wind_speed
    mock.daily_et0 = daily_et0
    mock.precipitation = 0.0
    mock.et0_hourly = 0.2
    return mock


class TestWeatherAdjustmentSkip:
    """Test should_skip() logic."""

    def test_skip_disabled(self, adj_db):
        """When weather adjustment is disabled, never skip."""
        conn = sqlite3.connect(adj_db)
        conn.execute("UPDATE settings SET value='0' WHERE key='weather.enabled'")
        conn.commit()
        conn.close()
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result['skip'] is False

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_skip_rain_24h(self, mock_get, adj_db):
        """Rain > threshold in 24h → skip."""
        mock_get.return_value = _mock_weather(precipitation_24h=8.0)
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result['skip'] is True
        assert 'rain' in result['reason'].lower()
        assert result['details']['type'] == 'rain'

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_skip_rain_forecast(self, mock_get, adj_db):
        """Rain forecast > threshold → skip."""
        mock_get.return_value = _mock_weather(precipitation_forecast_6h=7.0)
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result['skip'] is True
        assert 'forecast' in result['reason'].lower()

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_skip_freeze(self, mock_get, adj_db):
        """Temperature < freeze threshold → skip."""
        mock_get.return_value = _mock_weather(temperature=-1.0)
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result['skip'] is True
        assert 'freeze' in result['reason'].lower()
        assert result['details']['type'] == 'freeze'

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_skip_wind(self, mock_get, adj_db):
        """Wind > threshold → skip."""
        mock_get.return_value = _mock_weather(wind_speed=30.0)
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result['skip'] is True
        assert 'wind' in result['reason'].lower()

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_no_skip_normal(self, mock_get, adj_db):
        """Normal weather → no skip."""
        mock_get.return_value = _mock_weather(temperature=25.0, precipitation_24h=1.0, wind_speed=5.0)
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result['skip'] is False

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_no_skip_api_unavailable(self, mock_get, adj_db):
        """API unavailable → don't skip (water normally)."""
        mock_get.return_value = None
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result['skip'] is False
        assert result['details'].get('api_unavailable') is True

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_skip_rain_at_threshold(self, mock_get, adj_db):
        """Rain exactly at threshold should NOT skip (only > threshold)."""
        mock_get.return_value = _mock_weather(precipitation_24h=5.0)
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result['skip'] is False

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_skip_freeze_at_threshold(self, mock_get, adj_db):
        """Temp exactly at threshold should NOT skip (only < threshold)."""
        mock_get.return_value = _mock_weather(temperature=2.0)
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result['skip'] is False


class TestWeatherCoefficient:
    """Test get_coefficient() logic."""

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_coeff_normal(self, mock_get, adj_db):
        """Normal 25°C, 50% humidity → coefficient ~100%."""
        mock_get.return_value = _mock_weather(temperature=25.0, humidity=50.0, daily_et0=4.5)
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        coeff = adj.get_coefficient()
        assert 90 <= coeff <= 120  # Near 100%

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_coeff_hot(self, mock_get, adj_db):
        """Hot weather (>35°C) → coefficient > 130%."""
        mock_get.return_value = _mock_weather(temperature=38.0, humidity=30.0, daily_et0=7.0)
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        coeff = adj.get_coefficient()
        assert coeff > 130

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_coeff_cold(self, mock_get, adj_db):
        """Cold weather (<10°C) → coefficient < 70%."""
        mock_get.return_value = _mock_weather(temperature=8.0, humidity=70.0, daily_et0=1.0)
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        coeff = adj.get_coefficient()
        assert coeff < 70

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_coeff_humid(self, mock_get, adj_db):
        """High humidity (>80%) → coefficient < 100%."""
        mock_get.return_value = _mock_weather(temperature=25.0, humidity=85.0, daily_et0=4.5)
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        coeff = adj.get_coefficient()
        assert coeff < 100

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_coeff_with_rain(self, mock_get, adj_db):
        """Some rain (3mm) → coefficient reduced."""
        mock_get.return_value = _mock_weather(precipitation_24h=3.0, daily_et0=4.5)
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        coeff = adj.get_coefficient()
        assert coeff < 100

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_coeff_clamped_max(self, mock_get, adj_db):
        """Coefficient never exceeds 200%."""
        mock_get.return_value = _mock_weather(temperature=40.0, humidity=10.0, daily_et0=12.0, wind_speed=20.0)
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        coeff = adj.get_coefficient()
        assert coeff <= 200

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_coeff_clamped_min(self, mock_get, adj_db):
        """Coefficient never goes below 0%."""
        mock_get.return_value = _mock_weather(temperature=5.0, humidity=95.0, precipitation_24h=4.9, daily_et0=0.5)
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        coeff = adj.get_coefficient()
        assert coeff >= 0

    def test_coeff_disabled(self, adj_db):
        """When disabled, coefficient = 100%."""
        conn = sqlite3.connect(adj_db)
        conn.execute("UPDATE settings SET value='0' WHERE key='weather.enabled'")
        conn.commit()
        conn.close()
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        assert adj.get_coefficient() == 100

    @patch('services.weather_adjustment.WeatherAdjustment._get_weather')
    def test_coeff_api_unavailable(self, mock_get, adj_db):
        """API unavailable → coefficient = 100% (water normally)."""
        mock_get.return_value = None
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        assert adj.get_coefficient() == 100


class TestAdjustDuration:
    """Test duration adjustment."""

    @patch('services.weather_adjustment.WeatherAdjustment.get_coefficient')
    def test_adjust_150pct(self, mock_coeff, adj_db):
        mock_coeff.return_value = 150
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        assert adj.adjust_duration(10) == 15

    @patch('services.weather_adjustment.WeatherAdjustment.get_coefficient')
    def test_adjust_50pct(self, mock_coeff, adj_db):
        mock_coeff.return_value = 50
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        assert adj.adjust_duration(10) == 5

    @patch('services.weather_adjustment.WeatherAdjustment.get_coefficient')
    def test_adjust_min_1min(self, mock_coeff, adj_db):
        """Adjusted duration minimum is 1 minute when coefficient > 0."""
        mock_coeff.return_value = 10  # 10% of 10 = 1
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        assert adj.adjust_duration(10) == 1

    @patch('services.weather_adjustment.WeatherAdjustment.get_coefficient')
    def test_adjust_zero_coeff(self, mock_coeff, adj_db):
        """Zero coefficient (skip case) → 0 minutes."""
        mock_coeff.return_value = 0
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        assert adj.adjust_duration(10) == 0


class TestWeatherLog:
    """Test weather logging."""

    def test_log_adjustment(self, adj_db):
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        adj.log_adjustment(1, 10, 8, 80, False)
        conn = sqlite3.connect(adj_db)
        cur = conn.execute('SELECT * FROM weather_log')
        rows = cur.fetchall()
        assert len(rows) == 1
        conn.close()

    def test_log_skip(self, adj_db):
        from services.weather_adjustment import WeatherAdjustment
        adj = WeatherAdjustment(adj_db)
        adj.log_adjustment(1, 10, 0, 0, True, 'rain_skip: 8mm')
        conn = sqlite3.connect(adj_db)
        cur = conn.execute('SELECT * FROM weather_log WHERE skipped = 1')
        rows = cur.fetchall()
        assert len(rows) == 1
        conn.close()
