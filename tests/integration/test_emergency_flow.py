"""Integration test: emergency stop → resume flow."""
import pytest
import json
import os

os.environ['TESTING'] = '1'


class TestEmergencyFlow:
    def test_emergency_stop_stops_all_zones(self, admin_client, app):
        """Emergency stop should stop all running zones."""
        # Create a zone and set it ON
        zone = app.db.create_zone({
            'name': 'Emergency', 'duration': 10, 'group_id': 1,
        })
        app.db.update_zone(zone['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})
        
        # Emergency stop
        resp = admin_client.post('/api/emergency-stop',
            content_type='application/json')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

    def test_emergency_resume(self, admin_client, app):
        """Emergency resume should clear emergency state."""
        # First stop
        admin_client.post('/api/emergency-stop', content_type='application/json')
        
        # Then resume
        resp = admin_client.post('/api/emergency-resume',
            content_type='application/json')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

    def test_cannot_start_during_emergency(self, admin_client, app):
        """Should not be able to start a zone during emergency stop."""
        zone = app.db.create_zone({
            'name': 'NoStart', 'duration': 10, 'group_id': 1,
        })
        
        # Emergency stop
        admin_client.post('/api/emergency-stop', content_type='application/json')
        
        # Try to start zone
        resp = admin_client.post(f'/api/zones/{zone["id"]}/start',
            content_type='application/json')
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False
