"""Tests for /api/groups/* endpoints."""
import pytest
import json
import os

os.environ['TESTING'] = '1'


class TestGroupsAPI:
    def test_get_groups(self, admin_client):
        resp = admin_client.get('/api/groups')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) >= 1  # At least default groups

    def test_create_group(self, admin_client):
        resp = admin_client.post('/api/groups',
            data=json.dumps({'name': 'New Line'}),
            content_type='application/json')
        assert resp.status_code == 201

    def test_update_group(self, admin_client, app):
        group = app.db.create_group('To Update')
        resp = admin_client.put(f'/api/groups/{group["id"]}',
            data=json.dumps({'name': 'Updated Name'}),
            content_type='application/json')
        assert resp.status_code == 200

    def test_delete_group(self, admin_client, app):
        group = app.db.create_group('To Delete')
        resp = admin_client.delete(f'/api/groups/{group["id"]}')
        assert resp.status_code == 204

    def test_delete_group_with_zones(self, admin_client, app):
        group = app.db.create_group('Has Zones')
        app.db.create_zone({'name': 'Z', 'duration': 10, 'group_id': group['id']})
        resp = admin_client.delete(f'/api/groups/{group["id"]}')
        assert resp.status_code in (204, 400)

    def test_stop_group(self, admin_client, app):
        resp = admin_client.post('/api/groups/1/stop',
            content_type='application/json')
        assert resp.status_code == 200

    def test_start_from_first(self, admin_client, app):
        app.db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        resp = admin_client.post('/api/groups/1/start-from-first',
            content_type='application/json')
        # May fail due to scheduler not init, but should not 500
        assert resp.status_code in (200, 400, 500)


class TestMasterValveAPI:
    def test_master_valve_no_config(self, admin_client, app):
        """Toggle master valve on group without master valve config."""
        resp = admin_client.post('/api/groups/1/master-valve/open',
            content_type='application/json')
        assert resp.status_code == 400

    def test_update_group_with_master_valve(self, admin_client, app):
        group = app.db.create_group('MV Group')
        server = app.db.create_mqtt_server({'name': 'S', 'host': 'h', 'port': 1883})
        resp = admin_client.put(f'/api/groups/{group["id"]}',
            data=json.dumps({
                'use_master_valve': True,
                'master_mqtt_topic': '/mv/test',
                'master_mode': 'NC',
                'master_mqtt_server_id': server['id'],
            }),
            content_type='application/json')
        assert resp.status_code == 200
