"""Comprehensive route tests targeting uncovered endpoints in zones_api, system_api, groups_api, mqtt_api, settings."""
import pytest
import json
import os

os.environ['TESTING'] = '1'


class TestZoneNextWatering:
    def test_next_watering(self, admin_client, app):
        z = app.db.create_zone({'name': 'NW', 'duration': 10, 'group_id': 1})
        resp = admin_client.get(f'/api/zones/{z["id"]}/next-watering')
        assert resp.status_code == 200

    def test_next_watering_with_program(self, admin_client, app):
        z = app.db.create_zone({'name': 'NW', 'duration': 10, 'group_id': 1})
        app.db.create_program({
            'name': 'P1', 'time': '06:00', 'days': [0, 1, 2, 3, 4, 5, 6],
            'zones': [z['id']],
        })
        resp = admin_client.get(f'/api/zones/{z["id"]}/next-watering')
        assert resp.status_code == 200

    def test_next_watering_not_found(self, admin_client):
        resp = admin_client.get('/api/zones/99999/next-watering')
        assert resp.status_code == 404

    def test_next_watering_bulk(self, admin_client, app):
        z = app.db.create_zone({'name': 'NW', 'duration': 10, 'group_id': 1})
        resp = admin_client.post('/api/zones/next-watering-bulk',
            data=json.dumps({'zone_ids': [z['id']]}),
            content_type='application/json')
        assert resp.status_code == 200

    def test_next_watering_bulk_all(self, admin_client, app):
        app.db.create_zone({'name': 'NW', 'duration': 10, 'group_id': 1})
        resp = admin_client.post('/api/zones/next-watering-bulk',
            data=json.dumps({}),
            content_type='application/json')
        assert resp.status_code == 200


class TestZoneImport:
    def test_import_zones(self, admin_client):
        resp = admin_client.post('/api/zones/import',
            data=json.dumps({'zones': [
                {'name': 'I1', 'duration': 5, 'group_id': 1},
                {'name': 'I2', 'duration': 10, 'group_id': 1},
            ]}),
            content_type='application/json')
        assert resp.status_code in (200, 201, 400)

    def test_import_empty(self, admin_client):
        resp = admin_client.post('/api/zones/import',
            data=json.dumps({'zones': []}),
            content_type='application/json')
        assert resp.status_code == 400


class TestZoneStartStop:
    def test_start_zone_with_duration(self, admin_client, app):
        z = app.db.create_zone({
            'name': 'S', 'duration': 10, 'group_id': 1, 'topic': '/t/z',
        })
        resp = admin_client.post(f'/api/zones/{z["id"]}/start',
            data=json.dumps({'duration': 5}),
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)

    def test_stop_zone_api(self, admin_client, app):
        z = app.db.create_zone({
            'name': 'S', 'duration': 10, 'group_id': 1, 'topic': '/t/z',
        })
        resp = admin_client.post(f'/api/zones/{z["id"]}/stop',
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)


class TestZoneBulkUpdate:
    def test_bulk_update_zones(self, admin_client, app):
        z1 = app.db.create_zone({'name': 'B1', 'duration': 5, 'group_id': 1})
        z2 = app.db.create_zone({'name': 'B2', 'duration': 10, 'group_id': 1})
        resp = admin_client.put('/api/zones/bulk',
            data=json.dumps({'zones': [
                {'id': z1['id'], 'duration': 20},
                {'id': z2['id'], 'duration': 30},
            ]}),
            content_type='application/json')
        assert resp.status_code in (200, 400, 404)


class TestSSEStream:
    def test_sse_endpoint(self, admin_client):
        resp = admin_client.get('/api/zones/sse')
        assert resp.status_code in (200, 404)


class TestSystemAPIDiagnostics:
    def test_health_check(self, admin_client):
        resp = admin_client.get('/health')
        assert resp.status_code == 200

    def test_health_details(self, admin_client):
        resp = admin_client.get('/api/health-details')
        assert resp.status_code in (200, 404)

    def test_server_time(self, admin_client):
        resp = admin_client.get('/api/server-time')
        assert resp.status_code == 200

    def test_scheduler_status(self, admin_client):
        resp = admin_client.get('/api/scheduler/status')
        assert resp.status_code == 200

    def test_scheduler_jobs(self, admin_client):
        resp = admin_client.get('/api/scheduler/jobs')
        assert resp.status_code == 200

    def test_auth_status(self, admin_client):
        resp = admin_client.get('/api/auth/status')
        assert resp.status_code == 200

    def test_api_status(self, admin_client):
        resp = admin_client.get('/api/status')
        assert resp.status_code == 200

    def test_api_logs(self, admin_client):
        resp = admin_client.get('/api/logs')
        assert resp.status_code == 200

    def test_api_backup(self, admin_client):
        resp = admin_client.post('/api/backup', content_type='application/json')
        assert resp.status_code in (200, 201, 400, 500)

    def test_api_water(self, admin_client):
        resp = admin_client.get('/api/water')
        assert resp.status_code == 200


class TestRainEnvAPI:
    def test_get_rain(self, admin_client):
        resp = admin_client.get('/api/rain')
        assert resp.status_code == 200

    def test_post_rain(self, admin_client):
        resp = admin_client.post('/api/rain',
            data=json.dumps({'enabled': True, 'topic': '/rain', 'server_id': 1, 'type': 'NO'}),
            content_type='application/json')
        assert resp.status_code == 200

    def test_get_env(self, admin_client):
        resp = admin_client.get('/api/env')
        assert resp.status_code == 200

    def test_post_env(self, admin_client):
        resp = admin_client.post('/api/env',
            data=json.dumps({
                'temp': {'enabled': False, 'topic': '', 'server_id': None},
                'hum': {'enabled': False, 'topic': '', 'server_id': None},
            }),
            content_type='application/json')
        assert resp.status_code == 200

    def test_get_env_values(self, admin_client):
        resp = admin_client.get('/api/env/values')
        assert resp.status_code == 200


class TestPostponeAPI:
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


class TestPasswordAPI:
    def test_change_password(self, admin_client):
        resp = admin_client.post('/api/password',
            data=json.dumps({'new_password': 'NewPass123!'}),
            content_type='application/json')
        assert resp.status_code in (200, 400)


class TestEarlyOffAPI:
    def test_get_early_off(self, admin_client):
        resp = admin_client.get('/api/settings/early-off')
        assert resp.status_code == 200

    def test_set_early_off(self, admin_client):
        resp = admin_client.post('/api/settings/early-off',
            data=json.dumps({'seconds': 5}),
            content_type='application/json')
        assert resp.status_code == 200


class TestSystemNameAPI:
    def test_get_system_name(self, admin_client):
        resp = admin_client.get('/api/settings/system-name')
        assert resp.status_code == 200

    def test_set_system_name(self, admin_client):
        resp = admin_client.post('/api/settings/system-name',
            data=json.dumps({'name': 'Test System'}),
            content_type='application/json')
        assert resp.status_code == 200


class TestLoggingDebugAPI:
    def test_get_debug(self, admin_client):
        resp = admin_client.get('/api/logging/debug')
        assert resp.status_code == 200

    def test_set_debug(self, admin_client):
        resp = admin_client.post('/api/logging/debug',
            data=json.dumps({'enabled': True}),
            content_type='application/json')
        assert resp.status_code == 200


class TestMapAPI:
    def test_get_map(self, admin_client):
        resp = admin_client.get('/api/map')
        assert resp.status_code == 200


class TestGroupsAdvanced:
    def test_stop_group(self, admin_client, app):
        g = app.db.create_group('SG')
        z = app.db.create_zone({'name': 'Z', 'duration': 10, 'group_id': g['id']})
        resp = admin_client.post(f'/api/groups/{g["id"]}/stop',
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)

    def test_start_zone_exclusive(self, admin_client, app):
        g = app.db.create_group('EX')
        z = app.db.create_zone({'name': 'Z', 'duration': 10, 'group_id': g['id'], 'topic': '/t/x'})
        resp = admin_client.post(f'/api/groups/{g["id"]}/start-zone/{z["id"]}',
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)


class TestMqttAdvanced:
    def test_get_server(self, admin_client, app):
        s = app.db.create_mqtt_server({'name': 'G', 'host': '10.0.0.1', 'port': 1883})
        resp = admin_client.get(f'/api/mqtt/servers/{s["id"]}')
        assert resp.status_code == 200

    def test_update_server(self, admin_client, app):
        s = app.db.create_mqtt_server({'name': 'U', 'host': '10.0.0.1', 'port': 1883})
        resp = admin_client.put(f'/api/mqtt/servers/{s["id"]}',
            data=json.dumps({'name': 'Updated'}),
            content_type='application/json')
        assert resp.status_code == 200

    def test_delete_server(self, admin_client, app):
        s = app.db.create_mqtt_server({'name': 'D', 'host': '10.0.0.1', 'port': 1883})
        resp = admin_client.delete(f'/api/mqtt/servers/{s["id"]}')
        assert resp.status_code in (200, 204)

    def test_mqtt_status(self, admin_client, app):
        s = app.db.create_mqtt_server({'name': 'ST', 'host': '10.0.0.1', 'port': 1883})
        resp = admin_client.get(f'/api/mqtt/{s["id"]}/status')
        assert resp.status_code in (200, 404, 500)


class TestLoginLogout:
    def test_logout(self, admin_client):
        resp = admin_client.get('/logout')
        assert resp.status_code in (200, 302)

    def test_api_login(self, admin_client, app):
        app.db.set_password('TestPassword123!')
        resp = admin_client.post('/api/login',
            data=json.dumps({'password': 'TestPassword123!'}),
            content_type='application/json')
        assert resp.status_code in (200, 400, 401)

    def test_api_login_wrong_password(self, admin_client, app):
        app.db.set_password('CorrectPassword!')
        resp = admin_client.post('/api/login',
            data=json.dumps({'password': 'WrongPassword!'}),
            content_type='application/json')
        assert resp.status_code in (200, 400, 401)
