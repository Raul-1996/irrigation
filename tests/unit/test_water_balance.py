"""Tests for the H2 virtual water-balance engine (services/weather/balance.py).

Covers the four adversarial-review blockers and the spec's numeric scenarios:
night case (no underwatering), single coef per day, accumulation under heat,
rain as a soft credit (no hard reset), clamp bounds, the mode flag fallback,
job idempotency, interception, the H1 regression guard, and norm/zero-div.
"""

import os
import sqlite3
from datetime import date, timedelta
from unittest.mock import patch

import pytest

os.environ["TESTING"] = "1"

from services.weather import balance as wb

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def bal_db(tmp_path):
    """Temp DB with settings + weather_balance_log, balance defaults seeded."""
    db_path = str(tmp_path / "test_balance.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("""CREATE TABLE IF NOT EXISTS weather_balance_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, et0_fact REAL, et0_norm REAL, precip_fact REAL, precip_eff REAL,
        deficit_day REAL, deficit_window REAL, coefficient INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    defaults = {
        "weather.latitude": "42.6531",
        "weather.longitude": "77.0822",
        "weather.balance.enabled": "0",
        "weather.balance.window_days": "3",
        "weather.balance.norm_window_days": "30",
        "weather.balance.coef_min": "50",
        "weather.balance.coef_max": "150",
        "weather.balance.kc": "1.0",
        "weather.balance.intercept_mm": "4.0",
        "weather.balance.stale_fallback_days": "2",
        "weather.balance.coef_cached": "100",
    }
    for k, v in defaults.items():
        conn.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (k, v))
    conn.commit()
    conn.close()
    return db_path


def _history_payload(days):
    """Build an Open-Meteo-style history payload from a list of
    (date_str, et0, precip) tuples."""
    return {
        "daily": {
            "time": [d[0] for d in days],
            "et0_fao_evapotranspiration": [d[1] for d in days],
            "precipitation_sum": [d[2] for d in days],
        }
    }


def _days_back(n, et0, precip, end_offset=1):
    """N consecutive completed days ending `end_offset` days before today.

    Returns tuples (YYYY-MM-DD, et0, precip), chronologically ascending.
    `et0`/`precip` may be scalars (constant) or lists of length n.
    """
    today = date.today()
    out = []
    for i in range(n):
        # oldest first; newest day is `end_offset` days before today
        d = today - timedelta(days=end_offset + (n - 1 - i))
        e = et0[i] if isinstance(et0, list) else et0
        p = precip[i] if isinstance(precip, list) else precip
        out.append((d.isoformat(), e, p))
    return out


def _get_setting(db_path, key):
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _run_recalc(db_path, payload):
    with patch("services.weather.client.fetch_history", return_value=payload):
        return wb.recalc_balance(db_path)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestDeficitHelpers:
    def test_compute_deficit_basic(self):
        # need = 6.5*1; supply = max(0, 0-4) = 0
        assert wb.compute_deficit_day(6.5, 0.0, 1.0, 4.0) == pytest.approx(6.5)

    def test_interception_below_threshold_gives_zero_supply(self):
        """BLOCKER/req #8: P_fact < intercept → P_eff = 0 (no negative supply)."""
        # 3mm rain, intercept 4mm → supply 0 → deficit == need
        assert wb.compute_deficit_day(5.0, 3.0, 1.0, 4.0) == pytest.approx(5.0)

    def test_interception_above_threshold_credits_excess(self):
        # 10mm rain, intercept 4mm → supply 6 → deficit = 5 - 6 = -1
        assert wb.compute_deficit_day(5.0, 10.0, 1.0, 4.0) == pytest.approx(-1.0)

    def test_apply_rain_gate_is_noop_without_sensor(self):
        assert wb.apply_rain_gate(7.3, None) == 7.3


# ---------------------------------------------------------------------------
# recalc_balance — spec scenarios
# ---------------------------------------------------------------------------


class TestRecalcScenarios:
    def test_night_case_not_underwatered(self, bal_db):
        """Moderate days (ET0=4.5) → coef ~90, NOT ~50 like H1 at night."""
        days = _days_back(30, 4.5, 0.0)
        result = _run_recalc(bal_db, _history_payload(days))
        assert result is not None
        # norm bootstraps to 4.5; D_win = 13.5; Norm_win = 4.5*3 = 13.5 → 100
        # (steady state at the norm is 100; the point is it is NOT ~50).
        assert result["coefficient"] >= 90
        assert result["coefficient"] <= 100

    def test_heat_accumulation_pushes_up(self, bal_db):
        """30 moderate days then 3 hot days → coef rises toward ~130."""
        moderate = _days_back(33, 5.0, 0.0)
        # Overwrite the last 3 days with hot ET0=6.5
        days = [
            *moderate[:-3],
            (moderate[-3][0], 6.5, 0.0),
            (moderate[-2][0], 6.5, 0.0),
            (moderate[-1][0], 6.5, 0.0),
        ]
        result = _run_recalc(bal_db, _history_payload(days))
        assert result is not None
        # norm ~5.0 (EMA barely moved by 3 hot days), D_win=19.5, Norm_win=15 → ~130
        assert 125 <= result["coefficient"] <= 135

    def test_return_to_normal_after_heat(self, bal_db):
        """After heat, moderate days bring coef back toward ~100."""
        # Seed a norm of 5.0 and pretend heat already happened (yesterday's run).
        conn = sqlite3.connect(bal_db)
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('weather.balance.et0_norm_daily','5.0')")
        conn.commit()
        conn.close()
        days = _days_back(30, 5.0, 0.0)  # moderate again == norm
        result = _run_recalc(bal_db, _history_payload(days))
        assert result is not None
        assert 95 <= result["coefficient"] <= 105

    def test_rain_is_soft_credit_not_hard_reset(self, bal_db):
        """BLOCKER 2: a big rain day lowers the deficit but one false day does
        not wipe the whole window (other days remain)."""
        # Seed norm so the formula engages.
        conn = sqlite3.connect(bal_db)
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('weather.balance.et0_norm_daily','5.0')")
        conn.commit()
        conn.close()
        # window = 3 days: two hot dry (6.5, 0) + one big-rain day (6.5, 18mm)
        base = _days_back(30, 5.0, 0.0)
        days = [
            *base[:-3],
            (base[-3][0], 6.5, 0.0),
            (base[-2][0], 6.5, 0.0),
            (base[-1][0], 6.5, 18.0),  # P_eff = 14 → deficit = -7.5
        ]
        result = _run_recalc(bal_db, _history_payload(days))
        assert result is not None
        # D_win = 6.5 + 6.5 + (6.5-14) = 5.5 ; still > 0 (window not wiped),
        # Norm_win = 15 → coef ~37 → clamped to coef_min 50.
        assert result["deficit_window"] == pytest.approx(5.5, abs=0.01)
        assert result["coefficient"] == 50  # clamp_min, NOT 0/neutral reset

    def test_saturation_lower_clamp(self, bal_db):
        """Massive rain across the window → D_win clamped to 0, coef → coef_min."""
        conn = sqlite3.connect(bal_db)
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('weather.balance.et0_norm_daily','5.0')")
        conn.commit()
        conn.close()
        base = _days_back(30, 5.0, 0.0)
        days = base[:-3] + [(d[0], 5.0, 40.0) for d in base[-3:]]  # soaked
        result = _run_recalc(bal_db, _history_payload(days))
        assert result is not None
        assert result["deficit_window"] == 0.0
        assert result["coefficient"] == 50

    def test_clamp_extreme_et0(self, bal_db):
        """Extreme ET0 → coef bounded within [coef_min, coef_max]."""
        conn = sqlite3.connect(bal_db)
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('weather.balance.et0_norm_daily','5.0')")
        conn.commit()
        conn.close()
        base = _days_back(30, 5.0, 0.0)
        days = base[:-3] + [(d[0], 20.0, 0.0) for d in base[-3:]]  # absurd heat
        result = _run_recalc(bal_db, _history_payload(days))
        assert result is not None
        assert 50 <= result["coefficient"] <= 150
        assert result["coefficient"] == 150  # pinned at coef_max


# ---------------------------------------------------------------------------
# Blockers 3 & 4: norm readiness / cold start / zero-div
# ---------------------------------------------------------------------------


class TestNormAndColdStart:
    def test_short_history_gives_neutral_coef(self, bal_db):
        """BLOCKER 4: <7 days history → coef = 100 (neutral), not 50."""
        days = _days_back(3, 8.0, 0.0)  # only 3 completed days, very hot
        result = _run_recalc(bal_db, _history_payload(days))
        assert result is not None
        assert result["coefficient"] == 100
        assert result["norm_ready"] is False

    def test_norm_is_ema_separate_from_window(self, bal_db):
        """BLOCKER 3: with steady ET0, norm == that ET0 and coef == 100
        (deficit window normalised against the long-horizon daily norm)."""
        days = _days_back(30, 6.0, 0.0)
        result = _run_recalc(bal_db, _history_payload(days))
        assert result is not None
        assert result["et0_norm_daily"] == pytest.approx(6.0, abs=0.01)
        # D_win = 18, Norm_win = 6*3 = 18 → exactly 100 (does not stick low/high)
        assert result["coefficient"] == 100

    def test_zero_norm_guard_returns_neutral(self):
        """Norm_win <= 0 → coef = 100 (division guard, BLOCKER 3)."""
        assert wb._compute_coef(10.0, 0.0, 50, 150) == 100
        assert wb._compute_coef(10.0, -5.0, 50, 150) == 100

    def test_bootstrap_returns_none_for_short_history(self):
        rows = [{"date": "2026-06-01", "et0": 5.0, "precip": 0.0}]
        assert wb._bootstrap_norm(rows, 30) is None


# ---------------------------------------------------------------------------
# Idempotency + completed-day handling
# ---------------------------------------------------------------------------


class TestIdempotencyAndDays:
    def test_idempotent_same_day(self, bal_db):
        """Two recalcs in one day → second is a no-op, log not duplicated."""
        days = _days_back(30, 5.0, 0.0)
        payload = _history_payload(days)
        r1 = _run_recalc(bal_db, payload)
        assert r1 is not None
        r2 = _run_recalc(bal_db, payload)
        assert r2 is None  # idempotent skip
        conn = sqlite3.connect(bal_db)
        n = conn.execute("SELECT COUNT(*) FROM weather_balance_log").fetchone()[0]
        conn.close()
        assert n == 1

    def test_manual_recalc_same_day_no_double_fold(self, bal_db):
        """Manual recalc (clears last_recalc_date) must not re-fold the same
        completed day into the EMA nor duplicate the audit-log row (review #2)."""
        # Newest completed day hotter than the rest, so a second EMA fold would
        # visibly shift the norm if the per-completed-day guard were missing.
        days = _days_back(30, [5.0] * 29 + [10.0], 0.0)
        payload = _history_payload(days)
        r1 = _run_recalc(bal_db, payload)
        assert r1 is not None
        norm1 = _get_setting(bal_db, "weather.balance.et0_norm_daily")
        # Simulate the manual endpoint: clear the same-calendar-day marker.
        conn = sqlite3.connect(bal_db)
        conn.execute("DELETE FROM settings WHERE key = 'weather.balance.last_recalc_date'")
        conn.commit()
        conn.close()
        r2 = _run_recalc(bal_db, payload)
        assert r2 is not None  # it ran (bypassed the same-day skip)
        norm2 = _get_setting(bal_db, "weather.balance.et0_norm_daily")
        # EMA norm unchanged: the same completed day is not folded twice.
        assert float(norm2) == pytest.approx(float(norm1), abs=1e-6)
        # Audit log not duplicated for the same completed day.
        conn = sqlite3.connect(bal_db)
        n = conn.execute("SELECT COUNT(*) FROM weather_balance_log").fetchone()[0]
        conn.close()
        assert n == 1

    def test_current_partial_day_is_ignored(self, bal_db):
        """A row dated today (partial) must not be used as a completed day."""
        days = _days_back(30, 5.0, 0.0)
        # Append a today row with an absurd value — must be dropped.
        days = [*days, (date.today().isoformat(), 99.0, 0.0)]
        result = _run_recalc(bal_db, _history_payload(days))
        assert result is not None
        # If today leaked in, norm/coef would spike; it must not.
        assert result["et0_norm_daily"] == pytest.approx(5.0, abs=0.01)
        assert result["coefficient"] == 100

    def test_coef_cached_written(self, bal_db):
        days = _days_back(30, 5.0, 0.0)
        _run_recalc(bal_db, _history_payload(days))
        assert _get_setting(bal_db, "weather.balance.coef_cached") is not None
        assert wb.read_cached_coef(bal_db) == 100

    def test_two_waterings_same_day_same_coef(self, bal_db):
        """Coef is read from the cache (computed once nightly) → identical for
        any number of intra-day reads."""
        days = _days_back(33, 5.0, 0.0)
        days = days[:-3] + [(d[0], 6.5, 0.0) for d in days[-3:]]
        _run_recalc(bal_db, _history_payload(days))
        c1 = wb.read_cached_coef(bal_db)
        c2 = wb.read_cached_coef(bal_db)
        assert c1 == c2
        assert 125 <= c1 <= 135

    def test_recalc_no_location_skips(self, bal_db):
        conn = sqlite3.connect(bal_db)
        conn.execute("DELETE FROM settings WHERE key IN ('weather.latitude','weather.longitude')")
        conn.commit()
        conn.close()
        result = _run_recalc(bal_db, _history_payload(_days_back(30, 5.0, 0.0)))
        assert result is None

    def test_recalc_empty_history_keeps_previous(self, bal_db):
        with patch("services.weather.client.fetch_history", return_value=None):
            result = wb.recalc_balance(bal_db)
        assert result is None
        # previous coef untouched
        assert wb.read_cached_coef(bal_db) == 100
