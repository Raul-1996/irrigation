from typing import Optional
import logging
import threading
from utils import decrypt_secret
from database import db
from werkzeug.security import check_password_hash
from datetime import datetime, timedelta
import time
import json
import requests
import base64
import uuid
import asyncio
import os
try:
    # aiogram v3
    from aiogram import Bot, Dispatcher, F
    from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup as _AInlineKeyboardMarkup, InlineKeyboardButton as _AInlineKeyboardButton
except Exception:
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
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        logs_dir = os.path.join(base_dir, 'logs')
        os.makedirs(logs_dir, exist_ok=True)
        log_path = os.path.join(logs_dir, 'telegram.txt')
        fh = logging.FileHandler(log_path, encoding='utf-8')
        fmt = logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        # keep console output too
        try:
            has_stream = any(isinstance(h, logging.StreamHandler) for h in getattr(logger, 'handlers', []))
            if not has_stream:
                sh = logging.StreamHandler()
                sh.setFormatter(fmt)
                logger.addHandler(sh)
        except Exception:
            pass
        logger.setLevel(logging.INFO)
        logger.propagate = True
        logger._telegram_configured = True  # type: ignore[attr-defined]
        logger.info(f"telegram service logger initialized -> {log_path}")
except Exception:
    pass

def _redact_url(url: str) -> str:
    try:
        if '/bot' in url:
            a, b = url.split('/bot', 1)
            if '/' in b:
                return a + '/bot***' + '/' + b.split('/', 1)[1]
            return a + '/bot***'
        return url
    except Exception:
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
                return None
            token = decrypt_secret(tok_enc)
            if not token:
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
                except Exception:
                    fut.cancel()
        except Exception:
            pass
        return False

    def send_text(self, chat_id: int, text: str) -> bool:
        try:
            try:
                logger.info(f"send_text chat_id={int(chat_id)} len={len(str(text) or '')}")
            except Exception:
                pass
            # Prefer aiogram if running
            if Bot is not None:
                try:
                    global _aiogram_runner
                    bot = getattr(_aiogram_runner, '_bot', None) if '_aiogram_runner' in globals() else None
                    if bot is not None:
                        return self._submit_aiogram(bot.send_message(chat_id=int(chat_id), text=str(text)))
                except Exception:
                    pass
            token = self._ensure_token()
            if not token:
                return False
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {'chat_id': int(chat_id), 'text': str(text)}
            try:
                logger.info(f"http POST {_redact_url(url)} payload={payload}")
            except Exception:
                pass
            resp = requests.post(url, json=payload, timeout=10)
            try:
                logger.info(f"http RESP status={resp.status_code} body={resp.text[:200]}")
            except Exception:
                pass
            data = resp.json() if resp.ok else {}
            return bool(data.get('ok'))
        except Exception as e:
            logger.error(f"TelegramNotifier send_text failed: {e}")
            return False

    def send_message(self, chat_id: int, text: str, reply_markup=None) -> bool:
        try:
            try:
                logger.info(f"send_message chat_id={int(chat_id)} len={len(str(text) or '')} has_kb={bool(reply_markup)}")
            except Exception:
                pass
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
                            except Exception:
                                rk = None
                        return self._submit_aiogram(bot.send_message(chat_id=int(chat_id), text=str(text), reply_markup=rk))
                except Exception:
                    pass
            token = self._ensure_token()
            if not token:
                return False
            payload = {'chat_id': int(chat_id), 'text': str(text)}
            if reply_markup is not None:
                payload['reply_markup'] = reply_markup
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            try:
                logger.info(f"http POST {_redact_url(url)} payload_keys={list(payload.keys())}")
            except Exception:
                pass
            resp = requests.post(url, json=payload, timeout=10)
            try:
                logger.info(f"http RESP status={resp.status_code} body={resp.text[:200]}")
            except Exception:
                pass
            data = resp.json() if resp.ok else {}
            return bool(data.get('ok'))
        except Exception as e:
            logger.error(f"TelegramNotifier send_message failed: {e}")
            return False

    def send_menu(self, chat_id: int, text: str, inline_keyboard_rows: list[list[dict]]) -> bool:
        try:
            return self.send_message(chat_id, text, {'inline_keyboard': inline_keyboard_rows})
        except Exception:
            return False

    def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup=None) -> bool:
        try:
            try:
                logger.info(f"edit_message_text chat_id={int(chat_id)} msg_id={int(message_id)} len={len(str(text) or '')} has_kb={bool(reply_markup)}")
            except Exception:
                pass
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
                            except Exception:
                                rk = None
                        return self._submit_aiogram(bot.edit_message_text(chat_id=int(chat_id), message_id=int(message_id), text=str(text), reply_markup=rk))
                except Exception:
                    pass
            token = self._ensure_token()
            if not token:
                return False
            payload = {'chat_id': int(chat_id), 'message_id': int(message_id), 'text': str(text)}
            if reply_markup is not None:
                payload['reply_markup'] = reply_markup
            url = f"https://api.telegram.org/bot{token}/editMessageText"
            try:
                logger.info(f"http POST {_redact_url(url)} payload_keys={list(payload.keys())}")
            except Exception:
                pass
            resp = requests.post(url, json=payload, timeout=10)
            try:
                logger.info(f"http RESP status={resp.status_code} body={resp.text[:200]}")
            except Exception:
                pass
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
                        # Pass minimal args to avoid type issues with None
                        if text is None:
                            coro = bot.answer_callback_query(callback_query_id=str(callback_query_id))
                        else:
                            coro = bot.answer_callback_query(callback_query_id=str(callback_query_id), text=str(text), show_alert=bool(show_alert))
                        self._submit_aiogram(coro)
                        return
                except Exception:
                    pass
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

    def set_webhook(self, url: str) -> bool:
        try:
            token = self._ensure_token()
            if not token or not url:
                return False
            payload = {'url': str(url)}
            # Передадим секрет, если он задан
            try:
                wh_secret = db.get_setting_value('telegram_webhook_secret_path')
                if wh_secret:
                    payload['secret_token'] = str(wh_secret)
            except Exception:
                pass
            resp = requests.post(f"https://api.telegram.org/bot{token}/setWebhook", json=payload, timeout=10)
            data = resp.json() if resp.ok else {}
            return bool(data.get('ok'))
        except Exception as e:
            logger.error(f"TelegramNotifier set_webhook failed: {e}")
            return False

    def delete_webhook(self) -> bool:
        try:
            token = self._ensure_token()
            if not token:
                return False
            resp = requests.post(f"https://api.telegram.org/bot{token}/deleteWebhook", timeout=10)
            data = resp.json() if resp.ok else {}
            return bool(data.get('ok'))
        except Exception as e:
            logger.error(f"TelegramNotifier delete_webhook failed: {e}")
            return False


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
        except Exception:
            return
        try:
            logger.info(f"rx_message chat_id={chat_id} text={text[:160]}")
        except Exception:
            pass
        try:
        db.upsert_bot_user(int(chat_id), username, first_name)
        except Exception:
            pass
        try:
            ulock = db.get_bot_user_by_chat(int(chat_id)) or {}
            locked_until = ulock.get('locked_until')
            if locked_until:
                try:
                    lu = datetime.strptime(str(locked_until), '%Y-%m-%d %H:%M:%S')
                    if datetime.now() < lu and not text.startswith('/start'):
                        notifier.send_text(chat_id, 'Ваш аккаунт временно заблокирован. Попробуйте позже.')
                        return
                except Exception:
                    pass
        except Exception:
            pass

        if text.startswith('/start'):
            notifier.send_text(chat_id, (
                'Привет! Это WB‑Irrigation Bot.\n\n'
                'Доступные команды:\n'
                '/auth <пароль> — авторизация\n'
                '/menu — главное меню\n'
                '/help — краткая справка\n'
                '/report — быстрый отчёт\n'
                '/subscribe, /unsubscribe — подписки\n\n'
                'Сначала пройдите авторизацию: /auth <пароль>'
            ))
            return
        if text.startswith('/auth'):
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                pwd = parts[1].strip()
                h = db.get_setting_value('telegram_access_password_hash')
                if h and check_password_hash(h, pwd):
                    db.set_bot_user_authorized(int(chat_id), role='user')
                    notifier.send_text(chat_id, 'Готово. Доступ предоставлен.')
                    try:
                        from routes.telegram import _btn, _inline_markup
                        rows = [
                            [_btn('Группы', {'t': 'menu', 'a': 'groups'}), _btn('Зоны', {'t': 'menu', 'a': 'zones'})],
                            [_btn('Отложить полив', {'t': 'menu', 'a': 'postpone'}), _btn('Отчёты', {'t': 'menu', 'a': 'report'})],
                            [_btn('Подписки', {'t': 'menu', 'a': 'subs'}), _btn('Уведомления', {'t': 'menu', 'a': 'notif'})],
                        ]
                        ok = notifier.send_message(chat_id, 'Главное меню:', _inline_markup(rows))
                        if not ok:
                            notifier.send_text(chat_id, 'Меню: /groups, /zones <group>, /report today|7|30, /subscribe, /unsubscribe')
                    except Exception:
                        notifier.send_text(chat_id, 'Меню: /groups, /zones <group>, /report today|7|30, /subscribe, /unsubscribe')
                    return
                else:
                    failed = db.inc_bot_user_failed(int(chat_id))
                    if failed >= 5:
                        until = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
                        db.lock_bot_user_until(int(chat_id), until)
                    notifier.send_text(chat_id, f'Пароль неверный. Осталось попыток: {max(0, 5-failed)}')
                    return

        user = db.get_bot_user_by_chat(int(chat_id)) or {}
        if not user or not int(user.get('is_authorized') or 0):
            notifier.send_text(chat_id, 'Нет доступа. Авторизуйтесь: /auth <пароль>')
            return

        if text.startswith('/help'):
            notifier.send_text(chat_id, '/menu, /groups, /zones <group>, /group_start <id>, /group_stop <id>, /zone_start <id>, /zone_stop <id>, /report today')
            return
        if text.startswith('/menu'):
            try:
                from routes.telegram import _btn, _inline_markup
                rows = [
                    [_btn('Группы', {'t': 'menu', 'a': 'groups'}), _btn('Зоны', {'t': 'menu', 'a': 'zones'})],
                    [_btn('Отложить полив', {'t': 'menu', 'a': 'postpone'}), _btn('Отчёты', {'t': 'menu', 'a': 'report'})],
                    [_btn('Подписки', {'t': 'menu', 'a': 'subs'}), _btn('Уведомления', {'t': 'menu', 'a': 'notif'})],
                ]
                ok = notifier.send_message(chat_id, 'Главное меню:', _inline_markup(rows))
                if not ok:
                    notifier.send_text(chat_id, 'Меню: /groups, /zones <group>, /report today|7|30, /subscribe, /unsubscribe')
            except Exception:
                notifier.send_text(chat_id, 'Меню: /groups, /zones <group>, /report today|7|30, /subscribe, /unsubscribe')
            return
        if text.startswith('/groups'):
            gl = db.list_groups_min()
            txt = 'Группы:\n' + '\n'.join([f"{g['id']}: {g['name']}" for g in gl])
            notifier.send_text(chat_id, txt)
            return
        if text.startswith('/zones'):
            parts = text.split()
            try:
                gid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            except Exception:
                gid = 0
            if not gid:
                notifier.send_text(chat_id, 'Используйте: /zones <group_id>')
                return
            zl = db.list_zones_by_group_min(gid)
            txt = f'Зоны группы {gid}:\n' + '\n'.join([f"{z['id']}: {z['name']} ({z['state']})" for z in zl])
            notifier.send_text(chat_id, txt)
            return
        if text.startswith('/group_start'):
            parts = text.split()
            if len(parts) > 1 and parts[1].isdigit():
                gid = int(parts[1])
                try:
                    from irrigation_scheduler import get_scheduler
                    s = get_scheduler()
                    if s:
                        s.start_group_sequence(gid)
                    notifier.send_text(chat_id, f'▶ Группа {gid} запущена')
                except Exception:
                    notifier.send_text(chat_id, 'Ошибка запуска группы')
            return
        if text.startswith('/group_stop'):
            parts = text.split()
            if len(parts) > 1 and parts[1].isdigit():
                gid = int(parts[1])
                try:
                    from services.zone_control import stop_all_in_group
                    stop_all_in_group(gid, reason='telegram')
                    notifier.send_text(chat_id, f'⏹ Группа {gid} остановлена')
                except Exception:
                    notifier.send_text(chat_id, 'Ошибка остановки группы')
            return
        if text.startswith('/zone_start'):
            parts = text.split()
            if len(parts) > 1 and parts[1].isdigit():
                zid = int(parts[1])
                try:
                    from services.zone_control import exclusive_start_zone
                    exclusive_start_zone(zid)
                    notifier.send_text(chat_id, f'▶ Зона {zid} запущена')
                except Exception:
                    notifier.send_text(chat_id, 'Ошибка запуска зоны')
            return
        if text.startswith('/zone_stop'):
            parts = text.split()
            if len(parts) > 1 and parts[1].isdigit():
                zid = int(parts[1])
                try:
                    from services.zone_control import stop_zone
                    stop_zone(zid, reason='telegram')
                    notifier.send_text(chat_id, f'⏹ Зона {zid} остановлена')
                except Exception:
                    notifier.send_text(chat_id, 'Ошибка остановки зоны')
            return
        if text.startswith('/report'):
            from services.reports import build_report_text
            period = 'today'
            parts = text.split()
            if len(parts) > 1:
                period = parts[1]
            txt = build_report_text(period=period, fmt='brief')
            notifier.send_text(chat_id, txt)
            return
        if text.startswith('/whoami'):
            role = str((user or {}).get('role') or 'user')
            notifier.send_text(chat_id, f"chat_id={chat_id}, role={role}")
            return
        if text.startswith('/emergency_stop'):
            if str(user.get('role','user')) != 'admin':
                notifier.send_text(chat_id, 'Нет прав')
                return
            try:
                from app import app as _app
                with _app.test_request_context():
                    from app import api_emergency_stop as _es
                    _es()
                notifier.send_text(chat_id, '🚨 Аварийная остановка активирована')
            except Exception:
                notifier.send_text(chat_id, 'Ошибка аварийной остановки')
            return
        if text.startswith('/emergency_resume'):
            if str(user.get('role','user')) != 'admin':
                notifier.send_text(chat_id, 'Нет прав')
                return
            try:
                from app import app as _app
                with _app.test_request_context():
                    from app import api_emergency_resume as _er
                    _er()
                notifier.send_text(chat_id, '✅ Аварийная остановка снята')
            except Exception:
                notifier.send_text(chat_id, 'Ошибка снятия аварийной остановки')
            return
        if text.startswith('/broadcast'):
            if str(user.get('role','user')) != 'admin':
                notifier.send_text(chat_id, 'Нет прав')
                return
            msg = text[len('/broadcast'):].strip()
            if not msg:
                notifier.send_text(chat_id, 'Текст пуст')
                return
            try:
                import sqlite3
                with sqlite3.connect(db.db_path, timeout=5) as conn:
                    conn.row_factory = sqlite3.Row
                    cur = conn.execute('SELECT chat_id FROM bot_users WHERE is_authorized=1')
                    for r in cur.fetchall():
                        try:
                            notifier.send_text(int(r['chat_id']), msg)
                        except Exception:
                            pass
                notifier.send_text(chat_id, 'Рассылка выполнена')
            except Exception:
                notifier.send_text(chat_id, 'Ошибка рассылки')
            return
        if text.startswith('/subscribe'):
            try:
                parts = text.split()
                stype = parts[1] if len(parts)>1 else 'daily'
                sformat = parts[2] if len(parts)>2 else 'brief'
                time_local = parts[3] if len(parts)>3 else '08:00'
                dow = parts[4] if (len(parts)>4 and stype=='weekly') else None
            except Exception:
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
                except Exception:
                    pass
            notifier.send_text(chat_id, 'Подписки отключены')
            return

    async def _on_callback(self, cq: CallbackQuery):
        # 1) Always ACK first to stop Telegram spinner
        try:
            notifier.answer_callback(str(cq.id))
            logger.info(f"ack callback id={cq.id} chat_id={getattr(cq.from_user,'id',None)} data={(cq.data or '')[:120]}")
        except Exception:
            logger.exception("callback ack failed")
        # 2) Import processing helpers
        try:
            from routes.telegram import process_callback_json, _cb_decode
        except Exception as e:
            logger.exception("failed to import routes.telegram in _on_callback")
            return
        # 3) Parse data
        try:
            data = str(cq.data or '')
            from_chat = int((cq.message.chat.id if cq.message and cq.message.chat else cq.from_user.id))
        except Exception:
            return
        # 4) Handle JSON callbacks, then fallback to legacy string patterns
        try:
            jd = _cb_decode(data)
            logger.info(f"cb json chat_id={from_chat} data={jd}")
            if jd.get('t'):
                process_callback_json(int(from_chat), jd)
                return
            # Fallbacks for legacy string callback_data
            fb = None
            if data.startswith('menu:'):
                fb = {'t': 'menu', 'a': data.split(':', 1)[1]}
            elif data.startswith('zones:'):
                try:
                    fb = {'t': 'zones_select', 'gid': int(data.split(':', 1)[1])}
                except Exception:
                    fb = None
            elif data.startswith('zone_start:'):
                try:
                    fb = {'t': 'zone_start', 'zid': int(data.split(':', 1)[1])}
                except Exception:
                    fb = None
            elif data.startswith('zone_stop:'):
                try:
                    fb = {'t': 'zone_stop', 'zid': int(data.split(':', 1)[1])}
                except Exception:
                    fb = None
            elif data.startswith('grp_start:'):
                try:
                    fb = {'t': 'grp_start', 'gid': int(data.split(':', 1)[1])}
                except Exception:
                    fb = None
            elif data.startswith('grp_stop:'):
                try:
                    fb = {'t': 'grp_stop', 'gid': int(data.split(':', 1)[1])}
                except Exception:
                    fb = None
            elif data.startswith('grp_postpone:'):
                parts = data.split(':')
                try:
                    fb = {'t': 'postpone', 'gid': int(parts[1]), 'days': int(parts[2])}
                except Exception:
                    fb = None
            elif data == 'confirm:cancel':
                fb = {'t': 'confirm', 'a': 'cancel'}
            elif data.startswith('confirm:emergency:'):
                fb = {'t': 'confirm', 'a': 'emergency', 'do': data.split(':', 2)[2]}
            if fb:
                process_callback_json(int(from_chat), fb)
                return
        except Exception:
            logger.exception("callback processing failed")
        # Fallbacks are inside process_callback_json for legacy strings via webhook route; we only handle JSON callbacks here.

    async def _main(self):
        try:
            token = notifier._ensure_token()
            if not token or Bot is None or Dispatcher is None:
                return
            logger.info("[telegram] Starting aiogram v3 polling runner")
            self._bot = Bot(token=token)
            self._dp = Dispatcher()
            try:
                await self._bot.delete_webhook(drop_pending_updates=True)
            except Exception:
                pass
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
        except Exception:
            pass

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
                return
            try:
                # Ensure webhook is removed so Telegram delivers updates to getUpdates
                logger.info("[telegram] HTTP poller: deleting webhook (fallback mode)")
                requests.post(f"https://api.telegram.org/bot{token}/deleteWebhook", timeout=10)
            except Exception:
                pass
            logger.info("[telegram] Starting legacy HTTP polling fallback")
            self._running = True
            from routes.telegram import process_callback_json, _cb_decode
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
                        except Exception:
                            pass
                        cq = u.get('callback_query') or {}
                        if cq:
                            try:
                                cqid = cq.get('id')
                                if cqid:
                                    try:
                                        notifier.answer_callback(cqid)
                                    except Exception:
                                        pass
                                from_chat = ((cq.get('message') or {}).get('chat') or {}).get('id')
                                data_cb = cq.get('data') or ''
                                if from_chat and data_cb:
                                    try:
                                    jd2 = _cb_decode(str(data_cb))
                                        if jd2.get('t'):
                                            process_callback_json(int(from_chat), jd2)
                                        continue
                                except Exception:
                                    pass
                            except Exception:
                                pass
                        msg = u.get('message') or {}
                        chat = msg.get('chat') or {}
                        text = (msg.get('text') or '').strip()
                        cid = chat.get('id')
                        if cid and text:
                            try:
                                # Reuse aiogram-like handler logic via notifier and DB; keep in sync with _on_message
                                # Minimal inline handling to avoid duplication
                                username = chat.get('username'); first_name = chat.get('first_name')
                                db.upsert_bot_user(int(cid), username, first_name)
                                ulock = db.get_bot_user_by_chat(int(cid)) or {}
                                locked_until = ulock.get('locked_until')
                                if locked_until:
                                    try:
                                        lu = datetime.strptime(str(locked_until), '%Y-%m-%d %H:%M:%S')
                                        if datetime.now() < lu and not text.startswith('/start'):
                                            notifier.send_text(int(cid), 'Ваш аккаунт временно заблокирован. Попробуйте позже.')
                                            continue
                                    except Exception:
                                        pass
                                if text.startswith('/start'):
                                    notifier.send_text(int(cid), (
                                        'Привет! Это WB‑Irrigation Bot.\n\n'
                                        'Доступные команды:\n'
                                        '/auth <пароль> — авторизация\n'
                                        '/menu — главное меню\n'
                                        '/help — краткая справка\n'
                                        '/report — быстрый отчёт\n'
                                        '/subscribe, /unsubscribe — подписки\n\n'
                                        'Сначала пройдите авторизацию: /auth <пароль>'
                                    ))
                                    continue
                                if text.startswith('/auth'):
                                    parts = text.split(maxsplit=1)
                                    if len(parts) == 2:
                                        pwd = parts[1].strip()
                                        h = db.get_setting_value('telegram_access_password_hash')
                                        if h and check_password_hash(h, pwd):
                                            db.set_bot_user_authorized(int(cid), role='user')
                                            notifier.send_text(int(cid), 'Готово. Доступ предоставлен.')
                                            try:
                                                rows = [
                                                    [ {'text': 'Группы', 'callback_data': 'menu:groups'}, {'text': 'Зоны', 'callback_data': 'menu:zones'} ],
                                                    [ {'text': 'Отложить полив', 'callback_data': 'menu:postpone'}, {'text': 'Отчёты', 'callback_data': 'menu:report'} ],
                                                    [ {'text': 'Подписки', 'callback_data': 'menu:subs'}, {'text': 'Уведомления', 'callback_data': 'menu:notif'} ],
                                                ]
                                                notifier.send_menu(int(cid), 'Главное меню:', rows)
                                            except Exception:
                                                pass
                                            continue
                                        else:
                                            failed = db.inc_bot_user_failed(int(cid))
                                            if failed >= 5:
                                                until = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
                                                db.lock_bot_user_until(int(cid), until)
                                            notifier.send_text(int(cid), f'Пароль неверный. Осталось попыток: {max(0, 5-failed)}')
                                            continue
                                user = db.get_bot_user_by_chat(int(cid)) or {}
                                if not user or not int(user.get('is_authorized') or 0):
                                    notifier.send_text(int(cid), 'Нет доступа. Авторизуйтесь: /auth <пароль>')
                                    continue
                                if text.startswith('/help'):
                                    notifier.send_text(int(cid), '/menu, /groups, /zones <group>, /group_start <id>, /group_stop <id>, /zone_start <id>, /zone_stop <id>, /report today')
                                    continue
                                if text.startswith('/menu'):
                                    rows = [
                                        [ {'text': 'Группы', 'callback_data': 'menu:groups'}, {'text': 'Зоны', 'callback_data': 'menu:zones'} ],
                                        [ {'text': 'Отложить полив', 'callback_data': 'menu:postpone'}, {'text': 'Отчёты', 'callback_data': 'menu:report'} ],
                                        [ {'text': 'Подписки', 'callback_data': 'menu:subs'}, {'text': 'Уведомления', 'callback_data': 'menu:notif'} ],
                                    ]
                                    notifier.send_menu(int(cid), 'Главное меню:', rows)
                                    continue
                                if text.startswith('/groups'):
                                    gl = db.list_groups_min()
                                    txt = 'Группы:\n' + '\n'.join([f"{g['id']}: {g['name']}" for g in gl])
                                    notifier.send_text(int(cid), txt)
                                    continue
                                if text.startswith('/zones'):
                                    parts = text.split()
                                    gid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                                    if not gid:
                                        notifier.send_text(int(cid), 'Используйте: /zones <group_id>')
                                        continue
                                    zl = db.list_zones_by_group_min(gid)
                                    txt = f'Зоны группы {gid}:\n' + '\n'.join([f"{z['id']}: {z['name']} ({z['state']})" for z in zl])
                                    notifier.send_text(int(cid), txt)
                                    continue
                                if text.startswith('/group_start'):
                                    parts = text.split()
                                    if len(parts) > 1 and parts[1].isdigit():
                                        gid = int(parts[1])
                                        try:
                                            from irrigation_scheduler import get_scheduler
                                            s = get_scheduler()
                                            if s:
                                                s.start_group_sequence(gid)
                                            notifier.send_text(int(cid), f'▶ Группа {gid} запущена')
                                        except Exception:
                                            notifier.send_text(int(cid), 'Ошибка запуска группы')
                                    continue
                                if text.startswith('/group_stop'):
                                    parts = text.split()
                                    if len(parts) > 1 and parts[1].isdigit():
                                        gid = int(parts[1])
                                        try:
                                            from services.zone_control import stop_all_in_group
                                            stop_all_in_group(gid, reason='telegram')
                                            notifier.send_text(int(cid), f'⏹ Группа {gid} остановлена')
                                        except Exception:
                                            notifier.send_text(int(cid), 'Ошибка остановки группы')
                                    continue
                                if text.startswith('/zone_start'):
                                    parts = text.split()
                                    if len(parts) > 1 and parts[1].isdigit():
                                        zid = int(parts[1])
                                        try:
                                            from services.zone_control import exclusive_start_zone
                                            exclusive_start_zone(zid)
                                            notifier.send_text(int(cid), f'▶ Зона {zid} запущена')
                                        except Exception:
                                            notifier.send_text(int(cid), 'Ошибка запуска зоны')
                                    continue
                                if text.startswith('/zone_stop'):
                                    parts = text.split()
                                    if len(parts) > 1 and parts[1].isdigit():
                                        zid = int(parts[1])
                                        try:
                                            from services.zone_control import stop_zone
                                            stop_zone(zid, reason='telegram')
                                            notifier.send_text(int(cid), f'⏹ Зона {zid} остановлена')
                                        except Exception:
                                            notifier.send_text(int(cid), 'Ошибка остановки зоны')
                                    continue
                                if text.startswith('/report'):
                                    from services.reports import build_report_text
                                    period = 'today'
                                    parts = text.split()
                                    if len(parts) > 1:
                                        period = parts[1]
                                    txt = build_report_text(period=period, fmt='brief')
                                    notifier.send_text(int(cid), txt)
                                    continue
                            except Exception:
                                pass
                except Exception:
                    time.sleep(2)
                    continue
        except Exception as e:
            logger.error(f"HTTP poller failed: {e}")

    def start(self):
        if self._thr and self._thr.is_alive():
            return
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()


_poller = None  # legacy placeholder for backward compatibility
_aiogram_runner: Optional[AiogramBotRunner] = None
_http_poller: Optional[SimpleHTTPPoller] = None

def start_long_polling_if_needed():
    global _aiogram_runner, _http_poller
    try:
        if not notifier._ensure_token():
            return
        started = False
        # Try aiogram first
        try:
            if Bot is not None and Dispatcher is not None:
                if _aiogram_runner is None:
                    _aiogram_runner = AiogramBotRunner()
                    _aiogram_runner.start()
                started = True
        except Exception as e:
            logger.error(f"Aiogram start failed: {e}")
            started = False
        # Fallback to HTTP poller
        if not started:
            if _http_poller is None:
                _http_poller = SimpleHTTPPoller()
                _http_poller.start()
    except Exception:
        pass

notifier = TelegramNotifier()

def subscribe_to_events():
    try:
        from services import events as evt
    except Exception:
        return
    def _on_event(ev: dict):
        try:
            admin_chat = db.get_setting_value('telegram_admin_chat_id')
            if not admin_chat:
                return
            t = str(ev.get('type') or '')
            # Отправляем только критические уведомления
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
                except Exception:
                    pass
        except Exception:
            pass
    try:
        evt.subscribe(_on_event)
    except Exception:
        pass

