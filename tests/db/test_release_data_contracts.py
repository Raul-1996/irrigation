"""Release regressions for CAS and fail-closed migration contracts."""

from __future__ import annotations

import sqlite3
from unittest.mock import Mock, call

from db.migrations import MigrationRunner
from services.zones_state import update_zone_state, update_zone_state_internal


def test_repository_cas_uses_the_callers_expected_version(test_db):
    zone = test_db.create_zone({"name": "CAS", "duration": 10, "group_id": 1})

    won, previous = test_db.update_zone_versioned(
        zone["id"],
        {"name": "winner"},
        expected_version=zone["version"],
    )
    lost, current = test_db.update_zone_versioned(
        zone["id"],
        {"name": "stale"},
        expected_version=zone["version"],
    )

    assert won is True
    assert previous["version"] == zone["version"]
    assert lost is False
    assert current["version"] == zone["version"] + 1
    persisted = test_db.get_zone(zone["id"])
    assert persisted["name"] == "winner"
    assert persisted["version"] == zone["version"] + 1


def test_state_service_never_falls_back_after_cas_conflict():
    fake_db = Mock()
    fake_db.update_zone_versioned.return_value = (False, {"id": 7, "version": 4, "state": "on"})

    ok, snapshot = update_zone_state(
        7,
        {"state": "off"},
        expected_version=3,
        audit_reason="regression",
        db=fake_db,
    )

    assert ok is False
    assert snapshot["version"] == 4
    fake_db.update_zone_versioned.assert_called_once_with(7, {"state": "off"}, expected_version=3)
    fake_db.update_zone.assert_not_called()


def test_internal_state_transition_never_merges_into_a_newer_snapshot():
    fake_db = Mock()
    authorised = {"id": 7, "version": 3, "command_id": "old", "state": "stopping"}
    newer = {"id": 7, "version": 4, "command_id": "new", "state": "starting"}
    fake_db.update_zone_versioned.return_value = (False, newer)

    ok, snapshot = update_zone_state_internal(
        7,
        {"state": "off", "command_id": None},
        snapshot=authorised,
        audit_reason="physical_echo",
        db=fake_db,
    )

    assert ok is False
    assert snapshot == newer
    fake_db.update_zone_versioned.assert_called_once_with(
        7,
        {"state": "off", "command_id": None},
        expected_version=3,
    )
    fake_db.update_zone.assert_not_called()


def test_internal_state_transition_retries_only_unrelated_version_conflict():
    fake_db = Mock()
    authorised = {
        "id": 7,
        "version": 3,
        "name": "before",
        "state": "stopping",
        "commanded_state": "off",
        "command_id": "same-generation",
    }
    metadata_edit = {**authorised, "version": 4, "name": "after"}
    fake_db.update_zone_versioned.side_effect = [(False, metadata_edit), (True, metadata_edit)]

    ok, previous = update_zone_state_internal(
        7,
        {"observed_state": "off"},
        snapshot=authorised,
        audit_reason="physical_echo",
        db=fake_db,
    )

    assert ok is True
    assert previous == metadata_edit
    assert fake_db.update_zone_versioned.call_args_list == [
        call(7, {"observed_state": "off"}, expected_version=3),
        call(7, {"observed_state": "off"}, expected_version=4),
    ]
    fake_db.update_zone.assert_not_called()


def test_live_migration_downgrade_is_preview_only(test_db_path):
    runner = MigrationRunner(test_db_path)
    runner.init_database()
    before = runner.preview_rollback_migration("weather_create_log")

    assert before == {
        "migration": "weather_create_log",
        "known": True,
        "applied": True,
        "supported": False,
        "would_mutate": False,
        "error_code": "LIVE_DOWNGRADE_UNSUPPORTED",
        "recovery": "restore a pre-upgrade database backup",
    }
    assert runner.rollback_migration("weather_create_log") is False

    with sqlite3.connect(test_db_path) as conn:
        assert conn.execute("SELECT 1 FROM migrations WHERE name = 'weather_create_log'").fetchone() == (1,)
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'weather_log'").fetchone() == (
            1,
        )


def test_historical_zone_run_source_migration_is_non_mutating_preview(test_db_path):
    runner = MigrationRunner(test_db_path)
    runner.init_database()
    with sqlite3.connect(test_db_path) as conn:
        conn.execute(
            "INSERT INTO zone_runs(zone_id, group_id, start_utc, start_monotonic, source) "
            "VALUES (1, 1, '2026-01-05T06:00:00Z', 0.0, NULL)"
        )
        conn.execute("DELETE FROM migrations WHERE name = 'zone_runs_backfill_source'")
        conn.commit()

    runner.init_database()

    preview = runner.preview_zone_runs_source_backfill()
    assert preview == {
        "supported": False,
        "would_mutate": False,
        "error_code": "HISTORICAL_SOURCE_IDENTITY_UNAVAILABLE",
        "unresolved_rows": 1,
    }
    with sqlite3.connect(test_db_path) as conn:
        assert conn.execute("SELECT source FROM zone_runs").fetchone() == (None,)
        assert conn.execute("SELECT 1 FROM migrations WHERE name = 'zone_runs_backfill_source'").fetchone() == (1,)
