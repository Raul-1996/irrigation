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
from db.identity import MAX_ENTITY_ID
from db.migrations import MigrationRunner
from db.schema import APPLICATION_ID, USER_VERSION


def _program_payload(name: str, zones: list[int]) -> dict[str, object]:
    return {
        "name": name,
        "time": "06:00",
        "days": [0, 1, 2, 3, 4, 5, 6],
        "zones": zones,
    }


def _sqlite_artifact_snapshot(path: Path) -> dict[str, bytes | None]:
    return {
        suffix: candidate.read_bytes() if candidate.exists() else None
        for suffix in ("", "-wal", "-shm", "-journal")
        for candidate in (Path(f"{path}{suffix}"),)
    }


def _create_legacy_backup_schema(
    path: Path,
    *,
    group_name_unique: bool = True,
    zone_name_declaration: str = "TEXT NOT NULL",
    program_name_declaration: str = "TEXT NOT NULL",
    settings_key_declaration: str = "TEXT PRIMARY KEY",
    include_postpone_reason: bool = True,
) -> None:
    """Create the exact 24941f7 tracked-schema floor with controlled defects."""

    unique = " UNIQUE" if group_name_unique else ""
    postpone_reason_column = "postpone_reason TEXT," if include_postpone_reason else ""
    with sqlite3.connect(path) as conn:
        conn.executescript(f"""
            CREATE TABLE zones (
                id INTEGER PRIMARY KEY,
                state TEXT DEFAULT 'off',
                name {zone_name_declaration},
                icon TEXT DEFAULT '🌿',
                duration INTEGER DEFAULT 10,
                group_id INTEGER DEFAULT 1,
                topic TEXT,
                postpone_until TEXT,
                {postpone_reason_column}
                photo_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                watering_start_time TEXT,
                scheduled_start_time TEXT,
                last_watering_time TEXT,
                mqtt_server_id INTEGER,
                watering_start_source TEXT
            );
            CREATE TABLE groups (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL{unique},
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                use_rain_sensor INTEGER DEFAULT 0
            );
            CREATE TABLE programs (
                id INTEGER PRIMARY KEY,
                name {program_name_declaration},
                time TEXT NOT NULL,
                days TEXT NOT NULL,
                zones TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE settings (key {settings_key_declaration}, value TEXT);
            CREATE TABLE migrations (
                name TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE mqtt_servers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER DEFAULT 1883,
                username TEXT,
                password TEXT,
                client_id TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tls_enabled INTEGER DEFAULT 0,
                tls_ca_path TEXT,
                tls_cert_path TEXT,
                tls_key_path TEXT,
                tls_insecure INTEGER DEFAULT 0,
                tls_version TEXT
            );
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                details TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE water_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_id INTEGER,
                liters REAL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE program_cancellations (
                program_id INTEGER NOT NULL,
                run_date TEXT NOT NULL,
                group_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (program_id, run_date, group_id)
            );
            CREATE INDEX idx_zones_group ON zones(group_id);
            CREATE INDEX idx_zones_mqtt_server ON zones(mqtt_server_id);
            CREATE INDEX idx_zones_topic ON zones(topic);
            CREATE INDEX idx_logs_type ON logs(type);
            CREATE INDEX idx_logs_timestamp ON logs(timestamp);
            CREATE INDEX idx_water_zone ON water_usage(zone_id);
            CREATE INDEX idx_water_timestamp ON water_usage(timestamp);
        """)
        if not include_postpone_reason:
            conn.execute("ALTER TABLE zones ADD COLUMN postpone_reason TEXT")
        markers = [
            "days_format",
            "zones_add_postpone_reason",
            "zones_add_watering_start_time",
            "zones_add_scheduled_start_time",
            "zones_add_last_watering_time",
            "create_mqtt_servers",
            "zones_add_mqtt_server_id",
            "ensure_group_999",
            "zones_add_indexes",
            "groups_add_use_rain",
            "zones_add_watering_start_source",
            "mqtt_add_tls_options",
        ]
        conn.executemany(
            "INSERT INTO migrations(name) VALUES (?)",
            [(marker,) for marker in markers],
        )


def _create_pre_named_migrations_schema(path: Path) -> None:
    """Create the exact core shape used before the migrations table existed."""

    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE zones (
                id INTEGER PRIMARY KEY,
                state TEXT DEFAULT 'off',
                name TEXT NOT NULL,
                icon TEXT DEFAULT '🌿',
                duration INTEGER DEFAULT 10,
                group_id INTEGER DEFAULT 1,
                topic TEXT,
                postpone_until TEXT,
                postpone_reason TEXT,
                photo_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                watering_start_time TEXT,
                scheduled_start_time TEXT,
                last_watering_time TEXT,
                mqtt_server_id INTEGER
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE groups (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE programs (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                time TEXT NOT NULL,
                days TEXT NOT NULL,
                zones TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                details TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE water_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_id INTEGER,
                liters REAL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE mqtt_servers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER DEFAULT 1883,
                username TEXT,
                password TEXT,
                client_id TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX idx_zones_group ON zones(group_id);
            CREATE INDEX idx_logs_type ON logs(type);
            CREATE INDEX idx_logs_timestamp ON logs(timestamp);
            CREATE INDEX idx_water_zone ON water_usage(zone_id);
            CREATE INDEX idx_water_timestamp ON water_usage(timestamp);
            INSERT INTO groups(id, name) VALUES (1, 'Насос-1');
            INSERT INTO groups(id, name) VALUES (999, 'БЕЗ ПОЛИВА');
        """)


_PRE_NAMED_MIGRATION_STAGES = (
    "ea4da158",
    "6656d668",
    "03b7dc41",
    "7b87622e",
    "b113c94a",
    "6249f525",
    "50e3b5d9",
    "f1c6f8a1",
    "90f0ef67",
    "01d75961",
)


def _create_pre_named_migration_checkpoint(path: Path, stage: str) -> None:
    """Create each exact semantic stage via the historical ALTER sequence."""

    stage_index = _PRE_NAMED_MIGRATION_STAGES.index(stage)
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE zones (
                id INTEGER PRIMARY KEY,
                state TEXT DEFAULT 'off',
                name TEXT NOT NULL,
                icon TEXT DEFAULT '🌿',
                duration INTEGER DEFAULT 10,
                group_id INTEGER DEFAULT 1,
                topic TEXT,
                postpone_until TEXT,
                photo_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE groups (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE programs (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                time TEXT NOT NULL,
                days TEXT NOT NULL,
                zones TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                details TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE water_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_id INTEGER,
                liters REAL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX idx_zones_group ON zones(group_id);
            CREATE INDEX idx_logs_type ON logs(type);
            CREATE INDEX idx_logs_timestamp ON logs(timestamp);
            CREATE INDEX idx_water_zone ON water_usage(zone_id);
            CREATE INDEX idx_water_timestamp ON water_usage(timestamp);
        """)
        if stage_index >= _PRE_NAMED_MIGRATION_STAGES.index("6656d668"):
            conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        if stage_index >= _PRE_NAMED_MIGRATION_STAGES.index("03b7dc41"):
            conn.execute("ALTER TABLE zones ADD COLUMN postpone_reason TEXT")
            conn.execute("ALTER TABLE zones ADD COLUMN watering_start_time TEXT")
        if stage_index >= _PRE_NAMED_MIGRATION_STAGES.index("7b87622e"):
            conn.execute("ALTER TABLE zones ADD COLUMN scheduled_start_time TEXT")
            conn.execute("ALTER TABLE zones ADD COLUMN last_watering_time TEXT")
        if stage_index >= _PRE_NAMED_MIGRATION_STAGES.index("b113c94a"):
            conn.executescript("""
                CREATE TABLE mqtt_servers (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER DEFAULT 1883,
                    username TEXT,
                    password TEXT,
                    client_id TEXT,
                    enabled INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                ALTER TABLE zones ADD COLUMN mqtt_server_id INTEGER;
            """)
        if stage_index >= _PRE_NAMED_MIGRATION_STAGES.index("6249f525"):
            conn.execute("CREATE INDEX idx_zones_mqtt_server ON zones(mqtt_server_id)")
            conn.execute("CREATE INDEX idx_zones_topic ON zones(topic)")
        if stage_index >= _PRE_NAMED_MIGRATION_STAGES.index("50e3b5d9"):
            conn.execute("ALTER TABLE groups ADD COLUMN use_rain_sensor INTEGER DEFAULT 0")
        if stage_index >= _PRE_NAMED_MIGRATION_STAGES.index("f1c6f8a1"):
            conn.execute("ALTER TABLE zones ADD COLUMN watering_start_source TEXT")
        if stage_index >= _PRE_NAMED_MIGRATION_STAGES.index("90f0ef67"):
            conn.executescript("""
                ALTER TABLE mqtt_servers ADD COLUMN tls_enabled INTEGER DEFAULT 0;
                ALTER TABLE mqtt_servers ADD COLUMN tls_ca_path TEXT;
                ALTER TABLE mqtt_servers ADD COLUMN tls_cert_path TEXT;
                ALTER TABLE mqtt_servers ADD COLUMN tls_key_path TEXT;
                ALTER TABLE mqtt_servers ADD COLUMN tls_insecure INTEGER DEFAULT 0;
                ALTER TABLE mqtt_servers ADD COLUMN tls_version TEXT;
            """)
        if stage_index >= _PRE_NAMED_MIGRATION_STAGES.index("01d75961"):
            conn.execute("""
                CREATE TABLE program_cancellations (
                    program_id INTEGER NOT NULL,
                    run_date TEXT NOT NULL,
                    group_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (program_id, run_date, group_id)
                )
            """)
        conn.executemany(
            "INSERT INTO groups(id, name) VALUES (?, ?)",
            [(1, "Насос-1"), (999, "БЕЗ ПОЛИВА")],
        )


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


@pytest.mark.parametrize(
    ("application_id", "user_version", "error"),
    [
        (0x10203040, 0, "unsupported application_id"),
        (APPLICATION_ID, USER_VERSION + 1, "unsupported application user_version"),
    ],
)
def test_schema_stamp_preflight_rejects_foreign_or_future_database_without_writes(
    test_db,
    tmp_path,
    application_id,
    user_version,
    error,
):
    source = tmp_path / f"rejected-{application_id}-{user_version}.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute(f"PRAGMA application_id = {application_id}")
        conn.execute(f"PRAGMA user_version = {user_version}")

    before = source.read_bytes()
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()

    with pytest.raises(sqlite3.DatabaseError, match=error):
        MigrationRunner(str(source)).init_database()

    assert source.read_bytes() == before
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()


def test_schema_stamp_preflight_rejects_unstamped_foreign_schema_without_writes(tmp_path):
    source = tmp_path / "unstamped-foreign.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY, value TEXT)")

    before = source.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="not a recognizable irrigation schema"):
        MigrationRunner(str(source)).init_database()

    assert source.read_bytes() == before


def test_schema_preflight_accepts_exact_pre_named_migrations_history_without_writes(tmp_path):
    source = tmp_path / "pre-named-migrations.db"
    _create_pre_named_migrations_schema(source)
    before = source.read_bytes()
    runner = MigrationRunner(str(source))

    runner._validate_schema_file_preflight()

    assert source.read_bytes() == before
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()

    IrrigationDB(str(source))
    from db.logs import LogRepository

    LogRepository.validate_application_database(str(source))


@pytest.mark.parametrize("stage", _PRE_NAMED_MIGRATION_STAGES)
def test_schema_preflight_accepts_every_first_parent_pre_named_stage_without_writes(tmp_path, stage):
    source = tmp_path / f"pre-named-{stage}.db"
    _create_pre_named_migration_checkpoint(source, stage)
    before = source.read_bytes()
    runner = MigrationRunner(str(source))

    runner._validate_schema_file_preflight()

    assert source.read_bytes() == before
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()

    IrrigationDB(str(source))
    from db.logs import LogRepository

    LogRepository.validate_application_database(str(source))


def test_pre_named_upgrade_rejects_out_of_range_live_id_with_actionable_error(tmp_path):
    source = tmp_path / "pre-named-out-of-range-id.db"
    _create_pre_named_migration_checkpoint(source, "b113c94a")
    with sqlite3.connect(source) as conn:
        conn.execute(
            "INSERT INTO zones(id, name, group_id) VALUES (?, 'Unsafe historical ID', 1)",
            (MAX_ENTITY_ID + 1,),
        )

    with pytest.raises(sqlite3.DatabaseError, match=r"out-of-range durable identifier.*remap live identifiers"):
        MigrationRunner(str(source)).init_database()


def test_pre_named_upgrade_rejects_arbitrary_dangling_group_with_actionable_error(tmp_path):
    source = tmp_path / "pre-named-dangling-group.db"
    _create_pre_named_migration_checkpoint(source, "b113c94a")
    with sqlite3.connect(source) as conn:
        conn.execute("INSERT INTO zones(name, group_id) VALUES ('Unsafe historical group', 4242)")

    with pytest.raises(RuntimeError, match=r"dangling group references.*explicitly remap"):
        MigrationRunner(str(source)).init_database()


def test_schema_preflight_rejects_malformed_pre_named_shape_without_writes(tmp_path):
    source = tmp_path / "malformed-pre-named-migrations.db"
    with sqlite3.connect(source) as conn:
        conn.executescript("""
            CREATE TABLE zones (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE programs (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                time TEXT NOT NULL,
                days TEXT NOT NULL,
                zones TEXT NOT NULL
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE foreign_payload(secret TEXT NOT NULL);
            INSERT INTO foreign_payload(secret) VALUES ('preserve me');
        """)

    before = source.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="recognized historical schema"):
        MigrationRunner(str(source)).init_database()

    assert source.read_bytes() == before
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()


def test_schema_preflight_rejects_extra_table_in_exact_pre_named_history_without_writes(tmp_path):
    source = tmp_path / "foreign-pre-named-migrations.db"
    _create_pre_named_migrations_schema(source)
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE foreign_payload(secret TEXT NOT NULL)")
        conn.execute("INSERT INTO foreign_payload(secret) VALUES ('preserve me')")

    before = source.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="recognized historical schema"):
        MigrationRunner(str(source)).init_database()

    assert source.read_bytes() == before
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()


def test_schema_preflight_rejects_minimal_tracked_decoy_without_writes(tmp_path):
    source = tmp_path / "minimal-tracked-decoy.db"
    with sqlite3.connect(source) as conn:
        conn.executescript("""
            CREATE TABLE zones (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
            CREATE TABLE programs (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                time TEXT NOT NULL,
                days TEXT NOT NULL,
                zones TEXT NOT NULL
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE migrations (
                name TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE foreign_payload(secret TEXT NOT NULL);
            INSERT INTO migrations(name) VALUES ('days_format');
            INSERT INTO foreign_payload(secret) VALUES ('preserve me');
        """)

    before = source.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="tracked irrigation schema rejected before migration"):
        MigrationRunner(str(source)).init_database()

    assert source.read_bytes() == before
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()
    with sqlite3.connect(source) as conn:
        assert conn.execute("SELECT secret FROM foreign_payload").fetchone() == ("preserve me",)
        assert conn.execute("PRAGMA application_id").fetchone() == (0,)
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)


def test_schema_preflight_rejects_extra_table_on_exact_tracked_floor_without_writes(tmp_path):
    source = tmp_path / "foreign-exact-tracked-floor.db"
    _create_legacy_backup_schema(source)
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE foreign_payload(secret TEXT NOT NULL)")
        conn.execute("INSERT INTO foreign_payload(secret) VALUES ('preserve me')")

    before = source.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match=r"tracked irrigation schema.*unexpected=.*foreign_payload"):
        MigrationRunner(str(source)).init_database()

    assert source.read_bytes() == before
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()


@pytest.mark.parametrize("corruption", ["missing_base_column", "foreign_table"])
def test_schema_preflight_rejects_corrupt_stamped_tracked_schema_without_writes(
    test_db,
    tmp_path,
    corruption,
):
    source = tmp_path / f"corrupt-stamped-{corruption}.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        assert conn.execute("PRAGMA journal_mode = DELETE").fetchone() == ("delete",)
        if corruption == "missing_base_column":
            conn.execute("ALTER TABLE programs DROP COLUMN created_at")
        else:
            conn.execute("CREATE TABLE foreign_payload(secret TEXT NOT NULL)")
            conn.execute("INSERT INTO foreign_payload(secret) VALUES ('preserve me')")

    before = source.read_bytes()
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()

    with pytest.raises(sqlite3.DatabaseError, match="tracked irrigation schema rejected before migration"):
        MigrationRunner(str(source)).init_database()

    assert source.read_bytes() == before
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()


def test_schema_preflight_rejects_sqlitex_hidden_trigger_before_reserved_group_repair(test_db, tmp_path):
    source = tmp_path / "hidden-trigger-stamped.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        assert conn.execute("PRAGMA journal_mode = DELETE").fetchone() == ("delete",)
        conn.execute("INSERT INTO zones(name, group_id) VALUES ('Keeps bootstrap from reseeding', 1)")
        conn.execute("INSERT INTO programs(name, time, days, zones) VALUES ('preserve me', '06:00', '[0]', '[1]')")
        reserved_guard_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'trg_groups_restrict_reserved_delete'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER trg_groups_restrict_reserved_delete")
        conn.execute("DELETE FROM groups WHERE id = 999")
        conn.execute(reserved_guard_sql)
        assert conn.execute("SELECT 1 FROM retired_entity_ids WHERE entity = 'group' AND id = 999").fetchone() == (1,)
        conn.execute("""
            CREATE TRIGGER sqliteXevil
            AFTER DELETE ON retired_entity_ids
            BEGIN
                DELETE FROM programs;
            END
        """)

    before = _sqlite_artifact_snapshot(source)

    with pytest.raises(sqlite3.DatabaseError, match=r"unexpected triggers.*sqliteXevil"):
        MigrationRunner(str(source)).init_database()

    assert _sqlite_artifact_snapshot(source) == before
    with sqlite3.connect(source) as conn:
        assert conn.execute("SELECT name FROM programs").fetchall() == [("preserve me",)]
        assert conn.execute("SELECT 1 FROM groups WHERE id = 999").fetchone() is None


def test_schema_preflight_accepts_markerless_completed_drop_and_init_repairs_marker(test_db, tmp_path):
    source = tmp_path / "markerless-completed-drop.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        assert conn.execute("PRAGMA journal_mode = DELETE").fetchone() == ("delete",)
        conn.execute("DELETE FROM migrations WHERE name = 'zones_drop_last_watering_time'")
        assert "last_watering_time" not in {str(row[1]) for row in conn.execute("PRAGMA table_info(zones)").fetchall()}

    runner = MigrationRunner(str(source))
    before = _sqlite_artifact_snapshot(source)
    runner._validate_schema_file_preflight()
    assert _sqlite_artifact_snapshot(source) == before

    runner.init_database()
    with sqlite3.connect(source) as conn:
        assert conn.execute("SELECT 1 FROM migrations WHERE name = 'zones_drop_last_watering_time'").fetchone() == (1,)


def test_schema_preflight_reads_marker_from_wal_without_touching_original(test_db, tmp_path):
    source = tmp_path / "marker-only-in-wal.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        assert conn.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute("DELETE FROM migrations WHERE name = 'zones_drop_last_watering_time'")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        conn.execute("INSERT INTO migrations(name) VALUES ('zones_drop_last_watering_time')")
        conn.commit()
        assert Path(f"{source}-wal").stat().st_size > 0
        assert conn.execute("SELECT 1 FROM migrations WHERE name = 'zones_drop_last_watering_time'").fetchone() == (1,)

        before = _sqlite_artifact_snapshot(source)
        MigrationRunner(str(source))._validate_schema_file_preflight()
        assert _sqlite_artifact_snapshot(source) == before


@pytest.mark.parametrize(
    ("corruption", "error"),
    [
        ("future_user_version", "unsupported application user_version"),
        ("foreign_table", r"tracked irrigation schema.*foreign_payload"),
    ],
)
def test_schema_preflight_rejects_wal_only_corruption_without_creating_shm(
    test_db,
    tmp_path,
    corruption,
    error,
):
    source = tmp_path / f"wal-only-{corruption}.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        assert conn.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if corruption == "future_user_version":
            conn.execute(f"PRAGMA user_version = {USER_VERSION + 1}")
        else:
            conn.execute("CREATE TABLE foreign_payload(secret TEXT NOT NULL)")
        conn.commit()
        assert Path(f"{source}-wal").stat().st_size > 0
        Path(f"{source}-shm").unlink()

        before = _sqlite_artifact_snapshot(source)
        assert before["-shm"] is None
        with pytest.raises(sqlite3.DatabaseError, match=error):
            MigrationRunner(str(source)).init_database()
        assert _sqlite_artifact_snapshot(source) == before


def test_schema_preflight_accepts_unstamped_current_tracked_superset_without_writes(test_db, tmp_path):
    source = tmp_path / "unstamped-current-tracked.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        assert conn.execute("PRAGMA journal_mode = DELETE").fetchone() == ("delete",)
        conn.execute("PRAGMA application_id = 0")
        conn.execute("PRAGMA user_version = 0")
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()
    before = source.read_bytes()

    MigrationRunner(str(source))._validate_schema_file_preflight()

    assert source.read_bytes() == before
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()


def test_tracked_post_drop_history_restores_known_lost_group_index(tmp_path):
    source = tmp_path / "tracked-post-drop-index-loss.db"
    IrrigationDB(str(source))
    with sqlite3.connect(source) as conn:
        conn.execute("DROP INDEX idx_zones_group")
        conn.execute("PRAGMA application_id = 0")
        conn.execute("PRAGMA user_version = 0")
    before = source.read_bytes()
    runner = MigrationRunner(str(source))

    runner._validate_schema_file_preflight()

    assert source.read_bytes() == before
    runner.init_database()
    with sqlite3.connect(source) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_zones_group'"
        ).fetchone() == (1,)


def test_schema_stamp_preflight_rejects_stamped_decoy_without_writes(tmp_path):
    source = tmp_path / "stamped-foreign.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE foreign_payload(secret TEXT NOT NULL)")
        conn.execute("INSERT INTO foreign_payload(secret) VALUES ('preserve me')")
        conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        conn.execute(f"PRAGMA user_version = {USER_VERSION}")

    before = source.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="not a recognizable irrigation schema"):
        MigrationRunner(str(source)).init_database()

    assert source.read_bytes() == before
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()


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
        "trg_zones_reject_explicit_id_boundary",
        "trg_groups_reject_explicit_id_boundary",
        "trg_mqtt_servers_reject_explicit_id_boundary",
        "trg_programs_reject_explicit_id_boundary",
        "trg_zones_reject_id_update",
        "trg_groups_reject_id_update",
        "trg_mqtt_servers_reject_id_update",
        "trg_programs_reject_id_update",
        "trg_zones_mqtt_server_insert",
        "trg_zones_mqtt_server_update",
        "trg_groups_mqtt_server_insert",
        "trg_groups_mqtt_server_update",
        "trg_settings_mqtt_server_insert",
        "trg_settings_mqtt_server_update",
        "trg_mqtt_servers_restrict_referenced_delete",
        "trg_zones_group_insert",
        "trg_zones_group_update",
        "trg_groups_restrict_referenced_delete",
        "trg_groups_reject_replacing_name",
        "trg_groups_reject_replacing_name_update",
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


def test_forward_zone_rebuild_preserves_deleted_autoincrement_high_watermark(test_db):
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("DROP TRIGGER trg_zones_retire_id")
        conn.execute("ALTER TABLE zones ADD COLUMN last_watering_time TEXT")
        conn.execute("INSERT INTO zones(id, name, group_id) VALUES (999, 'High live', 1)")
        conn.execute("INSERT INTO zones(id, name, group_id) VALUES (1000, 'High deleted', 1)")
        conn.execute("DELETE FROM zones WHERE id = 1000")
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'zones'").fetchone() == (1000,)
        assert conn.execute("SELECT 1 FROM retired_entity_ids WHERE entity = 'zone' AND id = 1000").fetchone() is None
        conn.execute("DELETE FROM migrations WHERE name = 'zones_drop_last_watering_time'")

    migrated = IrrigationDB(test_db.db_path)
    created = migrated.create_zone({"name": "After forward rebuild", "group_id": 1})

    assert created is not None
    assert created["id"] == 1001


def test_forward_program_identity_rebuild_preserves_cancellations_with_foreign_keys(test_db):
    program = test_db.create_program(_program_payload("Preserved cancellation", []))
    assert test_db.cancel_program_run_for_group(program["id"], "2026-07-20", 1)

    with sqlite3.connect(test_db.db_path) as conn:
        before = conn.execute(
            "SELECT program_id, run_date, group_id, created_at FROM program_cancellations WHERE program_id = ?",
            (program["id"],),
        ).fetchone()
        _strip_autoincrement(conn, "programs")
        assert before is not None
        assert (
            conn.execute(
                "SELECT program_id, run_date, group_id, created_at FROM program_cancellations WHERE program_id = ?",
                (program["id"],),
            ).fetchone()
            == before
        )
        assert conn.execute("PRAGMA foreign_key_list(program_cancellations)").fetchall()

    IrrigationDB(test_db.db_path)

    with sqlite3.connect(test_db.db_path) as conn:
        program_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'programs'"
        ).fetchone()[0]
        assert "AUTOINCREMENT" in program_sql.upper()
        assert (
            conn.execute(
                "SELECT program_id, run_date, group_id, created_at FROM program_cancellations WHERE program_id = ?",
                (program["id"],),
            ).fetchone()
            == before
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_forward_durable_zone_rebuild_suspends_and_restores_cross_table_guards(test_db):
    cross_guard_names = (
        "trg_mqtt_servers_restrict_referenced_delete",
        "trg_groups_restrict_referenced_delete",
    )
    with sqlite3.connect(test_db.db_path) as conn:
        cross_guard_sql = [
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (name,),
            ).fetchone()[0]
            for name in cross_guard_names
        ]
        for name in cross_guard_names:
            conn.execute(f"DROP TRIGGER {name}")
        _strip_autoincrement(conn, "zones")
        for sql in cross_guard_sql:
            conn.execute(sql)
        zones_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'zones'").fetchone()[0]
        assert "AUTOINCREMENT" not in zones_sql.upper()
        assert {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()
        } >= set(cross_guard_names)

    IrrigationDB(test_db.db_path)

    with sqlite3.connect(test_db.db_path) as conn:
        zones_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'zones'").fetchone()[0]
        triggers = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()}
        assert "AUTOINCREMENT" in zones_sql.upper()
        assert set(cross_guard_names) <= triggers


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


@pytest.mark.parametrize("bad_id", [None, "", "01", "1.0", 1.0, True, 0, -1, MAX_ENTITY_ID, 2_147_483_648])
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


@pytest.mark.parametrize("bad_id", [None, "", "01", 1.0, True, 0, -1, MAX_ENTITY_ID, 2_147_483_648])
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


@pytest.mark.parametrize(
    ("table", "sql"),
    [
        ("zones", "INSERT INTO zones(id, name) VALUES (?, 'boundary')"),
        ("groups", "INSERT INTO groups(id, name) VALUES (?, 'boundary')"),
        ("mqtt_servers", "INSERT INTO mqtt_servers(id, name, host) VALUES (?, 'boundary', 'host')"),
        (
            "programs",
            "INSERT INTO programs(id, name, time, days, zones) VALUES (?, 'boundary', '06:00', '[]', '[]')",
        ),
    ],
)
def test_entity_boundary_rejection_preserves_next_auto_allocation(test_db, table, sql):
    with sqlite3.connect(test_db.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="entity identifier out of range"):
            conn.execute(sql, (MAX_ENTITY_ID,))

    if table == "zones":
        created = test_db.create_zone({"name": "Auto zone"})
    elif table == "groups":
        created = test_db.create_group("Auto group")
    elif table == "mqtt_servers":
        created = test_db.create_mqtt_server({"name": "Auto broker", "host": "localhost"})
    else:
        created = test_db.create_program(_program_payload("Auto program", []))

    assert created is not None
    assert 0 < created["id"] < MAX_ENTITY_ID


@pytest.mark.parametrize(
    ("table", "sql"),
    [
        ("zones", "INSERT INTO zones(id, name) VALUES (?, 'highest explicit')"),
        ("groups", "INSERT INTO groups(id, name) VALUES (?, 'highest explicit')"),
        ("mqtt_servers", "INSERT INTO mqtt_servers(id, name, host) VALUES (?, 'highest explicit', 'host')"),
        (
            "programs",
            "INSERT INTO programs(id, name, time, days, zones) VALUES (?, 'highest explicit', '06:00', '[]', '[]')",
        ),
    ],
)
def test_highest_explicit_entity_id_leaves_one_auto_allocation(test_db, table, sql):
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute(sql, (MAX_ENTITY_ID - 1,))
        conn.commit()

    if table == "zones":
        created = test_db.create_zone({"name": "Last auto zone"})
    elif table == "groups":
        created = test_db.create_group("Last auto group")
    elif table == "mqtt_servers":
        created = test_db.create_mqtt_server({"name": "Last auto broker", "host": "localhost"})
    else:
        created = test_db.create_program(_program_payload("Last auto program", []))

    assert created is not None
    assert created["id"] == MAX_ENTITY_ID


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


def test_direct_zone_group_writes_and_referenced_group_delete_are_guarded(test_db):
    referenced = test_db.create_group("Referenced group")
    replacement = test_db.create_group("Replacement candidate")

    with sqlite3.connect(test_db.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="missing group reference"):
            conn.execute("INSERT INTO zones(name, group_id) VALUES ('Missing group', 123456)")
        conn.execute("INSERT INTO zones(name, group_id) VALUES ('Legacy zero group', 0)")
        conn.execute("INSERT INTO zones(name, group_id) VALUES ('Legacy null group', NULL)")
        conn.execute(
            "INSERT INTO zones(name, group_id) VALUES ('Live group', ?)",
            (referenced["id"],),
        )
        with pytest.raises(sqlite3.IntegrityError, match="group is referenced"):
            conn.execute("DELETE FROM groups WHERE id = ?", (referenced["id"],))
        with pytest.raises(sqlite3.IntegrityError, match="group name conflict"):
            conn.execute(
                "INSERT OR REPLACE INTO groups(id, name) VALUES (?, ?)",
                (referenced["id"] + 10_000, referenced["name"]),
            )
        with pytest.raises(sqlite3.IntegrityError, match="group name conflict"):
            conn.execute(
                "UPDATE OR REPLACE groups SET name = ? WHERE id = ?",
                (referenced["name"], replacement["id"]),
            )
        with pytest.raises(sqlite3.IntegrityError, match="missing group reference"):
            conn.execute("UPDATE zones SET group_id = 123456 WHERE name = 'Legacy zero group'")


def test_forward_integrity_reconciliation_canonicalizes_settings_and_repairs_guards(test_db):
    server = test_db.create_mqtt_server({"name": "Canonical", "host": "broker", "port": 1883})

    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("DROP TRIGGER trg_settings_mqtt_server_insert")
        conn.execute("DROP TRIGGER trg_settings_mqtt_server_update")
        conn.execute("DROP TRIGGER trg_zones_group_update")
        conn.execute("DROP TRIGGER trg_groups_reject_id_update")
        conn.execute("DROP TRIGGER trg_mqtt_servers_reject_id_update")
        conn.execute("DROP TRIGGER trg_groups_reject_replacing_name")
        conn.execute("DROP TRIGGER trg_groups_reject_replacing_name_update")
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('rain.server_id', ?)",
            (f"0{server['id']}",),
        )

    IrrigationDB(test_db.db_path)

    with sqlite3.connect(test_db.db_path) as conn:
        assert conn.execute("SELECT value FROM settings WHERE key = 'rain.server_id'").fetchone() == (
            str(server["id"]),
        )
        trigger_names = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()
        }
        assert {
            "trg_settings_mqtt_server_insert",
            "trg_settings_mqtt_server_update",
            "trg_zones_group_update",
            "trg_groups_reject_id_update",
            "trg_mqtt_servers_reject_id_update",
            "trg_groups_reject_replacing_name",
            "trg_groups_reject_replacing_name_update",
        } <= trigger_names
        with pytest.raises(sqlite3.IntegrityError, match="mqtt server is referenced"):
            conn.execute("DELETE FROM mqtt_servers WHERE id = ?", (server["id"],))


def test_durable_entity_primary_keys_are_immutable_for_direct_writes(test_db):
    zone = test_db.create_zone({"name": "Immutable zone", "group_id": 1})
    group = test_db.create_group("Immutable group")
    server = test_db.create_mqtt_server({"name": "Immutable broker", "host": "broker"})
    program = test_db.create_program(_program_payload("Immutable program", []))
    rows = (
        ("zones", zone["id"]),
        ("groups", group["id"]),
        ("mqtt_servers", server["id"]),
        ("programs", program["id"]),
    )

    with sqlite3.connect(test_db.db_path) as conn:
        for table, row_id in rows:
            with pytest.raises(sqlite3.IntegrityError, match="entity identifier is immutable"):
                conn.execute(f"UPDATE {table} SET id = ? WHERE id = ?", (row_id + 10_000, row_id))


def test_trusted_group_and_mqtt_snapshot_restore_can_recover_final_auto_id(test_db):
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("INSERT INTO groups(id, name) VALUES (?, 'Group before final')", (MAX_ENTITY_ID - 1,))
        conn.execute(
            "INSERT INTO mqtt_servers(id, name, host) VALUES (?, 'Broker before final', 'broker')",
            (MAX_ENTITY_ID - 1,),
        )

    final_group = test_db.create_group("Final automatic group")
    final_server = test_db.create_mqtt_server({"name": "Final automatic broker", "host": "broker"})
    assert final_group["id"] == MAX_ENTITY_ID
    assert final_server["id"] == MAX_ENTITY_ID

    group_snapshot = test_db.get_group_storage_snapshot(MAX_ENTITY_ID)
    server_snapshot = test_db.get_mqtt_server_storage_snapshot(MAX_ENTITY_ID)
    assert group_snapshot is not None
    assert server_snapshot is not None
    assert test_db.delete_group(MAX_ENTITY_ID) is True
    assert test_db.delete_mqtt_server(MAX_ENTITY_ID) is True

    assert test_db.restore_group_snapshot(group_snapshot) is True
    assert test_db.restore_mqtt_server_snapshot(server_snapshot) is True
    assert test_db.get_group_storage_snapshot(MAX_ENTITY_ID) == group_snapshot
    assert test_db.get_mqtt_server_storage_snapshot(MAX_ENTITY_ID) == server_snapshot


@pytest.mark.parametrize("table", ["zones", "groups", "mqtt_servers", "programs"])
def test_forward_init_fails_fast_on_poisoned_durable_sequence(test_db, table):
    with sqlite3.connect(test_db.db_path) as conn:
        cursor = conn.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
            (MAX_ENTITY_ID + 100, table),
        )
        assert cursor.rowcount == 1

    with pytest.raises(sqlite3.DatabaseError, match=rf"sqlite_sequence for {table} exceeds"):
        MigrationRunner(test_db.db_path).init_database()


def test_forward_group_reconciliation_rejects_existing_dangling_reference(test_db, tmp_path):
    source = tmp_path / "dangling-zone-group-init.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        conn.execute("DROP TRIGGER trg_zones_group_insert")
        conn.execute("INSERT INTO zones(name, group_id) VALUES ('Dangling', 123456)")

    with pytest.raises(RuntimeError, match="dangling group references"):
        MigrationRunner(str(source)).init_database()


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


@pytest.mark.parametrize("group_id", [1, 999])
def test_reserved_groups_cannot_be_deleted_by_repository_or_direct_sql(test_db, group_id):
    snapshot = test_db.get_group_storage_snapshot(group_id)
    assert snapshot is not None
    assert test_db.delete_group(group_id) is False
    assert test_db.delete_group_if_unchanged(snapshot) is False

    with sqlite3.connect(test_db.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="reserved group cannot be deleted"):
            conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))


def test_forward_init_repairs_pre_guard_deleted_default_group(test_db, tmp_path):
    source = tmp_path / "deleted-default-group.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        conn.execute("INSERT INTO zones(name) VALUES ('Legacy default reference')")
        conn.execute("DROP TRIGGER trg_groups_restrict_reserved_delete")
        conn.execute("DROP TRIGGER trg_groups_restrict_referenced_delete")
        conn.execute("DELETE FROM groups WHERE id = 1")
        replacement_id = conn.execute("INSERT INTO groups(name) VALUES ('Насос-1')").lastrowid
        assert replacement_id != 1
        assert conn.execute("SELECT 1 FROM groups WHERE id = 1").fetchone() is None
        assert conn.execute("SELECT 1 FROM retired_entity_ids WHERE entity = 'group' AND id = 1").fetchone()

    MigrationRunner(str(source)).init_database()

    with sqlite3.connect(source) as conn:
        restored_name = conn.execute("SELECT name FROM groups WHERE id = 1").fetchone()[0]
        assert restored_name != "Насос-1"
        assert conn.execute("SELECT name FROM groups WHERE id = ?", (replacement_id,)).fetchone() == ("Насос-1",)
        assert conn.execute("SELECT 1 FROM retired_entity_ids WHERE entity = 'group' AND id = 1").fetchone() is None
        assert conn.execute("SELECT group_id FROM zones WHERE name = 'Legacy default reference'").fetchone() == (1,)
        zone_id = conn.execute("INSERT INTO zones(name) VALUES ('Default restored')").lastrowid
        assert conn.execute("SELECT group_id FROM zones WHERE id = ?", (zone_id,)).fetchone() == (1,)


def test_group_reconciliation_does_not_erase_live_retired_collision(test_db, tmp_path):
    source = tmp_path / "reserved-group-collision.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        conn.execute("INSERT INTO retired_entity_ids(entity, id) VALUES ('group', 1)")
        conn.execute("DELETE FROM migrations WHERE name = 'group_reference_integrity_v1'")

    with pytest.raises(sqlite3.DatabaseError, match=r"live group identifier 1.*also marked retired"):
        MigrationRunner(str(source)).init_database()

    with sqlite3.connect(source) as conn:
        assert conn.execute("SELECT 1 FROM retired_entity_ids WHERE entity = 'group' AND id = 1").fetchone()


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
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('backup_probe', 'committed-in-wal')")
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
        assert backup.execute("SELECT value FROM settings WHERE key = 'backup_probe'").fetchone() == (
            "committed-in-wal",
        )
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert not Path(result + ".partial-wal").exists()
    assert not Path(result + ".partial-shm").exists()


def test_vacuum_backup_accepts_valid_compaction_far_below_source_size(test_db, tmp_path, monkeypatch):
    backup_dir = tmp_path / "compact-backups"
    test_db.logs.backup_dir = str(backup_dir)
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("INSERT INTO logs(type, details) VALUES ('backup_bloat', zeroblob(5 * 1024 * 1024))")
        conn.commit()
        conn.execute("DELETE FROM logs WHERE type = 'backup_bloat'")
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
        assert backup.execute("SELECT COUNT(*) FROM logs WHERE type = 'backup_bloat'").fetchone() == (0,)


def test_backup_rejects_decoy_database_with_only_required_table_names(test_db, tmp_path):
    source = tmp_path / "decoy.db"
    with sqlite3.connect(source) as conn:
        for table in ("zones", "groups", "programs", "settings", "migrations", "mqtt_servers"):
            conn.execute(f'CREATE TABLE "{table}" (id INTEGER)')

    backup_dir = tmp_path / "decoy-backups"
    test_db.logs.db_path = str(source)
    test_db.logs.backup_dir = str(backup_dir)

    assert test_db.create_backup() is None
    assert not backup_dir.exists()


def test_backup_rejects_stamped_decoy_with_wrong_table_contract(tmp_path):
    from db.logs import LogRepository
    from db.schema import APPLICATION_ID, USER_VERSION

    source = tmp_path / "stamped-decoy.db"
    with sqlite3.connect(source) as conn:
        for table in LogRepository._REQUIRED_BACKUP_TABLES:
            conn.execute(f'CREATE TABLE "{table}" (id INTEGER)')
        conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        conn.execute(f"PRAGMA user_version = {USER_VERSION}")

    with pytest.raises(sqlite3.DatabaseError, match="missing columns"):
        LogRepository._validate_application_database(str(source))


@pytest.mark.parametrize(
    "missing_table",
    ["retired_entity_ids", "zone_runs", "program_cancellations", "program_queue_log", "float_events"],
)
def test_backup_schema_contract_rejects_missing_critical_auxiliary_table(test_db, tmp_path, missing_table):
    from db.logs import LogRepository

    source = tmp_path / f"missing-{missing_table}.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        conn.execute(f'DROP TABLE "{missing_table}"')

    with pytest.raises(sqlite3.DatabaseError, match="missing tables"):
        LogRepository._validate_application_database(str(source))


def test_initialized_stamped_database_with_missing_zone_runs_fails_fast(test_db, tmp_path):
    source = tmp_path / "stamped-missing-zone-runs.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        conn.execute("DROP TABLE zone_runs")

    with pytest.raises(sqlite3.DatabaseError, match="zone_runs"):
        IrrigationDB(str(source))


def test_backup_schema_contract_rejects_dangling_nonlegacy_zone_group(test_db, tmp_path):
    from db.logs import LogRepository

    source = tmp_path / "dangling-zone-group.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        conn.execute("DROP TRIGGER trg_zones_group_insert")
        conn.execute("INSERT INTO zones(name, group_id) VALUES ('dangling', 123456)")

    with pytest.raises(sqlite3.DatabaseError, match="dangling group references"):
        LogRepository._validate_application_database(str(source))


def test_backup_schema_contract_rejects_missing_zone_runs_runtime_index(test_db, tmp_path):
    from db.logs import LogRepository

    source = tmp_path / "missing-zone-runs-index.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        conn.execute("DROP INDEX idx_zone_runs_last_ok")

    with pytest.raises(sqlite3.DatabaseError, match="missing required index 'idx_zone_runs_last_ok'"):
        LogRepository._validate_application_database(str(source))


def test_backup_schema_contract_rejects_counterfeit_integrity_trigger(test_db, tmp_path):
    from db.logs import LogRepository

    source = tmp_path / "counterfeit-trigger.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        conn.execute("DROP TRIGGER trg_zones_mqtt_server_insert")
        conn.execute("CREATE TRIGGER trg_zones_mqtt_server_insert BEFORE INSERT ON zones BEGIN SELECT 1; END")

    with pytest.raises(sqlite3.DatabaseError, match="has an invalid definition"):
        LogRepository._validate_application_database(str(source))


def test_backup_schema_contract_rejects_missing_bot_foreign_key(test_db, tmp_path):
    from db.logs import LogRepository

    source = tmp_path / "missing-bot-fk.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        conn.executescript("""
            DROP TABLE bot_audit;
            CREATE TABLE bot_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
                payload_json TEXT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

    with pytest.raises(sqlite3.DatabaseError, match=r"bot_audit.*missing foreign keys"):
        LogRepository._validate_application_database(str(source))


def test_backup_schema_contract_accepts_legacy_compatible_core(tmp_path):
    source = tmp_path / "legacy-compatible.db"
    _create_legacy_backup_schema(source)
    IrrigationDB(str(source))

    from db.logs import LogRepository

    LogRepository._validate_application_database(str(source))


def test_backup_schema_contract_accepts_historical_alter_column_order(tmp_path):
    source = tmp_path / "legacy-alter-order.db"
    _create_legacy_backup_schema(source, include_postpone_reason=False)

    IrrigationDB(str(source))

    with sqlite3.connect(source) as conn:
        columns = [str(row[1]) for row in conn.execute("PRAGMA table_info(zones)").fetchall()]
        assert columns.index("postpone_reason") > columns.index("mqtt_server_id")
    from db.logs import LogRepository

    LogRepository._validate_application_database(str(source))


def test_backup_schema_contract_rejects_missing_required_constraint(tmp_path):
    source = tmp_path / "missing-constraint.db"
    _create_legacy_backup_schema(source, group_name_unique=False)

    with pytest.raises(sqlite3.DatabaseError, match="unique constraint"):
        IrrigationDB(str(source))


@pytest.mark.parametrize(
    ("schema_options", "error"),
    [
        ({"zone_name_declaration": "INTEGER NOT NULL"}, "has declared type 'INTEGER'.*canonical 'TEXT'"),
        ({"program_name_declaration": "TEXT"}, "missing NOT NULL constraint"),
        ({"settings_key_declaration": "TEXT"}, "has primary key"),
    ],
)
def test_backup_schema_contract_rejects_wrong_column_type_nullability_or_primary_key(tmp_path, schema_options, error):
    source = tmp_path / "invalid-core-contract.db"
    _create_legacy_backup_schema(source, **schema_options)

    with pytest.raises(sqlite3.DatabaseError, match=error):
        IrrigationDB(str(source))


def test_schema_preflight_rejects_affinity_alias_without_writes(tmp_path):
    source = tmp_path / "legacy-affinity-alias.db"
    _create_legacy_backup_schema(source, zone_name_declaration="VARCHAR(255) NOT NULL")
    before = source.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match=r"affinity-compatible aliases.*not a supported migration input"):
        IrrigationDB(str(source))

    assert source.read_bytes() == before
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()
    with sqlite3.connect(source) as conn:
        assert conn.execute("PRAGMA application_id").fetchone() == (0,)
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)


@pytest.mark.parametrize(
    ("pragma", "value", "error"),
    [
        ("application_id", 0, "unsupported application_id"),
        ("user_version", 0, "unsupported application user_version"),
        ("schema_version", 0, "invalid schema_version"),
    ],
)
def test_backup_schema_contract_rejects_invalid_version_metadata(test_db, tmp_path, pragma, value, error):
    source = tmp_path / f"invalid-{pragma}.db"
    with sqlite3.connect(test_db.db_path) as live, sqlite3.connect(source) as copied:
        live.backup(copied)
    with sqlite3.connect(source) as conn:
        conn.execute(f"PRAGMA {pragma} = {value}")

    from db.logs import LogRepository

    with pytest.raises(sqlite3.DatabaseError, match=error):
        LogRepository._validate_application_database(str(source))


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


def test_backup_fsync_order_makes_file_durable_before_publication(test_db, tmp_path, monkeypatch):
    backup_dir = tmp_path / "ordered-backups"
    test_db.logs.backup_dir = str(backup_dir)
    events: list[tuple[str, str]] = []
    real_replace = os.replace

    def record_file_sync(path: str) -> None:
        events.append(("file-sync", Path(path).name))

    def record_directory_sync(path: str) -> None:
        events.append(("directory-sync", str(path)))

    def record_replace(source: str, target: str) -> None:
        events.append(("replace", f"{Path(source).name}->{Path(target).name}"))
        real_replace(source, target)

    monkeypatch.setattr(test_db.logs, "_fsync_file", record_file_sync)
    monkeypatch.setattr(test_db.logs, "_fsync_directory", record_directory_sync)
    monkeypatch.setattr("db.logs.os.replace", record_replace)

    result = test_db.create_backup()

    assert result is not None
    assert [event[0] for event in events] == [
        "directory-sync",
        "file-sync",
        "directory-sync",
        "replace",
        "directory-sync",
    ]
    assert events[0][1] == str(tmp_path)
    assert events[1][1].endswith(".partial")


def test_backup_durably_publishes_each_new_directory_component(test_db, tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = first / "second"
    backup_dir = second / "backups"
    test_db.logs.backup_dir = str(backup_dir)
    directory_syncs: list[str] = []

    monkeypatch.setattr(test_db.logs, "_fsync_directory", lambda path: directory_syncs.append(str(path)))

    result = test_db.create_backup()

    assert result is not None
    assert directory_syncs[:3] == [str(tmp_path), str(first), str(second)]
    assert directory_syncs[-2:] == [str(backup_dir), str(backup_dir)]


def test_backup_fails_closed_when_directory_cannot_sync_before_publish(test_db, tmp_path, monkeypatch):
    backup_dir = tmp_path / "unsyncable-backups"
    test_db.logs.backup_dir = str(backup_dir)

    def fail_directory_sync(_path: str) -> None:
        raise OSError("directory fsync unsupported")

    monkeypatch.setattr(test_db.logs, "_fsync_directory", fail_directory_sync)

    assert test_db.create_backup() is None
    assert list(backup_dir.glob("irrigation_backup_*.db")) == []
    assert list(backup_dir.glob("*.partial")) == []


def test_backup_preserves_published_file_when_post_rename_sync_fails(test_db, tmp_path, monkeypatch):
    from db.logs import LogRepository

    backup_dir = tmp_path / "late-sync-failure"
    test_db.logs.backup_dir = str(backup_dir)
    calls = 0

    def fail_post_publish_directory_sync(_path: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("late directory fsync failure")

    monkeypatch.setattr(test_db.logs, "_fsync_directory", fail_post_publish_directory_sync)

    assert test_db.create_backup() is None
    published = list(backup_dir.glob("irrigation_backup_*.db"))
    assert len(published) == 1
    assert list(backup_dir.glob("*.partial")) == []
    LogRepository._validate_application_database(str(published[0]))


def test_backup_rotation_fsyncs_directory_after_deletions(test_db, tmp_path, monkeypatch):
    backup_dir = tmp_path / "rotation-sync"
    backup_dir.mkdir()
    test_db.logs.backup_dir = str(backup_dir)
    for index in range(9):
        path = backup_dir / f"irrigation_backup_202001{index + 1:02d}_000000.db"
        path.write_bytes(b"old backup")
        os.utime(path, (index + 1, index + 1))

    events: list[str] = []
    real_remove = os.remove

    def record_remove(path: str) -> None:
        events.append("remove")
        real_remove(path)

    def record_directory_sync(_path: str) -> None:
        events.append("directory-sync")

    monkeypatch.setattr("db.logs.os.remove", record_remove)
    monkeypatch.setattr(test_db.logs, "_fsync_directory", record_directory_sync)

    test_db.logs._cleanup_old_backups(keep_count=7)

    assert events == ["remove", "remove", "directory-sync"]
    assert len(list(backup_dir.glob("irrigation_backup_*.db"))) == 7


def test_backup_rotation_never_deletes_just_published_backup_after_clock_rollback(test_db, tmp_path):
    backup_dir = tmp_path / "future-dated-backups"
    backup_dir.mkdir()
    test_db.logs.backup_dir = str(backup_dir)
    for index in range(7):
        path = backup_dir / f"irrigation_backup_2099010{index + 1}_000000.db"
        path.write_bytes(b"future backup")
        future_mtime = 4_000_000_000 + index
        os.utime(path, (future_mtime, future_mtime))

    result = test_db.create_backup()

    assert result is not None
    assert Path(result).exists()
    assert len(list(backup_dir.glob("irrigation_backup_*.db"))) == 7
