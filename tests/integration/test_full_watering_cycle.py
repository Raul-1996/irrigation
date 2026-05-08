"""Integration test: full watering cycle start → run → stop."""
import pytest
import os
import time as _time
from datetime import datetime
from unittest.mock import patch, MagicMock

os.environ['TESTING'] = '1'


class TestFullWateringCycle:
    def test_start_water_stop_cycle(self, test_db):
        """Full cycle: create zone → start → verify ON → stop → verify OFF."""
        zone = test_db.create_zone({
            'name': 'Cycle Zone', 'duration': 1, 'group_id': 1,
            'topic': '/test/cycle', 'mqtt_server_id': None,
        })

        # Start
        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            from services.zone_control import exclusive_start_zone, stop_zone

            result = exclusive_start_zone(zone['id'])
            assert result is True

            z = test_db.get_zone(zone['id'])
            assert z['state'] in ('on', 'starting')
            assert z['watering_start_time'] is not None
            start_iso = z['watering_start_time']

            # Issue #2: ensure end-time is strictly after start-time after
            # a real start→sleep→stop cycle.
            _time.sleep(1.1)

            # Stop
            result = stop_zone(zone['id'], reason='test')
            assert result is True

            z = test_db.get_zone(zone['id'])
            assert z['state'] == 'off'
            assert z['watering_start_time'] is None
            # last_watering_time is now derived from zone_runs.end_utc
            # and injected by get_zone — must be present and strictly
            # later than the start.
            last_str = test_db.get_last_watering_time(int(zone['id']))
            assert last_str is not None
            assert z['last_watering_time'] == last_str, (
                'get_zone should inject the same value get_last_watering_time returns'
            )
            fmt = '%Y-%m-%d %H:%M:%S'
            assert datetime.strptime(last_str, fmt) > \
                   datetime.strptime(start_iso, fmt), (
                'last_watering_time must be the END of watering, not the start'
            )

    def test_sequential_group_watering(self, test_db):
        """Start zone 1 → start zone 2 → zone 1 should stop (exclusive)."""
        z1 = test_db.create_zone({
            'name': 'Seq 1', 'duration': 10, 'group_id': 1,
            'topic': '/test/s1',
        })
        z2 = test_db.create_zone({
            'name': 'Seq 2', 'duration': 10, 'group_id': 1,
            'topic': '/test/s2',
        })
        
        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            from services.zone_control import exclusive_start_zone
            
            exclusive_start_zone(z1['id'])
            assert test_db.get_zone(z1['id'])['state'] in ('on', 'starting')
            
            exclusive_start_zone(z2['id'])
            import time as _t
            _t.sleep(1.0)
            assert test_db.get_zone(z2['id'])['state'] in ('on', 'starting')
            # z1 should be off or stopping (peer stop is async via ThreadPool)
            assert test_db.get_zone(z1['id'])['state'] in ('off', 'stopping')
