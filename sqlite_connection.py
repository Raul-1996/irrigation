from __future__ import annotations

import sqlite3
from types import TracebackType
from typing import Literal


class ClosingConnection(sqlite3.Connection):
    """SQLite connection that closes after context commit or rollback."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(database: str, timeout: float = 5.0) -> ClosingConnection:
    """Open a context-closing SQLite connection."""
    return sqlite3.connect(database, timeout=timeout, factory=ClosingConnection)
