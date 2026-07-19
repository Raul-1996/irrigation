import logging
import sqlite3
from typing import Any

from db.base import BaseRepository, retry_on_busy

logger = logging.getLogger(__name__)

_GROUP_OBSERVATION_FIELDS = frozenset({"master_valve_observed"})

_GROUP_CONFIG_FIELDS = frozenset(
    {
        "name",
        "use_rain_sensor",
        "use_master_valve",
        "master_mqtt_topic",
        "master_mode",
        "master_mqtt_server_id",
        "use_pressure_sensor",
        "pressure_mqtt_topic",
        "pressure_unit",
        "pressure_mqtt_server_id",
        "use_water_meter",
        "water_mqtt_topic",
        "water_mqtt_server_id",
        "water_pulse_size",
        "water_base_value_m3",
        "water_base_pulses",
        "master_close_delay_sec",
        "master_valve_observed",
    }
)


class GroupRepository(BaseRepository):
    """Repository for group CRUD operations."""

    def get_groups_strict(self) -> list[dict[str, Any]]:
        """Return group configuration, propagating sqlite errors to callers.

        Runtime reconfiguration uses this loader to build a complete new
        subscription set before swapping clients. An empty list is therefore
        a valid database result, never a sentinel for a failed read.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT g.*, COUNT(z.id) as zone_count
                FROM groups g
                LEFT JOIN zones z ON g.id = z.group_id
                GROUP BY g.id
                ORDER BY g.id
            """)
            return [dict(row) for row in cursor.fetchall()]

    def get_groups(self) -> list[dict[str, Any]]:
        """Получить все группы, сохраняя legacy fail-soft контракт."""
        try:
            return self.get_groups_strict()
        except sqlite3.Error as e:
            logger.error("Ошибка получения групп: %s", e)
            return []

    def get_group_storage_snapshot(self, group_id: int) -> dict[str, Any] | None:
        """Return the exact stored row used only for compensating rollback."""

        return self._get_storage_snapshot("groups", group_id)

    def restore_group_snapshot(
        self,
        before: dict[str, Any],
        expected_current: dict[str, Any] | None = None,
        *,
        allow_observed_drift: bool = False,
    ) -> bool:
        return self._restore_storage_snapshot(
            "groups",
            "group",
            before,
            expected_current,
            ignored_current_fields=_GROUP_OBSERVATION_FIELDS if allow_observed_drift else frozenset(),
        )

    def delete_group_if_unchanged(
        self,
        expected: dict[str, Any],
        *,
        allow_observed_drift: bool = False,
    ) -> bool:
        try:
            if int(expected.get("id")) in {1, 999}:
                return False
        except (AttributeError, TypeError, ValueError):
            return False
        return self._delete_storage_snapshot_if_unchanged(
            "groups",
            expected,
            restrict_query="SELECT 1 FROM zones WHERE group_id = ? LIMIT 1",
            ignored_expected_fields=_GROUP_OBSERVATION_FIELDS if allow_observed_drift else frozenset(),
        )

    @retry_on_busy()
    def create_group(self, name: str) -> dict[str, Any] | None:
        """Создать новую группу."""
        try:
            with self._connect() as conn:
                cursor = conn.execute("INSERT INTO groups (name) VALUES (?)", (name,))
                new_id = cursor.lastrowid
                conn.commit()
                return {"id": new_id, "name": name, "zone_count": 0}
        except sqlite3.Error as e:
            logger.error("Ошибка создания группы '%s': %s", name, e)
            return None

    @retry_on_busy()
    def delete_group(self, group_id: int) -> bool:
        """Удалить группу. Запрещено для системных и непустых групп."""
        try:
            if group_id in {1, 999}:
                return False
            with self._connect() as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM zones WHERE group_id = ?", (group_id,))
                cnt = cursor.fetchone()[0]
                if cnt > 0:
                    return False
                deleted = conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
                conn.commit()
                return deleted.rowcount == 1
        except sqlite3.Error as e:
            logger.error("Ошибка удаления группы %s: %s", group_id, e)
            return False

    @retry_on_busy()
    def update_group(self, group_id: int, name: str) -> bool:
        """Обновить название группы."""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE groups
                    SET name = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """,
                    (name, group_id),
                )
                conn.commit()
                return cursor.rowcount == 1
        except sqlite3.Error as e:
            logger.error("Ошибка обновления группы %s: %s", group_id, e)
            return False

    @retry_on_busy()
    def update_group_fields(self, group_id: int, updates: dict[str, Any]) -> bool:
        """Обновить произвольные поля группы (мастер-клапан, сенсоры)."""
        if not updates:
            return False
        allowed = {
            "use_master_valve",
            "master_mqtt_topic",
            "master_mode",
            "master_mqtt_server_id",
            "use_pressure_sensor",
            "pressure_mqtt_topic",
            "pressure_unit",
            "pressure_mqtt_server_id",
            "use_water_meter",
            "water_mqtt_topic",
            "water_mqtt_server_id",
            "master_valve_observed",
            "water_pulse_size",
            "water_base_value_m3",
            "water_base_pulses",
            "master_close_delay_sec",
        }
        set_parts = []
        params = []
        persisted_fields = set()
        for k, v in updates.items():
            if k in allowed:
                set_parts.append(f"{k} = ?")
                params.append(v)
                persisted_fields.add(k)
        if not set_parts:
            return False
        params.append(group_id)
        try:
            with self._connect() as conn:
                if persisted_fields == {"master_valve_observed"}:
                    sql = f"UPDATE groups SET {', '.join(set_parts)} WHERE id = ?"
                else:
                    sql = f"UPDATE groups SET {', '.join(set_parts)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
                cursor = conn.execute(sql, tuple(params))
                conn.commit()
                return cursor.rowcount == 1
        except sqlite3.Error as e:
            logger.error("Ошибка обновления полей группы %s: %s", group_id, e)
            return False

    @retry_on_busy()
    def update_group_config_with_snapshot(
        self,
        group_id: int,
        updates: dict[str, Any],
        *,
        expected_current: dict[str, Any] | None = None,
        allow_observed_drift: bool = False,
    ) -> dict[str, Any] | None:
        """Update settings and return the exact committed row for later CAS."""
        fields = [(key, value) for key, value in updates.items() if key in _GROUP_CONFIG_FIELDS]
        if not fields:
            return None
        assignments = [f"{key} = ?" for key, _value in fields]
        params = [value for _key, value in fields]
        params.append(group_id)
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if expected_current is not None:
                    columns = self._storage_columns(conn, "groups")
                    current = conn.execute(
                        "SELECT * FROM groups WHERE id = ?",
                        (int(group_id),),
                    ).fetchone()
                    if not self._snapshot_matches(
                        current,
                        expected_current,
                        columns,
                        ignored_fields=_GROUP_OBSERVATION_FIELDS if allow_observed_drift else frozenset(),
                    ):
                        conn.rollback()
                        return None
                cursor = conn.execute(
                    f"UPDATE groups SET {', '.join(assignments)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    tuple(params),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return None
                row = conn.execute("SELECT * FROM groups WHERE id = ?", (int(group_id),)).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                snapshot = dict(row)
                conn.commit()
                return snapshot
        except sqlite3.Error as e:
            logger.error("Ошибка атомарного обновления группы %s: %s", group_id, e)
            return None

    def update_group_config(self, group_id: int, updates: dict[str, Any]) -> bool:
        """Atomically update all fields accepted by the group settings API."""
        return self.update_group_config_with_snapshot(group_id, updates) is not None

    def get_group_use_rain(self, group_id: int) -> bool:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute("SELECT use_rain_sensor FROM groups WHERE id = ? LIMIT 1", (group_id,))
                row = cur.fetchone()
                if not row:
                    return False
                val = row["use_rain_sensor"]
                return bool(int(val or 0))
        except sqlite3.Error as e:
            logger.error("Ошибка чтения use_rain_sensor для группы %s: %s", group_id, e)
            return False

    @retry_on_busy()
    def set_group_use_rain(self, group_id: int, enabled: bool) -> bool:
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "UPDATE groups SET use_rain_sensor = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (1 if enabled else 0, group_id),
                )
                conn.commit()
                return cursor.rowcount == 1
        except sqlite3.Error as e:
            logger.error("Ошибка записи use_rain_sensor для группы %s: %s", group_id, e)
            return False

    def list_groups_min(self) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute("SELECT id, name FROM groups ORDER BY id")
                return [dict(r) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("Ошибка получения минимального списка групп: %s", e)
            return []

    def list_zones_by_group_min(self, group_id: int) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT id, name, duration, state FROM zones WHERE group_id=? ORDER BY id", (int(group_id),)
                )
                return [dict(r) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("Ошибка получения зон группы %s (min): %s", group_id, e)
            return []
