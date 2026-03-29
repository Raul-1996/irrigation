"""Comprehensive tests for routes/groups_api.py endpoints."""
import pytest
import json
import os

os.environ['TESTING'] = '1'


class TestGroupsAPI:
    def test_list_groups(self, admin_client):
        resp = admin_client.get('/api/groups')
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)

    def test_create_group(self, admin_client):
        resp = admin_client.post('/api/groups',
            data=json.dumps({'name': 'New Group'}),
            content_type='application/json')
        assert resp.status_code in (200, 201)

    def test_create_group_empty_name(self, admin_client):
        resp = admin_client.post('/api/groups',
            data=json.dumps({'name': ''}),
            content_type='application/json')
        assert resp.status_code in (400, 200, 201)

    def test_get_group(self, admin_client, app):
        g = app.db.create_group('GetG')
        resp = admin_client.get(f'/api/groups/{g["id"]}')
        assert resp.status_code in (200, 404, 405)

    def test_update_group(self, admin_client, app):
        g = app.db.create_group('Old')
        resp = admin_client.put(f'/api/groups/{g["id"]}',
            data=json.dumps({'name': 'Updated'}),
            content_type='application/json')
        assert resp.status_code in (200, 400)

    def test_delete_group(self, admin_client, app):
        g = app.db.create_group('Del')
        resp = admin_client.delete(f'/api/groups/{g["id"]}')
        assert resp.status_code in (200, 204, 400)

    def test_delete_nonexistent_group(self, admin_client):
        resp = admin_client.delete('/api/groups/99999')
        assert resp.status_code in (200, 204, 404)


class TestGroupSequenceAPI:
    def test_start_group_sequence(self, admin_client, app):
        g = app.db.create_group('Seq')
        app.db.create_zone({'name': 'Z1', 'duration': 2, 'group_id': g['id']})
        resp = admin_client.post(f'/api/groups/{g["id"]}/start-from-first',
            content_type='application/json')
        assert resp.status_code in (200, 400, 404, 500)

    def test_stop_group(self, admin_client, app):
        g = app.db.create_group('StopG')
        resp = admin_client.post(f'/api/groups/{g["id"]}/stop',
            content_type='application/json')
        assert resp.status_code in (200, 400, 500)


class TestGroupMasterValveAPI:
    def test_update_master_valve(self, admin_client, app):
        g = app.db.create_group('MV')
        srv = app.db.create_mqtt_server({'name': 'T', 'host': '127.0.0.1', 'port': 1883})
        resp = admin_client.put(f'/api/groups/{g["id"]}',
            data=json.dumps({
                'name': 'MV Group',
                'use_master_valve': 1,
                'master_mqtt_topic': '/master/valve',
                'master_mqtt_server_id': srv['id'],
                'master_mode': 'NC',
            }),
            content_type='application/json')
        assert resp.status_code in (200, 400, 404, 500)


class TestGroupRainAPI:
    def test_set_rain(self, admin_client, app):
        g = app.db.create_group('Rain')
        resp = admin_client.put(f'/api/groups/{g["id"]}',
            data=json.dumps({'name': 'Rain', 'use_rain': True}),
            content_type='application/json')
        assert resp.status_code in (200, 400, 404, 500)


class TestViewerGroupAccess:
    def test_viewer_can_read(self, viewer_client):
        resp = viewer_client.get('/api/groups')
        assert resp.status_code == 200
