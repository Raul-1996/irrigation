"""Phase-2 regressions for zone persistence and scheduling metadata."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

import db.zones as zones_module
from db.base import BaseRepository, retry_on_busy
from db.zones import ZoneRepository


@pytest.mark.parametrize("stored_group_id", [0, None])
def test_partial_zone_update_preserves_zero_or_null_group(test_db, stored_group_id):
    zone = test_db.create_zone({"name": "Ungrouped", "duration": 10, "group_id": 1})
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("UPDATE zones SET group_id = ? WHERE id = ?", (stored_group_id, zone["id"]))

    updated = test_db.update_zone(zone["id"], {"name": "Renamed"})

    assert updated is not None
    with sqlite3.connect(test_db.db_path) as conn:
        assert conn.execute("SELECT group_id FROM zones WHERE id = ?", (zone["id"],)).fetchone()[0] is stored_group_id


def test_update_zone_returns_none_for_invalid_integer_field(test_db):
    zone = test_db.create_zone({"name": "Typed", "duration": 10, "group_id": 1})

    assert test_db.update_zone(zone["id"], {"group_id": "not-an-integer"}) is None
    assert test_db.get_zone(zone["id"])["group_id"] == 1


def test_bulk_upsert_invalid_row_rolls_back_entire_batch(test_db):
    first = test_db.create_zone({"name": "First", "duration": 10, "group_id": 1})
    second = test_db.create_zone({"name": "Second", "duration": 10, "group_id": 1})

    result = test_db.bulk_upsert_zones(
        [
            {"id": first["id"], "name": "Committed"},
            {"id": second["id"], "duration": None},
        ]
    )

    assert result == {
        "success": False,
        "created": 0,
        "updated": 0,
        "failed": 2,
        "rolled_back": True,
        "errors": [{"index": 1, "id": second["id"], "code": "invalid_data"}],
    }
    assert test_db.get_zone(first["id"])["name"] == "First"
    assert test_db.get_zone(second["id"])["duration"] == 10


def test_string_group_999_removes_zone_from_programs(test_db):
    zone = test_db.create_zone({"name": "Excluded", "duration": 10, "group_id": 1})
    program = test_db.create_program(
        {
            "name": "Daily",
            "time": "06:00",
            "days": [0, 1, 2, 3, 4, 5, 6],
            "zones": [zone["id"]],
        }
    )

    updated = test_db.update_zone(zone["id"], {"group_id": "999"})

    assert updated["group_id"] == 999
    assert zone["id"] not in test_db.get_program(program["id"])["zones"]


def test_partial_update_does_not_replay_unrelated_runtime_fields(test_db):
    """A metadata update must not replay or overwrite runtime columns."""

    zone = test_db.create_zone({"name": "Before", "duration": 10, "group_id": 1})
    repository = test_db.zones
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute(
            "UPDATE zones SET state = 'on', watering_start_time = ?, postpone_until = ? WHERE id = ?",
            ("2026-07-18 06:00:00", "2026-07-18 08:00:00", zone["id"]),
        )
    repository.update_zone(zone["id"], {"name": "After"})

    persisted = repository.get_zone(zone["id"])
    assert persisted["name"] == "After"
    assert persisted["state"] == "on"
    assert persisted["watering_start_time"] == "2026-07-18 06:00:00"
    assert persisted["postpone_until"] == "2026-07-18 08:00:00"


class _BusyProbeRepository(BaseRepository):
    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self.attempts = 0

    @retry_on_busy(max_retries=2, initial_backoff=0)
    def write_while_catching_sqlite_errors(self) -> bool:
        self.attempts += 1
        try:
            with self._connect() as conn:
                conn.execute("PRAGMA busy_timeout=0")
                conn.execute("UPDATE busy_probe SET value = value + 1")
                conn.commit()
                return True
        except sqlite3.Error:
            return False

    def write_without_retry_decorator(self) -> bool:
        """Non-decorated methods must retain their sqlite.Error contract."""

        try:
            with self._connect() as conn:
                conn.execute("PRAGMA busy_timeout=0")
                conn.execute("UPDATE busy_probe SET value = value + 1")
                conn.commit()
                return True
        except sqlite3.Error:
            return False


def test_retry_on_busy_bypasses_repository_sqlite_error_catch(test_db_path):
    with sqlite3.connect(test_db_path) as conn:
        conn.execute("CREATE TABLE busy_probe(value INTEGER NOT NULL)")
        conn.execute("INSERT INTO busy_probe VALUES (0)")

    probe = _BusyProbeRepository(test_db_path)
    with sqlite3.connect(test_db_path) as blocker:
        blocker.execute("PRAGMA journal_mode=WAL")
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE busy_probe SET value = 10")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            probe.write_while_catching_sqlite_errors()

    assert probe.attempts == 3


def test_non_decorated_repository_method_keeps_sqlite_error_contract(test_db_path):
    with sqlite3.connect(test_db_path) as conn:
        conn.execute("CREATE TABLE busy_probe(value INTEGER NOT NULL)")
        conn.execute("INSERT INTO busy_probe VALUES (0)")

    probe = _BusyProbeRepository(test_db_path)
    with sqlite3.connect(test_db_path) as blocker:
        blocker.execute("PRAGMA journal_mode=WAL")
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE busy_probe SET value = 10")

        assert probe.write_without_retry_decorator() is False


def test_empty_zone_updates_remain_timestamp_only_noops(test_db):
    zone = test_db.create_zone({"name": "No-op", "duration": 10, "group_id": 1})

    assert test_db.update_zone(zone["id"], {}) is not None
    assert test_db.bulk_update_zones([{"id": zone["id"]}]) == {"updated": 1, "failed": []}
    assert test_db.bulk_upsert_zones([{"id": zone["id"]}]) == {
        "success": True,
        "created": 0,
        "updated": 1,
        "failed": 0,
        "errors": [],
    }

    persisted = test_db.get_zone(zone["id"])
    assert persisted["name"] == "No-op"
    assert persisted["duration"] == 10
    assert persisted["group_id"] == 1


def test_update_zone_versioned_retries_failed_begin_immediate(test_db, monkeypatch):
    zone = test_db.create_zone({"name": "Locked", "duration": 10, "group_id": 1})
    repository = test_db.zones
    original_connect = repository._connect
    attempts = 0

    def fast_timeout_connect():
        nonlocal attempts
        attempts += 1
        conn = original_connect()
        conn.execute("PRAGMA busy_timeout=0")
        return conn

    monkeypatch.setattr(repository, "_connect", fast_timeout_connect)
    with sqlite3.connect(test_db.db_path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE zones SET name = 'held' WHERE id = ?", (zone["id"],))
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            repository.update_zone_versioned(zone["id"], {"state": "on"}, expected_version=zone["version"])

    assert attempts == 4


def test_finish_zone_run_uses_confirmation_seen_before_atomic_finish(test_db, monkeypatch):
    zone = test_db.create_zone({"name": "Run", "duration": 10, "group_id": 1})
    run_id = test_db.create_zone_run(zone["id"], 1, "2026-07-18 06:00:00", 1.0, None, 1)
    repository = test_db.zones
    original_connect = repository._connect
    confirmation_committed = False

    def confirm_once() -> None:
        nonlocal confirmation_committed
        if confirmation_committed:
            return
        confirmation_committed = True
        with original_connect() as conn:
            conn.execute("UPDATE zone_runs SET confirmed = 1 WHERE id = ?", (run_id,))
            conn.commit()

    class InterleavingCursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def fetchone(self):
            row = self._cursor.fetchone()
            confirm_once()
            return row

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class InterleavingConnection:
        def __init__(self):
            self._conn = original_connect()

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, *args):
            return self._conn.__exit__(*args)

        def execute(self, sql, params=()):
            normalized = " ".join(sql.split())
            if normalized.startswith("UPDATE zone_runs SET"):
                confirm_once()
            cursor = self._conn.execute(sql, params)
            if normalized.startswith("SELECT confirmed FROM zone_runs"):
                return InterleavingCursor(cursor)
            return cursor

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(repository, "_connect", InterleavingConnection)

    assert repository.finish_zone_run(run_id, "2026-07-18 06:10:00", 2.0, None, None, None) is True
    with sqlite3.connect(test_db.db_path) as conn:
        assert conn.execute("SELECT status, confirmed FROM zone_runs WHERE id = ?", (run_id,)).fetchone() == (
            "ok",
            1,
        )


def test_multi_day_interval_does_not_publish_unrecoverable_daily_guess(test_db):
    zone = test_db.create_zone({"name": "Interval", "duration": 10, "group_id": 1})
    test_db.create_program(
        {
            "name": "Every three days",
            "time": "06:00",
            "days": [],
            "zones": [zone["id"]],
            "schedule_type": "interval",
            "interval_days": 3,
        }
    )

    assert test_db.compute_next_run_for_zone(zone["id"]) is None


def test_postpone_anchor_is_shifted_by_zone_offset(test_db, monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 20, 6, 10, 0, tzinfo=tz)

    monkeypatch.setattr(zones_module, "datetime", FixedDateTime)
    first = test_db.create_zone({"name": "First", "duration": 30, "group_id": 1})
    second = test_db.create_zone({"name": "Second", "duration": 30, "group_id": 1})
    target = test_db.create_zone({"name": "Target", "duration": 10, "group_id": 1})
    test_db.create_program(
        {
            "name": "Morning",
            "time": "06:00",
            "days": [0, 1, 2, 3, 4, 5, 6],
            "zones": [first["id"], second["id"], target["id"]],
        }
    )
    test_db.update_zone_postpone(target["id"], "2026-07-20 06:30:00", "manual")

    assert test_db.compute_next_run_for_zone(target["id"]) == "2026-07-20 07:00:00"
