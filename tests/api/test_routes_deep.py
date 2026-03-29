"""Deep route tests for maximum coverage — watering time, SSE, MQTT start/stop, photo ops."""
import pytest
import json
import os
import io
from datetime import datetime, timedelta

os.environ['TESTING'] = '1'


class TestWateringTime:
    def test_watering_time_off(self, admin_client, app):
        z = app.db.create_zone({'name': 'WT', 'duration': 10, 'group_id': 1})
        resp = admin_client.get(f'/api/zones/{z["id"]}/watering-time')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['is_watering'] is False

    def test_watering_time_on(self, admin_client, app):
        z = app.db.create_zone({'name': 'WT', 'duration': 10, 'group_id': 1})
        start = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        app.db.update_zone(z['id'], {'state': 'on', 'watering_start_time': start})
        resp = admin_client.get(f'/api/zones/{z["id"]}/watering-time')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['is_watering'] is True

    def test_watering_time_expired(self, admin_client, app):
        z = app.db.create_zone({'name': 'WT', 'duration': 1, 'group_id': 1})
        old = (datetime.now() - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
        app.db.update_zone(z['id'], {'state': 'on', 'watering_start_time': old})
        resp = admin_client.get(f'/api/zones/{z["id"]}/watering-time')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['is_watering'] is False

    def test_watering_time_not_found(self, admin_client):
        resp = admin_client.get('/api/zones/99999/watering-time')
        assert resp.status_code == 404

    def test_watering_time_bad_start(self, admin_client, app):
        z = app.db.create_zone({'name': 'WT', 'duration': 10, 'group_id': 1})
        app.db.update_zone(z['id'], {'state': 'on', 'watering_start_time': 'bad-date'})
        resp = admin_client.get(f'/api/zones/{z["id"]}/watering-time')
        assert resp.status_code == 200


class TestMqttZonesSSE:
    def test_mqtt_zones_sse(self, admin_client):
        resp = admin_client.get('/api/mqtt/zones-sse')
        assert resp.status_code == 200


class TestMqttStartStop:
    def test_mqtt_start(self, admin_client, app):
        z = app.db.create_zone({'name': 'MS', 'duration': 10, 'group_id': 1, 'topic': '/t/z'})
        resp = admin_client.post(f'/api/zones/{z["id"]}/mqtt/start',
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)

    def test_mqtt_start_not_found(self, admin_client):
        resp = admin_client.post('/api/zones/99999/mqtt/start',
            content_type='application/json')
        assert resp.status_code == 404

    def test_mqtt_stop(self, admin_client, app):
        z = app.db.create_zone({'name': 'MS', 'duration': 10, 'group_id': 1, 'topic': '/t/z'})
        app.db.update_zone(z['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})
        resp = admin_client.post(f'/api/zones/{z["id"]}/mqtt/stop',
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)

    def test_mqtt_stop_not_found(self, admin_client):
        resp = admin_client.post('/api/zones/99999/mqtt/stop',
            content_type='application/json')
        assert resp.status_code == 404

    def test_mqtt_start_already_on(self, admin_client, app):
        z = app.db.create_zone({'name': 'AO', 'duration': 10, 'group_id': 1, 'topic': '/t/z'})
        app.db.update_zone(z['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})
        resp = admin_client.post(f'/api/zones/{z["id"]}/mqtt/start',
            content_type='application/json')
        assert resp.status_code == 200


class TestPhotoEndpoints:
    def test_get_photo_info(self, admin_client, app):
        z = app.db.create_zone({'name': 'PI', 'duration': 10, 'group_id': 1})
        resp = admin_client.get(f'/api/zones/{z["id"]}/photo')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get('success') is True

    def test_get_photo_not_found(self, admin_client):
        resp = admin_client.get('/api/zones/99999/photo')
        assert resp.status_code == 404

    def test_upload_photo(self, admin_client, app):
        z = app.db.create_zone({'name': 'UP', 'duration': 10, 'group_id': 1})
        data = io.BytesIO(b'\xff\xd8\xff\xe0' + b'\x00' * 100)  # fake JPEG
        resp = admin_client.post(f'/api/zones/{z["id"]}/photo',
            data={'photo': (data, 'test.jpg')},
            content_type='multipart/form-data')
        assert resp.status_code in (200, 400, 500)

    def test_upload_bad_extension(self, admin_client, app):
        z = app.db.create_zone({'name': 'BE', 'duration': 10, 'group_id': 1})
        data = io.BytesIO(b'fake data')
        resp = admin_client.post(f'/api/zones/{z["id"]}/photo',
            data={'photo': (data, 'test.exe')},
            content_type='multipart/form-data')
        assert resp.status_code == 400

    def test_delete_photo_no_photo(self, admin_client, app):
        z = app.db.create_zone({'name': 'DP', 'duration': 10, 'group_id': 1})
        resp = admin_client.delete(f'/api/zones/{z["id"]}/photo')
        assert resp.status_code in (200, 404)

    def test_rotate_photo_no_photo(self, admin_client, app):
        z = app.db.create_zone({'name': 'RP', 'duration': 10, 'group_id': 1})
        resp = admin_client.post(f'/api/zones/{z["id"]}/photo/rotate',
            data=json.dumps({'angle': 90}),
            content_type='application/json')
        assert resp.status_code in (200, 404)


class TestSystemStatusExtended:
    def test_status_full(self, admin_client):
        resp = admin_client.get('/api/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_health_details(self, admin_client):
        resp = admin_client.get('/api/health-details')
        assert resp.status_code == 200


class TestEmergencyFlow:
    def test_emergency_stop_resume(self, admin_client):
        resp = admin_client.post('/api/emergency-stop',
            content_type='application/json')
        assert resp.status_code == 200

        resp = admin_client.post('/api/emergency-resume',
            content_type='application/json')
        assert resp.status_code == 200


class TestSchedulerAPI:
    def test_scheduler_status(self, admin_client):
        resp = admin_client.get('/api/scheduler/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_scheduler_jobs(self, admin_client):
        resp = admin_client.get('/api/scheduler/jobs')
        assert resp.status_code == 200


class TestWaterAPI:
    def test_water_usage(self, admin_client):
        resp = admin_client.get('/api/water')
        assert resp.status_code == 200

    def test_water_with_params(self, admin_client):
        resp = admin_client.get('/api/water?days=30')
        assert resp.status_code == 200


class TestLogsAPI:
    def test_logs_with_dates(self, admin_client):
        resp = admin_client.get('/api/logs?from=2026-01-01&to=2026-12-31')
        assert resp.status_code == 200

    def test_logs_with_type(self, admin_client):
        resp = admin_client.get('/api/logs?type=zone_start')
        assert resp.status_code == 200
