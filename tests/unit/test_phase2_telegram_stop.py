"""Regression coverage for Telegram group stop semantics."""

from datetime import datetime
from unittest.mock import Mock, patch

from routes import telegram


def test_group_stop_cancels_scheduler_session() -> None:
    scheduler = Mock()

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch("services.zone_control.stop_all_in_group") as stop_all_in_group,
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
        patch.object(telegram.db, "get_zones_by_group", return_value=[zone]),
    ):
        result = telegram._do_group_stop(8)

    assert result == "⏹ Полив группы остановлен"
    get_scheduler.assert_called_once_with()
    stop_zone.assert_called_once_with(81, reason="telegram", force=True)


def test_group_stop_reports_unresolved_off_and_replants_hard_stop() -> None:
    scheduler = Mock()
    unresolved = [{"id": 71, "group_id": 7, "state": "fault"}]

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch.object(telegram.db, "get_zones_by_group", return_value=unresolved),
    ):
        result = telegram._do_group_stop(7)

    assert "OFF не подтверждён" in result
    assert "71" in result
    scheduler.schedule_zone_hard_stop.assert_called_once()
    zone_id, run_at = scheduler.schedule_zone_hard_stop.call_args.args
    assert zone_id == 71
    assert isinstance(run_at, datetime)


def test_group_stop_treats_logical_off_with_observed_on_as_unresolved() -> None:
    scheduler = Mock()
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
        patch.object(telegram.db, "get_zones_by_group", return_value=physically_on),
    ):
        result = telegram._do_group_stop(7)

    assert "OFF не подтверждён" in result
    assert "72" in result
    scheduler.schedule_zone_hard_stop.assert_called_once()


def test_group_stop_uses_failed_stop_result_even_for_logical_off() -> None:
    no_channel_zone = [{"id": 82, "group_id": 8, "state": "off", "observed_state": None}]

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=None),
        patch("services.zone_control.stop_zone", return_value=False) as stop_zone,
        patch.object(telegram.db, "get_zones_by_group", return_value=no_channel_zone),
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
        patch.object(telegram.db, "get_zones_by_group", return_value=unresolved),
        patch("services.observed_state.state_verifier.verify_async") as verify_async,
    ):
        result = telegram._do_group_stop(8)

    assert "OFF не подтверждён" in result
    verify_async.assert_called_once_with(81, "off")
