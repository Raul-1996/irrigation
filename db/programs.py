import sqlite3
import json
import logging
from typing import List, Dict, Any, Optional

from db.base import BaseRepository, retry_on_busy

logger = logging.getLogger(__name__)


class ProgramRepository(BaseRepository):
    """Repository for program CRUD, conflicts, and cancellations."""

    def get_programs(self) -> List[Dict[str, Any]]:
        """Получить все программы."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute('SELECT * FROM programs ORDER BY id')
                programs = []
                for row in cursor.fetchall():
                    program = dict(row)
                    program['days'] = [int(d) for d in json.loads(program['days'])]
                    program['zones'] = json.loads(program['zones'])
                    programs.append(program)
                return programs
        except sqlite3.Error as e:
            logger.error("Ошибка получения программ: %s", e)
            return []

    def get_program(self, program_id: int) -> Optional[Dict[str, Any]]:
        """Получить программу по ID."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute('SELECT * FROM programs WHERE id = ?', (program_id,))
                row = cursor.fetchone()
                if row:
                    program = dict(row)
                    program['days'] = [int(d) for d in json.loads(program['days'])]
                    program['zones'] = json.loads(program['zones'])
                    return program
                return None
        except sqlite3.Error as e:
            logger.error("Ошибка получения программы %s: %s", program_id, e)
            return None

    @retry_on_busy()
    def create_program(self, program_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Создать новую программу."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                try:
                    norm_days = [int(d) for d in program_data['days']]
                except (TypeError, ValueError, KeyError):
                    norm_days = []
                if norm_days and min(norm_days) >= 1 and max(norm_days) <= 7:
                    norm_days = [max(0, min(6, d - 1)) for d in norm_days]
                cursor = conn.execute('''
                    INSERT INTO programs (name, time, days, zones)
                    VALUES (?, ?, ?, ?)
                ''', (
                    program_data['name'],
                    program_data['time'],
                    json.dumps(norm_days),
                    json.dumps(program_data['zones'])
                ))
                program_id = cursor.lastrowid
                conn.commit()
                return self.get_program(program_id)
        except sqlite3.Error as e:
            logger.error("Ошибка создания программы: %s", e)
            return None

    @retry_on_busy()
    def update_program(self, program_id: int, program_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Обновить программу."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                try:
                    norm_days = [int(d) for d in program_data['days']]
                except (TypeError, ValueError, KeyError):
                    norm_days = []
                if norm_days and min(norm_days) >= 1 and max(norm_days) <= 7:
                    norm_days = [max(0, min(6, d - 1)) for d in norm_days]
                conn.execute('''
                    UPDATE programs 
                    SET name = ?, time = ?, days = ?, zones = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (
                    program_data['name'],
                    program_data['time'],
                    json.dumps(norm_days),
                    json.dumps(program_data['zones']),
                    program_id
                ))
                conn.commit()
                return self.get_program(program_id)
        except sqlite3.Error as e:
            logger.error("Ошибка обновления программы %s: %s", program_id, e)
            return None

    @retry_on_busy()
    def delete_program(self, program_id: int) -> bool:
        """Удалить программу."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('DELETE FROM programs WHERE id = ?', (program_id,))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка удаления программы %s: %s", program_id, e)
            return False

    def check_program_conflicts(self, program_id: int = None, time: str = None,
                                zones: List[int] = None, days: List[str] = None) -> List[Dict[str, Any]]:
        """Проверка пересечения программ полива."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row

                query = 'SELECT id, name, time, days, zones FROM programs'
                if program_id:
                    query += ' WHERE id != ?'
                    cursor = conn.execute(query, (program_id,))
                else:
                    cursor = conn.execute(query)

                programs = cursor.fetchall()
                conflicts = []

                if not time or not zones or not days:
                    return conflicts

                try:
                    program_hour, program_minute = map(int, time.split(':'))
                    program_minutes = program_hour * 60 + program_minute
                except (ValueError, AttributeError):
                    return conflicts

                try:
                    norm_days = [int(d) for d in days]
                except (TypeError, ValueError):
                    norm_days = days

                # Cache durations and groups
                durations_cache: Dict[int, int] = {}
                groups_cache: Dict[int, int] = {}
                try:
                    curz = conn.execute('SELECT id, duration, group_id FROM zones')
                    for zid, dur, gid in curz.fetchall():
                        durations_cache[int(zid)] = int(dur or 0)
                        groups_cache[int(zid)] = int(gid or 0)
                except sqlite3.Error:
                    logger.debug("Не удалось загрузить кеш зон для проверки конфликтов")

                def _get_dur(zid: int) -> int:
                    try:
                        return int(durations_cache.get(int(zid), 0))
                    except (TypeError, ValueError):
                        return 0

                def _get_gid(zid: int) -> int:
                    try:
                        return int(groups_cache.get(int(zid), 0))
                    except (TypeError, ValueError):
                        return 0

                total_duration = sum(_get_dur(int(zone_id)) for zone_id in zones)
                program_end_minutes = program_minutes + total_duration

                for program in programs:
                    program_data = dict(program)
                    program_data['days'] = json.loads(program_data['days'])
                    program_data['zones'] = json.loads(program_data['zones'])

                    common_days = set(norm_days) & set(program_data['days'])
                    if not common_days:
                        continue

                    common_zones = set(zones) & set(program_data['zones'])
                    zones_groups = {_get_gid(int(zid)) for zid in zones}
                    existing_zones_groups = {_get_gid(int(zid)) for zid in program_data['zones']}
                    common_groups = zones_groups & existing_zones_groups

                    if not common_zones and not common_groups:
                        continue

                    try:
                        existing_hour, existing_minute = map(int, program_data['time'].split(':'))
                        existing_minutes = existing_hour * 60 + existing_minute
                    except (ValueError, AttributeError):
                        continue

                    existing_total_duration = sum(_get_dur(int(zid)) for zid in program_data['zones'])
                    existing_end_minutes = existing_minutes + existing_total_duration

                    if program_minutes < existing_end_minutes and program_end_minutes > existing_minutes:
                        conflicts.append({
                            'program_id': program_data['id'],
                            'program_name': program_data['name'],
                            'program_time': program_data['time'],
                            'program_duration': existing_total_duration,
                            'common_zones': list(common_zones),
                            'common_groups': list(common_groups),
                            'common_days': list(common_days),
                            'overlap_start': max(program_minutes, existing_minutes),
                            'overlap_end': min(program_end_minutes, existing_end_minutes)
                        })

                return conflicts

        except sqlite3.Error as e:
            logger.error("Ошибка проверки пересечения программ: %s", e)
            return []

    # === Program cancellations (per date) ===
    @retry_on_busy()
    def cancel_program_run_for_group(self, program_id: int, run_date: str, group_id: int) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    INSERT OR REPLACE INTO program_cancellations(program_id, run_date, group_id)
                    VALUES (?, ?, ?)
                ''', (int(program_id), str(run_date), int(group_id)))
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("Ошибка записи отмены программы %s на %s для группы %s: %s", program_id, run_date, group_id, e)
            return False

    def is_program_run_cancelled_for_group(self, program_id: int, run_date: str, group_id: int) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('''
                    SELECT 1 FROM program_cancellations
                    WHERE program_id = ? AND run_date = ? AND group_id = ? LIMIT 1
                ''', (int(program_id), str(run_date), int(group_id)))
                return cur.fetchone() is not None
        except sqlite3.Error as e:
            logger.error("Ошибка чтения отмены программы %s на %s для группы %s: %s", program_id, run_date, group_id, e)
            return False

    @retry_on_busy()
    def clear_program_cancellations_for_group_on_date(self, group_id: int, run_date: str) -> bool:
        """Удалить все отмены программ для указанной группы на указанную дату."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    DELETE FROM program_cancellations
                    WHERE group_id = ? AND run_date = ?
                ''', (int(group_id), str(run_date)))
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("Ошибка очистки отмен программ на %s для группы %s: %s", run_date, group_id, e)
            return False
