import sqlite3
from pathlib import Path

import pytest

from db.identity import MAX_ENTITY_ID
from db.logs import LogRepository


def _copy_database(test_db, tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    with sqlite3.connect(test_db.db_path) as source, sqlite3.connect(target) as copied:
        source.backup(copied)
    return target


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


@pytest.mark.parametrize("foreign_key_suffix", ["ON UPDATE CASCADE ON DELETE CASCADE", "MATCH FULL ON DELETE CASCADE"])
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
            "sqlite_sequence for programs is missing or invalid",
        ),
    ],
)
def test_validator_rejects_poisoned_durable_identity_metadata(test_db, tmp_path, poison_sql, error):
    source = _copy_database(test_db, tmp_path, "poisoned-durable-identity.db")
    with sqlite3.connect(source) as conn:
        conn.execute(poison_sql)

    with pytest.raises(sqlite3.DatabaseError, match=error):
        LogRepository.validate_application_database(str(source))
