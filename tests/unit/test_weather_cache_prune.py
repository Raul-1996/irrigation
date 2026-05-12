"""Tests for weather_cache prune window (Phase 3, issue #29).

The prune used to delete rows older than ``4 * _CACHE_TTL_SEC`` (~2h),
which silently dropped slightly stale entries that ``read_stale`` would
otherwise serve. New behaviour: prune at 24 hours.
"""

import json
import sqlite3
import time

import pytest

from services.weather import cache as wcache


@pytest.fixture
def cache_db(tmp_path):
    db_path = str(tmp_path / "weather_cache.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE weather_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                data TEXT NOT NULL,
                fetched_at REAL NOT NULL
            )
        """)
        conn.commit()
    return db_path


def _insert(db_path, lat, lon, fetched_at, payload=None):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO weather_cache(latitude, longitude, data, fetched_at) VALUES (?, ?, ?, ?)",
            (round(lat, 4), round(lon, 4), json.dumps(payload or {"v": 1}), fetched_at),
        )
        conn.commit()


def _count(db_path, where=""):
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(f"SELECT COUNT(*) FROM weather_cache {where}")
        return cur.fetchone()[0]


def test_prune_keeps_3h_old_entry(cache_db):
    """A row 3h old must survive prune (< 24h)."""
    now = time.time()
    other_lat, other_lon = 10.0, 10.0
    _insert(cache_db, other_lat, other_lon, now - 3 * 3600)
    # Trigger save (and its prune) at a different location so the 3h-old
    # row is not overwritten by INSERT OR REPLACE.
    wcache.save(cache_db, 55.7, 37.6, {"fresh": True})
    assert _count(cache_db, "WHERE latitude=10.0 AND longitude=10.0") == 1


def test_prune_removes_25h_old_entry(cache_db):
    """A row 25h old must be deleted by prune (> 24h)."""
    now = time.time()
    other_lat, other_lon = 10.0, 10.0
    _insert(cache_db, other_lat, other_lon, now - 25 * 3600)
    wcache.save(cache_db, 55.7, 37.6, {"fresh": True})
    assert _count(cache_db, "WHERE latitude=10.0 AND longitude=10.0") == 0
    # The freshly-saved row at 55.7/37.6 is kept.
    assert _count(cache_db, "WHERE latitude=55.7 AND longitude=37.6") == 1
