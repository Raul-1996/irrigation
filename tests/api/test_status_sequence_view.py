"""Issues #24/#25: /api/status sequence view (sequence_active, switching, remaining_queue).

Cases:
1. Sequence active with current+queue
2. Pre-start: future SSTs only, no past evidence → sequence_active but switching=false
3. Mid-sequence switching: past SST + future SST + no zone on → switching=true
4. Cleanup window: past SST only, queue empty → switching=false
5. Single zone manual (no sequence)
6. Last zone running (no tail)
7. Postponed/emergency suppresses sequence view
8. Stale scheduled_start_time (>5min in the past) is dropped
"""
from datetime import datetime, timedelta
import os
import pytest

os.environ['TESTING'] = '1'


def _fmt(dt):
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def _setup_group_with_zones(app, n=3):
    """Create a group with n zones, return (group_id, [zone_ids])."""
    g = app.db.create_group('SeqG')
    zone_ids = []
    for i in range(n):
        z = app.db.create_zone({
            'name': f'SeqZ{i+1}',
            'duration': 15,
            'group_id': g['id'],
            'topic': f'/t/seq{i+1}',
        })
        zone_ids.append(z['id'])
    return g['id'], zone_ids


def _find_group(payload, gid):
    for g in payload.get('groups', []):
        if int(g['id']) == int(gid):
            return g
    return None


class TestSequenceView:
    def test_sequence_active_with_current_and_queue(self, admin_client, app):
        """Zone1 running, zone2/zone3 future-scheduled → sequence_active, queue=2."""
        gid, zids = _setup_group_with_zones(app, 3)
        now = datetime.now()
        app.db.update_zone(zids[0], {
            'state': 'on',
            'watering_start_time': _fmt(now),
            'planned_end_time': _fmt(now + timedelta(minutes=15)),
        })
        app.db.set_group_scheduled_starts(gid, {
            zids[1]: _fmt(now + timedelta(minutes=15)),
            zids[2]: _fmt(now + timedelta(minutes=30)),
        })
        resp = admin_client.get('/api/status')
        assert resp.status_code == 200
        g = _find_group(resp.get_json(), gid)
        assert g is not None
        assert g['sequence_active'] is True
        assert g['switching'] is False
        assert len(g['remaining_queue']) == 2
        assert g['remaining_queue'][0]['zone_id'] == zids[1]
        assert g['remaining_queue'][1]['zone_id'] == zids[2]
        assert g['remaining_queue'][0]['duration_min'] == 15
        assert g['remaining_queue'][0]['name']

    def test_pre_start_no_strip(self, admin_client, app):
        """Sequence scheduled but no zone has started yet → strip suppressed.

        UX rule: "Дальше" must NOT be shown while the program is merely waiting
        to start. sequence_active stays True (other UI may use it), but
        remaining_queue is empty so the strip doesn't render.
        """
        gid, zids = _setup_group_with_zones(app, 2)
        now = datetime.now()
        # Neither zone is on; both have future starts (pre-start state).
        app.db.set_group_scheduled_starts(gid, {
            zids[0]: _fmt(now + timedelta(seconds=30)),
            zids[1]: _fmt(now + timedelta(minutes=15)),
        })
        resp = admin_client.get('/api/status')
        assert resp.status_code == 200
        g = _find_group(resp.get_json(), gid)
        assert g is not None
        assert g['sequence_active'] is True
        assert g['switching'] is False
        assert g['remaining_queue'] == []

    def test_mid_sequence_switching(self, admin_client, app):
        """Past SST (zone already ran) + future SST + no zone on → switching=true."""
        gid, zids = _setup_group_with_zones(app, 2)
        now = datetime.now()
        # zone1 already started 1 minute ago (past SST, within 5-min stale window),
        # currently off (gap between zones). zone2 scheduled in the near future.
        app.db.set_group_scheduled_starts(gid, {
            zids[0]: _fmt(now - timedelta(minutes=1)),
            zids[1]: _fmt(now + timedelta(seconds=30)),
        })
        resp = admin_client.get('/api/status')
        assert resp.status_code == 200
        g = _find_group(resp.get_json(), gid)
        assert g is not None
        assert g['sequence_active'] is True
        assert g['switching'] is True
        assert len(g['remaining_queue']) == 1
        assert g['remaining_queue'][0]['zone_id'] == zids[1]

    def test_cleanup_window_no_switching(self, admin_client, app):
        """Last zone just stopped, queue empty → switching=false (avoid undefined chip)."""
        gid, zids = _setup_group_with_zones(app, 2)
        now = datetime.now()
        # Both SSTs are in the past (within stale window): sequence has finished
        # but cleanup hasn't cleared SSTs yet.
        app.db.set_group_scheduled_starts(gid, {
            zids[0]: _fmt(now - timedelta(minutes=2)),
            zids[1]: _fmt(now - timedelta(minutes=1)),
        })
        resp = admin_client.get('/api/status')
        assert resp.status_code == 200
        g = _find_group(resp.get_json(), gid)
        assert g is not None
        assert g['remaining_queue'] == []
        assert g['switching'] is False

    def test_single_zone_manual_no_sequence(self, admin_client, app):
        """One zone running, no scheduled_start_time anywhere → sequence_active=false."""
        gid, zids = _setup_group_with_zones(app, 2)
        now = datetime.now()
        app.db.update_zone(zids[0], {
            'state': 'on',
            'watering_start_time': _fmt(now),
            'planned_end_time': _fmt(now + timedelta(minutes=15)),
        })
        resp = admin_client.get('/api/status')
        assert resp.status_code == 200
        g = _find_group(resp.get_json(), gid)
        assert g is not None
        assert g['sequence_active'] is False
        assert g['switching'] is False
        assert g['remaining_queue'] == []

    def test_last_zone_running_no_tail(self, admin_client, app):
        """Last zone in sequence is running, queue empty → remaining_queue=[]."""
        gid, zids = _setup_group_with_zones(app, 2)
        now = datetime.now()
        # Simulate first zone already done, last one running.
        app.db.update_zone(zids[1], {
            'state': 'on',
            'watering_start_time': _fmt(now),
            'planned_end_time': _fmt(now + timedelta(minutes=15)),
        })
        # No future scheduled_start_time set.
        resp = admin_client.get('/api/status')
        assert resp.status_code == 200
        g = _find_group(resp.get_json(), gid)
        assert g is not None
        assert g['remaining_queue'] == []
        assert g['switching'] is False

    def test_postponed_suppresses_sequence(self, admin_client, app):
        """Group in postponed state → sequence_active=false even if scheduled_starts present."""
        gid, zids = _setup_group_with_zones(app, 2)
        now = datetime.now()
        future = (now + timedelta(days=1)).strftime('%Y-%m-%d 23:59:59')
        # Postpone all zones in the group.
        for zid in zids:
            app.db.update_zone_postpone(zid, future, 'manual')
        # Even with future scheduled starts, postponed state must win.
        app.db.set_group_scheduled_starts(gid, {
            zids[0]: _fmt(now + timedelta(minutes=5)),
            zids[1]: _fmt(now + timedelta(minutes=20)),
        })
        resp = admin_client.get('/api/status')
        assert resp.status_code == 200
        g = _find_group(resp.get_json(), gid)
        assert g is not None
        assert g['status'] == 'postponed'
        assert g['sequence_active'] is False
        assert g['remaining_queue'] == []
        assert g['switching'] is False

    def test_stale_starts_dropped(self, admin_client, app):
        """scheduled_start_time older than 5 minutes is treated as stale."""
        gid, zids = _setup_group_with_zones(app, 2)
        now = datetime.now()
        # Both starts are 1 hour in the past — stale.
        app.db.set_group_scheduled_starts(gid, {
            zids[0]: _fmt(now - timedelta(hours=1)),
            zids[1]: _fmt(now - timedelta(minutes=30)),
        })
        resp = admin_client.get('/api/status')
        assert resp.status_code == 200
        g = _find_group(resp.get_json(), gid)
        assert g is not None
        assert g['sequence_active'] is False
        assert g['remaining_queue'] == []
        assert g['switching'] is False
