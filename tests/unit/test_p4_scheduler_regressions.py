"""Post-review regressions for scheduler persistence and physical-stop safety."""

from __future__ import annotations

import contextlib
import importlib
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


def test_load_programs_reports_orphan_recurring_job_removal_failure(sched, test_db):
    _, program = _create_interval_program(test_db)
    sched.start(paused=True)
    assert sched.schedule_program(program["id"], program) is True
    job_id = _program_jobs(sched, program["id"])[0].id
    assert test_db.delete_program(program["id"]) is True
    real_remove = sched.scheduler.remove_job

    def fail_program_remove(candidate, *args, **kwargs):
        if candidate == job_id:
            raise RuntimeError("jobstore unavailable")
        return real_remove(candidate, *args, **kwargs)

    with patch.object(sched.scheduler, "remove_job", side_effect=fail_program_remove):
        assert sched.load_programs() is False

    assert [job.id for job in _program_jobs(sched, program["id"])] == [job_id]


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
    server = test_db.create_mqtt_server({"name": "broker", "host": "127.0.0.1", "port": 1883})
    zone = test_db.create_zone(
        {
            "name": "stopped",
            "duration": 5,
            "group_id": group["id"],
            "mqtt_server_id": server["id"],
            "topic": "/zones/confirmed-off",
        }
    )
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
    assert hard.id not in removed
    assert cap.id not in removed

    sched.cancel_zone_cap(zone["id"])
    assert sched.scheduler.remove_job.call_args_list[-1].args[0] == cap.id


def test_group_cancel_requests_observed_confirmation_and_retains_safety_without_echo(sched, test_db):
    group = test_db.create_group("confirmation-timeout")
    server = test_db.create_mqtt_server({"name": "broker", "host": "127.0.0.1", "port": 1883})
    zone = test_db.create_zone(
        {
            "name": "no-off-echo",
            "duration": 5,
            "group_id": group["id"],
            "mqtt_server_id": server["id"],
            "topic": "/zones/no-off-echo",
        }
    )
    token = "activation-still-owned"
    test_db.update_zone(
        zone["id"],
        {
            "state": "off",
            "commanded_state": "off",
            "observed_state": "unconfirmed",
            "watering_start_time": token,
        },
    )
    hard = MagicMock(id=f"zone_hard_stop:{zone['id']}")
    cap = MagicMock(id=f"zone_cap_stop:{zone['id']}")
    sched.active_zones[zone["id"]] = datetime.now() + timedelta(minutes=5)
    sched.scheduler = MagicMock()
    sched.scheduler.get_jobs.return_value = [hard, cap]

    with patch(
        "services.zone_control.stop_all_in_group",
        return_value={"success": False, "stopped": [], "unresolved": [zone["id"]]},
    ) as stop_all:
        result = sched.cancel_group_jobs(group["id"])

    assert stop_all.call_args.kwargs["require_observed_confirmation"] is True
    assert result["success"] is False
    assert result["unresolved"] == [zone["id"]]
    assert test_db.get_zone(zone["id"])["watering_start_time"] == token
    assert zone["id"] in sched.active_zones
    removed = {call.args[0] for call in sched.scheduler.remove_job.call_args_list}
    assert hard.id not in removed
    assert cap.id not in removed


def test_group_restart_keeps_physical_safety_until_fresh_off(sched, test_db):
    group = test_db.create_group("restart-ack-not-observed")
    server = test_db.create_mqtt_server({"name": "broker", "host": "127.0.0.1", "port": 1883})
    zone = test_db.create_zone(
        {
            "name": "physical-zone",
            "duration": 5,
            "group_id": group["id"],
            "mqtt_server_id": server["id"],
            "topic": "/zones/restart-ack",
        }
    )
    token = "activation-before-restart"
    test_db.update_zone(
        zone["id"],
        {
            "state": "on",
            "commanded_state": "on",
            "observed_state": "on",
            "watering_start_time": token,
        },
    )
    ordinary = MagicMock(id=f"zone_stop:{zone['id']}:old")
    hard = MagicMock(id=f"zone_hard_stop:{zone['id']}")
    cap = MagicMock(id=f"zone_cap_stop:{zone['id']}")
    sched.scheduler = MagicMock()
    sched.scheduler.get_jobs.return_value = [ordinary, hard, cap]
    sched.active_zones[zone["id"]] = datetime.now() + timedelta(minutes=5)

    def broker_ack_only(*_args, **_kwargs):
        test_db.update_zone(
            zone["id"],
            {
                "state": "off",
                "commanded_state": "off",
                "observed_state": "unconfirmed",
            },
        )
        return {"success": True, "stopped": [zone["id"]], "unresolved": []}

    with (
        patch.object(scheduler_module, "TESTING", False),
        patch("services.zone_control.stop_all_in_group", side_effect=broker_ack_only),
    ):
        assert sched.start_group_sequence(group["id"]) is True

    removed = {call.args[0] for call in sched.scheduler.remove_job.call_args_list}
    assert ordinary.id in removed
    assert hard.id not in removed
    assert cap.id not in removed
    assert zone["id"] in sched.active_zones


def test_group_restart_rejects_structured_unresolved_even_if_db_says_off(sched, test_db):
    group = test_db.create_group("restart-unresolved")
    zone = test_db.create_zone({"name": "logical-off", "duration": 5, "group_id": group["id"]})
    sched.scheduler = MagicMock()
    sched.scheduler.get_jobs.return_value = []

    with (
        patch.object(scheduler_module, "TESTING", False),
        patch(
            "services.zone_control.stop_all_in_group",
            return_value={"success": False, "stopped": [], "unresolved": [zone["id"]]},
        ) as stop_all,
    ):
        assert sched.start_group_sequence(group["id"]) is False

    assert stop_all.call_args.kwargs["require_observed_confirmation"] is True
    sched.scheduler.add_job.assert_not_called()
    assert group["id"] not in sched.group_cancel_events


def test_group_restart_rejects_explicit_legacy_bulk_off_failure(sched, test_db):
    group = test_db.create_group("legacy-stop-failure")
    test_db.create_zone({"name": "zone", "duration": 5, "group_id": group["id"]})

    with patch("services.zone_control.stop_all_in_group", return_value=False):
        assert sched.start_group_sequence(group["id"]) is False

    assert sched.is_group_session_active(group["id"]) is False


def test_group_start_returns_false_and_releases_claim_on_database_error(sched, test_db):
    group = test_db.create_group("group-start-db-error")

    with patch.object(sched.db, "get_zones", side_effect=sqlite3.OperationalError("database unavailable")):
        assert sched.start_group_sequence(group["id"]) is False

    assert sched.is_group_session_active(group["id"]) is False


def test_postpone_sweeper_cannot_erase_concurrent_manual_2099_deadline(sched, test_db):
    group = test_db.create_group("postpone-cas")
    zone = test_db.create_zone({"name": "postponed", "duration": 5, "group_id": group["id"]})
    expired = "2020-01-01 00:00:00"
    future = "2099-12-31 23:59:59"
    test_db.update_zone_postpone(zone["id"], expired, "rain")
    real_get_zones = test_db.get_zones
    snapshot_taken = threading.Event()
    future_written = threading.Event()

    def stale_sweeper_snapshot(*args, **kwargs):
        snapshot = real_get_zones(*args, **kwargs)
        if threading.current_thread().name == "postpone-sweeper":
            snapshot_taken.set()
            assert future_written.wait(timeout=1)
        return snapshot

    with patch.object(test_db, "get_zones", side_effect=stale_sweeper_snapshot):
        worker = threading.Thread(target=sched.clear_expired_postpones, name="postpone-sweeper")
        worker.start()
        assert snapshot_taken.wait(timeout=1)
        test_db.update_zone_postpone(zone["id"], future, "manual")
        future_written.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    persisted = test_db.get_zone(zone["id"])
    assert persisted["postpone_until"] == future
    assert persisted["postpone_reason"] == "manual"


def test_program_runner_skips_when_expiry_cas_loses_to_new_postpone(sched, test_db):
    group = test_db.create_group("postpone-runner-cas")
    zone = test_db.create_zone({"name": "postponed", "duration": 5, "group_id": group["id"]})
    expired = "2020-01-01 00:00:00"
    future = "2099-12-31 23:59:59"
    test_db.update_zone_postpone(zone["id"], expired, "rain")

    def concurrent_extension(*_args, **_kwargs):
        test_db.update_zone_postpone(zone["id"], future, "manual")
        return False

    with (
        patch.object(sched, "_clear_zone_postpone_if_expired", side_effect=concurrent_extension) as clear_expired,
        patch("services.zone_control.exclusive_start_zone") as start,
    ):
        sched._run_program_threaded(903, [zone["id"]], "postpone-race", manual=True)

    clear_expired.assert_called_once()
    assert clear_expired.call_args.args[:2] == (zone["id"], expired)
    assert isinstance(clear_expired.call_args.args[2], datetime)
    start.assert_not_called()
    assert test_db.get_zone(zone["id"])["postpone_until"] == future


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


@pytest.mark.parametrize(
    "stop_result",
    [False, {"success": False, "stopped": [], "unresolved": []}],
)
def test_group_cancel_cannot_report_success_after_explicit_bulk_off_failure(sched, test_db, stop_result):
    group = test_db.create_group("cancel-stop-failure")
    zone = test_db.create_zone({"name": "zone", "duration": 5, "group_id": group["id"]})

    with patch("services.zone_control.stop_all_in_group", return_value=stop_result):
        result = sched.cancel_group_jobs(group["id"])

    assert result["success"] is False
    assert result["unresolved"] == [zone["id"]]


def test_group_cancel_explicit_failure_stays_false_when_zone_discovery_is_empty(sched, test_db):
    group = test_db.create_group("cancel-empty-discovery")

    with patch("services.zone_control.stop_all_in_group", return_value=False):
        result = sched.cancel_group_jobs(group["id"])

    assert result == {"success": False, "stopped": [], "unresolved": [], "group_id": group["id"]}


def test_group_cancel_returns_fail_closed_structure_when_zone_discovery_raises(sched):
    sched.db = MagicMock()
    sched.db.get_zones.side_effect = sqlite3.OperationalError("database unavailable")

    assert sched.cancel_group_jobs(77) == {
        "success": False,
        "stopped": [],
        "unresolved": [],
        "group_id": 77,
    }


def test_group_cancel_rejects_foreign_zone_ids_without_touching_their_jobs(sched, test_db):
    requested = test_db.create_group("cancel-requested")
    other = test_db.create_group("cancel-other")
    requested_zone = test_db.create_zone({"name": "requested", "duration": 5, "group_id": requested["id"]})
    foreign_zone = test_db.create_zone({"name": "foreign", "duration": 5, "group_id": other["id"]})

    with (
        patch(
            "services.zone_control.stop_all_in_group",
            return_value={"success": True, "stopped": [foreign_zone["id"]], "unresolved": []},
        ),
        patch.object(sched, "cancel_zone_jobs") as cancel_jobs,
        patch.object(sched, "schedule_zone_hard_stop", return_value=True),
    ):
        result = sched.cancel_group_jobs(requested["id"])

    assert result["success"] is False
    assert result["unresolved"] == [requested_zone["id"]]
    assert {call.args[0] for call in cancel_jobs.call_args_list} == {requested_zone["id"]}


def test_cap_callback_is_bound_to_the_activation_that_planted_it(sched, test_db):
    group = test_db.create_group("activation")
    zone = test_db.create_zone({"name": "zone", "duration": 5, "group_id": group["id"]})
    test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": "2026-07-19 11:00:00"})

    with (
        patch.object(scheduler_module, "get_scheduler", return_value=sched),
        patch.object(sched, "_stop_zone") as stop_zone,
    ):
        scheduler_module.job_stop_zone_if_activation(zone["id"], "2026-07-19 10:00:00", True)

    stop_zone.assert_not_called()


def test_activation_callback_prefers_unique_command_id_over_legacy_timestamp(sched, test_db):
    group = test_db.create_group("activation-command-id")
    zone = test_db.create_zone({"name": "zone", "duration": 5, "group_id": group["id"]})
    legacy_timestamp = "2026-07-19 11:00:00"
    command_id = "9f02fc46-8097-4a94-b9ab-bb098f941e22"
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute(
            "UPDATE zones SET state = 'on', watering_start_time = ?, command_id = ? WHERE id = ?",
            (legacy_timestamp, command_id, zone["id"]),
        )
        conn.commit()

    with (
        patch.object(scheduler_module, "get_scheduler", return_value=sched),
        patch.object(sched, "_stop_zone") as stop_zone,
    ):
        scheduler_module.job_stop_zone_if_activation(zone["id"], legacy_timestamp, True)
        stop_zone.assert_not_called()
        scheduler_module.job_stop_zone_if_activation(zone["id"], command_id, True)

    stop_zone.assert_called_once_with(
        zone["id"],
        reason="activation_bound_stop",
        activation_token=command_id,
        force=True,
    )
    assert sched._current_activation_token(zone["id"]) == command_id


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


@pytest.mark.parametrize("safety_kind", ["hard", "cap"])
def test_safety_stop_forces_mqtt_off_and_rearms_after_publish_failure(sched, test_db, safety_kind):
    """Hard/cap expiry must retry OFF even when logical state already says off."""
    server = test_db.create_mqtt_server({"name": "broker", "host": "127.0.0.1", "port": 1883})
    group = test_db.create_group(f"safety-{safety_kind}")
    zone = test_db.create_zone(
        {
            "name": "logical-off-physical-on",
            "duration": 5,
            "group_id": group["id"],
            "topic": "/zones/safety-off",
            "mqtt_server_id": server["id"],
        }
    )
    test_db.update_zone(
        zone["id"],
        {
            "state": "off",
            "commanded_state": "off",
            "observed_state": "on",
            "watering_start_time": None,
        },
    )
    if safety_kind == "hard":
        sched.schedule_zone_hard_stop(zone["id"], datetime.now() + timedelta(minutes=1))
        job_id = f"zone_hard_stop:{zone['id']}"
    else:
        sched.schedule_zone_cap(zone["id"], cap_minutes=1)
        job_id = f"zone_cap_stop:{zone['id']}"
    planted = sched.scheduler.get_job(job_id)
    assert planted is not None

    with (
        patch.object(scheduler_module, "get_scheduler", return_value=sched),
        patch("services.zone_control.db", test_db),
        patch("services.zone_control.publish_mqtt_value", return_value=False) as publish,
        patch("services.zone_control.state_verifier"),
        patch("services.zone_control.water_monitor"),
        patch.object(sched, "schedule_zone_hard_stop", wraps=sched.schedule_zone_hard_stop) as rearm,
    ):
        planted.func(*planted.args)

    publish.assert_called_once()
    assert publish.call_args.args[2] == "0"
    assert test_db.get_zone(zone["id"])["state"] == "fault"
    assert planted.args == (zone["id"], None, True)
    rearm.assert_called_once()
    assert rearm.call_args.kwargs["activation_token"] is None


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


def test_boot_active_zone_scan_does_not_trust_fail_soft_repository_empty_list(sched, test_db):
    group = test_db.create_group("strict-boot-zone-scan")
    zone = test_db.create_zone({"name": "active", "duration": 5, "group_id": group["id"]})
    test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": "2026-07-19 06:00:00"})

    with (
        patch.object(test_db, "get_zones", return_value=[]),
        patch("services.zone_control.stop_zone", return_value=True) as stop_zone,
    ):
        assert sched.stop_on_boot_active_zones() is True

    stop_zone.assert_called_once_with(zone["id"], reason="recovery_boot", force=True)
    assert sched._boot_interrupted_zone_ids == {zone["id"]}


@pytest.mark.parametrize(
    ("stop_ok", "load_ok", "failed_step"),
    [(False, True, "active_zones"), (True, False, "programs")],
)
def test_boot_completion_stays_paused_when_required_reconcile_failed(test_db, stop_ok, load_ok, failed_step):
    old = scheduler_module.scheduler
    scheduler_module.scheduler = None
    try:
        with (
            patch.object(IrrigationScheduler, "start"),
            patch.object(IrrigationScheduler, "cleanup_jobs_on_boot", return_value=True),
            patch.object(IrrigationScheduler, "stop_on_boot_active_zones", return_value=stop_ok),
            patch.object(IrrigationScheduler, "load_programs", return_value=load_ok),
        ):
            value = scheduler_module.init_scheduler(test_db)

        assert value._boot_reconcile_ok is False
        assert failed_step in value._boot_reconcile_failures
        with (
            patch.object(value, "recover_missed_runs") as recover,
            patch.object(value.scheduler, "resume") as resume,
        ):
            assert value.complete_boot_recovery() is False
        recover.assert_not_called()
        resume.assert_not_called()
        assert value._started_paused is True
    finally:
        scheduler_module.scheduler = old


def test_boot_completion_stays_paused_when_transient_job_cleanup_failed(test_db):
    old = scheduler_module.scheduler
    scheduler_module.scheduler = None
    try:
        with (
            patch.object(IrrigationScheduler, "start"),
            patch.object(IrrigationScheduler, "cleanup_jobs_on_boot", return_value=False),
            patch.object(IrrigationScheduler, "stop_on_boot_active_zones", return_value=True),
            patch.object(IrrigationScheduler, "load_programs", return_value=True),
        ):
            value = scheduler_module.init_scheduler(test_db)

        assert value._boot_reconcile_ok is False
        assert value._boot_reconcile_failures == {"jobs"}
        assert value.complete_boot_recovery() is False
        assert value._started_paused is True
    finally:
        scheduler_module.scheduler = old


def test_paused_scheduler_shutdown_never_submits_jobs_after_executor_close(test_db, caplog):
    value = IrrigationScheduler(test_db)
    fired = threading.Event()
    value.start(paused=True)
    value.scheduler.add_job(
        fired.set,
        scheduler_module.DateTrigger(
            run_date=value._controller_now(naive=True),
            timezone=value._controller_timezone(),
        ),
        id="shutdown-race-regression",
        jobstore="volatile",
        replace_existing=True,
    )

    with caplog.at_level("ERROR", logger="apscheduler.scheduler"):
        assert value.stop() is True

    assert fired.is_set() is False
    assert not any("Error submitting job" in record.getMessage() for record in caplog.records)


def test_boot_cleanup_reports_transient_job_removal_failure(sched):
    stale = MagicMock(id="group_seq:77:123")
    sched.scheduler = MagicMock()
    sched.scheduler.get_jobs.return_value = [stale]
    sched.scheduler.remove_job.side_effect = RuntimeError("jobstore unavailable")

    assert sched.cleanup_jobs_on_boot() is False


def test_program_runner_aborts_before_next_zone_when_off_is_unresolved(sched, test_db):
    group = test_db.create_group("program-off-failure")
    zones = [test_db.create_zone({"name": f"z{index}", "duration": 1, "group_id": group["id"]}) for index in range(2)]

    with (
        patch("services.zone_control.exclusive_start_zone", return_value=True) as start,
        patch.object(sched._shutdown_event, "wait", return_value=True),
        patch.object(sched, "_stop_zone", return_value=False) as stop,
    ):
        sched._run_program_threaded(901, [zone["id"] for zone in zones], "off-failure", manual=True)

    start.assert_called_once_with(zones[0]["id"], source="program")
    stop.assert_called_once_with(zones[0]["id"], reason="auto")


def test_group_runner_aborts_before_next_zone_when_off_is_unresolved(sched, test_db, monkeypatch):
    monkeypatch.setenv("SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ", "1")
    group = test_db.create_group("group-off-failure")
    zones = [test_db.create_zone({"name": f"z{index}", "duration": 1, "group_id": group["id"]}) for index in range(2)]
    sched.group_cancel_events[group["id"]] = threading.Event()

    with (
        patch("services.zone_control.exclusive_start_zone", return_value=True) as start,
        patch.object(sched._shutdown_event, "wait", return_value=True),
        patch.object(sched, "_stop_zone", return_value=False) as stop,
    ):
        sched._run_group_sequence(group["id"], [zone["id"] for zone in zones], manual=True)

    start.assert_called_once_with(zones[0]["id"], source="program")
    stop.assert_called_once_with(zones[0]["id"], reason="group_sequence")


def test_broker_ack_keeps_last_safety_retry_until_observed_off(sched, test_db):
    group = test_db.create_group("ack-not-observed")
    server = test_db.create_mqtt_server({"name": "broker", "host": "127.0.0.1", "port": 1883})
    zone = test_db.create_zone(
        {
            "name": "zone",
            "duration": 5,
            "group_id": group["id"],
            "mqtt_server_id": server["id"],
            "topic": "/zones/ack-not-observed",
        }
    )
    token = "activation-opaque-token"
    test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": token})
    hard = MagicMock(id=f"zone_hard_stop:{zone['id']}")
    sched.scheduler = MagicMock()
    sched.scheduler.get_jobs.return_value = [hard]
    sched.active_zones[zone["id"]] = datetime.now() + timedelta(minutes=5)

    with patch("services.zone_control.stop_zone", return_value=True):
        assert sched._stop_zone(zone["id"], reason="auto") is True

    removed = {call.args[0] for call in sched.scheduler.remove_job.call_args_list}
    assert hard.id not in removed
    assert zone["id"] in sched.active_zones


def test_safety_ack_replants_hard_retry_when_firing_job_was_last_one(sched, test_db):
    group = test_db.create_group("ack-replant")
    server = test_db.create_mqtt_server({"name": "broker", "host": "127.0.0.1", "port": 1883})
    zone = test_db.create_zone(
        {
            "name": "zone",
            "duration": 5,
            "group_id": group["id"],
            "mqtt_server_id": server["id"],
            "topic": "/zones/ack-replant",
        }
    )
    token = "activation-opaque-token"
    test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": token})
    sched.scheduler = MagicMock()
    sched.scheduler.get_jobs.return_value = []

    with (
        patch("services.zone_control.stop_zone", return_value=True),
        patch.object(sched, "schedule_zone_hard_stop") as replant,
    ):
        assert sched._stop_zone(zone["id"], reason="activation_bound_stop", force=True) is True

    replant.assert_called_once()
    assert replant.call_args.kwargs["activation_token"] == token


def test_interval_crash_recovery_uses_live_preserved_occurrence(sched, test_db):
    group = test_db.create_group("interval-recovery")
    zones = [test_db.create_zone({"name": f"z{index}", "duration": 20, "group_id": group["id"]}) for index in range(2)]
    program = test_db.create_program(
        {
            "name": "anchored interval",
            "time": "05:50",
            "schedule_type": "interval",
            "interval_days": 2,
            "days": [],
            "zones": [zone["id"] for zone in zones],
            "enabled": True,
        }
    )
    now = datetime(2026, 7, 20, 6, 0, 0)
    timezone = sched._controller_timezone()
    anchor = (now - timedelta(minutes=10)).replace(tzinfo=timezone)
    sched.start(paused=True)
    sched.scheduler.add_job(
        scheduler_module.job_run_program,
        scheduler_module.IntervalTrigger(days=2, start_date=anchor, timezone=timezone),
        args=[program["id"], [zone["id"] for zone in zones], program["name"]],
        id=f"program:{program['id']}:main",
        replace_existing=True,
    )
    sched._boot_interrupted_zone_ids = {zones[0]["id"]}

    with patch.object(sched, "_controller_now", return_value=now):
        assert sched.recover_missed_runs(require_interrupted_evidence=True) is True

    intents = sched._read_boot_recovery_intents_strict()
    assert next(iter(intents.values()))["zones"] == [zone["id"] for zone in zones]
    recovery = [job for job in sched.scheduler.get_jobs() if str(job.id).startswith("boot_recovery:")]
    assert len(recovery) == 1
    assert list(recovery[0].args) == [next(iter(intents))]


def test_durable_intent_restores_job_after_submit_before_callback_crash(test_db):
    group = test_db.create_group("durable-recovery")
    zone = test_db.create_zone({"name": "interrupted", "duration": 30, "group_id": group["id"]})
    program = test_db.create_program(
        {
            "name": "durable recovery",
            "time": "06:00",
            "schedule_type": "weekdays",
            "days": [0, 1, 2, 3, 4, 5, 6],
            "zones": [zone["id"]],
            "enabled": True,
        }
    )
    now = datetime(2026, 7, 20, 6, 5, 0)

    first = IrrigationScheduler(test_db)
    first.start(paused=True)
    try:
        first._boot_interrupted_zone_ids = {zone["id"]}
        with patch.object(first, "_controller_now", return_value=now):
            assert first.recover_missed_runs(require_interrupted_evidence=True) is True
        intents = first._read_boot_recovery_intents_strict()
        intent_id = next(iter(intents))
        job_id = first._boot_recovery_job_id(intent_id)
        assert first.scheduler.get_job(job_id, jobstore="default") is not None

        # APScheduler removes one-shot jobs at submission. Simulate the crash
        # window after that removal but before the callback can ACK the intent.
        first.scheduler.remove_job(job_id, jobstore="default")
        assert first.scheduler.get_job(job_id, jobstore="default") is None
        assert intent_id in first._read_boot_recovery_intents_strict()
    finally:
        first.stop()

    second = IrrigationScheduler(test_db)
    second.start(paused=True)
    try:
        second._boot_interrupted_zone_ids = set()  # lifecycle marker was already handed off/cleared
        with patch.object(second, "_controller_now", return_value=now + timedelta(minutes=1)):
            assert second.recover_missed_runs(require_interrupted_evidence=True) is True
        restored = second._read_boot_recovery_intents_strict()
        assert restored[intent_id]["program_id"] == program["id"]
        assert second.scheduler.get_job(second._boot_recovery_job_id(intent_id), jobstore="default") is not None
    finally:
        second.stop()


def test_failed_boot_recovery_attempt_keeps_durable_intent(sched, test_db):
    group = test_db.create_group("failed-durable-recovery")
    zone = test_db.create_zone({"name": "interrupted", "duration": 30, "group_id": group["id"]})
    test_db.create_program(
        {
            "name": "retry later",
            "time": "06:00",
            "schedule_type": "weekdays",
            "days": [0, 1, 2, 3, 4, 5, 6],
            "zones": [zone["id"]],
            "enabled": True,
        }
    )
    now = datetime(2026, 7, 20, 6, 5, 0)
    sched.start(paused=True)
    sched._boot_interrupted_zone_ids = {zone["id"]}
    with patch.object(sched, "_controller_now", return_value=now):
        assert sched.recover_missed_runs(require_interrupted_evidence=True) is True
    intent_id = next(iter(sched._read_boot_recovery_intents_strict()))

    with (
        patch.object(sched, "_controller_now", return_value=now + timedelta(minutes=1)),
        patch.object(sched, "_run_program_threaded", return_value=False),
    ):
        assert sched._execute_boot_recovery_intent(intent_id) is False

    assert intent_id in sched._read_boot_recovery_intents_strict()


def test_boot_recovery_candidate_rejects_memory_jobstore_handoff(sched, test_db):
    group = test_db.create_group("non-durable-recovery")
    zone = test_db.create_zone({"name": "interrupted", "duration": 30, "group_id": group["id"]})
    test_db.create_program(
        {
            "name": "must persist",
            "time": "06:00",
            "schedule_type": "weekdays",
            "days": [0, 1, 2, 3, 4, 5, 6],
            "zones": [zone["id"]],
            "enabled": True,
        }
    )
    sched.start(paused=True)
    sched._boot_interrupted_zone_ids = {zone["id"]}
    sched.jobstore_backend = "memory-fallback"

    with patch.object(sched, "_controller_now", return_value=datetime(2026, 7, 20, 6, 5, 0)):
        assert sched.recover_missed_runs(require_interrupted_evidence=True) is False

    assert sched._read_boot_recovery_intents_strict()
    assert not any(str(job.id).startswith("boot_recovery:") for job in sched.scheduler.get_jobs())


def test_complete_boot_recovery_exposes_strict_durable_ack(sched):
    sched.start(paused=True)
    sched._boot_reconcile_ok = True
    sched._started_paused = True

    with patch.object(sched.scheduler, "resume") as resume:
        assert sched.complete_boot_recovery() is True

    resume.assert_called_once_with()
    assert sched.boot_recovery_handoff_is_durable() is True


def test_naive_hard_stop_is_interpreted_in_process_timezone(test_db, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("WB_TZ", "Asia/Yekaterinburg")
    value = IrrigationScheduler(test_db)
    process_timezone = scheduler_module.ZoneInfo("UTC")
    process_local_run_at = datetime.now(process_timezone).replace(tzinfo=None) + timedelta(minutes=10)

    value.schedule_zone_hard_stop(99_001, process_local_run_at, activation_token="activation-token")

    job = value.scheduler.get_job("zone_hard_stop:99001")
    assert job is not None
    remaining = (job.trigger.run_date - datetime.now(job.trigger.run_date.tzinfo)).total_seconds()
    assert 570 <= remaining <= 630


def test_timer_fire_revalidation_preserves_sorted_zone_execution_order(sched, test_db):
    group = test_db.create_group("sorted-runtime")
    zones = [test_db.create_zone({"name": f"z{index}", "duration": 5, "group_id": group["id"]}) for index in range(3)]
    descending_ids = sorted((zone["id"] for zone in zones), reverse=True)
    program = test_db.create_program(
        {
            "name": "stored user order",
            "time": "06:00",
            "days": [0],
            "zones": descending_ids,
            "enabled": True,
        }
    )

    with (
        patch.object(scheduler_module, "get_scheduler", return_value=sched),
        patch.object(sched, "_run_program_threaded") as run,
    ):
        scheduler_module.job_run_program(program["id"], descending_ids, program["name"], manual=True)

    run.assert_called_once_with(
        program["id"],
        sorted(descending_ids),
        program["name"],
        manual=True,
    )


def test_schedule_program_uses_admission_interval_anchor_exactly(sched, test_db):
    _, program = _create_interval_program(test_db)
    sched.start(paused=True)
    timezone = sched._controller_timezone()
    admitted_anchor = datetime(2026, 7, 25, 6, 45, tzinfo=timezone)

    assert (
        sched.schedule_program(
            program["id"],
            program,
            interval_anchors={"main": admitted_anchor},
        )
        is True
    )

    job = _program_jobs(sched, program["id"])[0]
    assert job.trigger.start_date == admitted_anchor


def test_explicit_interval_anchor_replaces_same_time_restored_phase(sched, test_db):
    _, program = _create_interval_program(test_db)
    sched.start(paused=True)
    assert sched.schedule_program(program["id"], program) is True
    original = _program_jobs(sched, program["id"])[0].trigger.start_date
    replacement = original + timedelta(days=1)

    assert (
        sched.schedule_program(
            program["id"],
            program,
            interval_anchors={"main": replacement},
        )
        is True
    )

    assert _program_jobs(sched, program["id"])[0].trigger.start_date == replacement


def test_stale_interval_revision_is_noop_after_newer_schedule(sched, test_db):
    _, original = _create_interval_program(test_db)
    sched.start(paused=True)
    timezone = sched._controller_timezone()
    original_anchor = datetime(2026, 7, 25, 6, 45, tzinfo=timezone)
    original_fingerprint = sched.program_schedule_fingerprint(original["id"], original)

    with patch.object(scheduler_module, "TESTING", False):
        assert sched.schedule_program(
            original["id"],
            original,
            interval_anchors={"main": original_anchor},
            expected_fingerprint=original_fingerprint,
        )

        newer = test_db.update_program(original["id"], {"time": "07:00"})
        newer_anchor = datetime(2026, 7, 25, 7, 0, tzinfo=timezone)
        newer_fingerprint = sched.program_schedule_fingerprint(newer["id"], newer)
        assert sched.schedule_program(
            newer["id"],
            newer,
            interval_anchors={"main": newer_anchor},
            expected_fingerprint=newer_fingerprint,
        )
        newer_job = _program_jobs(sched, newer["id"])[0]

        # Delayed continuation A arrives after B. Its old anchor must not
        # cancel or replace B, and stale is a successful no-op contract.
        assert sched.schedule_program(
            original["id"],
            original,
            interval_anchors={"main": original_anchor},
            expected_fingerprint=original_fingerprint,
        )

    current_job = _program_jobs(sched, newer["id"])[0]
    assert current_job.id == newer_job.id
    assert current_job.trigger.start_date == newer_anchor
    assert current_job.args[0] == newer["id"]


def test_partial_explicit_interval_anchor_map_fails_closed(sched, test_db):
    zone, program = _create_interval_program(test_db)
    program = test_db.update_program(
        program["id"],
        {"zones": [zone["id"]], "extra_times": ["18:45"]},
    )
    sched.start(paused=True)
    timezone = sched._controller_timezone()

    assert (
        sched.schedule_program(
            program["id"],
            program,
            interval_anchors={"main": datetime(2026, 7, 25, 6, 45, tzinfo=timezone)},
        )
        is False
    )
    assert _program_jobs(sched, program["id"]) == []


def test_rain_gate_blocks_group_sequence_before_physical_restart(sched, test_db, monkeypatch):
    group = test_db.create_group("rain-blocked-start")
    zone = test_db.create_zone({"name": "zone", "duration": 5, "group_id": group["id"]})
    rain_monitor = importlib.import_module("services.monitors.rain_monitor")

    monkeypatch.setattr(rain_monitor, "is_group_blocked", lambda group_id: group_id == group["id"], raising=False)
    with patch("services.zone_control.stop_all_in_group") as stop_all:
        assert sched.start_group_sequence(group["id"], zone_ids=[zone["id"]]) is False

    stop_all.assert_not_called()
    assert sched.is_group_session_active(group["id"]) is False


def test_rain_gate_failure_is_fail_closed_for_program_admission(sched, test_db, monkeypatch):
    group = test_db.create_group("rain-gate-error")
    zone = test_db.create_zone({"name": "zone", "duration": 5, "group_id": group["id"]})
    rain_monitor = importlib.import_module("services.monitors.rain_monitor")

    def broken_gate(_group_id):
        raise RuntimeError("rain gate unavailable")

    monkeypatch.setattr(rain_monitor, "is_group_blocked", broken_gate, raising=False)
    with (
        patch.object(sched, "_check_weather_skip", return_value={"skip": False}),
        patch("services.zone_control.exclusive_start_zone", return_value=True) as start_zone,
    ):
        assert sched._run_program_threaded(901, [zone["id"]], "rain admission") is False

    start_zone.assert_not_called()


def test_group_runner_rechecks_rain_gate_before_reopening_zone(sched, test_db, monkeypatch):
    group = test_db.create_group("rain-edge-before-on")
    zone = test_db.create_zone({"name": "zone", "duration": 5, "group_id": group["id"]})
    rain_monitor = importlib.import_module("services.monitors.rain_monitor")

    monkeypatch.setattr(rain_monitor, "is_group_blocked", lambda _group_id: True, raising=False)
    sched.group_cancel_events[group["id"]] = threading.Event()

    sched._run_group_sequence(group["id"], [zone["id"]], manual=True)

    assert test_db.get_zone(zone["id"])["state"] == "off"
    assert sched.is_group_session_active(group["id"]) is False


def test_cancel_program_reports_jobstore_failure_and_retains_retry_ownership(sched, test_db):
    _, program = _create_interval_program(test_db)
    sched.start(paused=True)
    assert sched.schedule_program(program["id"], program) is True
    job_id = _program_jobs(sched, program["id"])[0].id
    real_remove = sched.scheduler.remove_job

    def fail_program_remove(candidate, *args, **kwargs):
        if candidate == job_id:
            raise RuntimeError("jobstore unavailable")
        return real_remove(candidate, *args, **kwargs)

    with patch.object(sched.scheduler, "remove_job", side_effect=fail_program_remove):
        assert sched.cancel_program(program["id"]) is False

    assert [job.id for job in _program_jobs(sched, program["id"])] == [job_id]
    assert sched.program_jobs[program["id"]] == [job_id]
    assert sched.cancel_program(program["id"]) is True
    assert _program_jobs(sched, program["id"]) == []


def test_disable_program_propagates_recurring_job_removal_failure(sched, test_db):
    _, program = _create_interval_program(test_db)
    sched.start(paused=True)
    assert sched.schedule_program(program["id"], program) is True
    job_id = _program_jobs(sched, program["id"])[0].id
    disabled = test_db.update_program(program["id"], {"enabled": False})
    real_remove = sched.scheduler.remove_job

    def fail_program_remove(candidate, *args, **kwargs):
        if candidate == job_id:
            raise RuntimeError("jobstore unavailable")
        return real_remove(candidate, *args, **kwargs)

    with (
        patch.object(scheduler_module, "TESTING", False),
        patch.object(sched.scheduler, "remove_job", side_effect=fail_program_remove),
    ):
        assert sched.schedule_program(program["id"], disabled) is False

    assert [job.id for job in _program_jobs(sched, program["id"])] == [job_id]
