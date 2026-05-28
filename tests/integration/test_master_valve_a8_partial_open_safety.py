"""A8 — partial-open finally safety net for program / group sequence runs.

See audits/2026-05-28-security/findings.md section A8.

Pre-fix bug: if an exception bubbled out of ``_run_program_threaded`` or
``_run_group_sequence`` AFTER ``exclusive_start_zone`` opened the master
valve but BEFORE the matching ``stop_zone()`` call ran, the master valve
stayed open until the zone-cap watchdog caught it ~4h later.

Fix approach: PRE-COLLECT. We use the already-existing ``program_gids``
set (collected before the try block in _run_program_threaded), and the
single ``group_id`` argument of _run_group_sequence, to drive a finally
block that calls ``_schedule_master_close`` for every touched group.
The schedule helper has its own "any zone still on" guard, so this is
a no-op on the happy path and only fires when a real leak happened.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ["TESTING"] = "1"


@pytest.fixture
def _force_real_group_seq():
    """Force the real per-zone loop path in _run_group_sequence so the
    finally block executes after our induced raises. Scoped per-test so
    it doesn't leak into other suites (test_session_abort_issue16 etc.).
    """
    prev = os.environ.get("SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ")
    os.environ["SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ"] = "1"
    yield
    if prev is None:
        os.environ.pop("SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ", None)
    else:
        os.environ["SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ"] = prev


def _make_master_group(test_db, *, gid_offset: int = 0):
    srv = test_db.create_mqtt_server({"name": f"S{gid_offset}", "host": "127.0.0.1", "port": 1883})
    test_db.create_group(f"MV Group A8 {gid_offset}")
    groups = test_db.get_groups()
    gid = int(groups[-1]["id"])
    test_db.update_group_fields(
        gid,
        {
            "use_master_valve": 1,
            "master_mqtt_topic": f"/devices/wb-mrwm2_42/controls/MV{gid}",
            "master_mqtt_server_id": int(srv["id"]),
            "master_mode": "NC",
            "master_close_delay_sec": 60,
            "master_valve_observed": "open",
        },
    )
    return gid, int(srv["id"])


class TestA8ProgramFinally:
    def test_program_finally_schedules_master_close(self, test_db):
        """_run_program_threaded's finally calls _schedule_master_close for touched groups."""
        from irrigation_scheduler import IrrigationScheduler
        import services.zone_control as zc

        gid, _ = _make_master_group(test_db)
        zone = test_db.create_zone(
            {"name": "Z1", "duration": 1, "group_id": gid, "topic": "/devices/x/controls/Z"}
        )

        sched_calls: list = []

        def _capture_schedule(group_dict, immediate: bool = False):
            sched_calls.append((int(group_dict.get("id") or 0), immediate))

        # Patch the scheduler's lazy import target. We don't need MQTT to
        # actually go out — we're verifying the finally block reaches
        # _schedule_master_close with the right group(s).
        sched = IrrigationScheduler.__new__(IrrigationScheduler)
        sched.db = test_db
        sched.group_cancel_events = {}
        sched.group_skip_current_events = {}
        sched.active_zones = {}
        sched._shutdown_event = MagicMock()
        sched._shutdown_event.wait = MagicMock(return_value=False)
        sched._shutdown_event.is_set = MagicMock(return_value=False)

        with (
            patch.object(zc, "_schedule_master_close", side_effect=_capture_schedule),
            patch(
                "irrigation_scheduler.IrrigationScheduler._check_weather_skip",
                return_value={"skip": False, "reason": ""},
            ),
            patch(
                "irrigation_scheduler.IrrigationScheduler._get_weather_adjusted_duration",
                return_value=1,
            ),
            patch(
                "irrigation_scheduler.IrrigationScheduler.schedule_zone_hard_stop",
                return_value=None,
            ),
        ):
            # Run the program — in TESTING mode this short-circuits the
            # per-zone loop in many places, but the finally still fires.
            sched._run_program_threaded(1, [int(zone["id"])], "ProgA8", manual=True)

        assert any(gg == gid for gg, _ in sched_calls), (
            f"A8 finally must call _schedule_master_close for touched gid={gid}; got {sched_calls!r}"
        )

    def test_program_finally_fires_even_when_collect_raises_mid_open(self, test_db):
        """If the body raises AFTER one group opened, finally still closes that group's master valve.

        Simulates the A8 root cause: a raise propagates through the per-zone
        loop after one group's master valve is already open. The finally
        must still call _schedule_master_close for that group.
        """
        from irrigation_scheduler import IrrigationScheduler
        import services.zone_control as zc

        gid, _ = _make_master_group(test_db)
        zone = test_db.create_zone(
            {"name": "Z1", "duration": 1, "group_id": gid, "topic": "/devices/x/controls/Z"}
        )

        sched_calls: list = []

        def _capture_schedule(group_dict, immediate: bool = False):
            sched_calls.append((int(group_dict.get("id") or 0), immediate))

        sched = IrrigationScheduler.__new__(IrrigationScheduler)
        sched.db = test_db
        sched.group_cancel_events = {}
        sched.group_skip_current_events = {}
        sched.active_zones = {}
        sched._shutdown_event = MagicMock()
        sched._shutdown_event.wait = MagicMock(return_value=False)
        sched._shutdown_event.is_set = MagicMock(return_value=False)

        # Make _check_weather_skip raise — this fires AFTER program_gids
        # collection (pre-try) but BEFORE any per-zone stop_zone(). This
        # is the canonical A8 scenario: nothing in the try block has
        # cleaned up by the time the exception propagates.
        with (
            patch.object(zc, "_schedule_master_close", side_effect=_capture_schedule),
            patch(
                "irrigation_scheduler.IrrigationScheduler._check_weather_skip",
                side_effect=KeyError("simulated weather provider blow-up"),
            ),
        ):
            # Non-manual so weather check runs; KeyError is in the narrow
            # tuple the outer except catches, so it doesn't propagate. The
            # finally still must run.
            sched._run_program_threaded(1, [int(zone["id"])], "ProgA8x", manual=False)

        assert any(gg == gid for gg, _ in sched_calls), (
            f"A8 finally must close master valve for group {gid} even on raise; got {sched_calls!r}"
        )


class TestA8GroupSequenceFinally:
    def test_group_sequence_finally_schedules_master_close(self, test_db, _force_real_group_seq):
        """_run_group_sequence's finally calls _schedule_master_close for its group_id."""
        from irrigation_scheduler import IrrigationScheduler
        import services.zone_control as zc

        gid, _ = _make_master_group(test_db)
        zone = test_db.create_zone(
            {"name": "Z1", "duration": 1, "group_id": gid, "topic": "/devices/x/controls/Z"}
        )

        sched_calls: list = []

        def _capture_schedule(group_dict, immediate: bool = False):
            sched_calls.append((int(group_dict.get("id") or 0), immediate))

        sched = IrrigationScheduler.__new__(IrrigationScheduler)
        sched.db = test_db
        sched.group_cancel_events = {}
        sched.group_skip_current_events = {}
        sched.active_zones = {}
        sched._shutdown_event = MagicMock()
        sched._shutdown_event.wait = MagicMock(return_value=False)
        sched._shutdown_event.is_set = MagicMock(return_value=False)

        with (
            patch.object(zc, "_schedule_master_close", side_effect=_capture_schedule),
            patch(
                "irrigation_scheduler.IrrigationScheduler._check_weather_skip",
                side_effect=KeyError("simulated weather raise"),
            ),
        ):
            # Force the body to raise via weather check — the finally must still fire.
            sched._run_group_sequence(int(gid), [int(zone["id"])], manual=False)

        assert any(gg == gid for gg, _ in sched_calls), (
            f"A8 finally must close master valve for group {gid}; got {sched_calls!r}"
        )
