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

# ── Multi-tier login rate limiting (Issue #52) ─────────────────────────────
# Tier 1: 5 failures within 1 hour per IP → lockout until the hour window
#         elapses.
# Tier 2: if the IP keeps trying and accumulates 10 failures within 24h
#         (i.e. another 5 after a tier-1 lockout), trigger another hour
#         lockout.
# Tier 3: 15+ failures within 24h → 24h lockout.
LOGIN_TIER1_MAX = 5
LOGIN_TIER1_WINDOW_SEC = 3600  # 1 hour
LOGIN_TIER1_LOCKOUT_SEC = 3600  # lock until window end (~1h)
LOGIN_TIER2_MAX = 10
LOGIN_TIER3_MAX = 15
LOGIN_DAY_WINDOW_SEC = 86400  # 24h
LOGIN_TIER3_LOCKOUT_SEC = 86400  # 24h lockout

# Per-username throttle (independent of IP — slows username enumeration
# distributed across IPs).
LOGIN_USERNAME_MAX = 10
LOGIN_USERNAME_WINDOW_SEC = 3600

# ── Upload ─────────────────────────────────────────────────────────────────
MAX_UPLOAD_SIZE_BYTES = 2 * 1024 * 1024
