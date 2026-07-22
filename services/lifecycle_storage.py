"""Strict, deadline-aware SQLite operations for process lifecycle safety.

Repository read helpers intentionally return empty collections on SQLite
errors for ordinary UI resilience.  Boot and shutdown cannot use that contract:
an unreadable topology must be distinguishable from an empty installation.
These helpers therefore query the lifecycle inventory directly and propagate
all failures through a bounded worker boundary.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from utils import decrypt_secret

T = TypeVar("T")
logger = logging.getLogger(__name__)
_BOOT_INTERRUPTED_KEY = "lifecycle.boot_interrupted_zone_ids"


@dataclass(frozen=True)
class LifecycleSnapshot:
    zones: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    servers: dict[int, dict[str, Any]]


def _remaining(deadline: float) -> float:
    return max(0.0, float(deadline) - time.monotonic())


def _require_before_deadline(deadline: float) -> None:
    if _remaining(deadline) <= 0:
        raise TimeoutError("lifecycle database deadline exceeded")


def _connect(db_path: str, deadline: float) -> sqlite3.Connection:
    remaining = _remaining(deadline)
    if remaining <= 0:
        raise TimeoutError("lifecycle database deadline exceeded")
    conn = sqlite3.connect(str(db_path), timeout=max(0.001, remaining))
    conn.execute(f"PRAGMA busy_timeout = {max(1, int(remaining * 1000))}")
    return conn


def run_bounded(
    operation: Callable[[], T],
    *,
    deadline: float,
    name: str,
) -> tuple[bool, T | None, str | None]:
    """Execute one blocking lifecycle operation under the shared deadline."""
    remaining = _remaining(deadline)
    if remaining <= 0:
        return False, None, f"{name}: global deadline exceeded"

    result_queue: queue.Queue[tuple[bool, T | None, str | None]] = queue.Queue(maxsize=1)

    def worker() -> None:
        if _remaining(deadline) <= 0:
            result_queue.put((False, None, f"{name}: global deadline exceeded"))
            return
        try:
            result_queue.put((True, operation(), None))
        except Exception as exc:
            result_queue.put((False, None, f"{name}: {type(exc).__name__}: {exc}"))

    try:
        threading.Thread(target=worker, name=f"lifecycle-db-{name}", daemon=True).start()
    except RuntimeError as exc:
        return False, None, f"{name}: thread start failed: {exc}"

    try:
        ok, value, reason = result_queue.get(timeout=remaining)
    except queue.Empty:
        return False, None, f"{name}: global deadline exceeded"
    if time.monotonic() > deadline:
        return False, None, f"{name}: global deadline exceeded"
    return ok, value, reason


def strict_snapshot(db_path: str, *, deadline: float) -> LifecycleSnapshot:
    """Read zones, groups, and MQTT servers without swallow-on-error facades."""
    with _connect(db_path, deadline) as conn:
        conn.row_factory = sqlite3.Row
        zones = [dict(row) for row in conn.execute("SELECT * FROM zones ORDER BY id").fetchall()]
        groups = [dict(row) for row in conn.execute("SELECT * FROM groups ORDER BY id").fetchall()]
        raw_servers = [dict(row) for row in conn.execute("SELECT * FROM mqtt_servers ORDER BY id").fetchall()]

    servers: dict[int, dict[str, Any]] = {}
    for server in raw_servers:
        password = server.get("password")
        if isinstance(password, str) and password.startswith("ENC:"):
            server["password"] = decrypt_secret(password[4:])
        server_id = int(server["id"])
        if server_id in servers:
            raise sqlite3.IntegrityError(f"duplicate MQTT server id {server_id}")
        servers[server_id] = server
    return LifecycleSnapshot(zones=zones, groups=groups, servers=servers)


def abort_crash_open_runs(
    db_path: str,
    *,
    deadline: float,
    end_local: str,
) -> set[int]:
    """Abort pre-process open runs and return pre-reconcile active zone IDs."""
    with _connect(db_path, deadline) as conn:
        conn.execute("BEGIN IMMEDIATE")
        marker_row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (_BOOT_INTERRUPTED_KEY,),
        ).fetchone()
        durable_interrupted: set[int] = set()
        if marker_row is not None:
            try:
                raw_ids = json.loads(str(marker_row[0]))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise sqlite3.IntegrityError("invalid durable boot interruption marker") from exc
            if not isinstance(raw_ids, list) or any(type(zone_id) is not int for zone_id in raw_ids):
                raise sqlite3.IntegrityError("invalid durable boot interruption marker")
            durable_interrupted = {int(zone_id) for zone_id in raw_ids if int(zone_id) > 0}

        active_interrupted = {
            int(row[0])
            for row in conn.execute(
                "SELECT id FROM zones WHERE LOWER(COALESCE(state, '')) IN ('starting', 'on', 'stopping', 'paused')"
            ).fetchall()
        }
        conn.execute(
            "UPDATE zone_runs SET end_utc = ?, status = 'aborted', updated_at = ? WHERE end_utc IS NULL",
            (str(end_local), str(end_local)),
        )
        _require_before_deadline(deadline)
        conn.commit()
    return durable_interrupted | active_interrupted


def persist_boot_zones_off(
    db_path: str,
    zone_states: list[tuple[int, str]],
    *,
    interrupted_zone_ids: set[int],
    deadline: float,
    updated_local: str,
) -> None:
    """Atomically persist every confirmed boot OFF after the full sweep."""
    with _connect(db_path, deadline) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for zone_id, current_state in zone_states:
            snapshot_state = str(current_state or "").lower()
            row = conn.execute("SELECT state FROM zones WHERE id = ?", (int(zone_id),)).fetchone()
            if row is None:
                raise sqlite3.IntegrityError(f"zone {zone_id} disappeared during boot reconciliation")
            database_state = str(row[0] or "").lower()
            target_state = "fault" if "fault" in (snapshot_state, database_state) else "off"
            cursor = conn.execute(
                "UPDATE zones SET state = ?, commanded_state = 'off', "
                "watering_start_time = NULL, planned_end_time = NULL, "
                "version = COALESCE(version, 0) + 1, updated_at = ? WHERE id = ?",
                (target_state, str(updated_local), int(zone_id)),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError(f"zone {zone_id} disappeared during boot reconciliation")
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            (
                _BOOT_INTERRUPTED_KEY,
                json.dumps(sorted({int(zone_id) for zone_id in interrupted_zone_ids if int(zone_id) > 0})),
            ),
        )
        _require_before_deadline(deadline)
        conn.commit()


def clear_boot_interrupted_evidence(db_path: str, *, deadline: float) -> None:
    """Consume the durable handoff marker after scheduler recovery succeeds."""
    with _connect(db_path, deadline) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM settings WHERE key = ?", (_BOOT_INTERRUPTED_KEY,))
        _require_before_deadline(deadline)
        conn.commit()


def persist_confirmed_shutdown_off(
    db_path: str,
    zone_id: int,
    *,
    current_state: str,
    deadline: float,
    end_local: str,
    end_monotonic: float,
) -> int:
    """Atomically persist confirmed zone OFF and close every open zone_run."""
    snapshot_state = str(current_state or "").lower()
    with _connect(db_path, deadline) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT state FROM zones WHERE id = ?", (int(zone_id),)).fetchone()
        if row is None:
            raise sqlite3.IntegrityError(f"zone {zone_id} disappeared during shutdown")
        previous_state = str(row[0] or "")
        database_state = previous_state.lower()
        target_state = "fault" if "fault" in (snapshot_state, database_state) else "off"
        force_failed = 1 if target_state == "fault" else 0
        cursor = conn.execute(
            "UPDATE zones SET state = ?, commanded_state = 'off', "
            "watering_start_time = NULL, planned_end_time = NULL, "
            "version = COALESCE(version, 0) + 1, updated_at = ? WHERE id = ?",
            (target_state, str(end_local), int(zone_id)),
        )
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError(f"zone {zone_id} disappeared during shutdown")
        runs = conn.execute(
            "UPDATE zone_runs SET end_utc = ?, end_monotonic = ?, "
            "status = CASE WHEN ? = 1 OR COALESCE(confirmed, 0) = 0 THEN 'failed' ELSE 'ok' END, "
            "updated_at = ? WHERE zone_id = ? AND end_utc IS NULL",
            (str(end_local), float(end_monotonic), force_failed, str(end_local), int(zone_id)),
        )
        closed_runs = max(0, int(runs.rowcount))
        if target_state != database_state:
            try:
                conn.execute(
                    "INSERT INTO audit_log "
                    "(actor, source, action_type, target, payload_json, result) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "system",
                        "zones_state",
                        "zone_state_change",
                        f"zone:{int(zone_id)}",
                        json.dumps(
                            {
                                "from": previous_state,
                                "to": target_state,
                                "reason": "graceful_shutdown_confirmed",
                                "commanded_state": "off",
                            },
                            ensure_ascii=False,
                        ),
                        "success",
                    ),
                )
            except sqlite3.Error:
                logger.exception("graceful shutdown audit failed for zone %s", zone_id)
        _require_before_deadline(deadline)
        conn.commit()
    return closed_runs
