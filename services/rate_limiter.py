"""IP-based login rate limiter (TASK-009).

Tracks failed login attempts per IP address and enforces lockout
after too many failures within a sliding time window.
"""

import threading
import time
from typing import Tuple


class LoginRateLimiter:
    """Thread-safe IP-based rate limiter for login attempts."""

    def __init__(self, max_attempts: int = 5, window_sec: int = 300, lockout_sec: int = 900):
        """
        Args:
            max_attempts: Maximum failed attempts before lockout.
            window_sec: Sliding window in seconds to count failures.
            lockout_sec: How long (seconds) to lock out an IP after exceeding max_attempts.
        """
        self._lock = threading.Lock()
        self._attempts: dict[str, list[float]] = {}  # {ip: [timestamps of failures]}
        self._lockouts: dict[str, float] = {}  # {ip: lockout_expiry_timestamp}
        self.max_attempts = max_attempts
        self.window_sec = window_sec
        self.lockout_sec = lockout_sec

    def check(self, ip: str) -> Tuple[bool, int]:
        """Check if an IP is allowed to attempt login.

        Returns:
            (allowed, retry_after_seconds)
            If allowed is True, retry_after is 0.
            If allowed is False, retry_after indicates seconds until lockout expires.
        """
        now = time.time()
        with self._lock:
            # Check active lockout
            lockout_expiry = self._lockouts.get(ip)
            if lockout_expiry is not None:
                if now < lockout_expiry:
                    return False, int(lockout_expiry - now) + 1
                else:
                    # Lockout expired — clear it
                    del self._lockouts[ip]
                    self._attempts.pop(ip, None)

            # Count recent failures within window
            timestamps = self._attempts.get(ip, [])
            cutoff = now - self.window_sec
            recent = [ts for ts in timestamps if ts > cutoff]
            self._attempts[ip] = recent

            if len(recent) >= self.max_attempts:
                # Impose lockout
                expiry = now + self.lockout_sec
                self._lockouts[ip] = expiry
                return False, self.lockout_sec

            return True, 0

    def record_failure(self, ip: str) -> None:
        """Record a failed login attempt for the given IP."""
        now = time.time()
        with self._lock:
            if ip not in self._attempts:
                self._attempts[ip] = []
            self._attempts[ip].append(now)
            # Prune old entries
            cutoff = now - self.window_sec
            self._attempts[ip] = [ts for ts in self._attempts[ip] if ts > cutoff]

    def reset(self, ip: str) -> None:
        """Reset failure count and lockout for the given IP (e.g., after successful login)."""
        with self._lock:
            self._attempts.pop(ip, None)
            self._lockouts.pop(ip, None)


# Module-level singleton
login_limiter = LoginRateLimiter()
