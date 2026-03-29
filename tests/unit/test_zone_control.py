"""Tests for zone_control service: exclusive_start, stop, peer-off, MV logic."""
import pytest
import os
import sys
from unittest.mock import patch, MagicMock

os.environ['TESTING'] = '1'


class TestExclusiveStartZone:
    def test_start_nonexistent_zone_returns_false(self, test_db):
        """Starting a zone that doesn't exist should return False."""
        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'):
            from services.zone_control import exclusive_start_zone
            result = exclusive_start_zone(9999)
            assert result is False

    def test_start_valid_zone(self, test_db):
        """Starting an existing zone should return True."""
        # Create a zone
        zone = test_db.create_zone({
            'name': 'Test Zone', 'duration': 10, 'group_id': 1,
            'topic': '/test/zone1', 'mqtt_server_id': None,
        })
        assert zone is not None

        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            from services.zone_control import exclusive_start_zone
            result = exclusive_start_zone(zone['id'])
            assert result is True

    def test_exclusive_start_stops_peers(self, test_db):
        """Starting a zone should stop others in same group."""
        import time as _time
        # Create two zones in same group
        z1 = test_db.create_zone({
            'name': 'Zone 1', 'duration': 10, 'group_id': 1,
            'topic': '/test/z1',
        })
        z2 = test_db.create_zone({
            'name': 'Zone 2', 'duration': 10, 'group_id': 1,
            'topic': '/test/z2',
        })
        # Set z1 to ON
        test_db.update_zone(z1['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})

        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            from services.zone_control import exclusive_start_zone
            result = exclusive_start_zone(z2['id'])
            assert result is True

            # Peer stop uses ThreadPoolExecutor, wait a bit
            _time.sleep(1.0)

            # Check z1 is now off or stopping (state machine transitions)
            z1_after = test_db.get_zone(z1['id'])
            assert z1_after['state'] in ('off', 'stopping'), f"Expected off/stopping, got {z1_after['state']}"


class TestStopZone:
    def test_stop_nonexistent_zone(self, test_db):
        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'):
            from services.zone_control import stop_zone
            result = stop_zone(9999)
            assert result is False

    def test_stop_already_off_zone(self, test_db):
        """Stopping an already-off zone should return True (idempotent)."""
        zone = test_db.create_zone({
            'name': 'Zone', 'duration': 10, 'group_id': 1,
        })
        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'):
            from services.zone_control import stop_zone
            result = stop_zone(zone['id'])
            assert result is True

    def test_stop_on_zone(self, test_db):
        """Stopping an ON zone should set it to off."""
        zone = test_db.create_zone({
            'name': 'Zone', 'duration': 10, 'group_id': 1,
            'topic': '/test/z1',
        })
        test_db.update_zone(zone['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})

        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            from services.zone_control import stop_zone
            result = stop_zone(zone['id'])
            assert result is True

            z_after = test_db.get_zone(zone['id'])
            assert z_after['state'] == 'off'
            assert z_after['watering_start_time'] is None


class TestStopAllInGroup:
    def test_stop_all_in_group(self, test_db):
        """Stop all zones in a group."""
        z1 = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1, 'topic': '/t/1'})
        z2 = test_db.create_zone({'name': 'Z2', 'duration': 10, 'group_id': 1, 'topic': '/t/2'})
        test_db.update_zone(z1['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})
        test_db.update_zone(z2['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})

        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            from services.zone_control import stop_all_in_group
            stop_all_in_group(1, reason='test')

            assert test_db.get_zone(z1['id'])['state'] == 'off'
            assert test_db.get_zone(z2['id'])['state'] == 'off'
