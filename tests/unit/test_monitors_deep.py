"""Deep tests for services/monitors.py."""
import pytest
from unittest.mock import patch, MagicMock


class TestWaterMonitor:
    def test_water_monitor_singleton(self):
        from services.monitors import water_monitor
        assert water_monitor is not None

    def test_start_water_monitor(self):
        from services.monitors import start_water_monitor
        start_water_monitor()


class TestRainMonitor:
    def test_rain_monitor_exists(self):
        from services.monitors import rain_monitor
        assert rain_monitor is not None

    def test_rain_monitor_start_empty(self):
        from services.monitors import rain_monitor
        rain_monitor.start({})


class TestEnvMonitor:
    def test_env_monitor_exists(self):
        from services.monitors import env_monitor
        assert env_monitor is not None

    def test_env_monitor_start_empty(self):
        from services.monitors import env_monitor
        env_monitor.start({})


class TestProbeEnvValues:
    def test_probe_empty(self):
        from services.monitors import probe_env_values
        probe_env_values({})
