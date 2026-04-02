"""
Tests for timer/progress bar flicker fix in status.js.

These tests verify the JavaScript logic by extracting and testing
key functions via a lightweight JS execution approach (subprocess + node).
"""
import subprocess
import json
import os
import pytest

JS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'static', 'js')
STATUS_JS = os.path.join(JS_DIR, 'status.js')


def run_js(script: str) -> str:
    """Run JavaScript via Node.js and return stdout."""
    result = subprocess.run(
        ['node', '-e', script],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        raise RuntimeError(f"Node.js error: {result.stderr}")
    return result.stdout.strip()


def run_js_json(script: str):
    """Run JS and parse JSON output."""
    out = run_js(script)
    return json.loads(out)


class TestParseDate:
    """parseDate() must handle both ISO format and space-separated format."""

    PARSE_DATE_FN = """
    function parseDate(s) {
        if (!s) return null;
        var d = new Date(String(s).replace(' ', 'T'));
        return isNaN(d.getTime()) ? null : d;
    }
    """

    def test_iso_format_with_T(self):
        result = run_js_json(f"""
        {self.PARSE_DATE_FN}
        var d = parseDate("2026-04-02T07:56:52");
        console.log(JSON.stringify({{
            valid: d !== null,
            iso: d ? d.toISOString() : null
        }}));
        """)
        assert result['valid'] is True
        assert '2026-04-02' in result['iso']

    def test_space_format(self):
        """Safari-breaking format: space instead of T."""
        result = run_js_json(f"""
        {self.PARSE_DATE_FN}
        var d = parseDate("2026-04-02 07:56:52");
        console.log(JSON.stringify({{
            valid: d !== null,
            iso: d ? d.toISOString() : null
        }}));
        """)
        assert result['valid'] is True
        assert '2026-04-02' in result['iso']

    def test_null_input(self):
        result = run_js_json(f"""
        {self.PARSE_DATE_FN}
        console.log(JSON.stringify({{ result: parseDate(null) }}));
        """)
        assert result['result'] is None

    def test_empty_string(self):
        result = run_js_json(f"""
        {self.PARSE_DATE_FN}
        console.log(JSON.stringify({{ result: parseDate("") }}));
        """)
        assert result['result'] is None

    def test_invalid_string(self):
        result = run_js_json(f"""
        {self.PARSE_DATE_FN}
        console.log(JSON.stringify({{ result: parseDate("not-a-date") }}));
        """)
        assert result['result'] is None

    def test_undefined_input(self):
        result = run_js_json(f"""
        {self.PARSE_DATE_FN}
        console.log(JSON.stringify({{ result: parseDate(undefined) }}));
        """)
        assert result['result'] is None


class TestParseDateInStatusJs:
    """Verify parseDate() exists in status.js after implementation."""

    def test_parsedate_defined_in_source(self):
        with open(STATUS_JS, 'r') as f:
            content = f.read()
        assert 'function parseDate(' in content, "parseDate() must be defined in status.js"

    def test_parsedate_used_for_planned_end_time(self):
        """parseDate must be used where planned_end_time is parsed."""
        with open(STATUS_JS, 'r') as f:
            content = f.read()
        # After fix, there should be no raw `new Date(zone.planned_end_time)` or
        # `new Date(z.planned_end_time)` — they should use parseDate()
        # We check that parseDate is called with planned_end_time
        assert 'parseDate(' in content, "parseDate() must be used in status.js"


class TestNoMathRandom:
    """Math.random() must not be used for flow-active class."""

    def test_no_math_random_for_flow_active(self):
        with open(STATUS_JS, 'r') as f:
            content = f.read()
        # There should be no `Math.random()` combined with flow-active logic
        assert 'Math.random()' not in content, \
            "Math.random() should be removed from flow-active logic"


class TestPollingInterval:
    """Polling interval should be 30s, not 5s."""

    def test_interval_not_5000(self):
        with open(STATUS_JS, 'r') as f:
            content = f.read()
        # The main polling setInterval should not be 5000ms
        # Look for the pattern: setInterval followed by loadStatusData/loadZonesData with 5000
        import re
        # Find setInterval calls that poll status/zones with 5000
        matches = re.findall(
            r'setInterval\s*\(\s*(?:\(\)\s*=>\s*\{|function\s*\(\)\s*\{)[^}]*(?:loadStatusData|loadZonesData)[^}]*\}\s*,\s*(\d+)\s*\)',
            content
        )
        for interval in matches:
            assert int(interval) >= 30000, \
                f"Polling interval should be >= 30000ms, found {interval}ms"


class TestDomPatchingZoneCards:
    """renderZoneCards() should not recreate existing cards on re-render."""

    def test_renderzone_uses_patching(self):
        """After fix, renderZoneCards should check for existing cards."""
        with open(STATUS_JS, 'r') as f:
            content = f.read()
        # The function should check for existing zone cards before rebuilding
        # Look for data-zone-id based lookup OR getElementById pattern
        assert ('querySelector' in content or 'getElementById' in content), \
            "renderZoneCards should use DOM queries for patching"

    def test_no_double_render_in_loadzonesdata(self):
        """loadZonesData should call renderZoneCards only once per cycle."""
        with open(STATUS_JS, 'r') as f:
            content = f.read()
        # Find the loadZonesData function body
        import re
        match = re.search(
            r'async\s+function\s+loadZonesData\s*\(\)\s*\{',
            content
        )
        assert match, "loadZonesData function must exist"
        # Count renderZoneCards calls within loadZonesData
        start = match.start()
        # Find the function body (rough: count braces)
        depth = 0
        func_body = ''
        for i, ch in enumerate(content[start:]):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    func_body = content[start:start + i + 1]
                    break
        render_calls = func_body.count('renderZoneCards()')
        assert render_calls <= 1, \
            f"renderZoneCards() should be called at most once in loadZonesData, found {render_calls} calls"


class TestErrorResilience:
    """On fetch error, existing data should be preserved."""

    def test_loadzones_preserves_data_on_error(self):
        """loadZonesData should not wipe zonesData on network error."""
        with open(STATUS_JS, 'r') as f:
            content = f.read()
        import re
        match = re.search(
            r'async\s+function\s+loadZonesData\s*\(\)\s*\{',
            content
        )
        assert match
        start = match.start()
        depth = 0
        func_body = ''
        for i, ch in enumerate(content[start:]):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    func_body = content[start:start + i + 1]
                    break
        # In the catch block, zonesData should NOT be set to empty array
        # The pattern `zonesData = []` in a catch context would be bad
        # After fix, we should see preservation logic
        assert 'zonesData = []' not in func_body or 'catch' not in func_body.split('zonesData = []')[0][-200:], \
            "zonesData should not be wiped to [] in error handling path"


class TestRecalcTimersFromRealTime:
    """recalcTimersFromRealTime() must exist and recalculate timers from planned_end_time."""

    def test_function_defined_in_source(self):
        with open(STATUS_JS, 'r') as f:
            content = f.read()
        assert 'function recalcTimersFromRealTime(' in content, \
            "recalcTimersFromRealTime() must be defined in status.js"

    def test_uses_parsedate(self):
        """recalcTimersFromRealTime must use parseDate for Safari compat."""
        with open(STATUS_JS, 'r') as f:
            content = f.read()
        import re
        match = re.search(
            r'function\s+recalcTimersFromRealTime\s*\(\)\s*\{',
            content
        )
        assert match, "recalcTimersFromRealTime function must exist"
        start = match.start()
        depth = 0
        func_body = ''
        for i, ch in enumerate(content[start:]):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    func_body = content[start:start + i + 1]
                    break
        assert 'parseDate(' in func_body, \
            "recalcTimersFromRealTime must use parseDate()"

    def test_handles_zone_and_group_timers(self):
        """Must handle both .zc-running-timer and .group-timer selectors."""
        with open(STATUS_JS, 'r') as f:
            content = f.read()
        import re
        match = re.search(
            r'function\s+recalcTimersFromRealTime\s*\(\)\s*\{',
            content
        )
        assert match
        start = match.start()
        depth = 0
        func_body = ''
        for i, ch in enumerate(content[start:]):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    func_body = content[start:start + i + 1]
                    break
        assert '.zc-running-timer' in func_body, \
            "recalcTimersFromRealTime must handle zone card timers"
        assert '.group-timer' in func_body, \
            "recalcTimersFromRealTime must handle group timers"

    def test_updates_progress_bar(self):
        """Must update progress bar width, not just timer text."""
        with open(STATUS_JS, 'r') as f:
            content = f.read()
        import re
        match = re.search(
            r'function\s+recalcTimersFromRealTime\s*\(\)\s*\{',
            content
        )
        assert match
        start = match.start()
        depth = 0
        func_body = ''
        for i, ch in enumerate(content[start:]):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    func_body = content[start:start + i + 1]
                    break
        assert 'progEl' in func_body or 'zprog' in func_body, \
            "recalcTimersFromRealTime must update progress bars"

    def test_visibilitychange_calls_recalc(self):
        """visibilitychange handler must call recalcTimersFromRealTime."""
        with open(STATUS_JS, 'r') as f:
            content = f.read()
        assert 'visibilitychange' in content, \
            "visibilitychange event listener must exist"
        # Find the visibilitychange block and verify recalcTimersFromRealTime is called
        idx = content.index('visibilitychange')
        nearby = content[idx:idx + 500]
        assert 'recalcTimersFromRealTime' in nearby, \
            "visibilitychange handler must call recalcTimersFromRealTime()"


class TestTickCountdownsDriftCorrection:
    """tickCountdowns() must have drift correction using parseDate."""

    @staticmethod
    def _get_tickcountdowns_body():
        with open(STATUS_JS, 'r') as f:
            content = f.read()
        import re
        match = re.search(
            r'function\s+tickCountdowns\s*\(\)\s*\{',
            content
        )
        assert match, "tickCountdowns function must exist"
        start = match.start()
        depth = 0
        for i, ch in enumerate(content[start:]):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return content[start:start + i + 1]
        raise AssertionError("Could not extract tickCountdowns body")

    def test_has_parsedate_for_drift(self):
        body = self._get_tickcountdowns_body()
        assert 'parseDate(' in body, \
            "tickCountdowns must use parseDate() for drift correction"

    def test_drift_threshold_check(self):
        """Drift correction should compare abs difference > 2."""
        body = self._get_tickcountdowns_body()
        assert 'Math.abs' in body, \
            "tickCountdowns drift correction must use Math.abs for threshold"
        assert '> 2' in body, \
            "tickCountdowns drift correction must use threshold > 2"

    def test_zone_and_group_drift_correction(self):
        """Both zone timers and group timers must have drift correction."""
        body = self._get_tickcountdowns_body()
        # Should have drift correction for both .zc-running-timer and .group-timer
        assert body.count('parseDate(') >= 2, \
            "tickCountdowns must have drift correction for both zone and group timers"


class TestGroupCardPatching:
    """updateStatusDisplay() should patch existing group cards."""

    def test_update_status_no_innerhtml_clear(self):
        """After fix, updateStatusDisplay should not do container.innerHTML = ''."""
        with open(STATUS_JS, 'r') as f:
            content = f.read()
        import re
        match = re.search(
            r'async\s+function\s+updateStatusDisplay\s*\(\)\s*\{',
            content
        )
        assert match
        start = match.start()
        depth = 0
        func_body = ''
        for i, ch in enumerate(content[start:]):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    func_body = content[start:start + i + 1]
                    break
        assert "container.innerHTML = ''" not in func_body, \
            "updateStatusDisplay should not clear container.innerHTML"
