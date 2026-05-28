"""Per-IP login rate limiter — B4 (Pipeline B, issue #52).

Design (Раул's decisions, audits/2026-05-28-security/findings.md §B4):
  * Per-IP only — NO per-username bucket. A per-username bucket would let any
    scanner DoS the "admin" login by spamming wrong passwords.
  * Sliding window: 5 fail/min, 20 fail/hour. Above 20/hour → 429 + alert.
  * Progressive delay BEFORE the 4xx response, applied inside the request
    thread so each attempt holds a worker:
        - after 5 fails in the window → 2 s sleep
        - after 10 fails in the window → 5 s sleep
  * Telegram alert via env TELEGRAM_ALERT_URL once per hour per IP when the
    20/hour threshold is crossed. Empty env → log-only warning.

This module is intentionally separate from services/rate_limiter.py (the
generic in-memory limiter used by older endpoints) so old tests against
that limiter keep passing while the new auth flow gets the policy above.
"""

import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

logger = logging.getLogger(__name__)


# Tunables — frozen by Раул's decision (findings.md §B4).
_PER_MIN_LIMIT = 5
_PER_HOUR_LIMIT = 20
_DELAY_AFTER_N = 5
_DELAY_AFTER_N_SECONDS = 2.0
_HARD_DELAY_AFTER_N = 10
_HARD_DELAY_SECONDS = 5.0
_ALERT_THRESHOLD = 20  # fail per hour per IP
_ALERT_COOLDOWN_SECONDS = 3600


class _Bucket:
    __slots__ = ("failures",)

    def __init__(self) -> None:
        # Timestamps of failed logins, oldest → newest. Pruned per call.
        self.failures: deque[float] = deque()


class IPLoginLimiter:
    """Thread-safe per-IP sliding-window limiter for /api/login.

    All public methods are O(1) amortised — the bucket is pruned of entries
    older than 1 hour on every call, so memory stays bounded under sustained
    attack.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        # Last alert ts per IP — avoid alert spam during a burst.
        self._last_alert: dict[str, float] = {}

    # ── pre-flight ────────────────────────────────────────────────────────
    def pre_check(self, ip: str) -> tuple[bool, int]:
        """Return (allowed, retry_after_sec).

        Caller MUST reject with 429 when allowed is False — without running
        authenticate() — so brute force can't burn CPU on bcrypt.
        This method does NOT sleep; the progressive penalty lives in
        record_failure so it applies AFTER the wrong-password check.
        """
        now = time.time()
        with self._lock:
            b = self._buckets.get(ip)
            if b is None:
                return True, 0
            self._prune(b, now)
            if self._fail_count(b, now, window=60) >= _PER_MIN_LIMIT * 2:
                # Hard block: > 10 fails in last minute (= more than 2× the
                # progressive-delay threshold). Forces the attacker to wait
                # before they can even attempt the next password.
                return False, 60
            if len(b.failures) >= _PER_HOUR_LIMIT:
                return False, 3600
        return True, 0

    # ── recording ─────────────────────────────────────────────────────────
    def record_success(self, ip: str) -> None:
        """Successful login clears the bucket so a legitimate user who
        mistyped a few times doesn't trip the limiter next session."""
        with self._lock:
            self._buckets.pop(ip, None)

    def record_failure(self, ip: str) -> tuple[float, int, int]:
        """Append a failure timestamp and return the penalty to apply.

        Returns (sleep_sec, fails_in_last_minute, fails_in_last_hour).
        The route is expected to ``time.sleep(sleep_sec)`` itself so the
        attacker pays the cost in their own request thread.
        """
        now = time.time()
        with self._lock:
            b = self._buckets.setdefault(ip, _Bucket())
            b.failures.append(now)
            self._prune(b, now)
            fails_hour = len(b.failures)
            fails_min = self._fail_count(b, now, window=60)
        if fails_min >= _HARD_DELAY_AFTER_N:
            sleep_sec = _HARD_DELAY_SECONDS
        elif fails_min >= _DELAY_AFTER_N:
            sleep_sec = _DELAY_AFTER_N_SECONDS
        else:
            sleep_sec = 0.0
        return sleep_sec, fails_min, fails_hour

    def should_alert(self, ip: str, fails_hour: int) -> bool:
        """True iff (a) threshold crossed AND (b) cooldown elapsed.

        Idempotently records the alert timestamp so the caller can fire
        once per cooldown without extra bookkeeping.
        """
        if fails_hour < _ALERT_THRESHOLD:
            return False
        now = time.time()
        with self._lock:
            last = self._last_alert.get(ip, 0.0)
            if now - last < _ALERT_COOLDOWN_SECONDS:
                return False
            self._last_alert[ip] = now
        return True

    # ── housekeeping ──────────────────────────────────────────────────────
    def reset(self, ip: str) -> None:
        with self._lock:
            self._buckets.pop(ip, None)
            self._last_alert.pop(ip, None)

    # ── internal ──────────────────────────────────────────────────────────
    @staticmethod
    def _prune(b: _Bucket, now: float) -> None:
        cutoff = now - 3600
        while b.failures and b.failures[0] < cutoff:
            b.failures.popleft()

    @staticmethod
    def _fail_count(b: _Bucket, now: float, window: int) -> int:
        cutoff = now - window
        n = 0
        # b.failures is sorted oldest→newest; scan from the right.
        for ts in reversed(b.failures):
            if ts < cutoff:
                break
            n += 1
        return n


def send_telegram_alert(ip: str, fails_hour: int) -> None:
    """Best-effort POST to TELEGRAM_ALERT_URL. Empty env → log warning only.

    Exposed at module level so routes can call it inside their own
    try/except — alerts must NEVER raise into the auth flow.
    """
    message = f"wb-irrigation: brute-force suspected — {fails_hour} failed logins from {ip} in the last hour"
    url = os.environ.get("TELEGRAM_ALERT_URL", "").strip()
    if not url:
        logger.warning("SECURITY ALERT (no TELEGRAM_ALERT_URL set): %s", message)
        return
    try:
        data = f"text={urllib.parse.quote(message)}".encode("ascii")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # URL comes from operator-set TELEGRAM_ALERT_URL env; bandit B310.
        with urllib.request.urlopen(req, timeout=5):  # nosec B310
            pass
    except Exception as e:
        logger.error("Telegram alert POST failed: %s", e)


# Module-level singleton — every route imports this same instance.
ip_login_limiter = IPLoginLimiter()
