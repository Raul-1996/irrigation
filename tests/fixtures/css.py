"""Helpers for asserting on CSS that lives in linked stylesheets."""

import re


def fetch_inline_and_external_css(client, page_path: str) -> str:
    """Return concatenated CSS visible to the rendered page.

    The HTML response only contains <link rel="stylesheet" href=...> tags after
    CSS extraction (commit 791ff0e moved inline <style> blocks into
    static/css/*.css).  Tests that previously grep'd HTML for class names or
    media queries must now fetch every linked stylesheet via the test client
    and concatenate them with the HTML body so existing assertions still match.
    """
    with client.get(page_path) as resp:
        if resp.status_code != 200:
            return resp.data.decode("utf-8", errors="replace")
        html = resp.data.decode("utf-8", errors="replace")
    pieces = [html]
    for href in re.findall(r'<link[^>]+rel=["\']stylesheet["\'][^>]+href=["\']([^"\']+)["\']', html):
        # Normalise to a path the test client can fetch
        if href.startswith("http"):
            continue
        path = href if href.startswith("/") else "/" + href.lstrip("./")
        try:
            with client.get(path) as css_resp:
                if css_resp.status_code == 200:
                    pieces.append(css_resp.data.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
    return "\n".join(pieces)
