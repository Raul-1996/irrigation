"""Users service (Issue #52).

Thin CRUD layer over the `users` table. Uses werkzeug pbkdf2:sha256 for
password hashing — we explicitly do NOT pull in argon2-cffi (issue
prefers minimal-dependency approach; see Senior #2 angle).

Public API:
    authenticate(username, password)  -> User | None
    get_by_username(username)         -> User | None
    get_by_id(user_id)                -> User | None
    list_users()                      -> list[User]
    create_user(username, password, role) -> User | None
    change_password(user_id, new_password) -> bool
    change_role(user_id, role)        -> bool
    set_active(user_id, active)       -> bool
    mark_login(user_id)               -> None
"""

import logging
import sqlite3

from werkzeug.security import check_password_hash, generate_password_hash

from database import db
from models.user import User

logger = logging.getLogger(__name__)

_VALID_ROLES = {"viewer", "admin"}


def _conn() -> sqlite3.Connection:
    """Open a sqlite3 connection against the singleton db path with WAL + FK."""
    conn = sqlite3.connect(db.db_path, timeout=5)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def get_by_id(user_id: int) -> User | None:
    try:
        with _conn() as c:
            row = c.execute("SELECT * FROM users WHERE id = ? LIMIT 1", (int(user_id),)).fetchone()
            return User.from_row(row) if row else None
    except sqlite3.Error as e:
        logger.error("get_by_id(%s): %s", user_id, e)
        return None


def get_by_username(username: str) -> User | None:
    try:
        with _conn() as c:
            row = c.execute("SELECT * FROM users WHERE username = ? LIMIT 1", (str(username),)).fetchone()
            return User.from_row(row) if row else None
    except sqlite3.Error as e:
        logger.error("get_by_username(%s): %s", username, e)
        return None


def list_users() -> list[User]:
    try:
        with _conn() as c:
            rows = c.execute("SELECT * FROM users ORDER BY id").fetchall()
            return [User.from_row(r) for r in rows]
    except sqlite3.Error as e:
        logger.error("list_users: %s", e)
        return []


def authenticate(username: str, password: str) -> User | None:
    """Return the User if username+password match and account is active. None otherwise.

    NEVER reveals whether the username exists vs the password is wrong — the
    caller produces a generic error message.
    """
    user = get_by_username(username)
    if user is None or not user.is_active:
        return None
    try:
        if check_password_hash(user.password_hash, password):
            return user
    except (ValueError, TypeError) as e:
        logger.debug("authenticate hash compare failed for %s: %s", username, e)
    return None


def create_user(username: str, password: str, role: str = "viewer") -> User | None:
    if role not in _VALID_ROLES:
        logger.warning("create_user: invalid role %r", role)
        return None
    username = (username or "").strip()
    if not username:
        return None
    if not password:
        return None
    pw_hash = generate_password_hash(password, method="pbkdf2:sha256")
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO users(username, password_hash, role, is_active) VALUES (?, ?, ?, 1)",
                (username, pw_hash, role),
            )
            c.commit()
            new_id = cur.lastrowid
        return get_by_id(int(new_id)) if new_id else None
    except sqlite3.IntegrityError:
        logger.info("create_user: username %r already exists", username)
        return None
    except sqlite3.Error as e:
        logger.error("create_user(%s): %s", username, e)
        return None


def change_password(user_id: int, new_password: str) -> bool:
    if not new_password:
        return False
    pw_hash = generate_password_hash(new_password, method="pbkdf2:sha256")
    try:
        with _conn() as c:
            cur = c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, int(user_id)))
            c.commit()
            return cur.rowcount > 0
    except sqlite3.Error as e:
        logger.error("change_password(%s): %s", user_id, e)
        return False


def change_role(user_id: int, role: str) -> bool:
    if role not in _VALID_ROLES:
        return False
    try:
        with _conn() as c:
            cur = c.execute("UPDATE users SET role = ? WHERE id = ?", (role, int(user_id)))
            c.commit()
            return cur.rowcount > 0
    except sqlite3.Error as e:
        logger.error("change_role(%s, %s): %s", user_id, role, e)
        return False


def set_active(user_id: int, active: bool) -> bool:
    try:
        with _conn() as c:
            cur = c.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, int(user_id)))
            c.commit()
            return cur.rowcount > 0
    except sqlite3.Error as e:
        logger.error("set_active(%s, %s): %s", user_id, active, e)
        return False


def mark_login(user_id: int) -> None:
    """Update last_login_at = now(UTC). Best-effort."""
    try:
        with _conn() as c:
            c.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (int(user_id),))
            c.commit()
    except sqlite3.Error as e:
        logger.debug("mark_login(%s): %s", user_id, e)


def count_active_admins() -> int:
    """Used to protect against locking out the last admin."""
    try:
        with _conn() as c:
            row = c.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1").fetchone()
            return int(row[0]) if row else 0
    except sqlite3.Error as e:
        logger.error("count_active_admins: %s", e)
        return 0
