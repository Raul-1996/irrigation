"""Tests for issue #35 migration: zone_runs.source + backfill + index."""

import os
import sqlite3
from datetime import UTC, datetime, timedelta

os.environ["TESTING"] = "1"


def _local_iso_at(local_hour: int, local_minute: int, days_back: int = 0) -> str:
    """Build an ISO-8601 UTC timestamp whose LOCAL time-of-day is HH:MM.

    Use the OS-local timezone so the backfill (which converts to local time)
    sees the expected hour/minute. ``days_back`` lets the caller pick a date
    matching a specific weekday or even/odd day.
    """
    # Anchor at "today local" minus N days at the requested local clock time.
    now_local = datetime.now().astimezone()
    target_local = now_local.replace(
        hour=local_hour,
        minute=local_minute,
        second=0,
        microsecond=0,
    ) - timedelta(days=days_back)
    return target_local.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _make_db(test_db_path):
    from database import IrrigationDB

    return IrrigationDB(db_path=test_db_path)


class TestZoneRunsSourceMigration:
    """Migration adds `source` column, composite index, and backfills history."""

    def test_source_column_present(self, test_db_path):
        _make_db(test_db_path)
        conn = sqlite3.connect(test_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(zone_runs)").fetchall()}
        conn.close()
        assert "source" in cols

    def test_index_present(self, test_db_path):
        _make_db(test_db_path)
        conn = sqlite3.connect(test_db_path)
        idx_names = {
            row[1]
            for row in conn.execute(
                "SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='zone_runs'"
            ).fetchall()
        }
        conn.close()
        assert "idx_zone_runs_zone_start" in idx_names

    def test_migration_registered_in_table(self, test_db_path):
        _make_db(test_db_path)
        conn = sqlite3.connect(test_db_path)
        names = {row[0] for row in conn.execute("SELECT name FROM migrations").fetchall()}
        conn.close()
        assert "zone_runs_add_source" in names
        assert "zone_runs_backfill_source" in names

    def test_backfill_no_rows_is_noop(self, test_db_path):
        """Empty zone_runs ⇒ migration succeeds with no updates."""
        db = _make_db(test_db_path)
        # Sanity: no rows, no crash.
        conn = sqlite3.connect(test_db_path)
        cnt = conn.execute("SELECT COUNT(*) FROM zone_runs").fetchone()[0]
        conn.close()
        assert cnt == 0
        # Re-init is idempotent.
        from database import IrrigationDB

        IrrigationDB(db_path=test_db_path)  # no exception
        _ = db.get_zones()  # still functional

    def test_backfill_marks_matching_run_as_program(self, test_db_path, monkeypatch):
        """A pre-migration NULL row that lines up with a program's schedule
        becomes ``source='program'`` after backfill.

        We simulate "pre-existing" data by inserting directly into the DB,
        then triggering re-init via a fresh ``IrrigationDB`` — but the
        migration is already applied. So instead: insert NULL, then call the
        migration helper explicitly via ``rollback_migration`` + re-init.
        """
        db = _make_db(test_db_path)
        zone = db.create_zone(
            {
                "name": "Z1",
                "duration": 15,
                "group_id": 1,
                "topic": "/devices/t/K1",
            }
        )
        # Pick a past Monday so days=[0] (no 1..7 → 0..6 normalisation ambiguity).
        now_local = datetime.now().astimezone()
        days_back = (now_local.weekday() - 0) % 7
        if days_back == 0:
            days_back = 7  # avoid "today" — use last week's same weekday
        run_dt = (now_local - timedelta(days=days_back)).replace(
            hour=7,
            minute=0,
            second=30,
            microsecond=0,
        )
        assert run_dt.weekday() == 0  # sanity
        db.create_program(
            {
                "name": "P1",
                "time": "07:00",
                "days": [0],  # Monday
                "zones": [zone["id"]],
                "schedule_type": "weekdays",
                "enabled": True,
            }
        )
        # Insert a NULL-source row whose start_utc matches the program.
        start_utc = run_dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
        # Use direct sqlite insert because create_zone_run will now write source.
        conn = sqlite3.connect(test_db_path)
        conn.execute(
            "INSERT INTO zone_runs(zone_id, group_id, start_utc, start_monotonic, source) VALUES (?, ?, ?, ?, NULL)",
            (zone["id"], 1, start_utc, 0.0),
        )
        # Clear the backfill flag so re-init runs it again.
        conn.execute("DELETE FROM migrations WHERE name = 'zone_runs_backfill_source'")
        conn.commit()
        conn.close()

        # Re-run migrations.
        from database import IrrigationDB

        IrrigationDB(db_path=test_db_path)

        conn = sqlite3.connect(test_db_path)
        row = conn.execute("SELECT source FROM zone_runs WHERE start_utc = ?", (start_utc,)).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "program", f"expected 'program', got {row[0]!r}"

    def test_backfill_marks_unrelated_run_as_manual(self, test_db_path):
        """A row whose start_utc does NOT match any schedule becomes 'manual'."""
        db = _make_db(test_db_path)
        zone = db.create_zone(
            {
                "name": "Z1",
                "duration": 15,
                "group_id": 1,
                "topic": "/devices/t/K1",
            }
        )
        # Program at 07:00 — run inserted at 13:37 same weekday won't match.
        now_local = datetime.now().astimezone()
        days_back = (now_local.weekday() - 0) % 7 or 7
        run_dt = (now_local - timedelta(days=days_back)).replace(
            hour=13,
            minute=37,
            second=0,
            microsecond=0,
        )
        assert run_dt.weekday() == 0
        db.create_program(
            {
                "name": "P1",
                "time": "07:00",
                "days": [0],
                "zones": [zone["id"]],
                "schedule_type": "weekdays",
                "enabled": True,
            }
        )
        start_utc = run_dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
        conn = sqlite3.connect(test_db_path)
        conn.execute(
            "INSERT INTO zone_runs(zone_id, group_id, start_utc, start_monotonic, source) VALUES (?, ?, ?, ?, NULL)",
            (zone["id"], 1, start_utc, 0.0),
        )
        conn.execute("DELETE FROM migrations WHERE name = 'zone_runs_backfill_source'")
        conn.commit()
        conn.close()

        from database import IrrigationDB

        IrrigationDB(db_path=test_db_path)

        conn = sqlite3.connect(test_db_path)
        row = conn.execute("SELECT source FROM zone_runs WHERE start_utc = ?", (start_utc,)).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "manual"

    def test_backfill_handles_null_start_utc(self, test_db_path):
        """Rows with NULL start_utc fall back to 'manual' (no crash)."""
        db = _make_db(test_db_path)
        zone = db.create_zone(
            {
                "name": "Z1",
                "duration": 15,
                "group_id": 1,
                "topic": "/devices/t/K1",
            }
        )
        conn = sqlite3.connect(test_db_path)
        conn.execute(
            "INSERT INTO zone_runs(zone_id, group_id, start_utc, start_monotonic, source) VALUES (?, ?, NULL, ?, NULL)",
            (zone["id"], 1, 0.0),
        )
        conn.execute("DELETE FROM migrations WHERE name = 'zone_runs_backfill_source'")
        conn.commit()
        conn.close()

        from database import IrrigationDB

        IrrigationDB(db_path=test_db_path)

        conn = sqlite3.connect(test_db_path)
        row = conn.execute("SELECT source FROM zone_runs WHERE start_utc IS NULL").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "manual"

    def test_idempotent_rerun(self, test_db_path):
        """Running init twice does not change already-set source values."""
        db = _make_db(test_db_path)
        zone = db.create_zone(
            {
                "name": "Z1",
                "duration": 15,
                "group_id": 1,
                "topic": "/devices/t/K1",
            }
        )
        # Insert a row with explicit source='program' — should remain unchanged.
        conn = sqlite3.connect(test_db_path)
        conn.execute(
            "INSERT INTO zone_runs(zone_id, group_id, start_utc, start_monotonic, source) VALUES (?, ?, ?, ?, ?)",
            (zone["id"], 1, "2026-01-01T07:00:00Z", 0.0, "program"),
        )
        conn.commit()
        conn.close()

        from database import IrrigationDB

        IrrigationDB(db_path=test_db_path)
        IrrigationDB(db_path=test_db_path)

        conn = sqlite3.connect(test_db_path)
        row = conn.execute("SELECT source FROM zone_runs WHERE zone_id = ?", (zone["id"],)).fetchone()
        conn.close()
        assert row[0] == "program"
