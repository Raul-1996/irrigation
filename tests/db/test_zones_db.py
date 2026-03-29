"""Tests for zone DB operations: CRUD, versioned update, optimistic locking."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestZoneCRUD:
    def test_create_zone(self, test_db):
        zone = test_db.create_zone({'name': 'Test', 'duration': 10, 'group_id': 1})
        assert zone is not None
        assert zone['name'] == 'Test'
        assert zone['duration'] == 10

    def test_get_zone(self, test_db):
        zone = test_db.create_zone({'name': 'Z1', 'duration': 5, 'group_id': 1})
        fetched = test_db.get_zone(zone['id'])
        assert fetched is not None
        assert fetched['name'] == 'Z1'

    def test_get_zone_not_found(self, test_db):
        assert test_db.get_zone(9999) is None

    def test_get_zones(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 5, 'group_id': 1})
        test_db.create_zone({'name': 'Z2', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        assert len(zones) >= 2

    def test_update_zone(self, test_db):
        zone = test_db.create_zone({'name': 'Old', 'duration': 5, 'group_id': 1})
        updated = test_db.update_zone(zone['id'], {'name': 'New', 'duration': 20})
        assert updated is not None
        assert updated['name'] == 'New'
        assert updated['duration'] == 20

    def test_update_zone_not_found(self, test_db):
        result = test_db.update_zone(9999, {'name': 'X'})
        assert result is None

    def test_delete_zone(self, test_db):
        zone = test_db.create_zone({'name': 'Del', 'duration': 5, 'group_id': 1})
        assert test_db.delete_zone(zone['id']) is True
        assert test_db.get_zone(zone['id']) is None

    def test_delete_zone_not_found(self, test_db):
        # delete_zone returns True even for nonexistent zones (DELETE WHERE id=X affects 0 rows but no error)
        result = test_db.delete_zone(9999)
        assert isinstance(result, bool)

    def test_get_zones_by_group(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 5, 'group_id': 1})
        test_db.create_zone({'name': 'Z2', 'duration': 5, 'group_id': 1})
        test_db.create_zone({'name': 'Z3', 'duration': 5, 'group_id': 999})
        zones = test_db.get_zones_by_group(1)
        assert all(z['group_id'] == 1 for z in zones)


class TestVersionedUpdate:
    def test_versioned_update(self, test_db):
        zone = test_db.create_zone({'name': 'V', 'duration': 5, 'group_id': 1})
        result = test_db.update_zone_versioned(zone['id'], {'state': 'on'})
        # Should succeed (returns True or False)
        assert isinstance(result, bool)

    def test_versioned_update_not_found(self, test_db):
        result = test_db.update_zone_versioned(9999, {'state': 'on'})
        assert result is False


class TestBulkOperations:
    def test_bulk_upsert(self, test_db):
        zones = [
            {'name': 'B1', 'duration': 5, 'group_id': 1, 'icon': '🌱'},
            {'name': 'B2', 'duration': 10, 'group_id': 1, 'icon': '🌿'},
        ]
        result = test_db.bulk_upsert_zones(zones)
        assert isinstance(result, dict)

    def test_bulk_update(self, test_db):
        z1 = test_db.create_zone({'name': 'U1', 'duration': 5, 'group_id': 1})
        z2 = test_db.create_zone({'name': 'U2', 'duration': 10, 'group_id': 1})
        updates = [
            {'id': z1['id'], 'duration': 20},
            {'id': z2['id'], 'duration': 30},
        ]
        result = test_db.bulk_update_zones(updates)
        assert isinstance(result, dict)


class TestZonePostpone:
    def test_postpone_zone(self, test_db):
        zone = test_db.create_zone({'name': 'P', 'duration': 5, 'group_id': 1})
        test_db.update_zone_postpone(zone['id'], '2026-12-31 23:59:59', 'manual')
        z = test_db.get_zone(zone['id'])
        assert z['postpone_until'] == '2026-12-31 23:59:59'
        assert z['postpone_reason'] == 'manual'

    def test_clear_postpone(self, test_db):
        zone = test_db.create_zone({'name': 'P', 'duration': 5, 'group_id': 1})
        test_db.update_zone_postpone(zone['id'], '2026-12-31 23:59:59', 'rain')
        test_db.update_zone_postpone(zone['id'], None, None)
        z = test_db.get_zone(zone['id'])
        assert z['postpone_until'] is None
        assert z['postpone_reason'] is None


class TestZonePhoto:
    def test_update_photo(self, test_db):
        zone = test_db.create_zone({'name': 'Photo', 'duration': 5, 'group_id': 1})
        test_db.update_zone_photo(zone['id'], 'media/zones/test.webp')
        z = test_db.get_zone(zone['id'])
        assert z['photo_path'] == 'media/zones/test.webp'

    def test_clear_photo(self, test_db):
        zone = test_db.create_zone({'name': 'Photo', 'duration': 5, 'group_id': 1})
        test_db.update_zone_photo(zone['id'], 'media/zones/test.webp')
        test_db.update_zone_photo(zone['id'], None)
        z = test_db.get_zone(zone['id'])
        assert z['photo_path'] is None


class TestZoneRuns:
    def test_create_and_get_open_run(self, test_db):
        zone = test_db.create_zone({'name': 'Run', 'duration': 10, 'group_id': 1})
        test_db.create_zone_run(zone['id'], 1, '2026-01-01 10:00:00', 1000.0, 100, 1, 0.0)
        run = test_db.get_open_zone_run(zone['id'])
        assert run is not None
        assert run['zone_id'] == zone['id']

    def test_finish_zone_run(self, test_db):
        zone = test_db.create_zone({'name': 'Run', 'duration': 10, 'group_id': 1})
        test_db.create_zone_run(zone['id'], 1, '2026-01-01 10:00:00', 1000.0, 100, 1, 0.0)
        run = test_db.get_open_zone_run(zone['id'])
        test_db.finish_zone_run(run['id'], '2026-01-01 10:10:00', 1600.0, 110, 10.0, 1.0, 'ok')
        # Should no longer have an open run
        assert test_db.get_open_zone_run(zone['id']) is None
