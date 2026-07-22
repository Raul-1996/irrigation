"""Comprehensive tests for services/telegram_bot.py — mock-based, no real Telegram."""

import asyncio
import os
import subprocess
import sys
import threading
import time
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


def test_aiogram_start_reports_bootstrap_token_failure() -> None:
    from services import telegram_bot

    class InvalidTokenBot:
        def __init__(self, *, token: str):
            raise ValueError(f"invalid token: {token}")

    runner = telegram_bot.AiogramBotRunner()
    with (
        patch.object(telegram_bot.notifier, "_ensure_token", return_value="invalid-token"),
        patch.object(telegram_bot, "Bot", InvalidTokenBot),
        patch.object(telegram_bot, "Dispatcher", object),
    ):
        assert runner.start(timeout=1) is False

    assert runner._thread is not None
    runner._thread.join(timeout=1)
    assert not runner._thread.is_alive()


def test_immediate_polling_failure_is_reported_before_start_returns() -> None:
    from services import telegram_bot

    release_close = threading.Event()

    class Session:
        async def close(self) -> None:
            while not release_close.is_set():
                await asyncio.sleep(0.005)

    class AuthenticatedBot:
        def __init__(self, *, token: str):
            self.token = token
            self.session = Session()

        async def get_me(self):
            return {"id": 1}

        async def delete_webhook(self, *, drop_pending_updates: bool):
            return True

    class Registry:
        def register(self, *_args, **_kwargs) -> None:
            return None

    class FailingDispatcher:
        def __init__(self):
            self.message = Registry()
            self.callback_query = Registry()

        async def start_polling(self, *_args, **kwargs) -> None:
            assert kwargs["handle_signals"] is False
            raise RuntimeError("polling bootstrap failed")

        async def stop_polling(self) -> None:
            return None

    runner = telegram_bot.AiogramBotRunner()
    try:
        with (
            patch.object(telegram_bot.notifier, "_ensure_token", return_value="valid-token"),
            patch.object(telegram_bot, "Bot", AuthenticatedBot),
            patch.object(telegram_bot, "Dispatcher", FailingDispatcher),
        ):
            assert runner.start(timeout=1) is False
    finally:
        release_close.set()
        if runner._thread is not None:
            runner._thread.join(timeout=1)


def test_real_dispatcher_runs_in_background_thread_without_signal_handlers() -> None:
    """Real aiogram Dispatcher must not call set_wakeup_fd outside main thread."""
    from services import telegram_bot

    closed = threading.Event()

    class User:
        username = "irrigation_test_bot"
        full_name = "Irrigation Test Bot"

    class Session:
        timeout = None

        async def close(self) -> None:
            closed.set()

    class AuthenticatedBot:
        id = 1

        def __init__(self, *, token: str):
            self.token = token
            self.session = Session()
            self._updates = asyncio.Event()

        async def get_me(self):
            return User()

        async def me(self):
            return User()

        async def delete_webhook(self, *, drop_pending_updates: bool):
            return True

        async def __call__(self, *_args, **_kwargs):
            await self._updates.wait()
            return []

    runner = telegram_bot.AiogramBotRunner()
    with (
        patch.object(telegram_bot.notifier, "_ensure_token", return_value="valid-token"),
        patch.object(telegram_bot, "Bot", AuthenticatedBot),
    ):
        assert runner.start(timeout=2) is True
        assert runner._thread is not None
        assert runner._thread.is_alive()
        assert runner.stop(timeout=2) is True

    assert closed.is_set()


def test_stop_during_aiogram_bootstrap_never_enters_polling() -> None:
    from services import telegram_bot

    delete_started = threading.Event()
    release_delete = threading.Event()
    polling_started = threading.Event()

    class Session:
        async def close(self) -> None:
            return None

    class SlowBootstrapBot:
        def __init__(self, *, token: str):
            self.token = token
            self.session = Session()

        async def get_me(self):
            return {"id": 1}

        async def delete_webhook(self, *, drop_pending_updates: bool):
            delete_started.set()
            while not release_delete.is_set():
                await asyncio.sleep(0.005)
            return True

    class Registry:
        def register(self, *_args, **_kwargs) -> None:
            return None

    class BootstrapDispatcher:
        def __init__(self):
            self.message = Registry()
            self.callback_query = Registry()

        async def start_polling(self, *_args, **_kwargs) -> None:
            polling_started.set()

        async def stop_polling(self) -> None:
            raise RuntimeError("polling has not started")

    runner = telegram_bot.AiogramBotRunner()
    start_result: list[bool] = []
    with (
        patch.object(telegram_bot.notifier, "_ensure_token", return_value="valid-token"),
        patch.object(telegram_bot, "Bot", SlowBootstrapBot),
        patch.object(telegram_bot, "Dispatcher", BootstrapDispatcher),
    ):
        starter = threading.Thread(target=lambda: start_result.append(runner.start(timeout=2)), daemon=True)
        starter.start()
        assert delete_started.wait(timeout=1)
        assert runner.stop(timeout=0.05) is False
        release_delete.set()
        starter.join(timeout=2)
        assert not starter.is_alive()
        assert start_result == [False]
        assert not polling_started.is_set()


def test_reconfigure_invalid_token_restores_previous_runtime_and_config() -> None:
    """Exercise the real runner handshake, not a mocked start return value."""
    from services import telegram_bot

    persisted = {"token": "old-encrypted"}
    polling_tokens: list[str] = []

    class Session:
        async def close(self) -> None:
            return None

    class ValidatingBot:
        def __init__(self, *, token: str):
            self.token = token
            self.session = Session()

        async def get_me(self):
            if self.token == "new-invalid-token":
                raise RuntimeError("Telegram rejected token")
            return {"id": 1}

        async def delete_webhook(self, *, drop_pending_updates: bool):
            return True

    class Registry:
        def register(self, *_args, **_kwargs) -> None:
            return None

    class PollingDispatcher:
        def __init__(self):
            self.message = Registry()
            self.callback_query = Registry()
            self._stopped = asyncio.Event()

        async def start_polling(self, bot, **_kwargs) -> None:
            polling_tokens.append(bot.token)
            await self._stopped.wait()

        async def stop_polling(self) -> None:
            self._stopped.set()

    def get_setting(key: str):
        assert key == "telegram_bot_token_encrypted"
        return persisted["token"]

    def set_setting(key: str, value: str | None) -> bool:
        assert key == "telegram_bot_token_encrypted"
        persisted["token"] = value
        return True

    def decrypt(value: str) -> str:
        return {
            "old-encrypted": "old-valid-token",
            "new-encrypted": "new-invalid-token",
        }[value]

    telegram_bot.notifier.invalidate_token()
    with (
        patch("config.TESTING", False),
        patch.object(telegram_bot, "Bot", ValidatingBot),
        patch.object(telegram_bot, "Dispatcher", PollingDispatcher),
        patch.object(telegram_bot, "decrypt_secret", side_effect=decrypt),
        patch.object(telegram_bot.db, "get_setting_value", side_effect=get_setting),
        patch.object(telegram_bot.db, "set_setting_value", side_effect=set_setting),
        patch.object(telegram_bot, "_aiogram_runner", None),
        patch.object(telegram_bot, "_http_poller", None),
    ):
        assert telegram_bot._start_runtime_locked() is True
        deadline = time.monotonic() + 1
        while polling_tokens != ["old-valid-token"] and time.monotonic() < deadline:
            time.sleep(0.005)
        assert polling_tokens == ["old-valid-token"]

        assert telegram_bot.reconfigure_bot_token("new-encrypted") is False
        assert persisted["token"] == "old-encrypted"
        assert telegram_bot.notifier._token == "old-valid-token"

        deadline = time.monotonic() + 1
        while polling_tokens != ["old-valid-token", "old-valid-token"] and time.monotonic() < deadline:
            time.sleep(0.005)
        assert polling_tokens == ["old-valid-token", "old-valid-token"]
        assert telegram_bot._aiogram_runner is not None
        assert telegram_bot._aiogram_runner._thread is not None
        assert telegram_bot._aiogram_runner._thread.is_alive()
        assert telegram_bot._stop_runtime_locked() is True

    telegram_bot.notifier.invalidate_token()


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


def test_reconfigure_token_rolls_back_when_runtime_start_raises() -> None:
    from services import telegram_bot

    telegram_bot.notifier._token = "old-plaintext"
    starts = 0

    def start_runtime() -> bool:
        nonlocal starts
        starts += 1
        if starts == 1:
            raise RuntimeError("thread start rejected")
        telegram_bot.notifier._token = "old-plaintext"
        return True

    with (
        patch.object(telegram_bot.db, "get_setting_value", return_value="old-encrypted"),
        patch.object(telegram_bot, "_stop_runtime_locked", return_value=True),
        patch.object(telegram_bot.db, "set_setting_value", return_value=True) as persist,
        patch.object(telegram_bot, "_start_runtime_locked", side_effect=start_runtime),
    ):
        assert telegram_bot.reconfigure_bot_token("new-encrypted") is False

    assert persist.call_args_list == [
        (("telegram_bot_token_encrypted", "new-encrypted"),),
        (("telegram_bot_token_encrypted", "old-encrypted"),),
    ]
    assert starts == 2
    assert telegram_bot.notifier._token == "old-plaintext"
    telegram_bot.notifier.invalidate_token()


def test_permissive_umask_import_does_not_install_telegram_file_handler() -> None:
    """Telegram logging must inherit hardened root handlers, never create 0644 PII logs."""
    script = """
import logging
import os

os.umask(0)

def forbidden_file_handler(*args, **kwargs):
    raise AssertionError("dedicated Telegram FileHandler is forbidden")

logging.FileHandler = forbidden_file_handler
import services.telegram_bot as telegram_bot
assert telegram_bot.logger.handlers == []
"""
    env = dict(os.environ)
    env["TESTING"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_outgoing_http_logs_never_include_message_or_response_body(caplog) -> None:
    from services import telegram_bot

    class Response:
        ok = True
        status_code = 200
        text = "SECRET TELEGRAM RESPONSE BODY"

        @staticmethod
        def json() -> dict:
            return {"ok": True}

    notifier = telegram_bot.TelegramNotifier()
    with (
        patch("config.TESTING", False),
        patch.object(telegram_bot, "Bot", None),
        patch.object(notifier, "_ensure_token", return_value="123456:SECRET_TOKEN"),
        patch.object(telegram_bot.requests, "post", return_value=Response()),
        caplog.at_level("INFO", logger="TELEGRAM"),
    ):
        assert notifier.send_text(12345, "TOP SECRET CUSTOMER MESSAGE") is True

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "TOP SECRET CUSTOMER MESSAGE" not in logged
    assert "SECRET TELEGRAM RESPONSE BODY" not in logged
    assert "SECRET_TOKEN" not in logged
    assert "chat_id=12345" in logged


def test_send_text_transport_failure_never_logs_token_url(caplog) -> None:
    from services import telegram_bot

    token = "123456:SENTINEL_TRANSPORT_TOKEN"
    failure = telegram_bot.requests.exceptions.ConnectionError(
        f"request failed for https://api.telegram.org/bot{token}/sendMessage"
    )
    notifier = telegram_bot.TelegramNotifier()
    with (
        patch("config.TESTING", False),
        patch.object(telegram_bot, "Bot", None),
        patch.object(notifier, "_ensure_token", return_value=token),
        patch.object(telegram_bot.requests, "post", side_effect=failure),
        caplog.at_level("WARNING", logger="TELEGRAM"),
    ):
        assert notifier.send_text(12345, "safe test") is False

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in logged
    assert "SENTINEL_TRANSPORT_TOKEN" not in logged
    assert "/bot123456" not in logged
    assert "TelegramNotifier send_text transport failed" in logged


def test_send_text_does_not_swallow_unexpected_programmer_failure() -> None:
    from services import telegram_bot

    notifier = telegram_bot.TelegramNotifier()
    with (
        patch("config.TESTING", False),
        patch.object(telegram_bot, "Bot", None),
        patch.object(notifier, "_ensure_token", return_value="123456:TOKEN"),
        patch.object(telegram_bot.requests, "post", side_effect=AssertionError("programmer bug")),
        pytest.raises(AssertionError, match="programmer bug"),
    ):
        notifier.send_text(12345, "safe test")
