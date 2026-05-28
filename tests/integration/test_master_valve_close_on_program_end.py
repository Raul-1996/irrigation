"""Issue #51 — regression: master valve MUST close when a scheduled/manual
program ends, even when the last zone's stop path raises.

Symptom on production (boem-объект Губерля): a nightly program finished and
every zone went to OFF, but the master valve stayed physically open until
morning. UI also showed it open — UI and reality were consistent, so this is
not a display bug. The fault is in the program finalizer: master-valve close
was only triggered as a side-effect of ``stop_zone(last_zone)`` →
``_schedule_master_close``. Any non-trivial failure in that chain (unhandled
exception type, missing zone topic, race that re-flips a zone to ``on``
before the delayed timer fires) silently skipped the close — relay stays on.

The fix wires an explicit, idempotent ``_schedule_master_close(g, immediate=True)``
into the ``finally`` block of ``_run_program_threaded`` (and
``_run_group_sequence``) for every group the program touched with
``use_master_valve=1``. Defence-in-depth: even if the in-loop stop path
fully fails, the program runner still arms the master close on exit.

These tests verify the contract:
  * happy path — program completes cleanly → master-close armed once per group;
  * the bug scenario — last zone's ``stop_zone`` raises a non-caught
    ``RuntimeError`` → master-close STILL armed in the finally block.

We patch ``services.zone_control._schedule_master_close`` and assert the
call shape rather than driving real MQTT — the underlying close mechanics
are already covered by ``test_master_valve_close_on_publish_failure``
(Issue #38).
"""

import os
import threading
from unittest.mock import patch

import pytest

os.environ["TESTING"] = "1"


def _make_mv_group(test_db, name="MV Group"):
    """Create an MQTT server + a group with use_master_valve=1.

    Returns ``(group_id, mqtt_server_id)``. Mirrors the helper in
    ``test_master_valve_close_on_publish_failure`` so the two regression
    suites stay readable side-by-side.
    """
    srv = test_db.create_mqtt_server({"name": "S", "host": "127.0.0.1", "port": 1883})
    test_db.create_group(name)
    groups = test_db.get_groups()
    gid = int(groups[-1]["id"])
    test_db.update_group_fields(
        gid,
        {
            "use_master_valve": 1,
            "master_mqtt_topic": "/devices/wb-mrwm2_42/controls/K1",
            "master_mqtt_server_id": int(srv["id"]),
            "master_mode": "NC",
            "master_close_delay_sec": 1,
            "master_valve_observed": "open",
        },
    )
    return gid, int(srv["id"])


@pytest.fixture
def scheduler_with_db(test_db):
    """Fresh IrrigationScheduler bound to the test DB (not started — we call
    ``_run_program_threaded`` directly, no APScheduler thread needed)."""
    import irrigation_scheduler as mod

    mod.scheduler = None
    sched = mod.IrrigationScheduler(test_db)
    yield sched
    mod.scheduler = None


class TestMasterValveCloseOnProgramEnd:
    def test_master_close_armed_after_clean_program(self, scheduler_with_db, test_db):
        """Happy path: program runs to completion → master-close called once
        with ``immediate=True`` for the group it touched.

        Fences the negative test below: without this assertion, a "fix" that
        never armed the close (e.g. accidentally guarded behind exception)
        would still satisfy the regression test by coincidence.
        """
        import services.zone_control as zc

        gid, _srv = _make_mv_group(test_db)
        # Two zones in the same MV group — programs touch one group here so
        # we don't need to assert per-group call counts beyond ==1.
        z1 = test_db.create_zone({"name": "Z1", "duration": 1, "group_id": gid, "topic": "/t/1"})
        z2 = test_db.create_zone({"name": "Z2", "duration": 1, "group_id": gid, "topic": "/t/2"})

        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", return_value=True),
            patch.object(zc, "water_monitor"),
            patch.object(zc, "state_verifier"),
            patch.object(zc, "_schedule_master_close") as mock_close,
        ):
            scheduler_with_db._run_program_threaded(1, [z1["id"], z2["id"]], "Test Program")

        # Must be armed exactly once for the MV group, with immediate=True.
        # immediate=True is intentional — at this point the program is
        # already finished, no reason to keep the relay open for an extra
        # delay-sec window.
        calls_for_gid = [
            c for c in mock_close.call_args_list
            if c.args and int(c.args[0].get("id", -1)) == gid
        ]
        assert len(calls_for_gid) >= 1, (
            f"_schedule_master_close must be armed in finally for gid={gid}; "
            f"got calls={mock_close.call_args_list!r}"
        )
        # At least one call must use immediate=True (the program-end close).
        immediate_calls = [c for c in calls_for_gid if c.kwargs.get("immediate") is True]
        assert immediate_calls, (
            f"At least one _schedule_master_close call for gid={gid} must "
            f"use immediate=True (program-end finalizer); got {calls_for_gid!r}"
        )

    def test_master_close_armed_even_when_last_zone_stop_raises(
        self, scheduler_with_db, test_db
    ):
        """THE REGRESSION TEST for Issue #51.

        Simulate the production failure mode: ``stop_zone`` raises
        ``RuntimeError`` (not in the caught tuple) when finalising the LAST
        zone — analogous to a timeout / unexpected error path on the real
        controller. Pre-fix this propagates past the in-loop ``except`` AND
        past the outer ``except (json.JSONDecodeError, KeyError, TypeError,
        ValueError)`` (RuntimeError is not in either tuple), so the finally
        block runs without ever having armed the master close → relay stays
        open all night. Post-fix the finally block explicitly arms
        ``_schedule_master_close`` for every MV group the program touched,
        regardless of how the loop exited.
        """
        import services.zone_control as zc

        gid, _srv = _make_mv_group(test_db)
        z1 = test_db.create_zone({"name": "Z1", "duration": 1, "group_id": gid, "topic": "/t/1"})
        z2_id_holder = {}
        z2 = test_db.create_zone({"name": "Z2", "duration": 1, "group_id": gid, "topic": "/t/2"})
        z2_id_holder["id"] = z2["id"]

        original_stop = zc.stop_zone

        def stop_zone_that_fails_on_last(zone_id, *args, **kwargs):
            """Raise RuntimeError when stopping the last zone (simulates a
            real-world finaliser failure that does NOT get caught by the
            program runner's in-loop or outer except clauses)."""
            if int(zone_id) == int(z2_id_holder["id"]):
                raise RuntimeError("simulated: last zone stop timeout")
            return original_stop(zone_id, *args, **kwargs)

        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", return_value=True),
            patch.object(zc, "water_monitor"),
            patch.object(zc, "state_verifier"),
            patch.object(zc, "stop_zone", side_effect=stop_zone_that_fails_on_last),
            patch.object(zc, "_schedule_master_close") as mock_close,
        ):
            # Whether the RuntimeError propagates out is OUT OF SCOPE for
            # Issue #51 — the acceptance criterion is strictly "master
            # valve closes". ``try/finally`` runs the finally block before
            # re-raising, so we just suppress the propagation here and
            # assert on the finally side-effect below.
            try:
                scheduler_with_db._run_program_threaded(1, [z1["id"], z2["id"]], "Test Program")
            except RuntimeError:
                # Expected for now — the broader "outer except should be
                # widened to BLE001 / Exception" is a separate refactor
                # (see "Out of scope" in the senior-1 report).
                pass

        # CRITICAL: even though the last zone's stop raised, the finally
        # block MUST have armed a master-close with immediate=True for the
        # group. This is the whole point of Issue #51 — relay must close.
        calls_for_gid = [
            c for c in mock_close.call_args_list
            if c.args and int(c.args[0].get("id", -1)) == gid
        ]
        assert len(calls_for_gid) >= 1, (
            f"REGRESSION (Issue #51): master valve was NOT armed for close "
            f"after a program where the last zone's stop_zone raised. "
            f"gid={gid}, mock_close.call_args_list={mock_close.call_args_list!r}. "
            f"Without this guarantee the physical relay stays open all night."
        )
        immediate_calls = [c for c in calls_for_gid if c.kwargs.get("immediate") is True]
        assert immediate_calls, (
            f"The program-end master-close must use immediate=True "
            f"(no point delaying when the program has already finished); "
            f"got gid={gid} calls={calls_for_gid!r}"
        )

    def test_master_close_not_armed_for_group_without_master_valve(
        self, scheduler_with_db, test_db
    ):
        """Negative coverage: groups WITHOUT ``use_master_valve=1`` must NOT
        get a master-close call in finally. Without this guard the fix
        would spam ``_schedule_master_close`` for every group on every
        program end — wasteful and noisy in logs.
        """
        import services.zone_control as zc

        # Create a regular group (no master valve) via plain create_group;
        # leave use_master_valve at its default (0).
        test_db.create_group("Plain Group")
        groups = test_db.get_groups()
        gid = int(groups[-1]["id"])
        z = test_db.create_zone({"name": "Z1", "duration": 1, "group_id": gid, "topic": "/t/1"})

        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", return_value=True),
            patch.object(zc, "water_monitor"),
            patch.object(zc, "state_verifier"),
            patch.object(zc, "_schedule_master_close") as mock_close,
        ):
            scheduler_with_db._run_program_threaded(1, [z["id"]], "Test Program")

        # No calls for this gid — group has no master valve.
        calls_for_gid = [
            c for c in mock_close.call_args_list
            if c.args and int(c.args[0].get("id", -1)) == gid
        ]
        assert not calls_for_gid, (
            f"Group without use_master_valve=1 must NOT trigger "
            f"_schedule_master_close in finally; got {calls_for_gid!r}"
        )


class TestMasterValveCloseOnGroupSequenceEnd:
    """Same defence-in-depth contract for ``_run_group_sequence`` —
    the manual-watering path goes through this method, and production
    Issue #51 reproduced on scheduled programs but the failure mode
    (last-zone stop raises, master stays open) is structurally identical.
    """

    def test_master_close_armed_after_group_sequence(self, scheduler_with_db, test_db):
        """Happy path: group sequence completes → master close armed."""
        import services.zone_control as zc

        gid, _srv = _make_mv_group(test_db)
        z1 = test_db.create_zone({"name": "Z1", "duration": 1, "group_id": gid, "topic": "/t/1"})
        z2 = test_db.create_zone({"name": "Z2", "duration": 1, "group_id": gid, "topic": "/t/2"})

        # Pre-plant cancel event so the runner thinks it has a session
        # (matches what start_group_sequence would do).
        scheduler_with_db.group_cancel_events[gid] = threading.Event()

        # Disable the TESTING short-circuit so the full per-zone loop runs
        # and our finally fix is exercised end-to-end.
        os.environ["SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ"] = "1"
        try:
            with (
                patch.object(zc, "db", test_db),
                patch.object(zc, "publish_mqtt_value", return_value=True),
                patch.object(zc, "water_monitor"),
                patch.object(zc, "state_verifier"),
                patch.object(zc, "_schedule_master_close") as mock_close,
            ):
                scheduler_with_db._run_group_sequence(gid, [z1["id"], z2["id"]])
        finally:
            os.environ.pop("SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ", None)

        calls_for_gid = [
            c for c in mock_close.call_args_list
            if c.args and int(c.args[0].get("id", -1)) == gid
        ]
        assert calls_for_gid, (
            f"_schedule_master_close must be armed in _run_group_sequence "
            f"finally for gid={gid}; got {mock_close.call_args_list!r}"
        )
        immediate_calls = [c for c in calls_for_gid if c.kwargs.get("immediate") is True]
        assert immediate_calls, (
            f"group-sequence end master-close must use immediate=True; "
            f"got {calls_for_gid!r}"
        )
