"""Regression coverage for the controller-local irrigation-history contract."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import SQLAlchemyError


@pytest.fixture(autouse=True)
def _isolate_history_timezone():
    previous_wb_tz = os.environ.get("WB_TZ")
    previous_process_tz = os.environ.get("TZ")
    os.environ["WB_TZ"] = "UTC"
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if previous_wb_tz is None:
            os.environ.pop("WB_TZ", None)
        else:
            os.environ["WB_TZ"] = previous_wb_tz
        if previous_process_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_process_tz
        time.tzset()


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
    created_at = datetime.now(UTC).isoformat()
    with sqlite3.connect(app.db.db_path) as conn:
        conn.execute(
            "INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, "
            "start_monotonic, status, source, created_at) "
            "VALUES (?, 1, ?, ?, 0.0, 'ok', 'manual', ?)",
            (zone_id, start, end, created_at),
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


def test_api_adds_controller_offset_for_remote_browser_rendering(app, client, monkeypatch):
    """Naive storage becomes RFC 3339 without changing its controller wall time."""
    monkeypatch.setenv("WB_TZ", "Asia/Bishkek")
    monkeypatch.setenv("TZ", "Asia/Bishkek")
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


def test_buckets_list_and_csv_share_explicit_controller_timezone(app, client, monkeypatch):
    """WB_TZ, not the test runner's process timezone, owns history dates."""
    controller_tz = ZoneInfo("Pacific/Kiritimati")
    controller_now = datetime(2026, 1, 2, 0, 30, tzinfo=controller_tz)
    monkeypatch.setenv("WB_TZ", "Pacific/Kiritimati")

    with (
        _system_timezone("UTC"),
        patch(
            "routes.zones_history_api._controller_now",
            return_value=controller_now,
        ),
    ):
        zone = _create_zone(app, "history-one-timezone")
        _insert_local_run(
            app,
            zone["id"],
            "2026-01-02 00:05:00",
            "2026-01-02 00:20:00",
        )

        payload = client.get(f"/api/zones/{zone['id']}/history?days=7").get_json()
        csv_body = client.get(f"/api/zones/{zone['id']}/history.csv?days=7").get_data(as_text=True)

    assert payload["period"]["to"] == "2026-01-02"
    bucket = next(item for item in payload["daily"] if item["date"] == "2026-01-02")
    assert bucket["runs"] == 1
    assert bucket["actual_minutes"] == 15
    assert payload["runs"][0]["start_utc"] == "2026-01-02T00:05:00+14:00"
    assert "2026-01-02,00:05:00,00:20:00" in csv_body


def test_summary_does_not_claim_savings_for_future_slot_today(app, client, monkeypatch):
    controller_tz = ZoneInfo("UTC")
    controller_now = datetime(2026, 5, 4, 12, 0, tzinfo=controller_tz)
    monkeypatch.setenv("WB_TZ", "UTC")

    with patch("routes.zones_history_api._controller_now", return_value=controller_now):
        zone = _create_zone(app, "history-future-plan")
        app.db.create_program(
            {
                "name": "Morning and evening",
                "time": "07:00",
                "extra_times": ["19:00"],
                "days": [0],
                "zones": [zone["id"]],
                "schedule_type": "weekdays",
                "enabled": True,
            }
        )

        payload = client.get(f"/api/zones/{zone['id']}/history?days=7").get_json()

    today = next(item for item in payload["daily"] if item["date"] == "2026-05-04")
    assert today["plan_minutes"] == 15
    assert payload["summary"]["plan_minutes"] == 15
    assert payload["summary"]["saved_minutes"] == 15


def test_interval_occurrences_use_live_scheduler_slot_contract(monkeypatch):
    from routes.zones_history_api import _load_interval_occurrences

    controller_tz = ZoneInfo("UTC")
    range_start = datetime(2026, 5, 1, tzinfo=controller_tz)
    range_end = datetime(2026, 5, 8, tzinfo=controller_tz)
    morning = datetime(2026, 5, 4, 7, 0, tzinfo=controller_tz)
    evening = datetime(2026, 5, 4, 19, 0, tzinfo=controller_tz)

    class FakeScheduler:
        def get_program_interval_anchors(self, program_id):
            assert program_id == 17
            return {"main": morning, "extra:0": evening}

        def get_program_occurrences(self, program_id, start, end, *, limit):
            assert (program_id, start, end, limit) == (17, range_start, range_end, 512)
            return {"main": [morning], "extra:0": [evening]}

    monkeypatch.setattr("irrigation_scheduler.get_scheduler", lambda: FakeScheduler())
    programs = [
        {
            "id": 17,
            "enabled": True,
            "schedule_type": "interval",
            "time": "07:00",
            "extra_times": ["19:00"],
        }
    ]

    result = _load_interval_occurrences(programs, range_start, range_end, controller_tz)

    assert result == {17: [morning, evening]}


def test_interval_occurrences_preserve_both_berlin_repeated_hour_instants(monkeypatch):
    from routes.zones_history_api import _load_interval_occurrences

    controller_tz = ZoneInfo("Europe/Berlin")
    range_start = datetime(2026, 10, 25, 0, 0, tzinfo=controller_tz)
    range_end = datetime(2026, 10, 26, 0, 0, tzinfo=controller_tz)
    summer_fold = datetime(2026, 10, 25, 2, 30, tzinfo=controller_tz, fold=0)
    winter_fold = datetime(2026, 10, 25, 2, 30, tzinfo=controller_tz, fold=1)

    class FakeScheduler:
        def get_program_interval_anchors(self, _program_id):
            return {"main": summer_fold}

        def get_program_occurrences(self, _program_id, _start, _end, *, limit):
            assert limit == 512
            return {"main": [winter_fold, summer_fold]}

    monkeypatch.setattr("irrigation_scheduler.get_scheduler", lambda: FakeScheduler())
    programs = [
        {
            "id": 19,
            "enabled": True,
            "schedule_type": "interval",
            "time": "02:30",
            "extra_times": [],
        }
    ]

    result = _load_interval_occurrences(programs, range_start, range_end, controller_tz)

    assert [value.astimezone(UTC) for value in result[19]] == [
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
    ]


def test_interval_occurrences_fail_closed_without_a_valid_slot(monkeypatch):
    from routes.zones_history_api import _load_interval_occurrences

    controller_tz = ZoneInfo("UTC")
    range_start = datetime(2026, 5, 1, tzinfo=controller_tz)
    range_end = datetime(2026, 5, 8, tzinfo=controller_tz)

    class FakeScheduler:
        def get_program_interval_anchors(self, _program_id):
            return {}

        def get_program_occurrences(self, _program_id, _start, _end, *, limit):
            assert limit == 512
            return {}

    monkeypatch.setattr("irrigation_scheduler.get_scheduler", lambda: FakeScheduler())
    programs = [
        {
            "id": 18,
            "enabled": True,
            "schedule_type": "interval",
            "time": None,
            "extra_times": [],
        }
    ]

    result = _load_interval_occurrences(programs, range_start, range_end, controller_tz)

    assert result == {}


@pytest.mark.parametrize(
    ("failure_stage", "backend_error"),
    [
        ("anchors", sqlite3.OperationalError("database is locked")),
        ("occurrences", SQLAlchemyError("job store unavailable")),
    ],
)
def test_interval_metadata_backend_errors_fail_closed(
    app,
    client,
    monkeypatch,
    failure_stage,
    backend_error,
):
    controller_now = datetime(2030, 5, 4, 12, 0, tzinfo=ZoneInfo("UTC"))
    zone = _create_zone(app, f"history-interval-{failure_stage}")
    app.db.create_program(
        {
            "name": f"Interval {failure_stage} failure",
            "time": "07:00",
            "days": [],
            "zones": [zone["id"]],
            "schedule_type": "interval",
            "interval_days": 3,
            "enabled": True,
        }
    )

    class FailingScheduler:
        def get_program_interval_anchors(self, _program_id):
            if failure_stage == "anchors":
                raise backend_error
            return {"main": controller_now}

        def get_program_occurrences(self, _program_id, _start, _end, *, limit):
            assert limit == 512
            if failure_stage == "occurrences":
                raise backend_error
            return {"main": []}

    monkeypatch.setattr("irrigation_scheduler.get_scheduler", lambda: FailingScheduler())

    with patch("routes.zones_history_api._controller_now", return_value=controller_now):
        response = client.get(f"/api/zones/{zone['id']}/history?days=7")

    assert response.status_code == 200
    summary = response.get_json()["summary"]
    assert summary["plan_available"] is False
    assert summary["saved_minutes"] is None


def test_dst_fold_runs_sort_and_measure_on_utc_timeline(app, client, monkeypatch):
    controller_tz = ZoneInfo("America/New_York")
    controller_now = datetime(2026, 11, 1, 12, 0, tzinfo=controller_tz)
    monkeypatch.setenv("WB_TZ", "America/New_York")

    with patch("routes.zones_history_api._controller_now", return_value=controller_now):
        zone = _create_zone(app, "history-dst-fold")
        _insert_local_run(
            app,
            zone["id"],
            "2026-11-01T01:45:00-04:00",
            "2026-11-01T01:55:00-04:00",
        )
        _insert_local_run(
            app,
            zone["id"],
            "2026-11-01T01:15:00-05:00",
            "2026-11-01T01:25:00-05:00",
        )

        payload = client.get(f"/api/zones/{zone['id']}/history?days=7").get_json()

    assert [run["start_utc"] for run in payload["runs"]] == [
        "2026-11-01T01:15:00-05:00",
        "2026-11-01T01:45:00-04:00",
    ]
    assert payload["summary"]["total_minutes"] == 20


def test_berlin_repeated_hour_runs_sort_by_utc_instant(app, client, monkeypatch):
    controller_tz = ZoneInfo("Europe/Berlin")
    controller_now = datetime(2026, 10, 25, 12, 0, tzinfo=controller_tz)
    monkeypatch.setenv("WB_TZ", "Europe/Berlin")

    with patch("routes.zones_history_api._controller_now", return_value=controller_now):
        zone = _create_zone(app, "history-berlin-fold-sort")
        _insert_local_run(
            app,
            zone["id"],
            "2026-10-25T02:45:00+02:00",
            "2026-10-25T02:55:00+02:00",
        )
        _insert_local_run(
            app,
            zone["id"],
            "2026-10-25T02:15:00+01:00",
            "2026-10-25T02:25:00+01:00",
        )

        payload = client.get(f"/api/zones/{zone['id']}/history?days=7").get_json()

    assert [run["start_utc"] for run in payload["runs"]] == [
        "2026-10-25T02:15:00+01:00",
        "2026-10-25T02:45:00+02:00",
    ]


def test_naive_dst_fold_rows_sort_by_utc_creation_timeline(app, client, monkeypatch):
    controller_tz = ZoneInfo("America/New_York")
    controller_now = datetime(2026, 11, 1, 12, 0, tzinfo=controller_tz)
    monkeypatch.setenv("WB_TZ", "America/New_York")

    with patch("routes.zones_history_api._controller_now", return_value=controller_now):
        zone = _create_zone(app, "history-naive-dst-sort")
        with sqlite3.connect(app.db.db_path) as conn:
            first = conn.execute(
                "INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, "
                "start_monotonic, end_monotonic, status, source, created_at) "
                "VALUES (?, 1, '2026-11-01 01:45:00', '2026-11-01 01:55:00', "
                "100.0, 700.0, 'ok', 'manual', '2026-11-01 05:45:00')",
                (zone["id"],),
            ).lastrowid
            second = conn.execute(
                "INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, "
                "start_monotonic, end_monotonic, status, source, created_at) "
                "VALUES (?, 1, '2026-11-01 01:15:00', '2026-11-01 01:25:00', "
                "800.0, 1400.0, 'ok', 'manual', '2026-11-01 06:15:00')",
                (zone["id"],),
            ).lastrowid
            conn.commit()

        payload = client.get(f"/api/zones/{zone['id']}/history?days=7").get_json()

    assert [run["id"] for run in payload["runs"]] == [second, first]
    assert [run["start_utc"] for run in payload["runs"]] == [
        "2026-11-01T01:15:00-05:00",
        "2026-11-01T01:45:00-04:00",
    ]


def test_naive_dst_fold_resolution_flows_through_duration_and_aggregates(app, client, monkeypatch):
    controller_tz = ZoneInfo("America/New_York")
    controller_now = datetime(2026, 11, 1, 12, 0, tzinfo=controller_tz)
    monkeypatch.setenv("WB_TZ", "America/New_York")

    with (
        _system_timezone("Pacific/Kiritimati"),
        patch("routes.zones_history_api._controller_now", return_value=controller_now),
    ):
        zone = _create_zone(app, "history-naive-dst-duration")
        with sqlite3.connect(app.db.db_path) as conn:
            conn.execute(
                "INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, "
                "start_monotonic, end_monotonic, total_liters, status, source, created_at) "
                "VALUES (?, 1, '2026-11-01 01:55:00', '2026-11-01 01:05:00', "
                "0.0, NULL, 4.0, 'ok', 'manual', '2026-11-01 05:55:00')",
                (zone["id"],),
            )
            conn.commit()
        app.db.create_program(
            {
                "name": "DST morning plan",
                "time": "00:00",
                "days": [6],
                "zones": [zone["id"]],
                "schedule_type": "weekdays",
                "enabled": True,
            }
        )

        payload = client.get(f"/api/zones/{zone['id']}/history?days=7").get_json()

    run = payload["runs"][0]
    assert run["start_utc"] == "2026-11-01T01:55:00-04:00"
    assert run["end_utc"] == "2026-11-01T01:05:00-05:00"
    assert run["duration_min"] == 10
    assert payload["summary"]["actuals_complete"] is True
    assert payload["summary"]["total_runs"] == 1
    assert payload["summary"]["total_minutes"] == 10
    assert payload["summary"]["total_liters"] == 4.0
    assert payload["summary"]["saved_minutes"] == 5
    assert payload["summary"]["savings_available"] is True
    bucket = next(day for day in payload["daily"] if day["date"] == "2026-11-01")
    assert bucket["actual_minutes"] == 10
