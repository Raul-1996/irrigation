"""User repository — backing store for in-app auth (B-pillars, 2026-05-28).

Schema created by MigrationRunner._migrate_create_users:
    users(id PK, username UNIQUE NOT NULL, password_hash TEXT NOT NULL,
          role TEXT IN ('viewer','admin'), created_at, last_login_at,
          is_active INTEGER NOT NULL DEFAULT 1)
"""

import logging
import sqlite3
from typing import Any

from werkzeug.security import generate_password_hash

from db.base import BaseRepository, retry_on_busy

logger = logging.getLogger(__name__)


class UsersRepository(BaseRepository):
    """CRUD on the ``users`` table."""

    def get_by_username(self, username: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT id, username, password_hash, role, created_at, "
                    "last_login_at, is_active FROM users WHERE username = ? LIMIT 1",
                    (username,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        except sqlite3.Error as e:
            logger.error("users.get_by_username(%s): %s", username, e)
            return None

    def get_by_id(self, user_id: int) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT id, username, password_hash, role, created_at, "
                    "last_login_at, is_active FROM users WHERE id = ? LIMIT 1",
                    (int(user_id),),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        except sqlite3.Error as e:
            logger.error("users.get_by_id(%s): %s", user_id, e)
            return None

    def list_all(self) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT id, username, role, created_at, last_login_at, is_active FROM users ORDER BY id"
                )
                return [dict(r) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("users.list_all: %s", e)
            return []

    @retry_on_busy()
    def create(self, username: str, password: str, role: str) -> int | None:
        """Insert a user. Returns new id, or None on UNIQUE conflict / error."""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO users(username, password_hash, role, is_active) VALUES (?, ?, ?, 1)",
                    (username, generate_password_hash(password, method="pbkdf2:sha256"), role),
                )
                conn.commit()
                return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None
        except sqlite3.Error as e:
            logger.error("users.create(%s): %s", username, e)
            return None

    @retry_on_busy()
    def set_password(self, user_id: int, new_password: str) -> bool:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(new_password, method="pbkdf2:sha256"), int(user_id)),
                )
                conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error("users.set_password(%s): %s", user_id, e)
            return False

    @retry_on_busy()
    def set_role(self, user_id: int, role: str) -> bool:
        if role not in ("viewer", "admin"):
            return False
        try:
            with self._connect() as conn:
                cur = conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, int(user_id)))
                conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error("users.set_role(%s): %s", user_id, e)
            return False

    @retry_on_busy()
    def set_active(self, user_id: int, is_active: bool) -> bool:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE users SET is_active = ? WHERE id = ?",
                    (1 if is_active else 0, int(user_id)),
                )
                conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error("users.set_active(%s): %s", user_id, e)
            return False

    @retry_on_busy()
    def delete(self, user_id: int) -> bool:
        try:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
                conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error("users.delete(%s): %s", user_id, e)
            return False

    @retry_on_busy()
    def touch_last_login(self, user_id: int) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (int(user_id),),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.debug("users.touch_last_login(%s): %s", user_id, e)
