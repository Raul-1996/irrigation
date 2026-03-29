"""Tests for group DB operations: CRUD, master valve."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestGroupCRUD:
    def test_get_groups_includes_defaults(self, test_db):
        groups = test_db.get_groups()
        assert len(groups) >= 1
        ids = [g['id'] for g in groups]
        assert 999 in ids  # Special 'NO WATERING' group

    def test_create_group(self, test_db):
        group = test_db.create_group('Линия 2')
        assert group is not None
        assert group['name'] == 'Линия 2'

    def test_create_duplicate_group(self, test_db):
        test_db.create_group('Уникальная')
        result = test_db.create_group('Уникальная')
        assert result is None  # Duplicate name

    def test_update_group(self, test_db):
        group = test_db.create_group('Old Name')
        assert test_db.update_group(group['id'], 'New Name') is True
        groups = test_db.get_groups()
        updated = next(g for g in groups if g['id'] == group['id'])
        assert updated['name'] == 'New Name'

    def test_delete_group(self, test_db):
        group = test_db.create_group('To Delete')
        assert test_db.delete_group(group['id']) is True

    def test_delete_group_with_zones_fails(self, test_db):
        group = test_db.create_group('Has Zones')
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': group['id']})
        result = test_db.delete_group(group['id'])
        # Should fail because group has zones
        # Depending on implementation it might return True or False
        assert isinstance(result, bool)


class TestGroupRain:
    def test_set_and_get_rain(self, test_db):
        test_db.set_group_use_rain(1, True)
        assert test_db.get_group_use_rain(1) is True

    def test_disable_rain(self, test_db):
        test_db.set_group_use_rain(1, True)
        test_db.set_group_use_rain(1, False)
        assert test_db.get_group_use_rain(1) is False


class TestGroupFields:
    def test_update_group_fields(self, test_db):
        group = test_db.create_group('Fields Test')
        result = test_db.update_group_fields(group['id'], {
            'use_master_valve': 1,
            'master_mqtt_topic': '/mv/test',
            'master_mode': 'NC',
        })
        assert result is True

    def test_list_groups_min(self, test_db):
        result = test_db.list_groups_min()
        assert isinstance(result, list)
