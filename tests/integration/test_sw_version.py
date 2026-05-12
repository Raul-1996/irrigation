"""Issue #37: /sw.js must substitute __APP_VERSION__ placeholder."""

import os
import re

os.environ["TESTING"] = "1"


def test_sw_js_substitutes_version(client):
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "__APP_VERSION__" not in body, "placeholder must be replaced"
    assert "const CACHE_NAME = 'wb-irrigation-" in body
    # CACHE_NAME must contain only JS-string-safe chars (no quotes, no spaces).
    # Otherwise the SW silently fails to install. See Issue #37 review.
    m = re.search(r"const CACHE_NAME = '(wb-irrigation-[^']+)'", body)
    assert m, "CACHE_NAME line malformed"
    assert re.fullmatch(r"wb-irrigation-[A-Za-z0-9._-]+", m.group(1)), f"unsafe chars in CACHE_NAME: {m.group(1)!r}"


def test_sw_js_no_cache_header(client):
    resp = client.get("/sw.js")
    cc = resp.headers.get("Cache-Control", "")
    assert "no-cache" in cc, f"expected no-cache directive, got: {cc!r}"


def test_sw_js_javascript_mimetype(client):
    resp = client.get("/sw.js")
    assert resp.mimetype == "application/javascript"
