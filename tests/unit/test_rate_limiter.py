"""Tests for rate limiter logic."""
import pytest
import time
from services.rate_limiter import LoginRateLimiter


class TestLoginRateLimiter:
    def test_initial_allow(self):
        rl = LoginRateLimiter(max_attempts=5, window_sec=60, lockout_sec=30)
        allowed, retry = rl.check("1.2.3.4")
        assert allowed is True
        assert retry == 0

    def test_allows_under_limit(self):
        rl = LoginRateLimiter(max_attempts=5, window_sec=60, lockout_sec=30)
        for _ in range(4):
            rl.record_failure("1.2.3.4")
        allowed, _ = rl.check("1.2.3.4")
        assert allowed is True

    def test_lockout_at_limit(self):
        rl = LoginRateLimiter(max_attempts=3, window_sec=60, lockout_sec=30)
        for _ in range(3):
            rl.record_failure("1.2.3.4")
        allowed, retry = rl.check("1.2.3.4")
        assert allowed is False
        assert retry > 0

    def test_reset_clears_failures(self):
        rl = LoginRateLimiter(max_attempts=3, window_sec=60, lockout_sec=30)
        for _ in range(3):
            rl.record_failure("1.2.3.4")
        rl.reset("1.2.3.4")
        allowed, _ = rl.check("1.2.3.4")
        assert allowed is True

    def test_different_ips_independent(self):
        rl = LoginRateLimiter(max_attempts=2, window_sec=60, lockout_sec=30)
        for _ in range(2):
            rl.record_failure("1.1.1.1")
        allowed_locked, _ = rl.check("1.1.1.1")
        allowed_other, _ = rl.check("2.2.2.2")
        assert allowed_locked is False
        assert allowed_other is True

    def test_lockout_expires(self):
        rl = LoginRateLimiter(max_attempts=1, window_sec=60, lockout_sec=0)
        rl.record_failure("1.2.3.4")
        # Force lockout
        allowed, _ = rl.check("1.2.3.4")
        assert allowed is False
        # Manually expire the lockout
        rl._lockouts["1.2.3.4"] = time.time() - 1
        allowed, _ = rl.check("1.2.3.4")
        assert allowed is True

    def test_11th_request_429_scenario(self):
        """11th request should be blocked (simulating 429)."""
        rl = LoginRateLimiter(max_attempts=10, window_sec=300, lockout_sec=60)
        for _ in range(10):
            rl.record_failure("10.0.0.1")
        allowed, retry = rl.check("10.0.0.1")
        assert allowed is False
        assert retry > 0
