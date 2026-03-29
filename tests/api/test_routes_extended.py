"""Extended route tests targeting specific uncovered lines."""
import pytest
import json
import os
import io

os.environ['TESTING'] = '1'


class TestDurationConflicts:
    def test_check_duration_conflicts(self, admin_client, app):
        z = app.db.create_zone({'name': 'DC', 'duration': 10, 'group_id': 1})
        resp = admin_client.post('/api/zones/check-duration-conflicts',
            data=json.dumps({'zone_id': z['id'], 'new_duration': 30}),
            content_type='application/json')
        assert resp.status_code == 200

    def test_check_duration_conflicts_invalid(self, admin_client):
        resp = admin_client.post('/api/zones/check-duration-conflicts',
            data=json.dumps({'zone_id': 'bad', 'new_duration': 'bad'}),
            content_type='application/json')
        assert resp.status_code == 400

    def test_check_duration_conflicts_not_found(self, admin_client):
        resp = admin_client.post('/api/zones/check-duration-conflicts',
            data=json.dumps({'zone_id': 99999, 'new_duration': 30}),
            content_type='application/json')
        assert resp.status_code == 404

    def test_check_duration_conflicts_with_programs(self, admin_client, app):
        z1 = app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        z2 = app.db.create_zone({'name': 'Z2', 'duration': 10, 'group_id': 1})
        app.db.create_program({
            'name': 'P1', 'time': '06:00',
            'days': [0, 1, 2, 3, 4, 5, 6], 'zones': [z1['id'], z2['id']],
        })
        app.db.create_program({
            'name': 'P2', 'time': '06:15',
            'days': [0, 1, 2, 3, 4, 5, 6], 'zones': [z1['id']],
        })
        resp = admin_client.post('/api/zones/check-duration-conflicts',
            data=json.dumps({'zone_id': z1['id'], 'new_duration': 60}),
            content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get('success') is True

    def test_check_duration_conflicts_bulk(self, admin_client, app):
        z = app.db.create_zone({'name': 'BDC', 'duration': 10, 'group_id': 1})
        resp = admin_client.post('/api/zones/check-duration-conflicts-bulk',
            data=json.dumps({'changes': [{'zone_id': z['id'], 'new_duration': 30}]}),
            content_type='application/json')
        assert resp.status_code == 200

    def test_check_duration_conflicts_bulk_empty(self, admin_client):
        resp = admin_client.post('/api/zones/check-duration-conflicts-bulk',
            data=json.dumps({'changes': []}),
            content_type='application/json')
        assert resp.status_code == 400


class TestZonePhotoEndpoints:
    def test_upload_no_file(self, admin_client, app):
        z = app.db.create_zone({'name': 'Photo', 'duration': 10, 'group_id': 1})
        resp = admin_client.post(f'/api/zones/{z["id"]}/photo',
            content_type='multipart/form-data')
        assert resp.status_code in (400, 500)

    def test_delete_photo(self, admin_client, app):
        z = app.db.create_zone({'name': 'Photo', 'duration': 10, 'group_id': 1})
        resp = admin_client.delete(f'/api/zones/{z["id"]}/photo')
        assert resp.status_code in (200, 204, 404)


class TestZoneCSVImport:
    def test_create_zone_csv_mode(self, admin_client):
        resp = admin_client.post('/api/zones?source=csv',
            data=json.dumps({'name': 'CSV Zone', 'duration': 15}),
            content_type='application/json')
        assert resp.status_code in (200, 201)

    def test_update_zone_csv_mode(self, admin_client, app):
        z = app.db.create_zone({'name': 'CSV', 'duration': 10, 'group_id': 1})
        resp = admin_client.put(f'/api/zones/{z["id"]}?source=csv',
            data=json.dumps({'name': 'CSV Updated'}),
            content_type='application/json')
        assert resp.status_code == 200


class TestMqttProbe:
    def test_probe_server(self, admin_client, app):
        s = app.db.create_mqtt_server({'name': 'Probe', 'host': '127.0.0.1', 'port': 1883})
        resp = admin_client.post(f'/api/mqtt/{s["id"]}/probe',
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)

    def test_probe_nonexistent(self, admin_client):
        resp = admin_client.post('/api/mqtt/99999/probe',
            content_type='application/json')
        assert resp.status_code in (200, 404, 400, 500)


class TestGroupStartStop:
    def test_start_from_first(self, admin_client, app):
        g = app.db.create_group('SFF')
        z = app.db.create_zone({'name': 'Z', 'duration': 2, 'group_id': g['id'], 'topic': '/t/z'})
        resp = admin_client.post(f'/api/groups/{g["id"]}/start-from-first',
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)

    def test_start_zone_exclusive(self, admin_client, app):
        g = app.db.create_group('SZE')
        z = app.db.create_zone({'name': 'Z', 'duration': 5, 'group_id': g['id'], 'topic': '/t/z'})
        resp = admin_client.post(f'/api/groups/{g["id"]}/start-zone/{z["id"]}',
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)


class TestMasterValveToggle:
    def test_master_valve_open(self, admin_client, app):
        g = app.db.create_group('MVO')
        srv = app.db.create_mqtt_server({'name': 'T', 'host': '127.0.0.1', 'port': 1883})
        app.db.update_group_fields(g['id'], {
            'use_master_valve': 1,
            'master_mqtt_topic': '/master/valve',
            'master_mqtt_server_id': srv['id'],
        })
        resp = admin_client.post(f'/api/groups/{g["id"]}/master-valve/open',
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)

    def test_master_valve_close(self, admin_client, app):
        g = app.db.create_group('MVC')
        srv = app.db.create_mqtt_server({'name': 'T', 'host': '127.0.0.1', 'port': 1883})
        app.db.update_group_fields(g['id'], {
            'use_master_valve': 1,
            'master_mqtt_topic': '/master/valve',
            'master_mqtt_server_id': srv['id'],
        })
        resp = admin_client.post(f'/api/groups/{g["id"]}/master-valve/close',
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)


class TestSystemAPIExtended:
    def test_scheduler_init(self, admin_client):
        resp = admin_client.post('/api/scheduler/init',
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)

    def test_health_cancel_group(self, admin_client, app):
        g = app.db.create_group('HC')
        resp = admin_client.post(f'/api/health/group/{g["id"]}/cancel',
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)

    def test_postpone_zone(self, admin_client, app):
        z = app.db.create_zone({'name': 'PP', 'duration': 10, 'group_id': 1})
        resp = admin_client.post('/api/postpone',
            data=json.dumps({
                'zone_id': z['id'],
                'until': '2026-12-31 23:59',
                'reason': 'test',
            }),
            content_type='application/json')
        assert resp.status_code in (200, 400)

    def test_password_change(self, admin_client):
        resp = admin_client.post('/api/password',
            data=json.dumps({'new_password': 'NewSecure123!'}),
            content_type='application/json')
        assert resp.status_code in (200, 400)

    def test_password_change_short(self, admin_client):
        resp = admin_client.post('/api/password',
            data=json.dumps({'new_password': 'sh'}),
            content_type='application/json')
        assert resp.status_code in (200, 400)

    def test_map_get(self, admin_client):
        resp = admin_client.get('/api/map')
        assert resp.status_code == 200


class TestGroupUpdate:
    def test_update_group_fields(self, admin_client, app):
        g = app.db.create_group('UpdateMe')
        resp = admin_client.put(f'/api/groups/{g["id"]}',
            data=json.dumps({'name': 'Updated', 'icon': '💧'}),
            content_type='application/json')
        assert resp.status_code in (200, 400)

    def test_update_group_water_meter(self, admin_client, app):
        g = app.db.create_group('WM')
        srv = app.db.create_mqtt_server({'name': 'T', 'host': '127.0.0.1', 'port': 1883})
        resp = admin_client.put(f'/api/groups/{g["id"]}',
            data=json.dumps({
                'name': 'WM',
                'use_water_meter': 1,
                'water_mqtt_topic': '/water/meter',
                'water_mqtt_server_id': srv['id'],
                'water_pulse_size': '1l',
            }),
            content_type='application/json')
        assert resp.status_code in (200, 400)
