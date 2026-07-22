"""Regression coverage for Telegram group stop semantics."""

from datetime import datetime
from unittest.mock import Mock, patch

from routes import telegram


def _scheduler_stop_summary(
    group_id: int,
    *,
    stopped: list[int],
    unresolved: list[int] | None = None,
    aggregate_valid: bool = True,
    unverified_zone_ids: list[int] | None = None,
    retry_scheduled: bool = False,
) -> dict:
    unresolved = list(unresolved or [])
    unverified_zone_ids = list(unverified_zone_ids or [])
    return {
        "success": aggregate_valid and not unresolved and not unverified_zone_ids,
        "group_id": group_id,
        "aggregate_valid": aggregate_valid,
        "stopped": list(stopped),
        "unresolved": unresolved,
        "unverified_zone_ids": unverified_zone_ids,
        "retry_scheduled": retry_scheduled,
    }


def test_group_stop_cancels_scheduler_session() -> None:
    scheduler = Mock()
    scheduler.cancel_group_jobs.return_value = _scheduler_stop_summary(7, stopped=[])

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch("services.zone_control.stop_all_in_group") as stop_all_in_group,
        patch.object(telegram, "_read_group_zones_strict", return_value=[]),
    ):
        result = telegram._do_group_stop(7)

    assert result == "⏹ Полив группы остановлен"
    scheduler.cancel_group_jobs.assert_called_once_with(7)
    stop_all_in_group.assert_not_called()


def test_group_stop_falls_back_to_zone_control_without_scheduler() -> None:
    zone = {"id": 81, "group_id": 8, "state": "off", "observed_state": None}
    with (
        patch("irrigation_scheduler.get_scheduler", return_value=None) as get_scheduler,
        patch("services.zone_control.stop_zone", return_value=True) as stop_zone,
        patch.object(telegram, "_read_group_zones_strict", return_value=[zone]),
    ):
        result = telegram._do_group_stop(8)

    assert result == "⏹ Полив группы остановлен"
    get_scheduler.assert_called_once_with()
    stop_zone.assert_called_once_with(81, reason="telegram", force=True)


def test_group_stop_reports_unresolved_off_and_replants_hard_stop() -> None:
    scheduler = Mock()
    unresolved = [{"id": 71, "group_id": 7, "state": "fault"}]
    scheduler.cancel_group_jobs.return_value = _scheduler_stop_summary(7, stopped=[], unresolved=[71])
    scheduler.schedule_zone_hard_stop.return_value = True

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch.object(telegram, "_read_group_zones_strict", return_value=unresolved),
    ):
        result = telegram._do_group_stop(7)

    assert "OFF не подтверждён" in result
    assert "71" in result
    scheduler.schedule_zone_hard_stop.assert_called_once()
    zone_id, run_at = scheduler.schedule_zone_hard_stop.call_args.args
    assert zone_id == 71
    assert isinstance(run_at, datetime)


def test_group_stop_honors_structured_scheduler_unresolved_result() -> None:
    scheduler = Mock()
    scheduler.cancel_group_jobs.return_value = {
        "success": False,
        "aggregate_valid": True,
        "stopped": [],
        "unresolved": [74],
        "unverified_zone_ids": [],
        "retry_scheduled": True,
        "group_id": 7,
    }
    scheduler.schedule_zone_hard_stop.return_value = True
    stale_logical_off = [{"id": 74, "group_id": 7, "state": "off", "observed_state": None}]

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch.object(telegram, "_read_group_zones_strict", return_value=stale_logical_off),
    ):
        result = telegram._do_group_stop(7)

    assert "OFF не подтверждён" in result
    assert "74" in result
    scheduler.schedule_zone_hard_stop.assert_called_once()


def test_group_stop_honors_structured_scheduler_failure_without_ids() -> None:
    scheduler = Mock()
    scheduler.cancel_group_jobs.return_value = {
        "success": False,
        "aggregate_valid": False,
        "stopped": [],
        "unresolved": [],
        "unverified_zone_ids": [],
        "retry_scheduled": False,
        "group_id": 7,
    }

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch.object(telegram, "_read_group_zones_strict", return_value=[]),
    ):
        result = telegram._do_group_stop(7)

    assert "OFF не подтверждён для группы 7" in result
    assert "Повторная остановка не запланирована: нет доступных целей." in result
    assert "Повторная остановка запланирована." not in result
    scheduler.schedule_zone_hard_stop.assert_not_called()


def test_group_stop_treats_logical_off_with_observed_on_as_unresolved() -> None:
    scheduler = Mock()
    scheduler.cancel_group_jobs.return_value = _scheduler_stop_summary(7, stopped=[], unresolved=[72])
    scheduler.schedule_zone_hard_stop.return_value = True
    physically_on = [
        {
            "id": 72,
            "group_id": 7,
            "state": "off",
            "commanded_state": "off",
            "observed_state": "on",
            "mqtt_server_id": 1,
            "topic": "relay/72",
        }
    ]

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch.object(telegram, "_read_group_zones_strict", return_value=physically_on),
    ):
        result = telegram._do_group_stop(7)

    assert "OFF не подтверждён" in result
    assert "72" in result
    scheduler.schedule_zone_hard_stop.assert_called_once()


def test_group_stop_does_not_treat_stored_observed_off_as_fresh_confirmation() -> None:
    scheduler = Mock()
    scheduler.cancel_group_jobs.return_value = _scheduler_stop_summary(7, stopped=[], unresolved=[73])
    scheduler.schedule_zone_hard_stop.return_value = True
    stale_off = [
        {
            "id": 73,
            "group_id": 7,
            "state": "off",
            "commanded_state": "off",
            "observed_state": "off",
            "mqtt_server_id": 1,
            "topic": "relay/73",
        }
    ]

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch.object(telegram, "_read_group_zones_strict", return_value=stale_off),
    ):
        result = telegram._do_group_stop(7)

    assert "Команда OFF принята" in result
    assert "ожидается свежее подтверждение" in result
    scheduler.schedule_zone_hard_stop.assert_called_once()


def test_group_stop_uses_failed_stop_result_even_for_logical_off() -> None:
    no_channel_zone = [{"id": 82, "group_id": 8, "state": "off", "observed_state": None}]

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=None),
        patch("services.zone_control.stop_zone", return_value=False) as stop_zone,
        patch.object(telegram, "_read_group_zones_strict", return_value=no_channel_zone),
        patch("services.observed_state.state_verifier.verify_async") as verify_async,
    ):
        result = telegram._do_group_stop(8)

    stop_zone.assert_called_once_with(82, reason="telegram", force=True)
    assert "OFF не подтверждён" in result
    verify_async.assert_called_once_with(82, "off")


def test_group_stop_without_scheduler_restarts_observed_state_verification() -> None:
    unresolved = [{"id": 81, "group_id": 8, "state": "stopping"}]

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=None),
        patch("services.zone_control.stop_zone", return_value=False),
        patch.object(telegram, "_read_group_zones_strict", return_value=unresolved),
        patch("services.observed_state.state_verifier.verify_async") as verify_async,
    ):
        result = telegram._do_group_stop(8)

    assert "OFF не подтверждён" in result
    verify_async.assert_called_once_with(81, "off")


def test_group_stop_reports_stopping_off_as_pending_confirmation() -> None:
    pending_zone = [
        {
            "id": 83,
            "group_id": 8,
            "state": "stopping",
            "commanded_state": "off",
        }
    ]

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=None),
        patch("services.zone_control.stop_zone", return_value=False),
        patch.object(telegram, "_read_group_zones_strict", return_value=pending_zone),
        patch("services.observed_state.state_verifier.verify_async") as verify_async,
    ):
        result = telegram._do_group_stop(8)

    assert "Команда OFF принята для зон: 83" in result
    assert "ожидается свежее подтверждение реле" in result
    assert "OFF не подтверждён" not in result
    assert "Полив группы остановлен" not in result
    verify_async.assert_called_once_with(83, "off")


def test_group_stop_rejects_none_and_malformed_scheduler_results() -> None:
    zone = {"id": 71, "group_id": 7, "state": "off"}

    for summary in (None, {"success": True}, _scheduler_stop_summary(7, stopped=[True])):
        scheduler = Mock()
        scheduler.cancel_group_jobs.return_value = summary
        scheduler.schedule_zone_hard_stop.return_value = False
        with (
            patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
            patch.object(telegram, "_read_group_zones_strict", return_value=[zone]),
        ):
            result = telegram._do_group_stop(7)

        assert "Полив группы остановлен" not in result
        assert "OFF не подтверждён" in result
        assert "71" in result


def test_group_stop_rejects_wrong_group_invalid_aggregate_and_incomplete_partition() -> None:
    zones = [
        {"id": 71, "group_id": 7, "state": "off"},
        {"id": 72, "group_id": 7, "state": "off"},
    ]
    summaries = (
        _scheduler_stop_summary(8, stopped=[71, 72]),
        _scheduler_stop_summary(
            7,
            stopped=[],
            aggregate_valid=False,
            unverified_zone_ids=[71, 72],
        ),
        _scheduler_stop_summary(7, stopped=[71]),
    )

    for summary in summaries:
        scheduler = Mock()
        scheduler.cancel_group_jobs.return_value = summary
        scheduler.schedule_zone_hard_stop.return_value = False
        with (
            patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
            patch.object(telegram, "_read_group_zones_strict", return_value=zones),
        ):
            result = telegram._do_group_stop(7)

        assert "Полив группы остановлен" not in result
        assert "OFF не подтверждён" in result
        assert "71" in result and "72" in result


def test_group_stop_requires_logical_off_non_mqtt_zone_in_scheduler_partition() -> None:
    zones = [
        {"id": 71, "group_id": 7, "state": "off", "mqtt_server_id": 1, "topic": "relay/71"},
        {"id": 72, "group_id": 7, "state": "off", "mqtt_server_id": None, "topic": ""},
    ]
    scheduler = Mock()
    scheduler.cancel_group_jobs.return_value = _scheduler_stop_summary(7, stopped=[71])
    scheduler.schedule_zone_hard_stop.return_value = False

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch.object(telegram, "_read_group_zones_strict", return_value=zones),
    ):
        result = telegram._do_group_stop(7)

    assert "Полив группы остановлен" not in result
    assert "OFF не подтверждён" in result
    assert "72" in result


def test_group_stop_reports_retry_not_scheduled_when_scheduler_returns_false() -> None:
    zone = {"id": 71, "group_id": 7, "state": "fault"}
    scheduler = Mock()
    scheduler.cancel_group_jobs.return_value = _scheduler_stop_summary(7, stopped=[], unresolved=[71])
    scheduler.schedule_zone_hard_stop.return_value = False

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch.object(telegram, "_read_group_zones_strict", return_value=[zone]),
    ):
        result = telegram._do_group_stop(7)

    assert "Повторная остановка не запланирована" in result
    assert "Повторная остановка запланирована." not in result
