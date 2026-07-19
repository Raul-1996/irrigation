"""Comprehensive tests for services/telegram_bot.py — mock-based, no real Telegram."""

import asyncio
import os
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

os.environ["TESTING"] = "1"


class TestTelegramBotImport:
    def test_module_imports(self):
        """telegram_bot module should import without errors."""
        try:
            import services.telegram_bot
        except ImportError:
            pytest.skip("telegram_bot has unresolvable dependencies")

    def test_notifier_exists(self):
        """Module should expose a notifier object."""
        try:
            from services.telegram_bot import notifier
            # notifier may be None if aiogram is not configured
        except ImportError:
            pytest.skip("aiogram not available")


def test_same_loop_submission_is_scheduled_without_threadsafe_wait() -> None:
    """An aiogram handler must never wait synchronously on its own event loop."""
    from services import telegram_bot

    notifier = telegram_bot.TelegramNotifier()
    completed: list[bool] = []

    async def send() -> bool:
        completed.append(True)
        return True

    async def scenario() -> None:
        runner = SimpleNamespace(_bot=object(), _loop=asyncio.get_running_loop())
        with (
            patch.object(telegram_bot, "_aiogram_runner", runner),
            patch.object(
                telegram_bot.asyncio,
                "run_coroutine_threadsafe",
                side_effect=AssertionError("must not submit to the current loop"),
            ),
        ):
            assert notifier._submit_aiogram(send()) is True
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert completed == [True]


def test_cross_thread_submission_waits_for_aiogram_result() -> None:
    """Synchronous callers in Flask/background threads retain delivery semantics."""
    from services import telegram_bot

    notifier = telegram_bot.TelegramNotifier()
    completed = threading.Event()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def send() -> bool:
        completed.set()
        return True

    try:
        runner = SimpleNamespace(_bot=object(), _loop=loop)
        with patch.object(telegram_bot, "_aiogram_runner", runner):
            assert notifier._submit_aiogram(send()) is True
        assert completed.wait(timeout=1)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1)
        loop.close()


def test_same_loop_runner_stop_is_scheduled_without_threadsafe_wait() -> None:
    from services import telegram_bot

    async def scenario() -> None:
        runner = telegram_bot.AiogramBotRunner()
        runner._thread = threading.current_thread()
        runner._loop = asyncio.get_running_loop()
        runner._dp = Mock()

        async def stop_polling() -> None:
            return None

        runner._dp.stop_polling = stop_polling
        with patch.object(
            telegram_bot.asyncio,
            "run_coroutine_threadsafe",
            side_effect=AssertionError("must not submit to the current loop"),
        ):
            assert runner.stop(timeout=0) is False
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_aiogram_subscribe_command_is_reachable() -> None:
    from services import telegram_bot

    runner = telegram_bot.AiogramBotRunner()
    fake_db = Mock()
    fake_db.get_bot_user_by_chat.return_value = {"id": 17}
    fake_db.create_or_update_subscription.return_value = True
    fake_notifier = Mock()
    message = SimpleNamespace(
        chat=SimpleNamespace(id=42, username="admin", first_name="Admin"),
        text="/subscribe weekly full 09:30 1010101",
    )

    with (
        patch.object(telegram_bot, "db", fake_db),
        patch.object(telegram_bot, "notifier", fake_notifier),
        patch.object(runner, "_is_authorized_chat", return_value=True),
    ):
        asyncio.run(runner._on_message(message))

    fake_db.set_bot_user_authorized.assert_called_once_with(42, role="admin")
    fake_db.create_or_update_subscription.assert_called_once_with(17, "weekly", "full", "09:30", "1010101", True)
    fake_notifier.send_text.assert_called_once_with(42, "Подписка сохранена")
    fake_notifier.send_message.assert_not_called()


def test_http_fallback_unsubscribe_command_is_reachable() -> None:
    from services import telegram_bot

    poller = telegram_bot.SimpleHTTPPoller()
    fake_db = Mock()
    fake_db.get_bot_user_by_chat.return_value = {"id": 23}
    fake_db.create_or_update_subscription.return_value = True
    fake_notifier = Mock()
    fake_notifier._ensure_token.return_value = "token"

    class Response:
        ok = True

        def json(self):
            poller._running = False
            return {
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": 77, "username": "admin", "first_name": "Admin"},
                            "text": "/unsubscribe",
                        },
                    }
                ]
            }

    with (
        patch.object(telegram_bot, "db", fake_db),
        patch.object(telegram_bot, "notifier", fake_notifier),
        patch.object(telegram_bot, "_is_authorized_chat_id", return_value=True),
        patch.object(telegram_bot, "_load_routes_module", return_value=Mock()),
        patch.object(telegram_bot.requests, "post"),
        patch.object(telegram_bot.requests, "get", return_value=Response()),
    ):
        poller._run()

    fake_db.set_bot_user_authorized.assert_called_once_with(77, role="admin")
    assert fake_db.create_or_update_subscription.call_count == 2
    fake_notifier.send_text.assert_called_once_with(77, "Подписки отключены")
    fake_notifier.send_message.assert_not_called()


def test_reconfigure_token_stops_invalidates_persists_and_starts() -> None:
    from services import telegram_bot

    telegram_bot.notifier._token = "old-plaintext"
    events: list[str] = []

    with (
        patch.object(telegram_bot.db, "get_setting_value", return_value="old-encrypted"),
        patch.object(
            telegram_bot,
            "_stop_runtime_locked",
            side_effect=lambda: events.append("stop") or True,
        ),
        patch.object(
            telegram_bot.db,
            "set_setting_value",
            side_effect=lambda *_args: events.append("persist") or True,
        ),
        patch.object(
            telegram_bot,
            "_start_runtime_locked",
            side_effect=lambda: events.append("start") or True,
        ),
    ):
        assert telegram_bot.reconfigure_bot_token("new-encrypted") is True

    assert events == ["stop", "persist", "start"]
    assert telegram_bot.notifier._token is None


def test_reconfigure_token_delete_does_not_restart_runtime() -> None:
    from services import telegram_bot

    telegram_bot.notifier._token = "old-plaintext"
    with (
        patch.object(telegram_bot.db, "get_setting_value", return_value="old-encrypted"),
        patch.object(telegram_bot, "_stop_runtime_locked", return_value=True),
        patch.object(telegram_bot.db, "set_setting_value", return_value=True) as persist,
        patch.object(telegram_bot, "_start_runtime_locked") as start,
    ):
        assert telegram_bot.reconfigure_bot_token(None) is True

    persist.assert_called_once_with("telegram_bot_token_encrypted", None)
    start.assert_not_called()
    assert telegram_bot.notifier._token is None


def test_reconfigure_token_does_not_mutate_when_old_runner_will_not_stop() -> None:
    from services import telegram_bot

    telegram_bot.notifier._token = "old-plaintext"
    with (
        patch.object(telegram_bot.db, "get_setting_value", return_value="old-encrypted"),
        patch.object(telegram_bot, "_stop_runtime_locked", return_value=False),
        patch.object(telegram_bot.db, "set_setting_value") as persist,
        patch.object(telegram_bot, "_start_runtime_locked") as start,
    ):
        assert telegram_bot.reconfigure_bot_token("new-encrypted") is False

    persist.assert_not_called()
    start.assert_not_called()
    assert telegram_bot.notifier._token == "old-plaintext"
    telegram_bot.notifier.invalidate_token()


def test_reconfigure_token_rolls_back_when_new_runtime_cannot_start() -> None:
    from services import telegram_bot

    with (
        patch.object(telegram_bot.db, "get_setting_value", return_value="old-encrypted"),
        patch.object(telegram_bot, "_stop_runtime_locked", return_value=True),
        patch.object(telegram_bot.db, "set_setting_value", return_value=True) as persist,
        patch.object(telegram_bot, "_start_runtime_locked", side_effect=[False, True]) as start,
    ):
        assert telegram_bot.reconfigure_bot_token("bad-encrypted") is False

    assert persist.call_args_list == [
        (("telegram_bot_token_encrypted", "bad-encrypted"),),
        (("telegram_bot_token_encrypted", "old-encrypted"),),
    ]
    assert start.call_count == 2
