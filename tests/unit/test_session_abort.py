"""Unit tests for issue #16: IrrigationScheduler.is_group_session_active.

Per spec §4.1 (#1-3): the helper is the single discriminator for "does
this group currently have an in-flight sequence/program run?"  These
tests exercise it directly without going through the API layer.
"""
import os
import threading

os.environ['TESTING'] = '1'


class TestIsGroupSessionActive:
    def test_is_group_session_active_returns_false_when_no_event(self, test_db):
        """Fresh scheduler, no manipulation -> False."""
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        assert sched.is_group_session_active(5) is False

    def test_is_group_session_active_returns_true_when_event_present(self, test_db):
        """Event present (NOT set) -> True. Existence is what matters."""
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        sched.group_cancel_events[5] = threading.Event()
        assert sched.is_group_session_active(5) is True

    def test_is_group_session_active_returns_true_when_event_set(self, test_db):
        """Event present AND .set() -> still True (cancel-in-progress is
        still an active session until the runner thread finishes its
        cleanup)."""
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        ev = threading.Event()
        ev.set()
        sched.group_cancel_events[5] = ev
        assert sched.is_group_session_active(5) is True

    def test_is_group_session_active_handles_string_input(self, test_db):
        """The helper coerces via int(); strings like '5' work."""
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        sched.group_cancel_events[5] = threading.Event()
        assert sched.is_group_session_active('5') is True

    def test_is_group_session_active_returns_false_for_invalid_input(self, test_db):
        """Non-coercible input -> False (never raises)."""
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        assert sched.is_group_session_active('not-a-number') is False
        assert sched.is_group_session_active(None) is False


class TestRunProgramThreadedPreRegistersCancelEvents:
    """§6.4 fix: _run_program_threaded plants a cancel-event for every
    distinct gid the program touches, then cleans up only those it
    registered itself."""

    def test_pre_register_cleans_up_after_run(self, test_db):
        """Happy path: registered gids get popped on exit."""
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        # Build two zones in distinct groups.
        g1 = test_db.create_group('PreReg G1')
        g2 = test_db.create_group('PreReg G2')
        z1 = test_db.create_zone({'name': 'P1', 'duration': 1, 'group_id': g1['id']})
        z2 = test_db.create_zone({'name': 'P2', 'duration': 1, 'group_id': g2['id']})

        # Pre-condition.
        assert g1['id'] not in sched.group_cancel_events
        assert g2['id'] not in sched.group_cancel_events

        # Run synchronously.  In TESTING mode the program weather-skips
        # quickly OR runs the truncated 1..6 sec loop; either way the
        # function returns and the finally runs.
        sched._run_program_threaded(99001, [z1['id'], z2['id']], 'PreRegProg')

        # Cleanup must have popped both gids (we owned them).
        assert g1['id'] not in sched.group_cancel_events
        assert g2['id'] not in sched.group_cancel_events

    def test_pre_register_does_not_replace_existing_event(self, test_db):
        """If a concurrent start_group_sequence already planted an Event
        for one of the program's gids, _run_program_threaded must NOT
        overwrite it AND must NOT pop it on exit."""
        from irrigation_scheduler import IrrigationScheduler
        sched = IrrigationScheduler(test_db)
        g1 = test_db.create_group('PreReg G1')
        z1 = test_db.create_zone({'name': 'P1', 'duration': 1, 'group_id': g1['id']})

        # Concurrent caller's Event — _run_group_sequence owns this.
        existing_event = threading.Event()
        sched.group_cancel_events[g1['id']] = existing_event

        sched._run_program_threaded(99002, [z1['id']], 'PreRegProgB')

        # Must still be present, must be the SAME object.
        assert sched.group_cancel_events.get(g1['id']) is existing_event
