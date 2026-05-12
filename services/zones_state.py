"""Zone-state mutation utility — the ONLY blessed write path for zones.state.

All callers that need to change ``zones.state`` (or any state-machine field
like ``commanded_state``, ``observed_state``, ``fault_count``) MUST use
:func:`update_zone_state` — never call ``db.update_zone({'state': ...})``
directly.

Why a separate module?
  - ``services/zone_control.py`` historically owned ``_versioned_update``,
    but it imports ``services.sse_hub`` and ``services.observed_state``
    transitively.  Those modules in turn want to publish audited state
    transitions via this helper, which would create a circular import if we
    kept it under ``zone_control``.  Putting the helper in its own,
    dependency-light module breaks the cycle while letting both
    ``zone_control`` and the other services share one canonical implementation.

What it does:
  1. Calls :py:meth:`db.update_zone_versioned` (atomic optimistic-lock UPDATE
     that **also** returns the row snapshot taken inside the same
     ``BEGIN IMMEDIATE`` transaction — see ``db/zones.py``).  This eliminates
     the TOCTOU race that the old ``services/zone_control._versioned_update``
     had between an external pre-read of ``prev_state`` and the actual
     UPDATE.
  2. If the versioned write didn't apply (zone gone, lock contention), falls
     back to a plain ``db.update_zone()`` so the caller's intent isn't lost
     — same defensive fallback as the legacy implementation.
  3. When the update changes ``state``, emits a ``zone_state_change`` audit
     row carrying ``from``, ``to``, ``reason``, and ``commanded_state``.
     This is **always-on** audit (not gated by ``settings.logging.debug``)
     because zone-state transitions are the principal-critical signal in the
     irrigation system — without them post-incident triage is impossible.

This call is best-effort: an audit failure must never break the hot path.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

# NB: import the `database` module rather than re-binding ``db`` at module
# load.  Tests routinely patch ``services.zone_control.db`` (which used to
# host this code) and rely on ``database.db`` being resolved lazily so the
# patched test_db is honoured by every helper.  See tests/unit/test_zone_control.py.
import database as _database_mod

logger = logging.getLogger(__name__)


def update_zone_state(
    zone_id: int,
    updates: dict[str, Any],
    *,
    audit_reason: str = "",
    db: Any | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Apply versioned zone update and emit ``zone_state_change`` audit row.

    Args:
        zone_id: target zone ID.
        updates: fields to write — typically ``{'state': 'on', ...}`` but any
            zones-table column allowed by ``ZoneRepository.update_zone`` is
            supported.
        audit_reason: a short snake_case identifier describing **why** the
            transition is happening (e.g. ``manual_start``, ``peer_off``,
            ``mqtt_observed_change``, ``auto_stop``, ``emergency``,
            ``fault_detected``, ``runner_start``).  Recorded in the audit
            payload so post-incident triage can see WHO/WHAT triggered each
            transition without grep-spelunking the application log.

    Returns:
        ``(ok, prev_zone)`` where ``ok`` is True iff the versioned UPDATE
        applied (i.e. exactly one row was written under the optimistic-lock
        version match), and ``prev_zone`` is the dict snapshot of the row
        **before** the UPDATE (or ``None`` if the row didn't exist).  Even
        when ``ok=False`` we still attempt a fallback ``db.update_zone()``
        — callers that care about the difference can inspect ``ok``.

    Best-effort guarantees:
        * never raises — any DB or audit failure is logged + swallowed.
        * audit emit only happens when ``state`` actually changes (case-
          insensitive comparison against ``prev_zone['state']``); a no-op
          state write (e.g. ``state='on'`` while already ``'on'``) does NOT
          generate spurious audit rows.
    """
    # Resolve the db instance dynamically:
    #   1. Explicit ``db=`` kwarg (preferred for callers that already hold an
    #      IrrigationDB / test_db reference — e.g. StateVerifier.self.db).
    #   2. ``services.zone_control.db`` — patched by zone_control unit tests.
    #   3. ``database.db`` — production fallback.
    if db is None:
        try:
            from services import zone_control as _zc_mod  # late import: avoid circular

            db = getattr(_zc_mod, "db", None)
        except ImportError:
            db = None
    if db is None:
        db = getattr(_database_mod, "db", None)
    if db is None:
        logger.error("update_zone_state: no db available — cannot write zone=%s", zone_id)
        return False, None

    prev_zone: dict[str, Any] | None = None
    ok = False
    try:
        ok, prev_zone = db.update_zone_versioned(zone_id, updates)
    except (sqlite3.Error, OSError):
        # Promote to logger.exception — silent debug here previously masked
        # real DB failures (broken upgrade, locked WAL) during the MASTER-C2
        # audit.  Keep the ok=False/prev_zone=None defaults so the fallback
        # branch runs.
        logger.exception(
            "update_zone_state: versioned update failed (zone=%s)",
            zone_id,
        )
        ok = False
        prev_zone = None
    except (TypeError, ValueError):
        # Possible if a stub/mock returns a single bool instead of (ok, prev).
        logger.exception(
            "update_zone_state: versioned update returned unexpected shape (zone=%s)",
            zone_id,
        )
        ok = False
        prev_zone = None

    if not ok:
        # Versioned UPDATE didn't apply (lost optimistic-lock race, row
        # missing, or DB error above).  Fall back to a plain update_zone so
        # the caller's write intent is preserved — same defensive behaviour
        # the old ``_versioned_update`` had.
        try:
            db.update_zone(zone_id, updates)
        except (sqlite3.Error, OSError):
            logger.exception(
                "update_zone_state: fallback update_zone failed (zone=%s)",
                zone_id,
            )

    # Emit zone_state_change ONLY when state is actually changing.  Always-on
    # audit (not gated by debug flag) — zone state transitions are the most
    # principal-critical signal in the irrigation system.
    new_state = updates.get("state")
    if new_state is not None:
        prev_state = (prev_zone or {}).get("state")
        if str(new_state).lower() != str(prev_state or "").lower():
            try:
                # Local import to avoid pulling Flask/sqlite into modules that
                # only want this helper for read-only state writes.
                from services.audit import record_audit

                record_audit(
                    action_type="zone_state_change",
                    source="zones_state",
                    target=f"zone:{int(zone_id)}",
                    payload={
                        "from": prev_state,
                        "to": new_state,
                        "reason": audit_reason or "unknown",
                        "commanded_state": updates.get("commanded_state"),
                    },
                    actor="system",
                )
            except Exception:
                logger.exception(
                    "update_zone_state: record_audit zone_state_change failed (zone=%s)",
                    zone_id,
                )

    return ok, prev_zone
