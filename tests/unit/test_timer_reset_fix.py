"""
TDD tests for timer-reset-fix (spec: timer-reset-fix-spec.md).

Tests verify:
- JS content patterns (grep-based) in status.js
- Backend rate limiter skip for next-watering-bulk
- Template sanity
- DOM patching (updateZoneCards, updateStatusDisplay with gcard-*)
"""
import os
import re
import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATUS_JS = os.path.join(BASE_DIR, 'static', 'js', 'status.js')
STATUS_HTML = os.path.join(BASE_DIR, 'templates', 'status.html')
APP_PY = os.path.join(BASE_DIR, 'app.py')


@pytest.fixture(scope='module')
def status_js_content():
    with open(STATUS_JS, 'r', encoding='utf-8') as f:
        return f.read()


@pytest.fixture(scope='module')
def app_py_content():
    with open(APP_PY, 'r', encoding='utf-8') as f:
        return f.read()


@pytest.fixture(scope='module')
def status_html_content():
    with open(STATUS_HTML, 'r', encoding='utf-8') as f:
        return f.read()


# ── JS content tests ─────────────────────────────────────────────────────

class TestPollingInterval:
    """Fix 2: Polling intervals should be split and >= 10s."""

    def test_no_5s_combined_polling(self, status_js_content):
        """setInterval with 5000 for combined loadStatusData+loadZonesData must NOT exist."""
        combined_5s = re.findall(
            r'setInterval\s*\([^)]*(?:loadStatusData|loadZonesData)[^)]*,\s*5000\s*\)',
            status_js_content
        )
        block_intervals = re.findall(
            r'setInterval\s*\(\s*(?:\(\)\s*=>|function\s*\(\))\s*\{[^}]*\}\s*,\s*5000\s*\)',
            status_js_content, re.DOTALL
        )
        bad = [b for b in block_intervals if 'loadZonesData' in b]
        assert not combined_5s, f"Found 5s polling with zones: {combined_5s}"
        assert not bad, f"Found 5s block polling with loadZonesData: {bad}"

    def test_zones_polling_interval_ge_15s(self, status_js_content):
        """loadZonesData polling interval must be >= 15000ms."""
        m = re.search(r'setInterval\s*\(\s*loadZonesData\s*,\s*(\d+)\s*\)', status_js_content)
        assert m, "Expected setInterval(loadZonesData, N) not found"
        interval = int(m.group(1))
        assert interval >= 15000, f"loadZonesData interval {interval}ms < 15000ms"


class TestNextWateringCache:
    """Fix 1: _nextWatering must be cached between polling cycles."""

    def test_cache_variable_exists(self, status_js_content):
        """A nextWateringCache variable must be declared."""
        assert re.search(r'(var|let|const)\s+nextWateringCache\s*=', status_js_content), \
            "nextWateringCache variable not found in status.js"

    def test_cache_is_populated_on_success(self, status_js_content):
        """On successful bulk response, cache must be updated."""
        assert 'nextWateringCache[' in status_js_content or 'nextWateringCache [' in status_js_content, \
            "nextWateringCache is never written to"


class TestResponseCheck:
    """Fix 3: Response must be checked before updating next-watering data."""

    def test_response_ok_or_status_check(self, status_js_content):
        """Code must check nwResp.ok or nwResp.status before processing bulk data."""
        has_ok = 'nwResp.ok' in status_js_content
        has_status = 'nwResp.status' in status_js_content
        assert has_ok or has_status, \
            "No response.ok / response.status check found for next-watering-bulk"

    def test_429_handling(self, status_js_content):
        """Code must handle 429 responses (rate limiting)."""
        has_429 = '429' in status_js_content
        has_too_many = 'Too Many' in status_js_content or 'too many' in status_js_content.lower()
        has_retry = 'Retry-After' in status_js_content or 'retry-after' in status_js_content.lower()
        assert has_429 or has_too_many or has_retry, \
            "No 429 / 'Too Many' / 'Retry-After' handling found in status.js"


class TestSingleRender:
    """Fix 4: renderZoneCards must NOT be called twice in one loadZonesData cycle."""

    def test_single_render_in_load_zones(self, status_js_content):
        """Inside loadZonesData function body, updateZoneCards or renderZoneCards should appear exactly once."""
        m = re.search(
            r'async\s+function\s+loadZonesData\s*\(\s*\)\s*\{(.*?)(?=\n    (?:async\s+)?function\s|\n    //\s*Быстрая\s)',
            status_js_content, re.DOTALL
        )
        assert m, "loadZonesData function not found"
        body = m.group(1)
        render_count = len(re.findall(r'(?:renderZoneCards|updateZoneCards)\s*\(', body))
        assert render_count == 1, f"render/update called {render_count} times in loadZonesData (expected 1)"


# ── DOM Patching tests ────────────────────────────────────────────────────

class TestUpdateZoneCards:
    """DOM patching: updateZoneCards function for incremental zone card updates."""

    def test_function_exists(self, status_js_content):
        """status.js must contain function updateZoneCards."""
        assert re.search(r'function\s+updateZoneCards\s*\(', status_js_content), \
            "updateZoneCards function not found in status.js"

    def test_checks_data_zone_ids(self, status_js_content):
        """updateZoneCards must check data-zone-ids attribute before full re-render."""
        m = re.search(
            r'function\s+updateZoneCards\s*\(\s*\)\s*\{(.*?)(?=\n    function\s)',
            status_js_content, re.DOTALL
        )
        assert m, "updateZoneCards function body not found"
        body = m.group(1)
        assert 'data-zone-ids' in body, \
            "updateZoneCards does not check data-zone-ids attribute"

    def test_falls_back_to_render(self, status_js_content):
        """updateZoneCards must call renderZoneCards() when zone list changes."""
        m = re.search(
            r'function\s+updateZoneCards\s*\(\s*\)\s*\{(.*?)(?=\n    function\s)',
            status_js_content, re.DOTALL
        )
        assert m, "updateZoneCards function body not found"
        body = m.group(1)
        assert 'renderZoneCards()' in body, \
            "updateZoneCards does not fall back to renderZoneCards()"

    def test_does_not_touch_timers(self, status_js_content):
        """updateZoneCards must NOT directly set .zc-running-timer content (tickCountdowns handles it)."""
        m = re.search(
            r'function\s+updateZoneCards\s*\(\s*\)\s*\{(.*?)(?=\n    function\s)',
            status_js_content, re.DOTALL
        )
        assert m, "updateZoneCards function body not found"
        body = m.group(1)
        # Should not have querySelector('.zc-running-timer')...textContent = anywhere
        # except when creating new running block (state transition off→on)
        timer_writes = re.findall(r"querySelector\(['\"]\.zc-running-timer['\"].*?textContent\s*=", body, re.DOTALL)
        assert not timer_writes, \
            "updateZoneCards directly writes to .zc-running-timer (should let tickCountdowns handle it)"

    def test_render_sets_data_zone_ids(self, status_js_content):
        """renderZoneCards must set data-zone-ids attribute on the container."""
        m = re.search(
            r'function\s+renderZoneCards\s*\(\s*\)\s*\{(.*?)(?=\n    //\s*Incremental|\n    function\s)',
            status_js_content, re.DOTALL
        )
        assert m, "renderZoneCards function body not found"
        body = m.group(1)
        assert 'data-zone-ids' in body, \
            "renderZoneCards does not set data-zone-ids attribute"

    def test_load_zones_calls_update(self, status_js_content):
        """loadZonesData must call updateZoneCards() instead of renderZoneCards()."""
        m = re.search(
            r'async\s+function\s+loadZonesData\s*\(\s*\)\s*\{(.*?)(?=\n    (?:async\s+)?function\s|\n    //\s*Быстрая\s)',
            status_js_content, re.DOTALL
        )
        assert m, "loadZonesData function not found"
        body = m.group(1)
        assert 'updateZoneCards()' in body, \
            "loadZonesData does not call updateZoneCards()"


class TestUpdateStatusDisplay:
    """DOM patching: updateStatusDisplay with gcard-* ids and fingerprinting."""

    def test_group_cards_have_gcard_id(self, status_js_content):
        """Group cards must use id='gcard-${group.id}'."""
        assert re.search(r"card\.id\s*=\s*[`'\"]gcard-", status_js_content), \
            "Group cards do not have id='gcard-...' format"

    def test_uses_getelementbyid_gcard(self, status_js_content):
        """updateStatusDisplay must check for existing cards via getElementById('gcard-')."""
        m = re.search(
            r'async\s+function\s+updateStatusDisplay\s*\(\s*\)\s*\{(.*?)(?=\n    function\s)',
            status_js_content, re.DOTALL
        )
        assert m, "updateStatusDisplay function body not found"
        body = m.group(1)
        assert "getElementById('gcard-'" in body or 'getElementById("gcard-"' in body \
            or "getElementById('gcard-' +" in body, \
            "updateStatusDisplay does not use getElementById('gcard-...') to find existing cards"

    def test_no_unconditional_innerhtml_clear(self, status_js_content):
        """updateStatusDisplay must NOT unconditionally clear container.innerHTML = ''."""
        m = re.search(
            r'async\s+function\s+updateStatusDisplay\s*\(\s*\)\s*\{(.*?)(?=\n    function\s)',
            status_js_content, re.DOTALL
        )
        assert m, "updateStatusDisplay function body not found"
        body = m.group(1)
        assert "container.innerHTML = ''" not in body and 'container.innerHTML=""' not in body, \
            "updateStatusDisplay still unconditionally clears container.innerHTML"

    def test_uses_replacechild(self, status_js_content):
        """updateStatusDisplay must use replaceChild for changed cards."""
        m = re.search(
            r'async\s+function\s+updateStatusDisplay\s*\(\s*\)\s*\{(.*?)(?=\n    function\s)',
            status_js_content, re.DOTALL
        )
        assert m, "updateStatusDisplay function body not found"
        body = m.group(1)
        assert 'replaceChild' in body, \
            "updateStatusDisplay does not use replaceChild for existing cards"

    def test_uses_fingerprint_or_status_check(self, status_js_content):
        """updateStatusDisplay must check a fingerprint or status before rebuilding."""
        m = re.search(
            r'async\s+function\s+updateStatusDisplay\s*\(\s*\)\s*\{(.*?)(?=\n    function\s)',
            status_js_content, re.DOTALL
        )
        assert m, "updateStatusDisplay function body not found"
        body = m.group(1)
        has_fp = 'data-group-fp' in body or 'groupFingerprint' in body
        has_status_check = 'prevGroupStatuses' in body
        assert has_fp or has_status_check, \
            "updateStatusDisplay does not use fingerprint or status cache to avoid unnecessary rebuilds"


# ── Backend tests ─────────────────────────────────────────────────────────

class TestRateLimiterSkip:
    """Fix 5: next-watering-bulk must be excluded from general rate limiter."""

    def test_next_watering_bulk_in_skip_paths(self, app_py_content):
        """_general_api_rate_limit skip_paths must include next-watering-bulk."""
        m = re.search(
            r'def\s+_general_api_rate_limit\s*\(\s*\).*?(?=\ndef\s)',
            app_py_content, re.DOTALL
        )
        assert m, "_general_api_rate_limit function not found"
        func_body = m.group(0)
        assert 'next-watering-bulk' in func_body, \
            "next-watering-bulk not found in _general_api_rate_limit skip/exclusion logic"


class TestRateLimiterAPI:
    """Fix 5: next-watering-bulk endpoint returns 200 at normal frequency."""

    def test_bulk_endpoint_not_429(self):
        """POST /api/zones/next-watering-bulk should return 200, not 429, under normal use.
        (Integration test — requires running app. Skip if not available.)"""
        pytest.skip("Integration test — requires running server")


# ── Template tests ────────────────────────────────────────────────────────

class TestTemplate:
    """status.html must not contain fast polling setInterval."""

    def test_no_fast_setinterval_in_template(self, status_html_content):
        """status.html should not have setInterval with <= 5000ms for zones polling."""
        matches = re.findall(r'setInterval\s*\([^,]+,\s*(\d+)\s*\)', status_html_content)
        for val in matches:
            ms = int(val)
            if ms <= 5000:
                context = status_html_content[max(0, status_html_content.find(val)-200):status_html_content.find(val)+50]
                if 'loadZonesData' in context or 'loadStatusData' in context:
                    pytest.fail(f"Found setInterval with {ms}ms polling in status.html")
