"""Comprehensive tests for db/groups.py."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestGroupCRUD:
    def test_create_group(self, test_db):
        g = test_db.create_group('Test Group')
        assert g is not None
        assert g['name'] == 'Test Group'

    def test_get_groups(self, test_db):
        test_db.create_group('G1')
        test_db.create_group('G2')
        groups = test_db.get_groups()
        assert len(groups) >= 2

    def test_update_group(self, test_db):
        g = test_db.create_group('Old')
        result = test_db.update_group(g['id'], 'New')
        assert result is True

    def test_delete_group(self, test_db):
        g = test_db.create_group('Del')
        result = test_db.delete_group(g['id'])
        assert result is True

    def test_delete_nonexistent(self, test_db):
        result = test_db.delete_group(99999)
        assert isinstance(result, bool)


class TestGroupFields:
    def test_update_fields(self, test_db):
        g = test_db.create_group('Fields')
        result = test_db.update_group_fields(g['id'], {
            'use_master_valve': 1,
            'master_mqtt_topic': '/master/valve',
        })
        assert result is True

    def test_update_fields_nonexistent(self, test_db):
        result = test_db.update_group_fields(99999, {'name': 'X'})
        assert isinstance(result, bool)


class TestGroupRain:
    def test_get_use_rain_default(self, test_db):
        g = test_db.create_group('Rain')
        result = test_db.get_group_use_rain(g['id'])
        assert isinstance(result, bool)

    def test_set_use_rain(self, test_db):
        g = test_db.create_group('Rain')
        test_db.set_group_use_rain(g['id'], True)
        assert test_db.get_group_use_rain(g['id']) is True
        test_db.set_group_use_rain(g['id'], False)
        assert test_db.get_group_use_rain(g['id']) is False


class TestGroupMinLists:
    def test_list_groups_min(self, test_db):
        test_db.create_group('Min')
        result = test_db.list_groups_min()
        assert isinstance(result, list)

    def test_list_zones_by_group_min(self, test_db):
        g = test_db.create_group('Min')
        test_db.create_zone({'name': 'Z', 'duration': 10, 'group_id': g['id']})
        result = test_db.list_zones_by_group_min(g['id'])
        assert isinstance(result, list)
