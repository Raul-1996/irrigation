import sqlite3
import logging
from typing import List, Dict, Any, Optional

from db.base import BaseRepository, retry_on_busy

logger = logging.getLogger(__name__)


class GroupRepository(BaseRepository):
    """Repository for group CRUD operations."""

    def get_groups(self) -> List[Dict[str, Any]]:
        """Получить все группы."""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute('''
                    SELECT g.*, COUNT(z.id) as zone_count
                    FROM groups g
                    LEFT JOIN zones z ON g.id = z.group_id
                    GROUP BY g.id
                    ORDER BY g.id
                ''')
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("Ошибка получения групп: %s", e)
            return []

    @retry_on_busy()
    def create_group(self, name: str) -> Optional[Dict[str, Any]]:
        """Создать новую группу."""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cursor = conn.execute('INSERT INTO groups (name) VALUES (?)', (name,))
                new_id = cursor.lastrowid
                conn.commit()
                return {'id': new_id, 'name': name, 'zone_count': 0}
        except sqlite3.Error as e:
            logger.error("Ошибка создания группы '%s': %s", name, e)
            return None

    @retry_on_busy()
    def delete_group(self, group_id: int) -> bool:
        """Удалить группу. Запрещено для группы 999 и непустых групп."""
        try:
            if group_id == 999:
                return False
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cursor = conn.execute('SELECT COUNT(*) FROM zones WHERE group_id = ?', (group_id,))
                cnt = cursor.fetchone()[0]
                if cnt > 0:
                    return False
                conn.execute('DELETE FROM groups WHERE id = ?', (group_id,))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка удаления группы %s: %s", group_id, e)
            return False

    @retry_on_busy()
    def update_group(self, group_id: int, name: str) -> bool:
        """Обновить название группы."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    UPDATE groups 
                    SET name = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (name, group_id))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка обновления группы %s: %s", group_id, e)
            return False

    @retry_on_busy()
    def update_group_fields(self, group_id: int, updates: Dict[str, Any]) -> bool:
        """Обновить произвольные поля группы (мастер-клапан, сенсоры)."""
        if not updates:
            return False
        allowed = {
            'use_master_valve', 'master_mqtt_topic', 'master_mode', 'master_mqtt_server_id',
            'use_pressure_sensor', 'pressure_mqtt_topic', 'pressure_unit', 'pressure_mqtt_server_id',
            'use_water_meter', 'water_mqtt_topic', 'water_mqtt_server_id', 'master_valve_observed',
            'water_pulse_size', 'water_base_value_m3', 'water_base_pulses'
        }
        set_parts = []
        params = []
        for k, v in updates.items():
            if k in allowed:
                set_parts.append(f"{k} = ?")
                params.append(v)
        if not set_parts:
            return False
        params.append(group_id)
        try:
            with sqlite3.connect(self.db_path) as conn:
                sql = f"UPDATE groups SET {', '.join(set_parts)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
                conn.execute(sql, tuple(params))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка обновления полей группы %s: %s", group_id, e)
            return False

    def get_group_use_rain(self, group_id: int) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT use_rain_sensor FROM groups WHERE id = ? LIMIT 1', (group_id,))
                row = cur.fetchone()
                if not row:
                    return False
                val = row['use_rain_sensor']
                return bool(int(val or 0))
        except sqlite3.Error as e:
            logger.error("Ошибка чтения use_rain_sensor для группы %s: %s", group_id, e)
            return False

    @retry_on_busy()
    def set_group_use_rain(self, group_id: int, enabled: bool) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('UPDATE groups SET use_rain_sensor = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                             (1 if enabled else 0, group_id))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка записи use_rain_sensor для группы %s: %s", group_id, e)
            return False

    def list_groups_min(self) -> List[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT id, name FROM groups ORDER BY id')
                return [dict(r) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("Ошибка получения минимального списка групп: %s", e)
            return []

    def list_zones_by_group_min(self, group_id: int) -> List[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT id, name, duration, state FROM zones WHERE group_id=? ORDER BY id',
                                   (int(group_id),))
                return [dict(r) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("Ошибка получения зон группы %s (min): %s", group_id, e)
            return []
