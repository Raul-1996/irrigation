import functools
import logging
import sqlite3
import time
from contextvars import ContextVar
from typing import Any, Callable, NoReturn, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])
_RETRY_ON_BUSY_ACTIVE: ContextVar[bool] = ContextVar("db_retry_on_busy_active", default=False)


def _is_busy_error(error: sqlite3.OperationalError) -> bool:
    """Return whether ``error`` represents retryable SQLite contention."""

    code = getattr(error, "sqlite_errorcode", None)
    return code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or "locked" in str(error).lower()


class _RetryableBusyError(RuntimeError):
    """BUSY escaped from SQLite without matching repository sqlite catches.

    Repository methods historically catch :class:`sqlite3.Error` inside the
    ``@retry_on_busy`` boundary.  Translating only BUSY/LOCKED at the connection
    edge lets the decorator see contention while all other SQLite failures keep
    their established repository return-value contracts.
    """

    def __init__(self, original: sqlite3.OperationalError) -> None:
        super().__init__(str(original))
        self.original = original


def _raise_translated(error: sqlite3.OperationalError) -> NoReturn:
    if _is_busy_error(error) and _RETRY_ON_BUSY_ACTIVE.get():
        raise _RetryableBusyError(error) from error
    raise error


class _RetryAwareCursor(sqlite3.Cursor):
    """Cursor that promotes BUSY beyond inner ``except sqlite3.Error`` blocks."""

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().execute(*args, **kwargs)
        except sqlite3.OperationalError as error:
            _raise_translated(error)

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().executemany(*args, **kwargs)
        except sqlite3.OperationalError as error:
            _raise_translated(error)

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().executescript(*args, **kwargs)
        except sqlite3.OperationalError as error:
            _raise_translated(error)


class _RetryAwareConnection(sqlite3.Connection):
    """Connection variant that exposes BUSY to :func:`retry_on_busy`."""

    def cursor(self, factory: type[sqlite3.Cursor] | None = None) -> sqlite3.Cursor:
        return super().cursor(factory or _RetryAwareCursor)

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().execute(*args, **kwargs)
        except sqlite3.OperationalError as error:
            _raise_translated(error)

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().executemany(*args, **kwargs)
        except sqlite3.OperationalError as error:
            _raise_translated(error)

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().executescript(*args, **kwargs)
        except sqlite3.OperationalError as error:
            _raise_translated(error)

    def commit(self) -> None:
        try:
            super().commit()
        except sqlite3.OperationalError as error:
            _raise_translated(error)

    def __exit__(self, *args: Any) -> bool:
        try:
            return bool(super().__exit__(*args))
        except sqlite3.OperationalError as error:
            _raise_translated(error)


def retry_on_busy(max_retries: int = 3, initial_backoff: float = 0.1) -> Callable[[F], F]:
    """Decorator to retry SQLite operations on 'database is locked' errors."""

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(max_retries + 1):
                token = _RETRY_ON_BUSY_ACTIVE.set(True)
                try:
                    try:
                        return func(*args, **kwargs)
                    except (_RetryableBusyError, sqlite3.OperationalError) as error:
                        original = error.original if isinstance(error, _RetryableBusyError) else error
                        if _is_busy_error(original) and attempt < max_retries:
                            time.sleep(initial_backoff * (2**attempt))
                            logger.warning("SQLite BUSY retry %d/%d for %s", attempt + 1, max_retries, func.__name__)
                        else:
                            if isinstance(error, _RetryableBusyError):
                                raise original from error
                            raise
                finally:
                    _RETRY_ON_BUSY_ACTIVE.reset(token)

        return wrapper  # type: ignore[return-value]

    return decorator


class BaseRepository:
    """Base class for all database repositories."""

    def __init__(self, db_path: str) -> None:
        self.db_path: str = db_path

    def _connect(self) -> sqlite3.Connection:
        """Create a new SQLite connection with WAL mode and foreign keys.

        Wave 3: `PRAGMA busy_timeout=30000` is applied centrally here so
        every repository waits up to 30s for lock contention instead of
        failing fast with SQLITE_BUSY. Previously this was only on
        FloatRepository; moving it up gives the same guarantee to all
        write paths (zones/groups/programs/telegram/settings/mqtt/logs).
        """
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=5, factory=_RetryAwareConnection)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.row_factory = sqlite3.Row
            return conn
        except _RetryableBusyError:
            if conn is not None:
                conn.close()
            raise
        except sqlite3.OperationalError as error:
            if conn is not None:
                conn.close()
            _raise_translated(error)
