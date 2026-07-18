"""Regression coverage for the controller-local irrigation-history contract."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone


@contextmanager
def _system_timezone(name: str):
    """Temporarily set the process timezone for SQLite/Python localtime."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def _insert_local_run(app, zone_id: int, start: str, end: str) -> None:
    with sqlite3.connect(app.db.db_path) as conn:
        conn.execute(
            "INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, "
            "start_monotonic, status, source) VALUES (?, 1, ?, ?, 0.0, 'ok', 'manual')",
            (zone_id, start, end),
        )
        conn.commit()


def _create_zone(app, name: str):
    return app.db.create_zone({"name": name, "duration": 15, "group_id": 1, "topic": f"/{name}"})


def test_first_local_day_is_not_dropped_by_sql_window(app, client):
    """Space-separated production rows on the first day belong in the window."""
    with _system_timezone("UTC"):
        zone = _create_zone(app, "history-first-day")
        today = datetime.now().date()
        first_day = today - timedelta(days=6)
        _insert_local_run(
            app,
            zone["id"],
            f"{first_day.isoformat()} 05:00:00",
            f"{first_day.isoformat()} 05:15:00",
        )

        response = client.get(f"/api/zones/{zone['id']}/history?days=7")

        assert response.status_code == 200
        payload = response.get_json()
        assert len(payload["runs"]) == 1
        first_bucket = next(item for item in payload["daily"] if item["date"] == first_day.isoformat())
        assert first_bucket["runs"] == 1
        assert first_bucket["actual_minutes"] == 15


def test_api_adds_controller_offset_for_remote_browser_rendering(app, client):
    """Naive storage becomes RFC 3339 without changing its controller wall time."""
    with _system_timezone("Asia/Bishkek"):
        zone = _create_zone(app, "history-explicit-offset")
        today: date = datetime.now().date()
        _insert_local_run(
            app,
            zone["id"],
            f"{today.isoformat()} 06:00:00",
            f"{today.isoformat()} 06:30:00",
        )

        response = client.get(f"/api/zones/{zone['id']}/history?days=7")

        assert response.status_code == 200
        run = response.get_json()["runs"][0]
        assert run["start_utc"] == f"{today.isoformat()}T06:00:00+06:00"
        assert run["end_utc"] == f"{today.isoformat()}T06:30:00+06:00"

        # JavaScript Date applies the browser timezone to this same instant.
        remote_time = datetime.fromisoformat(run["start_utc"]).astimezone(timezone(timedelta(hours=3)))
        assert remote_time.strftime("%Y-%m-%d %H:%M") == f"{today.isoformat()} 03:00"
