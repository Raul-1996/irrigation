"""Deep tests for zones DB operations — targeting uncovered lines."""
import pytest


class TestZonesDBDeep:
    """Deep tests for zone repository methods."""

    def test_get_zones_by_group(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        test_db.create_zone({'name': 'Z2', 'duration': 15, 'group_id': 1})
        test_db.create_zone({'name': 'Z3', 'duration': 20, 'group_id': 999})
        zones = test_db.get_zones_by_group(1)
        assert len(zones) == 2
        assert all(z['group_id'] == 1 for z in zones)

    def test_update_zone_versioned(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        zid = zones[0]['id']
        result = test_db.update_zone_versioned(zid, {'state': 'on'})
        assert result is True
        zone = test_db.get_zone(zid)
        assert zone['state'] == 'on'

    def test_update_zone_postpone(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        zid = zones[0]['id']
        test_db.update_zone_postpone(zid, '2099-01-01 00:00:00', 'rain')
        zone = test_db.get_zone(zid)
        assert zone['postpone_until'] == '2099-01-01 00:00:00'
        assert zone['postpone_reason'] == 'rain'

    def test_clear_postpone(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        zid = zones[0]['id']
        test_db.update_zone_postpone(zid, '2099-01-01 00:00:00', 'rain')
        test_db.update_zone_postpone(zid, None, None)
        zone = test_db.get_zone(zid)
        assert zone.get('postpone_until') is None

    def test_bulk_update_zones(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        test_db.create_zone({'name': 'Z2', 'duration': 15, 'group_id': 1})
        zones = test_db.get_zones()
        updates = [
            {'id': zones[0]['id'], 'duration': 20},
            {'id': zones[1]['id'], 'duration': 25},
        ]
        result = test_db.bulk_update_zones(updates)
        assert result is not None

    def test_create_zone_with_topic(self, test_db):
        test_db.create_zone({
            'name': 'Z1', 'duration': 10, 'group_id': 1,
            'topic': '/devices/wb-mr6cv3_85/controls/K1',
            'mqtt_server_id': 1,
        })
        zones = test_db.get_zones()
        assert len(zones) == 1
        assert zones[0]['topic'] == '/devices/wb-mr6cv3_85/controls/K1'

    def test_clear_group_scheduled_starts(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        test_db.clear_group_scheduled_starts(1)  # Should not crash

    def test_set_group_scheduled_starts(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        test_db.set_group_scheduled_starts(1, {zones[0]['id']: '2024-01-01 06:00:00'})
        zone = test_db.get_zone(zones[0]['id'])
        assert zone.get('scheduled_start_time') is not None


class TestZoneRuns:
    """Tests for zone_runs table operations."""

    def test_create_zone_run(self, test_db):
        """Create and read a zone run."""
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        zid = zones[0]['id']
        # zone_runs should exist as a table
        import sqlite3
        with sqlite3.connect(test_db.db_path) as conn:
            conn.execute('''INSERT INTO zone_runs (zone_id, group_id, start_utc, status)
                            VALUES (?, 1, '2024-01-01T06:00:00', 'running')''', (zid,))
            conn.commit()
            cur = conn.execute('SELECT * FROM zone_runs WHERE zone_id=?', (zid,))
            row = cur.fetchone()
            assert row is not None
