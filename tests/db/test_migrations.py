"""Tests for DB migrations: all pass on empty DB, idempotent."""
import pytest
import os
import sqlite3

os.environ['TESTING'] = '1'


class TestMigrations:
    def test_init_on_empty_db(self, test_db_path):
        """All migrations should pass on a fresh empty DB."""
        from database import IrrigationDB
        db = IrrigationDB(db_path=test_db_path)
        # If we get here without exception, all migrations passed
        zones = db.get_zones()
        assert isinstance(zones, list)
        groups = db.get_groups()
        assert isinstance(groups, list)

    def test_migrations_idempotent(self, test_db_path):
        """Running init_database twice should not fail."""
        from database import IrrigationDB
        db = IrrigationDB(db_path=test_db_path)
        # Run again — should be idempotent
        db.init_database()
        # Everything should still work
        zones = db.get_zones()
        assert isinstance(zones, list)

    def test_all_tables_exist(self, test_db_path):
        """All expected tables should exist after migration."""
        from database import IrrigationDB
        IrrigationDB(db_path=test_db_path)

        conn = sqlite3.connect(test_db_path)
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}
        conn.close()

        expected = {
            'zones', 'groups', 'programs', 'logs', 'water_usage',
            'settings', 'migrations', 'mqtt_servers', 'program_cancellations',
            'zone_runs', 'bot_users', 'bot_subscriptions', 'bot_audit',
            'bot_idempotency',
        }
        for t in expected:
            assert t in tables, f"Table {t} missing"

    def test_special_group_999_exists(self, test_db_path):
        """Special group 999 'БЕЗ ПОЛИВА' should exist."""
        from database import IrrigationDB
        db = IrrigationDB(db_path=test_db_path)
        groups = db.get_groups()
        ids = [g['id'] for g in groups]
        assert 999 in ids

    def test_default_password_set(self, test_db_path):
        """Default password should be set after init."""
        from database import IrrigationDB
        db = IrrigationDB(db_path=test_db_path)
        h = db.get_password_hash()
        assert h is not None

    def test_migration_records_exist(self, test_db_path):
        """Migration names should be recorded in migrations table."""
        from database import IrrigationDB
        IrrigationDB(db_path=test_db_path)

        conn = sqlite3.connect(test_db_path)
        cur = conn.execute("SELECT name FROM migrations")
        names = {row[0] for row in cur.fetchall()}
        conn.close()

        # Check some key migrations
        assert 'zones_add_watering_start_time' in names
        assert 'create_mqtt_servers' in names
        assert 'zones_add_fault_tracking' in names

    def test_zones_table_has_all_columns(self, test_db_path):
        """Zones table should have all columns after migrations."""
        from database import IrrigationDB
        IrrigationDB(db_path=test_db_path)

        conn = sqlite3.connect(test_db_path)
        cur = conn.execute("PRAGMA table_info(zones)")
        cols = {row[1] for row in cur.fetchall()}
        conn.close()

        expected_cols = {
            'id', 'state', 'name', 'icon', 'duration', 'group_id', 'topic',
            'postpone_until', 'postpone_reason', 'photo_path',
            'watering_start_time', 'last_watering_time', 'mqtt_server_id',
            'planned_end_time', 'version', 'commanded_state', 'observed_state',
            'fault_count', 'last_fault',
        }
        for c in expected_cols:
            assert c in cols, f"Column {c} missing from zones table"
