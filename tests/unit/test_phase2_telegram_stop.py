"""Regression coverage for Telegram group stop semantics."""

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
