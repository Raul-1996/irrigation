"""Tests for rate limiter logic (Issue #52: multi-tier).

Tier 1 (IP):  LOGIN_TIER1_MAX fails / 1h         → 1h lockout
Tier 2 (IP):  LOGIN_TIER2_MAX fails / 24h        → 1h lockout
Tier 3 (IP):  LOGIN_TIER3_MAX fails / 24h        → 24h lockout
Per-username: LOGIN_USERNAME_MAX fails / 1h      → 1h username lockout

A successful login resets both the IP and the username bucket.
"""

import time

from constants import (
    LOGIN_TIER1_MAX,
    LOGIN_TIER3_MAX,
    LOGIN_USERNAME_MAX,
)
from services.rate_limiter import LoginRateLimiter


class TestLoginRateLimiter:
    def test_initial_allow(self):
        rl = LoginRateLimiter()
        allowed, retry = rl.check("1.2.3.4")
        assert allowed is True
        assert retry == 0

    def test_allows_under_limit(self):
        rl = LoginRateLimiter()
        for _ in range(LOGIN_TIER1_MAX - 1):
            rl.record_failure("1.2.3.4")
        allowed, _ = rl.check("1.2.3.4")
        assert allowed is True

    def test_lockout_at_tier1_limit(self):
        rl = LoginRateLimiter()
        for _ in range(LOGIN_TIER1_MAX):
            rl.record_failure("1.2.3.4")
        allowed, retry = rl.check("1.2.3.4")
        assert allowed is False
        assert retry > 0

    def test_reset_clears_failures(self):
        rl = LoginRateLimiter()
        for _ in range(LOGIN_TIER1_MAX):
            rl.record_failure("1.2.3.4")
        rl.reset("1.2.3.4")
        allowed, _ = rl.check("1.2.3.4")
        assert allowed is True

    def test_different_ips_independent(self):
        rl = LoginRateLimiter()
        for _ in range(LOGIN_TIER1_MAX):
            rl.record_failure("1.1.1.1")
        allowed_locked, _ = rl.check("1.1.1.1")
        allowed_other, _ = rl.check("2.2.2.2")
        assert allowed_locked is False
        assert allowed_other is True

    def test_lockout_expires(self):
        rl = LoginRateLimiter()
        for _ in range(LOGIN_TIER1_MAX):
            rl.record_failure("1.2.3.4")
        allowed, _ = rl.check("1.2.3.4")
        assert allowed is False
        # Manually expire the lockout.
        rl._ip_lockouts["1.2.3.4"] = time.time() - 1
        allowed, _ = rl.check("1.2.3.4")
        assert allowed is True

    def test_tier3_24h_lockout(self):
        """LOGIN_TIER3_MAX fails inside 24h triggers the long (24h) lockout."""
        rl = LoginRateLimiter()
        for _ in range(LOGIN_TIER3_MAX):
            rl.record_failure("10.0.0.1")
        allowed, retry = rl.check("10.0.0.1")
        assert allowed is False
        # >> 1h: must be the 24h tier.
        assert retry > 3600

    def test_per_username_lockout_crosses_ips(self):
        """The username bucket is independent of the IP bucket."""
        rl = LoginRateLimiter()
        for _ in range(LOGIN_USERNAME_MAX):
            rl.record_failure("1.1.1.1", username="bob")
        # Different IP, same username → still locked.
        allowed, retry = rl.check("2.2.2.2", username="bob")
        assert allowed is False
        assert retry > 0
        # Different username from the same IP → IP-tier still applies.
        allowed_other, _ = rl.check("2.2.2.2", username="alice")
        assert allowed_other is True
