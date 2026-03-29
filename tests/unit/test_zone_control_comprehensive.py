"""Comprehensive tests for services/zone_control.py."""
import pytest
import os
import time
from unittest.mock import patch, MagicMock, call

os.environ['TESTING'] = '1'


@pytest.fixture
def mock_deps(test_db):
    """Common mocks for zone_control tests."""
    with patch('services.zone_control.db', test_db), \
         patch('services.zone_control.publish_mqtt_value', return_value=True) as mock_pub, \
         patch('services.zone_control.water_monitor') as mock_water, \
         patch('services.zone_control.state_verifier') as mock_verifier:
        mock_water.get_pulses_at_or_before.return_value = None
        mock_water.get_pulses_at_or_after.return_value = None
        mock_water.summarize_run.return_value = (None, None)
        yield {
            'db': test_db,
            'publish': mock_pub,
            'water': mock_water,
            'verifier': mock_verifier,
        }


class TestVersionedUpdate:
    def test_versioned_update_success(self, test_db):
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        with patch('services.zone_control.db', test_db):
            from services.zone_control import _versioned_update
            _versioned_update(z['id'], {'state': 'on'})
            zone = test_db.get_zone(z['id'])
            assert zone['state'] == 'on'


class TestIsValidStates:
    def test_valid_start_states(self):
        from services.zone_control import _is_valid_start_state
        assert _is_valid_start_state('off') is True
        assert _is_valid_start_state('stopping') is True
        assert _is_valid_start_state('on') is False
        assert _is_valid_start_state('starting') is False
        assert _is_valid_start_state(None) is False
        assert _is_valid_start_state('') is False

    def test_valid_stop_states(self):
        from services.zone_control import _is_valid_stop_state
        assert _is_valid_stop_state('on') is True
        assert _is_valid_stop_state('starting') is True
        assert _is_valid_stop_state('off') is False
        assert _is_valid_stop_state('stopping') is False


class TestExclusiveStartZone:
    def test_start_nonexistent(self, mock_deps):
        from services.zone_control import exclusive_start_zone
        assert exclusive_start_zone(9999) is False

    def test_start_zone_without_mqtt(self, mock_deps):
        z = mock_deps['db'].create_zone({
            'name': 'Z1', 'duration': 10, 'group_id': 1,
        })
        from services.zone_control import exclusive_start_zone
        result = exclusive_start_zone(z['id'])
        assert result is True

    def test_start_zone_with_mqtt(self, mock_deps):
        srv = mock_deps['db'].create_mqtt_server({
            'name': 'Test', 'host': '127.0.0.1', 'port': 1883,
        })
        z = mock_deps['db'].create_zone({
            'name': 'Z1', 'duration': 10, 'group_id': 1,
            'topic': '/test/z1', 'mqtt_server_id': srv['id'],
        })
        from services.zone_control import exclusive_start_zone
        result = exclusive_start_zone(z['id'])
        assert result is True
        zone = mock_deps['db'].get_zone(z['id'])
        assert zone['state'] == 'on'

    def test_start_already_on_zone(self, mock_deps):
        z = mock_deps['db'].create_zone({
            'name': 'Z1', 'duration': 10, 'group_id': 1,
            'topic': '/test/z1',
        })
        mock_deps['db'].update_zone(z['id'], {'state': 'on'})
        from services.zone_control import exclusive_start_zone
        result = exclusive_start_zone(z['id'])
        assert result is True

    def test_exclusive_start_stops_peers(self, mock_deps):
        z1 = mock_deps['db'].create_zone({
            'name': 'Z1', 'duration': 10, 'group_id': 1, 'topic': '/t/1',
        })
        z2 = mock_deps['db'].create_zone({
            'name': 'Z2', 'duration': 10, 'group_id': 1, 'topic': '/t/2',
        })
        mock_deps['db'].update_zone(z1['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})

        from services.zone_control import exclusive_start_zone
        result = exclusive_start_zone(z2['id'])
        assert result is True
        time.sleep(1.5)  # wait for ThreadPoolExecutor
        z1_after = mock_deps['db'].get_zone(z1['id'])
        assert z1_after['state'] in ('off', 'stopping')

    def test_start_zone_with_master_valve(self, mock_deps):
        srv = mock_deps['db'].create_mqtt_server({
            'name': 'Test', 'host': '127.0.0.1', 'port': 1883,
        })
        mock_deps['db'].create_group('TestGroup')
        groups = mock_deps['db'].get_groups()
        gid = groups[0]['id']
        mock_deps['db'].update_group_fields(gid, {
            'use_master_valve': 1,
            'master_mqtt_topic': '/master/valve',
            'master_mqtt_server_id': srv['id'],
            'master_mode': 'NC',
        })
        z = mock_deps['db'].create_zone({
            'name': 'Z1', 'duration': 10, 'group_id': gid,
            'topic': '/test/z1', 'mqtt_server_id': srv['id'],
        })
        from services.zone_control import exclusive_start_zone
        result = exclusive_start_zone(z['id'])
        assert result is True


class TestStopZone:
    def test_stop_nonexistent(self, mock_deps):
        from services.zone_control import stop_zone
        assert stop_zone(9999) is False

    def test_stop_already_off(self, mock_deps):
        z = mock_deps['db'].create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        from services.zone_control import stop_zone
        assert stop_zone(z['id']) is True

    def test_stop_on_zone(self, mock_deps):
        srv = mock_deps['db'].create_mqtt_server({
            'name': 'Test', 'host': '127.0.0.1', 'port': 1883,
        })
        z = mock_deps['db'].create_zone({
            'name': 'Z1', 'duration': 10, 'group_id': 1,
            'topic': '/test/z1', 'mqtt_server_id': srv['id'],
        })
        mock_deps['db'].update_zone(z['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})

        from services.zone_control import stop_zone
        result = stop_zone(z['id'])
        assert result is True
        zone = mock_deps['db'].get_zone(z['id'])
        assert zone['state'] == 'off'
        assert zone['watering_start_time'] is None

    def test_stop_force(self, mock_deps):
        z = mock_deps['db'].create_zone({
            'name': 'Z1', 'duration': 10, 'group_id': 1, 'topic': '/t/1',
        })
        from services.zone_control import stop_zone
        result = stop_zone(z['id'], force=True)
        assert result is True

    def test_stop_with_water_stats(self, mock_deps):
        z = mock_deps['db'].create_zone({
            'name': 'Z1', 'duration': 10, 'group_id': 1, 'topic': '/t/1',
        })
        mock_deps['db'].update_zone(z['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})
        mock_deps['water'].summarize_run.return_value = (15.5, 2.3)

        from services.zone_control import stop_zone
        result = stop_zone(z['id'])
        assert result is True

    def test_stop_with_reason(self, mock_deps):
        z = mock_deps['db'].create_zone({
            'name': 'Z1', 'duration': 10, 'group_id': 1, 'topic': '/t/1',
        })
        mock_deps['db'].update_zone(z['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})
        from services.zone_control import stop_zone
        result = stop_zone(z['id'], reason='watchdog_cap')
        assert result is True


class TestStopAllInGroup:
    def test_stop_all(self, mock_deps):
        z1 = mock_deps['db'].create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1, 'topic': '/t/1'})
        z2 = mock_deps['db'].create_zone({'name': 'Z2', 'duration': 10, 'group_id': 1, 'topic': '/t/2'})
        mock_deps['db'].update_zone(z1['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})
        mock_deps['db'].update_zone(z2['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})

        from services.zone_control import stop_all_in_group
        stop_all_in_group(1)
        assert mock_deps['db'].get_zone(z1['id'])['state'] == 'off'
        assert mock_deps['db'].get_zone(z2['id'])['state'] == 'off'

    def test_stop_all_empty_group(self, mock_deps):
        from services.zone_control import stop_all_in_group
        stop_all_in_group(999)  # should not crash
