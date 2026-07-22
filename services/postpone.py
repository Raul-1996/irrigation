"""Единый postpone-сервис: откладывание полива группы.

Полный набор шагов для любого канала (HTTP API, Telegram):
postpone_until всем зонам группы, отмена активной последовательности до OFF,
физически подтверждённая остановка через центральный scheduler/zone-control
контракт, безопасное сохранение retry/cap-задач для неподтверждённых зон и
запись postpone_set в журнал.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta

from database import db

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
            elif requested == current and reason == "manual" and zone.get("postpone_reason") != "manual":
                # An explicit operator/Telegram postpone owns its intent even
                # when the selected deadline equals a rain-owned deadline.
                # Otherwise the later dry edge would clear the user's request.
                zones_to_update.append(zone)

        for zone in zones_to_update:
            if not db.update_zone_postpone(int(zone["id"]), serialized, reason):
                raise RuntimeError(f"Не удалось сохранить отсрочку зоны {zone['id']}")

        return {
            "postpone_until": serialized,
            "group_zones": group_zones,
            "updated_zone_ids": [int(zone["id"]) for zone in zones_to_update],
        }


def apply_group_rain_postpone(
    group_id: int,
    postpone_until: datetime | str,
    *,
    db_facade=None,
) -> dict | bool:
    """Apply a rain-owned deadline without touching any existing ownership.

    The service lock serializes this operation with manual postpone and expiry
    CAS helpers.  The repository then takes a complete group snapshot inside a
    single ``BEGIN IMMEDIATE`` transaction and writes only rows whose deadline
    *and* reason are SQL ``NULL``.  Existing manual, foreign, and earlier rain
    postpones are never extended or rewritten.

    Returns a result dict on success (including a successful no-op), otherwise
    ``False``.  ``db_facade`` keeps monitor/test instances on their injected DB.
    """
    facade = db if db_facade is None else db_facade
    try:
        group_id = int(group_id)
        requested = _parse_naive_deadline(postpone_until)
    except (TypeError, ValueError):
        return False
    serialized = requested.strftime("%Y-%m-%d %H:%M:%S")

    with _POSTPONE_MUTATION_LOCK:
        apply_atomic = getattr(facade, "apply_group_rain_postpone_atomic", None)
        if not callable(apply_atomic):
            logger.error("База данных не поддерживает атомарную установку дождевой отсрочки")
            return False
        try:
            result = apply_atomic(group_id, serialized)
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            logger.exception("Ошибка атомарной установки дождевой отсрочки группы %s", group_id)
            return False
        if result is None or result is False or not isinstance(result, dict):
            return False
        return {
            "postpone_until": serialized,
            "group_zones": list(result.get("group_zones") or []),
            "updated_zone_ids": [int(zone_id) for zone_id in result.get("updated_zone_ids") or []],
        }


def clear_group_rain_postpone(group_id: int, *, db_facade=None) -> bool:
    """Clear current rain ownership without erasing a concurrent/manual value.

    The complete group is re-read and all exact ``postpone_reason == 'rain'``
    rows are cleared in one repository transaction under the shared postpone
    lock.  Any read/write failure is reported as ``False`` and rolls back the
    whole group mutation.
    """
    facade = db if db_facade is None else db_facade
    try:
        group_id = int(group_id)
    except (TypeError, ValueError):
        return False

    with _POSTPONE_MUTATION_LOCK:
        clear_atomic = getattr(facade, "clear_group_rain_postpone_atomic", None)
        if not callable(clear_atomic):
            logger.error("База данных не поддерживает атомарную очистку дождевой отсрочки")
            return False
        try:
            return clear_atomic(group_id) is True
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            logger.exception("Ошибка атомарной очистки дождевой отсрочки группы %s", group_id)
            return False


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


def _postpone_group_zone_ids(group_zones: list[dict]) -> list[int]:
    """Return the complete validated zone-ID set from the postpone snapshot."""
    zone_ids: list[int] = []
    for zone in group_zones:
        try:
            zone_ids.append(int(zone["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(set(zone_ids))


def _invalid_postpone_stop_result(group_id: int, expected_zone_ids: list[int]) -> dict:
    """Return an explicit third bucket when physical evidence is unusable.

    ``unresolved`` is reserved for zones that a valid central aggregate
    explicitly classified.  Relabelling missing, overlapping, or malformed
    evidence as unresolved would invent physical/retry ownership.  Such zones
    therefore remain ``unverified_zone_ids`` and no retry is claimed.
    """
    return {
        "success": False,
        "aggregate_valid": False,
        "stopped": [],
        "unresolved": [],
        "unverified_zone_ids": list(expected_zone_ids),
        "retry_scheduled": False,
        "group_id": int(group_id),
        "error_code": "POSTPONE_STOP_UNAVAILABLE",
    }


def _strict_zone_id_list(value) -> list[int] | None:
    """Parse an aggregate list without coercing, de-duplicating, or guessing."""
    if not isinstance(value, list):
        return None
    if any(type(zone_id) is not int for zone_id in value):
        return None
    if len(value) != len(set(value)):
        return None
    return sorted(value)


def _normalize_postpone_stop_result(
    result,
    *,
    group_id: int,
    expected_zone_ids: list[int],
    source: str,
) -> dict:
    """Strictly validate one complete, disjoint physical stop aggregate.

    Scheduler results carry the canonical seven-field proof shape and may own
    durable retries.  The lower-level zone-control boundary carries a five-field
    immediate-stop shape and must never claim retry ownership.  In both cases,
    success must exactly match an empty unresolved bucket and every expected
    zone must occur once in exactly one physical bucket.
    """
    invalid = _invalid_postpone_stop_result(group_id, expected_zone_ids)
    if not isinstance(result, dict):
        return invalid
    required_keys = {
        "success",
        "group_id",
        "stopped",
        "unresolved",
        "retry_scheduled",
    }
    if source == "scheduler":
        required_keys |= {"aggregate_valid", "unverified_zone_ids"}
    elif source != "core":
        return invalid
    if set(result) != required_keys:
        return invalid

    result_group_id = result.get("group_id")
    success = result.get("success")
    retry_scheduled = result.get("retry_scheduled")
    if (
        type(result_group_id) is not int
        or result_group_id != int(group_id)
        or type(success) is not bool
        or type(retry_scheduled) is not bool
    ):
        return invalid
    if source == "core" and retry_scheduled is not False:
        # The core boundary owns only the immediate confirmed OFF attempt.
        # Durable retry ownership exists exclusively in the scheduler layer.
        return invalid

    if source == "scheduler" and result.get("aggregate_valid") is False:
        return invalid
    if source == "scheduler" and (result.get("aggregate_valid") is not True or result.get("unverified_zone_ids") != []):
        return invalid

    stopped = _strict_zone_id_list(result.get("stopped"))
    unresolved = _strict_zone_id_list(result.get("unresolved"))
    if stopped is None or unresolved is None:
        return invalid
    stopped_set = set(stopped)
    unresolved_set = set(unresolved)
    expected = set(expected_zone_ids)
    if stopped_set & unresolved_set or stopped_set | unresolved_set != expected:
        return invalid
    if success != (not unresolved):
        return invalid
    if retry_scheduled and not unresolved:
        return invalid

    return {
        "success": success,
        "aggregate_valid": True,
        "stopped": stopped,
        "unresolved": unresolved,
        "unverified_zone_ids": [],
        "retry_scheduled": retry_scheduled,
        "group_id": int(group_id),
        "error_code": None if success else "POSTPONE_STOP_PENDING",
    }


def _stop_group_for_postpone(group_id: int, group_zones: list[dict]) -> dict:
    """Cancel the active session first, then require confirmed physical OFF.

    The scheduler is the primary owner because it can atomically set the
    sequence cancel-event before issuing OFF commands and preserve/replant
    hard-stop and cap retries for unresolved zones.  When no scheduler exists,
    the fallback always attempts an immediate central confirmed OFF but refuses
    overall success unless the public session-quiesce boundary also succeeds.
    """
    # Preserve the complete original scope.  If evidence is malformed, these
    # IDs go to the explicit unverified bucket rather than being relabelled
    # from stale DB state as stopped or scheduler-owned unresolved.
    expected_zone_ids = _postpone_group_zone_ids(group_zones)
    try:
        from irrigation_scheduler import get_scheduler

        scheduler = get_scheduler()
    except ImportError:
        scheduler = None
    except (
        AttributeError,
        ConnectionError,
        TimeoutError,
        RuntimeError,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ):
        logger.exception("postpone_group: scheduler lookup failed group=%s", group_id)
        scheduler = None

    if scheduler is not None:
        cancel_group_jobs = getattr(scheduler, "cancel_group_jobs", None)
        if not callable(cancel_group_jobs):
            logger.error("postpone_group: scheduler has no structured cancel_group_jobs group=%s", group_id)
            return _invalid_postpone_stop_result(group_id, expected_zone_ids)
        try:
            result = cancel_group_jobs(int(group_id), master_close_immediately=True)
        except (
            AttributeError,
            ConnectionError,
            TimeoutError,
            RuntimeError,
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
            KeyError,
        ):
            logger.exception("postpone_group: structured scheduler stop failed group=%s", group_id)
            result = None
        return _normalize_postpone_stop_result(
            result,
            group_id=group_id,
            expected_zone_ids=expected_zone_ids,
            source="scheduler",
        )

    try:
        import irrigation_scheduler
        from services import zone_control
    except ImportError:
        logger.exception("postpone_group: central zone-control stop unavailable group=%s", group_id)
        return _invalid_postpone_stop_result(group_id, expected_zone_ids)

    quiesce_group_session = getattr(irrigation_scheduler, "quiesce_group_session", None)
    try:
        session_quiesced = callable(quiesce_group_session) and quiesce_group_session(int(group_id)) is True
    except (
        AttributeError,
        ConnectionError,
        TimeoutError,
        RuntimeError,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
        KeyError,
    ):
        logger.exception("postpone_group: fallback session quiesce failed group=%s", group_id)
        session_quiesced = False
    if not session_quiesced:
        logger.error("postpone_group: fallback session quiesce unresolved group=%s", group_id)

    # Immediate safety is independent of session-quiesce availability.  Always
    # attempt a central confirmed OFF, while refusing overall success until the
    # sequencing owner is also proven quiescent.
    stop_all_in_group = getattr(zone_control, "stop_all_in_group", None)
    try:
        result = (
            stop_all_in_group(
                int(group_id),
                reason="postpone",
                force=True,
                master_close_immediately=True,
                require_observed_confirmation=True,
            )
            if callable(stop_all_in_group)
            else None
        )
    except (
        AttributeError,
        ConnectionError,
        TimeoutError,
        RuntimeError,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
        KeyError,
    ):
        logger.exception("postpone_group: fallback confirmed stop failed group=%s", group_id)
        result = None
    normalized = _normalize_postpone_stop_result(
        result,
        group_id=group_id,
        expected_zone_ids=expected_zone_ids,
        source="core",
    )
    physical_stop_confirmed = normalized["success"] is True
    normalized["physical_stop_confirmed"] = physical_stop_confirmed
    normalized["session_quiesced"] = session_quiesced
    if not session_quiesced:
        normalized["success"] = False
        normalized["error_code"] = "POSTPONE_SESSION_NOT_QUIESCED"
    return normalized


def postpone_group(group_id: int, days: int, source: str = "api") -> dict:
    """Отложить полив всех зон группы на ``days`` дней (до конца дня).

    Срок сохраняется до попытки остановки. Возвращает физически правдивый
    структурированный результат: buckets подтверждённых, unresolved и
    unverified зон, владение retry, общий ``success/pending/error_code``,
    ``postpone_until`` и ``postpone_date``. При неподтверждённом OFF срок
    остаётся установлен, а ``success`` равен ``False``.
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
    stop_result = _stop_group_for_postpone(group_id, group_zones)
    db.add_log(
        "postpone_set",
        json.dumps(
            {
                "group": group_id,
                "days": days,
                "until": postpone_until,
                "source": source,
                "success": stop_result["success"],
                "aggregate_valid": stop_result["aggregate_valid"],
                "stopped": stop_result["stopped"],
                "unresolved": stop_result["unresolved"],
                "unverified_zone_ids": stop_result["unverified_zone_ids"],
                "retry_scheduled": stop_result["retry_scheduled"],
                "session_quiesced": stop_result.get(
                    "session_quiesced",
                    stop_result["aggregate_valid"] is True,
                ),
                "physical_stop_confirmed": stop_result.get(
                    "physical_stop_confirmed",
                    stop_result["success"] is True,
                ),
            }
        ),
    )
    return {
        "success": stop_result["success"],
        "pending": not stop_result["success"],
        "error_code": stop_result["error_code"],
        "group_id": group_id,
        "stopped": stop_result["stopped"],
        "unresolved": stop_result["unresolved"],
        "unverified_zone_ids": stop_result["unverified_zone_ids"],
        "aggregate_valid": stop_result["aggregate_valid"],
        "retry_scheduled": stop_result["retry_scheduled"],
        "session_quiesced": stop_result.get("session_quiesced", stop_result["aggregate_valid"] is True),
        "physical_stop_confirmed": stop_result.get("physical_stop_confirmed", stop_result["success"] is True),
        "postpone_until": postpone_until,
        "postpone_date": postpone_date,
    }
