"""B-pillar coverage tests — issue #52 in-app auth (Pipeline B).

These are unit-level tests against the auth primitives. The HTTP-level tests
live in tests/api/test_auth_in_app.py.

Covers: B4, B5, B6, B7, B8, B9, B11, B13, B14.
B1, B2, B3, B10, B12 + self-demotion guard are covered in the api/ suite.
"""

import os
import sqlite3
import time

import pytest

os.environ["TESTING"] = "1"

from datetime import timedelta


# ── B5/B6/B7: configuration constants ──────────────────────────────────────


class TestConfigSecurityDefaults:
    """B5/B6/B7 — config.Config must encode Раул's decisions."""

    def test_session_cookie_secure_default_on(self, monkeypatch):
        """B5: SESSION_COOKIE_SECURE defaults to True without env override."""
        monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
        # Re-import config module so class-attr re-evaluates.
        import importlib

        import config as _cfg

        importlib.reload(_cfg)
        assert _cfg.Config.SESSION_COOKIE_SECURE is True

    def test_session_cookie_secure_env_off(self, monkeypatch):
        """B5: SESSION_COOKIE_SECURE=0 in env disables the flag."""
        import importlib

        monkeypatch.setenv("SESSION_COOKIE_SECURE", "0")
        import config as _cfg

        importlib.reload(_cfg)
        assert _cfg.Config.SESSION_COOKIE_SECURE is False

    def test_csrf_time_limit_24h(self):
        """B6: WTF_CSRF_TIME_LIMIT == 24h (was unlimited)."""
        from config import Config

        assert Config.WTF_CSRF_TIME_LIMIT == 86400

    def test_session_lifetime_365_days(self):
        """B7: PERMANENT_SESSION_LIFETIME == 365 days (Раул's call)."""
        from config import Config

        assert Config.PERMANENT_SESSION_LIFETIME == timedelta(days=365)

    def test_session_cookie_httponly_and_lax(self):
        """SameSite=Lax + HttpOnly — hardening defaults."""
        from config import Config

        assert Config.SESSION_COOKIE_HTTPONLY is True
        assert Config.SESSION_COOKIE_SAMESITE == "Lax"


# ── B4: per-IP rate limiter ────────────────────────────────────────────────


class TestIPLoginLimiter:
    """B4 — sliding window, progressive sleep, hourly alert."""

    def _fresh(self):
        from services.login_rate_limiter import IPLoginLimiter

        return IPLoginLimiter()

    def test_no_sleep_first_failure(self):
        lim = self._fresh()
        sleep_s, fm, fh = lim.record_failure("1.2.3.4")
        assert sleep_s == 0.0
        assert fm == 1
        assert fh == 1

    def test_sleep_after_5_failures(self):
        lim = self._fresh()
        for _ in range(4):
            lim.record_failure("1.2.3.4")
        sleep_s, fm, _fh = lim.record_failure("1.2.3.4")
        assert sleep_s == 2.0
        assert fm == 5

    def test_hard_sleep_after_10_failures(self):
        lim = self._fresh()
        for _ in range(9):
            lim.record_failure("1.2.3.4")
        sleep_s, fm, _fh = lim.record_failure("1.2.3.4")
        assert sleep_s == 5.0
        assert fm == 10

    def test_hard_block_after_burst(self):
        """> 10 fails in 60s → pre_check returns (False, 60)."""
        lim = self._fresh()
        for _ in range(11):
            lim.record_failure("9.9.9.9")
        allowed, retry = lim.pre_check("9.9.9.9")
        assert allowed is False
        assert retry == 60

    def test_block_at_20_per_hour(self):
        """20+ fails in an hour → pre_check returns (False, 3600)."""
        lim = self._fresh()
        # Inject 20 stale-ish failures (within hour but outside the minute window).
        ip = "8.8.8.8"
        now = time.time()
        # 20 failures, spaced out across 50 minutes (so per-minute window has < 10)
        with lim._lock:
            from collections import deque

            from services.login_rate_limiter import _Bucket

            b = _Bucket()
            b.failures = deque(now - 60 * i for i in range(20, 0, -1))
            lim._buckets[ip] = b
        allowed, retry = lim.pre_check(ip)
        assert allowed is False
        assert retry == 3600

    def test_record_success_clears_bucket(self):
        lim = self._fresh()
        for _ in range(3):
            lim.record_failure("5.5.5.5")
        lim.record_success("5.5.5.5")
        allowed, retry = lim.pre_check("5.5.5.5")
        assert allowed is True
        assert retry == 0

    def test_alert_only_after_threshold(self):
        lim = self._fresh()
        assert lim.should_alert("1.1.1.1", fails_hour=10) is False
        assert lim.should_alert("1.1.1.1", fails_hour=20) is True
        # Same IP within cooldown → no re-alert.
        assert lim.should_alert("1.1.1.1", fails_hour=25) is False

    def test_telegram_alert_calls_url(self, monkeypatch):
        """should_alert + send_telegram_alert hits the configured URL."""
        from services import login_rate_limiter as lrl

        called = {"hits": 0}

        class _FakeResp:
            def __enter__(self_inner):
                called["hits"] += 1
                return self_inner

            def __exit__(self_inner, *a):
                return False

        def _fake_urlopen(*a, **kw):
            return _FakeResp()

        monkeypatch.setenv("TELEGRAM_ALERT_URL", "http://example.invalid/notify")
        monkeypatch.setattr(lrl.urllib.request, "urlopen", _fake_urlopen)
        lrl.send_telegram_alert("4.4.4.4", 25)
        assert called["hits"] == 1

    def test_alert_without_env_logs_only(self, monkeypatch, caplog):
        """No TELEGRAM_ALERT_URL → log a warning, do not raise."""
        from services import login_rate_limiter as lrl

        monkeypatch.delenv("TELEGRAM_ALERT_URL", raising=False)
        with caplog.at_level("WARNING"):
            lrl.send_telegram_alert("4.4.4.4", 25)
        assert any("SECURITY ALERT" in r.message for r in caplog.records)


# ── B8: timing-uniform authenticate ────────────────────────────────────────


class TestAuthTimingUniform:
    """B8: authenticate() against a missing user pays the same hash cost as
    a real user with a wrong password.

    Use a freshly-created user so the stored hash uses the current pbkdf2
    iteration count (same as the dummy hash). Legacy admin/1234 inherits a
    historic 120k-iter hash from `_insert_initial_data` and gets lazily
    upgraded on first successful login — that path is exercised separately.

    Threshold: median delta < 200 ms (1 hash op ≈ 100 ms on modest CI hardware).
    """

    def test_timing_diff_within_threshold(self, app):
        from services.users_service import authenticate, create_user

        # Create a fresh user with a current-default hash.
        create_user("timing_test_user", "valid-password-1", "viewer")

        # Warm-up — first call may JIT/cache.
        authenticate("timing_test_user", "warm")
        authenticate("__nope__", "warm")

        SAMPLES = 9

        def median(xs):
            xs = sorted(xs)
            n = len(xs)
            return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

        wrong_pw_times = []
        no_user_times = []
        for _ in range(SAMPLES):
            t0 = time.perf_counter()
            authenticate("timing_test_user", "wrong-password-xx")
            wrong_pw_times.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            authenticate("does-not-exist-xx", "wrong-password-xx")
            no_user_times.append(time.perf_counter() - t0)
        diff_s = abs(median(wrong_pw_times) - median(no_user_times))
        # 200 ms — generous for CI variability. Both paths run ONE pbkdf2
        # check at 1M iterations (~100 ms), so |diff| should be << one hash.
        assert diff_s < 0.2, (
            f"timing-leak suspected: wrong-pw median={median(wrong_pw_times):.4f}s, "
            f"no-user median={median(no_user_times):.4f}s, diff={diff_s:.4f}s"
        )

    def test_lazy_rehash_upgrades_legacy_hash(self, app):
        """B8 lazy-upgrade: successful login on a legacy 120k-iter hash
        re-hashes the password at the current default."""
        from services.users_service import _DUMMY_HASH, authenticate
        from database import db

        # The admin row in the test DB inherits the legacy 120k-iter hash
        # via _insert_initial_data; verify and then exercise the upgrade.
        admin = db.users.get_by_username("admin")
        if not admin:
            pytest.skip("no seeded admin in this test DB")
        legacy = str(admin["password_hash"])
        if legacy.startswith("pbkdf2:sha256:1000000"):
            pytest.skip("admin already at current iter count")

        # Login with the seeded password.
        result = authenticate("admin", "1234")
        if result is None:
            pytest.skip("seeded admin password isn't '1234' on this run")

        # After login, hash should match the dummy's iteration count.
        admin2 = db.users.get_by_username("admin")
        new = str(admin2["password_hash"])
        dummy_iters = _DUMMY_HASH.split("$", 1)[0]
        new_iters = new.split("$", 1)[0]
        assert new_iters == dummy_iters, f"expected {dummy_iters!r}, got {new_iters!r}"


# ── B9: username regex ────────────────────────────────────────────────────


class TestUsernameRegex:
    """B9: server-side username allowlist `^[a-zA-Z0-9_.\\-]{1,32}$`."""

    @pytest.mark.parametrize(
        "name", ["admin", "Poliv", "user.1", "user_2", "user-3", "a", "A1.B-2_C"]
    )
    def test_accept_valid(self, name):
        from services.users_service import validate_username

        ok, msg = validate_username(name)
        assert ok is True, msg

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "<script>",
            "user space",
            "юзер",  # cyrillic
            "a" * 33,
            "../../etc/passwd",
            "user@host",
            "user/path",
        ],
    )
    def test_reject_invalid(self, name):
        from services.users_service import validate_username

        ok, _msg = validate_username(name)
        assert ok is False


# ── B11: audit IP — no XFF trust ───────────────────────────────────────────


class TestAuditIPDoesNotTrustXFF:
    """B11: services.audit._resolve_ip must NOT read X-Forwarded-For.

    ProxyFix (TRUSTED_PROXY=1) handles that one rewrite for the whole WSGI
    pipeline; the audit helper must use remote_addr as-is.
    """

    def test_resolve_ip_ignores_xff(self):
        from services.audit import _resolve_ip

        class FakeHeaders(dict):
            def get(self, k, default=None):
                return dict.get(self, k, default)

        class FakeReq:
            remote_addr = "10.0.0.5"
            headers = FakeHeaders({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})

        assert _resolve_ip(FakeReq()) == "10.0.0.5"

    def test_resolve_ip_returns_remote_addr(self):
        from services.audit import _resolve_ip

        class FakeReq:
            remote_addr = "127.0.0.1"
            headers = {}

        assert _resolve_ip(FakeReq()) == "127.0.0.1"


# ── B13: seed migration is atomic ─────────────────────────────────────────


class TestSeedDefaultUsersAtomic:
    """B13: if seed of Poliv fails after admin was inserted, both roll back."""

    def test_rollback_on_second_insert_failure(self, tmp_path):
        """Patch second INSERT to raise — verify users table ends empty."""
        from db.migrations import MigrationRunner

        db_path = str(tmp_path / "seed_test.db")
        runner = MigrationRunner(db_path)
        # Apply the create_users migration first.
        with sqlite3.connect(db_path) as conn:
            runner._migrate_create_users(conn)

        # Wrap a real sqlite3.Connection in a proxy whose `execute` we control.
        class _Proxy:
            def __init__(self, real):
                self._real = real
                self._insert_count = 0

            def __getattr__(self, name):
                return getattr(self._real, name)

            def __enter__(self):
                self._real.__enter__()
                return self

            def __exit__(self, *args):
                return self._real.__exit__(*args)

            def execute(self, sql, *args, **kwargs):
                if isinstance(sql, str) and sql.startswith("INSERT INTO users"):
                    self._insert_count += 1
                    if self._insert_count == 2:
                        raise sqlite3.IntegrityError("simulated failure")
                return self._real.execute(sql, *args, **kwargs)

        with sqlite3.connect(db_path) as real_conn:
            proxy = _Proxy(real_conn)
            with pytest.raises(sqlite3.IntegrityError):
                runner._migrate_seed_default_users(proxy)

        # Verify atomicity — both rows should be rolled back.
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM users")
            (count,) = cur.fetchone()
        assert count == 0, "seed must be atomic — partial admin insert leaked"

    def test_successful_seed_inserts_both(self, tmp_path):
        """Sanity: happy path inserts both admin and Poliv."""
        from db.migrations import MigrationRunner

        db_path = str(tmp_path / "seed_ok.db")
        runner = MigrationRunner(db_path)
        with sqlite3.connect(db_path) as conn:
            runner._migrate_create_users(conn)
            runner._migrate_seed_default_users(conn)
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT username, role, is_active FROM users ORDER BY id"
            ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "admin"
        assert rows[0][1] == "admin"
        assert rows[1][0] == "Poliv"
        assert rows[1][1] == "viewer"


# ── B14: CSV injection guard ──────────────────────────────────────────────


class TestCSVInjectionGuard:
    """B14: dangerous prefixes (= + - @ \\t \\r) must be neutralised."""

    @pytest.mark.parametrize(
        "raw, expected_first_char",
        [
            ("=cmd|'/c calc'!A1", "'"),
            ("+1+1+cmd", "'"),
            ("-2+3", "'"),
            ("@SUM(1+1)*cmd", "'"),
            ("\t=evil", "'"),
            ("\r=evil", "'"),
            ("normal text", "n"),
            ("", ""),
        ],
    )
    def test_csv_safe_prefix(self, raw, expected_first_char):
        from routes.zones_history_api import _csv_safe

        out = _csv_safe(raw)
        if expected_first_char == "":
            assert out == ""
        else:
            assert out[0] == expected_first_char
