import sqlite3
import functools
import time
import logging
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar('F', bound=Callable[..., Any])


def retry_on_busy(max_retries: int = 3, initial_backoff: float = 0.1) -> Callable[[F], F]:
    """Decorator to retry SQLite operations on 'database is locked' errors."""
    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if 'database is locked' in str(e) and attempt < max_retries:
                        time.sleep(initial_backoff * (2 ** attempt))
                        logger.warning("SQLite BUSY retry %d/%d for %s", attempt + 1, max_retries, func.__name__)
                    else:
                        raise
        return wrapper  # type: ignore[return-value]
    return decorator


class BaseRepository:
    """Base class for all database repositories."""

    def __init__(self, db_path: str) -> None:
        self.db_path: str = db_path

    def _connect(self) -> sqlite3.Connection:
        """Create a new SQLite connection with WAL mode and foreign keys."""
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.row_factory = sqlite3.Row
        return conn
