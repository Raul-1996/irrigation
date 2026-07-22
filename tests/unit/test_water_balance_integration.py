"""Integration tests for the H2 shadow contract and the H1 regression guard.

- ``get_effective_coefficient()`` remains an H1 compatibility alias even when
  a fresh H2 diagnostic coefficient exists.
- H1 requests one past day for rolling rain while H2 history remains a
  separate daily-only, cache-independent request.
"""

import os
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
import requests

os.environ["TESTING"] = "1"

from services.weather import client as wc
from services.weather.adjustment import WeatherAdjustment


@pytest.fixture
def adj_db(tmp_path):
    db_path = str(tmp_path / "test_eff.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    seed = {
        "weather.enabled": "1",
        "weather.balance.enabled": "0",
        "weather.balance.stale_fallback_days": "2",
        "weather.balance.coef_cached": "130",
    }
    for k, v in seed.items():
        conn.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (k, v))
    conn.commit()
    conn.close()
    return db_path


def _set(db_path, key, value):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, value))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# H2 shadow contract (PR-060)
# ---------------------------------------------------------------------------


class TestEffectiveCoefficient:
    def test_disabled_returns_h1_untouched(self, adj_db):
        """Balance disabled → compatibility alias uses H1."""
        adj = WeatherAdjustment(adj_db)
        with patch.object(adj, "get_coefficient", return_value=77) as h1:
            assert adj.get_effective_coefficient() == 77
            h1.assert_called_once()

    def test_enabled_fresh_still_returns_h1(self, adj_db):
        """A fresh H2 value is diagnostic only and cannot steer watering."""
        _set(adj_db, "weather.balance.enabled", "1")
        _set(adj_db, "weather.balance.last_recalc_date", "2099-01-01")
        adj = WeatherAdjustment(adj_db)
        with patch.object(adj, "get_coefficient", return_value=77) as h1:
            assert adj.get_effective_coefficient() == 77
            h1.assert_called_once()

    def test_enabled_stale_falls_back_to_h1(self, adj_db):
        """Stale H2 also leaves the H1 path untouched."""
        _set(adj_db, "weather.balance.enabled", "1")
        _set(adj_db, "weather.balance.last_recalc_date", "2001-01-01")
        adj = WeatherAdjustment(adj_db)
        with patch.object(adj, "get_coefficient", return_value=77) as h1:
            assert adj.get_effective_coefficient() == 77
            h1.assert_called_once()

    def test_enabled_no_recalc_date_falls_back(self, adj_db):
        """An empty H2 cache leaves H1 untouched."""
        _set(adj_db, "weather.balance.enabled", "1")
        adj = WeatherAdjustment(adj_db)
        with patch.object(adj, "get_coefficient", return_value=88) as h1:
            assert adj.get_effective_coefficient() == 88
            h1.assert_called_once()


# ---------------------------------------------------------------------------
# H1 regression guard (req #9)
# ---------------------------------------------------------------------------


def _ok_response(payload):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.raise_for_status = MagicMock(return_value=None)
    resp.json = MagicMock(return_value=payload)
    return resp


class TestForecastRegressionGuard:
    def test_fetch_api_requests_one_past_day_for_rolling_rain(self):
        """H1 needs yesterday's hours; WeatherData selects daily rows by date."""
        captured = {}

        def _capture(url, params=None, timeout=None, headers=None):
            captured["params"] = params
            return _ok_response({"daily": {"time": ["2026-06-26"]}})

        with patch("requests.get", side_effect=_capture):
            wc.fetch_api(42.6531, 77.0822)
        assert captured["params"]["past_days"] == 1
        assert captured["params"]["forecast_days"] == 3

    def test_fetch_history_sends_past_days_separately(self):
        """The balance request carries past_days and only the daily block,
        on its own request — never via the cached forecast path."""
        captured = {}

        def _capture(url, params=None, timeout=None, headers=None):
            captured["params"] = params
            return _ok_response({"daily": {"time": []}})

        with patch("requests.get", side_effect=_capture):
            wc.fetch_history(42.6531, 77.0822, past_days=35)
        assert captured["params"]["past_days"] == 35
        # forecast keeps today so the caller can drop the partial current day
        assert captured["params"]["forecast_days"] == 1
        # history is daily-only (no hourly block that the H1 parser expects)
        assert "hourly" not in captured["params"]

    def test_fetch_history_does_not_touch_cache(self, tmp_path):
        """fetch_history returns raw JSON and must not write weather_cache."""
        db_path = str(tmp_path / "nocache.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE weather_cache (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "latitude REAL, longitude REAL, data TEXT, fetched_at REAL)"
        )
        conn.commit()
        conn.close()
        with patch("requests.get", side_effect=lambda *a, **k: _ok_response({"daily": {"time": []}})):
            wc.fetch_history(42.6531, 77.0822, past_days=35)
        conn = sqlite3.connect(db_path)
        n = conn.execute("SELECT COUNT(*) FROM weather_cache").fetchone()[0]
        conn.close()
        assert n == 0
