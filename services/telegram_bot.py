from typing import Optional
import logging
import threading
from utils import decrypt_secret
from database import db

logger = logging.getLogger(__name__)

class TelegramNotifier:
    def __init__(self):
        self._bot = None
        self._lock = threading.Lock()

    def _ensure_bot(self):
        try:
            if self._bot is not None:
                return self._bot
            tok_enc = db.get_setting_value('telegram_bot_token_encrypted')
            if not tok_enc:
                return None
            token = decrypt_secret(tok_enc)
            if not token:
                return None
            from telegram import Bot
            self._bot = Bot(token=token)
            return self._bot
        except Exception as e:
            logger.error(f"TelegramNotifier ensure_bot failed: {e}")
            return None

    def send_text(self, chat_id: int, text: str) -> bool:
        try:
            bot = self._ensure_bot()
            if not bot:
                return False
            bot.send_message(chat_id=chat_id, text=text)
            return True
        except Exception as e:
            logger.error(f"TelegramNotifier send_text failed: {e}")
            return False

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
            txt = None
            if t == 'zone_start':
                txt = f"▶ Зона {ev.get('id')} запущена ({ev.get('by','')})"
            elif t == 'zone_stop':
                txt = f"⏹ Зона {ev.get('id')} остановлена ({ev.get('by','')})"
            elif t == 'group_start':
                txt = f"▶ Группа {ev.get('id')} запущена ({ev.get('by','')})"
            elif t == 'group_stop':
                txt = f"⏹ Группа {ev.get('id')} остановлена ({ev.get('by','')})"
            elif t == 'emergency_on':
                txt = f"🚨 Аварийная остановка инициирована ({ev.get('by','')})"
            elif t == 'emergency_off':
                txt = f"✅ Аварийная остановка снята ({ev.get('by','')})"
            if txt:
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

