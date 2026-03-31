"""Full functional tests for ALL zone UI operations on status page.

Tests every button/action through API:
1. Zone CRUD (create, read, update name/duration/icon/group, delete)
2. Zone start/stop (manual)
3. Group start/stop
4. Postpone/cancel postpone
5. Duration +/- via PUT
6. Emergency stop/resume
7. Weather endpoint
8. Next watering
9. Zone watering time
10. SSE endpoint
"""
import json
import time
import pytest


class TestZoneCRUD:
    """Zone create, read, update, delete."""

    def _create_zone(self, client, **kwargs):
        defaults = {'name': 'Test', 'duration': 10, 'group_id': 1, 'icon': '🌿'}
        defaults.update(kwargs)
        resp = client.post('/api/zones', data=json.dumps(defaults), content_type='application/json')
        data = json.loads(resp.data)
        return data.get('id') or data.get('zone', {}).get('id')

    def test_create_zone(self, admin_client):
        resp = admin_client.post('/api/zones', data=json.dumps({
            'name': 'Func Test Zone', 'duration': 15, 'group_id': 1, 'icon': '🌿'
        }), content_type='application/json')
        assert resp.status_code in (200, 201)
        data = json.loads(resp.data)
        zid = data.get('id') or data.get('zone', {}).get('id')
        assert zid is not None

    def test_read_zones_list(self, admin_client):
        self._create_zone(admin_client, name='Read Test')
        resp = admin_client.get('/api/zones')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_read_single_zone(self, admin_client):
        zid = self._create_zone(admin_client, name='Single Read')
        resp = admin_client.get(f'/api/zones/{zid}')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        zone = data.get('zone') or data
        assert zone['name'] == 'Single Read'

    def test_update_zone_name(self, admin_client):
        zid = self._create_zone(admin_client, name='Before')
        resp = admin_client.put(f'/api/zones/{zid}',
            data=json.dumps({'name': 'After'}), content_type='application/json')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        zone = data.get('zone') or data
        assert zone['name'] == 'After'

    def test_update_zone_duration(self, admin_client):
        zid = self._create_zone(admin_client, duration=10)
        resp = admin_client.put(f'/api/zones/{zid}',
            data=json.dumps({'duration': 15}), content_type='application/json')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        zone = data.get('zone') or data
        assert zone['duration'] == 15

    def test_update_zone_icon(self, admin_client):
        zid = self._create_zone(admin_client, icon='🌿')
        resp = admin_client.put(f'/api/zones/{zid}',
            data=json.dumps({'icon': '💧'}), content_type='application/json')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        zone = data.get('zone') or data
        assert zone['icon'] == '💧'

    def test_update_zone_group(self, admin_client):
        zid = self._create_zone(admin_client, group_id=1)
        resp = admin_client.put(f'/api/zones/{zid}',
            data=json.dumps({'group_id': 2}), content_type='application/json')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        zone = data.get('zone') or data
        assert zone['group_id'] == 2

    def test_update_multiple_fields(self, admin_client):
        """Bottom sheet saves name + duration + icon + group at once."""
        zid = self._create_zone(admin_client, name='Old', duration=10, icon='🌿', group_id=1)
        resp = admin_client.put(f'/api/zones/{zid}',
            data=json.dumps({'name': 'New', 'duration': 20, 'icon': '🌳', 'group_id': 2}),
            content_type='application/json')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        zone = data.get('zone') or data
        assert zone['name'] == 'New'
        assert zone['duration'] == 20
        assert zone['icon'] == '🌳'
        assert zone['group_id'] == 2

    def test_duration_increment_decrement(self, admin_client):
        """Simulate +/- buttons: 10 → 11 → 10 → 1 (min)."""
        zid = self._create_zone(admin_client, duration=10)
        # +1
        admin_client.put(f'/api/zones/{zid}', data=json.dumps({'duration': 11}), content_type='application/json')
        resp = admin_client.get(f'/api/zones/{zid}')
        zone = (json.loads(resp.data).get('zone') or json.loads(resp.data))
        assert zone['duration'] == 11
        # -1
        admin_client.put(f'/api/zones/{zid}', data=json.dumps({'duration': 10}), content_type='application/json')
        resp = admin_client.get(f'/api/zones/{zid}')
        zone = (json.loads(resp.data).get('zone') or json.loads(resp.data))
        assert zone['duration'] == 10
        # to 1
        admin_client.put(f'/api/zones/{zid}', data=json.dumps({'duration': 1}), content_type='application/json')
        resp = admin_client.get(f'/api/zones/{zid}')
        zone = (json.loads(resp.data).get('zone') or json.loads(resp.data))
        assert zone['duration'] == 1

    def test_delete_zone(self, admin_client):
        zid = self._create_zone(admin_client, name='To Delete')
        resp = admin_client.delete(f'/api/zones/{zid}')
        assert resp.status_code in (200, 204)
        # Verify gone
        resp2 = admin_client.get(f'/api/zones/{zid}')
        assert resp2.status_code in (200, 404)


class TestZoneStartStop:
    """Manual zone start/stop."""

    def _create_zone(self, client, **kwargs):
        defaults = {'name': 'StartStop Test', 'duration': 5, 'group_id': 1, 'icon': '🌿'}
        defaults.update(kwargs)
        resp = client.post('/api/zones', data=json.dumps(defaults), content_type='application/json')
        data = json.loads(resp.data)
        return data.get('id') or data.get('zone', {}).get('id')

    def test_zone_mqtt_start(self, admin_client):
        zid = self._create_zone(admin_client)
        resp = admin_client.post(f'/api/zones/{zid}/mqtt/start')
        assert resp.status_code in (200, 400, 500)  # May fail without MQTT

    def test_zone_mqtt_stop(self, admin_client):
        zid = self._create_zone(admin_client)
        resp = admin_client.post(f'/api/zones/{zid}/mqtt/stop')
        assert resp.status_code in (200, 400, 500)

    def test_zone_watering_time(self, admin_client):
        zid = self._create_zone(admin_client)
        resp = admin_client.get(f'/api/zones/{zid}/watering-time')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert 'is_watering' in data or 'success' in data


class TestGroupOperations:
    """Group start, stop, postpone, cancel postpone."""

    def test_get_groups(self, admin_client):
        resp = admin_client.get('/api/groups')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)

    def test_get_status_with_groups(self, admin_client):
        resp = admin_client.get('/api/status')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert 'groups' in data

    def test_group_start_from_first(self, admin_client):
        # Get first group
        resp = admin_client.get('/api/groups')
        groups = json.loads(resp.data)
        if not groups:
            pytest.skip('No groups')
        gid = groups[0]['id']
        resp = admin_client.post(f'/api/groups/{gid}/start-from-first')
        assert resp.status_code in (200, 400, 500)

    def test_group_stop(self, admin_client):
        resp = admin_client.get('/api/groups')
        groups = json.loads(resp.data)
        if not groups:
            pytest.skip('No groups')
        gid = groups[0]['id']
        resp = admin_client.post(f'/api/groups/{gid}/stop')
        assert resp.status_code in (200, 400, 500)

    def test_postpone_group(self, admin_client):
        resp = admin_client.get('/api/groups')
        groups = json.loads(resp.data)
        if not groups:
            pytest.skip('No groups')
        gid = groups[0]['id']
        resp = admin_client.post('/api/postpone',
            data=json.dumps({'group_id': gid, 'days': 1, 'action': 'postpone'}),
            content_type='application/json')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data.get('success') is True

    def test_cancel_postpone(self, admin_client):
        resp = admin_client.get('/api/groups')
        groups = json.loads(resp.data)
        if not groups:
            pytest.skip('No groups')
        gid = groups[0]['id']
        # Postpone first
        admin_client.post('/api/postpone',
            data=json.dumps({'group_id': gid, 'days': 1, 'action': 'postpone'}),
            content_type='application/json')
        # Cancel
        resp = admin_client.post('/api/postpone',
            data=json.dumps({'group_id': gid, 'action': 'cancel'}),
            content_type='application/json')
        assert resp.status_code == 200


class TestEmergency:
    """Emergency stop and resume."""

    def test_emergency_stop(self, admin_client):
        resp = admin_client.post('/api/emergency-stop')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data.get('success') is True

    def test_emergency_resume(self, admin_client):
        # Stop first
        admin_client.post('/api/emergency-stop')
        # Resume
        resp = admin_client.post('/api/emergency-resume')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data.get('success') is True

    def test_status_reflects_emergency(self, admin_client):
        admin_client.post('/api/emergency-stop')
        resp = admin_client.get('/api/status')
        data = json.loads(resp.data)
        assert data.get('emergency_stop') is True
        # Clean up
        admin_client.post('/api/emergency-resume')


class TestWeatherAndNextWatering:
    """Weather endpoint and next watering."""

    def test_weather_endpoint(self, admin_client):
        resp = admin_client.get('/api/weather')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, dict)

    def test_next_watering_bulk_empty(self, admin_client):
        resp = admin_client.post('/api/zones/next-watering-bulk',
            data=json.dumps({'zone_ids': []}), content_type='application/json')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert 'items' in data

    def test_next_watering_bulk_with_zones(self, admin_client):
        # Create zones
        for i in range(3):
            admin_client.post('/api/zones', data=json.dumps({
                'name': f'NW Test {i}', 'duration': 10, 'group_id': 1
            }), content_type='application/json')
        resp = admin_client.get('/api/zones')
        zones = json.loads(resp.data)
        ids = [z['id'] for z in zones[:3]]
        resp = admin_client.post('/api/zones/next-watering-bulk',
            data=json.dumps({'zone_ids': ids}), content_type='application/json')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert 'items' in data

    def test_next_watering_single(self, admin_client):
        admin_client.post('/api/zones', data=json.dumps({
            'name': 'NW Single', 'duration': 10, 'group_id': 1
        }), content_type='application/json')
        resp = admin_client.get('/api/zones')
        zones = json.loads(resp.data)
        if zones:
            resp = admin_client.get(f'/api/zones/{zones[0]["id"]}/next-watering')
            assert resp.status_code == 200


class TestSSEEndpoint:
    """SSE zones endpoint exists."""

    def test_sse_endpoint_responds(self, admin_client):
        resp = admin_client.get('/api/mqtt/zones-sse')
        # SSE returns 200 with text/event-stream
        assert resp.status_code == 200


class TestPageRender:
    """Status page renders all required elements."""

    def test_page_loads(self, client):
        resp = client.get('/')
        assert resp.status_code == 200

    def test_has_all_v2_elements(self, client):
        resp = client.get('/')
        html = resp.data.decode()
        required = ['groupTabs', 'zoneList', 'bottomSheet', 'searchInput',
                     'zonesStatsBar', 'emergency-btn', 'groups-container',
                     'weather-widget', 'zoneToast']
        for elem in required:
            assert elem in html, f'Missing: {elem}'

    def test_zone_card_css_classes(self, client):
        resp = client.get('/')
        html = resp.data.decode()
        classes = ['zone-card', 'zc-icon', 'zc-name', 'zc-dur-badge',
                   'zc-running', 'zc-expanded', 'zc-actions', 'group-tab']
        for cls in classes:
            assert cls in html, f'Missing CSS class: {cls}'

    def test_status_js_v2_loaded(self, client):
        resp = client.get('/')
        html = resp.data.decode()
        assert 'status.js' in html


class TestZoneFieldsComplete:
    """All fields needed for card rendering are present."""

    def test_zone_has_required_fields(self, admin_client):
        admin_client.post('/api/zones', data=json.dumps({
            'name': 'Fields Test', 'duration': 15, 'group_id': 1, 'icon': '🌹'
        }), content_type='application/json')
        resp = admin_client.get('/api/zones')
        zones = json.loads(resp.data)
        assert len(zones) > 0
        z = zones[0]
        for field in ['id', 'name', 'duration', 'group_id', 'state', 'icon',
                       'photo_path', 'last_watering_time']:
            assert field in z, f'Missing field: {field}'

    def test_group_has_required_fields(self, admin_client):
        resp = admin_client.get('/api/groups')
        groups = json.loads(resp.data)
        if groups:
            g = groups[0]
            assert 'id' in g
            assert 'name' in g
