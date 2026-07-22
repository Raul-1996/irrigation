"""Release-boundary scheduler reconciliation regressions."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import irrigation_scheduler as scheduler_module
from irrigation_scheduler import IrrigationScheduler


def _program_jobs(scheduler: IrrigationScheduler, program_id: int):
    prefix = f"program:{int(program_id)}:"
    return [job for job in scheduler.scheduler.get_jobs() if str(job.id).startswith(prefix)]


def _two_zone_interval_program(test_db):
    group = test_db.create_group("release-reconcile")
    first = test_db.create_zone({"name": "first", "duration": 5, "group_id": group["id"]})
    second = test_db.create_zone({"name": "second", "duration": 5, "group_id": group["id"]})
    program = test_db.create_program(
        {
            "name": "release interval",
            "time": "06:45",
            "schedule_type": "interval",
            "interval_days": 3,
            "days": [],
            "zones": [first["id"], second["id"]],
            "enabled": True,
        }
    )
    return first, second, program


def test_load_programs_disables_legacy_smart_row_without_blocking_valid_program(test_db):
    group = test_db.create_group("legacy-smart")
    zone = test_db.create_zone({"name": "zone", "duration": 5, "group_id": group["id"]})
    smart = test_db.create_program(
        {
            "name": "legacy smart",
            "time": "05:30",
            "type": "smart",
            "days": [0],
            "zones": [zone["id"]],
            "enabled": True,
        }
    )
    # Older/manual DBs may contain non-canonical spelling even though current
    # repository writes reject it. Boot quarantine must still recognize it.
    with sqlite3.connect(test_db.db_path) as connection:
        connection.execute("UPDATE programs SET type = ' SMART ' WHERE id = ?", (smart["id"],))
        connection.commit()
    valid = test_db.create_program(
        {
            "name": "valid time based",
            "time": "06:30",
            "days": [0],
            "zones": [zone["id"]],
            "enabled": True,
        }
    )
    scheduler = IrrigationScheduler(test_db)
    scheduler.start(paused=True)
    try:
        scheduler.scheduler.add_job(
            scheduler_module.job_run_program,
            scheduler_module.CronTrigger(day_of_week=0, hour=5, minute=30),
            args=[smart["id"], [zone["id"]], smart["name"], False],
            id=f"program:{smart['id']}:main:d0",
            replace_existing=True,
        )

        assert scheduler.load_programs() is True

        assert test_db.get_program(smart["id"])["enabled"] is False
        assert _program_jobs(scheduler, smart["id"]) == []
        assert [job.id for job in _program_jobs(scheduler, valid["id"])] == [f"program:{valid['id']}:main:d0"]
        audit = test_db.get_audit_logs(
            action_type="unsupported_program_disabled",
            target=f"program:{smart['id']}",
        )
        assert len(audit) == 1
        assert audit[0]["source"] == "scheduler"
    finally:
        with contextlib.suppress(RuntimeError, ValueError):
            scheduler.stop()


def test_init_scheduler_does_not_pause_every_program_for_legacy_smart_row(test_db):
    group = test_db.create_group("legacy-smart-boot")
    zone = test_db.create_zone({"name": "zone", "duration": 5, "group_id": group["id"]})
    smart = test_db.create_program(
        {
            "name": "legacy smart at boot",
            "time": "05:30",
            "type": "smart",
            "days": [0],
            "zones": [zone["id"]],
            "enabled": True,
        }
    )
    valid = test_db.create_program(
        {
            "name": "valid at boot",
            "time": "06:30",
            "days": [0],
            "zones": [zone["id"]],
            "enabled": True,
        }
    )
    previous = scheduler_module.scheduler
    scheduler_module.scheduler = None
    value = None
    try:
        with (
            patch.object(IrrigationScheduler, "cleanup_jobs_on_boot", return_value=True),
            patch.object(IrrigationScheduler, "stop_on_boot_active_zones", return_value=True),
        ):
            value = scheduler_module.init_scheduler(test_db)

        assert value._boot_reconcile_ok is True
        assert value._boot_reconcile_failures == set()
        assert test_db.get_program(smart["id"])["enabled"] is False
        assert _program_jobs(value, smart["id"]) == []
        assert [job.id for job in _program_jobs(value, valid["id"])] == [f"program:{valid['id']}:main:d0"]
    finally:
        if value is not None:
            with contextlib.suppress(RuntimeError, ValueError):
                value.stop()
        scheduler_module.scheduler = previous


def test_reconcile_program_from_db_preserves_interval_anchor_after_zone_unlink(test_db):
    first, second, program = _two_zone_interval_program(test_db)
    scheduler = IrrigationScheduler(test_db)
    scheduler.start(paused=True)
    try:
        timezone = scheduler._controller_timezone()
        anchor = datetime(2026, 7, 25, 6, 45, tzinfo=timezone)
        fingerprint = scheduler.program_schedule_fingerprint(program["id"], program)
        assert scheduler.schedule_program(
            program["id"],
            program,
            interval_anchors={"main": anchor},
            expected_fingerprint=fingerprint,
        )

        moved = test_db.update_zone(first["id"], {"group_id": 999})
        assert moved["affected_program_ids"] == [program["id"]]
        updated = test_db.get_program(program["id"])
        assert updated["zones"] == [second["id"]]
        assert scheduler.reconcile_program_from_db(program["id"]) is True

        job = _program_jobs(scheduler, program["id"])[0]
        assert job.trigger.start_date == anchor
        assert list(job.args)[1] == [second["id"]]
        assert list(job.args)[-1] == scheduler.program_schedule_fingerprint(program["id"], updated)
        assert first["id"] not in list(job.args)[1]
    finally:
        with contextlib.suppress(RuntimeError, ValueError):
            scheduler.stop()


def test_stale_program_fire_self_heals_schedule_but_skips_current_fire(test_db):
    first, second, program = _two_zone_interval_program(test_db)
    scheduler = IrrigationScheduler(test_db)
    scheduler.start(paused=True)
    try:
        timezone = scheduler._controller_timezone()
        anchor = datetime(2026, 7, 25, 6, 45, tzinfo=timezone)
        old_fingerprint = scheduler.program_schedule_fingerprint(program["id"], program)
        assert scheduler.schedule_program(
            program["id"],
            program,
            interval_anchors={"main": anchor},
            expected_fingerprint=old_fingerprint,
        )
        old_args = list(_program_jobs(scheduler, program["id"])[0].args)

        moved = test_db.update_zone(first["id"], {"group_id": 999})
        assert moved["affected_program_ids"] == [program["id"]]
        updated = test_db.get_program(program["id"])
        with (
            patch.object(scheduler_module, "get_scheduler", return_value=scheduler),
            patch.object(scheduler, "_run_program_threaded") as run,
        ):
            scheduler_module.job_run_program(*old_args)

        run.assert_not_called()
        healed = _program_jobs(scheduler, program["id"])[0]
        assert healed.trigger.start_date == anchor
        assert list(healed.args)[1] == [second["id"]]
        assert list(healed.args)[-1] == scheduler.program_schedule_fingerprint(program["id"], updated)

        # The reconciled recurring job remains live for its future interval.
        assert healed.next_run_time is None or healed.next_run_time >= anchor - timedelta(days=3)
    finally:
        with contextlib.suppress(RuntimeError, ValueError):
            scheduler.stop()


def test_stale_interval_fire_self_heals_changed_time_and_slot_set(test_db):
    _first, _second, program = _two_zone_interval_program(test_db)
    scheduler = IrrigationScheduler(test_db)
    scheduler.start(paused=True)
    try:
        timezone = scheduler._controller_timezone()
        original_anchor = datetime(2026, 7, 25, 6, 45, tzinfo=timezone)
        fingerprint = scheduler.program_schedule_fingerprint(program["id"], program)
        assert scheduler.schedule_program(
            program["id"],
            program,
            interval_anchors={"main": original_anchor},
            expected_fingerprint=fingerprint,
        )
        stale_args = list(_program_jobs(scheduler, program["id"])[0].args)

        with patch.object(scheduler_module, "get_scheduler", return_value=scheduler):
            with patch.object(scheduler, "_run_program_threaded") as run:
                with_extra = test_db.update_program(
                    program["id"],
                    {"time": "07:15", "extra_times": ["18:45"]},
                )
                scheduler_module.job_run_program(*stale_args)

            run.assert_not_called()
            jobs = {str(job.id): job for job in _program_jobs(scheduler, program["id"])}
            assert set(jobs) == {
                f"program:{program['id']}:main",
                f"program:{program['id']}:extra:0",
            }
            assert (
                jobs[f"program:{program['id']}:main"].trigger.start_date.hour,
                jobs[f"program:{program['id']}:main"].trigger.start_date.minute,
            ) == (7, 15)
            assert (
                jobs[f"program:{program['id']}:extra:0"].trigger.start_date.hour,
                jobs[f"program:{program['id']}:extra:0"].trigger.start_date.minute,
            ) == (18, 45)
            assert all(
                list(job.args)[-1] == scheduler.program_schedule_fingerprint(program["id"], with_extra)
                for job in jobs.values()
            )

            stale_extra_args = list(jobs[f"program:{program['id']}:extra:0"].args)
            without_extra = test_db.update_program(program["id"], {"extra_times": []})
            scheduler_module.job_run_program(*stale_extra_args)

        healed = _program_jobs(scheduler, program["id"])
        assert [str(job.id) for job in healed] == [f"program:{program['id']}:main"]
        assert (healed[0].trigger.start_date.hour, healed[0].trigger.start_date.minute) == (7, 15)
        assert list(healed[0].args)[-1] == scheduler.program_schedule_fingerprint(program["id"], without_extra)
    finally:
        with contextlib.suppress(RuntimeError, ValueError):
            scheduler.stop()


def test_weather_rejection_has_no_orphan_program_lifecycle(test_db):
    first, second, program = _two_zone_interval_program(test_db)
    scheduler = IrrigationScheduler(test_db)

    with patch.object(
        scheduler,
        "_check_weather_skip",
        return_value={"skip": True, "reason": "forecast rain"},
    ):
        assert (
            scheduler._run_program_threaded(
                program["id"],
                [first["id"], second["id"]],
                program["name"],
            )
            is True
        )

    assert test_db.get_logs(event_type="program_start") == []
    assert test_db.get_logs(event_type="program_finish") == []
    assert test_db.get_logs(event_type="program_failed") == []
    assert len(test_db.get_logs(event_type="program_weather_skip")) == 1


def test_program_start_failure_records_failed_terminal_not_success(test_db):
    first, _second, program = _two_zone_interval_program(test_db)
    scheduler = IrrigationScheduler(test_db)

    with (
        patch.object(scheduler, "_check_weather_skip", return_value={"skip": False, "reason": ""}),
        patch("services.zone_control.exclusive_start_zone", return_value=False),
    ):
        assert scheduler._run_program_threaded(program["id"], [first["id"]], program["name"]) is False

    assert test_db.get_logs(event_type="program_start") == []
    assert test_db.get_logs(event_type="program_finish") == []
    failed = test_db.get_logs(event_type="program_failed")
    assert len(failed) == 1
    assert json.loads(failed[0]["details"]) == {
        "program_id": program["id"],
        "program_name": program["name"],
        "started": False,
        "status": "failed",
        "success": False,
    }


def test_failed_stop_closes_started_program_with_failed_terminal(test_db):
    first, _second, program = _two_zone_interval_program(test_db)
    scheduler = IrrigationScheduler(test_db)

    with (
        patch.object(scheduler, "_check_weather_skip", return_value={"skip": False, "reason": ""}),
        patch.object(scheduler, "_get_weather_adjusted_duration", return_value=1),
        patch.object(scheduler._shutdown_event, "wait", return_value=True),
        patch.object(scheduler, "schedule_zone_hard_stop", return_value=True),
        patch.object(scheduler, "_stop_zone", return_value=False),
        patch("services.zone_control.exclusive_start_zone", return_value=True),
    ):
        assert scheduler._run_program_threaded(program["id"], [first["id"]], program["name"]) is False

    started = test_db.get_logs(event_type="program_start")
    assert len(started) == 1
    assert json.loads(started[0]["details"])["status"] == "started"
    assert test_db.get_logs(event_type="program_finish") == []
    failed = test_db.get_logs(event_type="program_failed")
    assert len(failed) == 1
    assert json.loads(failed[0]["details"])["started"] is True


def test_program_lifecycle_logging_cannot_preempt_hard_stop_ownership(test_db):
    first, _second, program = _two_zone_interval_program(test_db)
    scheduler = IrrigationScheduler(test_db)
    events: list[str] = []
    real_add_log = test_db.add_log

    def add_log(log_type, details=None):
        if log_type == "program_start":
            events.append("lifecycle")
            raise RuntimeError("log store busy")
        return real_add_log(log_type, details)

    def plant_watchdog(*_args, **_kwargs):
        events.append("watchdog")
        return True

    with (
        patch.object(test_db, "add_log", side_effect=add_log),
        patch.object(scheduler, "_check_weather_skip", return_value={"skip": False, "reason": ""}),
        patch.object(scheduler, "_get_weather_adjusted_duration", return_value=1),
        patch.object(scheduler._shutdown_event, "wait", return_value=False),
        patch.object(scheduler, "schedule_zone_hard_stop", side_effect=plant_watchdog),
        patch.object(scheduler, "_stop_zone", return_value=True),
        patch("services.zone_control.exclusive_start_zone", return_value=True),
    ):
        assert scheduler._run_program_threaded(program["id"], [first["id"]], program["name"]) is True

    assert events[:2] == ["watchdog", "lifecycle"]
    assert len(test_db.get_logs(event_type="program_finish")) == 1


@pytest.mark.parametrize("watchdog_result", [False, None])
def test_unverified_watchdog_forces_immediate_stop_and_failed_terminal(test_db, watchdog_result):
    first, _second, program = _two_zone_interval_program(test_db)
    scheduler = IrrigationScheduler(test_db)

    with (
        patch.object(scheduler, "_check_weather_skip", return_value={"skip": False, "reason": ""}),
        patch.object(scheduler, "_get_weather_adjusted_duration", return_value=1),
        patch.object(scheduler, "schedule_zone_hard_stop", return_value=watchdog_result),
        patch.object(scheduler, "_stop_zone", return_value=True) as stop_zone,
        patch("services.zone_control.exclusive_start_zone", return_value=True),
    ):
        assert scheduler._run_program_threaded(program["id"], [first["id"]], program["name"]) is False

    stop_zone.assert_called_once_with(
        first["id"],
        reason="watchdog_arm_failed",
        activation_token=None,
        force=True,
    )
    assert test_db.get_logs(event_type="program_start") == []
    assert test_db.get_logs(event_type="program_finish") == []
    failed = test_db.get_logs(event_type="program_failed")
    assert len(failed) == 1
    assert json.loads(failed[0]["details"])["started"] is True


def test_partially_cancelled_program_is_not_logged_as_success(test_db):
    first, _second, program = _two_zone_interval_program(test_db)
    scheduler = IrrigationScheduler(test_db)
    group_id = first["group_id"]

    def start_then_cancel(_zone_id, **_kwargs):
        scheduler.group_cancel_events[group_id].set()
        return True

    with (
        patch.object(scheduler, "_check_weather_skip", return_value={"skip": False, "reason": ""}),
        patch.object(scheduler, "_get_weather_adjusted_duration", return_value=1),
        patch.object(scheduler, "schedule_zone_hard_stop", return_value=True),
        patch.object(scheduler, "_stop_zone", return_value=True),
        patch("services.zone_control.exclusive_start_zone", side_effect=start_then_cancel),
    ):
        assert scheduler._run_program_threaded(program["id"], [first["id"]], program["name"]) is True

    assert test_db.get_logs(event_type="program_finish") == []
    cancelled = test_db.get_logs(event_type="program_cancelled")
    assert len(cancelled) == 1
    payload = json.loads(cancelled[0]["details"])
    assert payload["status"] == "cancelled"
    assert payload["success"] is False
    assert payload["started"] is True


def test_program_stop_does_not_touch_a_replacement_activation(test_db):
    first, _second, program = _two_zone_interval_program(test_db)
    scheduler = IrrigationScheduler(test_db)
    old_token = "program-old-activation"
    new_token = "manual-replacement-activation"

    def start_old(zone_id, **_kwargs):
        test_db.update_zone(
            zone_id,
            {
                "state": "on",
                "commanded_state": "on",
                "command_id": old_token,
                "watering_start_time": "2026-07-23 06:45:00",
            },
        )
        return True

    replaced = False

    def replace_before_stop(*_args, **_kwargs):
        nonlocal replaced
        if not replaced:
            replaced = True
            test_db.update_zone(
                first["id"],
                {
                    "state": "on",
                    "commanded_state": "on",
                    "command_id": new_token,
                    "watering_start_time": "2026-07-23 06:46:00",
                },
            )
        return True

    with (
        patch.object(scheduler, "_check_weather_skip", return_value={"skip": False, "reason": ""}),
        patch.object(scheduler, "_get_weather_adjusted_duration", return_value=1),
        patch.object(scheduler, "schedule_zone_hard_stop", return_value=True),
        patch.object(scheduler, "_persist_program_activation_evidence", return_value=True),
        patch.object(scheduler, "_clear_program_activation_evidence") as clear_evidence,
        patch.object(scheduler, "cancel_zone_jobs") as cancel_zone_jobs,
        patch.object(scheduler._shutdown_event, "wait", side_effect=replace_before_stop),
        patch("services.zone_control.exclusive_start_zone", side_effect=start_old),
        patch("services.zone_control.stop_zone") as central_stop,
    ):
        assert scheduler._run_program_threaded(program["id"], [first["id"]], program["name"]) is False

    central_stop.assert_not_called()
    clear_evidence.assert_not_called()
    cancel_zone_jobs.assert_not_called()
    current = test_db.get_zone(first["id"])
    assert current["command_id"] == new_token
    assert current["state"] == "on"
    assert first["id"] in scheduler.active_zones
    assert test_db.get_logs(event_type="program_finish") == []
