"""Tests for fail-closed migration downgrade inspection."""

import sqlite3

import pytest

from db.migrations import MigrationRunner


@pytest.fixture
def runner(test_db_path):
    migration_runner = MigrationRunner(test_db_path)
    migration_runner.init_database()
    return migration_runner


@pytest.mark.parametrize(
    "migration",
    [
        "weather_add_settings",
        "weather_create_log",
        "weather_create_cache",
        "zones_add_fault_tracking",
        "encrypt_mqtt_passwords",
        "telegram_create_bot_idempotency",
        "telegram_add_fsm_and_notif",
        "telegram_create_bot_audit",
        "telegram_create_bot_subscriptions",
        "telegram_create_bot_users",
        "weather_add_balance_settings",
        "weather_create_balance_log",
    ],
)
def test_live_downgrade_is_preview_only(runner, test_db_path, migration):
    preview = runner.preview_rollback_migration(migration)
    assert preview == {
        "migration": migration,
        "known": True,
        "applied": True,
        "supported": False,
        "would_mutate": False,
        "error_code": "LIVE_DOWNGRADE_UNSUPPORTED",
        "recovery": "restore a pre-upgrade database backup",
    }

    with sqlite3.connect(test_db_path) as conn:
        before = conn.total_changes
    assert runner.rollback_migration(migration) is False
    with sqlite3.connect(test_db_path) as conn:
        assert conn.execute("SELECT 1 FROM migrations WHERE name = ?", (migration,)).fetchone() == (1,)
        assert conn.total_changes == before


def test_unknown_downgrade_is_also_non_mutating(runner):
    preview = runner.preview_rollback_migration("nonexistent_migration")
    assert preview["known"] is False
    assert preview["applied"] is False
    assert preview["would_mutate"] is False
    assert runner.rollback_migration("nonexistent_migration") is False


class TestRecreateTableWithoutColumns:
    """Keep the low-level schema helper covered; it is not a live API."""

    def test_drop_single_column(self, test_db_path):
        runner = MigrationRunner(test_db_path)
        with sqlite3.connect(test_db_path) as conn:
            conn.execute("CREATE TABLE test_t (id INTEGER PRIMARY KEY, a TEXT, b TEXT, c TEXT)")
            conn.execute("INSERT INTO test_t VALUES (1, 'x', 'y', 'z')")
            conn.commit()
            runner._recreate_table_without_columns(conn, "test_t", ["b"])
            cols = [row[1] for row in conn.execute("PRAGMA table_info(test_t)").fetchall()]
            assert cols == ["id", "a", "c"]
            assert conn.execute("SELECT * FROM test_t").fetchone() == (1, "x", "z")

    def test_drop_multiple_columns(self, test_db_path):
        runner = MigrationRunner(test_db_path)
        with sqlite3.connect(test_db_path) as conn:
            conn.execute("CREATE TABLE test_m (id INTEGER PRIMARY KEY, a TEXT, b TEXT, c TEXT)")
            conn.execute("INSERT INTO test_m VALUES (1, 'x', 'y', 'z')")
            conn.commit()
            runner._recreate_table_without_columns(conn, "test_m", ["a", "c"])
            cols = [row[1] for row in conn.execute("PRAGMA table_info(test_m)").fetchall()]
            assert cols == ["id", "b"]
