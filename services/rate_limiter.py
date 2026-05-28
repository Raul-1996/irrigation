"""Multi-tier login rate limiter (Issue #52).

Tracks failed login attempts per IP and per username with sliding windows.

IP tiers (cumulative within 24h):
    Tier 1: LOGIN_TIER1_MAX failures within LOGIN_TIER1_WINDOW_SEC (1h)
            → lockout for LOGIN_TIER1_LOCKOUT_SEC (~1h).
    Tier 2: LOGIN_TIER2_MAX failures within 24h
            → another LOGIN_TIER1_LOCKOUT_SEC lockout.
    Tier 3: LOGIN_TIER3_MAX failures within 24h
            → LOGIN_TIER3_LOCKOUT_SEC (24h) lockout.

Per-username throttle (independent of IP):
    LOGIN_USERNAME_MAX failures within LOGIN_USERNAME_WINDOW_SEC
            → lockout until the window elapses for that username.

A successful login resets BOTH the IP and the username buckets.

Each lockout transition emits an audit_log row (action_type=login_lockout).
"""

import logging
import threading
import time

from constants import (
    LOGIN_DAY_WINDOW_SEC,
    LOGIN_LOCKOUT_SEC,
    LOGIN_MAX_ATTEMPTS,
    LOGIN_TIER1_LOCKOUT_SEC,
    LOGIN_TIER1_MAX,
    LOGIN_TIER1_WINDOW_SEC,
    LOGIN_TIER2_MAX,
    LOGIN_TIER3_LOCKOUT_SEC,
    LOGIN_TIER3_MAX,
    LOGIN_USERNAME_MAX,
    LOGIN_USERNAME_WINDOW_SEC,
    LOGIN_WINDOW_SEC,
)

logger = logging.getLogger(__name__)


def _emit_audit(action: str, target: str, payload: dict) -> None:
    """Best-effort audit row (silent on any failure — never break the hot path)."""
    try:
        from services.audit import record_audit

        record_audit(action_type=action, source="api", target=target, payload=payload, actor="system")
    except Exception:
        logger.debug("login_lockout audit emit failed", exc_info=True)


class LoginRateLimiter:
    """Thread-safe multi-tier rate limiter for login attempts.

    Backwards-compatible with the previous single-tier API
    (check/record_failure/reset accept only `ip`); the `username` kwarg is
    optional and enables per-username throttling when supplied.
    """

    def __init__(
        self,
        max_attempts: int = LOGIN_MAX_ATTEMPTS,
        window_sec: int = LOGIN_WINDOW_SEC,
        lockout_sec: int = LOGIN_LOCKOUT_SEC,
    ) -> None:
        # Kept for back-compat — overridden by the multi-tier logic below.
        self._lock = threading.Lock()
        self._ip_attempts: dict[str, list[float]] = {}  # {ip: [ts...]}
        self._ip_lockouts: dict[str, float] = {}  # {ip: expiry_ts}
        self._user_attempts: dict[str, list[float]] = {}  # {username: [ts...]}
        self._user_lockouts: dict[str, float] = {}  # {username: expiry_ts}
        self.max_attempts = max_attempts
        self.window_sec = window_sec
        self.lockout_sec = lockout_sec

    # ── helpers ────────────────────────────────────────────────────────────
    @staticmethod
    def _prune(timestamps: list[float], cutoff: float) -> list[float]:
        return [ts for ts in timestamps if ts > cutoff]

    # ── public API ─────────────────────────────────────────────────────────
    def check(self, ip: str, username: str | None = None) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds).

        If both IP and username are throttled the longer lockout wins.
        """
        now = time.time()
        with self._lock:
            retry_after = 0

            # IP lockout
            exp = self._ip_lockouts.get(ip)
            if exp is not None:
                if now < exp:
                    retry_after = max(retry_after, int(exp - now) + 1)
                else:
                    del self._ip_lockouts[ip]

            # Username lockout
            if username:
                exp_u = self._user_lockouts.get(username)
                if exp_u is not None:
                    if now < exp_u:
                        retry_after = max(retry_after, int(exp_u - now) + 1)
                    else:
                        del self._user_lockouts[username]

            if retry_after > 0:
                return False, retry_after
            return True, 0

    def record_failure(self, ip: str, username: str | None = None) -> None:
        """Add a failure for the IP (and optional username), apply tiered lockouts."""
        now = time.time()
        with self._lock:
            # ── per-IP tracking ───────────────────────────────────────────
            day_cutoff = now - LOGIN_DAY_WINDOW_SEC
            ts = self._ip_attempts.setdefault(ip, [])
            ts.append(now)
            ts[:] = self._prune(ts, day_cutoff)
            self._ip_attempts[ip] = ts

            hour_cutoff = now - LOGIN_TIER1_WINDOW_SEC
            hour_fails = sum(1 for t in ts if t > hour_cutoff)
            day_fails = len(ts)

            # Tier 3 (longest) wins — 24h lockout.
            if day_fails >= LOGIN_TIER3_MAX:
                expiry = now + LOGIN_TIER3_LOCKOUT_SEC
                prev = self._ip_lockouts.get(ip, 0.0)
                if expiry > prev:
                    self._ip_lockouts[ip] = expiry
                    _emit_audit(
                        "login_lockout",
                        f"ip:{ip}",
                        {"tier": 3, "day_fails": day_fails, "lockout_sec": LOGIN_TIER3_LOCKOUT_SEC},
                    )
            # Tier 2: 10+ fails in 24h → 1h lockout
            elif day_fails >= LOGIN_TIER2_MAX:
                expiry = now + LOGIN_TIER1_LOCKOUT_SEC
                prev = self._ip_lockouts.get(ip, 0.0)
                if expiry > prev:
                    self._ip_lockouts[ip] = expiry
                    _emit_audit(
                        "login_lockout",
                        f"ip:{ip}",
                        {"tier": 2, "day_fails": day_fails, "lockout_sec": LOGIN_TIER1_LOCKOUT_SEC},
                    )
            # Tier 1: 5+ fails in 1h → 1h lockout
            elif hour_fails >= LOGIN_TIER1_MAX:
                expiry = now + LOGIN_TIER1_LOCKOUT_SEC
                prev = self._ip_lockouts.get(ip, 0.0)
                if expiry > prev:
                    self._ip_lockouts[ip] = expiry
                    _emit_audit(
                        "login_lockout",
                        f"ip:{ip}",
                        {"tier": 1, "hour_fails": hour_fails, "lockout_sec": LOGIN_TIER1_LOCKOUT_SEC},
                    )

            # ── per-username tracking ─────────────────────────────────────
            if username:
                u_cutoff = now - LOGIN_USERNAME_WINDOW_SEC
                u_ts = self._user_attempts.setdefault(username, [])
                u_ts.append(now)
                u_ts[:] = self._prune(u_ts, u_cutoff)
                self._user_attempts[username] = u_ts
                if len(u_ts) >= LOGIN_USERNAME_MAX:
                    expiry = now + LOGIN_USERNAME_WINDOW_SEC
                    prev = self._user_lockouts.get(username, 0.0)
                    if expiry > prev:
                        self._user_lockouts[username] = expiry
                        _emit_audit(
                            "login_lockout",
                            f"user:{username}",
                            {"per_user_fails": len(u_ts), "lockout_sec": LOGIN_USERNAME_WINDOW_SEC},
                        )

    def reset(self, ip: str, username: str | None = None) -> None:
        """Clear counters/lockouts for IP (and optional username) — called on success."""
        with self._lock:
            self._ip_attempts.pop(ip, None)
            self._ip_lockouts.pop(ip, None)
            if username:
                self._user_attempts.pop(username, None)
                self._user_lockouts.pop(username, None)


# Module-level singleton
login_limiter = LoginRateLimiter()
