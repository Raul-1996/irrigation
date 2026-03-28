# -*- coding: utf-8 -*-
from typing import Optional
import logging
import threading
from utils import decrypt_secret
from database import db
from werkzeug.security import check_password_hash
from datetime import datetime, timedelta
import time
import requests
import asyncio
import os
import sys
import importlib.util

BASE_DIR = os.path.abspath(os.path.dirname(__file__))  # .../irrigation/services
LOGS_DIR = os.path.join(BASE_DIR, 'logs')

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
    path = os.path.join(os.path.dirname(BASE_DIR), 'routes', 'telegram.py')
    if not os.path.exists(path):
        raise FileNotFoundError(f"routes file not found: {path}")
    spec = importlib.util.spec_from_file_location('wb_routes_telegram', path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # выполняем файл
    _routes_mod = mod
    # попозже, после создания notifier, мы дернём set_notifier()
    return _routes_mod
# ----------------------------------------------------------------

try:
    # aiogram v3
    from aiogram import Bot, Dispatcher, F
    from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup as _AInlineKeyboardMarkup, InlineKeyboardButton as _AInlineKeyboardButton
except Exception as e:
    logger.debug("Exception in _load_routes_module: %s", e)
    Bot = None
    Dispatcher = None
    F = None
    Message = None
    CallbackQuery = None
    _AInlineKeyboardMarkup = None
    _AInlineKeyboardButton = None

logger = logging.getLogger('TELEGRAM')
try:
    if not getattr(logger, '_telegram_configured', False):
        os.makedirs(LOGS_DIR, exist_ok=True)
        log_path = os.path.join(LOGS_DIR, 'telegram.txt')
        fh = logging.FileHandler(log_path, encoding='utf-8')
        fmt = logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        # консоль
        if not any(isinstance(h, logging.StreamHandler) for h in getattr(logger, 'handlers', [])):
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            logger.addHandler(sh)
        logger.setLevel(logging.INFO)
        logger.propagate = True
        logger._telegram_configured = True  # type: ignore[attr-defined]
        logger.info(f"telegram service logger initialized -> {log_path}")
except Exception as e:
    logger.debug("Handled exception in line_74: %s", e)


def _redact_url(url: str) -> str:
    try:
        if '/bot' in url:
            a, b = url.split('/bot', 1)
            if '/' in b:
                return a + '/bot***' + '/' + b.split('/', 1)[1]
            return a + '/bot***'
        return url
    except Exception as e:
        logger.debug("Exception in _redact_url: %s", e)
        return url


class TelegramNotifier:
    def __init__(self):
        self._token: Optional[str] = None
        self._lock = threading.Lock()

    def _ensure_token(self) -> Optional[str]:
        try:
            if self._token:
                return self._token
            tok_enc = db.get_setting_value('telegram_bot_token_encrypted')
            if not tok_enc:
                logger.error("TelegramNotifier: no encrypted token in DB (telegram_bot_token_encrypted)")
                return None
            token = decrypt_secret(tok_enc)
            if not token:
                logger.error("TelegramNotifier: decrypt_secret returned empty token")
                return None
            self._token = token
            return self._token
        except Exception as e:
            logger.error(f"TelegramNotifier ensure_token failed: {e}")
            return None

    def _submit_aiogram(self, coro) -> bool:
        try:
            global _aiogram_runner
            if _aiogram_runner and getattr(_aiogram_runner, '_bot', None) and getattr(_aiogram_runner, '_loop', None):
                fut = asyncio.run_coroutine_threadsafe(coro, _aiogram_runner._loop)
                try:
                    res = fut.result(timeout=10)
                    return bool(res)
                except Exception as e:
                    logger.exception(f"aiogram coroutine failed: {e}")
                    fut.cancel()
        except Exception as e:
            logger.debug("Handled exception in _submit_aiogram: %s", e)
        return False

    def send_text(self, chat_id: int, text: str) -> bool:
        try:
            logger.info(f"send_text chat_id={int(chat_id)} len={len(str(text) or '')}")
            # Prefer aiogram if running
            if Bot is not None:
                try:
                    global _aiogram_runner
                    bot = getattr(_aiogram_runner, '_bot', None) if '_aiogram_runner' in globals() else None
                    if bot is not None:
                        return self._submit_aiogram(bot.send_message(chat_id=int(chat_id), text=str(text)))
                except Exception as e:
                    logger.debug("Handled exception in send_text: %s", e)
            token = self._ensure_token()
            if not token:
                return False
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {'chat_id': int(chat_id), 'text': str(text)}
            logger.info(f"http POST {_redact_url(url)} payload={payload}")
            resp = requests.post(url, json=payload, timeout=10)
            logger.info(f"http RESP status={resp.status_code} body={resp.text[:200]}")
            data = resp.json() if resp.ok else {}
            return bool(data.get('ok'))
        except Exception as e:
            logger.error(f"TelegramNotifier send_text failed: {e}")
            return False

    def send_message(self, chat_id: int, text: str, reply_markup=None) -> bool:
        try:
            logger.info(f"send_message chat_id={int(chat_id)} len={len(str(text) or '')} has_kb={bool(reply_markup)}")
            if Bot is not None:
                try:
                    global _aiogram_runner
                    bot = getattr(_aiogram_runner, '_bot', None) if '_aiogram_runner' in globals() else None
                    if bot is not None:
                        rk = None
                        if reply_markup and _AInlineKeyboardMarkup and _AInlineKeyboardButton:
                            try:
                                rows = reply_markup.get('inline_keyboard') or []
                                kb = [[_AInlineKeyboardButton(**btn) for btn in row] for row in rows]
                                rk = _AInlineKeyboardMarkup(inline_keyboard=kb)
                            except Exception as e:
                                logger.exception(f"kb build failed: {e}")
                                rk = None
                        return self._submit_aiogram(bot.send_message(chat_id=int(chat_id), text=str(text), reply_markup=rk))
                except Exception as e:
                    logger.debug("Handled exception in send_message: %s", e)
            token = self._ensure_token()
            if not token:
                return False
            payload = {'chat_id': int(chat_id), 'text': str(text)}
            if reply_markup is not None:
                payload['reply_markup'] = reply_markup
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            logger.info(f"http POST {_redact_url(url)} payload_keys={list(payload.keys())}")
            resp = requests.post(url, json=payload, timeout=10)
            logger.info(f"http RESP status={resp.status_code} body={resp.text[:200]}")
            data = resp.json() if resp.ok else {}
            return bool(data.get('ok'))
        except Exception as e:
            logger.error(f"TelegramNotifier send_message failed: {e}")
            return False

    def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup=None) -> bool:
        try:
            logger.info(f"edit_message_text chat_id={int(chat_id)} msg_id={int(message_id)} len={len(str(text) or '')} has_kb={bool(reply_markup)}")
            if Bot is not None:
                try:
                    global _aiogram_runner
                    bot = getattr(_aiogram_runner, '_bot', None) if '_aiogram_runner' in globals() else None
                    if bot is not None:
                        rk = None
                        if reply_markup and _AInlineKeyboardMarkup and _AInlineKeyboardButton:
                            try:
                                rows = reply_markup.get('inline_keyboard') or []
                                kb = [[_AInlineKeyboardButton(**btn) for btn in row] for row in rows]
                                rk = _AInlineKeyboardMarkup(inline_keyboard=kb)
                            except Exception as e:
                                logger.exception(f"kb build failed: {e}")
                                rk = None
                        return self._submit_aiogram(
                            bot.edit_message_text(chat_id=int(chat_id), message_id=int(message_id), text=str(text), reply_markup=rk)
                        )
                except Exception as e:
                    logger.debug("Handled exception in edit_message_text: %s", e)
            token = self._ensure_token()
            if not token:
                return False
            payload = {'chat_id': int(chat_id), 'message_id': int(message_id), 'text': str(text)}
            if reply_markup is not None:
                payload['reply_markup'] = reply_markup
            url = f"https://api.telegram.org/bot{token}/editMessageText"
            logger.info(f"http POST {_redact_url(url)} payload_keys={list(payload.keys())}")
            resp = requests.post(url, json=payload, timeout=10)
            logger.info(f"http RESP status={resp.status_code} body={resp.text[:200]}")
            data = resp.json() if resp.ok else {}
            return bool(data.get('ok'))
        except Exception as e:
            logger.error(f"TelegramNotifier edit_message_text failed: {e}")
            return False

    def answer_callback(self, callback_query_id: str, text: str = None, show_alert: bool = False) -> None:
        try:
            if not callback_query_id:
                return
            if Bot is not None:
                try:
                    global _aiogram_runner
                    bot = getattr(_aiogram_runner, '_bot', None) if '_aiogram_runner' in globals() else None
                    if bot is not None:
                        if text is None:
                            coro = bot.answer_callback_query(callback_query_id=str(callback_query_id))
                        else:
                            coro = bot.answer_callback_query(callback_query_id=str(callback_query_id), text=str(text), show_alert=bool(show_alert))
                        self._submit_aiogram(coro)
                        return
                except Exception as e:
                    logger.debug("Handled exception in answer_callback: %s", e)
            token = self._ensure_token()
            if not token:
                return
            url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
            payload = {'callback_query_id': str(callback_query_id)}
            if text is not None:
                payload['text'] = str(text)
                payload['show_alert'] = bool(show_alert)
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logger.error(f"TelegramNotifier answer_callback failed: {e}")


class AiogramBotRunner:
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._bot: Optional[Bot] = None
        self._dp: Optional[Dispatcher] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def _on_message(self, message: Message):
        try:
            chat = message.chat
            chat_id = int(chat.id)
            text = str(message.text or '').strip()
            username = getattr(chat, 'username', None)
            first_name = getattr(chat, 'first_name', None)
        except Exception as e:
            logger.debug("Exception in __init__: %s", e)
            return
        try:
            db.upsert_bot_user(int(chat_id), username, first_name)
        except Exception as e:
            logger.debug("Handled exception in __init__: %s", e)
        try:
            routes = _load_routes_module()
            if hasattr(routes, 'set_notifier'):
                routes.set_notifier(notifier)
            kb = {'inline_keyboard': [[{'text': 'Группы', 'callback_data': 'menu:groups'}]]}
            notifier.send_message(chat_id, 'Главное меню:', kb)
        except Exception as e:
            logger.debug("Exception in __init__: %s", e)
            notifier.send_text(chat_id, 'Главное меню: нажмите «Группы»')
        return

        if text.startswith('/subscribe'):
            try:
                parts = text.split()
                stype = parts[1] if len(parts)>1 else 'daily'
                sformat = parts[2] if len(parts)>2 else 'brief'
                time_local = parts[3] if len(parts)>3 else '08:00'
                dow = parts[4] if (len(parts)>4 and stype=='weekly') else None
            except Exception as e:
                logger.debug("Exception in line_299: %s", e)
                stype, sformat, time_local, dow = 'daily','brief','08:00',None
            u = db.get_bot_user_by_chat(int(chat_id))
            if u:
                db.create_or_update_subscription(int(u.get('id')), stype, sformat, time_local, dow, True)
                notifier.send_text(chat_id, 'Подписка сохранена')
            return

        if text.startswith('/unsubscribe'):
            u = db.get_bot_user_by_chat(int(chat_id))
            if u:
                try:
                    db.create_or_update_subscription(int(u.get('id')), 'daily', 'brief', '08:00', None, False)
                    db.create_or_update_subscription(int(u.get('id')), 'weekly', 'brief', '08:00', '1111111', False)
                except Exception as e:
                    logger.debug("Handled exception in line_314: %s", e)
            notifier.send_text(chat_id, 'Подписки отключены')
            return

    async def _on_callback(self, cq: CallbackQuery):
        # 1) ACK first to stop spinner
        try:
            notifier.answer_callback(str(cq.id))
            logger.info(f"ack callback id={cq.id} chat_id={getattr(cq.from_user,'id',None)} data={(cq.data or '')[:120]}")
        except Exception:
            logger.exception("callback ack failed")

        # 2) Load routes
        try:
            routes = _load_routes_module()
            if hasattr(routes, 'set_notifier'):
                routes.set_notifier(notifier)
        except Exception:
            logger.exception("failed to load routes (telegram.py)")
            return

        # 3) Parse + route
        try:
            data = str(cq.data or '')
            from_chat = int((cq.message.chat.id if cq.message and cq.message.chat else cq.from_user.id))
            msg_id = int(cq.message.message_id) if cq.message else None
            jd = routes._cb_decode(data)
            logger.info(f"cb json chat_id={from_chat} data={jd}")
            if isinstance(jd, dict) and jd.get('t'):
                routes.process_callback_json(int(from_chat), jd, message_id=msg_id)
                return
        except Exception:
            logger.exception("callback processing failed")
            return

    async def _main(self):
        try:
            token = notifier._ensure_token()
            if not token or Bot is None or Dispatcher is None:
                logger.error("Aiogram _main: missing token or aiogram is not available")
                return
            logger.info("[telegram] Starting aiogram v3 polling runner")
            self._bot = Bot(token=token)
            self._dp = Dispatcher()
            try:
                await self._bot.delete_webhook(drop_pending_updates=True)
            except Exception as e:
                logger.warning(f"delete_webhook failed: {e}")
            self._dp.message.register(self._on_message, F.text)
            self._dp.callback_query.register(self._on_callback)
            await self._dp.start_polling(self._bot, allowed_updates=["message", "callback_query"])
        except Exception as e:
            logger.error(f"Aiogram runner failed: {e}")

    def _thread_target(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._main())
        except Exception as e:
            logger.exception(f"Aiogram thread target error: {e}")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._thread_target, daemon=True)
        self._thread.start()


class SimpleHTTPPoller:
    def __init__(self):
        self._thr: Optional[threading.Thread] = None
        self._running = False
        self._offset = None

    def _run(self):
        try:
            token = notifier._ensure_token()
            if not token:
                logger.error("HTTP poller: no token, abort")
                return
            try:
                logger.info("[telegram] HTTP poller: deleting webhook (fallback mode)")
                requests.post(f"https://api.telegram.org/bot{token}/deleteWebhook", timeout=10)
            except Exception as e:
                logger.debug("Handled exception in _run: %s", e)
            logger.info("[telegram] Starting legacy HTTP polling fallback")
            self._running = True

            routes = _load_routes_module()
            if hasattr(routes, 'set_notifier'):
                routes.set_notifier(notifier)

            while self._running:
                try:
                    params = {'timeout': 50}
                    if self._offset is not None:
                        params['offset'] = int(self._offset)
                    resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=60)
                    data = resp.json() if resp.ok else {}
                    for u in (data.get('result') or []):
                        try:
                            self._offset = int(u.get('update_id', 0)) + 1
                        except Exception as e:
                            logger.debug("Handled exception in _run: %s", e)

                        cq = u.get('callback_query') or {}
                        if cq:
                            try:
                                cqid = cq.get('id')
                                if cqid:
                                    try:
                                        notifier.answer_callback(cqid)
                                    except Exception as e:
                                        logger.debug("Handled exception in line_428: %s", e)
                                from_chat = ((cq.get('message') or {}).get('chat') or {}).get('id')
                                msg_id = ((cq.get('message') or {}).get('message_id'))
                                data_cb = cq.get('data') or ''
                                if from_chat and data_cb:
                                    try:
                                        jd2 = routes._cb_decode(str(data_cb))
                                        if isinstance(jd2, dict) and jd2.get('t'):
                                            routes.process_callback_json(int(from_chat), jd2, message_id=int(msg_id) if msg_id is not None else None)
                                            continue
                                    except Exception as e:
                                        logger.debug("Handled exception in line_439: %s", e)
                            except Exception as e:
                                logger.debug("Handled exception in line_441: %s", e)

                        msg = u.get('message') or {}
                        chat = msg.get('chat') or {}
                        text = (msg.get('text') or '').strip()
                        cid = chat.get('id')

                        if cid and text:
                            try:
                                username = chat.get('username')
                                first_name = chat.get('first_name')
                                db.upsert_bot_user(int(cid), username, first_name)
                                routes = _load_routes_module()
                                if hasattr(routes, 'set_notifier'):
                                    routes.set_notifier(notifier)
                                kb = {'inline_keyboard': [[{'text': 'Группы', 'callback_data': 'menu:groups'}]]}
                                notifier.send_message(int(cid), 'Главное меню:', kb)
                            except Exception as e:
                                logger.debug("Exception in line_459: %s", e)
                                try:
                                    notifier.send_text(int(cid), 'Главное меню: нажмите «Группы»')
                                except Exception as e:
                                    logger.debug("Handled exception in line_463: %s", e)
                            continue

                except Exception as e:
                    logger.debug("Exception in line_467: %s", e)
                    time.sleep(2)
                    continue
        except Exception as e:
            logger.error(f"HTTP poller failed: {e}")

    def start(self):
        if self._thr and self._thr.is_alive():
            return
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()


_poller = None
_aiogram_runner: Optional[AiogramBotRunner] = None
_http_poller: Optional[SimpleHTTPPoller] = None

notifier = TelegramNotifier()

def start_long_polling_if_needed():
    global _aiogram_runner, _http_poller
    try:
        if not notifier._ensure_token():
            logger.error("start_long_polling_if_needed: no token; skip")
            return
        started = False
        try:
            if Bot is not None and Dispatcher is not None:
                if _aiogram_runner is None:
                    _aiogram_runner = AiogramBotRunner()
                    _aiogram_runner.start()
                started = True
                logger.info("aiogram runner started")
        except Exception as e:
            logger.error(f"Aiogram start failed: {e}")
            started = False
        if not started:
            if _http_poller is None:
                _http_poller = SimpleHTTPPoller()
                _http_poller.start()
                logger.info("http poller started")
    except Exception as e:
        logger.exception(f"start_long_polling_if_needed error: {e}")


def subscribe_to_events():
    try:
        from services import events as evt
    except Exception as e:
        logger.debug("Exception in subscribe_to_events: %s", e)
        return

    def _on_event(ev: dict):
        try:
            admin_chat = db.get_setting_value('telegram_admin_chat_id')
            if not admin_chat:
                return
            t = str(ev.get('type') or '')
            if t in ('emergency_on', 'emergency_off', 'critical_error', 'error'):
                if t == 'emergency_on':
                    txt = f"🚨 Аварийная остановка инициирована ({ev.get('by','')})"
                elif t == 'emergency_off':
                    txt = f"✅ Аварийная остановка снята ({ev.get('by','')})"
                else:
                    code = ev.get('code') or ev.get('name') or 'error'
                    msg = ev.get('message') or ''
                    txt = f"❗️Критическая ошибка: {code}\n{msg}".strip()
                try:
                    notifier.send_text(int(admin_chat), txt)
                except Exception as e:
                    logger.debug("Handled exception in _on_event: %s", e)
        except Exception as e:
            logger.debug("Handled exception in _on_event: %s", e)

    try:
        evt.subscribe(_on_event)
    except Exception as e:
        logger.debug("Handled exception in _on_event: %s", e)
