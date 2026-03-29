"""Tests for migration downgrade support."""
import sqlite3
import pytest
from db.migrations import MigrationRunner


class TestMigrationDowngrade:
    """Test rollback_migration() for the last 10 migrations."""

    @pytest.fixture
    def runner(self, test_db_path):
        """Create a MigrationRunner with a fully migrated DB."""
        runner = MigrationRunner(test_db_path)
        runner.init_database()
        return runner

    def _has_table(self, db_path, table):
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            return cur.fetchone() is not None

    def _has_column(self, db_path, table, column):
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute("PRAGMA table_info(%s)" % table)
            return column in [r[1] for r in cur.fetchall()]

    def _is_applied(self, db_path, name):
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute("SELECT 1 FROM migrations WHERE name=?", (name,))
            return cur.fetchone() is not None

    def test_rollback_weather_add_settings(self, runner, test_db_path):
        assert self._is_applied(test_db_path, 'weather_add_settings')
        assert runner.rollback_migration('weather_add_settings')
        assert not self._is_applied(test_db_path, 'weather_add_settings')
        # Weather keys should be gone
        with sqlite3.connect(test_db_path) as conn:
            cur = conn.execute("SELECT value FROM settings WHERE key='weather.enabled'")
            assert cur.fetchone() is None

    def test_rollback_weather_create_log(self, runner, test_db_path):
        assert self._has_table(test_db_path, 'weather_log')
        assert runner.rollback_migration('weather_create_log')
        assert not self._has_table(test_db_path, 'weather_log')
        assert not self._is_applied(test_db_path, 'weather_create_log')

    def test_rollback_weather_create_cache(self, runner, test_db_path):
        assert self._has_table(test_db_path, 'weather_cache')
        assert runner.rollback_migration('weather_create_cache')
        assert not self._has_table(test_db_path, 'weather_cache')

    def test_rollback_zones_add_fault_tracking(self, runner, test_db_path):
        assert self._has_column(test_db_path, 'zones', 'last_fault')
        assert self._has_column(test_db_path, 'zones', 'fault_count')
        assert runner.rollback_migration('zones_add_fault_tracking')
        assert not self._has_column(test_db_path, 'zones', 'last_fault')
        assert not self._has_column(test_db_path, 'zones', 'fault_count')

    def test_rollback_encrypt_mqtt_passwords(self, runner, test_db_path):
        # This is a no-op downgrade (can't decrypt)
        assert runner.rollback_migration('encrypt_mqtt_passwords')
        assert not self._is_applied(test_db_path, 'encrypt_mqtt_passwords')

    def test_rollback_telegram_create_bot_idempotency(self, runner, test_db_path):
        assert self._has_table(test_db_path, 'bot_idempotency')
        assert runner.rollback_migration('telegram_create_bot_idempotency')
        assert not self._has_table(test_db_path, 'bot_idempotency')

    def test_rollback_telegram_add_fsm_and_notif(self, runner, test_db_path):
        assert self._has_column(test_db_path, 'bot_users', 'fsm_state')
        assert runner.rollback_migration('telegram_add_fsm_and_notif')
        assert not self._has_column(test_db_path, 'bot_users', 'fsm_state')
        assert not self._has_column(test_db_path, 'bot_users', 'notif_rain')
        # Core columns should survive
        assert self._has_column(test_db_path, 'bot_users', 'chat_id')

    def test_rollback_telegram_create_bot_audit(self, runner, test_db_path):
        assert self._has_table(test_db_path, 'bot_audit')
        assert runner.rollback_migration('telegram_create_bot_audit')
        assert not self._has_table(test_db_path, 'bot_audit')

    def test_rollback_telegram_create_bot_subscriptions(self, runner, test_db_path):
        assert self._has_table(test_db_path, 'bot_subscriptions')
        assert runner.rollback_migration('telegram_create_bot_subscriptions')
        assert not self._has_table(test_db_path, 'bot_subscriptions')

    def test_rollback_telegram_create_bot_users(self, runner, test_db_path):
        assert self._has_table(test_db_path, 'bot_users')
        assert runner.rollback_migration('telegram_create_bot_users')
        assert not self._has_table(test_db_path, 'bot_users')

    def test_rollback_unknown_migration_returns_false(self, runner):
        assert not runner.rollback_migration('nonexistent_migration')

    def test_rollback_unapplied_migration_returns_false(self, runner, test_db_path):
        # First rollback
        runner.rollback_migration('weather_add_settings')
        # Second rollback — not applied
        assert not runner.rollback_migration('weather_add_settings')

    def test_reapply_after_rollback(self, runner, test_db_path):
        """After rollback, re-running init_database should reapply the migration."""
        runner.rollback_migration('weather_create_cache')
        assert not self._has_table(test_db_path, 'weather_cache')
        runner.init_database()
        assert self._has_table(test_db_path, 'weather_cache')
        assert self._is_applied(test_db_path, 'weather_create_cache')


class TestRecreateTableWithoutColumns:
    """Test the SQLite-compatible column drop helper."""

    def test_drop_single_column(self, test_db_path):
        runner = MigrationRunner(test_db_path)
        with sqlite3.connect(test_db_path) as conn:
            conn.execute('CREATE TABLE test_t (id INTEGER PRIMARY KEY, a TEXT, b TEXT, c TEXT)')
            conn.execute("INSERT INTO test_t VALUES (1, 'x', 'y', 'z')")
            conn.commit()
            runner._recreate_table_without_columns(conn, 'test_t', ['b'])
            cur = conn.execute('PRAGMA table_info(test_t)')
            cols = [r[1] for r in cur.fetchall()]
            assert 'a' in cols
            assert 'b' not in cols
            assert 'c' in cols
            cur2 = conn.execute('SELECT * FROM test_t')
            row = cur2.fetchone()
            assert row == (1, 'x', 'z')

    def test_drop_multiple_columns(self, test_db_path):
        runner = MigrationRunner(test_db_path)
        with sqlite3.connect(test_db_path) as conn:
            conn.execute('CREATE TABLE test_m (id INTEGER PRIMARY KEY, a TEXT, b TEXT, c TEXT)')
            conn.execute("INSERT INTO test_m VALUES (1, 'x', 'y', 'z')")
            conn.commit()
            runner._recreate_table_without_columns(conn, 'test_m', ['a', 'c'])
            cur = conn.execute('PRAGMA table_info(test_m)')
            cols = [r[1] for r in cur.fetchall()]
            assert cols == ['id', 'b']
