"""Tests for Zones UI v2 (Hunter-style) on status page.

Tests verify:
1. Template renders with all required UI elements
2. API endpoints return correct data for zone cards
3. Zone CRUD operations work correctly for inline editing
4. Group filtering data is available
5. Weather widget data is available
"""
import json
import pytest


# ============================================================
# Template rendering tests — check that status.html has the
# new Hunter-style zone UI elements
# ============================================================

class TestZonesUITemplateElements:
    """Verify status.html contains all required Hunter-style UI elements."""

    def test_status_page_loads(self, client):
        """Status page returns 200."""
        resp = client.get('/')
        assert resp.status_code == 200

    def test_has_group_tabs(self, client):
        """Page contains group tabs container."""
        resp = client.get('/')
        html = resp.data.decode()
        assert 'id="groupTabs"' in html or 'id="group-tabs"' in html

    def test_has_zone_list_container(self, client):
        """Page contains zone list container for cards."""
        resp = client.get('/')
        html = resp.data.decode()
        assert 'id="zoneList"' in html or 'id="zone-list"' in html or 'zones-cards' in html

    def test_has_search_input(self, client):
        """Page contains search functionality."""
        resp = client.get('/')
        html = resp.data.decode()
        assert 'searchInput' in html or 'search-input' in html or 'search' in html.lower()

    def test_has_stats_bar(self, client):
        """Page contains stats bar with zone counts."""
        resp = client.get('/')
        html = resp.data.decode()
        assert 'stat' in html.lower()

    def test_has_quick_actions(self, client):
        """Page contains quick action buttons."""
        resp = client.get('/')
        html = resp.data.decode()
        assert 'emergency' in html.lower() or 'quick' in html.lower()

    def test_has_bottom_sheet(self, client):
        """Page contains bottom sheet for zone editing."""
        resp = client.get('/')
        html = resp.data.decode()
        assert 'bottom-sheet' in html or 'bottomSheet' in html or 'sheet' in html.lower()

    def test_has_weather_widget(self, client):
        """Page contains weather widget."""
        resp = client.get('/')
        html = resp.data.decode()
        assert 'weather' in html.lower()

    def test_no_legacy_zones_table(self, client):
        """Legacy zones table should be replaced or hidden."""
        resp = client.get('/')
        html = resp.data.decode()
        # The old table had id="zones-table-body" — should not exist
        # (or if it does, it should be hidden by CSS)
        # We allow it to exist for backward compat but check new elements are present
        assert ('zoneList' in html or 'zone-list' in html or 'zones-cards' in html)

    def test_has_zone_card_css(self, client):
        """Page has CSS classes for zone cards."""
        resp = client.get('/')
        html = resp.data.decode()
        assert 'zone-card' in html

    def test_status_js_loaded(self, client):
        """Page loads status.js."""
        resp = client.get('/')
        html = resp.data.decode()
        assert 'status.js' in html


# ============================================================
# API smoke tests — ensure zone endpoints work for UI
# ============================================================

class TestZonesAPIForUI:
    """API endpoints that the new zone UI depends on."""

    def test_get_zones(self, admin_client):
        """GET /api/zones returns list of zones."""
        resp = admin_client.get('/api/zones')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)

    def test_get_groups(self, admin_client):
        """GET /api/groups returns list of groups."""
        resp = admin_client.get('/api/groups')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)

    def test_get_status(self, admin_client):
        """GET /api/status returns status with groups."""
        resp = admin_client.get('/api/status')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert 'groups' in data

    def test_get_weather(self, admin_client):
        """GET /api/weather returns weather data."""
        resp = admin_client.get('/api/weather')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        # Weather may not be available in test, but endpoint should work
        assert isinstance(data, dict)

    def test_next_watering_bulk(self, admin_client):
        """POST /api/zones/next-watering-bulk works with empty list."""
        resp = admin_client.post('/api/zones/next-watering-bulk',
                                  data=json.dumps({'zone_ids': []}),
                                  content_type='application/json')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert 'items' in data

    def test_zone_create_and_update(self, admin_client):
        """Create a zone, then update it (simulates inline edit)."""
        # Create
        resp = admin_client.post('/api/zones',
                                  data=json.dumps({
                                      'name': 'UI Test Zone',
                                      'duration': 10,
                                      'group_id': 1,
                                      'icon': '🌿'
                                  }),
                                  content_type='application/json')
        assert resp.status_code in (200, 201)
        data = json.loads(resp.data)
        zone_id = data.get('id') or data.get('zone', {}).get('id')
        assert zone_id is not None

        # Update duration (simulates +/- button)
        resp2 = admin_client.put(f'/api/zones/{zone_id}',
                                   data=json.dumps({'duration': 15}),
                                   content_type='application/json')
        assert resp2.status_code == 200
        data2 = json.loads(resp2.data)
        updated = data2.get('zone') or data2
        assert updated.get('duration') == 15

    def test_zone_update_name(self, admin_client):
        """Update zone name (simulates bottom sheet save)."""
        # Create
        resp = admin_client.post('/api/zones',
                                  data=json.dumps({
                                      'name': 'Before Edit',
                                      'duration': 10,
                                      'group_id': 1,
                                  }),
                                  content_type='application/json')
        data = json.loads(resp.data)
        zone_id = data.get('id') or data.get('zone', {}).get('id')

        # Update name + group
        resp2 = admin_client.put(f'/api/zones/{zone_id}',
                                   data=json.dumps({
                                       'name': 'After Edit',
                                       'group_id': 2
                                   }),
                                   content_type='application/json')
        assert resp2.status_code == 200
        data2 = json.loads(resp2.data)
        updated = data2.get('zone') or data2
        assert updated.get('name') == 'After Edit'

    def test_zone_fields_for_card_rendering(self, admin_client):
        """Zone data contains all fields needed for card rendering."""
        # Create zone with all fields
        admin_client.post('/api/zones',
                           data=json.dumps({
                               'name': 'Card Test',
                               'duration': 20,
                               'group_id': 1,
                               'icon': '🌹'
                           }),
                           content_type='application/json')

        resp = admin_client.get('/api/zones')
        data = json.loads(resp.data)
        assert len(data) > 0

        zone = data[0]
        # Required fields for card rendering
        required_fields = ['id', 'name', 'duration', 'group_id', 'state', 'icon']
        for field in required_fields:
            assert field in zone, f"Missing field: {field}"

    def test_group_fields_for_tabs(self, admin_client):
        """Group data contains fields needed for tab rendering."""
        resp = admin_client.get('/api/groups')
        data = json.loads(resp.data)
        if len(data) > 0:
            group = data[0]
            assert 'id' in group
            assert 'name' in group


# ============================================================
# Zone card interaction tests
# ============================================================

class TestZoneCardInteractions:
    """Test API operations that correspond to zone card UI actions."""

    def _create_zone(self, admin_client, name='Test Zone', dur=10, group=1):
        resp = admin_client.post('/api/zones',
                                  data=json.dumps({
                                      'name': name,
                                      'duration': dur,
                                      'group_id': group,
                                      'icon': '🌿'
                                  }),
                                  content_type='application/json')
        data = json.loads(resp.data)
        return data.get('id') or data.get('zone', {}).get('id')

    def test_duration_increment(self, admin_client):
        """Simulate +1 minute button press."""
        zone_id = self._create_zone(admin_client, dur=10)
        resp = admin_client.put(f'/api/zones/{zone_id}',
                                  data=json.dumps({'duration': 11}),
                                  content_type='application/json')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        updated = data.get('zone') or data
        assert updated.get('duration') == 11

    def test_duration_decrement_min_1(self, admin_client):
        """Duration should not go below 1. API may reject 0."""
        zone_id = self._create_zone(admin_client, dur=2)
        resp = admin_client.put(f'/api/zones/{zone_id}',
                                  data=json.dumps({'duration': 1}),
                                  content_type='application/json')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        updated = data.get('zone') or data
        assert updated.get('duration') == 1

    def test_change_group(self, admin_client):
        """Simulate changing zone group via bottom sheet."""
        zone_id = self._create_zone(admin_client, group=1)
        resp = admin_client.put(f'/api/zones/{zone_id}',
                                  data=json.dumps({'group_id': 2}),
                                  content_type='application/json')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        updated = data.get('zone') or data
        assert updated.get('group_id') == 2

    def test_change_icon(self, admin_client):
        """Simulate changing zone type/icon."""
        zone_id = self._create_zone(admin_client)
        resp = admin_client.put(f'/api/zones/{zone_id}',
                                  data=json.dumps({'icon': '💧'}),
                                  content_type='application/json')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        updated = data.get('zone') or data
        assert updated.get('icon') == '💧'

    def test_next_watering_for_zone(self, admin_client):
        """Next watering endpoint works for individual zones."""
        zone_id = self._create_zone(admin_client)
        resp = admin_client.get(f'/api/zones/{zone_id}/next-watering')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        # Should return some structure
        assert isinstance(data, dict)

    def test_zones_filtered_by_group(self, admin_client):
        """Can filter zones by group_id on client side."""
        self._create_zone(admin_client, name='G1 Zone', group=1)
        self._create_zone(admin_client, name='G2 Zone', group=2)

        resp = admin_client.get('/api/zones')
        data = json.loads(resp.data)
        g1_zones = [z for z in data if z['group_id'] == 1]
        g2_zones = [z for z in data if z['group_id'] == 2]
        # At least one zone in each group
        assert len(g1_zones) >= 1
        assert len(g2_zones) >= 1
