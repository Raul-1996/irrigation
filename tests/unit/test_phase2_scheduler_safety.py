"""Phase 2 scheduler safety regressions.

The tests in this module intentionally stay at the scheduler boundary.  Route
and application-lifecycle call sites are owned by separate Phase 2 packages.
"""

from __future__ import annotations

import contextlib
import threading
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import irrigation_scheduler as scheduler_module
from irrigation_scheduler import IrrigationScheduler


class _FixedDateTime(datetime):
    value = datetime(2026, 7, 21, 0, 10)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.value
        return cls.value.replace(tzinfo=tz)


@pytest.fixture
def safety_scheduler(test_db):
    sched = IrrigationScheduler(test_db)
    yield sched
    if sched.is_running:
        with contextlib.suppress(RuntimeError, ValueError):
            sched.stop()


@pytest.fixture
def started_safety_scheduler(safety_scheduler):
    safety_scheduler.start()
    return safety_scheduler


def _program(test_db, zone_ids, **overrides):
    data = {
        "name": "Safety program",
        "type": "time-based",
        "time": "06:00",
        "days": [0, 1, 2, 3, 4, 5, 6],
        "zones": list(zone_ids),
        "enabled": True,
        "schedule_type": "weekdays",
    }
    data.update(overrides)
    return test_db.create_program(data)


def _program_jobs(sched, program_id):
    prefix = f"program:{int(program_id)}:"
    return [job for job in sched.scheduler.get_jobs() if str(job.id).startswith(prefix)]


def test_133_emergency_flag_blocks_cron_runner_before_db_or_hardware(safety_scheduler):
    with (
        patch.object(scheduler_module, "TESTING", False),
        patch("services.sse_hub._app_config", {"EMERGENCY_STOP": True}),
        patch.object(safety_scheduler.db, "get_zone", wraps=safety_scheduler.db.get_zone) as get_zone,
    ):
        safety_scheduler._run_program_threaded(133, [999_133], "must-not-run")

    get_zone.assert_not_called()
    assert safety_scheduler.group_cancel_events == {}


def test_63_group_start_physically_stops_every_peer_before_planting_sequence(safety_scheduler, test_db):
    group = test_db.create_group("physical-off")
    selected = test_db.create_zone({"name": "selected", "duration": 3, "group_id": group["id"]})
    peer = test_db.create_zone({"name": "already-running", "duration": 3, "group_id": group["id"]})
    test_db.update_zone(peer["id"], {"state": "on", "commanded_state": "on"})
    order = []

    safety_scheduler.scheduler = MagicMock()
    safety_scheduler.scheduler.get_jobs.return_value = []
    safety_scheduler.scheduler.add_job.side_effect = lambda *args, **kwargs: order.append("sequence") or MagicMock()

    def physical_off(*_args, **_kwargs):
        order.append("physical-off")
        test_db.update_zone(selected["id"], {"state": "off"})
        test_db.update_zone(peer["id"], {"state": "off"})
        return {
            "success": True,
            "group_id": group["id"],
            "stopped": [selected["id"], peer["id"]],
            "unresolved": [],
            "retry_scheduled": False,
        }

    with (
        patch.object(scheduler_module, "TESTING", False),
        patch("services.zone_control.db", test_db),
        patch("services.zone_control.stop_all_in_group", side_effect=physical_off) as stop_all,
    ):
        assert safety_scheduler.start_group_sequence(group["id"], zone_ids=[selected["id"]]) is True

    stop_all.assert_called_once_with(
        group["id"],
        reason="group_sequence_restart",
        force=True,
        skip_master_close=True,
        require_observed_confirmation=True,
    )
    assert order == ["physical-off", "sequence"]


def test_63_group_start_aborts_when_peer_remains_fault_after_physical_stop(safety_scheduler, test_db):
    group = test_db.create_group("failed-physical-off")
    selected = test_db.create_zone({"name": "selected", "duration": 3, "group_id": group["id"]})
    peer = test_db.create_zone({"name": "fault-peer", "duration": 3, "group_id": group["id"]})
    test_db.update_zone(peer["id"], {"state": "fault", "commanded_state": "off"})
    safety_scheduler.scheduler = MagicMock()
    safety_scheduler.scheduler.get_jobs.return_value = []

    with (
        patch.object(scheduler_module, "TESTING", False),
        patch("services.zone_control.stop_all_in_group"),
    ):
        assert safety_scheduler.start_group_sequence(group["id"], zone_ids=[selected["id"]]) is False

    safety_scheduler.scheduler.add_job.assert_not_called()
    assert test_db.get_zone(peer["id"])["state"] == "fault"
    assert group["id"] not in safety_scheduler.group_cancel_events


def test_65_second_group_session_is_rejected_instead_of_reusing_event(safety_scheduler, test_db):
    group = test_db.create_group("single-session")
    test_db.create_zone({"name": "z", "duration": 3, "group_id": group["id"]})
    existing = threading.Event()
    safety_scheduler.group_cancel_events[group["id"]] = existing
    safety_scheduler.scheduler = MagicMock()
    safety_scheduler.scheduler.get_jobs.return_value = []

    with patch.object(scheduler_module, "TESTING", False):
        assert safety_scheduler.start_group_sequence(group["id"]) is False

    assert safety_scheduler.group_cancel_events[group["id"]] is existing
    safety_scheduler.scheduler.add_job.assert_not_called()


def test_146_stop_during_group_initialization_prevents_late_sequence_job(safety_scheduler, test_db):
    group = test_db.create_group("stop-during-init")
    zone = test_db.create_zone({"name": "z", "duration": 3, "group_id": group["id"]})
    entered_lookup = threading.Event()
    release_lookup = threading.Event()
    real_get_zones = test_db.get_zones
    calls = 0

    def blocking_get_zones():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered_lookup.set()
            release_lookup.wait(timeout=2)
        return real_get_zones()

    safety_scheduler.scheduler = MagicMock()
    safety_scheduler.scheduler.get_jobs.return_value = []
    result = []
    with (
        patch.object(scheduler_module, "TESTING", False),
        patch.object(test_db, "get_zones", side_effect=blocking_get_zones),
        patch("services.zone_control.db", test_db),
        patch(
            "services.zone_control.stop_all_in_group",
            return_value={
                "success": True,
                "group_id": group["id"],
                "stopped": [zone["id"]],
                "unresolved": [],
                "retry_scheduled": False,
            },
        ),
    ):
        starter = threading.Thread(
            target=lambda: result.append(safety_scheduler.start_group_sequence(group["id"])), daemon=True
        )
        starter.start()
        assert entered_lookup.wait(timeout=1)
        try:
            assert group["id"] in safety_scheduler.group_cancel_events
            safety_scheduler.cancel_group_jobs(group["id"])
        finally:
            release_lookup.set()
            starter.join(timeout=2)

    assert result == [False]
    assert all(
        not str(call.kwargs.get("id") or "").startswith(f"group_seq:{group['id']}:")
        for call in safety_scheduler.scheduler.add_job.call_args_list
    )
    assert group["id"] not in safety_scheduler.group_cancel_events
    assert test_db.get_zone(zone["id"])["state"] == "off"


def test_127_invalid_update_removes_existing_persistent_jobs(started_safety_scheduler, test_db):
    zone = test_db.create_zone({"name": "z", "duration": 5, "group_id": 1})
    program = _program(test_db, [zone["id"]])
    started_safety_scheduler.schedule_program(program["id"], program)
    assert _program_jobs(started_safety_scheduler, program["id"])

    invalid = test_db.update_program(program["id"], {"zones": []})
    started_safety_scheduler.schedule_program(program["id"], invalid)

    assert _program_jobs(started_safety_scheduler, program["id"]) == []
    assert started_safety_scheduler.program_jobs[program["id"]] == []


def test_127_cancel_scans_jobstore_when_in_memory_registry_was_lost(started_safety_scheduler, test_db):
    zone = test_db.create_zone({"name": "z", "duration": 5, "group_id": 1})
    program = _program(test_db, [zone["id"]])
    started_safety_scheduler.schedule_program(program["id"], program)
    assert _program_jobs(started_safety_scheduler, program["id"])
    started_safety_scheduler.program_jobs.clear()

    started_safety_scheduler.cancel_program(program["id"])

    assert _program_jobs(started_safety_scheduler, program["id"]) == []


def test_145_delete_wins_over_stale_put_scheduler_continuation(started_safety_scheduler, test_db):
    zone = test_db.create_zone({"name": "z", "duration": 5, "group_id": 1})
    program = _program(test_db, [zone["id"]])
    started_safety_scheduler.schedule_program(program["id"], program)
    stale_put_result = dict(program)

    assert test_db.delete_program(program["id"]) is True
    started_safety_scheduler.cancel_program(program["id"])
    started_safety_scheduler.schedule_program(program["id"], stale_put_result)

    assert _program_jobs(started_safety_scheduler, program["id"]) == []


def test_66_87_init_stays_paused_until_explicit_boot_completion(test_db):
    old = scheduler_module.scheduler
    scheduler_module.scheduler = None
    try:
        with (
            patch.object(IrrigationScheduler, "start") as start,
            patch.object(IrrigationScheduler, "cleanup_jobs_on_boot", return_value=True) as cleanup,
            patch.object(
                IrrigationScheduler,
                "stop_on_boot_active_zones",
                return_value=True,
            ) as stop_active,
            patch.object(IrrigationScheduler, "load_programs", return_value=True) as load,
            patch.object(IrrigationScheduler, "recover_missed_runs") as recover,
        ):
            sched = scheduler_module.init_scheduler(test_db)
        start.assert_called_once_with(paused=True)
        cleanup.assert_called_once_with()
        stop_active.assert_called_once_with()
        load.assert_called_once_with()
        recover.assert_not_called()

        with (
            patch.object(sched, "recover_missed_runs", return_value=True) as recover_after_sync,
            patch.object(sched.scheduler, "resume") as resume,
        ):
            assert sched.complete_boot_recovery() is True
        recover_after_sync.assert_called_once_with(require_interrupted_evidence=True)
        resume.assert_called_once_with()
    finally:
        scheduler_module.scheduler = old


def test_30_recovery_considers_previous_local_date_when_window_crosses_midnight(safety_scheduler, test_db):
    group = test_db.create_group("midnight")
    zones = [test_db.create_zone({"name": f"z{i}", "duration": 20, "group_id": group["id"]}) for i in range(3)]
    program = _program(test_db, [z["id"] for z in zones], time="23:30", days=[0])
    safety_scheduler._boot_interrupted_zone_ids = {zones[1]["id"]}
    safety_scheduler._boot_interrupted_program_zones = {program["id"]: {zones[1]["id"]}}
    safety_scheduler.start(paused=True)

    with patch.object(scheduler_module, "datetime", _FixedDateTime):
        assert safety_scheduler.recover_missed_runs(require_interrupted_evidence=True) is True

    intents = safety_scheduler._read_boot_recovery_intents_strict()
    intent = next(iter(intents.values()))
    assert intent["program_id"] == program["id"]
    assert intent["zones"] == [zones[1]["id"], zones[2]["id"]]


def test_88_boot_recovery_requires_evidence_of_an_interrupted_active_zone(safety_scheduler, test_db):
    _FixedDateTime.value = datetime(2026, 7, 21, 6, 10)
    group = test_db.create_group("no-phantom-recovery")
    zone = test_db.create_zone({"name": "z", "duration": 30, "group_id": group["id"]})
    _program(test_db, [zone["id"]], time="06:00", days=[1])
    safety_scheduler._boot_interrupted_zone_ids = set()

    with (
        patch.object(scheduler_module, "datetime", _FixedDateTime),
        patch.object(safety_scheduler.scheduler, "add_job") as add_job,
    ):
        safety_scheduler.recover_missed_runs(require_interrupted_evidence=True)

    add_job.assert_not_called()


def test_128_overlapping_extra_window_does_not_recover_from_later_skipped_fire(safety_scheduler, test_db):
    _FixedDateTime.value = datetime(2026, 7, 21, 6, 40)
    group = test_db.create_group("overlap")
    zones = [test_db.create_zone({"name": f"z{i}", "duration": 20, "group_id": group["id"]}) for i in range(3)]
    program = _program(
        test_db,
        [z["id"] for z in zones],
        time="06:00",
        extra_times=["06:30"],
        # Include 0 so the legacy weekday normalizer does not reinterpret the
        # list as one-based while the separate db/programs owner fixes it.
        days=[0, 1],
    )
    safety_scheduler._boot_interrupted_zone_ids = {zones[2]["id"]}
    safety_scheduler._boot_interrupted_program_zones = {program["id"]: {zones[2]["id"]}}
    safety_scheduler.start(paused=True)

    with patch.object(scheduler_module, "datetime", _FixedDateTime):
        assert safety_scheduler.recover_missed_runs(require_interrupted_evidence=True) is True

    intents = safety_scheduler._read_boot_recovery_intents_strict()
    assert next(iter(intents.values()))["zones"] == [zones[2]["id"]]


def test_90_scheduled_start_uses_next_real_weekday_not_today(started_safety_scheduler, test_db):
    _FixedDateTime.value = datetime(2026, 7, 23, 10, 0)  # Thursday
    group = test_db.create_group("next-real-run")
    zone = test_db.create_zone({"name": "z", "duration": 15, "group_id": group["id"]})
    program = _program(test_db, [zone["id"]], time="06:00", days=[0])  # Monday only

    with patch.object(scheduler_module, "datetime", _FixedDateTime):
        started_safety_scheduler.schedule_program(program["id"], program)

    assert test_db.get_zone(zone["id"])["scheduled_start_time"] == "2026-07-27 06:00:00"


def test_149_quiesce_is_a_strict_fence_for_inflight_program_runner(safety_scheduler, test_db):
    group = test_db.create_group("shutdown-fence")
    zone = test_db.create_zone({"name": "z", "duration": 1, "group_id": group["id"]})
    entered_weather = threading.Event()
    release_weather = threading.Event()

    def blocked_weather(*args, **kwargs):
        entered_weather.set()
        release_weather.wait(timeout=2)
        return {"skip": False}

    with patch.object(safety_scheduler, "_check_weather_skip", side_effect=blocked_weather):
        runner = threading.Thread(
            target=safety_scheduler._run_program_threaded,
            args=(149, [zone["id"]], "shutdown-race"),
            daemon=True,
        )
        runner.start()
        assert entered_weather.wait(timeout=1)

        def release_after_fence():
            safety_scheduler._shutdown_event.wait(timeout=1)
            release_weather.set()

        releaser = threading.Thread(target=release_after_fence, daemon=True)
        releaser.start()
        try:
            assert safety_scheduler.quiesce(timeout_seconds=1.5) is True
        finally:
            release_weather.set()
            runner.join(timeout=2)
            releaser.join(timeout=2)

    assert test_db.get_zone(zone["id"])["state"] == "off"
    assert safety_scheduler.active_runner_count == 0


def test_110_scheduled_program_never_admits_fault_zone(safety_scheduler, test_db):
    group = test_db.create_group("program-fault")
    zone = test_db.create_zone({"name": "fault", "duration": 1, "group_id": group["id"]})
    test_db.update_zone(zone["id"], {"state": "fault", "commanded_state": "on"})

    with (
        patch.object(safety_scheduler, "_check_weather_skip", return_value={"skip": False}),
        patch.object(safety_scheduler, "_get_weather_adjusted_duration", return_value=1),
        patch.object(safety_scheduler._shutdown_event, "wait", return_value=True),
        patch.object(safety_scheduler, "schedule_zone_hard_stop") as hard_stop,
        patch("services.zone_control.exclusive_start_zone", return_value=True) as start_zone,
        patch("services.zone_control.stop_zone"),
    ):
        safety_scheduler._run_program_threaded(110, [zone["id"]], "fault-program")

    start_zone.assert_not_called()
    hard_stop.assert_not_called()
    assert test_db.get_zone(zone["id"])["state"] == "fault"
    assert zone["id"] not in safety_scheduler.active_zones


def test_110_program_honors_failed_central_start(safety_scheduler, test_db):
    group = test_db.create_group("program-rejected")
    zone = test_db.create_zone({"name": "off", "duration": 1, "group_id": group["id"]})

    with (
        patch.object(safety_scheduler, "_check_weather_skip", return_value={"skip": False}),
        patch.object(safety_scheduler, "_get_weather_adjusted_duration", return_value=1),
        patch.object(safety_scheduler._shutdown_event, "wait", return_value=True),
        patch.object(safety_scheduler, "schedule_zone_hard_stop") as hard_stop,
        patch("services.zone_control.exclusive_start_zone", return_value=False) as start_zone,
        patch("services.zone_control.stop_zone"),
    ):
        safety_scheduler._run_program_threaded(110, [zone["id"]], "rejected-program")

    assert start_zone.call_count == 1
    assert start_zone.call_args.args == (zone["id"],)
    assert start_zone.call_args.kwargs["source"] == "program"
    assert callable(start_zone.call_args.kwargs["cancel_guard"])
    hard_stop.assert_not_called()
    assert test_db.get_zone(zone["id"])["state"] == "off"
    assert zone["id"] not in safety_scheduler.active_zones


def test_110_program_does_not_overwrite_fault_reported_during_central_start(safety_scheduler, test_db):
    group = test_db.create_group("program-command-fault")
    zone = test_db.create_zone({"name": "off", "duration": 1, "group_id": group["id"]})

    def fault_during_start(zone_id, *, source, cancel_guard):
        assert source == "program"
        assert cancel_guard() is False
        test_db.update_zone(zone_id, {"state": "fault", "commanded_state": "on"})
        return True

    with (
        patch.object(safety_scheduler, "_check_weather_skip", return_value={"skip": False}),
        patch.object(safety_scheduler, "_get_weather_adjusted_duration", return_value=1),
        patch.object(safety_scheduler, "schedule_zone_hard_stop") as hard_stop,
        patch("services.zone_control.exclusive_start_zone", side_effect=fault_during_start),
    ):
        safety_scheduler._run_program_threaded(110, [zone["id"]], "fault-during-start")

    hard_stop.assert_not_called()
    assert test_db.get_zone(zone["id"])["state"] == "fault"
    assert zone["id"] not in safety_scheduler.active_zones


def test_110_group_sequence_never_admits_fault_zone(safety_scheduler, test_db, monkeypatch):
    group = test_db.create_group("group-fault")
    zone = test_db.create_zone({"name": "fault", "duration": 1, "group_id": group["id"]})
    test_db.update_zone(zone["id"], {"state": "fault", "commanded_state": "on"})
    safety_scheduler.group_cancel_events[group["id"]] = threading.Event()
    monkeypatch.setenv("SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ", "1")

    with (
        patch.object(safety_scheduler, "_check_weather_skip", return_value={"skip": False}),
        patch.object(safety_scheduler, "schedule_zone_hard_stop") as hard_stop,
        patch("services.zone_control.exclusive_start_zone", return_value=True) as start_zone,
        patch("services.zone_control.stop_zone"),
    ):
        safety_scheduler._run_group_sequence(group["id"], [zone["id"]])

    start_zone.assert_not_called()
    hard_stop.assert_not_called()
    assert test_db.get_zone(zone["id"])["state"] == "fault"
    assert group["id"] not in safety_scheduler.group_cancel_events


def test_110_group_sequence_honors_failed_central_start(safety_scheduler, test_db, monkeypatch):
    group = test_db.create_group("group-rejected")
    zone = test_db.create_zone({"name": "off", "duration": 1, "group_id": group["id"]})
    safety_scheduler.group_cancel_events[group["id"]] = threading.Event()
    monkeypatch.setenv("SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ", "1")

    with (
        patch.object(safety_scheduler, "_check_weather_skip", return_value={"skip": False}),
        patch.object(safety_scheduler, "schedule_zone_hard_stop") as hard_stop,
        patch("services.zone_control.exclusive_start_zone", return_value=False) as start_zone,
        patch("services.zone_control.stop_zone"),
    ):
        safety_scheduler._run_group_sequence(group["id"], [zone["id"]])

    assert start_zone.call_count == 1
    assert start_zone.call_args.args == (zone["id"],)
    assert start_zone.call_args.kwargs["source"] == "program"
    assert callable(start_zone.call_args.kwargs["cancel_guard"])
    hard_stop.assert_not_called()
    assert test_db.get_zone(zone["id"])["state"] == "off"
    assert group["id"] not in safety_scheduler.group_cancel_events


def test_110_group_sequence_does_not_overwrite_fault_reported_during_start(safety_scheduler, test_db, monkeypatch):
    group = test_db.create_group("group-command-fault")
    zone = test_db.create_zone({"name": "off", "duration": 1, "group_id": group["id"]})
    safety_scheduler.group_cancel_events[group["id"]] = threading.Event()
    monkeypatch.setenv("SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ", "1")

    def fault_during_start(zone_id, *, source, cancel_guard):
        assert source == "program"
        assert cancel_guard() is False
        test_db.update_zone(zone_id, {"state": "fault", "commanded_state": "on"})
        return True

    with (
        patch.object(safety_scheduler, "_check_weather_skip", return_value={"skip": False}),
        patch.object(safety_scheduler, "schedule_zone_hard_stop") as hard_stop,
        patch("services.zone_control.exclusive_start_zone", side_effect=fault_during_start),
    ):
        safety_scheduler._run_group_sequence(group["id"], [zone["id"]])

    hard_stop.assert_not_called()
    assert test_db.get_zone(zone["id"])["state"] == "fault"
    assert zone["id"] not in safety_scheduler.active_zones
    assert group["id"] not in safety_scheduler.group_cancel_events


@pytest.mark.parametrize("early_exit", ["emergency", "quiescing"])
def test_group_runner_early_exit_releases_owned_session_event(safety_scheduler, early_exit):
    group_id = 110_001
    owned = threading.Event()
    safety_scheduler.group_cancel_events[group_id] = owned
    if early_exit == "quiescing":
        safety_scheduler._accepting_runs = False

    with (
        patch.object(scheduler_module, "TESTING", False),
        patch.object(
            safety_scheduler,
            "_is_emergency_stop_active",
            return_value=early_exit == "emergency",
        ),
    ):
        safety_scheduler._run_group_sequence(group_id, [1])

    assert group_id not in safety_scheduler.group_cancel_events


def test_group_session_release_does_not_delete_replacement_event(safety_scheduler):
    group_id = 110_002
    owned = threading.Event()
    replacement = threading.Event()
    safety_scheduler.group_cancel_events[group_id] = replacement

    safety_scheduler._release_group_session(group_id, owned)

    assert safety_scheduler.group_cancel_events[group_id] is replacement
