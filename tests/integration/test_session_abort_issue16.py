"""Integration tests for issue #16: stop must abort the entire active session.

Reproducer for the bug described at
https://github.com/Raul-1996/irrigation/issues/16 — single-zone stop
endpoint left the group sequencer thread alive, so the next zone in the
sequence started by itself after the stopped zone's timer expired.

The architectural fix (specs/issue-16-architecture.md §3) makes the
single-zone stop endpoints route through cancel_group_jobs when an active
group session is in flight. This file proves the bug exists pre-fix and
that the fix closes it.
"""
import os
import threading
import time

os.environ['TESTING'] = '1'


class TestSessionAbortIssue16:
    def test_full_watering_cycle_zone_stop_aborts_sequence(self, admin_client, app):
        """Reproducer: stopping zone-1 mid-sequence must abort the whole session.

        In TESTING mode, start_group_sequence() runs synchronously and ONLY
        sets the first zone ON (the per-zone countdown / advance loop is
        skipped — see _run_group_sequence's TESTING short-circuit).  This
        means we cannot directly observe "zone 2 starts on its own" in a
        unit-fast test.  Instead we observe the *mechanism* that drives the
        bug:

          - Pre-fix: api_zone_mqtt_stop closes the valve but never sets
            group_cancel_events[gid]. Sequencer thread, gated by that
            Event, is unaware -> in production it would advance to the
            next zone after the timer expires.
          - Post-fix: api_zone_mqtt_stop detects the active session and
            calls cancel_group_jobs(gid), which .set()s the Event ->
            sequencer thread breaks out at its next 1s tick.

        We assert on group_cancel_events[gid].is_set() because that is the
        single ground-truth signal the sequencer reads to decide
        continue-vs-abort.
        """
        # Build a 3-zone group so the sequence has somewhere to advance to.
        group = app.db.create_group('Issue16 Group')
        z1 = app.db.create_zone({
            'name': 'Issue16 Z1', 'duration': 1, 'group_id': group['id'],
            'topic': '/test/i16/z1', 'mqtt_server_id': None,
        })
        app.db.create_zone({
            'name': 'Issue16 Z2', 'duration': 1, 'group_id': group['id'],
            'topic': '/test/i16/z2', 'mqtt_server_id': None,
        })
        app.db.create_zone({
            'name': 'Issue16 Z3', 'duration': 1, 'group_id': group['id'],
            'topic': '/test/i16/z3', 'mqtt_server_id': None,
        })

        # Init the scheduler and start a manual group sequence so the
        # session predicate group_cancel_events[gid] is populated.
        from irrigation_scheduler import init_scheduler, get_scheduler
        sched = init_scheduler(app.db)
        assert sched is not None
        # Pre-condition: no active session.
        assert not sched.is_group_session_active(group['id'])

        ok = sched.start_group_sequence(int(group['id']))
        assert ok is True

        # start_group_sequence registered the cancel-event; in TESTING the
        # synchronous _run_group_sequence skipped the finally cleanup, so
        # the entry is still there.
        assert sched.is_group_session_active(group['id']), (
            'precondition: start_group_sequence must have registered a '
            'cancel-event for the group'
        )
        cancel_event = sched.group_cancel_events.get(group['id'])
        assert isinstance(cancel_event, threading.Event)
        assert not cancel_event.is_set(), 'precondition: event not set yet'

        # User taps stop on zone-1 of the running sequence.
        resp = admin_client.post(
            f'/api/zones/{z1["id"]}/mqtt/stop', content_type='application/json'
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

        # Behaviour the fix MUST deliver: the group sequence is aborted.
        # The sequencer thread is gated on cancel_event.is_set(); a proper
        # abort flips that flag (via cancel_group_jobs).  Pre-fix, the
        # endpoint only closed the valve and the flag stayed False, so
        # the sequencer would advance to zone-2 on its next tick.
        cancel_event = sched.group_cancel_events.get(group['id'])
        # cancel_group_jobs may also pop the event after a cleanup race;
        # either "set" or "popped" proves the abort fired.  Pre-fix
        # behaviour: still present AND not set -> bug.
        if cancel_event is None:
            # Acceptable: the abort path fully cleaned up.
            return
        assert cancel_event.is_set(), (
            'BUG: api_zone_mqtt_stop did not abort the group session — '
            'cancel_event is still unset, so the sequencer would advance '
            'to the next zone after zone-1 timer expires (issue #16).'
        )

    def test_solo_zone_stop_unchanged_behaviour(self, admin_client, app):
        """Regression: solo-zone stop (no active session) must not flip session state."""
        group = app.db.create_group('Issue16 Solo')
        z = app.db.create_zone({
            'name': 'Issue16 Solo Z', 'duration': 5, 'group_id': group['id'],
            'topic': '/test/i16/solo', 'mqtt_server_id': None,
        })

        from irrigation_scheduler import init_scheduler
        sched = init_scheduler(app.db)
        assert sched is not None
        # Sanity: no active session for this group.
        assert not sched.is_group_session_active(group['id'])

        # Manually mark the zone ON (no group sequence in flight).
        app.db.update_zone(z['id'], {'state': 'on', 'watering_start_time': '2026-01-01 10:00:00'})

        # User stops the solo zone.
        resp = admin_client.post(
            f'/api/zones/{z["id"]}/mqtt/stop', content_type='application/json'
        )
        assert resp.status_code == 200

        # Zone must be off; session predicate must remain False (no
        # spurious event got planted).
        zone_after = app.db.get_zone(z['id'])
        assert zone_after['state'] == 'off'
        assert not sched.is_group_session_active(group['id'])
        # No session_aborted_by_user audit row should have been written.
        rows = app.db.get_audit_logs(action_type='session_aborted_by_user')
        assert all(
            r.get('target') != f'group:{group["id"]}' for r in rows
        ), 'solo stop must not emit session_aborted_by_user'

    def test_zone_stop_aborts_full_sequence_outcome(self, admin_client, app):
        """Outcome reproducer (spec §4.3 #10): zones 2 and 3 must NEVER
        reach state='on' after a mid-sequence single-zone stop.

        This complements the mechanism-level test above with a real
        outcome assertion: we run the genuine per-zone countdown loop
        (bypassing the synchronous-first-zone short-circuit via
        SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ=1), tap the API stop
        endpoint mid-flight, and POLL the zone state continuously for
        the full window the bug would use to advance the sequence.

        Pre-fix, zone 2 turns 'on' transiently at ~1s after sequence
        start (zone 1's truncated 1-second timer expires, sequence
        advances, zone 2 ON for 1s, then OFF — so a snapshot at end-of-
        run misses it). A polling loop captures the transient ON state.
        Post-fix, zone 2 never sees 'on' because cancel_group_jobs flips
        the cancel-event in time.

        Per-zone duration is truncated to 1s by the existing TESTING
        branch in `_run_group_sequence` (`total_seconds = min(6, max(1,
        duration))`).  We poll every 50ms for 5s.
        """
        group = app.db.create_group('Issue16 OutcomeGroup')
        z1 = app.db.create_zone({
            'name': 'Outcome Z1', 'duration': 1, 'group_id': group['id'],
            'topic': '/test/i16/out/z1', 'mqtt_server_id': None,
        })
        z2 = app.db.create_zone({
            'name': 'Outcome Z2', 'duration': 1, 'group_id': group['id'],
            'topic': '/test/i16/out/z2', 'mqtt_server_id': None,
        })
        z3 = app.db.create_zone({
            'name': 'Outcome Z3', 'duration': 1, 'group_id': group['id'],
            'topic': '/test/i16/out/z3', 'mqtt_server_id': None,
        })

        from irrigation_scheduler import init_scheduler
        sched = init_scheduler(app.db)
        assert sched is not None

        # Plant the cancel-event BEFORE invoking _run_group_sequence (this
        # is what start_group_sequence normally does on the same thread).
        # Skipping start_group_sequence here lets us also bypass its
        # apscheduler add_job machinery — we run the sequence directly in
        # a daemon thread.
        cancel_event = threading.Event()
        sched.group_cancel_events[group['id']] = cancel_event

        zone_ids = [z1['id'], z2['id'], z3['id']]
        # SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ=1 makes _run_group_sequence
        # run the real per-zone loop (truncated to 1s/zone via the inner
        # TESTING branch).  We restore the env var afterwards.
        prior = os.environ.get('SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ')
        os.environ['SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ'] = '1'

        # ever_on[zid] = True iff at any poll tick we saw that zone 'on'.
        # Snapshot-at-end is insufficient because zone 2/3 turns ON for
        # only ~1 second when the bug fires.
        ever_on = {z2['id']: False, z3['id']: False}

        try:
            t = threading.Thread(
                target=sched._run_group_sequence,
                args=(group['id'], zone_ids, 1),
                daemon=True,
            )
            t.start()

            # Give the sequence ~0.3s to start zone 1.
            time.sleep(0.3)
            assert app.db.get_zone(z1['id'])['state'] == 'on', (
                'precondition: zone 1 should be ON shortly after sequence start'
            )
            assert app.db.get_zone(z2['id'])['state'] == 'off'
            assert app.db.get_zone(z3['id'])['state'] == 'off'

            # User taps stop on zone 1 mid-sequence.
            resp = admin_client.post(
                f'/api/zones/{z1["id"]}/mqtt/stop',
                content_type='application/json',
            )
            assert resp.status_code == 200, resp.get_data(as_text=True)

            # Poll every 50ms for 5s.  Without the fix zone 2 turns 'on'
            # at ~1s after sequence start (zone 1's 1s timer expires →
            # sequence advances).  With the fix, the cancel-event flips
            # at the next 1s tick of the sequence loop and zone 2 never
            # starts.  Polling captures the transient ON state that an
            # end-of-run snapshot would miss.
            poll_deadline = time.monotonic() + 5.0
            while time.monotonic() < poll_deadline:
                if app.db.get_zone(z2['id'])['state'] == 'on':
                    ever_on[z2['id']] = True
                if app.db.get_zone(z3['id'])['state'] == 'on':
                    ever_on[z3['id']] = True
                if not t.is_alive():
                    break
                time.sleep(0.05)
            t.join(timeout=2)
        finally:
            if prior is None:
                os.environ.pop('SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ', None)
            else:
                os.environ['SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ'] = prior

        # OUTCOME ASSERTION: zones 2 and 3 must never have turned on at
        # any point during the polling window.
        assert not ever_on[z2['id']], (
            'BUG #16: zone 2 reached state="on" at some poll tick after '
            'the zone-1 stop — the sequence advanced instead of aborting.'
        )
        assert not ever_on[z3['id']], (
            'BUG #16: zone 3 reached state="on" at some poll tick after '
            'the zone-1 stop — the sequence advanced instead of aborting.'
        )
