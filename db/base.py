import functools
import logging
import sqlite3
import time
from contextvars import ContextVar
from typing import Any, Callable, NoReturn, TypeVar

from db.identity import MAX_ENTITY_ID

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])
_RETRY_ON_BUSY_ACTIVE: ContextVar[bool] = ContextVar("db_retry_on_busy_active", default=False)
_RETRY_COMMIT_SUCCEEDED: ContextVar[bool] = ContextVar("db_retry_commit_succeeded", default=False)


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
        had_transaction = self.in_transaction
        try:
            super().commit()
        except sqlite3.OperationalError as error:
            _raise_translated(error)
        if had_transaction and _RETRY_ON_BUSY_ACTIVE.get():
            _RETRY_COMMIT_SUCCEEDED.set(True)

    def __exit__(self, *args: Any) -> bool:
        had_transaction = self.in_transaction
        try:
            result = bool(super().__exit__(*args))
        except sqlite3.OperationalError as error:
            _raise_translated(error)
        if had_transaction and args and args[0] is None and _RETRY_ON_BUSY_ACTIVE.get():
            _RETRY_COMMIT_SUCCEEDED.set(True)
        return result


def retry_on_busy(max_retries: int = 3, initial_backoff: float = 0.1) -> Callable[[F], F]:
    """Decorator to retry SQLite operations on 'database is locked' errors."""

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(max_retries + 1):
                token = _RETRY_ON_BUSY_ACTIVE.set(True)
                commit_token = _RETRY_COMMIT_SUCCEEDED.set(False)
                try:
                    try:
                        return func(*args, **kwargs)
                    except (_RetryableBusyError, sqlite3.OperationalError) as error:
                        original = error.original if isinstance(error, _RetryableBusyError) else error
                        committed = _RETRY_COMMIT_SUCCEEDED.get()
                        if _is_busy_error(original) and not committed and attempt < max_retries:
                            time.sleep(initial_backoff * (2**attempt))
                            logger.warning("SQLite BUSY retry %d/%d for %s", attempt + 1, max_retries, func.__name__)
                        else:
                            if isinstance(error, _RetryableBusyError):
                                raise original from error
                            raise
                finally:
                    committed = _RETRY_COMMIT_SUCCEEDED.get()
                    _RETRY_COMMIT_SUCCEEDED.reset(commit_token)
                    _RETRY_ON_BUSY_ACTIVE.reset(token)
                    # Preserve the unsafe-to-retry state across nested
                    # decorated calls: an outer operation must not replay a
                    # durable write committed by an inner repository method.
                    if committed and _RETRY_ON_BUSY_ACTIVE.get():
                        _RETRY_COMMIT_SUCCEEDED.set(True)

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

    @staticmethod
    def _storage_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
        if table not in {"groups", "mqtt_servers"}:
            raise ValueError("unsupported snapshot table")
        columns = tuple(str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall())
        if not columns or "id" not in columns:
            raise sqlite3.DatabaseError(f"snapshot table {table!r} is unavailable")
        return columns

    @staticmethod
    def _validated_snapshot_values(snapshot: dict[str, Any], columns: tuple[str, ...]) -> tuple[Any, ...] | None:
        if not isinstance(snapshot, dict) or set(snapshot) != set(columns):
            return None
        return tuple(snapshot[column] for column in columns)

    @classmethod
    def _snapshot_matches(
        cls,
        row: sqlite3.Row | None,
        snapshot: dict[str, Any],
        columns: tuple[str, ...],
        *,
        ignored_fields: frozenset[str] = frozenset(),
    ) -> bool:
        values = cls._validated_snapshot_values(snapshot, columns)
        if row is None or values is None or not ignored_fields.issubset(columns):
            return False
        return all(row[column] == snapshot[column] for column in columns if column not in ignored_fields)

    def _get_storage_snapshot(self, table: str, row_id: int) -> dict[str, Any] | None:
        """Read an exact at-rest row for trusted compensating transactions."""

        try:
            with self._connect() as conn:
                self._storage_columns(conn, table)
                row = conn.execute(f'SELECT * FROM "{table}" WHERE id = ?', (int(row_id),)).fetchone()
                return dict(row) if row is not None else None
        except (sqlite3.Error, TypeError, ValueError) as error:
            logger.error("Failed to read %s rollback snapshot: %s", table, error)
            return None

    @retry_on_busy()
    def _restore_storage_snapshot(
        self,
        table: str,
        entity: str,
        before: dict[str, Any],
        expected_current: dict[str, Any] | None,
        *,
        ignored_current_fields: frozenset[str] = frozenset(),
    ) -> bool:
        """CAS-restore ``before`` or reinsert an exactly deleted durable row."""

        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                columns = self._storage_columns(conn, table)
                before_values = self._validated_snapshot_values(before, columns)
                if before_values is None:
                    conn.rollback()
                    return False
                row_id = int(before["id"])
                current = conn.execute(f'SELECT * FROM "{table}" WHERE id = ?', (row_id,)).fetchone()
                if current is None:
                    if expected_current is not None:
                        conn.rollback()
                        return False
                    conn.execute(
                        "DELETE FROM retired_entity_ids WHERE entity = ? AND id = ?",
                        (entity, row_id),
                    )
                    if row_id == MAX_ENTITY_ID:
                        # Public explicit MAX inserts stay forbidden, while a
                        # trusted compensation can recover the final ID by
                        # making SQLite allocate it normally under the writer
                        # lock. Rollback restores the tombstone/sequence if the
                        # exact auto-allocation cannot be completed.
                        conn.execute(
                            "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                            (MAX_ENTITY_ID - 1, table),
                        )
                        insert_columns = tuple(column for column in columns if column != "id")
                        names = ", ".join(f'"{column}"' for column in insert_columns)
                        placeholders = ", ".join("?" for _ in insert_columns)
                        cursor = conn.execute(
                            f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
                            tuple(before[column] for column in insert_columns),
                        )
                        if cursor.lastrowid != row_id:
                            conn.rollback()
                            return False
                    else:
                        names = ", ".join(f'"{column}"' for column in columns)
                        placeholders = ", ".join("?" for _ in columns)
                        conn.execute(
                            f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
                            before_values,
                        )
                else:
                    if expected_current is None or not self._snapshot_matches(
                        current,
                        expected_current,
                        columns,
                        ignored_fields=ignored_current_fields,
                    ):
                        conn.rollback()
                        return False
                    mutable_columns = tuple(column for column in columns if column != "id")
                    assignments = ", ".join(f'"{column}" = ?' for column in mutable_columns)
                    params = [before[column] for column in mutable_columns]
                    params.append(row_id)
                    conn.execute(f'UPDATE "{table}" SET {assignments} WHERE id = ?', params)
                conn.commit()
                return True
        except (sqlite3.Error, TypeError, ValueError) as error:
            logger.error("Failed to restore %s rollback snapshot: %s", table, error)
            return False

    @retry_on_busy()
    def _delete_storage_snapshot_if_unchanged(
        self,
        table: str,
        expected: dict[str, Any],
        *,
        restrict_query: str | None = None,
        ignored_expected_fields: frozenset[str] = frozenset(),
    ) -> bool:
        """Delete a just-created row only while its full stored value matches."""

        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                columns = self._storage_columns(conn, table)
                if self._validated_snapshot_values(expected, columns) is None:
                    conn.rollback()
                    return False
                row_id = int(expected["id"])
                current = conn.execute(f'SELECT * FROM "{table}" WHERE id = ?', (row_id,)).fetchone()
                if not self._snapshot_matches(
                    current,
                    expected,
                    columns,
                    ignored_fields=ignored_expected_fields,
                ):
                    conn.rollback()
                    return False
                if restrict_query is not None and conn.execute(restrict_query, (row_id,)).fetchone() is not None:
                    conn.rollback()
                    return False
                cursor = conn.execute(f'DELETE FROM "{table}" WHERE id = ?', (row_id,))
                conn.commit()
                return cursor.rowcount == 1
        except (sqlite3.Error, TypeError, ValueError) as error:
            logger.error("Failed to delete unchanged %s rollback snapshot: %s", table, error)
            return False
