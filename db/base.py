import sqlite3
import functools
import time
import logging

logger = logging.getLogger(__name__)


def retry_on_busy(max_retries=3, initial_backoff=0.1):
    """Decorator to retry SQLite operations on 'database is locked' errors."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if 'database is locked' in str(e) and attempt < max_retries:
                        time.sleep(initial_backoff * (2 ** attempt))
                        logger.warning("SQLite BUSY retry %d/%d for %s", attempt + 1, max_retries, func.__name__)
                    else:
                        raise
        return wrapper
    return decorator


class BaseRepository:
    """Base class for all database repositories."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self):
        """Create a new SQLite connection with WAL mode and foreign keys."""
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.row_factory = sqlite3.Row
        return conn
