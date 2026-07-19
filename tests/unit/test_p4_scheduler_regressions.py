"""Post-review regressions for scheduler persistence and physical-stop safety."""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import irrigation_scheduler as scheduler_module
from irrigation_scheduler import IrrigationScheduler


@pytest.fixture
def sched(test_db):
    value = IrrigationScheduler(test_db)
    yield value
    if value.is_running:
        with contextlib.suppress(RuntimeError, ValueError):
            value.stop()


def _create_interval_program(test_db, *, interval_days: int = 3):
    group = test_db.create_group("persistent-anchor")
    zone = test_db.create_zone({"name": "zone", "duration": 5, "group_id": group["id"]})
    program = test_db.create_program(
        {
            "name": "Every few days",
            "time": "06:45",
            "schedule_type": "interval",
            "interval_days": interval_days,
            "days": [],
            "zones": [zone["id"]],
            "enabled": True,
        }
    )
    return zone, program


def _program_jobs(value: IrrigationScheduler, program_id: int):
    prefix = f"program:{int(program_id)}:"
    return [job for job in value.scheduler.get_jobs() if str(job.id).startswith(prefix)]


def test_load_programs_preserves_persistent_misfire_and_interval_anchor(test_db):
    _, program = _create_interval_program(test_db)

    first = IrrigationScheduler(test_db)
    first.start(paused=True)
    try:
        assert first.schedule_program(program["id"], program) is True
        job = _program_jobs(first, program["id"])[0]
        anchor = job.trigger.start_date
    finally:
        first.stop()

    second = IrrigationScheduler(test_db)
    second.start(paused=True)
    try:
        restored = _program_jobs(second, program["id"])[0]
        assert restored.trigger.start_date == anchor
        pending_misfire = datetime.now(restored.trigger.timezone) - timedelta(minutes=5)
        second.scheduler.modify_job(restored.id, next_run_time=pending_misfire)

        assert second.load_programs() is True

        reconciled = _program_jobs(second, program["id"])[0]
        assert reconciled.trigger.start_date == anchor
        assert reconciled.next_run_time == pending_misfire
        assert second.get_program_interval_anchors(program["id"]) == {"main": anchor}
        occurrences = second.get_program_occurrences(
            program["id"],
            anchor,
            anchor + timedelta(days=7),
        )
        assert occurrences["main"] == [anchor, anchor + timedelta(days=3), anchor + timedelta(days=6)]
    finally:
        second.stop()


def test_load_programs_does_not_readd_an_unchanged_persistent_job(test_db):
    _, program = _create_interval_program(test_db)
    first = IrrigationScheduler(test_db)
    first.start(paused=True)
    try:
        assert first.schedule_program(program["id"], program) is True
    finally:
        first.stop()

    second = IrrigationScheduler(test_db)
    second.start(paused=True)
    try:
        real_add_job = second.scheduler.add_job

        def reject_program_readd(func, trigger=None, **kwargs):
            if str(kwargs.get("id") or "").startswith(f"program:{program['id']}:"):
                raise AssertionError("unchanged persistent program job was recreated")
            return real_add_job(func, trigger, **kwargs)

        with patch.object(second.scheduler, "add_job", side_effect=reject_program_readd):
            assert second.load_programs() is True
    finally:
        second.stop()


def test_load_programs_continues_after_malformed_persisted_payload(sched, test_db):
    group = test_db.create_group("malformed")
    zone = test_db.create_zone({"name": "zone", "duration": 5, "group_id": group["id"]})
    malformed = test_db.create_program(
        {
            "name": "broken legacy row",
            "time": "06:00",
            "schedule_type": "interval",
            "interval_days": 2,
            "days": [],
            "zones": [zone["id"]],
        }
    )
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("UPDATE programs SET zones = 'not-json' WHERE id = ?", (malformed["id"],))
        conn.commit()
    valid = test_db.create_program(
        {
            "name": "valid later row",
            "time": "07:00",
            "schedule_type": "interval",
            "interval_days": 3,
            "days": [],
            "zones": [zone["id"]],
        }
    )
    sched.start(paused=True)

    assert sched.load_programs() is False

    assert [job.id for job in _program_jobs(sched, valid["id"])] == [f"program:{valid['id']}:main"]
    assert _program_jobs(sched, malformed["id"]) == []
    assert test_db.get_zone(zone["id"]) is not None


def test_changed_program_reconcile_fails_closed_without_partial_jobs(sched, test_db):
    zone, program = _create_interval_program(test_db)
    sched.start(paused=True)
    assert sched.schedule_program(program["id"], program) is True

    changed = test_db.update_program(
        program["id"],
        {
            "time": "07:15",
            "extra_times": ["19:15"],
            "zones": [zone["id"]],
        },
    )
    real_add_job = sched.scheduler.add_job
    program_adds = 0

    def fail_second_program_add(func, trigger=None, **kwargs):
        nonlocal program_adds
        if str(kwargs.get("id") or "").startswith(f"program:{program['id']}:"):
            program_adds += 1
            if program_adds == 2:
                raise RuntimeError("simulated jobstore failure")
        return real_add_job(func, trigger, **kwargs)

    with patch.object(sched.scheduler, "add_job", side_effect=fail_second_program_add):
        assert sched.schedule_program(program["id"], changed) is False

    assert _program_jobs(sched, program["id"]) == []
    assert sched.program_jobs[program["id"]] == []


def test_program_triggers_use_wb_timezone_not_process_timezone(test_db, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("WB_TZ", "Asia/Yekaterinburg")
    zone, interval = _create_interval_program(test_db)
    weekday = test_db.create_program(
        {
            "name": "Local Monday",
            "time": "08:30",
            "schedule_type": "weekdays",
            "days": [0],
            "zones": [zone["id"]],
            "enabled": True,
        }
    )
    value = IrrigationScheduler(test_db)
    value.start(paused=True)
    try:
        assert value.schedule_program(interval["id"], interval) is True
        assert value.schedule_program(weekday["id"], weekday) is True
        assert getattr(value.scheduler.timezone, "key", None) == "Asia/Yekaterinburg"
        for job in _program_jobs(value, interval["id"]) + _program_jobs(value, weekday["id"]):
            assert getattr(job.trigger.timezone, "key", None) == "Asia/Yekaterinburg"
    finally:
        value.stop()


def test_cancel_group_preserves_safety_jobs_and_surfaces_failed_off(sched, test_db):
    group = test_db.create_group("failed-off")
    zone = test_db.create_zone({"name": "stuck", "duration": 5, "group_id": group["id"]})
    test_db.update_zone(zone["id"], {"state": "fault", "watering_start_time": "2026-07-19 10:00:00"})
    hard = MagicMock(id=f"zone_hard_stop:{zone['id']}")
    cap = MagicMock(id=f"zone_cap_stop:{zone['id']}")
    sched.scheduler = MagicMock()
    sched.scheduler.get_jobs.return_value = [hard, cap]

    with patch(
        "services.zone_control.stop_all_in_group",
        return_value={"success": False, "stopped": [], "unresolved": [zone["id"]]},
    ):
        result = sched.cancel_group_jobs(group["id"])

    assert result["success"] is False
    assert result["unresolved"] == [zone["id"]]
    removed = {call.args[0] for call in sched.scheduler.remove_job.call_args_list}
    assert hard.id not in removed
    assert cap.id not in removed


def test_cancel_group_keeps_cap_until_fresh_observed_off(sched, test_db):
    group = test_db.create_group("confirmed-off")
    zone = test_db.create_zone({"name": "stopped", "duration": 5, "group_id": group["id"]})
    hard = MagicMock(id=f"zone_hard_stop:{zone['id']}")
    cap = MagicMock(id=f"zone_cap_stop:{zone['id']}")
    sched.scheduler = MagicMock()
    sched.scheduler.get_jobs.return_value = [hard, cap]

    with patch(
        "services.zone_control.stop_all_in_group",
        return_value={"success": True, "stopped": [zone["id"]], "unresolved": []},
    ):
        result = sched.cancel_group_jobs(group["id"])

    assert result["success"] is True
    removed = {call.args[0] for call in sched.scheduler.remove_job.call_args_list}
    assert hard.id in removed
    assert cap.id not in removed

    sched.cancel_zone_cap(zone["id"])
    assert sched.scheduler.remove_job.call_args_list[-1].args[0] == cap.id


def test_cancelling_pending_group_sequence_releases_session_claim(sched, test_db):
    group = test_db.create_group("pending-sequence")
    test_db.create_zone({"name": "zone", "duration": 5, "group_id": group["id"]})
    owned = threading.Event()
    sched.group_cancel_events[group["id"]] = owned
    pending = MagicMock(id=f"group_seq:{group['id']}:123")
    sched.scheduler = MagicMock()
    sched.scheduler.get_jobs.return_value = [pending]

    with patch(
        "services.zone_control.stop_all_in_group",
        return_value={"success": True, "stopped": [], "unresolved": []},
    ):
        result = sched.cancel_group_jobs(group["id"])

    assert result["success"] is True
    assert owned.is_set()
    assert group["id"] not in sched.group_cancel_events


def test_cap_callback_is_bound_to_the_activation_that_planted_it(sched, test_db):
    group = test_db.create_group("activation")
    zone = test_db.create_zone({"name": "zone", "duration": 5, "group_id": group["id"]})
    test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": "2026-07-19 11:00:00"})

    with (
        patch.object(scheduler_module, "get_scheduler", return_value=sched),
        patch.object(sched, "_stop_zone") as stop_zone,
    ):
        scheduler_module.job_stop_zone_if_activation(zone["id"], "2026-07-19 10:00:00")

    stop_zone.assert_not_called()


def test_failed_auto_stop_rearms_activation_bound_hard_retry(sched, test_db):
    group = test_db.create_group("retry")
    zone = test_db.create_zone({"name": "zone", "duration": 5, "group_id": group["id"]})
    token = "2026-07-19 12:00:00"
    test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": token})

    with (
        patch("services.zone_control.stop_zone", return_value=False),
        patch.object(sched, "schedule_zone_hard_stop") as hard_stop,
    ):
        assert sched._stop_zone(zone["id"], reason="auto_stop") is False

    assert test_db.get_zone(zone["id"])["state"] == "on"
    hard_stop.assert_called_once()
    assert hard_stop.call_args.kwargs["activation_token"] == token


def test_scheduler_boot_aborts_crash_open_run_before_forced_stop(sched, test_db):
    group = test_db.create_group("boot")
    zone = test_db.create_zone({"name": "zone", "duration": 5, "group_id": group["id"]})
    test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": "2026-07-19 09:00:00"})
    run_id = test_db.create_zone_run(
        zone["id"],
        group["id"],
        "2026-07-19 06:00:00",
        1.0,
        None,
        0,
        source="program",
    )

    def forced_stop(zone_id, **kwargs):
        assert zone_id == zone["id"]
        assert test_db.get_open_zone_run(zone_id) is None
        return True

    with patch("services.zone_control.stop_zone", side_effect=forced_stop):
        assert sched.stop_on_boot_active_zones() is True

    with sqlite3.connect(test_db.db_path) as conn:
        status, end_utc = conn.execute("SELECT status, end_utc FROM zone_runs WHERE id = ?", (run_id,)).fetchone()
    assert status == "aborted"
    assert end_utc is not None
