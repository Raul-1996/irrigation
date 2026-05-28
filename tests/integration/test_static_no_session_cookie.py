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


def test_static_webp_content_type(client):
    """Acceptance #1: /static/*.webp must be served with Content-Type: image/webp.

    Python 3.11 stdlib knows this mapping, but we register it explicitly
    so the behaviour holds on minimal containers / older mime.types.
    """
    r = client.get("/static/media/zones/OLD/ZONE_1.webp")
    assert r.status_code == 200
    ct = r.headers.get("Content-Type", "")
    assert "image/webp" in ct, f"expected image/webp, got: {ct!r}"


def test_non_static_path_still_processed_by_auth_hook(client):
    """Regression guard: /static/* early-return must NOT short-circuit /api/* paths.

    After Issue #52 the auth hook no longer writes session["role"]="guest" on
    every request (no implicit guest role). The /static/* skip from #50 must
    still leave the /api/* auth/role gates working. We verify the hook is
    reachable by hitting an admin-only mutating endpoint from an anonymous
    client and expecting a 401/403 (NOT 200, which would indicate the hook
    was skipped).
    """
    r = client.post("/api/zones/1/start")
    assert r.status_code in (401, 403, 404), (
        f"anonymous POST must be blocked by auth hook, got {r.status_code}"
    )
