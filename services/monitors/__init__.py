# services/monitors — backward-compatible re-exports
# Original monitors.py was split into submodules; all public names stay importable from here.

from database import db  # noqa: F401 — tests patch services.monitors.db

try:
    import paho.mqtt.client as mqtt  # noqa: F401 — tests patch services.monitors.mqtt
except ImportError:
    mqtt = None

from services.monitors import rain_monitor as _rain_mod
from services.monitors import env_monitor as _env_mod
from services.monitors import water_monitor as _water_mod

from services.monitors.rain_monitor import RainMonitor, rain_monitor, start_rain_monitor
from services.monitors.env_monitor import EnvMonitor, env_monitor, start_env_monitor, probe_env_values
from services.monitors.water_monitor import WaterMonitor, water_monitor, start_water_monitor

__all__ = [
    'RainMonitor', 'rain_monitor', 'start_rain_monitor',
    'EnvMonitor', 'env_monitor', 'start_env_monitor', 'probe_env_values',
    'WaterMonitor', 'water_monitor', 'start_water_monitor',
    'db', 'mqtt',
]

# Allow tests to patch services.monitors.db and have it propagate to submodules.
# We override __setattr__ so that `patch('services.monitors.db', ...)` also patches submodule references.
import sys as _sys

class _MonitorsModule(_sys.modules[__name__].__class__):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name == 'db':
            _rain_mod.db = value
            _env_mod.db = value
            _water_mod.db = value
        elif name == 'mqtt':
            _rain_mod.mqtt = value
            _env_mod.mqtt = value
            _water_mod.mqtt = value

_sys.modules[__name__].__class__ = _MonitorsModule
