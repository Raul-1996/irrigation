import sqlite3
import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from db.base import BaseRepository, retry_on_busy

logger = logging.getLogger(__name__)


class TelegramRepository(BaseRepository):
    """Repository for bot_users, subscriptions, audit, FSM, idempotency."""

    # --- Bot users ---
    def get_bot_user_by_chat(self, chat_id: int) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT * FROM bot_users WHERE chat_id = ? LIMIT 1', (int(chat_id),))
                row = cur.fetchone()
                return dict(row) if row else None
        except sqlite3.Error as e:
            logger.error("Ошибка чтения bot_user chat_id=%s: %s", chat_id, e)
            return None

    @retry_on_busy()
    def upsert_bot_user(self, chat_id: int, username: Optional[str], first_name: Optional[str]) -> bool:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute('''
                    INSERT INTO bot_users(chat_id, username, first_name, created_at)
                    VALUES(?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_seen_at=CURRENT_TIMESTAMP
                ''', (int(chat_id), username, first_name))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка upsert bot_user chat_id=%s: %s", chat_id, e)
            return False

    @retry_on_busy()
    def set_bot_user_authorized(self, chat_id: int, role: str = 'user') -> bool:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute(
                    'UPDATE bot_users SET is_authorized=1, role=?, failed_attempts=0, locked_until=NULL, last_seen_at=CURRENT_TIMESTAMP WHERE chat_id=?',
                    (str(role), int(chat_id)))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка авторизации bot_user chat_id=%s: %s", chat_id, e)
            return False

    @retry_on_busy()
    def inc_bot_user_failed(self, chat_id: int) -> int:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute(
                    'UPDATE bot_users SET failed_attempts=COALESCE(failed_attempts,0)+1, last_seen_at=CURRENT_TIMESTAMP WHERE chat_id=?',
                    (int(chat_id),))
                conn.commit()
                cur = conn.execute('SELECT failed_attempts FROM bot_users WHERE chat_id=?', (int(chat_id),))
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error as e:
            logger.error("Ошибка инкремента failed_attempts chat_id=%s: %s", chat_id, e)
            return 0

    @retry_on_busy()
    def lock_bot_user_until(self, chat_id: int, until_iso: str) -> bool:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute('UPDATE bot_users SET locked_until=? WHERE chat_id=?', (str(until_iso), int(chat_id)))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка блокировки bot_user chat_id=%s: %s", chat_id, e)
            return False

    # --- FSM ---
    @retry_on_busy()
    def set_bot_fsm(self, chat_id: int, state: Optional[str], data: Optional[dict]) -> bool:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                try:
                    payload = None if data is None else json.dumps(data, ensure_ascii=False)
                except (TypeError, ValueError) as e:
                    logger.debug("set_user_state JSON encode: %s", e)
                    payload = None
                conn.execute(
                    'UPDATE bot_users SET fsm_state=?, fsm_data=?, last_seen_at=CURRENT_TIMESTAMP WHERE chat_id=?',
                    (None if state is None else str(state), payload, int(chat_id))
                )
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка установки FSM chat_id=%s: %s", chat_id, e)
            return False

    def get_bot_fsm(self, chat_id: int) -> tuple:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT fsm_state, fsm_data FROM bot_users WHERE chat_id=?', (int(chat_id),))
                row = cur.fetchone()
                if not row:
                    return None, None
                st = row['fsm_state']
                data = None
                try:
                    data = json.loads(row['fsm_data']) if row['fsm_data'] else None
                except (json.JSONDecodeError, TypeError) as e:
                    logger.debug("get_user_state JSON decode: %s", e)
                    data = None
                return st, data
        except sqlite3.Error as e:
            logger.error("Ошибка чт��ния FSM chat_id=%s: %s", chat_id, e)
            return None, None

    # --- Idempotency tokens ---
    @retry_on_busy()
    def is_new_idempotency_token(self, token: str, chat_id: int, action: str, ttl_seconds: int = 600) -> bool:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                try:
                    conn.execute('DELETE FROM bot_idempotency WHERE created_at < datetime("now", ?)',
                                 (f'-{int(ttl_seconds)} seconds',))
                except sqlite3.Error as e:
                    logger.debug("Ошибка очистки старых идемпотентных токенов: %s", e)
                try:
                    conn.execute('INSERT INTO bot_idempotency(token, chat_id, action) VALUES(?,?,?)',
                                 (str(token), int(chat_id), str(action)))
                    conn.commit()
                    return True
                except sqlite3.IntegrityError:
                    logger.debug("idempotency token already exists: %s", token)
                    return False
        except sqlite3.Error as e:
            logger.error("Ошибка записи идемпотентного токена %s: %s", token, e)
            return False

    # --- Notification toggles ---
    def get_bot_user_notif_settings(self, chat_id: int) -> dict:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('''
                    SELECT notif_critical, notif_emergency, notif_postpone, notif_zone_events, notif_rain
                    FROM bot_users WHERE chat_id=? LIMIT 1
                ''', (int(chat_id),))
                row = cur.fetchone()
                if not row:
                    return {}
                return {
                    'critical': int(row['notif_critical'] or 0),
                    'emergency': int(row['notif_emergency'] or 0),
                    'postpone': int(row['notif_postpone'] or 0),
                    'zone_events': int(row['notif_zone_events'] or 0),
                    'rain': int(row['notif_rain'] or 0),
                }
        except sqlite3.Error as e:
            logger.error("Ошибка чтения настроек уведомлений chat_id=%s: %s", chat_id, e)
            return {}

    @retry_on_busy()
    def set_bot_user_notif_toggle(self, chat_id: int, key: str, enabled: bool) -> bool:
        allowed = {
            'critical': 'notif_critical',
            'emergency': 'notif_emergency',
            'postpone': 'notif_postpone',
            'zone_events': 'notif_zone_events',
            'rain': 'notif_rain',
        }
        col = allowed.get(key)
        if not col:
            return False
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute(f'UPDATE bot_users SET {col}=? WHERE chat_id=?', (1 if enabled else 0, int(chat_id)))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка сохранения тумблера уведомлений %s chat_id=%s: %s", key, chat_id, e)
            return False

    # --- Subscriptions ---
    def get_due_bot_subscriptions(self, now_local: datetime) -> List[Dict[str, Any]]:
        try:
            hhmm = now_local.strftime('%H:%M')
            dow = now_local.weekday()
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('''
                    SELECT bs.*, bu.chat_id FROM bot_subscriptions bs
                    JOIN bot_users bu ON bu.id = bs.user_id
                    WHERE bs.enabled=1 AND bs.time_local=?
                ''', (hhmm,))
                out = []
                for r in cur.fetchall():
                    rec = dict(r)
                    if str(rec.get('type')) == 'weekly':
                        mask = (rec.get('dow_mask') or '').strip()
                        if not mask:
                            continue
                        try:
                            ok = mask[dow] == '1'
                        except (IndexError, TypeError) as e:
                            logger.debug("dow_mask check failed for reminder: %s", e)
                            ok = False
                        if not ok:
                            continue
                    out.append(rec)
                return out
        except sqlite3.Error as e:
            logger.error("Ошибка получения due подписок: %s", e)
            return []

    @retry_on_busy()
    def create_or_update_subscription(self, user_id: int, sub_type: str, fmt: str, time_local: str,
                                      dow_mask: Optional[str], enabled: bool = True) -> bool:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cur = conn.execute('SELECT id FROM bot_subscriptions WHERE user_id=? AND type=?',
                                   (int(user_id), str(sub_type)))
                row = cur.fetchone()
                if row:
                    conn.execute(
                        'UPDATE bot_subscriptions SET format=?, time_local=?, dow_mask=?, enabled=? WHERE id=?',
                        (str(fmt), str(time_local), (dow_mask or ''), 1 if enabled else 0, int(row[0])))
                else:
                    conn.execute(
                        'INSERT INTO bot_subscriptions(user_id, type, format, time_local, dow_mask, enabled) VALUES(?,?,?,?,?,?)',
                        (int(user_id), str(sub_type), str(fmt), str(time_local), (dow_mask or ''),
                         1 if enabled else 0))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка сохранения подписки: %s", e)
            return False
