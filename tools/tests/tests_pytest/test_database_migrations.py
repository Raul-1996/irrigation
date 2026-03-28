"""
Tests for database migrations — ensure init_database creates proper schema.
"""
import os
import sys
import sqlite3
import pytest

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from database import IrrigationDB


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'migration_test.db')
    d = IrrigationDB(db_path=path)
    d.init_database()
    return d, path


class TestSchemaCreation:
    def test_zones_table_exists(self, db):
        d, path = db
        conn = sqlite3.connect(path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert 'zones' in tables

    def test_groups_table_exists(self, db):
        d, path = db
        conn = sqlite3.connect(path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert 'groups' in tables

    def test_programs_table_exists(self, db):
        d, path = db
        conn = sqlite3.connect(path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert 'programs' in tables

    def test_settings_table_exists(self, db):
        d, path = db
        conn = sqlite3.connect(path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert 'settings' in tables

    def test_mqtt_servers_table_exists(self, db):
        d, path = db
        conn = sqlite3.connect(path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert 'mqtt_servers' in tables

    def test_logs_table_exists(self, db):
        d, path = db
        conn = sqlite3.connect(path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert 'logs' in tables

    def test_zone_runs_table_exists(self, db):
        d, path = db
        conn = sqlite3.connect(path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert 'zone_runs' in tables

    def test_bot_users_table_exists(self, db):
        d, path = db
        conn = sqlite3.connect(path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert 'bot_users' in tables


class TestMigrationIdempotency:
    def test_double_init(self, db):
        """Running init_database twice should not crash."""
        d, path = db
        d.init_database()  # Second time
        zones = d.get_zones()
        assert isinstance(zones, list)

    def test_triple_init(self, db):
        d, path = db
        d.init_database()
        d.init_database()
        groups = d.get_groups()
        assert isinstance(groups, list)


class TestZoneColumns:
    def test_zone_has_topic(self, db):
        d, path = db
        conn = sqlite3.connect(path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(zones)").fetchall()]
        conn.close()
        assert 'topic' in cols

    def test_zone_has_mqtt_server_id(self, db):
        d, path = db
        conn = sqlite3.connect(path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(zones)").fetchall()]
        conn.close()
        assert 'mqtt_server_id' in cols

    def test_zone_has_state(self, db):
        d, path = db
        conn = sqlite3.connect(path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(zones)").fetchall()]
        conn.close()
        assert 'state' in cols
