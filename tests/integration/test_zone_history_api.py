"""Issue #35: integration tests for /api/zones/.../history endpoints."""

import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

os.environ["TESTING"] = "1"


def _utc_iso_now_minus(minutes: int) -> str:
    """ISO-8601 UTC string for ``now - minutes``."""
    t = datetime.now(UTC) - timedelta(minutes=minutes)
    return t.isoformat().replace("+00:00", "Z")


def _today_local_iso() -> str:
    return datetime.now().astimezone().date().isoformat()


def _create_run(
    app, zone_id: int, group_id: int, start_min_ago: int, end_min_ago: int, liters=None, status="ok", source=None
):
    """Insert a finished zone_run row directly."""
    start = _utc_iso_now_minus(start_min_ago)
    end = _utc_iso_now_minus(end_min_ago) if end_min_ago is not None else None
    with sqlite3.connect(app.db.db_path) as conn:
        cur = conn.execute(
            "INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, "
            "start_monotonic, total_liters, status, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (zone_id, group_id, start, end, 0.0, liters, status, source),
        )
        conn.commit()
        return cur.lastrowid


@pytest.fixture
def seeded_zone(app):
    """One zone in group 1 with duration 15 min."""
    return app.db.create_zone(
        {
            "name": "Зона тест",
            "duration": 15,
            "group_id": 1,
            "topic": "/d/t/K1",
        }
    )


class TestPerZoneJson:
    def test_basic_shape(self, client, app, seeded_zone):
        resp = client.get(f"/api/zones/{seeded_zone['id']}/history?days=7")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        # zone block
        assert data["zone"]["id"] == seeded_zone["id"]
        assert data["zone"]["duration"] == 15
        # period block
        assert data["period"]["days"] == 7
        # daily — 7 entries
        assert len(data["daily"]) == 7
        # summary — keys present
        assert "plan_minutes" in data["summary"]
        assert "saved_minutes" in data["summary"]
        assert "has_plan" in data["summary"]
        assert "total_runs" in data["summary"]
        # runs — empty for this fresh DB
        assert data["runs"] == []

    def test_404_for_missing_zone(self, client, app):
        resp = client.get("/api/zones/99999/history?days=7")
        assert resp.status_code == 404

    def test_400_for_bad_days(self, client, app, seeded_zone):
        for bad in ["0", "1", "14", "100", "abc"]:
            resp = client.get(f"/api/zones/{seeded_zone['id']}/history?days={bad}")
            assert resp.status_code == 400, f"days={bad}"

    def test_30_days_accepted(self, client, app, seeded_zone):
        resp = client.get(f"/api/zones/{seeded_zone['id']}/history?days=30")
        assert resp.status_code == 200
        assert resp.get_json()["period"]["days"] == 30
        assert len(resp.get_json()["daily"]) == 30

    def test_completed_run_in_range_appears_in_daily_and_runs(self, app, client, seeded_zone):
        # Run finished 30 minutes ago, lasted 15 minutes (started 45m ago).
        _create_run(app, seeded_zone["id"], 1, 45, 30, liters=12.5, source="program")
        resp = client.get(f"/api/zones/{seeded_zone['id']}/history?days=7")
        assert resp.status_code == 200
        data = resp.get_json()
        # Today entry has actual_minutes >= 14 (15 min run, ±rounding tolerance).
        today_iso = _today_local_iso()
        today = next(d for d in data["daily"] if d["date"] == today_iso)
        assert today["actual_minutes"] >= 14
        assert today["runs"] == 1
        # One run in list.
        assert len(data["runs"]) == 1
        assert data["runs"][0]["source"] == "program"
        assert data["runs"][0]["duration_min"] >= 14

    def test_total_liters_aggregated_when_present(self, app, client, seeded_zone):
        _create_run(app, seeded_zone["id"], 1, 60, 45, liters=10.0)
        _create_run(app, seeded_zone["id"], 1, 120, 105, liters=20.0)
        data = client.get(f"/api/zones/{seeded_zone['id']}/history?days=7").get_json()
        assert data["summary"]["total_liters"] == 30.0
        assert data["summary"]["has_liters"] is True
        assert data["summary"]["liters_partial"] is False

    def test_liters_partial_flag_when_some_runs_lack_data(self, app, client, seeded_zone):
        _create_run(app, seeded_zone["id"], 1, 60, 45, liters=10.0)
        _create_run(app, seeded_zone["id"], 1, 120, 105, liters=None)
        data = client.get(f"/api/zones/{seeded_zone['id']}/history?days=7").get_json()
        assert data["summary"]["has_liters"] is True
        assert data["summary"]["liters_partial"] is True

    def test_has_plan_false_when_no_active_program(self, app, client, seeded_zone):
        data = client.get(f"/api/zones/{seeded_zone['id']}/history?days=7").get_json()
        assert data["summary"]["has_plan"] is False
        # plan_minutes per day should be None.
        assert all(d["plan_minutes"] is None for d in data["daily"])

    def test_has_plan_true_with_active_program(self, app, client, seeded_zone):
        # Program that runs today (every weekday).
        today_local = datetime.now().astimezone().date()
        app.db.create_program(
            {
                "name": "P",
                "time": "07:00",
                "days": [0, 1, 2, 3, 4, 5, 6],  # all weekdays
                "zones": [seeded_zone["id"]],
                "schedule_type": "weekdays",
                "enabled": True,
            }
        )
        data = client.get(f"/api/zones/{seeded_zone['id']}/history?days=7").get_json()
        assert data["summary"]["has_plan"] is True
        # plan_minutes per day = 15 (zone duration) * 1 firing/day = 15
        assert all(d["plan_minutes"] == 15 for d in data["daily"])
        assert data["summary"]["plan_minutes"] == 15 * 7


class TestGlobalJson:
    def test_default_returns_all_non_999(self, app, client):
        z1 = app.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1, "topic": "/x/1"})
        z2 = app.db.create_zone({"name": "Z2", "duration": 20, "group_id": 1, "topic": "/x/2"})
        resp = client.get("/api/zones/history?days=7")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["zone_count"] >= 2

    def test_filter_by_zone_id(self, app, client):
        z1 = app.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1, "topic": "/x/1"})
        z2 = app.db.create_zone({"name": "Z2", "duration": 20, "group_id": 1, "topic": "/x/2"})
        _create_run(app, z2["id"], 1, 60, 45, liters=5.0)
        data = client.get(f"/api/zones/history?days=7&zone_id={z1['id']}").get_json()
        assert data["zone_count"] == 1
        assert data["summary"]["total_runs"] == 0  # z1 has no runs
        data2 = client.get(f"/api/zones/history?days=7&zone_id={z2['id']}").get_json()
        assert data2["summary"]["total_runs"] == 1

    def test_400_for_bad_days(self, client):
        assert client.get("/api/zones/history?days=15").status_code == 400


class TestCsv:
    def test_csv_content_type_and_bom(self, app, client, seeded_zone):
        _create_run(app, seeded_zone["id"], 1, 60, 45, liters=12.5, source="manual", status="ok")
        resp = client.get(f"/api/zones/{seeded_zone['id']}/history.csv?days=7")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("Content-Type", "")
        body = resp.get_data(as_text=True)
        assert body.startswith("\ufeff")
        # Header row
        assert "date,start_time,end_time" in body
        # Data row contains our 'manual' source
        assert "manual" in body

    def test_csv_filename_has_zone_id_and_dates(self, client, app, seeded_zone):
        resp = client.get(f"/api/zones/{seeded_zone['id']}/history.csv?days=7")
        cd = resp.headers.get("Content-Disposition", "")
        assert f"zone-{seeded_zone['id']}" in cd
        assert ".csv" in cd
