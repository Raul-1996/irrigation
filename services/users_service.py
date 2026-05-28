"""User service — auth + CRUD helpers used by routes/auth.py and routes/admin_users.py.

B-pillars covered here:
  * B8 — timing-uniform authenticate(): always run check_password_hash even
         on unknown user, against a module-level dummy hash.
  * B9 — server-side username regex validation in create_user.
  * B12 — validate_password / validate_username return (ok, msg) tuples so
          callers can produce proper 400-with-message responses without
          try/except plumbing.
"""

import logging
import re
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from constants import MIN_PASSWORD_LENGTH
from database import db

logger = logging.getLogger(__name__)

# B9: server-side guard. The same regex is enforced client-side in admin UI.
USERNAME_REGEX = re.compile(r"^[a-zA-Z0-9_.-]{1,32}$")
USERNAME_PATTERN = USERNAME_REGEX.pattern

# B8: dummy pbkdf2 hash generated once at module load. authenticate() runs
# check_password_hash against this when the user doesn't exist so the timing
# profile is indistinguishable from a real user + wrong-password attempt.
_DUMMY_HASH: str = generate_password_hash(
    "dummy-for-timing-equalisation", method="pbkdf2:sha256"
)


# ── Validation helpers (tuple-return API used by routes) ───────────────────


def validate_username(username: str) -> tuple[bool, str]:
    """Return (ok, message). message is empty on success."""
    if not isinstance(username, str) or not USERNAME_REGEX.match(username):
        return False, f"username must match {USERNAME_PATTERN}"
    return True, ""


def validate_password(password: str) -> tuple[bool, str]:
    """Return (ok, message). message is empty on success."""
    if not isinstance(password, str):
        return False, "password must be a string"
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(password) > 128:
        return False, "password too long (max 128 characters)"
    return True, ""


# ── Auth ──────────────────────────────────────────────────────────────────


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    """Return the user row on success, None on failure.

    Always performs a password check (against a dummy hash if user is unknown
    or inactive) so timing leaks don't reveal enumeration (B8).

    On success, if the stored hash uses an outdated pbkdf2 iteration count,
    silently re-hash with the current default. This keeps timing uniform for
    future logins (otherwise legacy low-iter hashes vs new high-iter hashes
    leak which users were created when).
    """
    user = None
    try:
        user = db.users.get_by_username(username)
    except Exception as e:
        logger.warning("authenticate: get_by_username(%s) raised %s", username, e)

    if user and int(user.get("is_active") or 0) == 1:
        stored_hash = str(user.get("password_hash") or "")
        ok = check_password_hash(stored_hash, password)
        if ok:
            # B8 lazy upgrade: bring legacy hashes up to current iteration count
            # so the response time stops leaking which users are legacy seeds.
            try:
                if _hash_needs_upgrade(stored_hash):
                    db.users.set_password(int(user["id"]), password)
            except Exception as e:
                logger.debug("authenticate: lazy hash upgrade failed: %s", e)
            return user
        return None

    # User missing or deactivated — still consume time on a dummy hash.
    check_password_hash(_DUMMY_HASH, password)
    return None


def _hash_needs_upgrade(stored: str) -> bool:
    """True if `stored` is a pbkdf2 hash with fewer iterations than current default.

    Hash format: pbkdf2:sha256:<iterations>$<salt>$<hex>.
    Compares against the iteration count baked into `_DUMMY_HASH` so this
    function automatically follows whatever werkzeug's default is at module
    load time.
    """
    try:
        # Extract iteration count from both stored and dummy.
        def _iters(h: str) -> int:
            head = h.split("$", 1)[0]  # "pbkdf2:sha256:1000000"
            return int(head.split(":")[-1])

        return _iters(stored) < _iters(_DUMMY_HASH)
    except (ValueError, IndexError):
        return False


# ── CRUD (tuple-return API) ───────────────────────────────────────────────


def create_user(
    username: str, password: str, role: str
) -> tuple[bool, str, int | None]:
    """Create a user. Returns (ok, message, new_id).

    On failure new_id is None and message describes the problem.
    """
    ok, msg = validate_username(username)
    if not ok:
        return False, msg, None
    ok, msg = validate_password(password)
    if not ok:
        return False, msg, None
    if role not in ("viewer", "admin"):
        return False, "role must be 'viewer' or 'admin'", None
    new_id = db.users.create(username, password, role)
    if new_id is None:
        return False, "username already exists", None
    return True, "", new_id


def change_password(user_id: int, new_password: str) -> tuple[bool, str]:
    """Self-service password change. Caller must already be authenticated."""
    ok, msg = validate_password(new_password)
    if not ok:
        return False, msg
    if not db.users.set_password(int(user_id), new_password):
        return False, "user not found"
    return True, ""


def list_users() -> list[dict[str, Any]]:
    return db.users.list_all()


def delete_user(user_id: int) -> bool:
    return db.users.delete(int(user_id))


def set_role(user_id: int, role: str) -> tuple[bool, str]:
    if role not in ("viewer", "admin"):
        return False, "role must be 'viewer' or 'admin'"
    if not db.users.set_role(int(user_id), role):
        return False, "user not found"
    return True, ""


def set_active(user_id: int, is_active: bool) -> bool:
    return db.users.set_active(int(user_id), bool(is_active))


def get_user(user_id: int) -> dict[str, Any] | None:
    return db.users.get_by_id(int(user_id))
