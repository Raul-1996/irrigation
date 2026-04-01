"""Tests for XSS fix — TDD spec.

Covers:
- SSR JSON island (no |safe)
- escapeHtml utility in app.js
- innerHTML escaping in JS files
"""
import os
import re
import pytest

os.environ['TESTING'] = '1'

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Helpers ──────────────────────────────────────────────────────────

def read_file(relpath):
    """Read a project file and return its content."""
    path = os.path.join(PROJECT_ROOT, relpath)
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


# ── 1. SSR Template Tests ────────────────────────────────────────────

class TestSSRTemplateNoSafe:
    """status.html must NOT use |safe for SSR data — use JSON island instead."""

    def test_no_safe_filter_for_inline_zones(self):
        html = read_file('templates/status.html')
        # |safe should not appear near inline_zones
        assert '|safe' not in html or 'inline_zones' not in html.split('|safe')[0].split('\n')[-1], \
            "status.html still uses |safe for inline_zones"

    def test_no_safe_filter_for_inline_groups(self):
        html = read_file('templates/status.html')
        assert '|safe' not in html or 'inline_groups' not in html.split('|safe')[0].split('\n')[-1], \
            "status.html still uses |safe for inline_groups"

    def test_no_safe_filter_for_inline_status(self):
        html = read_file('templates/status.html')
        assert '|safe' not in html or 'inline_status' not in html.split('|safe')[0].split('\n')[-1], \
            "status.html still uses |safe for inline_status"

    def test_no_safe_anywhere_for_ssr_data(self):
        """More robust: no line should contain both inline_ and |safe."""
        html = read_file('templates/status.html')
        for i, line in enumerate(html.split('\n'), 1):
            if 'inline_zones' in line or 'inline_groups' in line or 'inline_status' in line:
                assert '|safe' not in line, \
                    f"Line {i} uses |safe with SSR data: {line.strip()}"

    def test_tojson_filter_used(self):
        """status.html should use |tojson filter for SSR data."""
        html = read_file('templates/status.html')
        assert '|tojson' in html, \
            "status.html missing |tojson filter for SSR data"
        assert html.count('|tojson') >= 3, \
            "Expected at least 3 |tojson usages for zones, groups, status"

    def test_json_parse_present(self):
        """status.html should use JSON.parse to decode SSR data."""
        html = read_file('templates/status.html')
        assert 'JSON.parse' in html, "status.html missing JSON.parse for SSR data"


# ── 2. SSR Rendering with XSS Payloads ──────────────────────────────

class TestSSRRenderingEscaping:
    """Test that SSR renders XSS payloads safely via Flask template rendering."""

    @pytest.fixture
    def rendered_status(self, client, test_db):
        """Create a zone with XSS payload and render status page."""
        # Insert zone with XSS name
        test_db.zones.create_zone({
            'name': '<img src=x onerror=alert(1)>',
            'duration': 10,
            'group_id': 1,
            'icon': '🌿',
            'topic': '/test/zone1',
        })
        # Insert group with XSS name
        test_db.groups.create_group({
            'name': '</script><script>alert(document.cookie)</script>',
        })
        resp = client.get('/')
        return resp.data.decode('utf-8')

    def test_script_tag_in_zone_name_escaped(self, rendered_status):
        """Zone name with </script> should not break HTML structure."""
        html = rendered_status
        # tojson should escape </script> inside JSON string values
        # so that the literal unescaped sequence never appears in SSR data block
        # Find the SSR script block
        ssr_match = re.search(r'window\._ssrZones\s*=\s*JSON\.parse\((.*?)\);', html)
        if ssr_match:
            ssr_block = ssr_match.group(1)
            # The raw </script> should not appear unescaped inside the JSON string
            assert '</script>' not in ssr_block.lower(), \
                "Unescaped </script> in SSR data — tojson should escape this"

    def test_img_onerror_in_zone_name_escaped(self, rendered_status):
        """<img src=x onerror=alert(1)> should be escaped in SSR output."""
        html = rendered_status
        assert '<img src=x onerror=alert(1)>' not in html, \
            "Unescaped XSS payload in SSR output"

    def test_xss_in_group_name_escaped(self, rendered_status):
        """Group name with XSS payload should be escaped."""
        html = rendered_status
        assert '<script>alert(document.cookie)</script>' not in html, \
            "Unescaped XSS script tag in group name"


# ── 3. API Endpoints Return Raw Data ────────────────────────────────

class TestAPIReturnsRawData:
    """API endpoints should return raw data — escaping happens on the client."""

    def test_api_zones_returns_raw_name(self, admin_client, test_db):
        """API should return zone name as-is (no HTML escaping)."""
        xss_name = '<img src=x onerror=alert(1)>'
        test_db.zones.create_zone({
            'name': xss_name,
            'duration': 10,
            'group_id': 1,
            'icon': '🌿',
            'topic': '/test/zone1',
        })
        resp = admin_client.get('/api/zones')
        data = resp.get_json()
        zones = data if isinstance(data, list) else data.get('zones', [])
        if zones:
            # At least one zone should have the XSS name (raw, not escaped)
            names = [z.get('name', '') for z in zones]
            assert xss_name in names, \
                "API should return raw zone name without HTML escaping"


# ── 4. escapeHtml Function in app.js ─────────────────────────────────

class TestEscapeHtmlFunction:
    """app.js must contain a proper escapeHtml function."""

    def test_escape_html_exists(self):
        js = read_file('static/js/app.js')
        assert 'function escapeHtml' in js, \
            "app.js missing escapeHtml function"

    def test_escape_html_handles_ampersand(self):
        js = read_file('static/js/app.js')
        assert "&amp;" in js, "escapeHtml should escape & to &amp;"

    def test_escape_html_handles_lt(self):
        js = read_file('static/js/app.js')
        assert "&lt;" in js, "escapeHtml should escape < to &lt;"

    def test_escape_html_handles_gt(self):
        js = read_file('static/js/app.js')
        assert "&gt;" in js, "escapeHtml should escape > to &gt;"

    def test_escape_html_handles_double_quote(self):
        js = read_file('static/js/app.js')
        assert "&quot;" in js, "escapeHtml should escape \" to &quot;"

    def test_escape_html_handles_single_quote(self):
        js = read_file('static/js/app.js')
        assert "&#039;" in js, "escapeHtml should escape ' to &#039;"

    def test_escape_html_handles_null(self):
        """escapeHtml should handle null/undefined by returning empty string."""
        js = read_file('static/js/app.js')
        # Should check for null: str == null catches both null and undefined
        assert 'str == null' in js or 'str === null' in js or 'str==null' in js, \
            "escapeHtml should handle null input"


# ── 5. innerHTML Usage Checks ────────────────────────────────────────

class TestInnerHTMLEscaping:
    """All innerHTML with user-controlled .name fields must use escapeHtml."""

    def _check_no_raw_name_in_innerhtml(self, filepath, patterns):
        """Check that none of the given raw patterns appear in innerHTML contexts."""
        content = read_file(filepath)
        lines = content.split('\n')
        violations = []
        for i, line in enumerate(lines, 1):
            if 'innerHTML' not in line and 'innerHTML' not in '\n'.join(lines[max(0,i-10):i]):
                continue
            for pat in patterns:
                if pat in line and 'escapeHtml' not in line:
                    # Check if this is inside an innerHTML assignment context
                    # Look backward for innerHTML
                    context = '\n'.join(lines[max(0,i-15):i])
                    if 'innerHTML' in context or '.innerHTML' in context:
                        violations.append(f"Line {i}: raw {pat} without escapeHtml: {line.strip()[:100]}")
        return violations

    def test_status_js_group_name_escaped(self):
        """status.js: group.name in innerHTML must use escapeHtml."""
        js = read_file('static/js/status.js')
        # Find lines with group.name inside template literals going to innerHTML
        violations = []
        lines = js.split('\n')
        for i, line in enumerate(lines, 1):
            if 'group.name' in line and 'escapeHtml' not in line:
                # Check if this is in innerHTML context (look for innerHTML in surrounding lines)
                ctx = '\n'.join(lines[max(0,i-20):i+5])
                if '.innerHTML' in ctx and ('${group.name}' in line or '+ group.name' in line or "' + g.name" in line):
                    violations.append(f"Line {i}: {line.strip()[:100]}")
        assert not violations, f"status.js has raw group.name in innerHTML:\n" + '\n'.join(violations)

    def test_status_js_zone_name_escaped(self):
        """status.js: zone.name/z.name in HTML string building must use escapeHtml."""
        js = read_file('static/js/status.js')
        violations = []
        lines = js.split('\n')
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Skip comments, variable assignments, textContent
            if stripped.startswith('//') or stripped.startswith('*'):
                continue
            if 'textContent' in line:
                continue
            # Only match HTML string building patterns (not plain assignments)
            # Look for z.name or zone.name inside string concatenation or template literals
            is_html_string = (
                ("'" in line or '"' in line or '`' in line) and
                ('<' in line or 'html' in line.lower() or '+' in line)
            )
            if not is_html_string:
                continue
            if ('zone.name' in line or 'z.name' in line) and 'escapeHtml' not in line:
                ctx = '\n'.join(lines[max(0,i-20):i+5])
                if '.innerHTML' in ctx:
                    violations.append(f"Line {i}: {stripped[:100]}")
        assert not violations, f"status.js has raw zone.name in innerHTML:\n" + '\n'.join(violations)

    def test_status_js_g_name_tabs_escaped(self):
        """status.js: g.name in HTML string building must use escapeHtml."""
        js = read_file('static/js/status.js')
        violations = []
        lines = js.split('\n')
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith('//') or stripped.startswith('*'):
                continue
            # Skip plain variable assignments like: groupNameById[g.id] = g.name;
            # or: gName = g.name;
            if '= g.name' in stripped and ('+' not in stripped and '<' not in stripped and '`' not in stripped):
                continue
            if "g.name" in line and 'escapeHtml' not in line:
                # Only flag if it's in HTML string building context
                is_html_string = ("'" in line or '"' in line or '`' in line) and ('<' in line or 'html' in line.lower() or '+' in line)
                if is_html_string and '.innerHTML' in '\n'.join(lines[max(0,i-15):i+5]):
                    violations.append(f"Line {i}: {stripped[:100]}")
        assert not violations, f"status.js has raw g.name in innerHTML:\n" + '\n'.join(violations)

    def test_zones_js_names_escaped(self):
        """zones.js: zone.name, group.name, s.name in innerHTML must use escapeHtml."""
        js = read_file('static/js/zones.js')
        violations = []
        lines = js.split('\n')
        name_patterns = ['${zone.name}', '${group.name}', '${s.name}',
                         '${c.checked_program_name}', '${c.other_program_name}']
        for i, line in enumerate(lines, 1):
            for pat in name_patterns:
                if pat in line and 'escapeHtml' not in line:
                    ctx = '\n'.join(lines[max(0,i-20):i+5])
                    if '.innerHTML' in ctx:
                        violations.append(f"Line {i}: raw {pat}: {line.strip()[:100]}")
        assert not violations, f"zones.js has raw names in innerHTML:\n" + '\n'.join(violations)

    def test_programs_js_names_escaped(self):
        """programs.js: program.name, meta.name, zone.name must use escapeHtml."""
        js = read_file('static/js/programs.js')
        violations = []
        lines = js.split('\n')
        name_patterns = ['${program.name}', '${meta.name}', '${zone.name}']
        for i, line in enumerate(lines, 1):
            for pat in name_patterns:
                if pat in line and 'escapeHtml' not in line:
                    ctx = '\n'.join(lines[max(0,i-20):i+5])
                    if '.innerHTML' in ctx:
                        violations.append(f"Line {i}: raw {pat}: {line.strip()[:100]}")
        assert not violations, f"programs.js has raw names in innerHTML:\n" + '\n'.join(violations)

    def test_mqtt_html_names_escaped(self):
        """mqtt.html: s.name, s.host, s.username in innerHTML must use escapeHtml."""
        html = read_file('templates/mqtt.html')
        violations = []
        lines = html.split('\n')
        name_patterns = ['${s.name', '${s.host', '${s.username', '${s.client_id']
        for i, line in enumerate(lines, 1):
            for pat in name_patterns:
                if pat in line and 'escapeHtml' not in line:
                    ctx = '\n'.join(lines[max(0,i-20):i+5])
                    if '.innerHTML' in ctx or 'innerHTML' in ctx:
                        violations.append(f"Line {i}: raw {pat}: {line.strip()[:100]}")
        assert not violations, f"mqtt.html has raw server data in innerHTML:\n" + '\n'.join(violations)

    def test_programs_html_names_escaped(self):
        """programs.html: p.name, zone.name, wizardData.name must use escapeHtml."""
        html = read_file('templates/programs.html')
        violations = []
        lines = html.split('\n')
        name_patterns = ['${p.name}', '${zone.name}', '${wizardData.name']
        for i, line in enumerate(lines, 1):
            for pat in name_patterns:
                if pat in line and 'escapeHtml' not in line:
                    ctx = '\n'.join(lines[max(0,i-20):i+5])
                    if '.innerHTML' in ctx or 'innerHTML' in ctx:
                        violations.append(f"Line {i}: raw {pat}: {line.strip()[:100]}")
        assert not violations, f"programs.html has raw names in innerHTML:\n" + '\n'.join(violations)

    def test_app_js_notification_escaped(self):
        """app.js: notification message in innerHTML must use escapeHtml."""
        js = read_file('static/js/app.js')
        violations = []
        lines = js.split('\n')
        for i, line in enumerate(lines, 1):
            if '${message}' in line and 'innerHTML' in '\n'.join(lines[max(0,i-5):i+3]):
                if 'escapeHtml' not in line:
                    violations.append(f"Line {i}: raw ${{message}} in innerHTML: {line.strip()[:100]}")
        assert not violations, f"app.js has raw message in innerHTML:\n" + '\n'.join(violations)
