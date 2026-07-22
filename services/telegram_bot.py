import asyncio
import concurrent.futures
import importlib.util
import logging
import os
import sqlite3
import threading
import time

import requests

from database import db
from utils import decrypt_secret

BASE_DIR = os.path.abspath(os.path.dirname(__file__))  # .../irrigation/services

# --- надёжная загрузка локального routes-модуля по пути файла ---
_routes_mod = None


def _load_routes_module():
    """
    Грузим services/telegram.py независимо от sys.path и
    одноимённых внешних пакетов. Кэшируем модуль.
    """
    global _routes_mod
    if _routes_mod:
        return _routes_mod
    # Загружаем модуль маршрутов бота из routes/telegram.py
    path = os.path.join(os.path.dirname(BASE_DIR), "routes", "telegram.py")
    if not os.path.exists(path):
        raise FileNotFoundError(f"routes file not found: {path}")
    spec = importlib.util.spec_from_file_location("wb_routes_telegram", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # выполняем файл
    _routes_mod = mod
    # попозже, после создания notifier, мы дернём set_notifier()
    return _routes_mod


# ----------------------------------------------------------------

logger = logging.getLogger("TELEGRAM")

_TELEGRAM_TRANSPORT_ERRORS = (
    requests.exceptions.RequestException,
    ConnectionError,
    TimeoutError,
    OSError,
)

try:
    # aiogram v3
    from aiogram import Bot, Dispatcher, F
    from aiogram.types import CallbackQuery, Message
    from aiogram.types import InlineKeyboardButton as _AInlineKeyboardButton
    from aiogram.types import InlineKeyboardMarkup as _AInlineKeyboardMarkup
except ImportError as e:
    logger.debug("Exception in _load_routes_module: %s", e)
    Bot = None
    Dispatcher = None
    F = None
    Message = None
    CallbackQuery = None
    _AInlineKeyboardMarkup = None
    _AInlineKeyboardButton = None

# Telegram inherits the application's hardened root handlers.  A dedicated
# handler previously created services/logs/telegram.txt under the process umask
# and duplicated unfiltered PII outside the protected application log.


def _redact_url(url: str) -> str:
    try:
        if "/bot" in url:
            a, b = url.split("/bot", 1)
            if "/" in b:
                return a + "/bot***" + "/" + b.split("/", 1)[1]
            return a + "/bot***"
        return url
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Exception in _redact_url: %s", e)
        return url


class TelegramNotifier:
    def __init__(self):
        self._token: str | None = None
        self._lock = threading.RLock()

    def _ensure_token(self) -> str | None:
        try:
            with self._lock:
                if self._token:
                    return self._token
                tok_enc = db.get_setting_value("telegram_bot_token_encrypted")
                if not tok_enc:
                    logger.error("TelegramNotifier: no encrypted token in DB (telegram_bot_token_encrypted)")
                    return None
                token = decrypt_secret(tok_enc)
                if not token:
                    logger.error("TelegramNotifier: decrypt_secret returned empty token")
                    return None
                self._token = token
                return self._token
        except (sqlite3.Error, OSError, ValueError, TypeError) as e:
            logger.error(f"TelegramNotifier ensure_token failed: {e}")
            return None

    def invalidate_token(self) -> None:
        """Forget cached plaintext after any token configuration transition."""
        with self._lock:
            self._token = None

    @staticmethod
    def _close_coroutine(coro) -> None:
        close = getattr(coro, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _log_scheduled_result(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except (asyncio.CancelledError, RuntimeError):
            return
        if exc is not None:
            if isinstance(exc, _TELEGRAM_TRANSPORT_ERRORS):
                logger.warning("scheduled aiogram coroutine transport failed")
            else:
                logger.error("scheduled aiogram coroutine failed: %s", exc)

    def _submit_aiogram(self, coro) -> bool:
        """Submit from sync callers without ever blocking the target loop.

        Flask, scheduler and monitor threads need synchronous delivery feedback,
        so they wait on ``run_coroutine_threadsafe``.  Aiogram handlers already
        execute on the target loop; waiting there would deadlock for ten seconds.
        In that case we enqueue the coroutine and return accepted-for-delivery.
        """
        try:
            global _aiogram_runner
            runner = _aiogram_runner
            loop = getattr(runner, "_loop", None) if runner else None
            if not runner or not getattr(runner, "_bot", None) or loop is None or loop.is_closed():
                self._close_coroutine(coro)
                return False

            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop is loop:
                task = loop.create_task(coro)
                task.add_done_callback(self._log_scheduled_result)
                return True

            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                res = fut.result(timeout=10)
                return bool(res)
            except _TELEGRAM_TRANSPORT_ERRORS:
                logger.warning("aiogram coroutine transport failed")
                fut.cancel()
            except RuntimeError:
                logger.exception("aiogram coroutine runtime failed")
                fut.cancel()
        except (RuntimeError, OSError) as e:
            logger.debug("Handled exception in _submit_aiogram: %s", e)
            self._close_coroutine(coro)
        return False

    def send_text(self, chat_id: int, text: str) -> bool:
        try:
            # Skip in TESTING mode
            from config import TESTING

            if TESTING:
                logger.debug(f"TESTING mode: skipping send_text to {chat_id}")
                return True

            logger.info(f"send_text chat_id={int(chat_id)} len={len(str(text) or '')}")
            # Prefer aiogram if running
            if Bot is not None:
                try:
                    global _aiogram_runner
                    bot = getattr(_aiogram_runner, "_bot", None) if "_aiogram_runner" in globals() else None
                    if bot is not None:
                        return self._submit_aiogram(bot.send_message(chat_id=int(chat_id), text=str(text)))
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Handled exception in send_text: %s", e)
            token = self._ensure_token()
            if not token:
                return False
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {"chat_id": int(chat_id), "text": str(text)}
            logger.info(
                "http POST %s chat_id=%s text_len=%s",
                _redact_url(url),
                int(chat_id),
                len(str(text)),
            )
            resp = requests.post(url, json=payload, timeout=10)
            logger.info("http RESP status=%s", resp.status_code)
            data = resp.json() if resp.ok else {}
            return bool(data.get("ok"))
        except _TELEGRAM_TRANSPORT_ERRORS:
            logger.warning("TelegramNotifier send_text transport failed")
            return False
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"TelegramNotifier send_text failed: {e}")
            return False

    def send_message(self, chat_id: int, text: str, reply_markup=None) -> bool:
        try:
            # Skip in TESTING mode
            from config import TESTING

            if TESTING:
                logger.debug(f"TESTING mode: skipping send_message to {chat_id}")
                return True

            logger.info(f"send_message chat_id={int(chat_id)} len={len(str(text) or '')} has_kb={bool(reply_markup)}")
            if Bot is not None:
                try:
                    global _aiogram_runner
                    bot = getattr(_aiogram_runner, "_bot", None) if "_aiogram_runner" in globals() else None
                    if bot is not None:
                        rk = None
                        if reply_markup and _AInlineKeyboardMarkup and _AInlineKeyboardButton:
                            try:
                                rows = reply_markup.get("inline_keyboard") or []
                                kb = [[_AInlineKeyboardButton(**btn) for btn in row] for row in rows]
                                rk = _AInlineKeyboardMarkup(inline_keyboard=kb)
                            except (KeyError, TypeError, ValueError) as e:
                                logger.exception(f"kb build failed: {e}")
                                rk = None
                        return self._submit_aiogram(
                            bot.send_message(chat_id=int(chat_id), text=str(text), reply_markup=rk)
                        )
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Handled exception in send_message: %s", e)
            token = self._ensure_token()
            if not token:
                return False
            payload = {"chat_id": int(chat_id), "text": str(text)}
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            logger.info(f"http POST {_redact_url(url)} payload_keys={list(payload.keys())}")
            resp = requests.post(url, json=payload, timeout=10)
            logger.info("http RESP status=%s", resp.status_code)
            data = resp.json() if resp.ok else {}
            return bool(data.get("ok"))
        except _TELEGRAM_TRANSPORT_ERRORS:
            logger.warning("TelegramNotifier send_message transport failed")
            return False
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"TelegramNotifier send_message failed: {e}")
            return False

    def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup=None) -> bool:
        try:
            logger.info(
                f"edit_message_text chat_id={int(chat_id)} msg_id={int(message_id)} len={len(str(text) or '')} has_kb={bool(reply_markup)}"
            )
            if Bot is not None:
                try:
                    global _aiogram_runner
                    bot = getattr(_aiogram_runner, "_bot", None) if "_aiogram_runner" in globals() else None
                    if bot is not None:
                        rk = None
                        if reply_markup and _AInlineKeyboardMarkup and _AInlineKeyboardButton:
                            try:
                                rows = reply_markup.get("inline_keyboard") or []
                                kb = [[_AInlineKeyboardButton(**btn) for btn in row] for row in rows]
                                rk = _AInlineKeyboardMarkup(inline_keyboard=kb)
                            except (KeyError, TypeError, ValueError) as e:
                                logger.exception(f"kb build failed: {e}")
                                rk = None
                        return self._submit_aiogram(
                            bot.edit_message_text(
                                chat_id=int(chat_id), message_id=int(message_id), text=str(text), reply_markup=rk
                            )
                        )
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Handled exception in edit_message_text: %s", e)
            token = self._ensure_token()
            if not token:
                return False
            payload = {"chat_id": int(chat_id), "message_id": int(message_id), "text": str(text)}
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            url = f"https://api.telegram.org/bot{token}/editMessageText"
            logger.info(f"http POST {_redact_url(url)} payload_keys={list(payload.keys())}")
            resp = requests.post(url, json=payload, timeout=10)
            logger.info("http RESP status=%s", resp.status_code)
            data = resp.json() if resp.ok else {}
            return bool(data.get("ok"))
        except _TELEGRAM_TRANSPORT_ERRORS:
            logger.warning("TelegramNotifier edit_message_text transport failed")
            return False
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"TelegramNotifier edit_message_text failed: {e}")
            return False

    def answer_callback(self, callback_query_id: str, text: str | None = None, show_alert: bool = False) -> None:
        try:
            if not callback_query_id:
                return
            if Bot is not None:
                try:
                    global _aiogram_runner
                    bot = getattr(_aiogram_runner, "_bot", None) if "_aiogram_runner" in globals() else None
                    if bot is not None:
                        if text is None:
                            coro = bot.answer_callback_query(callback_query_id=str(callback_query_id))
                        else:
                            coro = bot.answer_callback_query(
                                callback_query_id=str(callback_query_id), text=str(text), show_alert=bool(show_alert)
                            )
                        self._submit_aiogram(coro)
                        return
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Handled exception in answer_callback: %s", e)
            token = self._ensure_token()
            if not token:
                return
            url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
            payload = {"callback_query_id": str(callback_query_id)}
            if text is not None:
                payload["text"] = str(text)
                payload["show_alert"] = bool(show_alert)
            requests.post(url, json=payload, timeout=10)
        except _TELEGRAM_TRANSPORT_ERRORS:
            logger.warning("TelegramNotifier answer_callback transport failed")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"TelegramNotifier answer_callback failed: {e}")


def _valid_hhmm(value: str) -> bool:
    try:
        hours, minutes = str(value).split(":", 1)
        return len(hours) == 2 and len(minutes) == 2 and 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59
    except (ValueError, TypeError):
        return False


def _apply_subscription_command(chat_id: int, text: str) -> str | None:
    """Apply a subscription command, returning a response or ``None``.

    Both polling implementations call this function so command availability
    cannot drift between aiogram and the HTTP fallback.
    """
    parts = str(text or "").strip().split()
    if not parts:
        return None
    command = parts[0].split("@", 1)[0].lower()
    if command not in ("/subscribe", "/unsubscribe"):
        return None

    user = db.get_bot_user_by_chat(int(chat_id))
    if not user:
        return "Не удалось сохранить подписку: пользователь не зарегистрирован"
    user_id = int(user["id"])

    if command == "/unsubscribe":
        daily_ok = db.create_or_update_subscription(user_id, "daily", "brief", "08:00", None, False)
        weekly_ok = db.create_or_update_subscription(user_id, "weekly", "brief", "08:00", "1111111", False)
        return "Подписки отключены" if daily_ok and weekly_ok else "Не удалось отключить подписки"

    sub_type = parts[1].lower() if len(parts) > 1 else "daily"
    report_format = parts[2].lower() if len(parts) > 2 else "brief"
    time_local = parts[3] if len(parts) > 3 else "08:00"
    dow_mask = parts[4] if len(parts) > 4 and sub_type == "weekly" else None
    if sub_type not in ("daily", "weekly"):
        return "Тип подписки должен быть daily или weekly"
    if report_format not in ("brief", "full"):
        return "Формат подписки должен быть brief или full"
    if not _valid_hhmm(time_local):
        return "Время подписки должно быть в формате HH:MM"
    if sub_type == "weekly":
        dow_mask = dow_mask or "1111111"
        if len(dow_mask) != 7 or any(bit not in "01" for bit in dow_mask):
            return "Маска дней недели должна содержать 7 символов 0/1"

    saved = db.create_or_update_subscription(
        user_id,
        sub_type,
        report_format,
        time_local,
        dow_mask,
        True,
    )
    return "Подписка сохранена" if saved else "Не удалось сохранить подписку"


class AiogramBotRunner:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._running = False
        self._bot: Bot | None = None
        self._dp: Dispatcher | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_requested = threading.Event()
        self._startup_event = threading.Event()
        self._startup_ok = False
        self._startup_error: str | None = None

    def _signal_startup(self, ok: bool, error: str | None = None) -> None:
        if self._startup_event.is_set():
            return
        self._startup_ok = bool(ok)
        self._startup_error = error
        self._startup_event.set()

    def _is_authorized_chat(self, chat_id: int) -> bool:
        """SECURITY FIX (VULN-005): Check if chat_id is the admin chat."""
        try:
            admin_chat = db.get_setting_value("telegram_admin_chat_id")
            if not admin_chat:
                # No admin chat configured — deny all for safety
                logger.warning("telegram auth: no telegram_admin_chat_id configured, denying chat_id=%s", chat_id)
                return False
            return int(admin_chat) == int(chat_id)
        except (ValueError, TypeError, sqlite3.Error, OSError) as e:
            logger.error("telegram auth check failed: %s", e)
            return False

    async def _on_message(self, message: Message):
        try:
            chat = message.chat
            chat_id = int(chat.id)
            text = str(message.text or "").strip()
            username = getattr(chat, "username", None)
            first_name = getattr(chat, "first_name", None)
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in __init__: %s", e)
            return
        # SECURITY FIX (VULN-005): verify chat is authorized
        if not self._is_authorized_chat(chat_id):
            logger.warning("Unauthorized telegram message from chat_id=%s user=%s", chat_id, username)
            notifier.send_text(chat_id, "⛔ Доступ запрещён. Обратитесь к администратору.")
            return
        try:
            db.upsert_bot_user(int(chat_id), username, first_name)
            db.set_bot_user_authorized(int(chat_id), role="admin")
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in __init__: %s", e)

        command_response = _apply_subscription_command(chat_id, text)
        if command_response is not None:
            notifier.send_text(chat_id, command_response)
            return

        try:
            routes = _load_routes_module()
            if hasattr(routes, "set_notifier"):
                routes.set_notifier(notifier)
            kb = {"inline_keyboard": [[{"text": "Группы", "callback_data": "menu:groups"}]]}
            notifier.send_message(chat_id, "Главное меню:", kb)
        except (KeyError, TypeError, ValueError) as e:
            logger.debug("Exception in __init__: %s", e)
            notifier.send_text(chat_id, "Главное меню: нажмите «Группы»")
        return

    async def _on_callback(self, cq: CallbackQuery):
        # 1) ACK first to stop spinner
        try:
            notifier.answer_callback(str(cq.id))
            logger.info(
                f"ack callback id={cq.id} chat_id={getattr(cq.from_user, 'id', None)} data={(cq.data or '')[:120]}"
            )
        except (ValueError, TypeError, KeyError):
            logger.exception("callback ack failed")

        # SECURITY FIX (VULN-005): verify chat is authorized for callbacks
        try:
            from_chat = int(cq.message.chat.id if cq.message and cq.message.chat else cq.from_user.id)
            if not self._is_authorized_chat(from_chat):
                logger.warning("Unauthorized telegram callback from chat_id=%s", from_chat)
                return
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("telegram callback auth check failed: %s", e)
            return

        # 2) Load routes
        try:
            routes = _load_routes_module()
            if hasattr(routes, "set_notifier"):
                routes.set_notifier(notifier)
        except (ImportError, AttributeError):
            logger.exception("failed to load routes (telegram.py)")
            return

        # 3) Parse + route
        try:
            data = str(cq.data or "")
            from_chat = int(cq.message.chat.id if cq.message and cq.message.chat else cq.from_user.id)
            msg_id = int(cq.message.message_id) if cq.message else None
            jd = routes._cb_decode(data)
            logger.info(f"cb json chat_id={from_chat} data={jd}")
            if isinstance(jd, dict) and jd.get("t"):
                # Route actions are synchronous and may perform MQTT/SQLite
                # work.  Running them in a worker keeps the aiogram event loop
                # available for notifier coroutines submitted back to it.
                await asyncio.to_thread(routes.process_callback_json, int(from_chat), jd, message_id=msg_id)
                return
        except (ValueError, TypeError, KeyError):
            logger.exception("callback processing failed")
            return

    async def _main(self):
        bot = None
        polling_task = None
        try:
            token = notifier._ensure_token()
            if not token or Bot is None or Dispatcher is None:
                message = "Aiogram _main: missing token or aiogram is not available"
                logger.error(message)
                self._signal_startup(False, message)
                return
            logger.info("[telegram] Starting aiogram v3 polling runner")
            self._bot = Bot(token=token)
            bot = self._bot
            self._dp = Dispatcher()
            if self._stop_requested.is_set():
                self._signal_startup(False, "stop requested during bootstrap")
                return

            # Bot() only validates token syntax.  get_me performs an authenticated
            # request, so settings PUT cannot report success for a token that the
            # Telegram API rejects.  Both bootstrap calls are bounded because
            # reconfigure_bot_token synchronously depends on this handshake.
            await asyncio.wait_for(self._bot.get_me(), timeout=5.0)
            if self._stop_requested.is_set():
                self._signal_startup(False, "stop requested during bootstrap")
                return
            await asyncio.wait_for(self._bot.delete_webhook(drop_pending_updates=True), timeout=5.0)
            if self._stop_requested.is_set():
                self._signal_startup(False, "stop requested during bootstrap")
                return

            self._dp.message.register(self._on_message, F.text)
            self._dp.callback_query.register(self._on_callback)
            polling_task = asyncio.create_task(
                self._dp.start_polling(
                    self._bot,
                    allowed_updates=["message", "callback_query"],
                    handle_signals=False,
                    close_bot_session=False,
                )
            )
            if self._stop_requested.is_set():
                polling_task.cancel()
                await asyncio.gather(polling_task, return_exceptions=True)
                self._signal_startup(False, "stop requested during bootstrap")
                return

            # Give the real Dispatcher a bounded window to enter its steady
            # polling wait.  Immediate failures (bad thread setup, startup
            # observer errors, etc.) must be visible before settings persistence
            # is accepted by reconfigure_bot_token().
            done, _pending = await asyncio.wait({polling_task}, timeout=0.05)
            if polling_task in done:
                if polling_task.cancelled():
                    self._signal_startup(False, "polling cancelled during bootstrap")
                    return
                error = polling_task.exception()
                if error is not None:
                    raise error
                raise RuntimeError("polling exited during bootstrap")
            if self._stop_requested.is_set():
                polling_task.cancel()
                await asyncio.gather(polling_task, return_exceptions=True)
                self._signal_startup(False, "stop requested during bootstrap")
                return
            self._running = True
            self._signal_startup(True)
            await polling_task
        except Exception as e:  # Bootstrap failures must reach the synchronous caller.
            self._signal_startup(False, str(e))
            logger.error("Aiogram runner failed: %s", e)
        finally:
            if not self._startup_event.is_set():
                self._signal_startup(False, "aiogram runner exited during bootstrap")
            self._running = False
            if bot is not None:
                try:
                    await bot.session.close()
                except (ConnectionError, TimeoutError, OSError, RuntimeError) as e:
                    logger.debug("aiogram bot session close failed: %s", e)
            self._bot = None
            self._dp = None

    def _thread_target(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._main())
        except (ConnectionError, TimeoutError, OSError, RuntimeError) as e:  # catch-all: intentional
            self._signal_startup(False, str(e))
            logger.exception(f"Aiogram thread target error: {e}")
        finally:
            if not self._startup_event.is_set():
                self._signal_startup(False, "aiogram thread exited during bootstrap")
            loop = self._loop
            if loop is not None and not loop.is_closed():
                try:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except RuntimeError as e:
                    logger.debug("aiogram loop cleanup failed: %s", e)
                loop.close()
            self._loop = None
            self._running = False

    def start(self, timeout: float = 12.0) -> bool:
        if self._thread and self._thread.is_alive():
            return self._startup_event.is_set() and self._startup_ok
        self._stop_requested.clear()
        self._startup_event.clear()
        self._startup_ok = False
        self._startup_error = None
        self._thread = threading.Thread(target=self._thread_target, daemon=True)
        self._thread.start()
        if not self._startup_event.wait(timeout=max(0.0, timeout)):
            self._stop_requested.set()
            self._startup_error = "aiogram bootstrap timed out"
            logger.error(self._startup_error)
            return False
        if not self._startup_ok:
            self._thread.join(timeout=1.0)
            return False
        if not self._thread.is_alive():
            self._startup_error = "aiogram runner exited immediately after bootstrap"
            return False
        return True

    def stop(self, timeout: float = 10.0) -> bool:
        self._stop_requested.set()
        thread = self._thread
        if thread is None or not thread.is_alive():
            return True

        loop = self._loop
        dispatcher = self._dp
        if loop is not None and dispatcher is not None and loop.is_running():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is loop:
                task = loop.create_task(dispatcher.stop_polling())
                task.add_done_callback(TelegramNotifier._log_scheduled_result)
                # The current handler cannot join its own polling thread.  The
                # caller must retry configuration from a non-aiogram thread.
                return False
            try:
                future = asyncio.run_coroutine_threadsafe(dispatcher.stop_polling(), loop)
                future.result(timeout=min(5.0, timeout))
            except (concurrent.futures.CancelledError, ConnectionError, TimeoutError, OSError, RuntimeError) as e:
                logger.debug("aiogram stop_polling failed: %s", e)
        if thread is threading.current_thread():
            return False
        thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()


class SimpleHTTPPoller:
    def __init__(self):
        self._thr: threading.Thread | None = None
        self._running = False
        self._offset = None
        self._stop_requested = threading.Event()

    def _run(self):
        try:
            token = notifier._ensure_token()
            if not token:
                logger.error("HTTP poller: no token, abort")
                return
            try:
                logger.info("[telegram] HTTP poller: deleting webhook (fallback mode)")
                requests.post(f"https://api.telegram.org/bot{token}/deleteWebhook", timeout=10)
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Handled exception in _run: %s", e)
            if self._stop_requested.is_set():
                return
            logger.info("[telegram] Starting legacy HTTP polling fallback")
            self._running = True

            routes = _load_routes_module()
            if hasattr(routes, "set_notifier"):
                routes.set_notifier(notifier)

            while self._running and not self._stop_requested.is_set():
                try:
                    # A short long-poll keeps token replacement bounded.  The
                    # previous 50/60-second pair made it impossible to stop the
                    # old-token runner atomically from the settings request.
                    params = {"timeout": 5}
                    if self._offset is not None:
                        params["offset"] = int(self._offset)
                    resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=10)
                    if not self._running or self._stop_requested.is_set():
                        break
                    data = resp.json() if resp.ok else {}
                    for u in data.get("result") or []:
                        try:
                            self._offset = int(u.get("update_id", 0)) + 1
                        except (ValueError, TypeError, KeyError) as e:
                            logger.debug("Handled exception in _run: %s", e)

                        cq = u.get("callback_query") or {}
                        if cq:
                            try:
                                cqid = cq.get("id")
                                if cqid:
                                    try:
                                        notifier.answer_callback(cqid)
                                    except (ConnectionError, TimeoutError, OSError, ValueError) as e:
                                        logger.debug("Handled exception in line_428: %s", e)
                                from_chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
                                # SECURITY FIX (VULN-005): auth check in HTTP poller
                                if from_chat and not _is_authorized_chat_id(int(from_chat)):
                                    logger.warning(
                                        "Unauthorized telegram callback from chat_id=%s (HTTP poller)", from_chat
                                    )
                                    continue
                                msg_id = (cq.get("message") or {}).get("message_id")
                                data_cb = cq.get("data") or ""
                                if from_chat and data_cb:
                                    try:
                                        jd2 = routes._cb_decode(str(data_cb))
                                        if isinstance(jd2, dict) and jd2.get("t"):
                                            routes.process_callback_json(
                                                int(from_chat),
                                                jd2,
                                                message_id=int(msg_id) if msg_id is not None else None,
                                            )
                                            continue
                                    except (ValueError, TypeError, KeyError) as e:
                                        logger.debug("Handled exception in line_439: %s", e)
                            except (ValueError, TypeError, KeyError) as e:
                                logger.debug("Handled exception in line_441: %s", e)

                        msg = u.get("message") or {}
                        chat = msg.get("chat") or {}
                        text = (msg.get("text") or "").strip()
                        cid = chat.get("id")

                        if cid and text:
                            # SECURITY FIX (VULN-005): auth check in HTTP poller
                            if not _is_authorized_chat_id(int(cid)):
                                logger.warning("Unauthorized telegram message from chat_id=%s (HTTP poller)", cid)
                                try:
                                    notifier.send_text(int(cid), "⛔ Доступ запрещён. Обратитесь к администратору.")
                                except (ValueError, TypeError, KeyError) as e:
                                    logger.debug("Handled exception sending deny msg: %s", e)
                                continue
                            try:
                                username = chat.get("username")
                                first_name = chat.get("first_name")
                                db.upsert_bot_user(int(cid), username, first_name)
                                db.set_bot_user_authorized(int(cid), role="admin")
                                command_response = _apply_subscription_command(int(cid), text)
                                if command_response is not None:
                                    notifier.send_text(int(cid), command_response)
                                    continue
                                routes = _load_routes_module()
                                if hasattr(routes, "set_notifier"):
                                    routes.set_notifier(notifier)
                                kb = {"inline_keyboard": [[{"text": "Группы", "callback_data": "menu:groups"}]]}
                                notifier.send_message(int(cid), "Главное меню:", kb)
                            except (sqlite3.Error, OSError) as e:
                                logger.debug("Exception in line_459: %s", e)
                                try:
                                    notifier.send_text(int(cid), "Главное меню: нажмите «Группы»")
                                except (ValueError, TypeError, KeyError) as e:
                                    logger.debug("Handled exception in line_463: %s", e)
                            continue

                except (sqlite3.Error, OSError) as e:
                    logger.debug("Exception in line_467: %s", e)
                    time.sleep(2)
                    continue
        except (sqlite3.Error, OSError) as e:
            logger.error(f"HTTP poller failed: {e}")

    def start(self) -> bool:
        if self._thr and self._thr.is_alive():
            return True
        self._stop_requested.clear()
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()
        return True

    def stop(self, timeout: float = 11.0) -> bool:
        self._stop_requested.set()
        self._running = False
        thread = self._thr
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()


_poller = None
_aiogram_runner: AiogramBotRunner | None = None
_http_poller: SimpleHTTPPoller | None = None
_runtime_lock = threading.RLock()

notifier = TelegramNotifier()


def _is_authorized_chat_id(chat_id: int) -> bool:
    """SECURITY FIX (VULN-005): Module-level auth check for HTTP poller."""
    try:
        admin_chat = db.get_setting_value("telegram_admin_chat_id")
        if not admin_chat:
            logger.warning("telegram auth: no telegram_admin_chat_id configured, denying chat_id=%s", chat_id)
            return False
        return int(admin_chat) == int(chat_id)
    except (ValueError, TypeError, sqlite3.Error, OSError) as e:
        logger.error("telegram auth check failed: %s", e)
        return False


def _start_runtime_locked() -> bool:
    """Start exactly one polling implementation; caller holds runtime lock."""
    global _aiogram_runner, _http_poller
    try:
        from config import TESTING

        if TESTING:
            return True
    except ImportError:
        pass
    if not notifier._ensure_token():
        return False
    if Bot is not None and Dispatcher is not None:
        if _aiogram_runner is None:
            _aiogram_runner = AiogramBotRunner()
        return _aiogram_runner.start()
    if _http_poller is None:
        _http_poller = SimpleHTTPPoller()
    return _http_poller.start()


def _start_runtime_safely_locked() -> bool:
    """Normalize both bootstrap rejection and Thread.start exceptions."""
    try:
        return bool(_start_runtime_locked())
    except (OSError, RuntimeError, TypeError, ValueError) as e:
        logger.error("Telegram polling runtime start failed: %s", e)
        return False


def _stop_runtime_locked() -> bool:
    """Stop current pollers before a token transition; caller holds lock."""
    global _aiogram_runner, _http_poller
    stopped = True
    if _aiogram_runner is not None:
        aiogram_stopped = _aiogram_runner.stop()
        stopped = aiogram_stopped and stopped
        if aiogram_stopped:
            _aiogram_runner = None
    if _http_poller is not None:
        http_stopped = _http_poller.stop()
        stopped = http_stopped and stopped
        if http_stopped:
            _http_poller = None
    return stopped


def reconfigure_bot_token(token_encrypted: str | None) -> bool:
    """Atomically replace the persisted token and its polling runtime.

    If the old runner cannot stop, persistence is untouched.  If the new
    runtime cannot start, both the DB value and old runtime are restored.
    """
    with _runtime_lock:
        try:
            old_encrypted = db.get_setting_value("telegram_bot_token_encrypted")
            if not _stop_runtime_locked():
                logger.error("Telegram token change refused: old polling runtime did not stop")
                return False

            # Prevent a concurrent scheduler/monitor notification from loading
            # the old DB value back into the plaintext cache between invalidation
            # and persistence.  Runtime bootstrap itself happens after releasing
            # this lock because its worker thread must call _ensure_token().
            with notifier._lock:
                notifier.invalidate_token()
                if not db.set_setting_value("telegram_bot_token_encrypted", token_encrypted):
                    notifier.invalidate_token()
                    persisted = False
                else:
                    persisted = True

            if not persisted:
                if old_encrypted:
                    _start_runtime_safely_locked()
                return False
            if not token_encrypted:
                return True
            if _start_runtime_safely_locked():
                return True

            logger.error("Telegram token change rolled back: new polling runtime did not start")
            try:
                _stop_runtime_locked()
            except (OSError, RuntimeError, TypeError, ValueError) as e:
                logger.error("Failed to stop rejected Telegram runtime: %s", e)
            with notifier._lock:
                restored = db.set_setting_value("telegram_bot_token_encrypted", old_encrypted)
                notifier.invalidate_token()
            if old_encrypted:
                restored = _start_runtime_safely_locked() and restored
            if not restored:
                logger.critical("Telegram token rollback failed; polling remains stopped")
            return False
        except (sqlite3.Error, OSError, RuntimeError) as e:
            logger.error("Telegram token reconfiguration failed: %s", e)
            return False


def start_long_polling_if_needed():
    try:
        # Skip in TESTING mode
        from config import TESTING

        if TESTING:
            logger.debug("TESTING mode: skipping telegram long polling")
            return
        with _runtime_lock:
            if _start_runtime_safely_locked():
                logger.info("telegram polling runtime started")
            else:
                logger.error("telegram polling runtime failed to start")
    except (ValueError, TypeError, KeyError, OSError) as e:
        logger.exception(f"start_long_polling_if_needed error: {e}")


def subscribe_to_events():
    try:
        from services import events as evt
    except ImportError as e:
        logger.debug("Exception in subscribe_to_events: %s", e)
        return

    def _on_event(ev: dict):
        try:
            admin_chat = db.get_setting_value("telegram_admin_chat_id")
            if not admin_chat:
                return
            t = str(ev.get("type") or "")
            if t in ("emergency_on", "emergency_off", "critical_error", "error"):
                if t == "emergency_on":
                    txt = f"🚨 Аварийная остановка инициирована ({ev.get('by', '')})"
                elif t == "emergency_off":
                    txt = f"✅ Аварийная остановка снята ({ev.get('by', '')})"
                else:
                    code = ev.get("code") or ev.get("name") or "error"
                    msg = ev.get("message") or ""
                    txt = f"❗️Критическая ошибка: {code}\n{msg}".strip()
                try:
                    notifier.send_text(int(admin_chat), txt)
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Handled exception in _on_event: %s", e)
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in _on_event: %s", e)

    try:
        evt.subscribe(_on_event)
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.debug("Handled exception in _on_event: %s", e)
