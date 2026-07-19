"""Post-review regressions for durable DB identity and recovery contracts."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
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
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name IN ('zones', 'zone_runs')"
            ).fetchall()
        }
        triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }

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
            if trigger[0].startswith(("trg_zones_", "trg_mqtt_servers_", "trg_programs_")):
                conn.execute(f"DROP TRIGGER {trigger[0]}")
        for table in ("zones", "groups", "mqtt_servers", "programs"):
            _strip_autoincrement(conn, table)
        _remove_cancellation_fk(conn)
        conn.execute("DROP TABLE IF EXISTS retired_entity_ids")
        conn.execute(
            "DELETE FROM migrations WHERE name IN "
            "('durable_entity_ids_v1', 'program_cancellations_fk_v1', 'restore_runtime_indexes_v1')"
        )

        conn.execute(
            "INSERT INTO zones(id, name, duration, group_id, topic, mqtt_server_id) VALUES (5, 'Legacy', 10, 1, '/z', 88)"
        )
        conn.execute(
            "INSERT INTO programs(id, name, time, days, zones) VALUES (1, 'Legacy program', '06:00', '[]', '[77]')"
        )
        conn.execute(
            "INSERT INTO program_cancellations(program_id, run_date, group_id) VALUES (55, '2026-07-19', 1)"
        )
        conn.execute(
            "INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, status, source, confirmed) "
            "VALUES (77, 1200, '2026-07-18 06:00:00', '2026-07-18 06:10:00', 'ok', 'program', 1)"
        )
        conn.execute(
            "UPDATE groups SET master_mqtt_server_id = 88, float_mqtt_server_id = 88 WHERE id = 1"
        )
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('rain.server_id', '88')")
        conn.commit()

    upgraded = IrrigationDB(test_db_path)
    zone = upgraded.create_zone({"name": "After upgrade", "duration": 10, "group_id": 1})
    server = upgraded.create_mqtt_server({"name": "After upgrade", "host": "broker", "port": 1883})
    program = upgraded.create_program(_program_payload("After upgrade", []))
    group = upgraded.create_group("After upgrade")

    assert zone["id"] > 77
    assert server["id"] > 88
    assert program["id"] > 55
    assert group["id"] > 1200
    assert upgraded.get_program(1)["zones"] == [77]
    with sqlite3.connect(test_db_path) as conn:
        assert conn.execute("SELECT zone_id, group_id, status FROM zone_runs WHERE zone_id = 77").fetchone() == (
            77,
            1200,
            "ok",
        )
        assert conn.execute("SELECT 1 FROM retired_entity_ids WHERE entity = 'zone' AND id = 77").fetchone()
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
        global_plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM zone_runs ORDER BY start_utc DESC"
        ).fetchall()
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
