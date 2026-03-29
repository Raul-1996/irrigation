"""Deep tests for groups API routes."""
import json
import pytest
from unittest.mock import patch, MagicMock


class TestGroupsAPIDeep:
    def test_list_groups(self, admin_client):
        resp = admin_client.get('/api/groups')
        assert resp.status_code == 200

    def test_create_group(self, admin_client):
        resp = admin_client.post('/api/groups',
                                 data=json.dumps({'name': 'TestNewGroup'}),
                                 content_type='application/json')
        assert resp.status_code in (200, 201)

    def test_get_group_detail(self, admin_client, app):
        g = app.db.create_group('DetailG')
        if g:
            resp = admin_client.get(f'/api/groups/{g["id"]}')
            assert resp.status_code in (200, 404, 405)

    def test_update_group(self, admin_client, app):
        g = app.db.create_group('UpdG')
        if g:
            resp = admin_client.put(f'/api/groups/{g["id"]}',
                                    data=json.dumps({'name': 'Updated'}),
                                    content_type='application/json')
            assert resp.status_code == 200

    def test_group_stop(self, admin_client):
        resp = admin_client.post('/api/groups/1/stop')
        assert resp.status_code == 200

    def test_group_start_with_zones(self, admin_client, app):
        app.db.create_zone({'name': 'Z1', 'duration': 1, 'group_id': 1})
        resp = admin_client.post('/api/groups/1/start-from-first')
        assert resp.status_code == 200
