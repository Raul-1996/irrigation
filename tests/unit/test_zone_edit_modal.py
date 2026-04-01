"""Tests for zone edit modal: desktop CSS, API PUT, JS content."""
import os
import re
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE_PATH = os.path.join(PROJECT_ROOT, 'templates', 'status.html')
JS_PATH = os.path.join(PROJECT_ROOT, 'static', 'js', 'status.js')


def _read(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


# ─── Template tests ───

class TestTemplateElements:
    """status.html contains required bottom sheet elements."""

    def test_has_bottom_sheet(self):
        html = _read(TEMPLATE_PATH)
        assert 'id="bottomSheet"' in html or "id='bottomSheet'" in html

    def test_has_sheet_overlay(self):
        html = _read(TEMPLATE_PATH)
        assert 'id="sheetOverlay"' in html or "id='sheetOverlay'" in html

    def test_has_desktop_media_query(self):
        """Desktop media query for #bottomSheet exists."""
        html = _read(TEMPLATE_PATH)
        # Find @media (min-width: 768px) that contains bottomSheet or bottom-sheet
        pattern = r'@media\s*\(\s*min-width\s*:\s*768px\s*\)'
        matches = list(re.finditer(pattern, html))
        assert len(matches) > 0, "No @media (min-width: 768px) found in status.html"
        # Check that at least one of these media blocks references bottomSheet/bottom-sheet
        found = False
        for m in matches:
            # Get ~600 chars after the match to find the block content
            block = html[m.start():m.start() + 600]
            if 'bottomSheet' in block or 'bottom-sheet' in block:
                found = True
                break
        assert found, "@media (min-width: 768px) exists but doesn't reference #bottomSheet"

    def test_desktop_has_max_width(self):
        """Desktop #bottomSheet has max-width."""
        html = _read(TEMPLATE_PATH)
        pattern = r'@media\s*\(\s*min-width\s*:\s*768px\s*\)'
        for m in re.finditer(pattern, html):
            block = html[m.start():m.start() + 600]
            if ('bottomSheet' in block or 'bottom-sheet' in block) and 'max-width' in block:
                return  # pass
        pytest.fail("Desktop media query for #bottomSheet doesn't have max-width")

    def test_desktop_has_border_radius(self):
        """Desktop #bottomSheet has border-radius for all corners."""
        html = _read(TEMPLATE_PATH)
        pattern = r'@media\s*\(\s*min-width\s*:\s*768px\s*\)'
        for m in re.finditer(pattern, html):
            block = html[m.start():m.start() + 600]
            if ('bottomSheet' in block or 'bottom-sheet' in block) and 'border-radius' in block:
                return  # pass
        pytest.fail("Desktop media query for #bottomSheet doesn't have border-radius")

    def test_desktop_has_centering(self):
        """Desktop #bottomSheet is centered (translate(-50%, -50%) or equivalent)."""
        html = _read(TEMPLATE_PATH)
        pattern = r'@media\s*\(\s*min-width\s*:\s*768px\s*\)'
        for m in re.finditer(pattern, html):
            block = html[m.start():m.start() + 600]
            if ('bottomSheet' in block or 'bottom-sheet' in block):
                if 'translate(-50%' in block or 'margin' in block:
                    return
        pytest.fail("Desktop #bottomSheet lacks centering (translate(-50%,-50%) or margin:auto)")

    def test_mobile_no_max_width_on_bottom_sheet(self):
        """Mobile bottom-sheet base styles do NOT have max-width restriction."""
        html = _read(TEMPLATE_PATH)
        # Find the base .bottom-sheet rule (outside desktop media query)
        # Look for .bottom-sheet { ... } that's NOT inside @media (min-width: 768px)
        # Strategy: find .bottom-sheet rule, check it doesn't have max-width
        base_pattern = r'\.bottom-sheet\s*\{([^}]+)\}'
        for m in re.finditer(base_pattern, html):
            # Check this isn't inside a desktop media query
            preceding = html[max(0, m.start() - 200):m.start()]
            if 'min-width' in preceding and '768' in preceding:
                continue  # This is inside desktop media query, skip
            rule_body = m.group(1)
            assert 'max-width' not in rule_body, \
                f"Mobile .bottom-sheet should NOT have max-width, but found: {rule_body[:100]}"


# ─── API tests ───

class TestZoneEditAPI:
    """PUT /api/zones/<id> works for duration, name, icon."""

    def _create_zone(self, client):
        """Create a zone and return its id."""
        resp = client.post('/api/zones', json={
            'name': 'API Test Zone',
            'duration': 10,
            'icon': '🌿',
        })
        assert resp.status_code == 201, f"Zone creation failed: {resp.data}"
        data = resp.get_json()
        # handle both direct zone dict and nested {'zone': {...}} response
        if 'zone' in data and isinstance(data['zone'], dict):
            return data['zone']['id']
        return data['id']

    def test_put_duration(self, admin_client):
        zid = self._create_zone(admin_client)
        resp = admin_client.put(f'/api/zones/{zid}', json={'duration': 12})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['duration'] == 12

    def test_put_name(self, admin_client):
        zid = self._create_zone(admin_client)
        resp = admin_client.put(f'/api/zones/{zid}', json={'name': 'Тест'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['name'] == 'Тест'

    def test_put_all_fields(self, admin_client):
        zid = self._create_zone(admin_client)
        resp = admin_client.put(f'/api/zones/{zid}', json={
            'duration': 12,
            'name': 'Тест',
            'icon': '🌊',
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['duration'] == 12
        assert data['name'] == 'Тест'
        assert data['icon'] == '🌊'

    def test_put_duration_zero_rejected(self, admin_client):
        zid = self._create_zone(admin_client)
        resp = admin_client.put(f'/api/zones/{zid}', json={'duration': 0})
        assert resp.status_code == 400

    def test_put_duration_over_max_rejected(self, admin_client):
        zid = self._create_zone(admin_client)
        resp = admin_client.put(f'/api/zones/{zid}', json={'duration': 3601})
        assert resp.status_code == 400


# ─── JS content tests (grep-based) ───

class TestJSContent:
    """saveZoneEdit in status.js has correct structure."""

    def _get_save_function_body(self):
        """Extract saveZoneEdit function body from status.js."""
        js = _read(JS_PATH)
        # Find function saveZoneEdit and get its body
        idx = js.find('function saveZoneEdit')
        assert idx != -1, "saveZoneEdit function not found in status.js"
        # Get ~800 chars from function start
        return js[idx:idx + 800]

    def test_calls_api_put(self):
        body = self._get_save_function_body()
        assert 'api.put' in body, "saveZoneEdit should call api.put"

    def test_reads_edit_zone_duration(self):
        body = self._get_save_function_body()
        assert 'editZoneDuration' in body, "saveZoneEdit should read editZoneDuration"

    def test_calls_close_zone_sheet(self):
        body = self._get_save_function_body()
        assert 'closeZoneSheet' in body, "saveZoneEdit should call closeZoneSheet on success"
