"""Canonical compare-and-swap helpers for zone mutations.

Public/API callers must supply the version they previously read.  Internal
state-machine callers use :func:`update_zone_state_internal` with the complete
snapshot that justified their transition.  Both paths fail closed on a
conflict; neither falls back to an unconditional write.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

import database as _database_mod

logger = logging.getLogger(__name__)

_INTERNAL_GENERATION_FIELDS = (
    "state",
    "commanded_state",
    # A fresh relay report is physical evidence, not incidental metadata.
    # Retrying an older transition across it could overwrite a newly observed
    # ON with stale OFF completion from the previous command generation.
    "observed_state",
    "command_id",
    "sequence_id",
    "watering_start_time",
    "mqtt_server_id",
    "topic",
    "group_id",
)
_INTERNAL_CAS_ATTEMPTS = 3


def _resolve_db(explicit_db: Any | None) -> Any | None:
    if explicit_db is not None:
        return explicit_db
    try:
        from services import zone_control as _zone_control

        resolved = getattr(_zone_control, "db", None)
    except ImportError:
        resolved = None
    return resolved or getattr(_database_mod, "db", None)


def _record_state_audit(
    zone_id: int,
    updates: dict[str, Any],
    previous: dict[str, Any],
    audit_reason: str,
) -> None:
    new_state = updates.get("state")
    previous_state = previous.get("state")
    if new_state is None or str(new_state).lower() == str(previous_state or "").lower():
        return
    try:
        from services.audit import record_audit

        record_audit(
            action_type="zone_state_change",
            source="zones_state",
            target=f"zone:{int(zone_id)}",
            payload={
                "from": previous_state,
                "to": new_state,
                "reason": audit_reason or "unknown",
                "commanded_state": updates.get("commanded_state"),
            },
            actor="system",
        )
    except Exception:
        # Audit is observational; a committed safety transition must not be
        # reported as failed merely because its audit sink is unavailable.
        logger.exception("zone state audit failed zone=%s", zone_id)


def compare_and_swap_zone(
    zone_id: int,
    updates: dict[str, Any],
    *,
    expected_version: int,
    audit_reason: str = "",
    db: Any | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Apply one caller-owned CAS and return ``(applied, conflict_snapshot)``.

    On success the second item is the row before the update.  On a stale
    version it is the current row.  No conflict or database error is ever
    followed by an unconditional update.
    """
    resolved_db = _resolve_db(db)
    if resolved_db is None:
        logger.error("zone CAS has no database zone=%s", zone_id)
        return False, None
    try:
        applied, snapshot = resolved_db.update_zone_versioned(
            int(zone_id),
            updates,
            expected_version=expected_version,
        )
    except (sqlite3.Error, OSError, TypeError, ValueError):
        logger.exception("zone CAS failed zone=%s expected_version=%s", zone_id, expected_version)
        return False, None
    if applied and snapshot is not None:
        _record_state_audit(int(zone_id), updates, snapshot, audit_reason)
    return bool(applied), snapshot


def compare_and_swap_zone_detailed(
    zone_id: int,
    updates: dict[str, Any],
    *,
    expected_version: int,
    audit_reason: str = "",
    db: Any | None = None,
) -> dict[str, Any]:
    """Apply one CAS and retain its canonical in-transaction read model.

    Public CRUD needs the exact committed revision, including derived fields,
    without a post-commit read that could accidentally return a later writer.
    Internal transition callers keep using the compact tuple contract above.
    """
    failure: dict[str, Any] = {
        "success": False,
        "reason": "database_error",
        "previous": None,
        "current": None,
        "affected_program_ids": [],
    }
    resolved_db = _resolve_db(db)
    detailed_update = getattr(resolved_db, "update_zone_versioned_detailed", None)
    if not callable(detailed_update):
        logger.error("detailed zone CAS is unavailable zone=%s", zone_id)
        return failure
    try:
        result = detailed_update(
            int(zone_id),
            updates,
            expected_version=expected_version,
        )
    except (sqlite3.Error, OSError, TypeError, ValueError):
        logger.exception("detailed zone CAS failed zone=%s expected_version=%s", zone_id, expected_version)
        return failure
    if not isinstance(result, dict):
        logger.error("detailed zone CAS returned invalid result zone=%s result=%r", zone_id, result)
        return failure
    if result.get("success") is True and isinstance(result.get("previous"), dict):
        _record_state_audit(int(zone_id), updates, result["previous"], audit_reason)
    return result


def update_zone_state(
    zone_id: int,
    updates: dict[str, Any],
    *,
    expected_version: int,
    audit_reason: str = "",
    db: Any | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Strict state-machine CAS for a caller-owned ``expected_version``."""
    return compare_and_swap_zone(
        zone_id,
        updates,
        expected_version=expected_version,
        audit_reason=audit_reason,
        db=db,
    )


def update_zone_state_internal(
    zone_id: int,
    updates: dict[str, Any],
    *,
    snapshot: dict[str, Any],
    audit_reason: str = "",
    db: Any | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """CAS an internal transition against the snapshot that authorised it.

    The complete snapshot, rather than a user-supplied scalar, makes the
    internal contract explicit.  Physical echo/verifier code must re-read and
    validate its generation before calling this helper.  If any writer wins
    after that read, this transition is rejected instead of being merged into
    a newer activation.
    """
    try:
        snapshot_id = int(snapshot["id"])
        expected_version = snapshot["version"]
    except (KeyError, TypeError, ValueError):
        logger.error("internal zone CAS requires an id/version snapshot zone=%s", zone_id)
        return False, None
    if snapshot_id != int(zone_id):
        logger.error("internal zone CAS snapshot mismatch zone=%s snapshot=%s", zone_id, snapshot_id)
        return False, None
    authorised = snapshot
    current = snapshot
    for _attempt in range(_INTERNAL_CAS_ATTEMPTS):
        applied, conflict = compare_and_swap_zone(
            zone_id,
            updates,
            expected_version=expected_version,
            audit_reason=audit_reason,
            db=db,
        )
        if applied or conflict is None:
            return applied, conflict
        # A version bump caused only by non-generation metadata (for example
        # an API name edit) must not make a physical OFF/echo disappear.  It
        # is safe to retry only while the activation/topology identity that
        # authorised this transition remains byte-for-byte unchanged.
        if any(conflict.get(field) != authorised.get(field) for field in _INTERNAL_GENERATION_FIELDS):
            return False, conflict
        current = conflict
        expected_version = current.get("version")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            return False, current
    return False, current
