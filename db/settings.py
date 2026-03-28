import sqlite3
import logging
from typing import Dict, Any, Optional

from werkzeug.security import generate_password_hash

from db.base import BaseRepository, retry_on_busy

logger = logging.getLogger(__name__)


class SettingsRepository(BaseRepository):
    """Repository for settings, configs, and password management."""

    def get_setting_value(self, key: str) -> Optional[str]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT value FROM settings WHERE key = ? LIMIT 1', (key,))
                row = cur.fetchone()
                return str(row['value']) if row and row['value'] is not None else None
        except sqlite3.Error as e:
            logger.error("Ошибка чтения settings[%s]: %s", key, e)
            return None

    @retry_on_busy()
    def set_setting_value(self, key: str, value: Optional[str]) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                if value is None:
                    conn.execute('DELETE FROM settings WHERE key = ?', (key,))
                else:
                    conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', (key, str(value)))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка записи settings[%s]: %s", key, e)
            return False

    @retry_on_busy()
    def ensure_password_change_required(self) -> None:
        """Если пароль не установлен — генерируем случайный временный пароль и требуем смену."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT value FROM settings WHERE key = ? LIMIT 1', ('password_hash',))
                row = cur.fetchone()
                if not row:
                    import secrets
                    temp_password = secrets.token_urlsafe(12)
                    pw_hash = generate_password_hash(temp_password, method='pbkdf2:sha256')
                    conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', ('password_hash', pw_hash))
                    conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', ('password_must_change', '1'))
                    logger.warning("Initial random password generated: %s (change it on first login!)", temp_password)
                else:
                    cur2 = conn.execute('SELECT value FROM settings WHERE key = ? LIMIT 1', ('password_must_change',))
                    row2 = cur2.fetchone()
                    if not row2:
                        conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', ('password_must_change', '1'))
                conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка установки флага обязательной смены пароля: %s", e)

    def get_logging_debug(self) -> bool:
        val = self.get_setting_value('logging.debug')
        return str(val or '0') in ('1', 'true', 'True')

    @retry_on_busy()
    def set_logging_debug(self, enabled: bool) -> bool:
        return self.set_setting_value('logging.debug', '1' if enabled else '0')

    def get_rain_config(self) -> Dict[str, Any]:
        """Глобальная конфигурация датчика дождя."""
        enabled = self.get_setting_value('rain.enabled')
        topic = self.get_setting_value('rain.topic') or ''
        sensor_type = self.get_setting_value('rain.type') or 'NO'
        server_id = self.get_setting_value('rain.server_id')
        return {
            'enabled': str(enabled or '0') in ('1', 'true', 'True'),
            'topic': topic,
            'type': sensor_type if sensor_type in ('NO', 'NC') else 'NO',
            'server_id': int(server_id) if server_id and str(server_id).isdigit() else None,
        }

    @retry_on_busy()
    def set_rain_config(self, cfg: Dict[str, Any]) -> bool:
        ok = True
        ok &= self.set_setting_value('rain.enabled', '1' if cfg.get('enabled') else '0')
        if 'topic' in cfg:
            ok &= self.set_setting_value('rain.topic', cfg.get('topic') or '')
        if 'type' in cfg:
            t = cfg.get('type')
            ok &= self.set_setting_value('rain.type', t if t in ('NO', 'NC') else 'NO')
        if 'server_id' in cfg:
            sid = cfg.get('server_id')
            ok &= self.set_setting_value('rain.server_id', str(int(sid)) if sid is not None else None)
        return bool(ok)

    def get_master_config(self) -> Dict[str, Any]:
        try:
            enabled = self.get_setting_value('master.enabled')
            topic = self.get_setting_value('master.topic') or ''
            server_id = self.get_setting_value('master.server_id')
            delay_ms = self.get_setting_value('master.delay_ms')
            return {
                'enabled': str(enabled or '0') in ('1', 'true', 'True'),
                'topic': topic,
                'server_id': int(server_id) if server_id and str(server_id).isdigit() else None,
                'delay_ms': int(delay_ms) if (delay_ms and str(delay_ms).isdigit()) else 300
            }
        except (ValueError, TypeError) as e:
            logger.error("Ошибка чтения master_config: %s", e)
            return {'enabled': False, 'topic': '', 'server_id': None, 'delay_ms': 300}

    @retry_on_busy()
    def set_master_config(self, cfg: Dict[str, Any]) -> bool:
        ok = True
        try:
            ok &= self.set_setting_value('master.enabled', '1' if cfg.get('enabled') else '0')
            if 'topic' in cfg:
                ok &= self.set_setting_value('master.topic', cfg.get('topic') or '')
            if 'server_id' in cfg:
                sid = cfg.get('server_id')
                ok &= self.set_setting_value('master.server_id', str(int(sid)) if sid is not None else None)
            if 'delay_ms' in cfg:
                ok &= self.set_setting_value('master.delay_ms', str(int(cfg.get('delay_ms') or 300)))
            return bool(ok)
        except (ValueError, TypeError) as e:
            logger.error("Ошибка записи master_config: %s", e)
            return False

    def get_env_config(self) -> Dict[str, Any]:
        temp_enabled = self.get_setting_value('env.temp.enabled')
        temp_topic = self.get_setting_value('env.temp.topic') or ''
        temp_server_id = self.get_setting_value('env.temp.server_id')
        hum_enabled = self.get_setting_value('env.hum.enabled')
        hum_topic = self.get_setting_value('env.hum.topic') or ''
        hum_server_id = self.get_setting_value('env.hum.server_id')
        return {
            'temp': {
                'enabled': str(temp_enabled or '0') in ('1', 'true', 'True'),
                'topic': temp_topic,
                'server_id': int(temp_server_id) if temp_server_id and str(temp_server_id).isdigit() else None,
            },
            'hum': {
                'enabled': str(hum_enabled or '0') in ('1', 'true', 'True'),
                'topic': hum_topic,
                'server_id': int(hum_server_id) if hum_server_id and str(hum_server_id).isdigit() else None,
            }
        }

    @retry_on_busy()
    def set_env_config(self, cfg: Dict[str, Any]) -> bool:
        ok = True
        temp = cfg.get('temp') or {}
        hum = cfg.get('hum') or {}
        ok &= self.set_setting_value('env.temp.enabled', '1' if temp.get('enabled') else '0')
        ok &= self.set_setting_value('env.temp.topic', temp.get('topic') or '')
        ok &= self.set_setting_value('env.temp.server_id',
                                     str(int(temp.get('server_id'))) if temp.get('server_id') is not None else None)
        ok &= self.set_setting_value('env.hum.enabled', '1' if hum.get('enabled') else '0')
        ok &= self.set_setting_value('env.hum.topic', hum.get('topic') or '')
        ok &= self.set_setting_value('env.hum.server_id',
                                     str(int(hum.get('server_id'))) if hum.get('server_id') is not None else None)
        return bool(ok)

    # === Password ===
    def get_password_hash(self) -> Optional[str]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute('SELECT value FROM settings WHERE key = ? LIMIT 1', ('password_hash',))
                row = cur.fetchone()
                return row[0] if row else None
        except sqlite3.Error as e:
            logger.error("Ошибка чтения пароля: %s", e)
            return None

    @retry_on_busy()
    def set_password(self, new_password: str) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', (
                    'password_hash', generate_password_hash(new_password, method='pbkdf2:sha256')
                ))
                conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', (
                    'password_must_change', '0'
                ))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка обновления пароля: %s", e)
            return False

    # === Early off seconds ===
    def get_early_off_seconds(self) -> int:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute('SELECT value FROM settings WHERE key = ? LIMIT 1', ('early_off_seconds',))
                row = cur.fetchone()
                val = int(row[0]) if row and row[0] is not None else 3
                if val < 0: val = 0
                if val > 15: val = 15
                return val
        except (sqlite3.Error, ValueError, TypeError) as e:
            logger.error("Ошибка чтения early_off_seconds: %s", e)
            return 3

    @retry_on_busy()
    def set_early_off_seconds(self, seconds: int) -> bool:
        try:
            val = int(seconds)
            if val < 0: val = 0
            if val > 15: val = 15
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', (
                    'early_off_seconds', str(val)
                ))
                conn.commit()
            return True
        except (sqlite3.Error, ValueError, TypeError) as e:
            logger.error("Ошибка записи early_off_seconds: %s", e)
            return False
