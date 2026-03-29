"""Comprehensive tests for db/migrations.py."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestMigrations:
    def test_init_database(self, test_db):
        """Database should initialize correctly with all migrations."""
        # test_db fixture already calls init_database
        zones = test_db.get_zones()
        assert isinstance(zones, list)

    def test_rerun_migrations(self, test_db):
        """Re-running init should be idempotent."""
        test_db.init_database()
        zones = test_db.get_zones()
        assert isinstance(zones, list)

    def test_create_after_migration(self, test_db):
        """CRUD operations should work after migrations."""
        z = test_db.create_zone({'name': 'PostMigration', 'duration': 10, 'group_id': 1})
        assert z is not None
        p = test_db.create_program({
            'name': 'PostMig', 'time': '06:00', 'days': [0], 'zones': [z['id']],
        })
        assert p is not None
        g = test_db.create_group('PostMigGroup')
        assert g is not None
        s = test_db.create_mqtt_server({
            'name': 'PostMig', 'host': '127.0.0.1', 'port': 1883,
        })
        assert s is not None

    def test_fresh_db_from_scratch(self, test_db_path):
        """Create a completely fresh DB."""
        from database import IrrigationDB
        db = IrrigationDB(db_path=test_db_path)
        assert db is not None
        zones = db.get_zones()
        assert isinstance(zones, list)
