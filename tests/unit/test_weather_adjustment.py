"""Tests for weather adjustment engine (rain/freeze/wind skip, coefficients)."""

import json
import os
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

os.environ["TESTING"] = "1"


@pytest.fixture
def adj_db(tmp_path):
    """Create a temp DB with weather settings."""
    db_path = str(tmp_path / "test_adj.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("""CREATE TABLE IF NOT EXISTS weather_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        latitude REAL NOT NULL, longitude REAL NOT NULL,
        data TEXT NOT NULL, fetched_at REAL NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS weather_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        zone_id INTEGER, original_duration INTEGER, adjusted_duration INTEGER,
        coefficient INTEGER, skipped INTEGER DEFAULT 0, skip_reason TEXT,
        weather_data TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS weather_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, time TEXT NOT NULL,
        temperature REAL, humidity REAL, precipitation_24h REAL, wind_speed REAL,
        coefficient INTEGER NOT NULL, decision TEXT NOT NULL, reason TEXT,
        mode TEXT NOT NULL DEFAULT 'auto', data_sources TEXT DEFAULT '{}',
        user_override INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.enabled', '1')")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.latitude', '55.7558')")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.longitude', '37.6176')")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.rain_threshold_mm', '5.0')")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.freeze_threshold_c', '2.0')")
    conn.execute("INSERT INTO settings(key, value) VALUES('weather.wind_threshold_kmh', '25.0')")
    conn.commit()
    conn.close()
    return db_path


def _mock_weather(
    temperature=25.0,
    humidity=50.0,
    precipitation_24h=0.0,
    precipitation_forecast_6h=0.0,
    wind_speed=10.0,
    daily_et0=4.5,
    min_temp_forecast_6h=None,
):
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
    mock.min_temp_forecast_6h = min_temp_forecast_6h if min_temp_forecast_6h is not None else temperature
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
        assert result["skip"] is False

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_skip_rain_24h(self, mock_get, adj_db):
        """Rain > threshold in 24h → skip."""
        mock_get.return_value = _mock_weather(precipitation_24h=8.0)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result["skip"] is True
        assert "rain" in result["reason"].lower()
        assert result["details"]["type"] == "rain"

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_skip_rain_forecast(self, mock_get, adj_db):
        """Rain forecast > threshold → skip."""
        mock_get.return_value = _mock_weather(precipitation_forecast_6h=7.0)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result["skip"] is True
        assert "forecast" in result["reason"].lower()

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_skip_freeze(self, mock_get, adj_db):
        """Temperature < freeze threshold → skip."""
        mock_get.return_value = _mock_weather(temperature=-1.0)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result["skip"] is True
        assert "freeze" in result["reason"].lower()
        assert result["details"]["type"] == "freeze"

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_skip_wind(self, mock_get, adj_db):
        """Wind > threshold → skip."""
        mock_get.return_value = _mock_weather(wind_speed=30.0)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result["skip"] is True
        assert "wind" in result["reason"].lower()

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_no_skip_normal(self, mock_get, adj_db):
        """Normal weather → no skip."""
        mock_get.return_value = _mock_weather(temperature=25.0, precipitation_24h=1.0, wind_speed=5.0)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result["skip"] is False

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_no_skip_api_unavailable(self, mock_get, adj_db):
        """API unavailable → fail safe rather than inventing dry weather."""
        mock_get.return_value = None
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result["skip"] is True
        assert result["details"]["type"] == "weather_unavailable"
        assert result["details"].get("api_unavailable") is True

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_skip_rain_at_threshold(self, mock_get, adj_db):
        """Rain exactly at the configured safety threshold skips."""
        mock_get.return_value = _mock_weather(precipitation_24h=5.0)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result["skip"] is True
        assert result["details"]["type"] == "rain"

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_skip_freeze_at_threshold(self, mock_get, adj_db):
        """Temperature exactly at the freeze threshold skips."""
        mock_get.return_value = _mock_weather(temperature=2.0)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        result = adj.should_skip()
        assert result["skip"] is True
        assert result["details"]["type"] == "freeze"


class TestWeatherCoefficient:
    """Test get_coefficient() logic."""

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_coeff_normal(self, mock_get, adj_db):
        """Normal 25°C, 50% humidity → coefficient ~100%."""
        mock_get.return_value = _mock_weather(temperature=25.0, humidity=50.0, daily_et0=4.5)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        coeff = adj.get_coefficient()
        assert 90 <= coeff <= 120  # Near 100%

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_coeff_hot(self, mock_get, adj_db):
        """Hot weather (>35°C) → coefficient > 130%."""
        mock_get.return_value = _mock_weather(temperature=38.0, humidity=30.0, daily_et0=7.0)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        coeff = adj.get_coefficient()
        assert coeff > 130

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_coeff_cold(self, mock_get, adj_db):
        """Cold weather (<10°C) → coefficient < 70%."""
        mock_get.return_value = _mock_weather(temperature=8.0, humidity=70.0, daily_et0=1.0)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        coeff = adj.get_coefficient()
        assert coeff < 70

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_coeff_humid(self, mock_get, adj_db):
        """High humidity (>80%) → coefficient < 100%."""
        mock_get.return_value = _mock_weather(temperature=25.0, humidity=85.0, daily_et0=4.5)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        coeff = adj.get_coefficient()
        assert coeff < 100

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_coeff_with_rain(self, mock_get, adj_db):
        """Some rain (3mm) → coefficient reduced."""
        mock_get.return_value = _mock_weather(precipitation_24h=3.0, daily_et0=4.5)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        coeff = adj.get_coefficient()
        assert coeff < 100

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_coeff_clamped_max(self, mock_get, adj_db):
        """Coefficient never exceeds 200%."""
        mock_get.return_value = _mock_weather(temperature=40.0, humidity=10.0, daily_et0=12.0, wind_speed=20.0)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        coeff = adj.get_coefficient()
        assert coeff <= 200

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
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

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_coeff_api_unavailable(self, mock_get, adj_db):
        """API unavailable → coefficient is fail-safe zero."""
        mock_get.return_value = None
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        assert adj.get_coefficient() == 0


class TestAdjustDuration:
    """Test duration adjustment."""

    @patch("services.weather_adjustment.WeatherAdjustment.get_coefficient")
    def test_adjust_150pct(self, mock_coeff, adj_db):
        mock_coeff.return_value = 150
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        assert adj.adjust_duration(10) == 15

    @patch("services.weather_adjustment.WeatherAdjustment.get_coefficient")
    def test_adjust_50pct(self, mock_coeff, adj_db):
        mock_coeff.return_value = 50
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        assert adj.adjust_duration(10) == 5

    @patch("services.weather_adjustment.WeatherAdjustment.get_coefficient")
    def test_adjust_min_1min(self, mock_coeff, adj_db):
        """Adjusted duration minimum is 1 minute when coefficient > 0."""
        mock_coeff.return_value = 10  # 10% of 10 = 1
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        assert adj.adjust_duration(10) == 1

    @patch("services.weather_adjustment.WeatherAdjustment.get_coefficient")
    def test_adjust_zero_coeff(self, mock_coeff, adj_db):
        """Zero coefficient (skip case) → 0 minutes."""
        mock_coeff.return_value = 0
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        assert adj.adjust_duration(10) == 0


class TestWeatherSafetyChecks:
    """Issue #27: safety thresholds must apply even when factor_* flags are off."""

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_coef_zero_on_heavy_rain_with_factor_disabled(self, mock_get, adj_db):
        """factor_rain=False but rain >> threshold → coef=0 (hard safety)."""
        conn = sqlite3.connect(adj_db)
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('weather.factor.rain','0')")
        conn.commit()
        conn.close()
        mock_get.return_value = _mock_weather(precipitation_24h=50.0)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        assert adj.get_coefficient() == 0

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_coef_no_zerodiv_when_threshold_zero(self, mock_get, adj_db):
        """rain_threshold_mm=0 + rain_24h>0 must NOT divide by zero."""
        conn = sqlite3.connect(adj_db)
        conn.execute("UPDATE settings SET value='0' WHERE key='weather.rain_threshold_mm'")
        conn.commit()
        conn.close()
        mock_get.return_value = _mock_weather(precipitation_24h=2.0)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        # threshold=0 ⇒ safety check trips on any rain > 0 → coef=0.
        # The point of this test is "no ZeroDivisionError".
        coeff = adj.get_coefficient()
        assert isinstance(coeff, int)
        assert 0 <= coeff <= 200

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_safety_check_freeze(self, mock_get, adj_db):
        """temp << freeze_threshold → _check_safety_skip True → coef=0."""
        mock_get.return_value = _mock_weather(temperature=-10.0)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        settings = adj._get_settings()
        weather = adj._get_weather()
        assert adj._check_safety_skip(weather, settings) is True
        assert adj.get_coefficient() == 0


class TestWeatherLog:
    """Test weather logging."""

    def test_log_adjustment(self, adj_db):
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        adj.log_adjustment(1, 10, 8, 80, False)
        conn = sqlite3.connect(adj_db)
        cur = conn.execute("SELECT * FROM weather_log")
        rows = cur.fetchall()
        assert len(rows) == 1
        conn.close()

    def test_log_skip(self, adj_db):
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        adj.log_adjustment(1, 10, 0, 0, True, "rain_skip: 8mm")
        conn = sqlite3.connect(adj_db)
        cur = conn.execute("SELECT * FROM weather_log WHERE skipped = 1")
        rows = cur.fetchall()
        assert len(rows) == 1
        conn.close()


class TestLogAdjustmentSnapshot:
    """Issue M5: log_adjustment must persist weather_data JSON snapshot."""

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_log_adjustment_with_snapshot(self, mock_get, adj_db):
        """Explicit snapshot dict → stored as JSON in weather_data."""
        mock_get.return_value = None  # don't auto-fetch
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        snap = {"temperature": 22.5, "humidity": 60, "precipitation_24h": 1.2}
        adj.log_adjustment(1, 10, 8, 80, False, "", weather_snapshot=snap)
        conn = sqlite3.connect(adj_db)
        cur = conn.execute("SELECT weather_data FROM weather_log ORDER BY id DESC LIMIT 1")
        (data,) = cur.fetchone()
        conn.close()
        decoded = json.loads(data)
        assert decoded["temperature"] == 22.5
        assert decoded["humidity"] == 60

    @patch("services.weather_adjustment.WeatherAdjustment._get_weather")
    def test_log_adjustment_auto_snapshot(self, mock_get, adj_db):
        """No snapshot passed → auto-fetched via _get_weather().to_dict()."""
        mock_get.return_value = _mock_weather(temperature=18.0, humidity=70.0)
        # Force MagicMock.to_dict() to produce a real dict.
        mock_get.return_value.to_dict.return_value = {"temperature": 18.0, "humidity": 70.0}
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        adj.log_adjustment(1, 10, 9, 90, False)
        conn = sqlite3.connect(adj_db)
        cur = conn.execute("SELECT weather_data FROM weather_log ORDER BY id DESC LIMIT 1")
        (data,) = cur.fetchone()
        conn.close()
        decoded = json.loads(data)
        assert decoded.get("temperature") == 18.0


class TestLogDecision:
    """Issue #5: log_decision writes to weather_decisions for UI history."""

    @pytest.mark.parametrize(
        "coef,skip,expected",
        [
            (50, False, "adjust"),
            (100, False, "water"),
            (150, False, "adjust"),
            (0, True, "skip"),
        ],
    )
    def test_log_decision_skip_adjust_water(self, adj_db, coef, skip, expected):
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        weather = _mock_weather(temperature=20.0, humidity=55.0, precipitation_24h=0.5, wind_speed=3.0)
        adj.log_decision(weather, coef, skip, "test reason")
        conn = sqlite3.connect(adj_db)
        cur = conn.execute(
            "SELECT decision, coefficient, temperature, mode, user_override "
            "FROM weather_decisions ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        conn.close()
        assert row is not None
        decision, coefficient, temperature, mode, user_override = row
        assert decision == expected
        assert coefficient == coef
        assert temperature == 20.0
        assert mode == "auto"
        assert user_override == 0

    def test_log_decision_none_safe(self, adj_db):
        """None weather attrs → NULLs in DB, no crash."""
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        weather = _mock_weather(temperature=None, humidity=None, precipitation_24h=None, wind_speed=None)
        adj.log_decision(weather, 100, False, "")
        conn = sqlite3.connect(adj_db)
        cur = conn.execute("SELECT temperature, humidity FROM weather_decisions ORDER BY id DESC LIMIT 1")
        temp, hum = cur.fetchone()
        conn.close()
        assert temp is None and hum is None


class TestApiDownAlert:
    """Issue M4: throttled Telegram alert when weather API returns None / stale."""

    def _set_admin_chat(self, db_path, chat_id="12345"):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('telegram_admin_chat_id', ?)",
            (chat_id,),
        )
        conn.commit()
        conn.close()

    @patch("services.weather.adjustment.get_weather_service")
    def test_alert_throttle_30min(self, mock_svc, adj_db):
        """Two _maybe_alert_api_down within 30min → notifier.send_text called once."""
        self._set_admin_chat(adj_db)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        with patch("services.telegram_bot.notifier") as mock_notifier:
            adj._maybe_alert_api_down("weather=None")
            adj._maybe_alert_api_down("weather=None")
            assert mock_notifier.send_text.call_count == 1

    @patch("services.weather.adjustment.get_weather_service")
    def test_alert_after_30min(self, mock_svc, adj_db):
        """After 30min throttle window → second alert allowed."""
        self._set_admin_chat(adj_db)
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        with patch("services.telegram_bot.notifier") as mock_notifier:
            with patch("services.weather.adjustment.time.time", return_value=1000.0):
                adj._maybe_alert_api_down("weather=None")
            with patch("services.weather.adjustment.time.time", return_value=1000.0 + 1801):
                adj._maybe_alert_api_down("weather=None")
            assert mock_notifier.send_text.call_count == 2

    def test_no_alert_when_disabled(self, adj_db):
        """weather.enabled=0 + weather=None → still no alert (disabled = no need)."""
        # Disable weather adjustment.
        conn = sqlite3.connect(adj_db)
        conn.execute("UPDATE settings SET value='0' WHERE key='weather.enabled'")
        conn.commit()
        conn.close()
        from services.weather_adjustment import WeatherAdjustment

        adj = WeatherAdjustment(adj_db)
        # Public path: should_skip() short-circuits when disabled and never
        # reaches _get_weather → no alert is fired.
        with patch("services.telegram_bot.notifier") as mock_notifier:
            result = adj.should_skip()
            assert result["skip"] is False
            assert mock_notifier.send_text.call_count == 0
