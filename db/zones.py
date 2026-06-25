import contextlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from db.base import BaseRepository, retry_on_busy

logger = logging.getLogger(__name__)


class ZoneRepository(BaseRepository):
    """Repository for zone CRUD, bulk operations, and zone_runs."""

    def get_zones(self) -> list[dict[str, Any]]:
        """Получить все зоны.

        Injects ``last_watering_time`` (derived from ``zone_runs.end_utc``)
        into each row so API/UI consumers keep working after the
        ``zones_drop_last_watering_time`` migration.
        """
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT z.*, g.name as group_name, g.use_water_meter as use_water_meter
                    FROM zones z
                    LEFT JOIN groups g ON z.group_id = g.id
                    ORDER BY z.id
                """)
                zones = []
                for row in cursor.fetchall():
                    zone = dict(row)
                    zone["group"] = zone["group_id"]
                    zones.append(zone)
                # Single batched query — derive last_watering_time from
                # zone_runs (idx_zone_runs_active covers it). Done after
                # row.fetchall() so the cursor isn't held while we issue
                # a second statement on the same connection.
                try:
                    cur2 = conn.execute(
                        "SELECT zone_id, MAX(end_utc) FROM zone_runs "
                        "WHERE status = 'ok' AND end_utc IS NOT NULL "
                        "GROUP BY zone_id"
                    )
                    last_map = {int(r[0]): r[1] for r in cur2.fetchall()}
                except sqlite3.Error as e:
                    logger.debug("get_zones: zone_runs aggregation failed: %s", e)
                    last_map = {}
                for z in zones:
                    z["last_watering_time"] = last_map.get(int(z["id"]))
                return zones
        except sqlite3.Error as e:
            logger.error("Ошибка получения зон: %s", e)
            return []

    def get_zone(self, zone_id: int) -> dict[str, Any] | None:
        """Получить зону по ID.

        Injects ``last_watering_time`` derived from ``zone_runs.end_utc``
        (see :meth:`get_last_watering_time`) so consumers don't have to
        know about the schema change.
        """
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT z.*, g.name as group_name
                    FROM zones z
                    LEFT JOIN groups g ON z.group_id = g.id
                    WHERE z.id = ?
                """,
                    (zone_id,),
                )
                row = cursor.fetchone()
                if row:
                    zone = dict(row)
                    zone["group"] = zone["group_id"]
                    zone["last_watering_time"] = self.get_last_watering_time(int(zone_id))
                    return zone
                return None
        except sqlite3.Error as e:
            logger.error("Ошибка получения зоны %s: %s", zone_id, e)
            return None

    @retry_on_busy()
    def create_zone(self, zone_data: dict[str, Any]) -> dict[str, Any] | None:
        """Создать новую зону."""
        try:
            with self._connect() as conn:
                topic = (zone_data.get("topic") or "").strip()
                mqtt_sid = zone_data.get("mqtt_server_id")
                if mqtt_sid is None:
                    # Auto-select only when exactly one enabled server exists
                    try:
                        rows = conn.execute(
                            "SELECT id FROM mqtt_servers WHERE enabled=1 ORDER BY id LIMIT 2"
                        ).fetchall()
                        if len(rows) == 1:
                            mqtt_sid = rows[0][0]
                        # 0 or >1 servers: leave mqtt_sid as None, API layer should validate
                    except (ConnectionError, TimeoutError, OSError):
                        pass
                zid_explicit = None
                try:
                    zid_explicit = int(zone_data.get("id")) if zone_data.get("id") is not None else None
                except (TypeError, ValueError) as e:
                    logger.debug("create_zone explicit id parse: %s", e)
                    zid_explicit = None

                if zid_explicit is not None:
                    try:
                        conn.execute(
                            """
                            INSERT INTO zones (id, name, icon, duration, group_id, topic, mqtt_server_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                            (
                                zid_explicit,
                                zone_data.get("name") or "Зона",
                                zone_data.get("icon") or "🌿",
                                int(zone_data.get("duration") or 10),
                                int(zone_data.get("group_id", zone_data.get("group", 1))),
                                topic,
                                mqtt_sid,
                            ),
                        )
                        conn.commit()
                        return self.get_zone(zid_explicit)
                    except sqlite3.Error:
                        logger.warning("Не удалось вставить зону с явным id=%s, пробуем без id", zid_explicit)

                cursor = conn.execute(
                    """
                    INSERT INTO zones (name, icon, duration, group_id, topic, mqtt_server_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        zone_data.get("name") or "Зона",
                        zone_data.get("icon") or "🌿",
                        int(zone_data.get("duration") or 10),
                        int(zone_data.get("group_id", zone_data.get("group", 1))),
                        topic,
                        mqtt_sid,
                    ),
                )
                zone_id = cursor.lastrowid
                conn.commit()
                return self.get_zone(zone_id)
        except sqlite3.Error as e:
            logger.error("Ошибка создания зоны: %s", e)
            return None

    @retry_on_busy()
    def update_zone(self, zone_id: int, zone_data: dict[str, Any]) -> dict[str, Any] | None:
        """Обновить зону."""
        try:
            with self._connect() as conn:
                current_zone = self.get_zone(zone_id)
                if not current_zone:
                    return None

                updated_data = current_zone.copy()
                updated_data.update(zone_data)

                sql_fields = []
                params = []

                if "name" in updated_data:
                    sql_fields.append("name = ?")
                    params.append(updated_data["name"])
                if "icon" in updated_data:
                    sql_fields.append("icon = ?")
                    params.append(updated_data["icon"])
                if "duration" in updated_data:
                    sql_fields.append("duration = ?")
                    params.append(updated_data["duration"])
                if "group_id" in updated_data or "group" in updated_data:
                    sql_fields.append("group_id = ?")
                    params.append(updated_data.get("group_id", updated_data.get("group", 1)))
                if "topic" in updated_data:
                    sql_fields.append("topic = ?")
                    params.append((updated_data.get("topic") or "").strip())
                if "state" in updated_data:
                    sql_fields.append("state = ?")
                    params.append(updated_data["state"])
                if "postpone_until" in updated_data:
                    sql_fields.append("postpone_until = ?")
                    params.append(updated_data["postpone_until"])
                if "photo_path" in updated_data:
                    sql_fields.append("photo_path = ?")
                    params.append(updated_data["photo_path"])
                if "watering_start_time" in updated_data:
                    sql_fields.append("watering_start_time = ?")
                    params.append(updated_data["watering_start_time"])
                if "scheduled_start_time" in updated_data:
                    sql_fields.append("scheduled_start_time = ?")
                    params.append(updated_data["scheduled_start_time"])
                # 'last_watering_time' is no longer a column on zones —
                # it is derived from zone_runs.end_utc and injected at
                # read time. Silently ignore the key in the update payload
                # so legacy callers that still pass it don't crash.
                if "last_avg_flow_lpm" in updated_data:
                    sql_fields.append("last_avg_flow_lpm = ?")
                    params.append(updated_data["last_avg_flow_lpm"])
                if "last_total_liters" in updated_data:
                    sql_fields.append("last_total_liters = ?")
                    params.append(updated_data["last_total_liters"])
                if "mqtt_server_id" in updated_data:
                    sql_fields.append("mqtt_server_id = ?")
                    params.append(updated_data.get("mqtt_server_id"))
                if "planned_end_time" in zone_data:
                    sql_fields.append("planned_end_time = ?")
                    params.append(zone_data["planned_end_time"])
                if "watering_start_source" in zone_data:
                    sql_fields.append("watering_start_source = ?")
                    params.append(zone_data["watering_start_source"])
                if "commanded_state" in zone_data:
                    sql_fields.append("commanded_state = ?")
                    params.append(zone_data["commanded_state"])
                # PHYS-1 / MASTER-C1: allow StateVerifier._record_fault() to
                # persist observed_state/fault_count/last_fault so zones can
                # be pinned to state='fault' after N MQTT-observation retries.
                if "observed_state" in zone_data:
                    sql_fields.append("observed_state = ?")
                    params.append(zone_data["observed_state"])
                if "fault_count" in zone_data:
                    sql_fields.append("fault_count = ?")
                    params.append(zone_data["fault_count"])
                if "last_fault" in zone_data:
                    sql_fields.append("last_fault = ?")
                    params.append(zone_data["last_fault"])

                sql_fields.append("updated_at = CURRENT_TIMESTAMP")
                params.append(zone_id)

                sql = f"""
                    UPDATE zones
                    SET {", ".join(sql_fields)}
                    WHERE id = ?
                """
                conn.execute(sql, params)

                # Если зону переводят в группу 999 — исключаем из всех программ
                target_group_id = updated_data.get("group_id", updated_data.get("group"))
                if target_group_id == 999:
                    cursor = conn.execute("SELECT id, zones FROM programs")
                    for row in cursor.fetchall():
                        try:
                            zones_list = json.loads(row[1])
                        except (json.JSONDecodeError, TypeError) as e:
                            logger.debug("zones list parse in program %s: %s", row[0], e)
                            continue
                        if zone_id in zones_list:
                            zones_list = [z for z in zones_list if z != zone_id]
                            conn.execute(
                                "UPDATE programs SET zones = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (json.dumps(zones_list), row[0]),
                            )

                conn.commit()
                return self.get_zone(zone_id)
        except sqlite3.Error as e:
            logger.error("Ошибка обновления зоны %s: %s", zone_id, e)
            return None

    @retry_on_busy()
    def update_zone_versioned(self, zone_id: int, updates: dict[str, Any]) -> tuple:
        """Обновить зону с инкрементом version (optimistic lock).

        Returns tuple ``(ok: bool, prev_zone: dict | None)`` where ``prev_zone``
        is the row snapshot **before** the update was applied (None if the row
        didn't exist).  The pre-read and the UPDATE happen in a single
        ``BEGIN IMMEDIATE`` transaction so callers (services.zones_state.
        update_zone_state) can compare prev/new state atomically without a
        TOCTOU race against concurrent writers — important for emitting
        ``zone_state_change`` audit rows that always reflect the actual
        transition.

        Backwards-compat: legacy callers that did ``ok = update_zone_versioned(...)``
        relied on the bool return value.  Tuples are truthy when ok=True, so
        a plain ``if update_zone_versioned(...):`` still works, but
        ``ok = update_zone_versioned(...)`` will now bind ``ok`` to the tuple
        — those few sites have been updated alongside this change.
        """
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                # BEGIN IMMEDIATE → take a write lock right away so the
                # pre-read snapshot is consistent with the row we then UPDATE.
                # Without this, two concurrent versioned updates can both
                # observe the same prev_state and emit duplicate / wrong
                # zone_state_change audit rows.
                try:
                    conn.execute("BEGIN IMMEDIATE")
                except sqlite3.Error:
                    # Already in a transaction (e.g. nested) — fall through
                    # and rely on the implicit one.
                    pass
                cur = conn.execute("SELECT * FROM zones WHERE id = ?", (zone_id,))
                row = cur.fetchone()
                if not row:
                    with contextlib.suppress(sqlite3.Error):
                        conn.commit()
                    return (False, None)
                prev_zone = dict(row)
                old_version = int(prev_zone.get("version") or 0)
                fields = []
                params = []
                for k, v in updates.items():
                    fields.append(f"{k} = ?")
                    params.append(v)
                fields.append("version = version + 1")
                params.extend([zone_id, old_version])
                sql = (
                    f"UPDATE zones SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND version = ?"
                )
                cur2 = conn.execute(sql, params)
                conn.commit()
                return (cur2.rowcount == 1, prev_zone)
        except sqlite3.Error as e:
            logger.error("Ошибка versioned-обновления зоны %s: %s", zone_id, e)
            return (False, None)

    @retry_on_busy()
    def bulk_update_zones(self, updates: list[dict[str, Any]]) -> dict[str, Any]:
        """Пакетное обновление зон в одной транзакции."""
        updated = 0
        failed: list[int] = []
        if not updates:
            return {"updated": 0, "failed": []}
        try:
            with self._connect() as conn:
                for upd in updates:
                    try:
                        zone_id = int(upd.get("id"))
                    except (TypeError, ValueError) as e:
                        logger.debug("batch_update zone id parse: %s", e)
                        continue
                    cur = conn.execute("SELECT * FROM zones WHERE id = ?", (zone_id,))
                    row = cur.fetchone()
                    if not row:
                        failed.append(zone_id)
                        continue
                    current = dict(zip([d[0] for d in cur.description], row))
                    merged = current.copy()
                    merged.update(upd)
                    fields = []
                    params = []

                    def add(field: str, value):
                        fields.append(f"{field} = ?")
                        params.append(value)

                    if "name" in merged:
                        add("name", merged["name"])
                    if "icon" in merged:
                        add("icon", merged["icon"])
                    if "duration" in merged:
                        add("duration", int(merged["duration"]))
                    if ("group_id" in merged) or ("group" in merged):
                        add("group_id", int(merged.get("group_id", merged.get("group", 1))))
                    if "topic" in merged:
                        add("topic", (merged.get("topic") or "").strip())
                    if "state" in merged:
                        add("state", merged["state"])
                    if "postpone_until" in merged:
                        add("postpone_until", merged["postpone_until"])
                    if "postpone_reason" in merged:
                        add("postpone_reason", merged["postpone_reason"])
                    if "photo_path" in merged:
                        add("photo_path", merged["photo_path"])
                    if "watering_start_time" in merged:
                        add("watering_start_time", merged["watering_start_time"])
                    if "scheduled_start_time" in merged:
                        add("scheduled_start_time", merged["scheduled_start_time"])
                    # 'last_watering_time' was dropped — derived from zone_runs now.
                    if "last_avg_flow_lpm" in merged:
                        add("last_avg_flow_lpm", merged["last_avg_flow_lpm"])
                    if "last_total_liters" in merged:
                        add("last_total_liters", merged["last_total_liters"])
                    if "mqtt_server_id" in merged:
                        add("mqtt_server_id", merged.get("mqtt_server_id"))
                    fields.append("updated_at = CURRENT_TIMESTAMP")
                    params.append(zone_id)
                    sql = f"UPDATE zones SET {', '.join(fields)} WHERE id = ?"
                    try:
                        conn.execute(sql, params)
                        updated += 1
                    except sqlite3.Error as e:
                        logger.warning("Ошибка обновления зоны %s в bulk: %s", zone_id, e)
                        failed.append(zone_id)
                conn.commit()
            return {"updated": updated, "failed": failed}
        except sqlite3.Error as e:
            logger.error("Ошибка bulk-обновления зон: %s", e)
            return {"updated": updated, "failed": failed or []}

    @retry_on_busy()
    def bulk_upsert_zones(self, zones: list[dict[str, Any]]) -> dict[str, Any]:
        """Импорт зон: upsert множества зон в одной транзакции."""
        created = 0
        updated = 0
        failed = 0
        if not zones:
            return {"created": 0, "updated": 0, "failed": 0}
        try:
            with self._connect() as conn:
                for z in zones:
                    try:
                        zid = int(z["id"]) if z.get("id") is not None else None
                    except (TypeError, ValueError) as e:
                        logger.debug("import_zones id parse: %s", e)
                        zid = None
                    try:
                        if zid is not None:
                            cur = conn.execute("SELECT id FROM zones WHERE id = ?", (zid,))
                            row = cur.fetchone()
                            if row:
                                # SEC-004: build UPDATE via a strict column
                                # whitelist. Never interpolate a field name
                                # that wasn't preauthorized at import time —
                                # otherwise a future refactor that lets user
                                # data leak into the key side promotes this
                                # to a full SQL injection.
                                #
                                # B1 FIX: 'state' and other state-machine
                                # fields are deliberately EXCLUDED here —
                                # bulk-upsert must never bypass the
                                # state-machine guard, optimistic-lock, and
                                # audit trail in services.zones_state.  Any
                                # caller that wants to change zone runtime
                                # state must use /api/zones/<id>/start|stop
                                # or services.zones_state.update_zone_state
                                # so a zone_state_change audit row is
                                # emitted.
                                _ALLOWED_UPDATE_COLUMNS = {
                                    "name",
                                    "icon",
                                    "duration",
                                    "group_id",
                                    "topic",
                                    "mqtt_server_id",
                                }

                                assignments = []
                                params = []

                                def _set(column: str, value):
                                    if column not in _ALLOWED_UPDATE_COLUMNS:
                                        # Defensive: this branch is only
                                        # reachable if someone edits this
                                        # function and passes a bad name.
                                        raise ValueError(f"refusing to UPDATE unknown zones column: {column!r}")
                                    assignments.append(f"{column} = ?")
                                    params.append(value)

                                if "name" in z:
                                    _set("name", z["name"])
                                if "icon" in z:
                                    _set("icon", z["icon"])
                                if "duration" in z:
                                    _set("duration", int(z["duration"]))
                                if ("group_id" in z) or ("group" in z):
                                    _set("group_id", int(z.get("group_id", z.get("group", 1))))
                                if "topic" in z:
                                    _set("topic", (z.get("topic") or "").strip())
                                # B1 FIX: 'state' deliberately not handled — see
                                # _ALLOWED_UPDATE_COLUMNS comment above.
                                if "mqtt_server_id" in z:
                                    _set("mqtt_server_id", z.get("mqtt_server_id"))
                                # updated_at is always set but uses SQL
                                # CURRENT_TIMESTAMP — not a parameter, and
                                # not user-controllable.
                                assignments.append("updated_at = CURRENT_TIMESTAMP")
                                params.append(zid)
                                if assignments:
                                    conn.execute(
                                        f"UPDATE zones SET {', '.join(assignments)} WHERE id = ?",
                                        params,
                                    )
                                    updated += 1
                            else:
                                conn.execute(
                                    """
                                    INSERT INTO zones (id, name, icon, duration, group_id, topic, mqtt_server_id)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                    (
                                        zid,
                                        z.get("name") or "Зона",
                                        z.get("icon") or "🌿",
                                        int(z.get("duration") or 10),
                                        int(z.get("group_id", z.get("group", 1))),
                                        (z.get("topic") or "").strip(),
                                        z.get("mqtt_server_id"),
                                    ),
                                )
                                created += 1
                        else:
                            conn.execute(
                                """
                                INSERT INTO zones (name, icon, duration, group_id, topic, mqtt_server_id)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """,
                                (
                                    z.get("name") or "Зона",
                                    z.get("icon") or "🌿",
                                    int(z.get("duration") or 10),
                                    int(z.get("group_id", z.get("group", 1))),
                                    (z.get("topic") or "").strip(),
                                    z.get("mqtt_server_id"),
                                ),
                            )
                            created += 1
                    except sqlite3.Error as e:
                        logger.warning("Ошибка upsert зоны: %s", e)
                        failed += 1
                conn.commit()
            return {"created": created, "updated": updated, "failed": failed}
        except sqlite3.Error as e:
            logger.error("Ошибка bulk-импорта зон: %s", e)
            return {"created": created, "updated": updated, "failed": (failed or 0)}

    @retry_on_busy()
    def delete_zone(self, zone_id: int) -> bool:
        """Удалить зону."""
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM zones WHERE id = ?", (zone_id,))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка удаления зоны %s: %s", zone_id, e)
            return False

    def get_zones_by_group(self, group_id: int) -> list[dict[str, Any]]:
        """Получить зоны по группе.

        Injects ``last_watering_time`` from ``zone_runs`` so callers get the
        same schema as :meth:`get_zones`.
        """
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT z.*, g.name as group_name
                    FROM zones z
                    LEFT JOIN groups g ON z.group_id = g.id
                    WHERE z.group_id = ?
                    ORDER BY z.id
                """,
                    (group_id,),
                )
                zones = []
                for row in cursor.fetchall():
                    zone = dict(row)
                    zone["group"] = zone["group_id"]
                    zones.append(zone)
                try:
                    cur2 = conn.execute(
                        "SELECT zone_id, MAX(end_utc) FROM zone_runs "
                        "WHERE status = 'ok' AND end_utc IS NOT NULL "
                        "GROUP BY zone_id"
                    )
                    last_map = {int(r[0]): r[1] for r in cur2.fetchall()}
                except sqlite3.Error as e:
                    logger.debug("get_zones_by_group: zone_runs aggregation failed: %s", e)
                    last_map = {}
                for z in zones:
                    z["last_watering_time"] = last_map.get(int(z["id"]))
                return zones
        except sqlite3.Error as e:
            logger.error("Ошибка получения зон группы %s: %s", group_id, e)
            return []

    @retry_on_busy()
    def clear_group_scheduled_starts(self, group_id: int) -> None:
        """Очистить плановые времена старта у всех зон в группе."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE zones
                    SET scheduled_start_time = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE group_id = ?
                """,
                    (group_id,),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка очистки scheduled_start_time в группе %s: %s", group_id, e)

    @retry_on_busy()
    def set_group_scheduled_starts(self, group_id: int, schedule: dict[int, str]) -> None:
        """Установить плановые времена старта по зоне в группе."""
        try:
            with self._connect() as conn:
                for zone_id, ts in schedule.items():
                    conn.execute(
                        """
                        UPDATE zones
                        SET scheduled_start_time = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND group_id = ?
                    """,
                        (ts, zone_id, group_id),
                    )
                conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка установки расписания scheduled_start_time для группы %s: %s", group_id, e)

    @retry_on_busy()
    def clear_scheduled_for_zone_group_peers(self, zone_id: int, group_id: int) -> None:
        """Очистить scheduled_start_time у всех зон группы, кроме указанной."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE zones
                    SET scheduled_start_time = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE group_id = ? AND id != ?
                """,
                    (group_id, zone_id),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка очистки расписания у одногруппных зон для зоны %s: %s", zone_id, e)

    @retry_on_busy()
    def update_zone_postpone(self, zone_id: int, postpone_until: str | None = None, reason: str | None = None) -> bool:
        """Обновить отложенный полив зоны с указанием причины."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE zones
                    SET postpone_until = ?, postpone_reason = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """,
                    (postpone_until, reason, zone_id),
                )
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка обновления отложенного полива зоны %s: %s", zone_id, e)
            return False

    @retry_on_busy()
    def update_zone_photo(
        self, zone_id: int, photo_path: str | None, photo_thumb: str | None = None, update_thumb: bool = False
    ) -> bool:
        """Обновить фотографию зоны.

        Issue #11: optional ``photo_thumb`` (relative path to 400x400 file).
        Pass ``update_thumb=True`` to update both columns in one statement
        (used by upload + delete). When ``update_thumb=False`` (default),
        only ``photo_path`` is touched — preserves backwards compatibility
        with callers that don't know about thumbs (e.g. legacy CRUD paths).
        """
        try:
            with self._connect() as conn:
                if update_thumb:
                    conn.execute(
                        """
                        UPDATE zones
                        SET photo_path = ?, photo_thumb = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """,
                        (photo_path, photo_thumb, zone_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE zones
                        SET photo_path = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """,
                        (photo_path, zone_id),
                    )
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка обновления фото зоны %s: %s", zone_id, e)
            return False

    def get_zone_duration(self, zone_id: int) -> int:
        """Получить продолжительность полива зоны."""
        try:
            with self._connect() as conn:
                cursor = conn.execute("SELECT duration FROM zones WHERE id = ?", (zone_id,))
                result = cursor.fetchone()
                return result[0] if result else 0
        except sqlite3.Error as e:
            logger.error("Ошибка получения продолжительности зоны %s: %s", zone_id, e)
            return 0

    # --- Zone runs ---
    @retry_on_busy()
    def create_zone_run(
        self,
        zone_id: int,
        group_id: int,
        start_utc: str,
        start_monotonic: float,
        start_raw_pulses: int | None,
        pulse_liters_at_start: int,
        base_m3_at_start: float | None = None,
        *,
        source: str | None = None,
    ) -> int | None:
        """Open a new zone_runs row.

        ``source`` (issue #35) is a keyword-only argument so existing positional
        call-sites stay valid. Accepted values: ``'program'``, ``'manual'``,
        or ``None`` (NULL — unknown, treated as manual by the history backfill).
        """
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO zone_runs(zone_id, group_id, start_utc, start_monotonic, start_raw_pulses, pulse_liters_at_start, base_m3_at_start, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        int(zone_id),
                        int(group_id),
                        str(start_utc),
                        float(start_monotonic),
                        None if start_raw_pulses is None else int(start_raw_pulses),
                        int(pulse_liters_at_start),
                        None if base_m3_at_start is None else float(base_m3_at_start),
                        None if source is None else str(source),
                    ),
                )
                run_id = cur.lastrowid
                conn.commit()
                return int(run_id)
        except sqlite3.Error as e:
            logger.error("Ошибка создания zone_run для зоны %s: %s", zone_id, e)
            return None

    def get_open_zone_run(self, zone_id: int) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    """
                    SELECT * FROM zone_runs WHERE zone_id = ? AND end_utc IS NULL ORDER BY id DESC LIMIT 1
                """,
                    (int(zone_id),),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        except sqlite3.Error as e:
            logger.error("Ошибка чтения открытого run для зоны %s: %s", zone_id, e)
            return None

    def mark_zone_run_confirmed(self, zone_id: int) -> bool:
        """Flag the zone's currently-open run as physically confirmed — the
        relay echoed 'on'. The SSE hub calls this on a real relay-on event so
        finish_zone_run can tell a genuine watering from a command that never
        reached the valve. Idempotent; no-op if there is no open run.
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE zone_runs SET confirmed = 1 WHERE zone_id = ? AND end_utc IS NULL",
                    (int(zone_id),),
                )
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка mark_zone_run_confirmed зоны %s: %s", zone_id, e)
            return False

    @retry_on_busy()
    def finish_zone_run(
        self,
        run_id: int,
        end_utc: str,
        end_monotonic: float,
        end_raw_pulses: int | None,
        total_liters: float | None,
        avg_flow_lpm: float | None,
        status: str = "ok",
    ) -> bool:
        try:
            with self._connect() as conn:
                # History truth: 'ok' is only honest if the relay's physical
                # 'on' was confirmed (confirmed=1) at least once during the run.
                # Downgrade an unconfirmed run to 'failed' so history never
                # claims a watering that didn't physically happen. An explicit
                # non-'ok' status (e.g. 'aborted') is left as-is.
                if status == "ok":
                    try:
                        row = conn.execute(
                            "SELECT confirmed FROM zone_runs WHERE id = ?", (int(run_id),)
                        ).fetchone()
                        if row is not None and not row[0]:
                            status = "failed"
                    except sqlite3.Error:
                        pass
                fields = ["end_utc = ?", "end_monotonic = ?", "status = ?", "updated_at = CURRENT_TIMESTAMP"]
                params: list = [str(end_utc), float(end_monotonic), str(status)]
                if end_raw_pulses is not None:
                    fields.append("end_raw_pulses = ?")
                    params.append(int(end_raw_pulses))
                if total_liters is not None:
                    fields.append("total_liters = ?")
                    params.append(float(total_liters))
                if avg_flow_lpm is not None:
                    fields.append("avg_flow_lpm = ?")
                    params.append(float(avg_flow_lpm))
                params.append(int(run_id))
                sql = f"UPDATE zone_runs SET {', '.join(fields)} WHERE id = ?"
                conn.execute(sql, params)
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка завершения zone_run %s: %s", run_id, e)
            return False

    def get_last_watering_time(self, zone_id: int) -> str | None:
        """Return the most recent successful watering end-time for a zone.

        Single source of truth = ``zone_runs``. The denormalised
        ``zones.last_watering_time`` column was dropped by migration
        ``zones_drop_last_watering_time``; this helper computes the value
        on-demand from ``MAX(end_utc)`` over rows with ``status='ok'`` and
        a non-NULL ``end_utc`` (i.e. the run actually finished cleanly).

        The covering index ``idx_zone_runs_active(zone_id, end_utc)`` keeps
        this O(log n) per zone. Returns ``None`` for a zone that has never
        been watered (or whose only runs are aborted / still open).
        """
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT MAX(end_utc) FROM zone_runs WHERE zone_id = ? AND status = 'ok' AND end_utc IS NOT NULL",
                    (int(zone_id),),
                )
                row = cur.fetchone()
                return row[0] if row and row[0] else None
        except sqlite3.Error as e:
            logger.error("get_last_watering_time(%s): %s", zone_id, e)
            return None

    @staticmethod
    def _parse_postpone_dt(s: str | None) -> datetime | None:
        """Local datetime parser mirroring irrigation_scheduler._parse_dt.

        Duplicated (instead of imported from services.helpers) to avoid a
        layering violation: db/* must not depend on services/*.
        """
        if not s:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(s, fmt)
            except (ValueError, TypeError, KeyError):
                continue
        return None

    def compute_next_run_for_zone(self, zone_id: int, programs_getter=None) -> str | None:
        """Рассчитать ближайшее будущее время запуска зоны по всем программам.
        programs_getter: callable that returns list of programs (injected from facade).

        Если у зоны установлен ``postpone_until`` в будущем, нижняя граница
        поиска (``now``) сдвигается вперёд до конца окна отложки — иначе UI
        показывал бы ближайший старт программы внутри отложенного окна, хотя
        реальный планировщик его пропустит.
        """
        try:
            zone = self.get_zone(zone_id)
            if not zone:
                return None
            programs = programs_getter() if programs_getter else []
            if not programs:
                return None
            now = datetime.now()
            postpone_dt = self._parse_postpone_dt(zone.get("postpone_until"))
            if postpone_dt and postpone_dt > now:
                now = postpone_dt
            best_dt: datetime | None = None
            for prog in programs:
                if zone_id not in prog.get("zones", []):
                    continue
                for offset in range(0, 14):
                    dt_candidate = now + timedelta(days=offset)
                    if dt_candidate.weekday() in prog["days"]:
                        hour, minute = map(int, prog["time"].split(":"))
                        start_dt = dt_candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        if start_dt <= now:
                            continue
                        cum = 0
                        for zid in sorted(prog["zones"]):
                            dur = self.get_zone_duration(zid)
                            if zid == zone_id:
                                candidate = start_dt + timedelta(minutes=cum)
                                if best_dt is None or candidate < best_dt:
                                    best_dt = candidate
                                break
                            cum += dur
                        break
            if best_dt:
                return best_dt.strftime("%Y-%m-%d %H:%M:%S")
            return None
        except (sqlite3.Error, OSError) as e:
            logger.exception("Ошибка расчета следующего запуска для зоны %s: %s", zone_id, e)
            return None

    def reschedule_group_to_next_program(self, group_id: int, programs_getter=None) -> None:
        """Пересчитать и записать scheduled_start_time всем зонам группы."""
        try:
            zones = self.get_zones_by_group(group_id)
            schedule: dict[int, str] = {}
            for z in zones:
                nxt = self.compute_next_run_for_zone(z["id"], programs_getter=programs_getter)
                if nxt:
                    schedule[z["id"]] = nxt
            self.clear_group_scheduled_starts(group_id)
            if schedule:
                self.set_group_scheduled_starts(group_id, schedule)
        except (sqlite3.Error, OSError) as e:
            logger.exception("Ошибка перестройки расписания группы %s: %s", group_id, e)
