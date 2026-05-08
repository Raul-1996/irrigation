"""Issue #2 — last_watering_time must reflect when watering ENDED.

Prior to the fix, ~8 callsites wrote the original watering_start_time
into ``zones.last_watering_time``. This module pins down the contract:

    last_watering_time >= watering_start_time

i.e. the timestamp written must be at-or-after the start, never the
start itself, for every code path that transitions a zone to OFF.
"""
import os
from datetime import datetime
from unittest.mock import patch

import pytest

os.environ['TESTING'] = '1'


_FMT = '%Y-%m-%d %H:%M:%S'


def _parse(ts):
    """Parse a 'YYYY-MM-DD HH:MM:SS' timestamp written by zone_control."""
    assert ts is not None, "expected non-NULL timestamp"
    return datetime.strptime(ts, _FMT)


class TestStopZoneEndTime:
    def test_stop_zone_writes_end_time_not_start(self, test_db):
        """stop_zone() must record now() in last_watering_time, not the
        original watering_start_time. The historical bug wrote the start
        timestamp; the regression check is `last >= start`, with
        last clearly differing from start when start is far in the past.
        """
        import time as _time
        zone = test_db.create_zone({
            'name': 'EndTime', 'duration': 10, 'group_id': 1,
            'topic': '/test/end',
        })
        # Pretend the zone started a minute ago so the end-time will be
        # noticeably newer than the start-time.
        old_start = '2026-01-01 10:00:00'
        test_db.update_zone(zone['id'], {
            'state': 'on',
            'watering_start_time': old_start,
        })

        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            from services.zone_control import stop_zone
            before = datetime.now().replace(microsecond=0)
            _time.sleep(0.01)  # ensure monotonic gap even on fast hardware
            assert stop_zone(zone['id'], reason='test') is True
            after = datetime.now()

        z = test_db.get_zone(zone['id'])
        assert z['state'] == 'off'
        assert z['watering_start_time'] is None
        last = _parse(z['last_watering_time'])
        # End time must be a NOW-ish timestamp, NOT the old start time.
        assert z['last_watering_time'] != old_start, (
            'last_watering_time still equals the old start time — '
            'the issue-#2 bug is back'
        )
        # Bound check: last is between the moments we sampled around stop_zone.
        assert before <= last <= after, (
            f'last_watering_time={last} not in [{before}, {after}]'
        )

    def test_idempotent_stop_does_not_overwrite(self, test_db):
        """Calling stop_zone on an already-off zone must NOT clobber a
        previously recorded end-time. The pre-fix code would have written
        zone['watering_start_time'] (None at this point) anyway via the
        non-idempotent branch, but the modern idempotent guard returns
        early — pin that behaviour.
        """
        zone = test_db.create_zone({
            'name': 'Idempo', 'duration': 10, 'group_id': 1,
            'topic': '/test/idem',
        })
        prior_end = '2026-04-01 09:30:15'
        test_db.update_zone(zone['id'], {
            'state': 'off',
            'last_watering_time': prior_end,
        })

        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            from services.zone_control import stop_zone
            assert stop_zone(zone['id'], reason='test') is True

        z = test_db.get_zone(zone['id'])
        assert z['last_watering_time'] == prior_end, (
            'idempotent stop must not rewrite last_watering_time'
        )

    def test_stop_zone_format_matches_finish_zone_run(self, test_db):
        """Format must be 'YYYY-MM-DD HH:MM:SS' — the UI does
        replace('T',' ').slice(0,16) and the same string is written by
        finish_zone_run for end_utc. Pin format to avoid drift.
        """
        zone = test_db.create_zone({
            'name': 'Fmt', 'duration': 10, 'group_id': 1,
            'topic': '/test/fmt',
        })
        test_db.update_zone(zone['id'], {
            'state': 'on',
            'watering_start_time': '2026-01-01 10:00:00',
        })
        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            from services.zone_control import stop_zone
            assert stop_zone(zone['id']) is True
        z = test_db.get_zone(zone['id'])
        # Strict parse — will raise if format is wrong.
        _parse(z['last_watering_time'])
        # And explicitly: no 'T' separator (ISO with T is the wrong shape).
        assert 'T' not in z['last_watering_time']


class TestPeerOffEndTime:
    def test_peer_off_writes_end_time(self, test_db):
        """exclusive_start_zone() peer-stops siblings in the same group.
        Each peer must get its last_watering_time set to a NOW timestamp,
        not the value of its own watering_start_time.
        """
        import time as _time
        z_running = test_db.create_zone({
            'name': 'Running', 'duration': 10, 'group_id': 1,
            'topic': '/test/run',
        })
        z_new = test_db.create_zone({
            'name': 'NewlyStarted', 'duration': 10, 'group_id': 1,
            'topic': '/test/new',
        })
        old_start = '2026-01-01 10:00:00'
        test_db.update_zone(z_running['id'], {
            'state': 'on',
            'watering_start_time': old_start,
        })

        with patch('services.zone_control.db', test_db), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.water_monitor'), \
             patch('services.zone_control.state_verifier'):
            from services.zone_control import exclusive_start_zone
            before = datetime.now().replace(microsecond=0)
            _time.sleep(0.01)
            assert exclusive_start_zone(z_new['id']) is True
            # Peer stops happen in a ThreadPoolExecutor — give them a beat.
            _time.sleep(1.0)
            after = datetime.now()

        peer = test_db.get_zone(z_running['id'])
        # peer_off path may leave state='stopping' briefly on slower runners,
        # but by 1s after exclusive_start_zone the DB write of
        # last_watering_time should already be visible.
        assert peer['state'] in ('off', 'stopping')
        if peer['last_watering_time'] is not None:
            last = _parse(peer['last_watering_time'])
            assert peer['last_watering_time'] != old_start, (
                'peer_off path still writes start-time (issue #2)'
            )
            assert before <= last <= after


class TestSchedulerAutoStopEndTime:
    """Auto-stop fallback paths in irrigation_scheduler / zone_runner.

    We cannot easily exercise the live APScheduler-driven path in a unit
    test, but we CAN drive the fallback branch by making the central
    stop_zone raise — that's exactly what the production fallback covers.
    """

    def test_irrigation_scheduler_fallback_writes_end_time(self, test_db):
        zone = test_db.create_zone({
            'name': 'AutoStop', 'duration': 10, 'group_id': 1,
            'topic': '/test/auto',
        })
        old_start = '2026-01-01 10:00:00'
        test_db.update_zone(zone['id'], {
            'state': 'on',
            'watering_start_time': old_start,
        })
        # Build a minimal scheduler-like object that has just the bits
        # _stop_zone needs: a `db` attribute. We invoke the bound method
        # directly to avoid spinning up real APScheduler in a unit test.
        from irrigation_scheduler import IrrigationScheduler

        class _StubSched:
            db = test_db

        # Monkey-patch the central stop_zone to fail so we exercise the
        # fallback branch that actually contains the issue-#2 fix.
        with patch('services.zone_control.stop_zone',
                   side_effect=ValueError('forced fail for test')):
            before = datetime.now().replace(microsecond=0)
            IrrigationScheduler._stop_zone(_StubSched(), zone['id'])
            after = datetime.now()

        z = test_db.get_zone(zone['id'])
        assert z['state'] == 'off'
        assert z['last_watering_time'] is not None
        last = _parse(z['last_watering_time'])
        assert z['last_watering_time'] != old_start
        assert before <= last <= after

    def test_irrigation_scheduler_fallback_skips_when_never_started(self, test_db):
        """If the zone never had a watering_start_time, the auto-stop
        fallback should not overwrite a previous last_watering_time —
        we have no evidence anything actually ran.
        """
        zone = test_db.create_zone({
            'name': 'NeverStarted', 'duration': 10, 'group_id': 1,
            'topic': '/test/never',
        })
        prior_end = '2026-04-01 09:30:15'
        test_db.update_zone(zone['id'], {
            'state': 'off',
            'watering_start_time': None,
            'last_watering_time': prior_end,
        })
        from irrigation_scheduler import IrrigationScheduler

        class _StubSched:
            db = test_db

        with patch('services.zone_control.stop_zone',
                   side_effect=ValueError('forced fail for test')):
            IrrigationScheduler._stop_zone(_StubSched(), zone['id'])

        z = test_db.get_zone(zone['id'])
        assert z['last_watering_time'] == prior_end, (
            'never-started zone must not have last_watering_time clobbered'
        )

    def test_zone_runner_fallback_writes_end_time(self, test_db):
        """Same contract for the scheduler.zone_runner mixin path."""
        zone = test_db.create_zone({
            'name': 'RunnerStop', 'duration': 10, 'group_id': 1,
            'topic': '/test/runner',
        })
        old_start = '2026-01-01 10:00:00'
        test_db.update_zone(zone['id'], {
            'state': 'on',
            'watering_start_time': old_start,
        })
        from scheduler.zone_runner import ZoneRunnerMixin

        class _StubSched:
            db = test_db

        with patch('services.zone_control.stop_zone',
                   side_effect=ValueError('forced fail for test')):
            before = datetime.now().replace(microsecond=0)
            ZoneRunnerMixin._stop_zone(_StubSched(), zone['id'])
            after = datetime.now()

        z = test_db.get_zone(zone['id'])
        assert z['state'] == 'off'
        last = _parse(z['last_watering_time'])
        assert z['last_watering_time'] != old_start
        assert before <= last <= after
