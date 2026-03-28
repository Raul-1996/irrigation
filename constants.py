"""Centralized magic numbers and configuration constants (TASK-028).

Import from this module instead of hardcoding values throughout the codebase.
"""

# ── Zone Control ───────────────────────────────────────────────────────────
MAX_MANUAL_WATERING_MIN = 240
MASTER_VALVE_CLOSE_DELAY_SEC = 60
ANTI_RESTART_WINDOW_SEC = 5
ZONE_CAP_DEFAULT_MIN = 240
MAX_CONCURRENT_ZONES = 4

# ── MQTT ───────────────────────────────────────────────────────────────────
MQTT_CACHE_TTL_SEC = 300
GROUP_DEBOUNCE_SEC = 0.8

# ── Events / Dedup ─────────────────────────────────────────────────────────
DEDUP_SET_MAX_SIZE = 4096
DEDUP_TTL_SEC = 300

# ── Watchdog ───────────────────────────────────────────────────────────────
WATCHDOG_INTERVAL_SEC = 30

# ── Observed State Verification ────────────────────────────────────────────
OBSERVED_STATE_TIMEOUT_SEC = 10
OBSERVED_STATE_MAX_RETRIES = 3

# ── Auth / Security ────────────────────────────────────────────────────────
MIN_PASSWORD_LENGTH = 8
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SEC = 300
LOGIN_LOCKOUT_SEC = 900

# ── Upload ─────────────────────────────────────────────────────────────────
MAX_UPLOAD_SIZE_BYTES = 2 * 1024 * 1024
