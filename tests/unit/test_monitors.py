"""Tests for monitors: rain, env, water."""
import pytest
import os
from unittest.mock import patch, MagicMock

os.environ['TESTING'] = '1'


class TestRainMonitor:
    def test_initial_state(self):
        from services.monitors import RainMonitor
        rm = RainMonitor()
        assert rm.is_rain is None
        assert rm.client is None

    def test_handle_payload_rain_on(self):
        from services.monitors import RainMonitor
        rm = RainMonitor()
        rm._cfg = {'type': 'NO'}
        with patch.object(rm, '_on_rain_start'):
            rm._handle_payload('1')
            assert rm.is_rain is True

    def test_handle_payload_rain_off(self):
        from services.monitors import RainMonitor
        rm = RainMonitor()
        rm._cfg = {'type': 'NO'}
        with patch.object(rm, '_on_rain_stop'):
            rm._handle_payload('0')
            assert rm.is_rain is False

    def test_handle_payload_nc_inverted(self):
        """NC sensor type should invert the signal."""
        from services.monitors import RainMonitor
        rm = RainMonitor()
        rm._cfg = {'type': 'NC'}
        with patch.object(rm, '_on_rain_stop'):
            rm._handle_payload('1')
            assert rm.is_rain is False

    def test_handle_payload_garbage_ignored(self):
        """Garbage payloads should be ignored."""
        from services.monitors import RainMonitor
        rm = RainMonitor()
        rm._cfg = {'type': 'NO'}
        rm._handle_payload('garbage')
        assert rm.is_rain is None

    def test_stop_idempotent(self):
        from services.monitors import RainMonitor
        rm = RainMonitor()
        rm.stop()  # Should not raise even without client


class TestEnvMonitor:
    def test_initial_state(self):
        from services.monitors import EnvMonitor
        em = EnvMonitor()
        assert em.temp_value is None
        assert em.hum_value is None

    def test_stop_idempotent(self):
        from services.monitors import EnvMonitor
        em = EnvMonitor()
        em.stop()  # Should not raise


class TestWaterMonitor:
    def test_get_raw_pulses_no_data(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        assert wm.get_raw_pulses(1) is None

    def test_summarize_run_no_data(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        total, avg = wm.summarize_run(1, None)
        assert total is None
        assert avg is None

    def test_summarize_run_empty_since(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        total, avg = wm.summarize_run(1, '')
        assert total is None
        assert avg is None

    def test_get_pulses_at_or_before_empty(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        assert wm.get_pulses_at_or_before(1, 0) is None

    def test_get_pulses_at_or_after_empty(self):
        from services.monitors import WaterMonitor
        wm = WaterMonitor()
        assert wm.get_pulses_at_or_after(1, 0) is None
