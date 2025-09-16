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

logger = logging.getLogger(__name__)

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

    def send_text(self, chat_id: int, text: str) -> bool:
        try:
            token = self._ensure_token()
            if not token:
                return False
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            resp = requests.post(url, json={
                'chat_id': int(chat_id),
                'text': str(text)
            }, timeout=10)
            data = resp.json() if resp.ok else {}
            return bool(data.get('ok'))
        except Exception as e:
            logger.error(f"TelegramNotifier send_text failed: {e}")
            return False

    def send_message(self, chat_id: int, text: str, reply_markup=None) -> bool:
        try:
            token = self._ensure_token()
            if not token:
                return False
            payload = {'chat_id': int(chat_id), 'text': str(text)}
            if reply_markup is not None:
                payload['reply_markup'] = reply_markup
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json() if resp.ok else {}
            return bool(data.get('ok'))
        except Exception as e:
            logger.error(f"TelegramNotifier send_message failed: {e}")
            return False

    def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup=None) -> bool:
        try:
            token = self._ensure_token()
            if not token:
                return False
            payload = {'chat_id': int(chat_id), 'message_id': int(message_id), 'text': str(text)}
            if reply_markup is not None:
                payload['reply_markup'] = reply_markup
            url = f"https://api.telegram.org/bot{token}/editMessageText"
            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json() if resp.ok else {}
            return bool(data.get('ok'))
        except Exception as e:
            logger.error(f"TelegramNotifier edit_message_text failed: {e}")
            return False

    def answer_callback(self, callback_query_id: str, text: str = None, show_alert: bool = False) -> None:
        try:
            token = self._ensure_token()
            if not token or not callback_query_id:
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


class TelegramLongPoller:
    def __init__(self):
        self._thr: Optional[threading.Thread] = None
        self._running = False
        self._offset = None

    def _handle_message(self, chat_id: int, text: str, username: Optional[str] = None, first_name: Optional[str] = None):
        # very close to routes/telegram handlers, simplified
        if not chat_id:
            return
        db.upsert_bot_user(int(chat_id), username, first_name)
        # lock check
        try:
            ulock = db.get_bot_user_by_chat(int(chat_id)) or {}
            locked_until = ulock.get('locked_until')
            if locked_until:
                try:
                    lu = datetime.strptime(str(locked_until), '%Y-%m-%d %H:%M:%S')
                    if datetime.now() < lu and not (text or '').startswith('/start'):
                        notifier.send_text(chat_id, 'Ваш аккаунт временно заблокирован. Попробуйте позже.')
                        return
                except Exception:
                    pass
        except Exception:
            pass

        # /start
        if text.startswith('/start'):
            notifier.send_text(chat_id, 'Привет! Это WB-Irrigation. Для доступа отправьте команду /auth <пароль>.')
            return
        # /auth
        if text.startswith('/auth'):
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                pwd = parts[1].strip()
                h = db.get_setting_value('telegram_access_password_hash')
                if h and check_password_hash(h, pwd):
                    db.set_bot_user_authorized(int(chat_id), role='user')
                    notifier.send_text(chat_id, 'Готово. Доступ предоставлен. Введите /menu.')
                    return
                else:
                    failed = db.inc_bot_user_failed(int(chat_id))
                    if failed >= 5:
                        until = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
                        db.lock_bot_user_until(int(chat_id), until)
                    notifier.send_text(chat_id, f'Пароль неверный. Осталось попыток: {max(0, 5-failed)}')
                    return
        # rate limit
        # simple per-minute counter in memory (omitted to keep thread-safe minimal)

        user = db.get_bot_user_by_chat(int(chat_id)) or {}
        if not user or not int(user.get('is_authorized') or 0):
            notifier.send_text(chat_id, 'Нет доступа. Авторизуйтесь: /auth <пароль>')
            return

        # commands (subset)
        if text.startswith('/help'):
            notifier.send_text(chat_id, '/menu, /groups, /zones <group>, /group_start <id>, /group_stop <id>, /zone_start <id>, /zone_stop <id>, /report today')
            return
        if text.startswith('/menu'):
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

    def _run(self):
        try:
            # if webhook is active, skip polling
            token = notifier._ensure_token()
            if not token:
                return
            try:
                resp = requests.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=10)
                info = resp.json() if resp.ok else {}
                if (info.get('ok') and (info.get('result') or {}).get('url')):
                    return  # webhook active
            except Exception:
                pass
            self._running = True
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
                        msg = u.get('message') or {}
                        chat = msg.get('chat') or {}
                        text = msg.get('text') or ''
                        cid = chat.get('id')
                        if cid:
                            self._handle_message(int(cid), text, chat.get('username'), chat.get('first_name'))
                except Exception:
                    time.sleep(2)
                    continue
        except Exception as e:
            logger.error(f"TelegramLongPoller failed: {e}")

    def start(self):
        if self._thr and self._thr.is_alive():
            return
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()


_poller: Optional[TelegramLongPoller] = None

def start_long_polling_if_needed():
    global _poller
    try:
        # Ensure bot configured
        if not notifier._ensure_bot():
            return
        # If webhook has been set, do nothing (will be skipped inside as well)
        if _poller is None:
            _poller = TelegramLongPoller()
            _poller.start()
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

