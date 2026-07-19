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
    with (
        patch("irrigation_scheduler.get_scheduler", return_value=None) as get_scheduler,
        patch("services.zone_control.stop_all_in_group") as stop_all_in_group,
    ):
        result = telegram._do_group_stop(8)

    assert result == "⏹ Полив группы остановлен"
    get_scheduler.assert_called_once_with()
    stop_all_in_group.assert_called_once_with(8, reason="telegram", force=True)


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


def test_group_stop_without_scheduler_restarts_observed_state_verification() -> None:
    unresolved = [{"id": 81, "group_id": 8, "state": "stopping"}]

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=None),
        patch("services.zone_control.stop_all_in_group"),
        patch.object(telegram.db, "get_zones_by_group", return_value=unresolved),
        patch("services.observed_state.state_verifier.verify_async") as verify_async,
    ):
        result = telegram._do_group_stop(8)

    assert "OFF не подтверждён" in result
    verify_async.assert_called_once_with(81, "off")
