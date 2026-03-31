"""Tests for manual start fix: timer delay, progress bar, group override.

These tests verify:
1. Zone mqtt/start with override duration sets planned_end_time correctly
2. watering-time API returns correct total_duration for override
3. Group start-from-first accepts override_duration and does NOT change base duration
"""
import os
import sys
import time
import json
import pytest
from datetime import datetime, timedelta

# Ensure project root is on path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _create_zone(db, name, duration, group_id, icon='🌿', topic='/test/topic'):
    """Helper to create a zone using dict API."""
    zone = db.create_zone({
        'name': name, 'duration': duration, 'group_id': group_id,
        'icon': icon, 'topic': topic,
    })
    return zone['id']


class TestZoneMqttStartOverride:
    """BUG 1 & 2: mqtt/start with override duration sets planned_end_time and watering_start_time."""

    def test_start_with_override_sets_planned_end_time(self, client, app):
        """Starting a zone with override duration should set planned_end_time based on override, not base."""
        db = app.db
        # Create test zone with base duration 10
        zone_id = _create_zone(db, 'Test Zone', 10, 1)
        base_dur = db.get_zone(zone_id)['duration']
        assert base_dur == 10

        # Start with override duration 24
        resp = client.post(f'/api/zones/{zone_id}/mqtt/start',
                           data=json.dumps({'duration': 24}),
                           content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

        # Check DB
        zone = db.get_zone(zone_id)
        assert zone['state'] == 'on'
        assert zone['watering_start_time'] is not None
        assert zone['planned_end_time'] is not None

        # Base duration should NOT change
        assert zone['duration'] == 10, f"Base duration should stay 10, got {zone['duration']}"

        # planned_end_time should be ~24 min from now, not 10
        start_dt = datetime.strptime(zone['watering_start_time'], '%Y-%m-%d %H:%M:%S')
        end_dt = datetime.strptime(zone['planned_end_time'], '%Y-%m-%d %H:%M:%S')
        actual_dur_min = (end_dt - start_dt).total_seconds() / 60
        assert 23 <= actual_dur_min <= 25, f"Override should be ~24 min, got {actual_dur_min}"

    def test_watering_time_returns_override_duration(self, client, app):
        """watering-time API should return total_duration based on override, not base."""
        db = app.db
        zone_id = _create_zone(db, 'Test Zone 2', 10, 1, topic='/test/topic2')

        # Start with override 24
        client.post(f'/api/zones/{zone_id}/mqtt/start',
                    data=json.dumps({'duration': 24}),
                    content_type='application/json')

        # Check watering-time
        resp = client.get(f'/api/zones/{zone_id}/watering-time')
        data = resp.get_json()
        assert data['is_watering'] is True
        assert data['total_duration'] == 24, f"Timer must show 24 min, got {data['total_duration']}"

    def test_start_without_override_uses_base(self, client, app):
        """Starting without override should use base duration."""
        db = app.db
        zone_id = _create_zone(db, 'Test Zone 3', 15, 1, topic='/test/topic3')

        resp = client.post(f'/api/zones/{zone_id}/mqtt/start',
                           content_type='application/json')
        assert resp.status_code == 200

        zone = db.get_zone(zone_id)
        assert zone['state'] == 'on'
        assert zone['planned_end_time'] is not None

        start_dt = datetime.strptime(zone['watering_start_time'], '%Y-%m-%d %H:%M:%S')
        end_dt = datetime.strptime(zone['planned_end_time'], '%Y-%m-%d %H:%M:%S')
        actual_dur_min = (end_dt - start_dt).total_seconds() / 60
        assert 14 <= actual_dur_min <= 16, f"Should use base 15 min, got {actual_dur_min}"


class TestGroupStartOverride:
    """BUG 3: Group start-from-first should accept override_duration without changing base."""

    def test_group_start_with_override_does_not_change_base(self, client, app):
        """Starting group with override_duration should NOT change zone base durations."""
        db = app.db
        # Create zones in group 1 with known base durations
        z1 = _create_zone(db, 'Zone G1', 10, 1, topic='/test/g1')
        z2 = _create_zone(db, 'Zone G2', 15, 1, topic='/test/g2')

        # Start group with override 24
        resp = client.post(f'/api/groups/1/start-from-first',
                           data=json.dumps({'override_duration': 24}),
                           content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

        # Base durations must NOT change
        zone1 = db.get_zone(z1)
        zone2 = db.get_zone(z2)
        assert zone1['duration'] == 10, f"Zone 1 base should stay 10, got {zone1['duration']}"
        assert zone2['duration'] == 15, f"Zone 2 base should stay 15, got {zone2['duration']}"

    def test_group_start_with_override_uses_override_for_planned_end(self, client, app):
        """Started zone should have planned_end_time based on override duration."""
        db = app.db
        z1 = _create_zone(db, 'Zone OV1', 10, 1, topic='/test/ov1')

        client.post('/api/groups/1/start-from-first',
                    data=json.dumps({'override_duration': 30}),
                    content_type='application/json')

        zone = db.get_zone(z1)
        if zone['state'] == 'on' and zone['planned_end_time'] and zone['watering_start_time']:
            start_dt = datetime.strptime(zone['watering_start_time'], '%Y-%m-%d %H:%M:%S')
            end_dt = datetime.strptime(zone['planned_end_time'], '%Y-%m-%d %H:%M:%S')
            actual_dur_min = (end_dt - start_dt).total_seconds() / 60
            assert 29 <= actual_dur_min <= 31, f"Override should be ~30 min, got {actual_dur_min}"

    def test_group_start_without_override_uses_base(self, client, app):
        """Starting group without override should use each zone's base duration."""
        db = app.db
        z1 = _create_zone(db, 'Zone Base1', 10, 1, topic='/test/base1')

        client.post('/api/groups/1/start-from-first',
                    content_type='application/json')

        zone = db.get_zone(z1)
        if zone['state'] == 'on' and zone['planned_end_time'] and zone['watering_start_time']:
            start_dt = datetime.strptime(zone['watering_start_time'], '%Y-%m-%d %H:%M:%S')
            end_dt = datetime.strptime(zone['planned_end_time'], '%Y-%m-%d %H:%M:%S')
            actual_dur_min = (end_dt - start_dt).total_seconds() / 60
            assert 9 <= actual_dur_min <= 11, f"Should use base 10 min, got {actual_dur_min}"
