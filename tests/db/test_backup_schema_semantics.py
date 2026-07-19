import sqlite3
from pathlib import Path

import pytest

from db.identity import MAX_ENTITY_ID
from db.logs import LogRepository
from db.migrations import MigrationRunner


def _copy_database(test_db, tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    with sqlite3.connect(test_db.db_path) as source, sqlite3.connect(target) as copied:
        source.backup(copied)
    return target


def _set_table_schema_sql(conn: sqlite3.Connection, table: str, replacement: str) -> None:
    current = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()[0]
    assert replacement != current
    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    conn.execute("PRAGMA writable_schema = ON")
    try:
        conn.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?",
            (replacement, table),
        )
    finally:
        conn.execute("PRAGMA writable_schema = OFF")
    conn.execute(f"PRAGMA schema_version = {schema_version + 1}")


def test_validator_rejects_noop_group_reference_trigger(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "noop-group-trigger.db")
    with sqlite3.connect(source) as conn:
        conn.execute("DROP TRIGGER trg_zones_group_insert")
        conn.execute("""
            CREATE TRIGGER trg_zones_group_insert
            BEFORE INSERT ON zones
            WHEN NEW.group_id IS NOT NULL
             AND NEW.group_id != 0
             AND NOT EXISTS (SELECT 1 FROM groups WHERE id = NEW.group_id)
            BEGIN
                SELECT 1;
            END
        """)

    with pytest.raises(sqlite3.DatabaseError, match="invalid definition"):
        LogRepository.validate_application_database(str(source))


def test_validator_rejects_noop_mqtt_reference_trigger(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "noop-mqtt-trigger.db")
    with sqlite3.connect(source) as conn:
        conn.execute("DROP TRIGGER trg_zones_mqtt_server_insert")
        conn.execute("""
            CREATE TRIGGER trg_zones_mqtt_server_insert
            BEFORE INSERT ON zones
            WHEN NEW.mqtt_server_id IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM mqtt_servers WHERE id = NEW.mqtt_server_id)
            BEGIN
                SELECT 1;
            END
        """)

    with pytest.raises(sqlite3.DatabaseError, match="invalid definition"):
        LogRepository.validate_application_database(str(source))


def test_validator_rejects_mqtt_delete_guard_without_all_reference_sources(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "incomplete-mqtt-delete-trigger.db")
    with sqlite3.connect(source) as conn:
        conn.execute("DROP TRIGGER trg_mqtt_servers_restrict_referenced_delete")
        conn.execute("""
            CREATE TRIGGER trg_mqtt_servers_restrict_referenced_delete
            BEFORE DELETE ON mqtt_servers
            WHEN EXISTS (SELECT 1 FROM zones WHERE mqtt_server_id = OLD.id)
              OR EXISTS (SELECT 1 FROM groups WHERE master_mqtt_server_id = OLD.id)
            BEGIN
                SELECT RAISE(ABORT, 'mqtt server is referenced');
            END
        """)

    with pytest.raises(sqlite3.DatabaseError, match="invalid definition"):
        LogRepository.validate_application_database(str(source))


def test_validator_rejects_changed_case_sensitive_trigger_literal(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "wrong-retired-entity-literal.db")
    with sqlite3.connect(source) as conn:
        canonical_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'trg_zones_retire_id'"
        ).fetchone()[0]
        counterfeit_sql = canonical_sql.replace("'zone'", "'ZONE'", 1)
        assert counterfeit_sql != canonical_sql
        conn.execute("DROP TRIGGER trg_zones_retire_id")
        conn.execute(counterfeit_sql)

    with pytest.raises(sqlite3.DatabaseError, match="invalid definition"):
        LogRepository.validate_application_database(str(source))


def test_validator_and_init_reject_unexpected_destructive_trigger(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "unexpected-destructive-trigger.db")
    with sqlite3.connect(source) as conn:
        conn.execute("""
            CREATE TRIGGER stale_program_wipe
            AFTER UPDATE OF name ON zones
            BEGIN
                DELETE FROM programs;
            END
        """)

    with pytest.raises(sqlite3.DatabaseError, match="unexpected triggers"):
        LogRepository.validate_application_database(str(source))
    with pytest.raises(sqlite3.DatabaseError, match="unexpected triggers"):
        MigrationRunner(str(source)).init_database()


@pytest.mark.parametrize(
    "replacement_sql",
    [
        "CREATE INDEX idx_zones_group ON zones(group_id) WHERE group_id = 1",
        "CREATE UNIQUE INDEX idx_zones_group ON zones(group_id)",
    ],
)
def test_validator_rejects_wrong_required_index_flags(test_db, tmp_path, replacement_sql):
    source = _copy_database(test_db, tmp_path, "wrong-index-flags.db")
    with sqlite3.connect(source) as conn:
        conn.execute("DROP INDEX idx_zones_group")
        conn.execute(replacement_sql)

    with pytest.raises(sqlite3.DatabaseError, match=r"index 'idx_zones_group'.*invalid"):
        LogRepository.validate_application_database(str(source))


@pytest.mark.parametrize(
    "replacement_sql",
    [
        """
            CREATE INDEX idx_zone_runs_last_ok
            ON zone_runs(zone_id, end_utc DESC)
            WHERE status = 'ok' AND end_utc IS NOT NULL AND 0
        """,
        """
            CREATE INDEX idx_zone_runs_last_ok
            ON zone_runs(zone_id, end_utc ASC)
            WHERE status = 'ok' AND end_utc IS NOT NULL
        """,
        """
            CREATE INDEX idx_zone_runs_last_ok
            ON zone_runs(zone_id, end_utc DESC)
            WHERE status = 'OK' AND end_utc IS NOT NULL
        """,
    ],
)
def test_validator_rejects_wrong_runtime_index_predicate_or_order(test_db, tmp_path, replacement_sql):
    source = _copy_database(test_db, tmp_path, "wrong-last-ok-index.db")
    with sqlite3.connect(source) as conn:
        conn.execute("DROP INDEX idx_zone_runs_last_ok")
        conn.execute(replacement_sql)

    with pytest.raises(sqlite3.DatabaseError, match=r"index 'idx_zone_runs_last_ok'.*invalid"):
        LogRepository.validate_application_database(str(source))


def test_validator_rejects_unexpected_unique_constraint(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "unexpected-unique.db")
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE UNIQUE INDEX unexpected_unique_zone_name ON zones(name)")

    with pytest.raises(sqlite3.DatabaseError, match="unexpected unique constraint"):
        LogRepository.validate_application_database(str(source))


def test_validator_rejects_autoincrement_token_only_in_comment(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "comment-only-autoincrement.db")
    with sqlite3.connect(source) as conn:
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute(
            "UPDATE sqlite_master SET sql = REPLACE(sql, 'AUTOINCREMENT', '/* AUTOINCREMENT */') "
            "WHERE type = 'table' AND name = 'zones'"
        )
        conn.execute("PRAGMA writable_schema = OFF")
        conn.execute(f"PRAGMA schema_version = {schema_version + 1}")

    with pytest.raises(sqlite3.DatabaseError, match=r"zones.*missing AUTOINCREMENT"):
        LogRepository.validate_application_database(str(source))


def test_validator_rejects_unexpected_not_null_on_optional_column(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "unexpected-not-null.db")
    with sqlite3.connect(source) as conn:
        current = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'zones'").fetchone()[0]
        counterfeit = current.replace("mqtt_server_id INTEGER", "mqtt_server_id INTEGER NOT NULL", 1)
        _set_table_schema_sql(conn, "zones", counterfeit)

    with pytest.raises(sqlite3.DatabaseError, match=r"zones.*mqtt_server_id.*NOT NULL"):
        LogRepository.validate_application_database(str(source))


def test_validator_rejects_table_check_constraint(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "unexpected-check.db")
    with sqlite3.connect(source) as conn:
        current = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'zones'").fetchone()[0]
        stripped = current.rstrip()
        assert stripped.endswith(")")
        _set_table_schema_sql(conn, "zones", f"{stripped[:-1]}, CHECK(0))")

    with pytest.raises(sqlite3.DatabaseError, match=r"zones.*unexpected constraint.*CHECK"):
        LogRepository.validate_application_database(str(source))


def test_validator_rejects_unknown_column_constraint(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "unexpected-collation.db")
    with sqlite3.connect(source) as conn:
        current = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'zones'").fetchone()[0]
        counterfeit = current.replace("name TEXT NOT NULL", "name TEXT COLLATE NOCASE NOT NULL", 1)
        _set_table_schema_sql(conn, "zones", counterfeit)

    with pytest.raises(sqlite3.DatabaseError, match=r"zones.*unexpected constraint.*COLLATE"):
        LogRepository.validate_application_database(str(source))


def test_validator_rejects_extra_required_table_column(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "unexpected-column.db")
    with sqlite3.connect(source) as conn:
        current = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'zones'").fetchone()[0]
        stripped = current.rstrip()
        assert stripped.endswith(")")
        _set_table_schema_sql(conn, "zones", f"{stripped[:-1]}, poison TEXT NOT NULL)")

    with pytest.raises(sqlite3.DatabaseError, match=r"zones.*unexpected columns"):
        LogRepository.validate_application_database(str(source))


def test_validator_rejects_unexpected_app_table_index(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "unexpected-expression-index.db")
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE INDEX evil_zone_name_json ON zones(json_extract(name, '$.x'))")

    with pytest.raises(sqlite3.DatabaseError, match=r"unexpected indexes.*evil_zone_name_json"):
        LogRepository.validate_application_database(str(source))


def test_validator_rejects_unexpected_index_with_case_changed_catalog_owner(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "uppercase-index-owner.db")
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE INDEX evil_zone_name_json ON zones(json_extract(name, '$.x'))")
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.execute("PRAGMA writable_schema = ON")
        try:
            conn.execute(
                "UPDATE sqlite_master SET tbl_name = 'ZONES' WHERE type = 'index' AND name = 'evil_zone_name_json'"
            )
        finally:
            conn.execute("PRAGMA writable_schema = OFF")
        conn.execute(f"PRAGMA schema_version = {schema_version + 1}")
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    with pytest.raises(sqlite3.DatabaseError, match=r"unexpected indexes.*evil_zone_name_json"):
        LogRepository.validate_application_database(str(source))


def test_validator_rejects_extra_table_that_can_block_app_deletes(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "extra-incoming-foreign-key.db")
    with sqlite3.connect(source) as conn:
        zone_id = conn.execute("INSERT INTO zones(name) VALUES ('Delete target')").lastrowid
        conn.execute("CREATE TABLE delete_blocker(zone_id INTEGER REFERENCES zones(id) ON DELETE RESTRICT)")
        conn.execute("INSERT INTO delete_blocker(zone_id) VALUES (?)", (zone_id,))

    with pytest.raises(sqlite3.DatabaseError, match=r"unexpected tables.*delete_blocker"):
        LogRepository.validate_application_database(str(source))


@pytest.mark.parametrize(
    ("artifact", "ddl", "error"),
    [
        ("table", "CREATE TABLE local_probe(value TEXT)", r"unexpected tables.*local_probe"),
        ("view", "CREATE VIEW local_probe AS SELECT id FROM zones", r"unexpected views.*local_probe"),
    ],
)
def test_validator_and_backup_reject_artifact_that_startup_would_reject(
    test_db,
    tmp_path,
    artifact,
    ddl,
    error,
):
    source = _copy_database(test_db, tmp_path, f"extra-{artifact}.db")
    with sqlite3.connect(source) as conn:
        conn.execute(ddl)

    with pytest.raises(sqlite3.DatabaseError, match=error):
        LogRepository.validate_application_database(str(source))

    backup_dir = tmp_path / f"extra-{artifact}-backups"
    test_db.logs.db_path = str(source)
    test_db.logs.backup_dir = str(backup_dir)
    assert test_db.create_backup() is None
    assert not list(backup_dir.glob("*.db"))


@pytest.mark.parametrize(
    "foreign_key_suffix",
    [
        "ON UPDATE CASCADE ON DELETE CASCADE",
        "MATCH FULL ON DELETE CASCADE",
        "MATCH custom ON DELETE CASCADE",
    ],
)
def test_validator_rejects_noncanonical_foreign_key_semantics(test_db, tmp_path, foreign_key_suffix):
    source = _copy_database(test_db, tmp_path, "wrong-foreign-key.db")
    with sqlite3.connect(source) as conn:
        conn.executescript(f"""
            DROP TABLE program_cancellations;
            CREATE TABLE program_cancellations (
                program_id INTEGER NOT NULL,
                run_date TEXT NOT NULL,
                group_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (program_id, run_date, group_id),
                FOREIGN KEY (program_id) REFERENCES programs(id) {foreign_key_suffix}
            );
        """)

    with pytest.raises(sqlite3.DatabaseError, match="foreign keys"):
        LogRepository.validate_application_database(str(source))


def test_validator_and_init_reject_unexpected_history_foreign_key(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "unexpected-history-foreign-key.db")
    with sqlite3.connect(source) as conn:
        conn.executescript("""
            DROP TABLE weather_log;
            CREATE TABLE weather_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_id INTEGER,
                original_duration INTEGER,
                adjusted_duration INTEGER,
                coefficient INTEGER,
                skipped INTEGER DEFAULT 0,
                skip_reason TEXT,
                weather_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (zone_id) REFERENCES zones(id) ON DELETE CASCADE
            );
            CREATE INDEX idx_weather_log_zone ON weather_log(zone_id);
            CREATE INDEX idx_weather_log_time ON weather_log(created_at);
        """)

    with pytest.raises(sqlite3.DatabaseError, match=r"weather_log.*invalid foreign keys"):
        LogRepository.validate_application_database(str(source))
    with pytest.raises(sqlite3.DatabaseError, match=r"weather_log.*invalid foreign keys"):
        MigrationRunner(str(source)).init_database()


def test_validator_rejects_noncanonical_required_column_default(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "wrong-default.db")
    with sqlite3.connect(source) as conn:
        conn.executescript("""
            DROP TABLE weather_balance_log;
            CREATE TABLE weather_balance_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                et0_fact REAL,
                et0_norm REAL,
                precip_fact REAL,
                precip_eff REAL,
                deficit_day REAL,
                deficit_window REAL,
                coefficient INTEGER,
                created_at TIMESTAMP DEFAULT 'BROKEN'
            );
            CREATE INDEX idx_weather_balance_log_date ON weather_balance_log(date);
            CREATE INDEX idx_weather_balance_log_created ON weather_balance_log(created_at);
        """)

    with pytest.raises(sqlite3.DatabaseError, match=r"weather_balance_log.*created_at.*default"):
        LogRepository.validate_application_database(str(source))


@pytest.mark.parametrize(
    ("poison_sql", "error"),
    [
        (
            f"UPDATE sqlite_sequence SET seq = {MAX_ENTITY_ID + 1} WHERE name = 'zones'",
            "outside the supported durable identifier range",
        ),
        (
            "UPDATE sqlite_sequence SET seq = 1 WHERE name = 'groups'",
            "below its durable identifier high-water mark",
        ),
        (
            f"INSERT INTO retired_entity_ids(entity, id) VALUES ('zone', {MAX_ENTITY_ID + 1})",
            "retired zone identifier.*out of range",
        ),
        (
            "DELETE FROM sqlite_sequence WHERE name = 'programs'",
            "sqlite_sequence for programs must contain exactly one row",
        ),
        (
            f"INSERT INTO sqlite_sequence(name, seq) VALUES ('zones', {MAX_ENTITY_ID + 100})",
            "sqlite_sequence for zones must contain exactly one row",
        ),
        (
            "INSERT INTO retired_entity_ids(entity, id) VALUES ('zone', 1.5)",
            "retired zone identifier.*invalid storage",
        ),
        (
            "INSERT INTO retired_entity_ids(entity, id) VALUES ('group', 1)",
            "live group identifier 1.*also marked retired",
        ),
    ],
)
def test_validator_rejects_poisoned_durable_identity_metadata(test_db, tmp_path, poison_sql, error):
    source = _copy_database(test_db, tmp_path, "poisoned-durable-identity.db")
    with sqlite3.connect(source) as conn:
        conn.execute(poison_sql)

    with pytest.raises(sqlite3.DatabaseError, match=error):
        LogRepository.validate_application_database(str(source))


@pytest.mark.parametrize(
    "poison_sql",
    [
        f"INSERT INTO sqlite_sequence(name, seq) VALUES ('zones', {MAX_ENTITY_ID + 100})",
        "INSERT INTO retired_entity_ids(entity, id) VALUES ('zone', 1.5)",
        "INSERT INTO retired_entity_ids(entity, id) VALUES ('group', 1)",
    ],
)
def test_forward_init_rejects_ambiguous_durable_identity_metadata(test_db, tmp_path, poison_sql):
    source = _copy_database(test_db, tmp_path, "ambiguous-durable-identity.db")
    with sqlite3.connect(source) as conn:
        conn.execute(poison_sql)

    with pytest.raises(sqlite3.DatabaseError):
        MigrationRunner(str(source)).init_database()


def test_validator_rejects_poisoned_non_durable_autoincrement_sequence(test_db, tmp_path):
    source = _copy_database(test_db, tmp_path, "poisoned-log-sequence.db")
    with sqlite3.connect(source) as conn:
        cursor = conn.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = 'logs'",
            (9_223_372_036_854_775_807,),
        )
        if cursor.rowcount == 0:
            conn.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES ('logs', ?)",
                (9_223_372_036_854_775_807,),
            )

    with pytest.raises(sqlite3.DatabaseError, match=r"sqlite_sequence for logs has invalid value"):
        LogRepository.validate_application_database(str(source))
