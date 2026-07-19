import logging
import os
import sqlite3
import stat as stat_module
from datetime import datetime
from typing import Any
from urllib.parse import quote

from db.base import BaseRepository, retry_on_busy

logger = logging.getLogger(__name__)


class LogRepository(BaseRepository):
    """Repository for logs, water_usage, water_stats, and backups."""

    _REQUIRED_BACKUP_TABLES = frozenset({"zones", "groups", "programs", "settings", "migrations", "mqtt_servers"})

    def __init__(self, db_path: str, backup_dir: str = "backups"):
        super().__init__(db_path)
        self.backup_dir = backup_dir

    def get_logs(
        self, event_type: str | None = None, from_date: str | None = None, to_date: str | None = None
    ) -> list[dict[str, Any]]:
        """Получить логи с фильтрацией."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                query = (
                    "SELECT id, type, details, "
                    "strftime('%Y-%m-%d %H:%M:%S', timestamp, 'localtime') AS timestamp "
                    "FROM logs WHERE 1=1"
                )
                params = []

                if event_type:
                    query += " AND type = ?"
                    params.append(event_type)
                if from_date:
                    query += " AND date(timestamp, 'localtime') >= date(?)"
                    params.append(from_date)
                if to_date:
                    query += " AND date(timestamp, 'localtime') <= date(?)"
                    params.append(to_date)

                query += " ORDER BY timestamp DESC LIMIT 1000"
                cursor = conn.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("Ошибка получения логов: %s", e)
            return []

    @retry_on_busy()
    def add_log(self, log_type: str, details: str | None = None) -> int | None:
        """Добавить запись в лог."""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO logs (type, details)
                    VALUES (?, ?)
                """,
                    (log_type, details),
                )
                log_id = cursor.lastrowid
                conn.commit()
                return log_id
        except sqlite3.Error as e:
            logger.error("Ошибка добавления лога: %s", e)
            return None

    def get_water_usage(self, days: int = 7, zone_id: int | None = None) -> list[dict[str, Any]]:
        """Получить данные расхода воды."""
        try:
            days = int(days)
            day_modifier = f"-{days} days"
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                if zone_id:
                    cursor = conn.execute(
                        """
                        SELECT w.*, z.name as zone_name
                        FROM water_usage w
                        LEFT JOIN zones z ON w.zone_id = z.id
                        WHERE w.zone_id = ? AND w.timestamp >= datetime('now', ?)
                        ORDER BY w.timestamp DESC
                    """,
                        (zone_id, day_modifier),
                    )
                else:
                    cursor = conn.execute(
                        """
                        SELECT w.*, z.name as zone_name
                        FROM water_usage w
                        LEFT JOIN zones z ON w.zone_id = z.id
                        WHERE w.timestamp >= datetime('now', ?)
                        ORDER BY w.timestamp DESC
                    """,
                        (day_modifier,),
                    )
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("Ошибка получения данных расхода воды: %s", e)
            return []

    @retry_on_busy()
    def add_water_usage(self, zone_id: int, liters: float) -> bool:
        """Добавить запись о расходе воды."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO water_usage (zone_id, liters)
                    VALUES (?, ?)
                """,
                    (zone_id, liters),
                )
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка добавления записи расхода воды: %s", e)
            return False

    def get_water_statistics(self, days: int = 30) -> dict[str, Any]:
        """Получить статистику расхода воды."""
        try:
            days = int(days)
            day_modifier = f"-{days} days"
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    SELECT SUM(liters) as total_liters
                    FROM water_usage
                    WHERE timestamp >= datetime('now', ?)
                """,
                    (day_modifier,),
                )
                total_liters = cursor.fetchone()[0] or 0

                cursor = conn.execute(
                    """
                    SELECT z.name, SUM(w.liters) as liters
                    FROM water_usage w
                    LEFT JOIN zones z ON w.zone_id = z.id
                    WHERE w.timestamp >= datetime('now', ?)
                    GROUP BY w.zone_id, z.name
                    ORDER BY liters DESC
                """,
                    (day_modifier,),
                )
                zone_usage = [dict(row) for row in cursor.fetchall()]

                cursor = conn.execute(
                    """
                    SELECT AVG(daily_liters) as avg_daily
                    FROM (
                        SELECT DATE(timestamp) as date, SUM(liters) as daily_liters
                        FROM water_usage
                        WHERE timestamp >= datetime('now', ?)
                        GROUP BY DATE(timestamp)
                    )
                """,
                    (day_modifier,),
                )
                avg_daily = cursor.fetchone()[0] or 0

                return {
                    "total_liters": round(total_liters, 2),
                    "avg_daily": round(avg_daily, 2),
                    "zone_usage": zone_usage,
                    "period_days": days,
                }
        except sqlite3.Error as e:
            logger.error("Ошибка получения статистики воды: %s", e)
            return {"total_liters": 0, "avg_daily": 0, "zone_usage": [], "period_days": days}

    def create_backup(self) -> str | None:
        """Create a WAL-consistent, validated SQLite snapshot.

        Copying only the main ``.db`` file is never a valid fallback while
        WAL mode is active: committed pages may still live exclusively in
        ``-wal``. Both strategies below ask SQLite itself for one coherent
        read snapshot; if both fail, backup creation fails closed.
        """
        try:
            if not self._is_valid_backup_source(self.db_path):
                return None

            os.makedirs(self.backup_dir, mode=0o700, exist_ok=True)
            os.chmod(self.backup_dir, 0o700)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            backup_path = os.path.join(self.backup_dir, f"irrigation_backup_{timestamp}.db")
            partial_path = backup_path + ".partial"

            try:
                self._backup_via_api(self.db_path, partial_path)
            except (sqlite3.Error, OSError) as api_error:
                logger.warning("SQLite backup API failed, trying VACUUM INTO: %s", api_error)
                self._remove_incomplete_backup(partial_path)
                try:
                    self._backup_via_vacuum(self.db_path, partial_path)
                except (sqlite3.Error, OSError) as vacuum_error:
                    logger.error(
                        "Both WAL-consistent backup methods failed (backup=%s, vacuum=%s)",
                        api_error,
                        vacuum_error,
                    )
                    self._remove_incomplete_backup(partial_path)
                    return None

            try:
                # The containing directory is private throughout creation;
                # chmod before validation/rename makes the final file 0600
                # even if the process was started with a permissive umask.
                os.chmod(partial_path, 0o600)
            except OSError as e:
                logger.error("Не удалось защитить права бэкапа %s: %s", partial_path, e)
                self._remove_incomplete_backup(partial_path)
                return None

            try:
                self._validate_application_database(partial_path)
                os.replace(partial_path, backup_path)
                self._remove_sqlite_sidecars(partial_path)
            except (sqlite3.Error, OSError) as e:
                logger.error("Backup validation failed: %s", e)
                self._remove_incomplete_backup(partial_path)
                return None

            self._cleanup_old_backups()
            logger.info("Резервная копия создана: %s", backup_path)
            return backup_path
        except OSError as e:
            logger.error("Ошибка создания резервной копии: %s", e)
            return None

    @staticmethod
    def _backup_via_api(source_path: str, target_path: str) -> None:
        with sqlite3.connect(LogRepository._read_only_uri(source_path), timeout=30, uri=True) as source:
            source.execute("PRAGMA busy_timeout=30000")
            with sqlite3.connect(target_path, timeout=30) as target:
                source.backup(target)
                # backup() also copies the source journal-mode header. Convert
                # the finished snapshot back to a standalone main DB through
                # SQLite itself so restore never depends on sidecar files.
                target.execute("PRAGMA journal_mode=DELETE")

    @staticmethod
    def _backup_via_vacuum(source_path: str, target_path: str) -> None:
        with sqlite3.connect(LogRepository._read_only_uri(source_path), timeout=30, uri=True) as source:
            source.execute("PRAGMA busy_timeout=30000")
            source.execute("VACUUM INTO ?", (target_path,))

    @staticmethod
    def _read_only_uri(path: str) -> str:
        return f"file:{quote(os.path.abspath(path), safe='/')}?mode=ro"

    @classmethod
    def _validate_application_database(cls, path: str) -> None:
        """Validate SQLite structure and readable application tables."""

        with sqlite3.connect(cls._read_only_uri(path), timeout=30, uri=True) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise sqlite3.DatabaseError(f"integrity_check returned {integrity!r}")
            tables = {
                str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            missing = cls._REQUIRED_BACKUP_TABLES - tables
            if missing:
                raise sqlite3.DatabaseError(f"application schema is missing tables: {sorted(missing)!r}")
            for table in sorted(cls._REQUIRED_BACKUP_TABLES):
                conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.DatabaseError("foreign_key_check returned violations")

    @classmethod
    def _is_valid_backup_source(cls, path: str) -> bool:
        try:
            source_stat = os.stat(path)
            if not stat_module.S_ISREG(source_stat.st_mode) or source_stat.st_size <= 0:
                logger.error("Backup source is not a non-empty regular file: %s", path)
                return False
            cls._validate_application_database(path)
            return True
        except (OSError, sqlite3.Error) as e:
            logger.error("Backup source validation failed for %s: %s", path, e)
            return False

    @staticmethod
    def _remove_incomplete_backup(path: str) -> None:
        LogRepository._remove_sqlite_sidecars(path)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.error("Не удалось удалить незавершённый бэкап %s: %s", path, e)

    @staticmethod
    def _remove_sqlite_sidecars(path: str) -> None:
        for suffix in ("-wal", "-shm"):
            sidecar = path + suffix
            try:
                os.remove(sidecar)
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.error("Не удалось удалить sidecar бэкапа %s: %s", sidecar, e)

    def _cleanup_old_backups(self, keep_count: int = 7):
        """Удалить старые резервные копии."""
        try:
            backup_files = []
            for file in os.listdir(self.backup_dir):
                if file.startswith("irrigation_backup_") and file.endswith(".db"):
                    file_path = os.path.join(self.backup_dir, file)
                    backup_files.append((file_path, os.path.getmtime(file_path)))

            backup_files.sort(key=lambda x: x[1])

            for file_path, _ in backup_files[:-keep_count]:
                os.remove(file_path)
                logger.info("Удалена старая резервная копия: %s", file_path)
        except OSError as e:
            logger.error("Ошибка очистки старых резервных копий: %s", e)
