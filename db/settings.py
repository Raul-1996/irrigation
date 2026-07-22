import logging
import os
import secrets
import sqlite3
from typing import Any

from werkzeug.security import generate_password_hash

from constants import MIN_PASSWORD_LENGTH
from db.base import BaseRepository, retry_on_busy
from utils import create_private_file, ensure_private_directory, ensure_private_file, read_private_file

logger = logging.getLogger(__name__)

_MAX_PASSWORD_LENGTH = 32
_PASSWORD_BLOCKLIST = {"1234", "12345678", "0000", "password", "admin", "qwerty"}
_BOOTSTRAP_PASSWORD_FILE = ".initial_admin_password"


def normalize_password(password: str) -> str:
    """Apply the same edge-whitespace contract used by the login endpoint."""

    if not isinstance(password, str):
        raise TypeError("password must be a string")
    return password.strip()


class SettingsRepository(BaseRepository):
    """Repository for settings, configs, and password management."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        if self._uses_filesystem_database:
            # IrrigationDB constructs SettingsRepository before running schema
            # migrations.  Pre-creating the database as 0600 closes the umask
            # window for the DB and for SQLite sidecars derived from its mode.
            ensure_private_file(self.db_path, create=True)

    @property
    def _uses_filesystem_database(self) -> bool:
        path = str(self.db_path)
        return bool(path) and path != ":memory:" and not path.startswith("file:")

    def _connect(self) -> sqlite3.Connection:
        connection = super()._connect()
        if not self._uses_filesystem_database:
            return connection
        try:
            for path in (self.db_path, f"{self.db_path}-wal", f"{self.db_path}-shm"):
                if os.path.lexists(path):
                    ensure_private_file(path)
        except OSError:
            connection.close()
            raise
        return connection

    def _bootstrap_password_path(self) -> str:
        if self._uses_filesystem_database:
            database_directory = os.path.dirname(os.path.abspath(self.db_path))
        else:
            database_directory = os.getcwd()
        return os.path.join(database_directory, "backups", _BOOTSTRAP_PASSWORD_FILE)

    def _load_or_create_bootstrap_password(self) -> tuple[str, str]:
        recovery_path = self._bootstrap_password_path()
        ensure_private_directory(os.path.dirname(recovery_path))
        try:
            raw_password = read_private_file(recovery_path)
        except FileNotFoundError:
            generated = secrets.token_urlsafe(12)
            try:
                create_private_file(recovery_path, generated.encode("utf-8"))
                return generated, recovery_path
            except FileExistsError:
                raw_password = read_private_file(recovery_path)

        try:
            password = raw_password.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise RuntimeError("Initial admin password recovery file is invalid; restore or remove it") from error
        if len(password) < MIN_PASSWORD_LENGTH:
            raise RuntimeError("Initial admin password recovery file is invalid; restore or remove it")
        return password, recovery_path

    def _remove_bootstrap_password_file(self) -> None:
        recovery_path = self._bootstrap_password_path()
        try:
            os.unlink(recovery_path)
        except FileNotFoundError:
            return
        except OSError as error:
            # The password hash has already changed, so returning False would
            # falsely report a failed update.  Keep any stale recovery material
            # owner-only and surface a safe operational warning instead.
            try:
                ensure_private_file(recovery_path)
            except OSError:
                pass
            logger.warning("Unable to remove obsolete initial admin password recovery file: %s", error)

    def get_setting_value(self, key: str) -> str | None:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute("SELECT value FROM settings WHERE key = ? LIMIT 1", (key,))
                row = cur.fetchone()
                return str(row["value"]) if row and row["value"] is not None else None
        except sqlite3.Error as e:
            logger.error("Ошибка чтения settings[%s]: %s", key, e)
            return None

    @retry_on_busy()
    def set_setting_value(self, key: str, value: str | None) -> bool:
        try:
            with self._connect() as conn:
                if value is None:
                    conn.execute("DELETE FROM settings WHERE key = ?", (key,))
                else:
                    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, str(value)))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка записи settings[%s]: %s", key, e)
            return False

    @retry_on_busy()
    def ensure_password_change_required(self) -> None:
        """Если пароль не установлен — генерируем случайный временный пароль и требуем смену."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute("SELECT value FROM settings WHERE key = ? LIMIT 1", ("password_hash",))
                row = cur.fetchone()
                if not row:
                    temp_password, recovery_path = self._load_or_create_bootstrap_password()
                    pw_hash = generate_password_hash(temp_password, method="pbkdf2:sha256")
                    conn.execute(
                        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", ("password_hash", pw_hash)
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", ("password_must_change", "1")
                    )
                    logger.warning(
                        "Initial admin password is available only in private recovery file %s; "
                        "change it on first login",
                        recovery_path,
                    )
                else:
                    cur2 = conn.execute("SELECT value FROM settings WHERE key = ? LIMIT 1", ("password_must_change",))
                    row2 = cur2.fetchone()
                    if not row2:
                        conn.execute(
                            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", ("password_must_change", "1")
                        )
                conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка установки флага обязательной смены пароля: %s", e)

    def get_logging_debug(self) -> bool:
        val = self.get_setting_value("logging.debug")
        return str(val or "0") in ("1", "true", "True")

    @retry_on_busy()
    def set_logging_debug(self, enabled: bool) -> bool:
        return self.set_setting_value("logging.debug", "1" if enabled else "0")

    def get_rain_config(self) -> dict[str, Any]:
        """Глобальная конфигурация датчика дождя."""
        enabled = self.get_setting_value("rain.enabled")
        topic = self.get_setting_value("rain.topic") or ""
        sensor_type = self.get_setting_value("rain.type") or "NO"
        server_id = self.get_setting_value("rain.server_id")
        return {
            "enabled": str(enabled or "0") in ("1", "true", "True"),
            "topic": topic,
            "type": sensor_type if sensor_type in ("NO", "NC") else "NO",
            "server_id": int(server_id) if server_id and str(server_id).isdigit() else None,
        }

    @retry_on_busy()
    def set_rain_config(self, cfg: dict[str, Any]) -> bool:
        ok = True
        ok &= self.set_setting_value("rain.enabled", "1" if cfg.get("enabled") else "0")
        if "topic" in cfg:
            ok &= self.set_setting_value("rain.topic", cfg.get("topic") or "")
        if "type" in cfg:
            t = cfg.get("type")
            ok &= self.set_setting_value("rain.type", t if t in ("NO", "NC") else "NO")
        if "server_id" in cfg:
            sid = cfg.get("server_id")
            ok &= self.set_setting_value("rain.server_id", str(int(sid)) if sid is not None else None)
        return bool(ok)

    def get_master_config(self) -> dict[str, Any]:
        try:
            enabled = self.get_setting_value("master.enabled")
            topic = self.get_setting_value("master.topic") or ""
            server_id = self.get_setting_value("master.server_id")
            delay_ms = self.get_setting_value("master.delay_ms")
            return {
                "enabled": str(enabled or "0") in ("1", "true", "True"),
                "topic": topic,
                "server_id": int(server_id) if server_id and str(server_id).isdigit() else None,
                "delay_ms": int(delay_ms) if (delay_ms and str(delay_ms).isdigit()) else 300,
            }
        except (ValueError, TypeError) as e:
            logger.error("Ошибка чтения master_config: %s", e)
            return {"enabled": False, "topic": "", "server_id": None, "delay_ms": 300}

    @retry_on_busy()
    def set_master_config(self, cfg: dict[str, Any]) -> bool:
        ok = True
        try:
            ok &= self.set_setting_value("master.enabled", "1" if cfg.get("enabled") else "0")
            if "topic" in cfg:
                ok &= self.set_setting_value("master.topic", cfg.get("topic") or "")
            if "server_id" in cfg:
                sid = cfg.get("server_id")
                ok &= self.set_setting_value("master.server_id", str(int(sid)) if sid is not None else None)
            if "delay_ms" in cfg:
                ok &= self.set_setting_value("master.delay_ms", str(int(cfg.get("delay_ms") or 300)))
            return bool(ok)
        except (ValueError, TypeError) as e:
            logger.error("Ошибка записи master_config: %s", e)
            return False

    def get_env_config(self) -> dict[str, Any]:
        temp_enabled = self.get_setting_value("env.temp.enabled")
        temp_topic = self.get_setting_value("env.temp.topic") or ""
        temp_server_id = self.get_setting_value("env.temp.server_id")
        hum_enabled = self.get_setting_value("env.hum.enabled")
        hum_topic = self.get_setting_value("env.hum.topic") or ""
        hum_server_id = self.get_setting_value("env.hum.server_id")
        return {
            "temp": {
                "enabled": str(temp_enabled or "0") in ("1", "true", "True"),
                "topic": temp_topic,
                "server_id": int(temp_server_id) if temp_server_id and str(temp_server_id).isdigit() else None,
            },
            "hum": {
                "enabled": str(hum_enabled or "0") in ("1", "true", "True"),
                "topic": hum_topic,
                "server_id": int(hum_server_id) if hum_server_id and str(hum_server_id).isdigit() else None,
            },
        }

    @retry_on_busy()
    def set_env_config(self, cfg: dict[str, Any]) -> bool:
        try:
            if not isinstance(cfg, dict):
                raise TypeError("env config must be an object")

            updates: list[tuple[str, str | None]] = []
            for sensor in ("temp", "hum"):
                sensor_cfg = cfg.get(sensor)
                if sensor_cfg is None:
                    sensor_cfg = {}
                if not isinstance(sensor_cfg, dict):
                    raise TypeError(f"env.{sensor} config must be an object")

                server_id = sensor_cfg.get("server_id")
                if isinstance(server_id, bool):
                    raise ValueError(f"env.{sensor}.server_id must be an integer")
                if isinstance(server_id, float) and not server_id.is_integer():
                    raise ValueError(f"env.{sensor}.server_id must be an integer")
                normalized_server_id = str(int(server_id)) if server_id is not None else None

                updates.extend(
                    (
                        (f"env.{sensor}.enabled", "1" if sensor_cfg.get("enabled") else "0"),
                        (f"env.{sensor}.topic", str(sensor_cfg.get("topic") or "")),
                        (f"env.{sensor}.server_id", normalized_server_id),
                    )
                )
        except (TypeError, ValueError) as e:
            logger.error("Ошибка валидации env_config: %s", e)
            return False

        try:
            with self._connect() as conn:
                for key, value in updates:
                    if value is None:
                        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
                    else:
                        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("Ошибка записи env_config: %s", e)
            return False

    # === Password ===
    def get_password_hash(self) -> str | None:
        try:
            with self._connect() as conn:
                cur = conn.execute("SELECT value FROM settings WHERE key = ? LIMIT 1", ("password_hash",))
                row = cur.fetchone()
                return row[0] if row else None
        except sqlite3.Error as e:
            logger.error("Ошибка чтения пароля: %s", e)
            return None

    @retry_on_busy()
    def set_password(self, new_password: str) -> bool:
        try:
            normalized_password = normalize_password(new_password)
        except TypeError:
            logger.warning("Rejected invalid admin password value")
            return False
        if not (MIN_PASSWORD_LENGTH <= len(normalized_password) <= _MAX_PASSWORD_LENGTH):
            logger.warning("Rejected admin password outside the configured length policy")
            return False
        if normalized_password.casefold() in _PASSWORD_BLOCKLIST:
            logger.warning("Rejected blocklisted admin password")
            return False
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                    ("password_hash", generate_password_hash(normalized_password, method="pbkdf2:sha256")),
                )
                conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", ("password_must_change", "0"))
                conn.commit()
                self._remove_bootstrap_password_file()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка обновления пароля: %s", e)
            return False

    # === Early off seconds ===
    def get_early_off_seconds(self) -> int:
        try:
            with self._connect() as conn:
                cur = conn.execute("SELECT value FROM settings WHERE key = ? LIMIT 1", ("early_off_seconds",))
                row = cur.fetchone()
                val = int(row[0]) if row and row[0] is not None else 3
                if val < 0:
                    val = 0
                if val > 15:
                    val = 15
                return val
        except (sqlite3.Error, ValueError, TypeError) as e:
            logger.error("Ошибка чтения early_off_seconds: %s", e)
            return 3

    @retry_on_busy()
    def set_early_off_seconds(self, seconds: int) -> bool:
        try:
            val = int(seconds)
            if val < 0:
                val = 0
            if val > 15:
                val = 15
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", ("early_off_seconds", str(val))
                )
                conn.commit()
            return True
        except (sqlite3.Error, ValueError, TypeError) as e:
            logger.error("Ошибка записи early_off_seconds: %s", e)
            return False
