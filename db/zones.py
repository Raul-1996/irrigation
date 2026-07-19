import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from db.base import BaseRepository, retry_on_busy

logger = logging.getLogger(__name__)

# SEC-004: bulk-импорт (bulk_upsert_zones) обновляет только этот строгий
# whitelist колонок. B1 FIX: 'state' и прочие поля state-machine намеренно
# ИСКЛЮЧЕНЫ — импорт не должен обходить state-machine guard, optimistic-lock
# и audit trail в services.zones_state. Любой вызывающий, который хочет
# менять runtime-состояние зоны, обязан идти через /api/zones/<id>/start|stop
# или services.zones_state.update_zone_state, чтобы был zone_state_change
# audit row.
_ALLOWED_UPDATE_COLUMNS = frozenset(
    {
        "name",
        "icon",
        "duration",
        "group_id",
        "topic",
        "mqtt_server_id",
    }
)


class ZoneRepository(BaseRepository):
    """Repository for zone CRUD, bulk operations, and zone_runs."""

    @staticmethod
    def _build_zone_update_fields(
        merged: dict[str, Any], payload: dict[str, Any], allowed: frozenset[str] | None = None
    ) -> tuple[list[str], list[Any]]:
        """Собрать SET-часть UPDATE zones по единому whitelist'у колонок.

        Only keys explicitly present in ``payload`` are written.  ``merged``
        remains in the signature for compatibility with the bulk callers, but
        must never be used as a source of SET values: replaying a previously
        read full-row snapshot can overwrite concurrent runtime state.

        ``allowed`` дополнительно сужает список колонок (SEC-004: импорт).
        Имена колонок здесь всегда литеральные, значения идут только через
        placeholder'ы — пользовательские данные не попадают в SQL.
        """
        fields: list[str] = []
        params: list[Any] = []

        def add(column: str, value: Any) -> None:
            if allowed is not None and column not in allowed:
                return
            fields.append(f"{column} = ?")
            params.append(value)

        if "name" in payload:
            add("name", payload["name"])
        if "icon" in payload:
            add("icon", payload["icon"])
        if "duration" in payload:
            add("duration", int(payload["duration"]))
        if "group_id" in payload:
            add("group_id", int(payload["group_id"]))
        elif "group" in payload:
            add("group_id", int(payload["group"]))
        if "topic" in payload:
            add("topic", (payload.get("topic") or "").strip())
        if "state" in payload:
            add("state", payload["state"])
        if "postpone_until" in payload:
            add("postpone_until", payload["postpone_until"])
        if "postpone_reason" in payload:
            add("postpone_reason", payload["postpone_reason"])
        if "photo_path" in payload:
            add("photo_path", payload["photo_path"])
        if "watering_start_time" in payload:
            add("watering_start_time", payload["watering_start_time"])
        if "scheduled_start_time" in payload:
            add("scheduled_start_time", payload["scheduled_start_time"])
        # 'last_watering_time' is no longer a column on zones — it is derived
        # from zone_runs.end_utc and injected at read time. Silently ignore
        # the key so legacy callers that still pass it don't crash.
        if "last_avg_flow_lpm" in payload:
            add("last_avg_flow_lpm", payload["last_avg_flow_lpm"])
        if "last_total_liters" in payload:
            add("last_total_liters", payload["last_total_liters"])
        if "mqtt_server_id" in payload:
            add("mqtt_server_id", payload.get("mqtt_server_id"))
        for column in (
            "planned_end_time",
            "watering_start_source",
            "commanded_state",
            "observed_state",
            "fault_count",
            "last_fault",
        ):
            if column in payload:
                add(column, payload[column])

        # updated_at is always set but uses SQL CURRENT_TIMESTAMP — not a
        # parameter, and not user-controllable.
        fields.append("updated_at = CURRENT_TIMESTAMP")
        return fields, params

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
                    SELECT z.*, g.name as group_name, g.use_water_meter as use_water_meter,
                           (
                               SELECT zr.end_utc
                               FROM zone_runs zr
                               WHERE zr.zone_id = z.id
                                 AND zr.status = 'ok'
                                 AND zr.end_utc IS NOT NULL
                               ORDER BY zr.end_utc DESC
                               LIMIT 1
                           ) AS last_watering_time
                    FROM zones z
                    LEFT JOIN groups g ON z.group_id = g.id
                    ORDER BY z.id
                """)
                zones = []
                for row in cursor.fetchall():
                    zone = dict(row)
                    zone["group"] = zone["group_id"]
                    zones.append(zone)
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
                    SELECT z.*, g.name as group_name,
                           (
                               SELECT zr.end_utc
                               FROM zone_runs zr
                               WHERE zr.zone_id = z.id
                                 AND zr.status = 'ok'
                                 AND zr.end_utc IS NOT NULL
                               ORDER BY zr.end_utc DESC
                               LIMIT 1
                           ) AS last_watering_time
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
        """Обновить только явно переданные поля зоны."""
        try:
            with self._connect() as conn:
                if conn.execute("SELECT 1 FROM zones WHERE id = ?", (zone_id,)).fetchone() is None:
                    return None

                sql_fields, params = self._build_zone_update_fields(zone_data, zone_data)
                params.append(zone_id)

                sql = f"""
                    UPDATE zones
                    SET {", ".join(sql_fields)}
                    WHERE id = ?
                """
                conn.execute(sql, params)

                # Если зону переводят в группу 999 — исключаем из всех программ
                target_group_id = None
                if "group_id" in zone_data or "group" in zone_data:
                    target_group_id = int(zone_data.get("group_id", zone_data.get("group")))
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
        except (TypeError, ValueError) as e:
            logger.warning("Некорректные данные обновления зоны %s: %s", zone_id, e)
            return None
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
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute("SELECT * FROM zones WHERE id = ?", (zone_id,))
                row = cur.fetchone()
                if not row:
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
                    try:
                        fields, params = self._build_zone_update_fields(merged, upd)
                    except (TypeError, ValueError) as e:
                        logger.warning("Некорректные данные bulk-обновления зоны %s: %s", zone_id, e)
                        failed.append(zone_id)
                        continue
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
        """Atomically import a batch of zones.

        Import is all-or-nothing: an invalid row, constraint failure or other
        database error rolls back every preceding row.  This invariant belongs
        in the repository because route validation cannot prevent a concurrent
        schema/constraint change between validation and write time.
        """
        created = 0
        updated = 0
        if not zones:
            return {"success": True, "created": 0, "updated": 0, "failed": 0, "errors": []}

        failed_index: int | None = None
        failed_id: Any = None
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                for index, z in enumerate(zones):
                    failed_index = index
                    if not isinstance(z, dict):
                        failed_id = None
                        raise TypeError("zone import row must be an object")
                    candidate_id = z.get("id")
                    failed_id = candidate_id if isinstance(candidate_id, (str, int, float, bool)) else None
                    try:
                        zid = int(z["id"]) if z.get("id") is not None else None
                    except (TypeError, ValueError) as e:
                        logger.debug("import_zones id parse: %s", e)
                        zid = None

                    if zid is not None:
                        cur = conn.execute("SELECT id FROM zones WHERE id = ?", (zid,))
                        row = cur.fetchone()
                        if row:
                            # SEC-004 / B1 FIX: импорт строит UPDATE через
                            # общий builder, суженный до
                            # _ALLOWED_UPDATE_COLUMNS (см. комментарий у
                            # константы) — state-machine-поля исключены.
                            assignments, params = self._build_zone_update_fields(
                                z, z, allowed=_ALLOWED_UPDATE_COLUMNS
                            )
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
                conn.commit()
            return {"success": True, "created": created, "updated": updated, "failed": 0, "errors": []}
        except (sqlite3.Error, TypeError, ValueError, KeyError) as e:
            logger.error("Atomic zone import rolled back at row %s: %s", failed_index, e)
            if isinstance(e, sqlite3.IntegrityError):
                error_code = "constraint_error"
            elif isinstance(e, sqlite3.Error):
                error_code = "database_error"
            else:
                error_code = "invalid_data"
            return {
                "success": False,
                "created": 0,
                "updated": 0,
                "failed": len(zones),
                "rolled_back": True,
                "errors": [{"index": failed_index, "id": failed_id, "code": error_code}],
            }

    @retry_on_busy()
    def delete_zone(self, zone_id: int) -> bool:
        """Delete a zone while retiring its identity and schedule links.

        ``zone_runs`` and water history intentionally remain untouched.  The
        durable-ID migration prevents those retained rows from ever attaching
        to a replacement zone.
        """
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT 1 FROM zones WHERE id = ?", (int(zone_id),)).fetchone() is None:
                    conn.rollback()
                    return False

                program_updates: list[tuple[str, int]] = []
                for program_id, zones_json in conn.execute("SELECT id, zones FROM programs").fetchall():
                    try:
                        identifiers = json.loads(zones_json or "[]")
                    except (json.JSONDecodeError, TypeError) as error:
                        logger.error(
                            "Cannot safely delete zone %s: program %s has malformed zones JSON: %s",
                            zone_id,
                            program_id,
                            error,
                        )
                        conn.rollback()
                        return False
                    if not isinstance(identifiers, list):
                        logger.error(
                            "Cannot safely delete zone %s: program %s zones is not a list",
                            zone_id,
                            program_id,
                        )
                        conn.rollback()
                        return False

                    filtered: list[Any] = []
                    removed = False
                    for identifier in identifiers:
                        try:
                            is_deleted = int(identifier) == int(zone_id)
                        except (TypeError, ValueError):
                            is_deleted = False
                        if is_deleted:
                            removed = True
                        else:
                            filtered.append(identifier)
                    if removed:
                        program_updates.append((json.dumps(filtered, ensure_ascii=False), int(program_id)))

                for zones_json, program_id in program_updates:
                    conn.execute(
                        "UPDATE programs SET zones = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (zones_json, program_id),
                    )
                cursor = conn.execute("DELETE FROM zones WHERE id = ?", (int(zone_id),))
                conn.commit()
                return cursor.rowcount == 1
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
                    SELECT z.*, g.name as group_name,
                           (
                               SELECT zr.end_utc
                               FROM zone_runs zr
                               WHERE zr.zone_id = z.id
                                 AND zr.status = 'ok'
                                 AND zr.end_utc IS NOT NULL
                               ORDER BY zr.end_utc DESC
                               LIMIT 1
                           ) AS last_watering_time
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
                # Evaluate confirmation in the same UPDATE that closes the row;
                # a relay echo committed before this write therefore cannot be
                # overwritten with a status computed from a stale pre-read.
                fields = [
                    "end_utc = ?",
                    "end_monotonic = ?",
                    "status = CASE WHEN ? = 'ok' AND COALESCE(confirmed, 0) = 0 THEN 'failed' ELSE ? END",
                    "updated_at = CURRENT_TIMESTAMP",
                ]
                params: list = [str(end_utc), float(end_monotonic), str(status), str(status)]
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

        The partial covering index
        ``idx_zone_runs_last_ok(zone_id, end_utc DESC)`` keeps this O(log n)
        per zone. Returns ``None`` for a zone that has never been watered (or
        whose only runs are aborted / still open).
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

    @staticmethod
    def _program_start_times(prog: dict[str, Any]) -> list[str]:
        """Основное время программы плюс extra_times (строки 'HH:MM')."""
        times = [str(prog.get("time") or "00:00")]
        extra = prog.get("extra_times") or []
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except (json.JSONDecodeError, TypeError):
                extra = []
        times.extend(str(t) for t in extra)
        return times

    @staticmethod
    def _first_program_start_after(prog: dict[str, Any], hour: int, minute: int, lower_bound: datetime) -> datetime | None:
        """Первый старт программы строго позже ``lower_bound``.

        Семантика повторяет планировщик (irrigation_scheduler._schedule_single_time):
        weekdays — по дням недели (пустые days — никогда), even-odd — по чётности
        числа месяца. Для interval_days > 1 точный якорь живёт только внутри
        APScheduler, поэтому репозиторий возвращает None вместо заведомо ложной
        ежедневной даты.
        """
        schedule_type = prog.get("schedule_type") or "weekdays"
        if schedule_type == "interval":
            try:
                if int(prog.get("interval_days") or 1) > 1:
                    return None
            except (TypeError, ValueError):
                return None
        for offset in range(0, 14):
            d = lower_bound + timedelta(days=offset)
            if schedule_type == "even-odd":
                # NULL в even_odd планировщик трактует как нечётные дни
                want_even = prog.get("even_odd", "even") == "even"
                if (d.day % 2 == 0) != want_even:
                    continue
            elif schedule_type != "interval":
                if d.weekday() not in (prog.get("days") or []):
                    continue
            cand = d.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if cand > lower_bound:
                return cand
        return None

    def compute_next_run_for_zone(self, zone_id: int, programs_getter=None) -> str | None:
        """Рассчитать ближайшее будущее время запуска зоны по всем программам.
        programs_getter: callable that returns list of programs (injected from facade).

        Учитываются только включённые программы (``enabled``), их schedule_type
        (weekdays / interval / even-odd) и все времена старта (``time`` +
        ``extra_times``) — та же семантика, что у живого планировщика.

        Нижняя граница относится к слоту самой зоны, а перед поиском старта
        программы сдвигается назад на накопленный offset предыдущих зон. Так
        программа, уже начавшаяся до ``postpone_until``, остаётся кандидатом,
        если конкретный слот зоны наступит после окончания отложки.
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
            slot_lower_bound = max(now, postpone_dt) if postpone_dt else now
            best_dt: datetime | None = None
            for prog in programs:
                if not prog.get("enabled", True):
                    continue
                if zone_id not in prog.get("zones", []):
                    continue
                cumulative_minutes = 0
                for zid in sorted(prog["zones"]):
                    if zid == zone_id:
                        break
                    cumulative_minutes += self.get_zone_duration(zid)
                else:
                    continue
                program_lower_bound = slot_lower_bound - timedelta(minutes=cumulative_minutes)
                for time_str in self._program_start_times(prog):
                    try:
                        hour, minute = map(int, time_str.split(":", 1))
                    except (ValueError, TypeError):
                        continue
                    start_dt = self._first_program_start_after(prog, hour, minute, program_lower_bound)
                    if not start_dt:
                        continue
                    candidate = start_dt + timedelta(minutes=cumulative_minutes)
                    if best_dt is None or candidate < best_dt:
                        best_dt = candidate
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
