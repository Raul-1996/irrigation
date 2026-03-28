"""
Tests for groups API — CRUD, start/stop, master valve.
"""
import os
import sys
import json
import pytest
from unittest.mock import patch

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("TESTING", "1")


class TestGroupsAPI:
    def test_get_groups(self, client):
        r = client.get('/api/groups')
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)

    def test_create_group(self, client):
        r = client.post('/api/groups', json={'name': 'Test Pump'})
        assert r.status_code in (200, 201, 400)

    def test_create_group_empty_name(self, client):
        r = client.post('/api/groups', json={'name': ''})
        assert r.status_code in (200, 400)

    def test_update_group(self, client):
        r = client.put('/api/groups/1', json={'name': 'Renamed Group'})
        assert r.status_code in (200, 404, 400)

    def test_delete_group(self, client):
        # Create, then delete
        r = client.post('/api/groups', json={'name': 'Temp Group'})
        if r.status_code in (200, 201):
            data = r.get_json()
            gid = data.get('id') or data.get('group', {}).get('id')
            if gid:
                r2 = client.delete(f'/api/groups/{gid}')
                assert r2.status_code in (200, 204, 404)


class TestGroupOperations:
    @patch('app._publish_mqtt_value', return_value=True)
    def test_stop_group(self, mock_pub, client):
        r = client.post('/api/groups/1/stop')
        assert r.status_code in (200, 400, 404)

    @patch('app._publish_mqtt_value', return_value=True)
    def test_start_from_first(self, mock_pub, client):
        r = client.post('/api/groups/1/start-from-first')
        assert r.status_code in (200, 400, 404)

    @patch('app._publish_mqtt_value', return_value=True)
    def test_start_zone_in_group(self, mock_pub, client):
        r = client.post('/api/groups/1/start-zone/1')
        assert r.status_code in (200, 400, 404)

    @patch('app._publish_mqtt_value', return_value=True)
    def test_master_valve_open(self, mock_pub, client):
        r = client.post('/api/groups/1/master-valve/open')
        assert r.status_code in (200, 400, 404)

    @patch('app._publish_mqtt_value', return_value=True)
    def test_master_valve_close(self, mock_pub, client):
        r = client.post('/api/groups/1/master-valve/close')
        assert r.status_code in (200, 400, 404)

    def test_stop_nonexistent_group(self, client):
        r = client.post('/api/groups/99999/stop')
        assert r.status_code in (200, 400, 404)
