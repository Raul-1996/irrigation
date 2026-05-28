"""User model (Issue #52).

Plain dataclass — no ORM. Matches the `users` table created by
db.migrations._migrate_create_users.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class User:
    id: int
    username: str
    password_hash: str
    role: str  # 'viewer' | 'admin'
    created_at: str | None
    last_login_at: str | None
    is_active: bool

    @classmethod
    def from_row(cls, row: Any) -> "User":
        """Build from a sqlite3.Row or tuple-like with the standard column order."""
        return cls(
            id=int(row["id"]),
            username=str(row["username"]),
            password_hash=str(row["password_hash"]),
            role=str(row["role"]),
            created_at=row["created_at"] if row["created_at"] else None,
            last_login_at=row["last_login_at"] if row["last_login_at"] else None,
            is_active=bool(row["is_active"]),
        )

    def to_dict(self, *, include_hash: bool = False) -> dict[str, Any]:
        """Serialise for JSON responses. password_hash is NEVER returned by default."""
        d = {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
            "is_active": self.is_active,
        }
        if include_hash:
            d["password_hash"] = self.password_hash
        return d
