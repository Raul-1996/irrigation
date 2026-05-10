"""Integration tests for issue #14 — skip current zone in group watering.

Tests the per-group threading.Event "skip current" mechanism:
- API endpoint /api/groups/<gid>/skip-current
- Sequencer loop polling and behavior
- Edge cases: no active session, double-click, finally cleanup
"""
import os
import json
import time
import threading
from unittest.mock import patch, MagicMock

import pytest

os.environ['TESTING'] = '1'


def poll_until(predicate, timeout=3.0, interval=0.05):
    """Block until predicate() returns truthy or timeout. Returns final value."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def _make_scheduler(test_db):
    """Create a fresh IrrigationScheduler bound to test_db (no APScheduler start)."""
    # Patch database.db FIRST so transitive imports see the test DB
    import database as _dbmod
    _dbmod.db = test_db
    from irrigation_scheduler import IrrigationScheduler
    sch = IrrigationScheduler(test_db)
    # Don't call start() — we don't need APScheduler for these tests; we drive
    # _run_group_sequence directly.
    return sch


class TestSkipEndpointAPI:
    def test_skip_no_active_session_returns_400(self, admin_client, app):
        """No skip event is set; group exists but no session active → 400 clean message."""
        app.db.create_zone({'name': 'Z1', 'duration': 5, 'group_id': 1, 'topic': '/t/z1'})
        sch = _make_scheduler(app.db)
        # Empty group_cancel_events => is_group_session_active returns False
        with patch('routes.groups_api.get_scheduler', return_value=sch):
            resp = admin_client.post('/api/groups/1/skip-current',
                                     content_type='application/json', data='{}')
        assert resp.status_code == 400, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body['success'] is False
        assert 'актив' in (body.get('message') or '').lower()

    def test_skip_group_not_found_returns_404(self, admin_client, app):
        """Skip on a non-existent group → 404."""
        resp = admin_client.post('/api/groups/9999/skip-current',
                                 content_type='application/json', data='{}')
        assert resp.status_code == 404
        body = resp.get_json()
        assert body['success'] is False

    def test_skip_active_session_sets_event_and_returns_200(self, admin_client, app):
        """With active session + active zone, skip succeeds and the scheduler
        event is set. Outcome-based: poll the event from the API thread."""
        z1 = app.db.create_zone({'name': 'Z1', 'duration': 5, 'group_id': 1, 'topic': '/t/z1'})
        app.db.create_zone({'name': 'Z2', 'duration': 5, 'group_id': 1, 'topic': '/t/z2'})
        app.db.update_zone(z1['id'], {'state': 'on'})

        sch = _make_scheduler(app.db)
        # Simulate "session active": registered cancel event
        sch.group_cancel_events[1] = threading.Event()
        # Inject our scheduler into the route's get_scheduler()
        with patch('routes.groups_api.get_scheduler', return_value=sch):
            resp = admin_client.post('/api/groups/1/skip-current',
                                     content_type='application/json', data='{}')

        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body['success'] is True
        assert body['skipped_zone_id'] == z1['id']
        # Outcome: skip event must be set so the (real) loop would consume it.
        ev = sch.group_skip_current_events.get(1)
        assert ev is not None and ev.is_set(), "skip event not set after successful skip API call"


class TestSkipSequencerLoop:
    """Drive _run_group_sequence directly with TESTING off, mocked I/O."""

    def _patches(self, test_db, sch, started_zones, stopped_zones):
        """Build a list of context managers patching all heavy I/O for the loop.

        started_zones and stopped_zones are mutable lists where the test
        records which zones were started/stopped, so it can assert sequence.
        """
        def _fake_start(zid):
            test_db.update_zone(zid, {'state': 'on'})
            started_zones.append(zid)
            return True

        def _fake_stop(zid, reason='auto', force=False,
                       master_close_immediately=False, skip_master_close=False):
            test_db.update_zone(zid, {'state': 'off', 'watering_start_time': None})
            stopped_zones.append(zid)
            return True

        # Replace module-level TESTING in irrigation_scheduler so the function
        # takes the real (non-short-circuit) path.
        import irrigation_scheduler as _is_mod
        return [
            patch.object(_is_mod, 'TESTING', False),
            # _check_weather_skip: short-circuit no skip
            patch.object(sch, '_check_weather_skip', return_value={'skip': False}),
            # _get_weather_adjusted_duration: passthrough
            patch.object(sch, '_get_weather_adjusted_duration',
                         side_effect=lambda zid, base: base),
            # zones_state.update_zone_state: route through plain DB update so we
            # don't depend on audit infrastructure.
            patch('services.zones_state.update_zone_state',
                  side_effect=lambda zid, fields, audit_reason=None, db=None:
                      test_db.update_zone(zid, fields)),
            # zone_control.stop_zone: minimal DB update
            patch('services.zone_control.stop_zone', side_effect=_fake_stop),
            # Avoid scheduling a real hard-stop APScheduler job
            patch.object(sch, 'schedule_zone_hard_stop', return_value=None),
            # Avoid MQTT
            patch('services.mqtt_pub.publish_mqtt_value', return_value=True),
            # Avoid db.reschedule_group_to_next_program touching real schedules
            patch.object(test_db, 'reschedule_group_to_next_program', return_value=None),
        ]

    def test_skip_advances_to_next_zone(self, app):
        """Outcome: 3-zone group, fire skip on zone1 → zone1 stops, zone2 starts."""
        test_db = app.db
        z1 = test_db.create_zone({'name': 'Z1', 'duration': 1, 'group_id': 1, 'topic': '/t/z1'})
        z2 = test_db.create_zone({'name': 'Z2', 'duration': 1, 'group_id': 1, 'topic': '/t/z2'})
        z3 = test_db.create_zone({'name': 'Z3', 'duration': 1, 'group_id': 1, 'topic': '/t/z3'})

        sch = _make_scheduler(test_db)
        sch.group_cancel_events[1] = threading.Event()

        started = []
        stopped = []
        patches = self._patches(test_db, sch, started, stopped)
        for p in patches:
            p.start()
        try:
            # Run the sequencer in a background thread; total duration would be
            # 3 minutes (3*1) but we'll skip everything quickly.
            t = threading.Thread(
                target=sch._run_group_sequence,
                args=(1, [z1['id'], z2['id'], z3['id']]),
                kwargs={'override_duration': 1},  # 60 seconds per zone
                daemon=True,
            )
            t.start()

            # Wait for zone1 to start
            assert poll_until(lambda: test_db.get_zone(z1['id'])['state'] == 'on',
                              timeout=3.0), "zone1 never started"

            # Fire skip
            assert sch.request_skip_current_zone(1) is True

            # Outcome: zone1 stops AND zone2 starts within ~2s of skip
            assert poll_until(lambda: test_db.get_zone(z1['id'])['state'] == 'off',
                              timeout=3.0), "zone1 did not stop after skip"
            assert poll_until(lambda: test_db.get_zone(z2['id'])['state'] == 'on',
                              timeout=3.0), "zone2 did not start after skip"

            # zone3 is still 'off' at this moment (not yet skipped)
            assert test_db.get_zone(z3['id'])['state'] == 'off'

            # Cleanup: cancel session so loop exits
            sch.group_cancel_events[1].set()
            t.join(timeout=5.0)
            assert not t.is_alive(), "sequencer thread did not exit"

            # Audit: zone_skip log was emitted
            try:
                logs = test_db.get_logs(event_type='zone_skip') or []
            except (TypeError, AttributeError):
                logs = []
            assert any(
                json.loads(l.get('details') or '{}').get('zone_id') == z1['id']
                for l in logs
            ), f"no zone_skip log for zone {z1['id']}: {logs}"
        finally:
            for p in patches:
                try:
                    p.stop()
                except Exception:
                    pass

    def test_skip_double_click_advances_only_once(self, app):
        """Outcome: two skip presses ~50ms apart → only one zone advances."""
        test_db = app.db
        z1 = test_db.create_zone({'name': 'Z1', 'duration': 1, 'group_id': 1, 'topic': '/t/z1'})
        z2 = test_db.create_zone({'name': 'Z2', 'duration': 1, 'group_id': 1, 'topic': '/t/z2'})
        z3 = test_db.create_zone({'name': 'Z3', 'duration': 1, 'group_id': 1, 'topic': '/t/z3'})

        sch = _make_scheduler(test_db)
        sch.group_cancel_events[1] = threading.Event()

        started = []
        stopped = []
        patches = self._patches(test_db, sch, started, stopped)
        for p in patches:
            p.start()
        try:
            t = threading.Thread(
                target=sch._run_group_sequence,
                args=(1, [z1['id'], z2['id'], z3['id']]),
                kwargs={'override_duration': 1},
                daemon=True,
            )
            t.start()

            assert poll_until(lambda: test_db.get_zone(z1['id'])['state'] == 'on',
                              timeout=3.0), "zone1 never started"

            # Two skips back-to-back
            sch.request_skip_current_zone(1)
            sch.request_skip_current_zone(1)  # idempotent — same event already set

            # Outcome: zone2 starts, zone3 does NOT start before we cancel.
            assert poll_until(lambda: test_db.get_zone(z2['id'])['state'] == 'on',
                              timeout=3.0), "zone2 did not start"

            # Window: confirm zone3 is still off — i.e. we didn't double-skip.
            # Wait briefly to let the loop "settle" on zone2's tick.
            time.sleep(0.5)
            assert test_db.get_zone(z3['id'])['state'] == 'off', \
                "double-skip incorrectly advanced past zone2 to zone3"

            # Cleanup
            sch.group_cancel_events[1].set()
            t.join(timeout=5.0)
        finally:
            for p in patches:
                try:
                    p.stop()
                except Exception:
                    pass

    def test_skip_event_cleared_in_finally(self, app):
        """Outcome: after sequencer exits, group_skip_current_events[gid] is gone."""
        test_db = app.db
        z1 = test_db.create_zone({'name': 'Z1', 'duration': 1, 'group_id': 1, 'topic': '/t/z1'})

        sch = _make_scheduler(test_db)
        sch.group_cancel_events[1] = threading.Event()

        started = []
        stopped = []
        patches = self._patches(test_db, sch, started, stopped)
        for p in patches:
            p.start()
        try:
            t = threading.Thread(
                target=sch._run_group_sequence,
                args=(1, [z1['id']]),
                kwargs={'override_duration': 1},
                daemon=True,
            )
            t.start()
            assert poll_until(lambda: test_db.get_zone(z1['id'])['state'] == 'on',
                              timeout=3.0)
            # Set skip then immediately cancel — will populate the dict entry.
            sch.request_skip_current_zone(1)
            assert 1 in sch.group_skip_current_events
            sch.group_cancel_events[1].set()
            t.join(timeout=5.0)
            assert not t.is_alive()

            # Outcome: dict entry popped on exit (per finally block).
            assert 1 not in sch.group_skip_current_events, \
                "group_skip_current_events not cleaned in finally"
            assert 1 not in sch.group_cancel_events, \
                "group_cancel_events not cleaned in finally"
        finally:
            for p in patches:
                try:
                    p.stop()
                except Exception:
                    pass
