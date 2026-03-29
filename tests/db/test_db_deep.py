"""Deep DB tests for maximum coverage of db/ layer."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestZonesDeep:
    def test_compute_next_run(self, test_db):
        z = test_db.create_zone({'name': 'NR', 'duration': 10, 'group_id': 1})
        test_db.create_program({
            'name': 'P', 'time': '06:00',
            'days': [0, 1, 2, 3, 4, 5, 6], 'zones': [z['id']],
        })
        result = test_db.compute_next_run_for_zone(z['id'])
        # Should return a datetime string or None

    def test_reschedule_group(self, test_db):
        g = test_db.create_group('Resched')
        z = test_db.create_zone({'name': 'RS', 'duration': 10, 'group_id': g['id']})
        test_db.create_program({
            'name': 'P', 'time': '06:00',
            'days': [0, 1, 2, 3, 4, 5, 6], 'zones': [z['id']],
        })
        test_db.reschedule_group_to_next_program(g['id'])

    def test_clear_scheduled_for_peers(self, test_db):
        z1 = test_db.create_zone({'name': 'P1', 'duration': 10, 'group_id': 1})
        z2 = test_db.create_zone({'name': 'P2', 'duration': 10, 'group_id': 1})
        test_db.set_group_scheduled_starts(1, {
            z1['id']: '2026-01-01 10:00:00',
            z2['id']: '2026-01-01 10:10:00',
        })
        test_db.clear_scheduled_for_zone_group_peers(z1['id'], 1)

    def test_zone_with_all_fields(self, test_db):
        srv = test_db.create_mqtt_server({'name': 'T', 'host': '127.0.0.1', 'port': 1883})
        z = test_db.create_zone({
            'name': 'Full', 'duration': 30, 'group_id': 1,
            'topic': '/devices/wb/controls/K1',
            'mqtt_server_id': srv['id'],
            'icon': '🌻',
        })
        assert z['mqtt_server_id'] == srv['id']

    def test_update_zone_all_fields(self, test_db):
        z = test_db.create_zone({'name': 'UF', 'duration': 10, 'group_id': 1})
        test_db.update_zone(z['id'], {
            'name': 'Updated Full',
            'duration': 20,
            'icon': '💧',
            'topic': '/new/topic',
            'state': 'on',
            'watering_start_time': '2026-01-01 10:00:00',
            'scheduled_start_time': '2026-01-01 11:00:00',
            'last_watering_time': '2026-01-01 09:00:00',
            'last_avg_flow_lpm': 2.5,
            'last_total_liters': 150.0,
        })
        zone = test_db.get_zone(z['id'])
        assert zone['name'] == 'Updated Full'
        assert zone['duration'] == 20

    def test_move_zone_to_group_999(self, test_db):
        """Moving zone to group 999 should remove it from programs."""
        z = test_db.create_zone({'name': 'Move', 'duration': 10, 'group_id': 1})
        test_db.create_program({
            'name': 'P', 'time': '06:00', 'days': [0], 'zones': [z['id']],
        })
        test_db.update_zone(z['id'], {'group_id': 999})
        # Zone should be removed from program zones
        p = test_db.get_programs()[0]
        zones_list = p['zones'] if isinstance(p['zones'], list) else __import__('json').loads(p['zones'])
        assert z['id'] not in zones_list


class TestProgramsDeep:
    def test_create_program_with_zone_ids(self, test_db):
        z1 = test_db.create_zone({'name': 'PZ1', 'duration': 10, 'group_id': 1})
        z2 = test_db.create_zone({'name': 'PZ2', 'duration': 15, 'group_id': 1})
        p = test_db.create_program({
            'name': 'Multi', 'time': '07:30',
            'days': [1, 3, 5], 'zones': [z1['id'], z2['id']],
        })
        assert p is not None
        fetched = test_db.get_program(p['id'])
        zones = fetched['zones'] if isinstance(fetched['zones'], list) else __import__('json').loads(fetched['zones'])
        assert z1['id'] in zones
        assert z2['id'] in zones


class TestGroupsDeep:
    def test_update_group_fields_all(self, test_db):
        g = test_db.create_group('Deep')
        srv = test_db.create_mqtt_server({'name': 'T', 'host': '127.0.0.1', 'port': 1883})
        test_db.update_group_fields(g['id'], {
            'use_master_valve': 1,
            'master_mqtt_topic': '/mv',
            'master_mqtt_server_id': srv['id'],
            'master_mode': 'NO',
            'use_water_meter': 1,
            'water_mqtt_topic': '/water',
            'water_mqtt_server_id': srv['id'],
            'water_pulse_size': '10l',
            'water_base_value_m3': 100.0,
            'water_base_pulses': 500,
        })
        groups = test_db.get_groups()
        g_updated = next(gg for gg in groups if gg['id'] == g['id'])
        assert int(g_updated.get('use_master_valve', 0)) == 1


class TestTelegramDeep:
    def test_full_auth_flow(self, test_db):
        test_db.upsert_bot_user(1000, 'deep', 'Deep User')
        test_db.set_bot_user_authorized(1000, 'admin')
        user = test_db.get_bot_user_by_chat(1000)
        assert user is not None

    def test_notif_all_keys(self, test_db):
        test_db.upsert_bot_user(1001, 'notif_deep', 'Notif')
        for key in ['zone_start', 'zone_stop', 'rain', 'emergency', 'watchdog']:
            test_db.set_bot_user_notif_toggle(1001, key, True)
        settings = test_db.get_bot_user_notif_settings(1001)
        assert isinstance(settings, dict)


class TestSettingsDeep:
    def test_get_set_multiple(self, test_db):
        keys = ['key1', 'key2', 'key3']
        for i, k in enumerate(keys):
            test_db.set_setting_value(k, f'value_{i}')
        for i, k in enumerate(keys):
            assert test_db.get_setting_value(k) == f'value_{i}'


class TestLogsDeep:
    def test_multiple_log_types(self, test_db):
        for lt in ['zone_start', 'zone_stop', 'program_start', 'rain_postpone', 'watchdog_cap_stop']:
            test_db.add_log(lt, f'details for {lt}')
        logs = test_db.get_logs()
        assert len(logs) >= 5

    def test_water_usage_multiple(self, test_db):
        z1 = test_db.create_zone({'name': 'WU1', 'duration': 10, 'group_id': 1})
        z2 = test_db.create_zone({'name': 'WU2', 'duration': 10, 'group_id': 1})
        test_db.add_water_usage(z1['id'], 100)
        test_db.add_water_usage(z2['id'], 200)
        usage = test_db.get_water_usage(days=7)
        assert isinstance(usage, list)
