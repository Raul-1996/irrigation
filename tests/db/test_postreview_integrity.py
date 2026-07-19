"""Post-review regressions for durable DB identity and recovery contracts."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

from database import IrrigationDB
from db.base import BaseRepository, retry_on_busy
from db.migrations import MigrationRunner


def _program_payload(name: str, zones: list[int]) -> dict[str, object]:
    return {
        "name": name,
        "time": "06:00",
        "days": [0, 1, 2, 3, 4, 5, 6],
        "zones": zones,
    }


def test_deleted_zone_identity_is_retired_without_destroying_history(test_db):
    zone = test_db.create_zone({"name": "Retired", "duration": 10, "group_id": 1})
    program = test_db.create_program(_program_payload("Legacy schedule", [zone["id"]]))
    run_id = test_db.create_zone_run(zone["id"], 1, "2026-07-19 06:00:00", 1.0, None, 1)

    assert test_db.delete_zone(zone["id"]) is True
    assert test_db.get_program(program["id"])["zones"] == []

    with sqlite3.connect(test_db.db_path) as conn:
        assert conn.execute("SELECT zone_id FROM zone_runs WHERE id = ?", (run_id,)).fetchone() == (zone["id"],)

    replacement = test_db.create_zone({"name": "Replacement", "duration": 10, "group_id": 1})
    explicit_replacement = test_db.create_zone(
        {"id": zone["id"], "name": "Imported replacement", "duration": 10, "group_id": 1}
    )

    assert replacement["id"] > zone["id"]
    assert explicit_replacement["id"] > zone["id"]
    assert test_db.get_zone(zone["id"]) is None


def test_referenced_mqtt_server_delete_is_restricted_and_structured(test_db):
    server = test_db.create_mqtt_server({"name": "Referenced", "host": "broker-a", "port": 1883})
    zone = test_db.create_zone(
        {
            "name": "Hardware zone",
            "duration": 10,
            "group_id": 1,
            "topic": "/devices/wb/K1",
            "mqtt_server_id": server["id"],
        }
    )
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute(
            "UPDATE groups SET master_mqtt_server_id = ?, float_mqtt_server_id = ? WHERE id = 1",
            (server["id"], server["id"]),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('rain.server_id', ?)",
            (str(server["id"]),),
        )

    references = test_db.get_mqtt_server_references(server["id"])

    assert references["zones"] == [zone["id"]]
    assert references["groups_master"] == [1]
    assert references["groups_float"] == [1]
    assert references["settings"] == ["rain.server_id"]
    assert test_db.delete_mqtt_server(server["id"]) is False
    assert test_db.get_mqtt_server(server["id"])["host"] == "broker-a"


def test_unreferenced_mqtt_server_id_is_never_reused(test_db):
    server = test_db.create_mqtt_server({"name": "Old", "host": "broker-a", "port": 1883})

    assert test_db.delete_mqtt_server(server["id"]) is True
    replacement = test_db.create_mqtt_server({"name": "New", "host": "broker-b", "port": 1883})

    assert replacement["id"] > server["id"]


def test_missing_mqtt_server_update_is_not_reported_as_success(test_db):
    assert test_db.update_mqtt_server(999_999, {"host": "missing"}) is False


def test_program_delete_cascades_cancellation_and_never_reuses_identity(test_db):
    program = test_db.create_program(_program_payload("Old program", []))
    assert test_db.cancel_program_run_for_group(program["id"], "2026-07-19", 1) is True

    assert test_db.delete_program(program["id"]) is True
    replacement = test_db.create_program(_program_payload("New program", []))

    assert replacement["id"] > program["id"]
    assert not test_db.is_program_run_cancelled_for_group(replacement["id"], "2026-07-19", 1)
    with sqlite3.connect(test_db.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM program_cancellations WHERE program_id = ?", (program["id"],)
        ).fetchone() == (0,)


def test_deleted_group_identity_is_retired_without_destroying_history(test_db):
    group = test_db.create_group("Historical group")
    zone = test_db.create_zone({"name": "History source", "duration": 10, "group_id": 1})
    run_id = test_db.create_zone_run(zone["id"], group["id"], "2026-07-19 06:00:00", 1.0, None, 1)

    assert test_db.delete_group(group["id"]) is True
    replacement = test_db.create_group("Replacement group")

    assert replacement["id"] > group["id"]
    with sqlite3.connect(test_db.db_path) as conn:
        assert conn.execute("SELECT group_id FROM zone_runs WHERE id = ?", (run_id,)).fetchone() == (group["id"],)


class _PostCommitBusyProbe(BaseRepository):
    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self.attempts = 0

    @retry_on_busy(max_retries=3, initial_backoff=0)
    def insert_then_observe_busy(self) -> None:
        self.attempts += 1
        with self._connect() as conn:
            conn.execute("INSERT INTO post_commit_probe(value) VALUES (?)", (self.attempts,))
            conn.commit()
        # Models create_* methods whose follow-up read hits contention after
        # the INSERT has already become durable.
        raise sqlite3.OperationalError("database is locked after commit")


def test_busy_retry_never_replays_an_operation_after_a_successful_commit(test_db_path):
    with sqlite3.connect(test_db_path) as conn:
        conn.execute("CREATE TABLE post_commit_probe(value INTEGER NOT NULL)")

    probe = _PostCommitBusyProbe(test_db_path)
    with pytest.raises(sqlite3.OperationalError, match="locked after commit"):
        probe.insert_then_observe_busy()

    assert probe.attempts == 1
    with sqlite3.connect(test_db_path) as conn:
        assert conn.execute("SELECT value FROM post_commit_probe").fetchall() == [(1,)]


def test_strict_zone_inventory_propagates_database_errors(test_db, monkeypatch):
    def fail_connect():
        raise sqlite3.OperationalError("inventory unavailable")

    monkeypatch.setattr(test_db.zones, "_connect", fail_connect)

    assert test_db.get_zones() == []
    with pytest.raises(sqlite3.OperationalError, match="inventory unavailable"):
        test_db.get_zones_strict()


def test_strict_mqtt_server_read_distinguishes_missing_row_from_database_error(test_db, monkeypatch):
    assert test_db.get_mqtt_servers_strict() == []
    assert test_db.get_mqtt_server_strict(999_999) is None

    def fail_connect():
        raise sqlite3.OperationalError("mqtt inventory unavailable")

    monkeypatch.setattr(test_db.mqtt, "_connect", fail_connect)

    assert test_db.get_mqtt_servers() == []
    assert test_db.get_mqtt_server(999_999) is None
    with pytest.raises(sqlite3.OperationalError, match="mqtt inventory unavailable"):
        test_db.get_mqtt_servers_strict()
    with pytest.raises(sqlite3.OperationalError, match="mqtt inventory unavailable"):
        test_db.get_mqtt_server_strict(999_999)


def test_named_migration_failure_aborts_database_initialization(tmp_path, monkeypatch):
    db_path = tmp_path / "migration-failure.db"
    runner = MigrationRunner(str(db_path))

    def fail_migration(_conn):
        raise RuntimeError("schema invariant failed")

    monkeypatch.setattr(runner, "_migrate_days_format", fail_migration)

    with pytest.raises(RuntimeError, match="schema invariant failed"):
        runner.init_database()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT 1 FROM migrations WHERE name = 'days_format'").fetchone() is None


def test_fresh_schema_has_durable_ids_cascade_and_required_indexes(test_db_path):
    IrrigationDB(test_db_path)

    with sqlite3.connect(test_db_path) as conn:
        table_sql = {
            name: sql
            for name, sql in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('zones', 'groups', 'mqtt_servers', 'programs', 'program_cancellations')"
            ).fetchall()
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name IN ('zones', 'zone_runs')"
            ).fetchall()
        }
        triggers = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()}

    for table in ("zones", "groups", "mqtt_servers", "programs"):
        assert "AUTOINCREMENT" in table_sql[table].upper()
    cancellation_sql = " ".join(table_sql["program_cancellations"].upper().split())
    assert "REFERENCES PROGRAMS(ID) ON DELETE CASCADE" in cancellation_sql
    assert {
        "idx_zones_group",
        "idx_zones_mqtt_server",
        "idx_zones_topic",
        "idx_zone_runs_last_ok",
        "idx_zone_runs_start",
        "idx_zone_runs_group_start",
    } <= indexes
    assert {
        "trg_zones_retire_id",
        "trg_zones_reject_retired_id",
        "trg_groups_retire_id",
        "trg_groups_reject_retired_id",
        "trg_mqtt_servers_retire_id",
        "trg_mqtt_servers_reject_retired_id",
        "trg_programs_retire_id",
        "trg_programs_reject_retired_id",
        "trg_zones_reject_out_of_range_id",
        "trg_groups_reject_out_of_range_id",
        "trg_mqtt_servers_reject_out_of_range_id",
        "trg_programs_reject_out_of_range_id",
        "trg_zones_mqtt_server_insert",
        "trg_zones_mqtt_server_update",
        "trg_groups_mqtt_server_insert",
        "trg_groups_mqtt_server_update",
        "trg_settings_mqtt_server_insert",
        "trg_settings_mqtt_server_update",
        "trg_mqtt_servers_restrict_referenced_delete",
    } <= triggers


def _strip_autoincrement(conn: sqlite3.Connection, table: str) -> None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    assert row and row[0]
    old_sql = row[0]
    if "AUTOINCREMENT" not in old_sql.upper():
        return
    indexes = [
        sql
        for (sql,) in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL", (table,)
        ).fetchall()
    ]
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    temp = f"{table}__legacy"
    create_sql = re.sub(
        rf"(?i)CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\"{table}\"|{table})",
        f"CREATE TABLE {temp}",
        old_sql,
        count=1,
    ).replace("AUTOINCREMENT", "")
    conn.execute(create_sql)
    names = ", ".join(columns)
    conn.execute(f"INSERT INTO {temp} ({names}) SELECT {names} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {temp} RENAME TO {table}")
    for sql in indexes:
        conn.execute(sql)


def _remove_cancellation_fk(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE program_cancellations__legacy (
            program_id INTEGER NOT NULL,
            run_date TEXT NOT NULL,
            group_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (program_id, run_date, group_id)
        )
    """)
    conn.execute(
        "INSERT INTO program_cancellations__legacy "
        "SELECT program_id, run_date, group_id, created_at FROM program_cancellations"
    )
    conn.execute("DROP TABLE program_cancellations")
    conn.execute("ALTER TABLE program_cancellations__legacy RENAME TO program_cancellations")


def test_existing_database_upgrade_retires_dangling_ids_without_losing_history(test_db_path):
    IrrigationDB(test_db_path)

    with sqlite3.connect(test_db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        for trigger in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
            if trigger[0].startswith(("trg_zones_", "trg_groups_", "trg_mqtt_servers_", "trg_settings_")):
                conn.execute(f"DROP TRIGGER {trigger[0]}")
        for table in ("zones", "groups", "mqtt_servers", "programs"):
            _strip_autoincrement(conn, table)
        _remove_cancellation_fk(conn)
        conn.execute("DROP TABLE IF EXISTS retired_entity_ids")
        conn.execute(
            "DELETE FROM migrations WHERE name IN "
            "('durable_entity_ids_v1', 'durable_entity_ids_v2', 'program_cancellations_fk_v1', "
            "'restore_runtime_indexes_v1', 'mqtt_reference_integrity_v1')"
        )

        conn.execute(
            "INSERT INTO zones(id, name, duration, group_id, topic, mqtt_server_id) VALUES (5, 'Legacy', 10, 1, '/z', 88)"
        )
        conn.execute(
            "INSERT INTO programs(id, name, time, days, zones) VALUES (1, 'Legacy program', '06:00', '[]', '[77]')"
        )
        conn.execute("INSERT INTO program_cancellations(program_id, run_date, group_id) VALUES (55, '2026-07-19', 1)")
        conn.execute(
            "INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, status, source, confirmed) "
            "VALUES (77, 1200, '2026-07-18 06:00:00', '2026-07-18 06:10:00', 'ok', 'program', 1)"
        )
        conn.execute(
            "INSERT INTO weather_log(zone_id, original_duration, adjusted_duration, coefficient) "
            "VALUES (78, 10, 10, 100)"
        )
        conn.execute(
            "INSERT INTO program_queue_log(entry_id, program_id, group_id, zone_ids, scheduled_time, "
            "enqueued_at, state) VALUES ('legacy-entry', 1, 1, '[79]', '06:00', "
            "'2026-07-18 06:00:00', 'completed')"
        )
        conn.execute("INSERT INTO float_events(group_id, event_type, paused_zones) VALUES (1, 'pause', '[80]')")
        conn.execute("UPDATE groups SET master_mqtt_server_id = 88, float_mqtt_server_id = 88 WHERE id = 1")
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('rain.server_id', '88')")
        conn.commit()

    upgraded = IrrigationDB(test_db_path)
    assert upgraded.update_zone(5, {"name": "Must repair missing broker first"}) is None
    assert upgraded.update_group_fields(1, {"master_valve_observed": "closed"}) is False
    assert upgraded.set_setting_value("rain.server_id", "88") is False
    zone = upgraded.create_zone({"name": "After upgrade", "duration": 10, "group_id": 1})
    server = upgraded.create_mqtt_server({"name": "After upgrade", "host": "broker", "port": 1883})
    program = upgraded.create_program(_program_payload("After upgrade", []))
    group = upgraded.create_group("After upgrade")

    assert zone["id"] > 80
    assert server["id"] > 88
    assert program["id"] > 55
    assert group["id"] > 1200
    assert upgraded.get_zone(5)["name"] == "Legacy"
    assert upgraded.get_program(1)["zones"] == [77]
    with sqlite3.connect(test_db_path) as conn:
        assert conn.execute("SELECT zone_id, group_id, status FROM zone_runs WHERE zone_id = 77").fetchone() == (
            77,
            1200,
            "ok",
        )
        assert conn.execute("SELECT 1 FROM retired_entity_ids WHERE entity = 'zone' AND id = 77").fetchone()
        assert conn.execute("SELECT 1 FROM retired_entity_ids WHERE entity = 'zone' AND id = 78").fetchone()
        assert conn.execute("SELECT 1 FROM retired_entity_ids WHERE entity = 'zone' AND id = 79").fetchone()
        assert conn.execute("SELECT 1 FROM retired_entity_ids WHERE entity = 'zone' AND id = 80").fetchone()
        assert conn.execute("SELECT 1 FROM retired_entity_ids WHERE entity = 'mqtt_server' AND id = 88").fetchone()
        assert conn.execute("SELECT 1 FROM retired_entity_ids WHERE entity = 'program' AND id = 55").fetchone()
        assert conn.execute("SELECT 1 FROM retired_entity_ids WHERE entity = 'group' AND id = 1200").fetchone()
        assert conn.execute("SELECT 1 FROM program_cancellations WHERE program_id = 55").fetchone() is None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_zone_snapshots_use_indexed_latest_run_lookup(test_db):
    zone = test_db.create_zone({"name": "Indexed", "duration": 10, "group_id": 1})
    with sqlite3.connect(test_db.db_path) as conn:
        conn.executemany(
            "INSERT INTO zone_runs(zone_id, group_id, end_utc, status) VALUES (?, 1, ?, 'ok')",
            ((zone["id"], f"2026-07-{day:02d} 06:10:00") for day in range(1, 20)),
        )

    statements: list[str] = []
    original_connect = test_db.zones._connect

    def traced_connect():
        conn = original_connect()
        conn.set_trace_callback(statements.append)
        return conn

    test_db.zones._connect = traced_connect
    try:
        assert test_db.get_zones()[0]["last_watering_time"] == "2026-07-19 06:10:00"
        assert test_db.get_zones_by_group(1)[0]["last_watering_time"] == "2026-07-19 06:10:00"
    finally:
        test_db.zones._connect = original_connect

    zone_run_queries = [sql for sql in statements if "zone_runs" in sql]
    assert zone_run_queries
    assert all("GROUP BY zone_id" not in sql for sql in zone_run_queries)
    assert all("ORDER BY zr.end_utc DESC" in sql and "LIMIT 1" in sql for sql in zone_run_queries)

    with sqlite3.connect(test_db.db_path) as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT "
            "(SELECT zr.end_utc FROM zone_runs zr "
            "WHERE zr.zone_id = z.id AND zr.status = 'ok' AND zr.end_utc IS NOT NULL "
            "ORDER BY zr.end_utc DESC LIMIT 1) FROM zones z"
        ).fetchall()
    assert any("idx_zone_runs_last_ok" in str(row) for row in plan)

    with sqlite3.connect(test_db.db_path) as conn:
        global_plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM zone_runs ORDER BY start_utc DESC").fetchall()
        group_plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM zone_runs WHERE group_id = ? ORDER BY start_utc DESC",
            (1,),
        ).fetchall()
    assert any("idx_zone_runs_start" in str(row) for row in global_plan)
    assert any("idx_zone_runs_group_start" in str(row) for row in group_plan)


def test_bulk_zone_import_rolls_back_mid_batch_constraint_failure(test_db):
    first = test_db.create_zone({"name": "First before", "duration": 10, "group_id": 1})
    second = test_db.create_zone({"name": "Second before", "duration": 10, "group_id": 1})
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("""
            CREATE TRIGGER reject_injected_zone_import
            BEFORE UPDATE OF name ON zones
            WHEN NEW.name = 'reject-me'
            BEGIN
                SELECT RAISE(ABORT, 'injected import constraint');
            END
        """)

    result = test_db.bulk_upsert_zones(
        [
            {"id": first["id"], "name": "First mutated"},
            {"id": second["id"], "name": "reject-me"},
        ]
    )

    assert result == {
        "success": False,
        "created": 0,
        "updated": 0,
        "failed": 2,
        "rolled_back": True,
        "errors": [{"index": 1, "id": second["id"], "code": "constraint_error"}],
    }
    assert test_db.get_zone(first["id"])["name"] == "First before"
    assert test_db.get_zone(second["id"])["name"] == "Second before"


@pytest.mark.parametrize("bad_id", [None, "", "01", "1.0", 1.0, True, 0, -1, 2_147_483_648])
def test_bulk_zone_import_rejects_explicit_noncanonical_id_and_rolls_back(test_db, bad_id):
    existing = test_db.create_zone({"name": "Before", "duration": 10, "group_id": 1})

    result = test_db.bulk_upsert_zones(
        [
            {"id": existing["id"], "name": "Mutated"},
            {"id": bad_id, "name": "Must not be auto-created"},
        ]
    )

    assert result == {
        "success": False,
        "created": 0,
        "updated": 0,
        "failed": 2,
        "rolled_back": True,
        "errors": [{"index": 1, "id": bad_id, "code": "invalid_data"}],
    }
    assert test_db.get_zone(existing["id"])["name"] == "Before"
    assert len(test_db.get_zones()) == 1


@pytest.mark.parametrize("bad_id", [None, "", "01", 1.0, True, 0, -1, 2_147_483_648])
def test_create_zone_rejects_invalid_explicit_id_without_poisoning_sequence(test_db, bad_id):
    assert test_db.create_zone({"id": bad_id, "name": "Invalid explicit ID"}) is None

    created = test_db.create_zone({"name": "Normal allocation"})

    assert created is not None
    assert created["id"] < 100


@pytest.mark.parametrize(
    ("table", "sql"),
    [
        ("zones", "INSERT INTO zones(id, name) VALUES (?, 'bad')"),
        ("groups", "INSERT INTO groups(id, name) VALUES (?, 'bad')"),
        ("mqtt_servers", "INSERT INTO mqtt_servers(id, name, host) VALUES (?, 'bad', 'host')"),
        ("programs", "INSERT INTO programs(id, name, time, days, zones) VALUES (?, 'bad', '06:00', '[]', '[]')"),
    ],
)
@pytest.mark.parametrize("bad_id", [0, -1, 2_147_483_648])
def test_entity_tables_reject_out_of_range_explicit_ids(test_db, table, sql, bad_id):
    with sqlite3.connect(test_db.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="entity identifier out of range"):
            conn.execute(sql, (bad_id,))
        sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()

    assert sequence is None or sequence[0] < 2_147_483_648


def test_all_mqtt_reference_writes_reject_missing_server(test_db):
    zone = test_db.create_zone({"name": "No broker yet"})

    assert test_db.update_zone(zone["id"], {"mqtt_server_id": 999_999}) is None
    assert test_db.update_group_fields(1, {"master_mqtt_server_id": 999_999}) is False
    assert test_db.set_setting_value("rain.server_id", "999999") is False
    assert test_db.get_zone(zone["id"])["mqtt_server_id"] is None
    assert test_db.get_setting_value("rain.server_id") is None

    with sqlite3.connect(test_db.db_path) as conn:
        group_ref = conn.execute("SELECT master_mqtt_server_id FROM groups WHERE id = 1").fetchone()
    assert group_ref == (None,)


def test_direct_mqtt_server_delete_is_restricted_by_database_trigger(test_db):
    server = test_db.create_mqtt_server({"name": "Referenced", "host": "broker", "port": 1883})
    zone = test_db.create_zone({"name": "Hardware", "mqtt_server_id": server["id"]})

    with sqlite3.connect(test_db.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="mqtt server is referenced"):
            conn.execute("DELETE FROM mqtt_servers WHERE id = ?", (server["id"],))

    assert test_db.get_zone(zone["id"])["mqtt_server_id"] == server["id"]
    assert test_db.get_mqtt_server(server["id"]) is not None


def test_delete_first_update_second_race_cannot_commit_dangling_mqtt_reference(test_db):
    zone = test_db.create_zone({"name": "Raced zone"})
    server = test_db.create_mqtt_server({"name": "Raced", "host": "broker", "port": 1883})
    delete_conn = sqlite3.connect(test_db.db_path, timeout=5, check_same_thread=False)
    delete_conn.execute("PRAGMA busy_timeout=5000")
    delete_conn.execute("BEGIN IMMEDIATE")
    delete_conn.execute("DELETE FROM mqtt_servers WHERE id = ?", (server["id"],))

    started = threading.Event()
    result: list[object] = []

    def update_after_delete_started():
        started.set()
        result.append(test_db.update_zone(zone["id"], {"mqtt_server_id": server["id"]}))

    thread = threading.Thread(target=update_after_delete_started)
    thread.start()
    assert started.wait(1)
    delete_conn.commit()
    delete_conn.close()
    thread.join(5)

    assert not thread.is_alive()
    assert result == [None]
    assert test_db.get_mqtt_server(server["id"]) is None
    assert test_db.get_zone(zone["id"])["mqtt_server_id"] is None


def test_group_storage_snapshot_restores_delete_and_cas_guards_update(test_db):
    group = test_db.create_group("Before")
    before = test_db.get_group_storage_snapshot(group["id"])
    assert before is not None

    assert test_db.update_group(group["id"], "Committed") is True
    committed = test_db.get_group_storage_snapshot(group["id"])
    assert committed is not None
    assert test_db.update_group(group["id"], "Concurrent") is True

    assert test_db.restore_group_snapshot(before, committed) is False
    assert test_db.get_group_storage_snapshot(group["id"])["name"] == "Concurrent"

    concurrent = test_db.get_group_storage_snapshot(group["id"])
    assert test_db.restore_group_snapshot(before, concurrent) is True
    assert test_db.get_group_storage_snapshot(group["id"]) == before

    assert test_db.delete_group(group["id"]) is True
    assert test_db.get_group_storage_snapshot(group["id"]) is None
    assert test_db.restore_group_snapshot(before) is True
    assert test_db.get_group_storage_snapshot(group["id"]) == before
    with sqlite3.connect(test_db.db_path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM retired_entity_ids WHERE entity = 'group' AND id = ?",
                (group["id"],),
            ).fetchone()
            is None
        )


def test_group_created_rollback_deletes_only_unchanged_row(test_db):
    group = test_db.create_group("Created")
    created = test_db.get_group_storage_snapshot(group["id"])
    assert test_db.update_group(group["id"], "Concurrent") is True

    assert test_db.delete_group_if_unchanged(created) is False
    concurrent = test_db.get_group_storage_snapshot(group["id"])
    assert test_db.delete_group_if_unchanged(concurrent) is True
    assert test_db.get_group_storage_snapshot(group["id"]) is None


def test_mqtt_storage_snapshot_restores_encrypted_secret_and_exact_identity(test_db):
    server = test_db.create_mqtt_server(
        {"name": "Before", "host": "old-broker", "port": 1883, "password": "old-secret"}
    )
    before = test_db.get_mqtt_server_storage_snapshot(server["id"])
    assert before is not None
    assert before["password"].startswith("ENC:")

    assert test_db.update_mqtt_server(
        server["id"], {"name": "Committed", "host": "new-broker", "password": "new-secret"}
    )
    committed = test_db.get_mqtt_server_storage_snapshot(server["id"])
    assert test_db.restore_mqtt_server_snapshot(before, committed) is True
    assert test_db.get_mqtt_server_storage_snapshot(server["id"]) == before
    assert test_db.get_mqtt_server(server["id"])["password"] == "old-secret"

    assert test_db.delete_mqtt_server(server["id"]) is True
    assert test_db.restore_mqtt_server_snapshot(before) is True
    assert test_db.get_mqtt_server_storage_snapshot(server["id"]) == before
    with sqlite3.connect(test_db.db_path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM retired_entity_ids WHERE entity = 'mqtt_server' AND id = ?",
                (server["id"],),
            ).fetchone()
            is None
        )


def test_mqtt_created_rollback_respects_cas_and_references(test_db):
    server = test_db.create_mqtt_server({"name": "Created", "host": "broker", "port": 1883})
    created = test_db.get_mqtt_server_storage_snapshot(server["id"])
    assert test_db.update_mqtt_server(server["id"], {"name": "Concurrent"}) is True

    assert test_db.delete_mqtt_server_if_unchanged(created) is False
    concurrent = test_db.get_mqtt_server_storage_snapshot(server["id"])
    zone = test_db.create_zone({"name": "Reference", "mqtt_server_id": server["id"]})
    assert test_db.delete_mqtt_server_if_unchanged(concurrent) is False
    assert test_db.delete_zone(zone["id"]) is True
    assert test_db.delete_mqtt_server_if_unchanged(concurrent) is True
    assert test_db.get_mqtt_server_storage_snapshot(server["id"]) is None


def test_backup_uses_wal_consistent_vacuum_fallback(test_db, tmp_path, monkeypatch):
    backup_dir = tmp_path / "backups"
    test_db.logs.backup_dir = str(backup_dir)
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute("CREATE TABLE backup_probe(value TEXT NOT NULL)")
        conn.execute("INSERT INTO backup_probe VALUES ('committed-in-wal')")
        conn.commit()

    fallback_called = False

    def fail_backup_api(_source: str, _target: str) -> None:
        raise sqlite3.OperationalError("backup API unavailable")

    def vacuum_fallback(source: str, target: str) -> None:
        nonlocal fallback_called
        fallback_called = True
        with sqlite3.connect(source) as conn:
            conn.execute("VACUUM INTO ?", (target,))

    monkeypatch.setattr(test_db.logs, "_backup_via_api", fail_backup_api, raising=False)
    monkeypatch.setattr(test_db.logs, "_backup_via_vacuum", vacuum_fallback, raising=False)

    result = test_db.create_backup()

    assert fallback_called is True
    assert result is not None
    with sqlite3.connect(result) as backup:
        assert backup.execute("SELECT value FROM backup_probe").fetchone() == ("committed-in-wal",)
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert not Path(result + ".partial-wal").exists()
    assert not Path(result + ".partial-shm").exists()


def test_vacuum_backup_accepts_valid_compaction_far_below_source_size(test_db, tmp_path, monkeypatch):
    backup_dir = tmp_path / "compact-backups"
    test_db.logs.backup_dir = str(backup_dir)
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("CREATE TABLE backup_bloat(payload BLOB NOT NULL)")
        conn.execute("INSERT INTO backup_bloat(payload) VALUES (zeroblob(5 * 1024 * 1024))")
        conn.commit()
        conn.execute("DELETE FROM backup_bloat")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    source_size = Path(test_db.db_path).stat().st_size

    def force_vacuum(_source: str, _target: str) -> None:
        raise sqlite3.OperationalError("use compacting fallback")

    monkeypatch.setattr(test_db.logs, "_backup_via_api", force_vacuum)
    result = test_db.create_backup()

    assert result is not None
    assert Path(result).stat().st_size < source_size * 0.5
    with sqlite3.connect(result) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("SELECT COUNT(*) FROM backup_bloat").fetchone() == (0,)


@pytest.mark.parametrize("source_kind", ["missing", "empty", "corrupt", "wrong-schema"])
def test_invalid_backup_source_never_publishes_or_rotates_existing_backups(test_db, tmp_path, source_kind):
    backup_dir = tmp_path / "existing-backups"
    backup_dir.mkdir()
    existing = [backup_dir / f"irrigation_backup_2020010{index}_000000.db" for index in range(1, 9)]
    for path in existing:
        path.write_bytes(b"operator backup marker")

    source = tmp_path / f"{source_kind}.db"
    if source_kind == "empty":
        source.touch()
    elif source_kind == "corrupt":
        source.write_bytes(b"not sqlite")
    elif source_kind == "wrong-schema":
        with sqlite3.connect(source) as conn:
            conn.execute("CREATE TABLE unrelated(value TEXT)")

    test_db.logs.db_path = str(source)
    test_db.logs.backup_dir = str(backup_dir)

    assert test_db.create_backup() is None
    assert source.exists() is (source_kind != "missing")
    assert all(path.read_bytes() == b"operator backup marker" for path in existing)
    assert sorted(backup_dir.iterdir()) == sorted(existing)


def test_backup_reports_failure_when_all_consistent_snapshot_methods_fail(test_db, tmp_path, monkeypatch):
    backup_dir = tmp_path / "backups"
    test_db.logs.backup_dir = str(backup_dir)

    def fail(_source: str, _target: str) -> None:
        raise sqlite3.OperationalError("snapshot unavailable")

    monkeypatch.setattr(test_db.logs, "_backup_via_api", fail, raising=False)
    monkeypatch.setattr(test_db.logs, "_backup_via_vacuum", fail, raising=False)

    assert test_db.create_backup() is None
    if backup_dir.exists():
        assert list(Path(backup_dir).glob("*.partial")) == []
        assert list(Path(backup_dir).glob("irrigation_backup_*.db")) == []


def test_backup_permissions_are_private_even_with_permissive_umask(test_db, tmp_path, monkeypatch):
    backup_dir = tmp_path / "private-backups"
    test_db.logs.backup_dir = str(backup_dir)

    def fail_backup_api(_source: str, _target: str) -> None:
        raise sqlite3.OperationalError("force consistent fallback")

    monkeypatch.setattr(test_db.logs, "_backup_via_api", fail_backup_api)

    previous_umask = os.umask(0)
    try:
        result = test_db.create_backup()
    finally:
        os.umask(previous_umask)

    assert result is not None
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(Path(result).stat().st_mode) == 0o600
