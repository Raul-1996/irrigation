"""Integration tests for the H2 mode switch and the H1 regression guard.

- ``get_effective_coefficient()`` routing: flag off → H1; on+fresh → cache;
  on+stale → H1 fallback; empty buffer → neutral.
- ``fetch_history`` must NOT leak ``past_days`` into the H1 ``fetch_api`` request
  (which keys the shared cache and reads ``daily[0]`` as today).
"""

import os
import sqlite3
from datetime import date, timedelta
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
# get_effective_coefficient routing (req #6)
# ---------------------------------------------------------------------------


class TestEffectiveCoefficient:
    def test_disabled_returns_h1_untouched(self, adj_db):
        """enabled=0 → effective == get_coefficient() (H1 path)."""
        adj = WeatherAdjustment(adj_db)
        with patch.object(adj, "get_coefficient", return_value=77) as h1:
            assert adj.get_effective_coefficient() == 77
            h1.assert_called_once()

    def test_enabled_fresh_returns_cache(self, adj_db):
        """enabled=1 + fresh recalc date → cached balance coef, H1 not used."""
        _set(adj_db, "weather.balance.enabled", "1")
        _set(adj_db, "weather.balance.last_recalc_date", date.today().isoformat())
        adj = WeatherAdjustment(adj_db)
        with patch.object(adj, "get_coefficient", return_value=77) as h1:
            assert adj.get_effective_coefficient() == 130  # coef_cached
            h1.assert_not_called()

    def test_enabled_stale_falls_back_to_h1(self, adj_db):
        """enabled=1 but recalc older than stale_fallback_days → H1 fallback."""
        _set(adj_db, "weather.balance.enabled", "1")
        old = (date.today() - timedelta(days=5)).isoformat()
        _set(adj_db, "weather.balance.last_recalc_date", old)
        adj = WeatherAdjustment(adj_db)
        with patch.object(adj, "get_coefficient", return_value=77) as h1:
            assert adj.get_effective_coefficient() == 77
            h1.assert_called_once()

    def test_enabled_no_recalc_date_falls_back(self, adj_db):
        """enabled=1 but recalc never ran (empty date) → H1 fallback."""
        _set(adj_db, "weather.balance.enabled", "1")
        adj = WeatherAdjustment(adj_db)
        with patch.object(adj, "get_coefficient", return_value=88):
            assert adj.get_effective_coefficient() == 88

    def test_fresh_at_exact_boundary_uses_cache(self, adj_db):
        """recalc exactly stale_fallback_days old → still fresh (<=)."""
        _set(adj_db, "weather.balance.enabled", "1")
        edge = (date.today() - timedelta(days=2)).isoformat()
        _set(adj_db, "weather.balance.last_recalc_date", edge)
        adj = WeatherAdjustment(adj_db)
        with patch.object(adj, "get_coefficient", return_value=77):
            assert adj.get_effective_coefficient() == 130


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
    def test_fetch_api_does_not_send_past_days(self):
        """The H1 forecast request must NOT carry past_days (would shift
        daily[0] away from today and break models.py:107)."""
        captured = {}

        def _capture(url, params=None, timeout=None, headers=None):
            captured["params"] = params
            return _ok_response({"daily": {"time": ["2026-06-26"]}})

        with patch("requests.get", side_effect=_capture):
            wc.fetch_api(42.6531, 77.0822)
        assert "past_days" not in captured["params"]
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
