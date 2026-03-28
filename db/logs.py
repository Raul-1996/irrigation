import sqlite3
import os
import shutil
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from db.base import BaseRepository, retry_on_busy

logger = logging.getLogger(__name__)


class LogRepository(BaseRepository):
    """Repository for logs, water_usage, water_stats, and backups."""

    def __init__(self, db_path: str, backup_dir: str = 'backups'):
        super().__init__(db_path)
        self.backup_dir = backup_dir

    def get_logs(self, event_type: str = None, from_date: str = None, to_date: str = None) -> List[Dict[str, Any]]:
        """Получить логи с фильтрацией."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                query = (
                    "SELECT id, type, details, "
                    "strftime('%Y-%m-%d %H:%M:%S', timestamp, 'localtime') AS timestamp "
                    "FROM logs WHERE 1=1"
                )
                params = []

                if event_type:
                    query += ' AND type = ?'
                    params.append(event_type)
                if from_date:
                    query += ' AND timestamp >= ?'
                    params.append(from_date)
                if to_date:
                    query += ' AND timestamp <= ?'
                    params.append(f"{to_date} 23:59:59")

                query += ' ORDER BY timestamp DESC LIMIT 1000'
                cursor = conn.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("Ошибка получения логов: %s", e)
            return []

    @retry_on_busy()
    def add_log(self, log_type: str, details: str = None) -> Optional[int]:
        """Добавить запись в лог."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute('''
                    INSERT INTO logs (type, details)
                    VALUES (?, ?)
                ''', (log_type, details))
                log_id = cursor.lastrowid
                conn.commit()
                return log_id
        except sqlite3.Error as e:
            logger.error("Ошибка добавления лога: %s", e)
            return None

    def get_water_usage(self, days: int = 7, zone_id: int = None) -> List[Dict[str, Any]]:
        """Получить данные расхода воды."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                if zone_id:
                    cursor = conn.execute('''
                        SELECT w.*, z.name as zone_name
                        FROM water_usage w
                        LEFT JOIN zones z ON w.zone_id = z.id
                        WHERE w.zone_id = ? AND w.timestamp >= datetime('now', '-{} days')
                        ORDER BY w.timestamp DESC
                    '''.format(days), (zone_id,))
                else:
                    cursor = conn.execute('''
                        SELECT w.*, z.name as zone_name
                        FROM water_usage w
                        LEFT JOIN zones z ON w.zone_id = z.id
                        WHERE w.timestamp >= datetime('now', '-{} days')
                        ORDER BY w.timestamp DESC
                    '''.format(days))
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("Ошибка получения данных расхода воды: %s", e)
            return []

    @retry_on_busy()
    def add_water_usage(self, zone_id: int, liters: float) -> bool:
        """Добавить запись о расходе воды."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    INSERT INTO water_usage (zone_id, liters)
                    VALUES (?, ?)
                ''', (zone_id, liters))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка добавления записи расхода воды: %s", e)
            return False

    def get_water_statistics(self, days: int = 30) -> Dict[str, Any]:
        """Получить статистику расхода воды."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute('''
                    SELECT SUM(liters) as total_liters
                    FROM water_usage
                    WHERE timestamp >= datetime('now', '-{} days')
                '''.format(days))
                total_liters = cursor.fetchone()[0] or 0

                cursor = conn.execute('''
                    SELECT z.name, SUM(w.liters) as liters
                    FROM water_usage w
                    LEFT JOIN zones z ON w.zone_id = z.id
                    WHERE w.timestamp >= datetime('now', '-{} days')
                    GROUP BY w.zone_id, z.name
                    ORDER BY liters DESC
                '''.format(days))
                zone_usage = [dict(row) for row in cursor.fetchall()]

                cursor = conn.execute('''
                    SELECT AVG(daily_liters) as avg_daily
                    FROM (
                        SELECT DATE(timestamp) as date, SUM(liters) as daily_liters
                        FROM water_usage
                        WHERE timestamp >= datetime('now', '-{} days')
                        GROUP BY DATE(timestamp)
                    )
                '''.format(days))
                avg_daily = cursor.fetchone()[0] or 0

                return {
                    'total_liters': round(total_liters, 2),
                    'avg_daily': round(avg_daily, 2),
                    'zone_usage': zone_usage,
                    'period_days': days
                }
        except sqlite3.Error as e:
            logger.error("Ошибка получения статистики воды: %s", e)
            return {
                'total_liters': 0,
                'avg_daily': 0,
                'zone_usage': [],
                'period_days': days
            }

    def create_backup(self) -> Optional[str]:
        """Создать резервную копию базы данных."""
        try:
            if not os.path.exists(self.backup_dir):
                os.makedirs(self.backup_dir)

            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_path = os.path.join(self.backup_dir, f'irrigation_backup_{timestamp}.db')

            try:
                with sqlite3.connect(self.db_path) as src_conn:
                    with sqlite3.connect(backup_path) as dst_conn:
                        src_conn.backup(dst_conn)
                try:
                    with sqlite3.connect(self.db_path) as c:
                        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        c.commit()
                except sqlite3.Error as e:
                    logger.debug("WAL checkpoint after backup: %s", e)
            except sqlite3.Error:
                shutil.copy2(self.db_path, backup_path)

            self._cleanup_old_backups()
            logger.info("Резервная копия создана: %s", backup_path)
            return backup_path
        except (OSError, IOError) as e:
            logger.error("Ошибка создания резервной копии: %s", e)
            return None

    def _cleanup_old_backups(self, keep_count: int = 7):
        """Удалить старые резервные копии."""
        try:
            backup_files = []
            for file in os.listdir(self.backup_dir):
                if file.startswith('irrigation_backup_') and file.endswith('.db'):
                    file_path = os.path.join(self.backup_dir, file)
                    backup_files.append((file_path, os.path.getmtime(file_path)))

            backup_files.sort(key=lambda x: x[1])

            for file_path, _ in backup_files[:-keep_count]:
                os.remove(file_path)
                logger.info("Удалена старая резервная копия: %s", file_path)
        except (OSError, IOError) as e:
            logger.error("Ошибка очистки старых резервных копий: %s", e)
