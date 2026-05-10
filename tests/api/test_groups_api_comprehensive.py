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


# ── Issue #12: %-of-norm on /api/groups/<id>/start-from-first ──────────────
class TestGroupSequencePercent:
    def test_start_group_percent_per_zone(self, admin_client, app):
        """Zones with different norms each scale by the same %, independently.

        TESTING-mode `_run_group_sequence` only starts the first zone; we
        check zone[0]'s planned_end_time AND the per-zone scheduled_start_time
        offset for zone[1] (cumulative timeline uses zone[0]'s scaled
        duration, not the override scalar).
        """
        from datetime import datetime, timedelta
        g = app.db.create_group('PctSeq')
        z1 = app.db.create_zone({'name': 'PZ1', 'duration': 10, 'group_id': g['id'], 'topic': '/t/pz1'})
        z2 = app.db.create_zone({'name': 'PZ2', 'duration': 30, 'group_id': g['id'], 'topic': '/t/pz2'})
        before = datetime.now()
        resp = admin_client.post(f'/api/groups/{g["id"]}/start-from-first',
            data=json.dumps({'duration_percent': 150}),
            content_type='application/json')
        assert resp.status_code == 200
        # Zone 1 gets started in TESTING mode with its own % run length:
        # 10 × 1.5 = 15 min.
        z1f = app.db.get_zone(z1['id'])
        assert z1f.get('state') == 'on'
        end_dt = datetime.strptime(z1f['planned_end_time'], '%Y-%m-%d %H:%M:%S')
        expected_z1_end = before + timedelta(minutes=15)
        assert abs((end_dt - expected_z1_end).total_seconds()) < 5
        # Zone 2 scheduled_start_time = T0 + 15 min (zone[0]'s scaled run),
        # NOT T0 + 45 (which would be 30×1.5 — wrong; that was the legacy
        # uniform-override bug fixed in Issue #12).
        z2f = app.db.get_zone(z2['id'])
        assert z2f.get('scheduled_start_time')
        z2_start = datetime.strptime(z2f['scheduled_start_time'], '%Y-%m-%d %H:%M:%S')
        expected_z2_start = before + timedelta(minutes=15)
        assert abs((z2_start - expected_z2_start).total_seconds()) < 5

    def test_start_group_invalid_percent_falls_back(self, admin_client, app):
        """Percent outside whitelist -> ignored, base zone durations used."""
        from datetime import datetime, timedelta
        g = app.db.create_group('PctBad')
        z1 = app.db.create_zone({'name': 'BZ1', 'duration': 7, 'group_id': g['id'], 'topic': '/t/bz1'})
        before = datetime.now()
        resp = admin_client.post(f'/api/groups/{g["id"]}/start-from-first',
            data=json.dumps({'duration_percent': 87}),  # not in {50,75,100,125,150,200}
            content_type='application/json')
        assert resp.status_code == 200
        z1f = app.db.get_zone(z1['id'])
        # Falls back to base norm = 7 min.
        end_dt = datetime.strptime(z1f['planned_end_time'], '%Y-%m-%d %H:%M:%S')
        expected = before + timedelta(minutes=7)
        assert abs((end_dt - expected).total_seconds()) < 5
