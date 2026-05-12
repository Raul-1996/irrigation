"""Wave 2 F3 — unit tests for services.correlation + X-Request-ID middleware.

Covers design doc §4.5 acceptance matrix (9 tests):
    1. valid header flows into request/response + ContextVar
    2. missing header → UUID generated
    3. malicious header → sanitised (fresh UUID)
    4. too-short header → regenerated
    5. too-long header → regenerated
    6. correlation_id propagates into log records via ContextVar
    7. X-Correlation-ID alias is accepted
    8. ContextVar reset between requests (no leak)
    9. ContextVar isolation across threads (regression)
"""

import re
import threading

from services.correlation import (
    extract_or_generate,
    generate_correlation_id,
    get_correlation_id,
    reset_correlation_id,
    set_correlation_id,
    validate_correlation_id,
)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# ── 1. valid header flows through ──────────────────────────────────────────
def test_header_present_flows_into_log(client):
    """Valid X-Request-ID is accepted and echoed."""
    # /health exists on the app (see routes/system_status_api.py); use it as a
    # cheap endpoint that does not require auth.
    resp = client.get("/health", headers={"X-Request-ID": "abc123-def456"})
    assert resp.status_code in (200, 503)  # whichever the health check reports
    echoed = resp.headers.get("X-Request-ID")
    assert echoed == "abc123-def456", f"expected echo 'abc123-def456', got {echoed!r}"


# ── 2. missing header → UUID ───────────────────────────────────────────────
def test_header_missing_generates_uuid(client):
    resp = client.get("/health")
    echoed = resp.headers.get("X-Request-ID")
    assert echoed, "X-Request-ID response header missing"
    assert _UUID_RE.match(echoed), f"expected UUIDv4, got {echoed!r}"


# ── 3. malicious → sanitised ───────────────────────────────────────────────
def test_header_malicious_sanitised(client):
    resp = client.get(
        "/health",
        headers={
            "X-Request-ID": "'; DROP TABLE zones; --",
        },
    )
    echoed = resp.headers.get("X-Request-ID")
    assert echoed != "'; DROP TABLE zones; --"
    assert _UUID_RE.match(echoed), f"expected UUIDv4 fallback, got {echoed!r}"


# ── 4. too short → regenerated ─────────────────────────────────────────────
def test_header_too_short_regenerated(client):
    resp = client.get("/health", headers={"X-Request-ID": "ab"})
    echoed = resp.headers.get("X-Request-ID")
    assert echoed != "ab"
    assert _UUID_RE.match(echoed)


# ── 5. too long → regenerated ──────────────────────────────────────────────
def test_header_too_long_regenerated(client):
    too_long = "a" * 70  # > 64 chars
    resp = client.get("/health", headers={"X-Request-ID": too_long})
    echoed = resp.headers.get("X-Request-ID")
    assert echoed != too_long
    assert _UUID_RE.match(echoed)


# ── 6. propagation into log records (via ContextVar) ───────────────────────
def test_correlation_id_propagates_to_logs():
    """Setting correlation_id_var makes get_correlation_id() return it —
    this is the hook WBJsonFormatter uses to inject `correlation_id` into
    every log line without per-call plumbing.
    """
    token = set_correlation_id("propagation-test-abc")
    try:
        assert get_correlation_id() == "propagation-test-abc"
    finally:
        reset_correlation_id(token)
    # After reset, value returns to default (None — outside any request).
    assert get_correlation_id() is None


# ── 7. X-Correlation-ID alias ──────────────────────────────────────────────
def test_correlation_id_alias_header(client):
    resp = client.get("/health", headers={"X-Correlation-ID": "alias-valid-id-12345"})
    echoed = resp.headers.get("X-Request-ID")
    assert echoed == "alias-valid-id-12345", f"expected alias accepted and echoed as X-Request-ID, got {echoed!r}"


# ── 8. ContextVar resets between requests ──────────────────────────────────
def test_correlation_id_resets_between_requests(client):
    """After request 1's teardown, get_correlation_id() must NOT still see
    request 1's ID — prevents a second request on the same worker from
    inheriting the previous correlation ID in its logs.
    """
    r1 = client.get("/health", headers={"X-Request-ID": "req1-abcdefgh"})
    cid1 = r1.headers.get("X-Request-ID")
    assert cid1 == "req1-abcdefgh"
    # Between requests, nothing is bound.
    assert get_correlation_id() is None
    r2 = client.get("/health", headers={"X-Request-ID": "req2-ijklmnop"})
    cid2 = r2.headers.get("X-Request-ID")
    assert cid2 == "req2-ijklmnop"
    assert cid1 != cid2
    assert get_correlation_id() is None


# ── 9. thread isolation (regression) ───────────────────────────────────────
def test_contextvar_isolated_across_threads():
    """ContextVar guarantees per-thread isolation; regression test that each
    thread sees only its own correlation_id even when they interleave.
    """
    import time

    seen = {}
    start_gate = threading.Event()

    def _worker(name: str):
        token = set_correlation_id(f"thread-{name}-abcdefgh")
        start_gate.wait(timeout=1.0)
        # Sleep so threads overlap and could cross-contaminate if broken.
        time.sleep(0.02)
        seen[name] = get_correlation_id()
        reset_correlation_id(token)

    t1 = threading.Thread(target=_worker, args=("aaa",))
    t2 = threading.Thread(target=_worker, args=("bbb",))
    t1.start()
    t2.start()
    start_gate.set()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    assert seen.get("aaa") == "thread-aaa-abcdefgh"
    assert seen.get("bbb") == "thread-bbb-abcdefgh"


# ── Extra unit coverage of validation helpers (not counted in the 9) ───────
def test_validate_rejects_none_and_empty():
    assert validate_correlation_id(None) is None
    assert validate_correlation_id("") is None
    assert validate_correlation_id("   ") is None


def test_validate_accepts_alnum_dash_underscore():
    assert validate_correlation_id("abc-123_xyz") == "abc-123_xyz"
    assert validate_correlation_id("A" * 8) == "A" * 8
    assert validate_correlation_id("A" * 64) == "A" * 64


def test_validate_rejects_illegal_chars():
    for bad in ("abc 123xyz", "abc.123xyz", "abc;123x", "abc<123>x", "abc'123x"):
        assert validate_correlation_id(bad) is None, f"should reject {bad!r}"


def test_generate_returns_valid_uuid():
    for _ in range(5):
        cid = generate_correlation_id()
        assert _UUID_RE.match(cid)
        # Also passes the validator (length 36, alnum+dash)
        assert validate_correlation_id(cid) == cid


def test_extract_from_dict_headers():
    assert extract_or_generate({"X-Request-ID": "headervalue123"}) == "headervalue123"
    # Alias path
    assert extract_or_generate({"X-Correlation-ID": "aliasvalue123"}) == "aliasvalue123"
    # Primary wins over alias
    assert (
        extract_or_generate(
            {
                "X-Request-ID": "primaryvalue",
                "X-Correlation-ID": "aliasvalue12",
            }
        )
        == "primaryvalue"
    )
    # Neither → UUID
    assert _UUID_RE.match(extract_or_generate({}))
