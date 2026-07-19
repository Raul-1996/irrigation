"""Единый postpone-сервис: откладывание полива группы.

Полный набор шагов для любого канала (HTTP API, Telegram):
postpone_until всем зонам группы, аудируемая остановка активных зон
с публикацией MQTT '0', снятие заданий планировщика (cancel_group_jobs
ставит cancel-event, снимает zone/group_seq-job'ы и перестраивает
расписание группы) и запись postpone_set в журнал.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta

from database import db
from services.mqtt_pub import publish_mqtt_value
from utils import normalize_topic

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

logger = logging.getLogger(__name__)

MIN_POSTPONE_DAYS = 1
MAX_POSTPONE_DAYS = 3
_POSTPONE_MUTATION_LOCK = threading.Lock()


class InvalidPostponeDaysError(ValueError):
    """The requested relative postpone interval is outside the service contract."""

    error_code = "INVALID_POSTPONE_DAYS"


class PostponeConflictError(RuntimeError):
    """A persisted deadline prevents a monotonic group postpone mutation."""

    error_code = "POSTPONE_CONFLICT"

    def __init__(self, message: str, *, zone_id: int | None = None):
        super().__init__(message)
        self.zone_id = zone_id


class PostponeWouldShortenError(PostponeConflictError):
    """The requested deadline is earlier than an existing deadline."""

    error_code = "POSTPONE_WOULD_SHORTEN"


class InvalidExistingPostponeError(PostponeConflictError):
    """A persisted non-null deadline cannot be safely interpreted."""

    error_code = "POSTPONE_INVALID_EXISTING"


def _parse_naive_deadline(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        if not value or value != value.strip():
            raise ValueError("empty or padded deadline")
        parsed = datetime.fromisoformat(value)
    else:
        raise TypeError("deadline must be datetime or string")
    if parsed.tzinfo is not None:
        raise ValueError("timezone-aware deadline is not supported")
    return parsed.replace(microsecond=0)


def apply_group_postpone_deadline(
    group_id: int,
    postpone_until: datetime | str,
    *,
    reason: str,
) -> dict:
    """Atomically-at-service-level apply a monotonic deadline to one group.

    All callers share one lock around the snapshot, validation, and writes.
    Only SQL ``NULL`` is considered unset.  Any malformed non-null value or a
    deadline that would be shortened fails before the first write.  Equal
    deadlines are left untouched so their original reason/provenance survives.

    This lower-level helper is suitable for absolute-deadline producers such as
    the rain monitor; channel-level stop/audit side effects remain the caller's
    responsibility.
    """
    group_id = int(group_id)
    requested = _parse_naive_deadline(postpone_until)
    serialized = requested.strftime("%Y-%m-%d %H:%M:%S")

    with _POSTPONE_MUTATION_LOCK:
        zones = db.get_zones() or []
        group_zones = [z for z in zones if int(z.get("group_id") or 0) == group_id]
        zones_to_update: list[dict] = []

        # Validate the complete snapshot before the first mutation so a single
        # conflicting zone cannot leave the rest of the group partially changed.
        for zone in group_zones:
            current_raw = zone.get("postpone_until")
            if current_raw is None:
                zones_to_update.append(zone)
                continue
            try:
                current = _parse_naive_deadline(current_raw)
            except (TypeError, ValueError) as exc:
                raise InvalidExistingPostponeError(
                    "Сохранённый срок отсрочки повреждён; изменение отклонено",
                    zone_id=int(zone["id"]),
                ) from exc
            if requested < current:
                raise PostponeWouldShortenError(
                    "Новый срок не может сокращать действующую отсрочку",
                    zone_id=int(zone["id"]),
                )
            if requested > current:
                zones_to_update.append(zone)

        for zone in zones_to_update:
            if not db.update_zone_postpone(int(zone["id"]), serialized, reason):
                raise RuntimeError(f"Не удалось сохранить отсрочку зоны {zone['id']}")

        return {
            "postpone_until": serialized,
            "group_zones": group_zones,
            "updated_zone_ids": [int(zone["id"]) for zone in zones_to_update],
        }


def clear_zone_postpone_if_expired(
    zone_id: int,
    observed_deadline: datetime | str | None,
    now: datetime | str,
    *,
    db_facade=None,
) -> bool:
    """CAS-clear one expired deadline without erasing a concurrent extension.

    ``observed_deadline`` must be the raw value from the caller's earlier
    snapshot.  Under the same lock used by group postpone writes, this helper
    re-reads the zone and clears only when that exact value is still current,
    valid, naive, and expired.  ``db_facade`` keeps scheduler instances bound
    to their injected/test database instead of the module singleton.
    """
    facade = db if db_facade is None else db_facade
    try:
        zone_id = int(zone_id)
        if observed_deadline is None:
            return False
        observed = _parse_naive_deadline(observed_deadline)
        checked_at = _parse_naive_deadline(now)
    except (TypeError, ValueError):
        return False

    with _POSTPONE_MUTATION_LOCK:
        zone = facade.get_zone(zone_id)
        if not zone:
            return False
        current_raw = zone.get("postpone_until")
        if current_raw is None:
            return False
        try:
            current = _parse_naive_deadline(current_raw)
        except (TypeError, ValueError):
            return False

        # String snapshots use an exact raw-value CAS.  This rejects even a
        # formatting/provenance rewrite that happens to parse to the same time.
        if isinstance(observed_deadline, str):
            if not isinstance(current_raw, str) or current_raw != observed_deadline:
                return False
        elif current != observed:
            return False
        if current > checked_at:
            return False
        return bool(facade.update_zone_postpone(zone_id, None, None))


def postpone_group(group_id: int, days: int, source: str = "api") -> dict:
    """Отложить полив всех зон группы на ``days`` дней (до конца дня).

    Возвращает {"postpone_until": str-до-конца-дня, "postpone_date": datetime}.
    """
    group_id = int(group_id)
    if isinstance(days, bool) or not isinstance(days, int) or not MIN_POSTPONE_DAYS <= days <= MAX_POSTPONE_DAYS:
        raise InvalidPostponeDaysError(
            f"Количество дней должно быть целым числом от {MIN_POSTPONE_DAYS} до {MAX_POSTPONE_DAYS}"
        )
    postpone_date = datetime.now() + timedelta(days=days)
    requested_until = postpone_date.replace(hour=23, minute=59, second=59, microsecond=0)
    applied = apply_group_postpone_deadline(group_id, requested_until, reason="manual")
    postpone_until = applied["postpone_until"]
    group_zones = applied["group_zones"]
    try:
        for zone in group_zones:
            try:
                if (zone.get("state") == "on") or zone.get("watering_start_time"):
                    # Postpone applies to a group — each zone going OFF
                    # is an audited transition (operator action) that we
                    # want to see in audit_log.
                    try:
                        from services.zones_state import update_zone_state as _uzs

                        _uzs(
                            zone["id"],
                            {"state": "off", "watering_start_time": None},
                            audit_reason="postpone_action",
                        )
                    except (sqlite3.Error, OSError, ImportError):
                        logger.exception(
                            "postpone_group: audited path failed zone=%s — falling back to raw update_zone",
                            zone.get("id"),
                        )
                        db.update_zone(zone["id"], {"state": "off", "watering_start_time": None})
                    sid = zone.get("mqtt_server_id")
                    topic = (zone.get("topic") or "").strip()
                    if mqtt and sid and topic:
                        t = normalize_topic(topic)
                        server = db.get_mqtt_server(int(sid))
                        if server:
                            publish_mqtt_value(server, t, "0", min_interval_sec=0.0, qos=2, retain=True)
            except (ConnectionError, TimeoutError, OSError):
                logger.exception("Ошибка остановки зоны при установке отложенного полива")
        try:
            from irrigation_scheduler import get_scheduler

            scheduler = get_scheduler()
            if scheduler:
                scheduler.cancel_group_jobs(group_id)
        except (ImportError, ValueError, KeyError, RuntimeError):
            logger.exception("Ошибка отмены заданий планировщика при отложенном поливе группы")
    except (ConnectionError, TimeoutError, OSError):
        logger.exception("Ошибка массовой остановки зон при отложенном поливе группы")
    db.add_log(
        "postpone_set", json.dumps({"group": group_id, "days": days, "until": postpone_until, "source": source})
    )
    return {"postpone_until": postpone_until, "postpone_date": postpone_date}
