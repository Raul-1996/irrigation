"""asset() helper must produce URL-safe ?v= values (no spaces/parens).

Regression for the prod bug where APP_VERSION = "2.X (sha)" landed
inside <link href="...?v=2.X (sha)">, breaking CSS/JS loading.
"""

import os
import re

os.environ["TESTING"] = "1"


def test_asset_url_safe(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    matches = re.findall(r'(?:href|src)="[^"]*\?v=([^"]+)"', body)
    assert matches, "no asset URLs found — page may be broken"
    for v in matches:
        assert re.fullmatch(r"[A-Za-z0-9._+-]+", v), f"unsafe chars in asset ?v= value: {v!r}"
