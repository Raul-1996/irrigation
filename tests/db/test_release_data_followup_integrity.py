"""Release follow-up contracts for zone versions and corrective migrations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from db.migrations import MigrationRunner


def _create_program(test_db, zone_ids: list[int], *, name: str = "Program") -> dict[str, Any]:
    program = test_db.create_program(
        {
            "name": name,
            "time": "06:00",
            "days": [0],
            "zones": zone_ids,
            "enabled": True,
        }
    )
    assert program is not None
    return program


def _zone_version(test_db, zone_id: int) -> int:
    zone = test_db.get_zone(zone_id)
    assert zone is not None
    return int(zone["version"])


@pytest.mark.parametrize(
    "operation",
    [
        "plain",
        "bulk_update",
        "bulk_upsert",
        "scheduled_set",
        "scheduled_clear",
        "postpone",
        "rain_apply",
        "rain_clear",
        "photo",
        "photo_and_thumb",
    ],
)
def test_every_zone_repository_update_increments_version_exactly_once(test_db, operation: str):
    zone = test_db.create_zone({"name": "Versioned", "duration": 10, "group_id": 1})
    zone_id = int(zone["id"])

    if operation == "rain_clear":
        with sqlite3.connect(test_db.db_path) as conn:
            conn.execute(
                "UPDATE zones SET postpone_until = '2026-08-01', postpone_reason = 'rain' WHERE id = ?",
                (zone_id,),
            )

    before = _zone_version(test_db, zone_id)
    operations: dict[str, Callable[[], object]] = {
        "plain": lambda: test_db.update_zone(zone_id, {"name": "Renamed"}),
        "bulk_update": lambda: test_db.bulk_update_zones([{"id": zone_id, "name": "Bulk"}]),
        "bulk_upsert": lambda: test_db.bulk_upsert_zones([{"id": zone_id, "name": "Imported"}]),
        "scheduled_set": lambda: test_db.set_group_scheduled_starts(1, {zone_id: "2026-08-01 06:00:00"}),
        "scheduled_clear": lambda: test_db.clear_group_scheduled_starts(1),
        "postpone": lambda: test_db.update_zone_postpone(zone_id, "2026-08-01", "manual"),
        "rain_apply": lambda: test_db.apply_group_rain_postpone_atomic(1, "2026-08-01"),
        "rain_clear": lambda: test_db.clear_group_rain_postpone_atomic(1),
        "photo": lambda: test_db.update_zone_photo(zone_id, "zone.jpg"),
        "photo_and_thumb": lambda: test_db.update_zone_photo(
            zone_id,
            "zone.jpg",
            photo_thumb="zone-thumb.jpg",
            update_thumb=True,
        ),
    }

    operations[operation]()

    assert _zone_version(test_db, zone_id) == before + 1


def test_peer_schedule_update_increments_only_rows_actually_updated(test_db):
    excluded = test_db.create_zone({"name": "Excluded", "group_id": 1})
    peer = test_db.create_zone({"name": "Peer", "group_id": 1})
    excluded_before = _zone_version(test_db, excluded["id"])
    peer_before = _zone_version(test_db, peer["id"])

    test_db.clear_scheduled_for_zone_group_peers(excluded["id"], 1)

    assert _zone_version(test_db, excluded["id"]) == excluded_before
    assert _zone_version(test_db, peer["id"]) == peer_before + 1


def test_raw_external_zone_update_is_guarded_by_forward_version_trigger(test_db):
    zone = test_db.create_zone({"name": "Raw", "group_id": 1})
    before = _zone_version(test_db, zone["id"])

    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("UPDATE zones SET name = 'external' WHERE id = ?", (zone["id"],))

    assert _zone_version(test_db, zone["id"]) == before + 1

    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("UPDATE zones SET version = 0 WHERE id = ?", (zone["id"],))

    assert _zone_version(test_db, zone["id"]) == before + 2


def test_version_trigger_is_reconciled_even_when_marker_already_exists(test_db):
    with sqlite3.connect(test_db.db_path) as conn:
        assert conn.execute("SELECT 1 FROM migrations WHERE name = 'zones_version_invalidation_v1'").fetchone() == (1,)
        conn.execute("DROP TRIGGER trg_zones_version_invalidate")

    MigrationRunner(test_db.db_path).init_database()

    with sqlite3.connect(test_db.db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'trg_zones_version_invalidate'"
        ).fetchone() == (1,)


def test_version_trigger_with_wrong_sql_is_replaced_before_migration_writes(test_db):
    zone = test_db.create_zone({"name": "Tamper proof", "group_id": 1})
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("DROP TRIGGER trg_zones_version_invalidate")
        conn.execute(
            """
            CREATE TRIGGER trg_zones_version_invalidate
            AFTER UPDATE ON zones
            BEGIN
                SELECT 1;
            END
            """
        )

    MigrationRunner(test_db.db_path).init_database()
    before = _zone_version(test_db, zone["id"])
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("UPDATE zones SET name = 'external' WHERE id = ?", (zone["id"],))

    assert _zone_version(test_db, zone["id"]) == before + 1


def test_detailed_cas_returns_atomic_enriched_post_write_row(test_db):
    zone = test_db.create_zone({"name": "CAS", "group_id": 1})
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute(
            "INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, status, source) "
            "VALUES (?, 1, '2026-07-01 05:00:00', '2026-07-01 05:10:00', 'ok', 'manual')",
            (zone["id"],),
        )

    result = test_db.update_zone_versioned_detailed(
        zone["id"],
        {"name": "CAS updated"},
        expected_version=zone["version"],
    )

    assert result["success"] is True
    assert result["reason"] == "updated"
    assert result["previous"]["version"] == zone["version"]
    assert result["current"]["name"] == "CAS updated"
    assert result["current"]["version"] == zone["version"] + 1
    assert result["current"]["group"] == 1
    assert result["current"]["group_name"] == "Насос-1"
    assert result["current"]["last_watering_time"] == "2026-07-01 05:10:00"
    assert result["current"]["updated_at"] is not None
    assert result["affected_program_ids"] == []


def test_detailed_cas_conflict_returns_locked_current_snapshot(test_db):
    zone = test_db.create_zone({"name": "CAS", "group_id": 1})
    updated = test_db.update_zone(zone["id"], {"name": "Newer"})

    result = test_db.update_zone_versioned_detailed(
        zone["id"],
        {"name": "Stale"},
        expected_version=zone["version"],
    )

    assert result == {
        "success": False,
        "reason": "version_conflict",
        "previous": None,
        "current": {key: value for key, value in updated.items() if key != "affected_program_ids"},
        "affected_program_ids": [],
    }
    assert test_db.get_zone(zone["id"])["name"] == "Newer"


@pytest.mark.parametrize("path", ["plain", "cas", "bulk_update", "bulk_upsert"])
def test_group_999_unlink_is_atomic_and_reports_affected_programs(test_db, path: str):
    zone = test_db.create_zone({"name": "Excluded", "group_id": 1})
    program = _create_program(test_db, [zone["id"]])
    # Legacy installations can contain numeric zone identifiers serialized as
    # strings. They remain semantically the same zone and must be unlinked.
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute(
            "UPDATE programs SET zones = ? WHERE id = ?",
            (json.dumps([str(zone["id"])]), program["id"]),
        )

    if path == "plain":
        result = test_db.update_zone(zone["id"], {"group_id": 999})
    elif path == "cas":
        result = test_db.update_zone_versioned_detailed(
            zone["id"],
            {"group_id": 999},
            expected_version=zone["version"],
        )
    elif path == "bulk_update":
        result = test_db.bulk_update_zones([{"id": zone["id"], "group_id": 999}])
    else:
        result = test_db.bulk_upsert_zones([{"id": zone["id"], "group_id": 999}])

    assert result["affected_program_ids"] == [program["id"]]
    assert test_db.get_zone(zone["id"])["group_id"] == 999
    persisted_program = test_db.get_program(program["id"])
    assert persisted_program["zones"] == []
    assert persisted_program["enabled"] is False


def test_group_999_new_import_unlinks_legacy_dangling_program_reference(test_db):
    with sqlite3.connect(test_db.db_path) as conn:
        program_id = conn.execute(
            "INSERT INTO programs(name, time, days, zones, enabled) VALUES ('Dangling', '06:00', '[0]', '[777]', 1)"
        ).lastrowid

    result = test_db.bulk_upsert_zones([{"id": 777, "name": "Excluded import", "group_id": 999}])

    assert result["success"] is True
    assert result["affected_program_ids"] == [program_id]
    program = test_db.get_program(program_id)
    assert program["zones"] == []
    assert program["enabled"] is False


def test_group_999_unlink_keeps_program_enabled_when_another_zone_remains(test_db):
    moved = test_db.create_zone({"name": "Moved", "group_id": 1})
    remaining = test_db.create_zone({"name": "Remaining", "group_id": 1})
    program = _create_program(test_db, [moved["id"], remaining["id"]])

    result = test_db.update_zone_versioned_detailed(
        moved["id"],
        {"group_id": 999},
        expected_version=moved["version"],
    )

    assert result["affected_program_ids"] == [program["id"]]
    persisted = test_db.get_program(program["id"])
    assert persisted["zones"] == [remaining["id"]]
    assert persisted["enabled"] is True


def test_compat_cas_keeps_tuple_shape_and_attaches_nonempty_program_effects(test_db):
    zone = test_db.create_zone({"name": "Compat", "group_id": 1})
    program = _create_program(test_db, [zone["id"]])

    applied, previous = test_db.update_zone_versioned(
        zone["id"],
        {"group_id": 999},
        expected_version=zone["version"],
    )

    assert applied is True
    assert previous["version"] == zone["version"]
    assert previous["affected_program_ids"] == [program["id"]]


@pytest.mark.parametrize("stored_zones", ["{broken", '{"1": true}'])
@pytest.mark.parametrize("path", ["plain", "cas", "bulk_update", "bulk_upsert"])
def test_group_999_unlink_fails_closed_on_malformed_program_zones(
    test_db,
    path: str,
    stored_zones: str,
):
    zone = test_db.create_zone({"name": "Protected", "group_id": 1})
    program = _create_program(test_db, [zone["id"]])
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("UPDATE programs SET zones = ? WHERE id = ?", (stored_zones, program["id"]))

    before_version = _zone_version(test_db, zone["id"])
    if path == "plain":
        result = test_db.update_zone(zone["id"], {"group_id": 999})
        assert result is None
    elif path == "cas":
        result = test_db.update_zone_versioned_detailed(
            zone["id"],
            {"group_id": 999},
            expected_version=before_version,
        )
        assert result["success"] is False
        assert result["reason"] == "database_error"
    elif path == "bulk_update":
        result = test_db.bulk_update_zones([{"id": zone["id"], "group_id": 999}])
        assert result["updated"] == 0
        assert result["failed"] == [zone["id"]]
    else:
        result = test_db.bulk_upsert_zones([{"id": zone["id"], "group_id": 999}])
        assert result["success"] is False
        assert result["rolled_back"] is True

    persisted = test_db.get_zone(zone["id"])
    assert persisted["group_id"] == 1
    assert persisted["version"] == before_version


def test_source_correction_clears_only_pre_marker_unverifiable_labels(test_db_path):
    runner = MigrationRunner(test_db_path)
    runner.init_database()
    with sqlite3.connect(test_db_path) as conn:
        conn.execute("DELETE FROM migrations WHERE name = 'zone_runs_clear_unverifiable_source_v1'")
        conn.execute(
            "UPDATE migrations SET applied_at = '2026-01-02 00:00:00' WHERE name = 'zone_runs_backfill_source'"
        )
        rows = [
            (1, "program", "2026-01-01 00:00:00"),
            (2, "manual", "2026-01-02 00:00:00"),
            (3, "manual", "2026-01-03 00:00:00"),
            (4, "operator_import", "2026-01-01 00:00:00"),
        ]
        conn.executemany(
            "INSERT INTO zone_runs(id, zone_id, group_id, source, created_at) VALUES (?, 1, 1, ?, ?)",
            rows,
        )

    runner.init_database()

    with sqlite3.connect(test_db_path) as conn:
        assert conn.execute("SELECT id, source FROM zone_runs ORDER BY id").fetchall() == [
            (1, None),
            (2, None),
            (3, "manual"),
            (4, "operator_import"),
        ]
        assert conn.execute(
            "SELECT 1 FROM migrations WHERE name = 'zone_runs_clear_unverifiable_source_v1'"
        ).fetchone() == (1,)


def test_forward_migration_disables_enabled_smart_programs_with_audit(test_db_path):
    runner = MigrationRunner(test_db_path)
    runner.init_database()
    with sqlite3.connect(test_db_path) as conn:
        conn.execute("DELETE FROM migrations WHERE name = 'programs_disable_unsupported_smart_v1'")
        conn.executemany(
            "INSERT INTO programs(id, name, time, days, zones, type, enabled) VALUES (?, ?, '06:00', '[0]', '[]', ?, ?)",
            [
                (101, "Enabled smart", "smart", 1),
                (102, "Disabled smart", "smart", 0),
                (103, "Enabled time", "time-based", 1),
                (104, "Legacy spaced smart", " SMART ", 1),
            ],
        )

    runner.init_database()

    with sqlite3.connect(test_db_path) as conn:
        assert conn.execute("SELECT id, enabled FROM programs WHERE id >= 101 ORDER BY id").fetchall() == [
            (101, 0),
            (102, 0),
            (103, 1),
            (104, 0),
        ]
        audit = conn.execute(
            "SELECT actor, source, action_type, target, result, error_msg "
            "FROM audit_log WHERE action_type = 'migration_disable_unsupported_smart'"
        ).fetchall()
        assert audit == [
            (
                "system",
                "migration",
                "migration_disable_unsupported_smart",
                "program:101",
                "disabled",
                "PROGRAM_TYPE_UNSUPPORTED",
            ),
            (
                "system",
                "migration",
                "migration_disable_unsupported_smart",
                "program:104",
                "disabled",
                "PROGRAM_TYPE_UNSUPPORTED",
            ),
        ]
        assert conn.execute(
            "SELECT 1 FROM migrations WHERE name = 'programs_disable_unsupported_smart_v1'"
        ).fetchone() == (1,)
