"""SQLite-backed cache for Open-Meteo responses, plus location lookup.

Single responsibility: persist the most recent raw API payload keyed by
(latitude, longitude), return fresh values within ``_CACHE_TTL_SEC``, and
expose a dedicated ``read_stale`` helper used by the degraded-mode path
when the upstream API is unavailable.

Also hosts ``get_location()`` — reading ``weather.latitude`` / ``weather.longitude``
from the ``settings`` table — because location + cache are joined at every
read and splitting them forces a circular import.

NOTE(wave4, CQ-015): direct ``sqlite3.connect(self.db_path, timeout=5)`` calls
are kept here (unchanged from the monolithic module). Migrating to
``BaseRepository._connect()`` is tracked as follow-up work (see
``irrigation-audit/findings/code-quality.md`` CQ-015) — combining
decomposition with a repository migration in a single wave was judged too
risky by the Wave 4 scope owner.
"""
import json
import logging
import sqlite3
import time
from typing import Any, Dict, Optional

from services.weather.models import WeatherData, _CACHE_TTL_SEC

logger = logging.getLogger(__name__)


def get_location(db_path: str) -> Optional[Dict[str, float]]:
    """Read (latitude, longitude) from the ``settings`` table.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        ``{'latitude': float, 'longitude': float}`` if both are configured,
        otherwise ``None``.
    """
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT value FROM settings WHERE key = 'weather.latitude'")
            lat_row = cur.fetchone()
            cur = conn.execute("SELECT value FROM settings WHERE key = 'weather.longitude'")
            lon_row = cur.fetchone()
            if lat_row and lon_row and lat_row['value'] and lon_row['value']:
                return {
                    'latitude': float(lat_row['value']),
                    'longitude': float(lon_row['value']),
                }
    except (sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("Weather location read error: %s", e)
    return None


def read_fresh(db_path: str, lat: float, lon: float) -> Optional[WeatherData]:
    """Return cached weather data if still within the TTL window.

    Args:
        db_path: SQLite path.
        lat: Latitude to key on (rounded to 4 decimals).
        lon: Longitude to key on (rounded to 4 decimals).

    Returns:
        ``WeatherData`` if a cache row younger than ``_CACHE_TTL_SEC`` exists,
        else ``None``.
    """
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                'SELECT data, fetched_at FROM weather_cache '
                'WHERE latitude = ? AND longitude = ? '
                'ORDER BY fetched_at DESC LIMIT 1',
                (round(lat, 4), round(lon, 4)),
            )
            row = cur.fetchone()
            if row:
                fetched_at = float(row['fetched_at'])
                if time.time() - fetched_at < _CACHE_TTL_SEC:
                    data = json.loads(row['data'])
                    data['_fetched_at'] = fetched_at
                    return WeatherData(data)
    except (sqlite3.Error, json.JSONDecodeError, ValueError, TypeError) as e:
        logger.debug("Weather cache read error: %s", e)
    return None


def read_stale(db_path: str, lat: float, lon: float) -> Optional[WeatherData]:
    """Return the most recent cached entry regardless of age.

    Used by the degraded-mode fallback when the API is unreachable — stale
    forecast data is strictly better than no data for the irrigation decision
    engine (the coefficient just won't track sub-hour changes).

    Args:
        db_path: SQLite path.
        lat: Latitude to key on.
        lon: Longitude to key on.

    Returns:
        ``WeatherData`` if any cache row exists for these coordinates, else
        ``None``.
    """
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                'SELECT data, fetched_at FROM weather_cache '
                'WHERE latitude = ? AND longitude = ? '
                'ORDER BY fetched_at DESC LIMIT 1',
                (round(lat, 4), round(lon, 4)),
            )
            row = cur.fetchone()
            if row:
                data = json.loads(row['data'])
                data['_fetched_at'] = float(row['fetched_at'])
                logger.info("Weather: using stale cache (API unavailable)")
                return WeatherData(data)
    except (sqlite3.Error, json.JSONDecodeError) as e:
        logger.debug("Weather stale cache read error: %s", e)
    return None


def save(db_path: str, lat: float, lon: float, data: Dict[str, Any]) -> None:
    """Upsert the raw API payload into ``weather_cache`` and prune old rows.

    Old rows are defined as those with ``fetched_at`` older than
    ``4 * _CACHE_TTL_SEC`` (i.e. two hours at the current TTL).

    Args:
        db_path: SQLite path.
        lat: Latitude (rounded to 4 decimals for the key).
        lon: Longitude (rounded to 4 decimals for the key).
        data: Raw JSON payload from the Open-Meteo API.
    """
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            now = time.time()
            conn.execute(
                'INSERT OR REPLACE INTO weather_cache '
                '(latitude, longitude, data, fetched_at) VALUES (?, ?, ?, ?)',
                (round(lat, 4), round(lon, 4), json.dumps(data), now),
            )
            conn.execute(
                'DELETE FROM weather_cache WHERE fetched_at < ?',
                (now - _CACHE_TTL_SEC * 4,),
            )
            conn.commit()
    except (sqlite3.Error, json.JSONDecodeError) as e:
        logger.debug("Weather cache write error: %s", e)
