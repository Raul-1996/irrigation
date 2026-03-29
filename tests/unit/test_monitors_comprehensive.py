"""Comprehensive tests for services/monitors.py — RainMonitor, EnvMonitor, WaterMonitor."""
import pytest
import os
import time
from unittest.mock import patch, MagicMock
from collections import deque

os.environ['TESTING'] = '1'


class TestRainMonitor:
    def test_init(self):
        from services.monitors import RainMonitor
        rm = RainMonitor()
        assert rm.is_rain is None
        assert rm.client is None
        assert rm.topic is None

    def test_stop_no_client(self):
        from services.monitors import RainMonitor
        rm = RainMonitor()
        rm.stop()  # should not crash

    def test_start_disabled(self):
        from services.monitors import RainMonitor
        rm = RainMonitor()
        rm.start({'enabled': False, 'topic': '/rain', 'server_id': 1})
        assert rm.client is None

    def test_start_no_topic(self):
        from services.monitors import RainMonitor
        rm = RainMonitor()
        rm.start({'enabled': True, 'topic': '', 'server_id': 1})
        assert rm.client is None

    def test_handle_payload_rain(self):
        from services.monitors import RainMonitor
        rm = RainMonitor()
        rm._cfg = {'type': 'NO'}
        with patch.object(rm, '_on_rain_start') as mock_start:
            rm._handle_payload('1')
            assert rm.is_rain is True
            mock_start.assert_called_once()

    def test_handle_payload_no_rain(self):
        from services.monitors import RainMonitor
        rm = RainMonitor()
        rm._cfg = {'type': 'NO'}
        with patch.object(rm, '_on_rain_stop') as mock_stop:
            rm._handle_payload('0')
            assert rm.is_rain is False
            mock_stop.assert_called_once()

    def test_handle_payload_nc_inverted(self):
        from services.monitors import RainMonitor
        rm = RainMonitor()
        rm._cfg = {'type': 'NC'}
        with patch.object(rm, '_on_rain_stop') as mock_stop:
            rm._handle_payload('1')  # NC: 1 = no rain
            assert rm.is_rain is False
            mock_stop.assert_called_once()

    def test_handle_payload_invalid(self):
        from services.monitors import RainMonitor
        rm = RainMonitor()
        rm._cfg = {'type': 'NO'}
        rm._handle_payload('garbage')
        assert rm.is_rain is None

    def test_on_rain_start(self, test_db):
        from services.monitors import RainMonitor
        rm = RainMonitor()
        test_db.create_group('G1')
        groups = test_db.get_groups()
        gid = groups[0]['id']
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': gid})

        with patch('services.monitors.db', test_db), \
             patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            test_db.set_group_use_rain(gid, True)
            rm._on_rain_start()
            zone = test_db.get_zone(z['id'])
            assert zone.get('postpone_reason') == 'rain'

    def test_on_rain_stop(self, test_db):
        from services.monitors import RainMonitor
        rm = RainMonitor()
        test_db.create_group('G1')
        groups = test_db.get_groups()
        gid = groups[0]['id']
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': gid})
        test_db.update_zone_postpone(z['id'], '2026-12-31 23:59:59', 'rain')
        test_db.set_group_use_rain(gid, True)

        with patch('services.monitors.db', test_db):
            rm._on_rain_stop()
            zone = test_db.get_zone(z['id'])
            assert zone.get('postpone_reason') is None or zone.get('postpone_reason') == ''


class TestEnvMonitor:
    def test_init(self):
        from services.monitors import EnvMonitor
        em = EnvMonitor()
        assert em.temp_value is None
        assert em.hum_value is None

    def test_stop_no_clients(self):
        from services.monitors import EnvMonitor
        em = EnvMonitor()
        em.stop()

    def test_start_no_mqtt(self):
        from services.monitors import EnvMonitor
        em = EnvMonitor()
        with patch('services.monitors.mqtt', None):
            em.start({'temp': {'enabled': True, 'topic': '/t', 'server_id': 1}})
            assert em.temp_client is None


class TestWaterMonitor:
    def test_init(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        assert wm._clients == {}
        assert wm._samples == {}

    def test_get_raw_pulses_empty(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        assert wm.get_raw_pulses(1) is None

    def test_get_raw_pulses_with_data(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        wm._samples[1] = deque([(time.time(), 42)], maxlen=256)
        assert wm.get_raw_pulses(1) == 42

    def test_get_pulses_at_or_before(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        now = time.time()
        wm._samples[1] = deque([
            (now - 10, 100),
            (now - 5, 110),
            (now + 5, 120),
        ], maxlen=256)
        result = wm.get_pulses_at_or_before(1, now)
        assert result == 110

    def test_get_pulses_at_or_before_empty(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        assert wm.get_pulses_at_or_before(1, time.time()) is None

    def test_get_pulses_at_or_after(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        now = time.time()
        wm._samples[1] = deque([
            (now - 10, 100),
            (now + 5, 110),
            (now + 10, 120),
        ], maxlen=256)
        result = wm.get_pulses_at_or_after(1, now)
        assert result == 110

    def test_get_pulses_at_or_after_empty(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        assert wm.get_pulses_at_or_after(1, time.time()) is None

    def test_get_flow_lpm_no_since(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        assert wm.get_flow_lpm(1, None) is None

    def test_get_flow_lpm_invalid_date(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        assert wm.get_flow_lpm(1, 'bad-date') is None

    def test_get_flow_lpm_insufficient_data(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        now = time.time()
        wm._samples[1] = deque([(now, 100)], maxlen=256)
        since = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 10))
        assert wm.get_flow_lpm(1, since) is None

    def test_get_flow_lpm_with_data(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        wm._pulse_liters[1] = 1
        now = time.time()
        wm._samples[1] = deque([
            (now - 60, 100),
            (now - 30, 105),
            (now, 110),
        ], maxlen=256)
        since = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 120))
        result = wm.get_flow_lpm(1, since)
        assert result is not None
        assert result > 0

    def test_summarize_run_no_since(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        assert wm.summarize_run(1, None) == (None, None)

    def test_summarize_run_invalid_date(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        assert wm.summarize_run(1, 'bad') == (None, None)

    def test_summarize_run_insufficient_data(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        now = time.time()
        wm._samples[1] = deque([(now, 100)], maxlen=256)
        since = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 10))
        result = wm.summarize_run(1, since)
        assert result == (0.0, 0.0)

    def test_summarize_run_with_data(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        wm._pulse_liters[1] = 10
        now = time.time()
        wm._samples[1] = deque([
            (now - 60, 100),
            (now, 110),
        ], maxlen=256)
        since = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 120))
        total_l, avg_lpm = wm.summarize_run(1, since)
        assert total_l == 100.0
        assert avg_lpm is not None and avg_lpm > 0

    def test_get_current_reading_m3(self, test_db):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        test_db.create_group('G1')
        groups = test_db.get_groups()
        gid = groups[0]['id']
        test_db.update_group_fields(gid, {
            'use_water_meter': 1,
            'water_base_value_m3': 10.0,
            'water_base_pulses': 100,
        })
        wm._pulse_liters[gid] = 1
        now = time.time()
        wm._samples[gid] = deque([(now, 150)], maxlen=256)

        with patch('services.monitors.db', test_db):
            result = wm.get_current_reading_m3(gid)
            assert result is not None
            assert result > 10.0

    def test_get_current_reading_no_group(self, test_db):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        with patch('services.monitors.db', test_db):
            result = wm.get_current_reading_m3(999)
            # Returns None if group not found, or 0.0 if defaults apply
            assert result is None or result == 0.0

    def test_start_no_mqtt(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        with patch('services.monitors.mqtt', None):
            wm.start()


class TestModuleLevelFunctions:
    def test_start_rain_monitor(self, test_db):
        with patch('services.monitors.db', test_db), \
             patch('services.monitors.rain_monitor') as mock_rm:
            from services.monitors import start_rain_monitor
            start_rain_monitor()

    def test_start_env_monitor(self):
        with patch('services.monitors.env_monitor') as mock_em:
            from services.monitors import start_env_monitor
            start_env_monitor({})

    def test_start_water_monitor(self):
        with patch('services.monitors.water_monitor') as mock_wm:
            from services.monitors import start_water_monitor
            start_water_monitor()
