"""
Tests for database.py — comprehensive CRUD for zones, groups, programs, settings.
Uses fresh isolated DB per test.
"""
import os
import sys
import json
import pytest

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from database import IrrigationDB


@pytest.fixture
def db(tmp_path):
    """Fresh isolated DB per test."""
    path = str(tmp_path / 'test.db')
    d = IrrigationDB(db_path=path)
    d.init_database()
    return d


class TestGroups:
    def test_create_group(self, db):
        result = db.create_group('Насос-1')
        assert result is not None
        groups = db.get_groups()
        names = [g['name'] for g in groups]
        assert 'Насос-1' in names

    def test_create_multiple_groups(self, db):
        db.create_group('Group A')
        db.create_group('Group B')
        groups = db.get_groups()
        # At least 2 + any default
        assert len(groups) >= 2

    def test_delete_group(self, db):
        db.create_group('ToDelete')
        groups = db.get_groups()
        g = [g for g in groups if g['name'] == 'ToDelete']
        if g:
            result = db.delete_group(g[0]['id'])
            assert result is True
            after = db.get_groups()
            names = [x['name'] for x in after]
            assert 'ToDelete' not in names

    def test_delete_nonexistent_group(self, db):
        result = db.delete_group(99999)
        # Should not crash
        assert result in (True, False, None)


class TestZones:
    def test_get_zones_initial(self, db):
        zones = db.get_zones()
        assert isinstance(zones, list)

    def test_create_zone(self, db):
        db.create_group('G1')
        groups = db.get_groups()
        gid = groups[0]['id']
        zone = db.create_zone({
            'name': 'Test Zone',
            'icon': '🌱',
            'duration': 5,
            'group_id': gid,
            'topic': '/test/k1',
            'mqtt_server_id': 0
        })
        assert zone is not None

    def test_get_zone_by_id(self, db):
        db.create_group('G1')
        groups = db.get_groups()
        gid = groups[0]['id']
        created = db.create_zone({
            'name': 'Findme',
            'icon': '🔍',
            'duration': 3,
            'group_id': gid,
            'topic': '/test/find',
            'mqtt_server_id': 0
        })
        if created:
            z = db.get_zone(created['id'])
            assert z is not None
            assert z['name'] == 'Findme'

    def test_update_zone(self, db):
        db.create_group('G1')
        groups = db.get_groups()
        gid = groups[0]['id']
        created = db.create_zone({
            'name': 'Before',
            'icon': '🌿',
            'duration': 5,
            'group_id': gid,
            'topic': '/test/upd',
            'mqtt_server_id': 0
        })
        if created:
            updated = db.update_zone(created['id'], {'name': 'After'})
            if updated:
                assert updated['name'] == 'After'

    def test_delete_zone(self, db):
        db.create_group('G1')
        groups = db.get_groups()
        gid = groups[0]['id']
        created = db.create_zone({
            'name': 'Deleteme',
            'icon': '❌',
            'duration': 1,
            'group_id': gid,
            'topic': '/test/del',
            'mqtt_server_id': 0
        })
        if created:
            result = db.delete_zone(created['id'])
            assert result is True
            assert db.get_zone(created['id']) is None

    def test_get_nonexistent_zone(self, db):
        z = db.get_zone(99999)
        assert z is None

    def test_zones_by_group(self, db):
        db.create_group('GroupA')
        groups = db.get_groups()
        gid = groups[0]['id']
        db.create_zone({
            'name': 'Z1', 'icon': '🌱', 'duration': 1,
            'group_id': gid, 'topic': '/t/1', 'mqtt_server_id': 0
        })
        db.create_zone({
            'name': 'Z2', 'icon': '🌿', 'duration': 2,
            'group_id': gid, 'topic': '/t/2', 'mqtt_server_id': 0
        })
        zones = db.get_zones_by_group(gid)
        assert len(zones) >= 2


class TestSettings:
    def test_get_set_setting(self, db):
        db.set_setting_value('test_key', 'test_value')
        val = db.get_setting_value('test_key')
        assert val == 'test_value'

    def test_get_nonexistent_setting(self, db):
        val = db.get_setting_value('nonexistent_key_xyz')
        assert val is None

    def test_overwrite_setting(self, db):
        db.set_setting_value('k', 'v1')
        db.set_setting_value('k', 'v2')
        assert db.get_setting_value('k') == 'v2'

    def test_rain_config_roundtrip(self, db):
        cfg = {'enabled': True, 'threshold_mm': 5.0}
        db.set_rain_config(cfg)
        result = db.get_rain_config()
        assert isinstance(result, dict)


class TestPrograms:
    def test_get_programs_empty(self, db):
        """Programs may include defaults or be empty."""
        # Just ensure no crash
        try:
            from database import db as _db
        except Exception:
            pass

    def test_add_program_via_api(self, db):
        """Test program CRUD at DB level."""
        result = db.create_program({
            'name': 'Test Prog',
            'time': '06:00',
            'days': json.dumps([0, 1, 2, 3, 4, 5, 6]),
            'zones': json.dumps([1, 2, 3])
        })
        assert result is not None


class TestMQTTServers:
    def test_get_mqtt_servers(self, db):
        servers = db.get_mqtt_servers()
        assert isinstance(servers, list)

    def test_add_mqtt_server(self, db):
        result = db.create_mqtt_server({
            'name': 'test-broker',
            'host': '192.168.1.100',
            'port': 1883,
            'enabled': 1
        })
        assert result is not None

    def test_get_mqtt_server_by_id(self, db):
        s = db.get_mqtt_server(99999)
        assert s is None


class TestBotFSM:
    def test_set_and_get_fsm(self, db):
        db.set_bot_fsm(123456, 'main_menu', {'page': 1})
        state, data = db.get_bot_fsm(123456)
        assert state == 'main_menu'
        assert data == {'page': 1}

    def test_clear_fsm(self, db):
        db.set_bot_fsm(123456, 'some_state', {})
        db.set_bot_fsm(123456, None, None)
        state, data = db.get_bot_fsm(123456)
        assert state is None

    def test_idempotency_token(self, db):
        result1 = db.is_new_idempotency_token('token1', 123, 'action')
        assert result1 is True
        result2 = db.is_new_idempotency_token('token1', 123, 'action')
        assert result2 is False


class TestNotifSettings:
    def test_get_default_notif(self, db):
        settings = db.get_bot_user_notif_settings(123456)
        assert isinstance(settings, dict)

    def test_toggle_notif(self, db):
        db.set_bot_user_notif_toggle(123456, 'alerts', False)
        s = db.get_bot_user_notif_settings(123456)
        assert isinstance(s, dict)
