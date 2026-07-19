"""Issue #35: integration tests for /api/zones/.../history endpoints."""

import csv
import io
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

os.environ["TESTING"] = "1"


@pytest.fixture(autouse=True)
def _isolate_history_timezone(monkeypatch):
    monkeypatch.setenv("WB_TZ", "UTC")
    monkeypatch.setenv("TZ", "UTC")


def _utc_iso_now_minus(minutes: int, now_utc: datetime | None = None) -> str:
    """ISO-8601 UTC string for ``now - minutes``."""
    t = (now_utc or datetime.now(UTC)) - timedelta(minutes=minutes)
    return t.isoformat().replace("+00:00", "Z")


def _create_run(
    app,
    zone_id: int,
    group_id: int,
    start_min_ago: int,
    end_min_ago: int,
    liters=None,
    status="ok",
    source=None,
    now_utc: datetime | None = None,
):
    """Insert a finished zone_run row directly."""
    now_utc = now_utc or datetime.now(UTC)
    start = _utc_iso_now_minus(start_min_ago, now_utc)
    end = _utc_iso_now_minus(end_min_ago, now_utc) if end_min_ago is not None else None
    with sqlite3.connect(app.db.db_path) as conn:
        cur = conn.execute(
            "INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, "
            "start_monotonic, total_liters, status, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                zone_id,
                group_id,
                start,
                end,
                0.0,
                liters,
                status,
                source,
                now_utc.isoformat(),
            ),
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
        now_utc = datetime.now(UTC)
        _create_run(
            app,
            seeded_zone["id"],
            1,
            45,
            30,
            liters=12.5,
            source="program",
            now_utc=now_utc,
        )
        resp = client.get(f"/api/zones/{seeded_zone['id']}/history?days=7")
        assert resp.status_code == 200
        data = resp.get_json()
        # Runs are bucketed by their local start date, which may be yesterday
        # when this test executes shortly after local midnight.
        run_date_iso = (now_utc - timedelta(minutes=45)).date().isoformat()
        run_day = next(d for d in data["daily"] if d["date"] == run_date_iso)
        assert run_day["actual_minutes"] >= 14
        assert run_day["runs"] == 1
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

    def test_unconfirmed_aborted_attempt_does_not_inflate_actual_summary(self, app, client, seeded_zone):
        now = datetime.now(UTC)
        with sqlite3.connect(app.db.db_path) as conn:
            conn.execute(
                "INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, "
                "start_monotonic, total_liters, status, source, confirmed, created_at) "
                "VALUES (?, 1, ?, ?, 0.0, 20.0, 'aborted', 'manual', 0, ?)",
                (
                    seeded_zone["id"],
                    _utc_iso_now_minus(30, now),
                    _utc_iso_now_minus(20, now),
                    now.isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, "
                "start_monotonic, total_liters, status, source, confirmed, created_at) "
                "VALUES (?, 1, ?, ?, 0.0, 5.0, 'aborted', 'manual', 1, ?)",
                (
                    seeded_zone["id"],
                    _utc_iso_now_minus(15, now),
                    _utc_iso_now_minus(10, now),
                    now.isoformat(),
                ),
            )
            conn.commit()

        data = client.get(f"/api/zones/{seeded_zone['id']}/history?days=7").get_json()

        assert len(data["runs"]) == 2
        assert data["summary"]["total_runs"] == 1
        assert data["summary"]["total_minutes"] == 5
        assert data["summary"]["total_liters"] == 5.0
        runs_by_liters = {run["liters"]: run for run in data["runs"]}
        assert runs_by_liters[20.0]["confirmed"] is False
        assert runs_by_liters[20.0]["counts_as_actual"] is False
        assert runs_by_liters[5.0]["confirmed"] is True
        assert runs_by_liters[5.0]["counts_as_actual"] is True

    def test_unconfirmed_open_attempt_stays_visible_but_not_actual(self, app, client, seeded_zone):
        now = datetime.now(UTC)
        with sqlite3.connect(app.db.db_path) as conn:
            conn.execute(
                "INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, "
                "start_monotonic, total_liters, status, source, confirmed, created_at) "
                "VALUES (?, 1, ?, NULL, 0.0, 12.0, NULL, 'manual', 0, ?)",
                (seeded_zone["id"], _utc_iso_now_minus(10, now), now.isoformat()),
            )
            conn.commit()

        data = client.get(f"/api/zones/{seeded_zone['id']}/history?days=7").get_json()

        assert len(data["runs"]) == 1
        assert data["runs"][0]["counts_as_actual"] is False
        assert data["summary"]["total_runs"] == 0
        assert data["summary"]["total_minutes"] == 0
        assert data["summary"]["total_liters"] is None

    def test_confirmed_open_run_suppresses_savings_until_it_closes(self, app, client, seeded_zone):
        fixed_now = datetime(2030, 5, 4, 12, 0, tzinfo=UTC)
        with sqlite3.connect(app.db.db_path) as conn:
            conn.execute(
                "INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, "
                "start_monotonic, total_liters, status, source, confirmed, created_at) "
                "VALUES (?, 1, ?, NULL, 100.0, 12.0, NULL, 'manual', 1, ?)",
                (
                    seeded_zone["id"],
                    _utc_iso_now_minus(10, fixed_now),
                    fixed_now.isoformat(),
                ),
            )
            conn.commit()
        app.db.create_program(
            {
                "name": "Completed morning slot",
                "time": "07:00",
                "days": [fixed_now.weekday()],
                "zones": [seeded_zone["id"]],
                "schedule_type": "weekdays",
                "enabled": True,
            }
        )

        with patch("routes.zones_history_api._controller_now", return_value=fixed_now):
            data = client.get(f"/api/zones/{seeded_zone['id']}/history?days=7").get_json()

        assert data["runs"][0]["counts_as_actual"] is True
        assert data["summary"]["total_runs"] == 1
        assert data["summary"]["savings_available"] is False
        assert data["summary"]["saved_minutes"] is None
        assert data["summary"]["savings_unavailable_reason"] == "actual_run_open"

    def test_has_plan_false_when_no_active_program(self, app, client, seeded_zone):
        data = client.get(f"/api/zones/{seeded_zone['id']}/history?days=7").get_json()
        assert data["summary"]["has_plan"] is False
        # plan_minutes per day should be None.
        assert all(d["plan_minutes"] is None for d in data["daily"])

    def test_has_plan_true_with_active_program(self, app, client, seeded_zone, monkeypatch):
        fixed_now = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
        monkeypatch.setenv("WB_TZ", "UTC")
        with patch("routes.zones_history_api._controller_now", return_value=fixed_now):
            app.db.create_program(
                {
                    "name": "P",
                    "time": "07:00",
                    "days": [0, 1, 2, 3, 4, 5, 6],
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

    def test_interval_plan_fails_closed_without_scheduler_metadata(self, app, client, seeded_zone, monkeypatch):
        fixed_now = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
        monkeypatch.setenv("WB_TZ", "UTC")
        app.db.create_program(
            {
                "name": "Interval unavailable",
                "time": "07:00",
                "days": [],
                "zones": [seeded_zone["id"]],
                "schedule_type": "interval",
                "interval_days": 3,
                "enabled": True,
            }
        )

        with patch("routes.zones_history_api._controller_now", return_value=fixed_now):
            data = client.get(f"/api/zones/{seeded_zone['id']}/history?days=7").get_json()

        assert data["summary"]["has_plan"] is True
        assert data["summary"]["plan_available"] is False
        assert data["summary"]["savings_available"] is False
        assert data["summary"]["plan_minutes"] is None
        assert data["summary"]["saved_minutes"] is None
        assert all(day["plan_minutes"] is None for day in data["daily"])

    def test_interval_plan_uses_scheduler_occurrence(self, app, client, seeded_zone, monkeypatch):
        fixed_now = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
        monkeypatch.setenv("WB_TZ", "UTC")
        program = app.db.create_program(
            {
                "name": "Interval authoritative",
                "time": "07:00",
                "days": [],
                "zones": [seeded_zone["id"]],
                "schedule_type": "interval",
                "interval_days": 3,
                "enabled": True,
            }
        )
        occurrences = {program["id"]: [datetime(2026, 5, 4, 7, 0, tzinfo=UTC)]}

        with (
            patch("routes.zones_history_api._controller_now", return_value=fixed_now),
            patch(
                "routes.zones_history_api._load_interval_occurrences",
                return_value=occurrences,
                create=True,
            ),
        ):
            data = client.get(f"/api/zones/{seeded_zone['id']}/history?days=7").get_json()

        assert data["summary"]["plan_available"] is True
        assert data["summary"]["plan_minutes"] == 15
        assert data["summary"]["saved_minutes"] == 15


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

    def test_group_filter_uses_group_stored_on_run(self, app, client, monkeypatch):
        fixed_now = datetime(2030, 5, 4, 12, 0, tzinfo=UTC)
        monkeypatch.setenv("WB_TZ", "UTC")
        zone = app.db.create_zone({"name": "Moved", "duration": 10, "group_id": 1, "topic": "/x/moved"})
        cohort_zone = app.db.create_zone({"name": "Still group 1", "duration": 10, "group_id": 1, "topic": "/x/stable"})
        target_group = app.db.create_group("Moved target")
        _create_run(app, zone["id"], 1, 60, 50, status="ok", now_utc=fixed_now)
        assert app.db.update_zone(zone["id"], {"group_id": target_group["id"]}) is not None
        app.db.create_program(
            {
                "name": "Current group plan",
                "time": "00:00",
                "days": [fixed_now.weekday()],
                "zones": [cohort_zone["id"]],
                "schedule_type": "weekdays",
                "enabled": True,
            }
        )

        with patch("routes.zones_history_api._controller_now", return_value=fixed_now):
            old_group = client.get("/api/zones/history?days=7&group_id=1").get_json()
            new_group = client.get(f"/api/zones/history?days=7&group_id={target_group['id']}").get_json()

        assert old_group["summary"]["total_runs"] == 1
        assert [run["zone_id"] for run in old_group["runs"]] == [zone["id"]]
        assert old_group["summary"]["cohort_matches_current"] is False
        assert old_group["summary"]["savings_available"] is False
        assert old_group["summary"]["saved_minutes"] is None
        assert new_group["summary"]["total_runs"] == 0
        assert new_group["runs"] == []

    def test_group_cohort_detects_selected_zone_run_from_another_group(self, app, client):
        fixed_now = datetime(2030, 5, 4, 12, 0, tzinfo=UTC)
        zone = app.db.create_zone({"name": "Moved in", "duration": 10, "group_id": 1, "topic": "/x/moved-in"})
        _create_run(app, zone["id"], 2, 60, 50, status="ok", now_utc=fixed_now)
        app.db.create_program(
            {
                "name": "Current group 1 plan",
                "time": "07:00",
                "days": [fixed_now.weekday()],
                "zones": [zone["id"]],
                "schedule_type": "weekdays",
                "enabled": True,
            }
        )

        with patch("routes.zones_history_api._controller_now", return_value=fixed_now):
            data = client.get("/api/zones/history?days=7&group_id=1").get_json()

        assert data["runs"] == []
        assert data["summary"]["cohort_matches_current"] is False
        assert data["summary"]["savings_available"] is False
        assert data["summary"]["saved_minutes"] is None
        assert data["summary"]["savings_unavailable_reason"] == "historical_zone_cohort_changed"

    def test_legacy_reused_id_runs_are_not_named_after_current_zone(self, app, client):
        """Seed a pre-durable-ID collision without exercising supported CRUD reuse."""
        current_group = app.db.create_group("Current replacement group")
        current = app.db.create_zone(
            {
                "name": "Current replacement",
                "duration": 10,
                "group_id": current_group["id"],
                "topic": "/x/current",
            }
        )
        now = datetime.now(UTC)
        old_created = (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(app.db.db_path) as conn:
            for start_min, end_min, run_created in (
                (120, 110, old_created),
                # SQLite CURRENT_TIMESTAMP has one-second precision. This row
                # pins the equal-created_at fallback to its earlier start.
                (60, 50, current["created_at"]),
            ):
                conn.execute(
                    "INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, "
                    "start_monotonic, total_liters, status, source, confirmed, created_at) "
                    "VALUES (?, 1, ?, ?, 0.0, 6.0, 'ok', 'manual', 0, ?)",
                    (
                        current["id"],
                        _utc_iso_now_minus(start_min, now),
                        _utc_iso_now_minus(end_min, now),
                        run_created,
                    ),
                )
            conn.commit()

        payload = client.get(f"/api/zones/history?days=7&zone_id={current['id']}").get_json()

        assert len(payload["runs"]) == 2
        assert all(run["zone_id"] == current["id"] for run in payload["runs"])
        assert all(run["zone_name"] == "Удалённая зона" for run in payload["runs"])
        assert all(run["zone_deleted"] is True for run in payload["runs"])
        assert payload["summary"]["total_runs"] == 0
        assert payload["summary"]["total_minutes"] == 0
        assert payload["summary"]["total_liters"] is None


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

    @pytest.mark.parametrize(
        "dangerous_name",
        [
            '=WEBSERVICE("https://example.invalid")',
            "+1+1",
            "-1+1",
            "@SUM(A1)",
            "\t=1+1",
            "\r=1+1",
            " \t=1+1",
        ],
    )
    def test_csv_neutralizes_spreadsheet_formula_prefixes(self, app, client, dangerous_name):
        zone = app.db.create_zone(
            {
                "name": dangerous_name,
                "duration": 15,
                "group_id": 1,
                "topic": "/x/csv-formula",
            }
        )
        with sqlite3.connect(app.db.db_path) as conn:
            conn.execute(
                "UPDATE zones SET created_at = datetime('now', '-2 hours') WHERE id = ?",
                (zone["id"],),
            )
            conn.commit()
        _create_run(app, zone["id"], 1, 60, 45, status="ok")

        body = client.get(f"/api/zones/{zone['id']}/history.csv?days=7").get_data(as_text=True)
        rows = list(csv.reader(io.StringIO(body.removeprefix("\ufeff"), newline="")))

        assert rows[1][4] == f"'{dangerous_name}"
