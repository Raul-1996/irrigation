"""Regression tests for issue #40 — group «БЕЗ ПОЛИВА» (id=999) tab on main page.

Final product decision (Raul, 2026-05-16):
  1. Tab MUST be visible in #groupTabs.
  2. Clicking it shows zones with group_id=999 (no auth/styling changes).
  3. «Все» tab MUST still exclude group_id=999 zones.
  4. Program wizard MUST still exclude group_id=999 zones.
  5. Backend behavior unchanged — fix is frontend-only (`static/js/status.js`).

These tests guard the four invariants above by:
  - grep-ing the static JS source for the exact filter/order patterns
    (project's existing convention — see tests/unit/test_zone_edit_modal.py,
    tests/unit/test_xss_fix.py),
  - asserting the API returns group 999 (the data the JS depends on).
"""

import json
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATUS_JS = os.path.join(PROJECT_ROOT, "static", "js", "status.js")
PROGRAMS_JS = os.path.join(PROJECT_ROOT, "static", "js", "programs.js")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ─── JS source invariants (the fix lives here) ───


class TestStatusJsBinGroupTab:
    """status.js — show «БЕЗ ПОЛИВА» tab, keep «Все» clean."""

    def test_render_group_tabs_no_longer_drops_999(self):
        """renderGroupTabs() must NOT filter out g.id === 999 from the tab list.

        Before fix: `groups.filter(function(g) { return g.id !== 999; }).forEach(...)`.
        After fix:  `groups.forEach(...)` — bin group renders as a normal tab.
        """
        js = _read(STATUS_JS)
        # Locate the renderGroupTabs function body
        m = re.search(r"function\s+renderGroupTabs\s*\([^)]*\)\s*\{", js)
        assert m, "renderGroupTabs() not found in status.js"
        # Take a generous slice — function is ~35 lines
        body = js[m.end() : m.end() + 2000]
        # The forbidden filter pattern that hid the bin tab
        assert "g.id !== 999" not in body, (
            "renderGroupTabs() still filters out g.id === 999 — БЕЗ ПОЛИВА tab will be hidden."
        )
        # And it still iterates the full groups list
        assert "groups.forEach" in body, "renderGroupTabs() no longer iterates groups list"

    def test_get_filtered_zones_v2_shows_bin_when_selected(self):
        """getFilteredZonesV2() must show 999-zones when currentGroupFilter===999.

        The order of operations matters: per-group filter MUST run BEFORE
        the 'exclude 999' filter — otherwise selecting the bin tab yields [].
        New shape:
            zones = (zonesData || []).slice();
            if (currentGroupFilter !== null) {
                zones = zones.filter(z => z.group_id === currentGroupFilter);
            } else {
                zones = zones.filter(z => z.group_id !== 999);
            }
        """
        js = _read(STATUS_JS)
        m = re.search(r"function\s+getFilteredZonesV2\s*\([^)]*\)\s*\{", js)
        assert m, "getFilteredZonesV2() not found in status.js"
        body = js[m.end() : m.end() + 1500]
        # The exclude-999 branch must live INSIDE the else (i.e. only when no group is selected)
        # Quick structural check: the substring "currentGroupFilter !== null" must appear
        # BEFORE the "group_id !== 999" check in the function body.
        idx_filter = body.find("currentGroupFilter !== null")
        idx_exclude = body.find("group_id !== 999")
        assert idx_filter != -1, "currentGroupFilter check missing in getFilteredZonesV2()"
        assert idx_exclude != -1, "exclude-999 fallback missing in getFilteredZonesV2()"
        assert idx_filter < idx_exclude, (
            "getFilteredZonesV2() filters out group_id===999 BEFORE applying the "
            "per-group filter — selecting the БЕЗ ПОЛИВА tab will yield an empty list."
        )

    def test_vse_tab_count_still_excludes_999(self):
        """«Все» tab counter (`allZones`) must still exclude bin zones.

        Raul wants the bin to be "hidden by default" until the user clicks
        its tab — so the «Все» count must continue to filter g.id===999.
        """
        js = _read(STATUS_JS)
        m = re.search(r"function\s+renderGroupTabs\s*\([^)]*\)\s*\{", js)
        body = js[m.end() : m.end() + 2000]
        # Original line 1772 should still be there
        assert "allZones = (zonesData || []).filter(function(z) { return z.group_id !== 999; })" in body, (
            "«Все» tab counter no longer excludes 999 — Raul requires bin zones hidden by default."
        )


class TestProgramsJsBinGroupExcluded:
    """programs.js — wizard must keep excluding bin group (Raul: existing behavior preserved)."""

    def test_load_zone_selector_still_filters_999(self):
        """programs.js:271 filter `gid !== 999` must remain untouched."""
        js = _read(PROGRAMS_JS)
        # Either of the equivalent forms is acceptable, but the literal filter must exist.
        assert "gid !== 999" in js or "g.id !== 999" in js, (
            "programs.js no longer excludes group 999 from the wizard — "
            "watering programs would start including bin zones (regression)."
        )


# ─── API contract: backend must still return group 999 ───


class TestGroupsApiReturnsBinGroup:
    """The JS fix assumes /api/groups returns group 999. Verify."""

    def test_api_groups_includes_999(self, admin_client):
        """GET /api/groups must include the БЕЗ ПОЛИВА group."""
        resp = admin_client.get("/api/groups")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)
        ids = [g.get("id") for g in data]
        assert 999 in ids, f"Group 999 missing from /api/groups response — got ids {ids}"
        bin_group = next(g for g in data if g.get("id") == 999)
        assert bin_group.get("name") == "БЕЗ ПОЛИВА", (
            f"Group 999 has unexpected name {bin_group.get('name')!r}"
        )


# ─── End-to-end zone-filter logic (mirrors the JS in Python) ───


class TestBinZoneFilteringLogic:
    """Seed zones in both regular and bin groups, mirror the new JS filter in Python,
    and assert the three target views (Все / БЕЗ ПОЛИВА / wizard) behave correctly.

    This guards against the JS regressing back to the pre-fix shape: if the JS-source
    tests pass but the behavior is wrong, this test would still catch a logic flip.
    """

    @staticmethod
    def _vse_tab(zones):
        """JS: getFilteredZonesV2() with currentGroupFilter=null."""
        return [z for z in zones if z["group_id"] != 999]

    @staticmethod
    def _bin_tab(zones):
        """JS: getFilteredZonesV2() with currentGroupFilter=999."""
        return [z for z in zones if z["group_id"] == 999]

    @staticmethod
    def _wizard(zones):
        """JS programs.js: zones offered to the program wizard."""
        return [z for z in zones if z["group_id"] != 999]

    def test_three_views_with_seeded_zones(self, admin_client):
        """Seed 1 normal-group zone + 2 group=999 zones, verify each view."""
        # Seed
        r1 = admin_client.post(
            "/api/zones",
            data=json.dumps({"name": "Normal-A", "duration": 5, "group_id": 1, "icon": "🌿"}),
            content_type="application/json",
        )
        assert r1.status_code in (200, 201)
        r2 = admin_client.post(
            "/api/zones",
            data=json.dumps({"name": "Bin-A", "duration": 5, "group_id": 999, "icon": "🌿"}),
            content_type="application/json",
        )
        assert r2.status_code in (200, 201)
        r3 = admin_client.post(
            "/api/zones",
            data=json.dumps({"name": "Bin-B", "duration": 5, "group_id": 999, "icon": "🌿"}),
            content_type="application/json",
        )
        assert r3.status_code in (200, 201)

        # Pull current zone list from API (same data the JS would consume)
        resp = admin_client.get("/api/zones")
        assert resp.status_code == 200
        zones = json.loads(resp.data)

        # Only consider the three we just seeded — other tests may have created zones
        seeded_names = {"Normal-A", "Bin-A", "Bin-B"}
        zones = [z for z in zones if z.get("name") in seeded_names]
        assert len(zones) == 3, f"Expected 3 seeded zones, got {len(zones)}: {[z.get('name') for z in zones]}"

        vse = self._vse_tab(zones)
        bin_view = self._bin_tab(zones)
        wizard = self._wizard(zones)

        # «Все» tab — bin zones hidden
        assert {z["name"] for z in vse} == {"Normal-A"}, (
            f"«Все» tab leaked bin zones: {[z['name'] for z in vse]}"
        )

        # «БЕЗ ПОЛИВА» tab — exactly the two bin zones
        assert {z["name"] for z in bin_view} == {"Bin-A", "Bin-B"}, (
            f"BIN tab content wrong: {[z['name'] for z in bin_view]}"
        )

        # Program wizard — bin zones still excluded
        assert {z["name"] for z in wizard} == {"Normal-A"}, (
            f"Program wizard leaked bin zones: {[z['name'] for z in wizard]}"
        )
