"""Local sensor state snapshot for the weather adjustment engine.

``_get_env_state`` reports the ``EnvMonitor`` temperature/humidity readings
together with per-sensor freshness flags (<``SENSOR_STALE_TIMEOUT``), so the
adjustment engine can prefer local MQTT sensors over API values.

``_get_env_state`` stays module-level in this module because
``tests/unit/test_weather_source_selection.py`` patches it by fully-qualified
name (``services.weather.merge._get_env_state``).
"""

import logging

from services.weather.models import SENSOR_STALE_TIMEOUT

logger = logging.getLogger(__name__)


def _get_env_state(now):
    # type: (float) -> Dict[str, Any]
    """Get current EnvMonitor state."""
    try:
        from services.monitors import env_monitor

        cfg = env_monitor.cfg or {}
        temp_cfg = cfg.get("temp") or {}
        hum_cfg = cfg.get("hum") or {}

        return {
            "temp_enabled": bool(temp_cfg.get("enabled")),
            "temp_value": env_monitor.temp_value,
            "temp_last_rx": env_monitor.last_temp_rx_ts,
            "temp_online": (
                bool(temp_cfg.get("enabled"))
                and env_monitor.last_temp_rx_ts > 0
                and (now - env_monitor.last_temp_rx_ts) < SENSOR_STALE_TIMEOUT
            ),
            "hum_enabled": bool(hum_cfg.get("enabled")),
            "hum_value": env_monitor.hum_value,
            "hum_last_rx": env_monitor.last_hum_rx_ts,
            "hum_online": (
                bool(hum_cfg.get("enabled"))
                and env_monitor.last_hum_rx_ts > 0
                and (now - env_monitor.last_hum_rx_ts) < SENSOR_STALE_TIMEOUT
            ),
        }
    except (ImportError, Exception) as e:
        logger.debug("EnvMonitor state unavailable: %s", e)
        return {
            "temp_enabled": False,
            "temp_value": None,
            "temp_last_rx": 0,
            "temp_online": False,
            "hum_enabled": False,
            "hum_value": None,
            "hum_last_rx": 0,
            "hum_online": False,
        }
