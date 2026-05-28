"""Issue #50: /static/* must not emit Set-Cookie / Cache-Control: no-cache.

Root cause: `_auth_before_request` did `session["role"] = "guest"`
unconditionally on every request, including /static/*.webp. Flask saw a
dirty session → emitted `Set-Cookie: session=...` and `Cache-Control:
no-cache`, defeating browser caching of map images and other static assets.

Fix (variant B): skip the entire auth hook for `/static/*` paths — they
require no auth and need no session writes.
"""


def _hdr_lower(headers):
    """Flask test response headers → dict with lowercase keys."""
    return {k.lower(): v for k, v in headers.items()}


def test_static_first_request_no_session_writes(client):
    """First GET to /static/* must not set a session cookie."""
    r = client.get("/static/css/base.css")
    assert r.status_code == 200
    h = _hdr_lower(r.headers)
    assert "set-cookie" not in h, f"Set-Cookie leaked on /static/*: {h.get('set-cookie')!r}"
    cc = h.get("cache-control", "")
    assert "no-cache" not in cc, f"no-cache must not be on /static/*: {cc!r}"
    # Acceptance #1: positive cache directive.
    assert "max-age=604800" in cc, f"expected max-age=604800, got: {cc!r}"
    assert "public" in cc, f"expected public, got: {cc!r}"


def test_static_second_request_no_set_cookie(client):
    """Repeat request must not produce Set-Cookie (acceptance criterion #3)."""
    client.get("/static/css/base.css")
    r2 = client.get("/static/css/base.css")
    assert r2.status_code == 200
    h = _hdr_lower(r2.headers)
    assert "set-cookie" not in h, f"Set-Cookie on 2nd /static/* request: {h.get('set-cookie')!r}"
    cc = h.get("cache-control", "")
    assert "no-cache" not in cc, f"no-cache on 2nd /static/* request: {cc!r}"


def test_non_static_path_still_processed_by_auth_hook(client):
    """Regression guard: non-/static/ paths must still flow through auth hook.

    `/api/status` is a public-GET endpoint — under guest role it returns 200,
    and the very first request should bind session["role"]="guest" (which
    emits Set-Cookie). This proves the early-return is scoped to /static/* only.
    """
    r = client.get("/api/status")
    # 200 (public status) or 500 if MQTT not configured — both OK, we only
    # care that the auth hook ran (session was touched on first request).
    assert r.status_code in (200, 500)
    h = _hdr_lower(r.headers)
    # First-ever API request from a fresh client → Flask writes the session
    # cookie because session["role"] = "guest" makes it dirty.
    assert "set-cookie" in h, "session must be initialised on first /api/* request"
