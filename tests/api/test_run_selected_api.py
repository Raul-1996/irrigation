"""Tests for issue #15 — POST /api/groups/<gid>/run-selected (ad-hoc multi-zone run).

Spec: specs/issue-15-architecture.md §3.1.

Notes:
  - The zones table has no ``enabled`` column on this branch (db/migrations.py).
    Spec §6.1 → drop the "is enabled" check from validation. Test originally
    intended to assert "zone disabled" is reframed as "zone non-existent".
  - In TESTING mode, ``_run_group_sequence`` short-circuits after marking the
    first zone in the list ON, including planned_end_time. We use that to
    assert the override duration was honoured.
"""
import json
import os
import time
from datetime import datetime

import pytest

os.environ['TESTING'] = '1'


def _create_group_with_zones(app, n=3, durations=None):
    """Helper: returns (gid, [zone_id, ...]) with N zones in a fresh group."""
    g = app.db.create_group(f'TestG-{int(time.time() * 1000) % 100000}')
    zone_ids = []
    durs = durations or [10, 20, 30]
    for i in range(n):
        d = durs[i] if i < len(durs) else 10
        z = app.db.create_zone({
            'name': f'Z{i + 1}',
            'duration': d,
            'group_id': g['id'],
        })
        zone_ids.append(int(z['id']))
    return int(g['id']), zone_ids


class TestRunSelectedHappyPath:
    def test_minutes_ok(self, admin_client, app):
        """Minutes mode: 2 zones at 15 min each — first zone gets ON in TESTING mode."""
        gid, zids = _create_group_with_zones(app, n=3, durations=[10, 20, 30])
        resp = admin_client.post(
            f'/api/groups/{gid}/run-selected',
            data=json.dumps({'zones': [zids[0], zids[1]], 'duration': 15}),
            content_type='application/json',
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()
        assert data['success'] is True
        assert data['ad_hoc_program_id'] < 0  # negative sentinel
        # In TESTING mode, the first zone in the list is set ON with planned_end.
        z = app.db.get_zone(zids[0])
        assert z['state'] == 'on'
        assert z.get('planned_end_time')
        # planned_end ≈ start + 15 min
        end_dt = datetime.strptime(z['planned_end_time'], '%Y-%m-%d %H:%M:%S')
        delta = (end_dt - datetime.now()).total_seconds()
        # Allow ±5 sec (test execution) — duration is 15 min = 900 sec.
        assert 895 <= delta <= 905, f'planned_end delta={delta}s'

    def test_percent_ok(self, admin_client, app):
        """Percent mode: 80% of base 20 → 16 min."""
        gid, zids = _create_group_with_zones(app, n=2, durations=[20, 40])
        resp = admin_client.post(
            f'/api/groups/{gid}/run-selected',
            data=json.dumps({'zones': [zids[0], zids[1]], 'duration_percent': 80}),
            content_type='application/json',
        )
        assert resp.status_code == 200, resp.get_json()
        z = app.db.get_zone(zids[0])
        assert z['state'] == 'on'
        end_dt = datetime.strptime(z['planned_end_time'], '%Y-%m-%d %H:%M:%S')
        delta = (end_dt - datetime.now()).total_seconds()
        # 20 * 0.8 = 16 min = 960 sec.
        assert 955 <= delta <= 965, f'planned_end delta={delta}s'

    def test_minutes_wins_over_percent(self, admin_client, app):
        """Both sent → minutes used, percent ignored."""
        gid, zids = _create_group_with_zones(app, n=2, durations=[20, 40])
        resp = admin_client.post(
            f'/api/groups/{gid}/run-selected',
            data=json.dumps({'zones': [zids[0]], 'duration': 5, 'duration_percent': 200}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        z = app.db.get_zone(zids[0])
        end_dt = datetime.strptime(z['planned_end_time'], '%Y-%m-%d %H:%M:%S')
        delta = (end_dt - datetime.now()).total_seconds()
        # If minutes lost: would be 20 * 2 = 40 min. With minutes-wins: 5 min = 300s.
        assert 295 <= delta <= 305, f'planned_end delta={delta}s — minutes did not win'


class TestRunSelectedValidation:
    def test_empty_zones(self, admin_client, app):
        gid, _ = _create_group_with_zones(app, n=2)
        resp = admin_client.post(
            f'/api/groups/{gid}/run-selected',
            data=json.dumps({'zones': []}),
            content_type='application/json',
        )
        assert resp.status_code == 400
        assert 'zones' in resp.get_json()['message']

    def test_zones_missing_key(self, admin_client, app):
        gid, _ = _create_group_with_zones(app, n=2)
        resp = admin_client.post(
            f'/api/groups/{gid}/run-selected',
            data=json.dumps({'duration': 10}),
            content_type='application/json',
        )
        assert resp.status_code == 400

    def test_zone_not_in_group(self, admin_client, app):
        """zone belongs to a different group → 400."""
        gid_a, zids_a = _create_group_with_zones(app, n=1, durations=[10])
        gid_b, zids_b = _create_group_with_zones(app, n=1, durations=[10])
        # Try to start zid_b under gid_a.
        resp = admin_client.post(
            f'/api/groups/{gid_a}/run-selected',
            data=json.dumps({'zones': [zids_b[0]], 'duration': 5}),
            content_type='application/json',
        )
        assert resp.status_code == 400
        assert 'не принадлежит' in resp.get_json()['message']

    def test_zone_does_not_exist(self, admin_client, app):
        """zone_id that is not in the DB → 400 (covers the "disabled" case for now)."""
        gid, _ = _create_group_with_zones(app, n=1)
        resp = admin_client.post(
            f'/api/groups/{gid}/run-selected',
            data=json.dumps({'zones': [9999999], 'duration': 5}),
            content_type='application/json',
        )
        assert resp.status_code == 400
        assert 'не найдена' in resp.get_json()['message']

    def test_group_does_not_exist(self, admin_client, app):
        resp = admin_client.post(
            '/api/groups/9999999/run-selected',
            data=json.dumps({'zones': [1], 'duration': 5}),
            content_type='application/json',
        )
        assert resp.status_code == 404

    def test_invalid_duration_too_high(self, admin_client, app):
        gid, zids = _create_group_with_zones(app, n=1)
        resp = admin_client.post(
            f'/api/groups/{gid}/run-selected',
            data=json.dumps({'zones': [zids[0]], 'duration': 999}),
            content_type='application/json',
        )
        assert resp.status_code == 400

    def test_invalid_percent_too_high(self, admin_client, app):
        gid, zids = _create_group_with_zones(app, n=1)
        resp = admin_client.post(
            f'/api/groups/{gid}/run-selected',
            data=json.dumps({'zones': [zids[0]], 'duration_percent': 500}),
            content_type='application/json',
        )
        assert resp.status_code == 400

    def test_zones_not_int(self, admin_client, app):
        gid, _ = _create_group_with_zones(app, n=1)
        resp = admin_client.post(
            f'/api/groups/{gid}/run-selected',
            data=json.dumps({'zones': ['abc'], 'duration': 5}),
            content_type='application/json',
        )
        assert resp.status_code == 400


class TestManualVsScheduledGuard:
    def test_scheduled_blocked_by_active_manual(self, admin_client, app):
        """If a manual / ad-hoc session is active, _run_program_threaded skips and audits."""
        gid, zids = _create_group_with_zones(app, n=2, durations=[10, 20])

        # Plant a synthetic manual session by setting the cancel event directly.
        # This is exactly the state ad-hoc / manual run leaves behind during its
        # lifetime (start_group_sequence sets it; _run_group_sequence finally clears).
        from irrigation_scheduler import init_scheduler
        sched = init_scheduler(app.db)
        import threading as _th
        sched.group_cancel_events[gid] = _th.Event()
        try:
            assert sched.is_group_session_active(gid) is True

            # Snapshot logs BEFORE the call.
            logs_before = app.db.get_logs() or []
            n_skipped_before = sum(1 for r in logs_before
                                   if (r.get('type') or r.get('action')) == 'prog_skipped_manual_running')

            # Invoke _run_program_threaded directly with a positive program_id.
            # It should hit the issue-#15 guard at top, log the skip, and return.
            sched._run_program_threaded(42, zids, 'TestSched')

            logs_after = app.db.get_logs() or []
            n_skipped_after = sum(1 for r in logs_after
                                  if (r.get('type') or r.get('action')) == 'prog_skipped_manual_running')
            assert n_skipped_after == n_skipped_before + 1, (
                f'expected one prog_skipped_manual_running, '
                f'got {n_skipped_after - n_skipped_before}'
            )

            # And the zones must NOT have been started by the scheduled fire.
            for zid in zids:
                z = app.db.get_zone(zid)
                # Zones started ON by the manual session would be the first zone only —
                # but we never invoked the manual run, only planted the cancel event.
                # The scheduled call should not have flipped any state.
                assert z['state'] != 'on' or z.get('watering_start_source') != 'schedule'
        finally:
            # Cleanup: clear the synthetic event so subsequent tests aren't poisoned.
            sched.group_cancel_events.pop(gid, None)
