import json
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from db.base import BaseRepository, retry_on_busy

logger = logging.getLogger(__name__)

_TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")
_VALID_PROGRAM_TYPES = {"time-based", "smart"}
_VALID_SCHEDULE_TYPES = {"weekdays", "interval", "even-odd"}
_CONFLICT_HORIZON_DAYS = 930


def _time_minutes(value: object) -> int | None:
    if not isinstance(value, str) or _TIME_RE.fullmatch(value) is None:
        return None
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _read_json_list(value: object) -> list[Any]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    return decoded if isinstance(decoded, list) else []


def _read_int_list(value: object, *, minimum: int, maximum: int | None = None) -> list[int]:
    result: list[int] = []
    for item in _read_json_list(value):
        if isinstance(item, bool):
            continue
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed < minimum or (maximum is not None and parsed > maximum):
            continue
        if parsed not in result:
            result.append(parsed)
    return result


def _read_time_list(value: object) -> list[str]:
    result: list[str] = []
    for item in _read_json_list(value):
        if _time_minutes(item) is not None and item not in result:
            result.append(item)
    return result


def _read_anchor_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


class ProgramRepository(BaseRepository):
    """Repository for program CRUD, conflicts, and cancellations."""

    @staticmethod
    def _decode_program_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        program = dict(row)
        program["days"] = _read_int_list(program.get("days"), minimum=0, maximum=6)
        program["zones"] = _read_int_list(program.get("zones"), minimum=1)
        program["extra_times"] = [
            slot for slot in _read_time_list(program.get("extra_times", "[]")) if slot != program.get("time")
        ]
        program["enabled"] = bool(program.get("enabled", 1))
        if program.get("schedule_type") == "even_odd":
            program["schedule_type"] = "even-odd"
        return program

    @staticmethod
    def _normalize_write_payload(program_data: dict[str, Any], *, create: bool) -> dict[str, Any] | None:
        if not isinstance(program_data, dict):
            return None
        normalized = dict(program_data)

        if create and any(field not in normalized for field in ("name", "time", "zones")):
            return None
        if "name" in normalized and (not isinstance(normalized["name"], str) or not normalized["name"].strip()):
            return None
        if "time" in normalized and _time_minutes(normalized["time"]) is None:
            return None

        if "days" in normalized:
            days = normalized["days"]
            if not isinstance(days, list) or any(
                isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in days
            ):
                return None
            normalized["days"] = sorted(set(days))
        elif create:
            normalized["days"] = []

        if "zones" in normalized:
            zones = normalized["zones"]
            if not isinstance(zones, list) or any(
                isinstance(zone_id, bool) or not isinstance(zone_id, int) or zone_id <= 0 for zone_id in zones
            ):
                return None
            if len(zones) != len(set(zones)):
                return None
            normalized["zones"] = list(zones)

        if "extra_times" in normalized:
            extra_times = normalized["extra_times"]
            if not isinstance(extra_times, list) or any(_time_minutes(item) is None for item in extra_times):
                return None
            if len(extra_times) != len(set(extra_times)):
                return None
            if "time" in normalized and normalized["time"] in extra_times:
                return None
            normalized["extra_times"] = list(extra_times)
        elif create:
            normalized["extra_times"] = []

        if normalized.get("schedule_type") == "even_odd":
            normalized["schedule_type"] = "even-odd"
        if "type" in normalized and normalized["type"] not in _VALID_PROGRAM_TYPES:
            return None
        if "schedule_type" in normalized and normalized["schedule_type"] not in _VALID_SCHEDULE_TYPES:
            return None
        if "interval_days" in normalized and normalized["interval_days"] is not None:
            interval_days = normalized["interval_days"]
            if isinstance(interval_days, bool) or not isinstance(interval_days, int) or not 1 <= interval_days <= 30:
                return None
        if "even_odd" in normalized and normalized["even_odd"] not in (None, "even", "odd"):
            return None
        schedule_type = normalized.get("schedule_type", "weekdays")
        if schedule_type == "interval" and normalized.get("interval_days") is None:
            return None
        if "enabled" in normalized:
            enabled = normalized["enabled"]
            if not isinstance(enabled, bool) and enabled not in (0, 1):
                return None
        return normalized

    def get_programs(self) -> list[dict[str, Any]]:
        """Получить все программы."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("SELECT * FROM programs ORDER BY id")
                return [self._decode_program_row(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("Ошибка получения программ: %s", e)
            return []

    def get_program(self, program_id: int) -> dict[str, Any] | None:
        """Получить программу по ID."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("SELECT * FROM programs WHERE id = ?", (program_id,))
                row = cursor.fetchone()
                if row:
                    return self._decode_program_row(row)
                return None
        except sqlite3.Error as e:
            logger.error("Ошибка получения программы %s: %s", program_id, e)
            return None

    @retry_on_busy()
    def create_program(self, program_data: dict[str, Any]) -> dict[str, Any] | None:
        """Создать новую программу."""
        normalized = self._normalize_write_payload(program_data, create=True)
        if normalized is None:
            logger.warning("Отказ создания программы: неканонические поля")
            return None
        try:
            with self._connect() as conn:
                # v2 fields with defaults
                cursor = conn.execute(
                    """
                    INSERT INTO programs (name, time, days, zones, type, schedule_type,
                                          interval_days, even_odd, color, enabled, extra_times)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        normalized["name"],
                        normalized["time"],
                        json.dumps(normalized["days"]),
                        json.dumps(normalized["zones"]),
                        normalized.get("type", "time-based"),
                        normalized.get("schedule_type", "weekdays"),
                        normalized.get("interval_days"),
                        normalized.get("even_odd"),
                        normalized.get("color", "#42a5f5"),
                        1 if normalized.get("enabled", True) else 0,
                        json.dumps(normalized["extra_times"]),
                    ),
                )
                program_id = cursor.lastrowid
                conn.commit()
                return self.get_program(program_id)
        except (sqlite3.Error, KeyError, TypeError, ValueError) as e:
            logger.error("Ошибка создания программы: %s", e)
            return None

    @retry_on_busy()
    def update_program(self, program_id: int, program_data: dict[str, Any]) -> dict[str, Any] | None:
        """Обновить программу."""
        current = self.get_program(program_id)
        if current is None:
            return None
        effective = self._normalize_write_payload({**current, **program_data}, create=True)
        if effective is None:
            logger.warning("Отказ обновления программы %s: неканонические поля", program_id)
            return None
        normalized = {
            field: effective[field]
            for field in program_data
            if field in effective and field not in ("id", "created_at", "updated_at")
        }
        try:
            with self._connect() as conn:
                # Build dynamic UPDATE based on provided fields
                updates = []
                params = []

                # Core fields
                if "name" in normalized:
                    updates.append("name = ?")
                    params.append(normalized["name"])
                if "time" in normalized:
                    updates.append("time = ?")
                    params.append(normalized["time"])
                if "days" in normalized:
                    updates.append("days = ?")
                    params.append(json.dumps(normalized["days"]))
                if "zones" in normalized:
                    updates.append("zones = ?")
                    params.append(json.dumps(normalized["zones"]))

                # v2 fields
                if "type" in normalized:
                    updates.append("type = ?")
                    params.append(normalized["type"])
                if "schedule_type" in normalized:
                    updates.append("schedule_type = ?")
                    params.append(normalized["schedule_type"])
                if "interval_days" in normalized:
                    updates.append("interval_days = ?")
                    params.append(normalized["interval_days"])
                if "even_odd" in normalized:
                    updates.append("even_odd = ?")
                    params.append(normalized["even_odd"])
                if "color" in normalized:
                    updates.append("color = ?")
                    params.append(normalized["color"])
                if "enabled" in normalized:
                    updates.append("enabled = ?")
                    params.append(1 if normalized["enabled"] else 0)
                if "extra_times" in normalized:
                    updates.append("extra_times = ?")
                    params.append(json.dumps(normalized["extra_times"]))

                if not updates:
                    return self.get_program(program_id)

                updates.append("updated_at = CURRENT_TIMESTAMP")
                sql = f"UPDATE programs SET {', '.join(updates)} WHERE id = ?"
                params.append(program_id)

                cursor = conn.execute(sql, params)
                if cursor.rowcount != 1:
                    return None
                conn.commit()
                return self.get_program(program_id)
        except (sqlite3.Error, KeyError, TypeError, ValueError) as e:
            logger.error("Ошибка обновления программы %s: %s", program_id, e)
            return None

    @retry_on_busy()
    def delete_program(self, program_id: int) -> bool:
        """Удалить программу."""
        try:
            with self._connect() as conn:
                cursor = conn.execute("DELETE FROM programs WHERE id = ?", (program_id,))
                if cursor.rowcount != 1:
                    return False
                conn.execute("DELETE FROM program_cancellations WHERE program_id = ?", (program_id,))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка удаления программы %s: %s", program_id, e)
            return False

    @retry_on_busy()
    def duplicate_program(self, program_id: int) -> dict[str, Any] | None:
        """Дублировать программу (создать копию с суффиксом '(копия)')."""
        try:
            original = self.get_program(program_id)
            if not original:
                logger.error("Программа %s не найдена для дублирования", program_id)
                return None

            # Копируем все поля кроме id, created_at, updated_at
            copy_data = {k: v for k, v in original.items() if k not in ("id", "created_at", "updated_at")}
            copy_data["name"] = original["name"] + " (копия)"

            return self.create_program(copy_data)
        except (KeyError, TypeError, ValueError) as e:
            logger.error("Ошибка дублирования программы %s: %s", program_id, e)
            return None

    def check_program_conflicts(
        self,
        program_id: int | None = None,
        time: str | None = None,
        zones: list[int] | None = None,
        days: list[int] | None = None,
        weather_factor: int | None = None,
        include_weather: bool = False,
        extra_times: list[str] | None = None,
        schedule_type: str = "weekdays",
        interval_days: int | None = None,
        even_odd: str | None = None,
        candidate_interval_anchors: dict[str, object] | None = None,
        stored_interval_anchors: dict[int, dict[str, object]] | None = None,
        raise_on_error: bool = False,
    ) -> Any:
        """Return schedule-aware overlaps with stored programs and own slots.

        The 930-day recurrence horizon covers weekday, calendar parity and
        every supported 1..30 day interval cycle, including windows which
        cross midnight. Interval anchors must come from live/persisted
        scheduler triggers. Missing anchors are modeled fail-closed as a
        possible daily occurrence and marked ``anchor_unknown``.
        """
        v2 = weather_factor is not None or include_weather
        current_coeff = 100
        empty_v2 = {"has_conflicts": False, "conflicts": [], "current_weather_coefficient": current_coeff}

        candidate_days = _read_int_list(days, minimum=0, maximum=6)
        candidate_zones = _read_int_list(zones, minimum=1)
        candidate_slots = [slot for slot in [time, *_read_time_list(extra_times)] if _time_minutes(slot) is not None]
        candidate_schedule = "even-odd" if schedule_type == "even_odd" else schedule_type
        if (
            not candidate_slots
            or not candidate_zones
            or candidate_schedule not in _VALID_SCHEDULE_TYPES
            or (candidate_schedule == "weekdays" and not candidate_days)
            or (
                candidate_schedule == "interval"
                and (
                    isinstance(interval_days, bool)
                    or not isinstance(interval_days, int)
                    or not 1 <= interval_days <= 30
                )
            )
            or (candidate_schedule == "even-odd" and even_odd not in ("even", "odd"))
        ):
            return empty_v2 if v2 else []

        model_base_date = date.today()

        def scheduled_on(day_value: date, spec: dict[str, Any], slot: str) -> bool:
            kind = spec["schedule_type"]
            if kind == "weekdays":
                return day_value.weekday() in spec["days"]
            if kind == "even-odd":
                return (day_value.day % 2 == 0) == (spec["even_odd"] == "even")
            anchor = _read_anchor_date(spec["interval_anchors"].get(slot))
            if anchor is None:
                return True
            delta = (day_value - anchor).days
            return delta >= 0 and delta % spec["interval_days"] == 0

        def occurrence_starts(slot: str, spec: dict[str, Any]) -> list[int]:
            minute = _time_minutes(slot)
            if minute is None:
                return []
            starts = []
            for offset in range(-2, _CONFLICT_HORIZON_DAYS + 2):
                day_value = model_base_date + timedelta(days=offset)
                if scheduled_on(day_value, spec, slot):
                    starts.append(offset * 1440 + minute)
            return starts

        def first_overlap(
            left_starts: list[int],
            left_duration: float,
            right_starts: list[int],
            right_duration: float,
        ) -> tuple[float, int, int] | None:
            if left_duration <= 0 or right_duration <= 0:
                return None
            left_index = 0
            right_index = 0
            while left_index < len(left_starts) and right_index < len(right_starts):
                left_start = left_starts[left_index]
                right_start = right_starts[right_index]
                left_end = left_start + left_duration
                right_end = right_start + right_duration
                overlap = min(left_end, right_end) - max(left_start, right_start)
                if overlap > 0:
                    return overlap, left_start, right_start
                if left_end <= right_end:
                    left_index += 1
                else:
                    right_index += 1
            return None

        candidate_spec = {
            "schedule_type": candidate_schedule,
            "days": candidate_days,
            "interval_days": interval_days,
            "even_odd": even_odd,
            "interval_anchors": candidate_interval_anchors or {},
        }

        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(programs)").fetchall()}

                def selected(name: str, fallback: str) -> str:
                    return name if name in columns else f"{fallback} AS {name}"

                selected_columns = [
                    "id",
                    "name",
                    "time",
                    "days",
                    "zones",
                    selected("extra_times", "'[]'"),
                    selected("schedule_type", "'weekdays'"),
                    selected("interval_days", "NULL"),
                    selected("even_odd", "NULL"),
                ]
                query = f"SELECT {', '.join(selected_columns)} FROM programs"
                conditions: list[str] = []
                params: list[Any] = []
                if "enabled" in columns:
                    conditions.append("COALESCE(enabled, 1) != 0")
                if program_id is not None:
                    conditions.append("id != ?")
                    params.append(program_id)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                stored_programs = [dict(row) for row in conn.execute(query, params).fetchall()]

                try:
                    from services.weather_adjustment import get_weather_adjustment

                    adjustment = get_weather_adjustment(self.db_path)
                    if adjustment and adjustment.is_enabled():
                        current_coeff = int(adjustment.get_coefficient())
                except Exception:
                    logger.debug("Не удалось прочитать текущий погодный коэффициент", exc_info=True)

                if include_weather and weather_factor is None:
                    try:
                        row = conn.execute(
                            "SELECT value FROM settings WHERE key = 'max_weather_coefficient'"
                        ).fetchone()
                        if row and row["value"]:
                            weather_factor = int(row["value"])
                    except (sqlite3.Error, ValueError, TypeError):
                        weather_factor = None

                durations: dict[int, int] = {}
                groups: dict[int, int] = {}
                for row in conn.execute("SELECT id, duration, group_id FROM zones").fetchall():
                    durations[int(row["id"])] = max(0, int(row["duration"] or 0))
                    groups[int(row["id"])] = int(row["group_id"] or 0)
                group_names: dict[int, str] = {}
                try:
                    for row in conn.execute("SELECT id, name FROM groups").fetchall():
                        group_names[int(row["id"])] = str(row["name"] or "")
                except sqlite3.Error:
                    logger.debug("Не удалось прочитать имена групп для конфликтов")

                candidate_duration = sum(durations.get(zone_id, 0) for zone_id in candidate_zones)
                candidate_groups = {groups.get(zone_id, 0) for zone_id in candidate_zones}
                candidate_occurrences = {
                    slot: occurrence_starts(slot, candidate_spec) for slot in dict.fromkeys(candidate_slots)
                }
                legacy_conflicts: list[dict[str, Any]] = []
                v2_conflicts: list[dict[str, Any]] = []
                factor = max(100, int(weather_factor or 100))

                # A program invocation is single-session: its own main and extra
                # starts must not overlap, even before it is inserted into DB.
                unique_slots = list(candidate_occurrences)
                for left_index, left_slot in enumerate(unique_slots):
                    for right_slot in unique_slots[left_index + 1 :]:
                        base = first_overlap(
                            candidate_occurrences[left_slot],
                            candidate_duration,
                            candidate_occurrences[right_slot],
                            candidate_duration,
                        )
                        weather = None
                        if factor > 100:
                            weather = first_overlap(
                                candidate_occurrences[left_slot],
                                candidate_duration * factor / 100.0,
                                candidate_occurrences[right_slot],
                                candidate_duration * factor / 100.0,
                            )
                        if base is None and weather is None:
                            continue
                        overlap, left_start, right_start = base or weather  # type: ignore[misc]
                        anchor_unknown = candidate_schedule == "interval" and any(
                            _read_anchor_date(candidate_spec["interval_anchors"].get(slot)) is None
                            for slot in (left_slot, right_slot)
                        )
                        if v2:
                            level = "error" if base is not None else "warning"
                            v2_conflicts.append(
                                {
                                    "program_id": program_id,
                                    "program_name": "Текущая программа",
                                    "level": level,
                                    "overlap_minutes": round(overlap, 1),
                                    "weather_factor": 100 if level == "error" else factor,
                                    "group_id": next(iter(candidate_groups), 0),
                                    "group_name": "",
                                    "candidate_self_conflict": True,
                                    "anchor_unknown": anchor_unknown,
                                    "message": (
                                        "Основное и дополнительное время текущей программы пересекаются"
                                        if level == "error"
                                        else "Основное и дополнительное время пересекаются с погодным коэффициентом"
                                    ),
                                }
                            )
                        else:
                            legacy_conflicts.append(
                                {
                                    "program_id": program_id,
                                    "program_name": "Текущая программа",
                                    "program_time": right_slot,
                                    "program_duration": candidate_duration,
                                    "common_zones": candidate_zones,
                                    "common_groups": sorted(candidate_groups),
                                    "common_days": candidate_days,
                                    "overlap_start": max(left_start, right_start),
                                    "overlap_end": min(
                                        left_start + candidate_duration,
                                        right_start + candidate_duration,
                                    ),
                                    "candidate_self_conflict": True,
                                    "anchor_unknown": anchor_unknown,
                                }
                            )

                for stored in stored_programs:
                    stored_zones = _read_int_list(stored.get("zones"), minimum=1)
                    stored_days = _read_int_list(stored.get("days"), minimum=0, maximum=6)
                    stored_schedule = stored.get("schedule_type") or "weekdays"
                    if stored_schedule == "even_odd":
                        stored_schedule = "even-odd"
                    stored_interval = stored.get("interval_days")
                    stored_parity = stored.get("even_odd")
                    if stored_schedule == "even-odd" and stored_parity is None:
                        stored_parity = "odd"
                    if (
                        stored_schedule not in _VALID_SCHEDULE_TYPES
                        or (stored_schedule == "weekdays" and not stored_days)
                        or (
                            stored_schedule == "interval"
                            and (
                                isinstance(stored_interval, bool)
                                or not isinstance(stored_interval, int)
                                or not 1 <= stored_interval <= 30
                            )
                        )
                        or (stored_schedule == "even-odd" and stored_parity not in ("even", "odd"))
                    ):
                        continue

                    stored_groups = {groups.get(zone_id, 0) for zone_id in stored_zones}
                    common_groups = candidate_groups & stored_groups
                    common_zones = set(candidate_zones) & set(stored_zones)
                    if not common_groups:
                        continue
                    stored_duration = sum(durations.get(zone_id, 0) for zone_id in stored_zones)
                    stored_spec = {
                        "schedule_type": stored_schedule,
                        "days": stored_days,
                        "interval_days": stored_interval,
                        "even_odd": stored_parity,
                        "interval_anchors": (stored_interval_anchors or {}).get(int(stored["id"]), {}),
                    }
                    stored_slots = [stored.get("time"), *_read_time_list(stored.get("extra_times"))]
                    stored_slots = [slot for slot in dict.fromkeys(stored_slots) if _time_minutes(slot) is not None]
                    stored_occurrences = {slot: occurrence_starts(slot, stored_spec) for slot in stored_slots}

                    for candidate_slot, candidate_starts in candidate_occurrences.items():
                        for stored_slot, stored_starts in stored_occurrences.items():
                            base = first_overlap(
                                candidate_starts,
                                candidate_duration,
                                stored_starts,
                                stored_duration,
                            )
                            weather = None
                            if factor > 100:
                                weather = first_overlap(
                                    candidate_starts,
                                    candidate_duration * factor / 100.0,
                                    stored_starts,
                                    stored_duration * factor / 100.0,
                                )
                            if base is None and weather is None:
                                continue

                            overlap, candidate_start, stored_start = base or weather  # type: ignore[misc]
                            anchor_unknown = (
                                candidate_schedule == "interval"
                                and _read_anchor_date(candidate_spec["interval_anchors"].get(candidate_slot)) is None
                            ) or (
                                stored_schedule == "interval"
                                and _read_anchor_date(stored_spec["interval_anchors"].get(stored_slot)) is None
                            )
                            if v2:
                                level = "error" if base is not None else "warning"
                                applied_factor = 100 if base is not None else factor
                                for group_id in sorted(common_groups):
                                    v2_conflicts.append(
                                        {
                                            "program_id": stored["id"],
                                            "program_name": stored["name"],
                                            "level": level,
                                            "overlap_minutes": round(overlap, 1),
                                            "weather_factor": applied_factor,
                                            "group_id": group_id,
                                            "group_name": group_names.get(group_id, ""),
                                            "anchor_unknown": anchor_unknown,
                                            "message": (
                                                f'Конфликт при базовой длительности с программой "{stored["name"]}"'
                                                if level == "error"
                                                else f"Конфликт при погодном коэфф. {factor}% "
                                                f'с программой "{stored["name"]}"'
                                            ),
                                        }
                                    )
                            else:
                                legacy_conflicts.append(
                                    {
                                        "program_id": stored["id"],
                                        "program_name": stored["name"],
                                        "program_time": stored_slot,
                                        "program_duration": stored_duration,
                                        "common_zones": sorted(common_zones),
                                        "common_groups": sorted(common_groups),
                                        "common_days": sorted(set(candidate_days) & set(stored_days)),
                                        "overlap_start": max(candidate_start, stored_start),
                                        "overlap_end": min(
                                            candidate_start + candidate_duration,
                                            stored_start + stored_duration,
                                        ),
                                        "candidate_time": candidate_slot,
                                        "anchor_unknown": anchor_unknown,
                                    }
                                )

                if v2:
                    return {
                        "has_conflicts": bool(v2_conflicts),
                        "conflicts": v2_conflicts,
                        "current_weather_coefficient": current_coeff,
                    }
                return legacy_conflicts
        except sqlite3.Error as error:
            logger.error("Ошибка проверки пересечения программ: %s", error)
            if raise_on_error:
                raise
            if v2:
                return {"has_conflicts": False, "conflicts": [], "current_weather_coefficient": current_coeff}
            return []

    # === Program cancellations (per date) ===
    @retry_on_busy()
    def cancel_program_run_for_group(self, program_id: int, run_date: str, group_id: int) -> bool:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO program_cancellations(program_id, run_date, group_id)
                    VALUES (?, ?, ?)
                """,
                    (int(program_id), str(run_date), int(group_id)),
                )
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("Ошибка записи отмены программы %s на %s для группы %s: %s", program_id, run_date, group_id, e)
            return False

    def is_program_run_cancelled_for_group(self, program_id: int, run_date: str, group_id: int) -> bool:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    """
                    SELECT 1 FROM program_cancellations
                    WHERE program_id = ? AND run_date = ? AND group_id = ? LIMIT 1
                """,
                    (int(program_id), str(run_date), int(group_id)),
                )
                return cur.fetchone() is not None
        except sqlite3.Error as e:
            logger.error("Ошибка чтения отмены программы %s на %s для группы %s: %s", program_id, run_date, group_id, e)
            return False

    @retry_on_busy()
    def clear_program_cancellations_for_group_on_date(self, group_id: int, run_date: str) -> bool:
        """Удалить все отмены программ для указанной группы на указанную дату."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    DELETE FROM program_cancellations
                    WHERE group_id = ? AND run_date = ?
                """,
                    (int(group_id), str(run_date)),
                )
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("Ошибка очистки отмен программ на %s для группы %s: %s", run_date, group_id, e)
            return False
