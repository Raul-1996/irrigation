"""Float-sensor repository (PHYS-3 / MASTER-H3 / audit N16).

The float-sensor path is safety-critical: it protects the pump from
dry-run by pausing zones when the tank float switch reports empty.
Before PHYS-3 this path opened raw `sqlite3.connect()` connections,
bypassing `BaseRepository._connect()`. That meant:

  * `PRAGMA foreign_keys=ON` was NOT applied -> orphaned rows on cascade
  * `PRAGMA journal_mode=WAL` was set ad-hoc, not through the central
    contract, so future PRAGMA additions (synchronous=NORMAL, temp_store,
    ...) would silently skip this safety-critical path.
  * `busy_timeout` was 30s via direct PRAGMA but only on THIS connection,
    not inherited from the central policy.

Wave 3 consolidation: `PRAGMA busy_timeout=30000` has moved up to
`BaseRepository._connect()`, so FloatRepository no longer needs to
override `_connect()` — every repo now gets the same 30s busy timeout.

See audit-report.md PHYS-3 for the full rationale.
"""

import logging
import sqlite3
from typing import Any

from db.base import BaseRepository, retry_on_busy

logger = logging.getLogger(__name__)


class FloatRepository(BaseRepository):
    """DB operations for the float-sensor monitor.

    All writes go through BaseRepository._connect() which guarantees:
        PRAGMA journal_mode=WAL
        PRAGMA foreign_keys=ON
        PRAGMA busy_timeout=30000
        connection timeout=5s
    Float writes are safety-critical (pump dry-run protection) and the
    30s busy_timeout ensures they wait for WAL checkpoints instead of
    failing fast with SQLITE_BUSY.
    """

    # ------------------------------------------------------------------
    @retry_on_busy()
    def get_float_enabled_groups(self) -> list[dict[str, Any]]:
        """Return all groups with float_enabled=1."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, name, float_enabled, float_mqtt_topic, "
                    "float_mqtt_server_id, float_mode, float_timeout_minutes, "
                    "float_debounce_seconds "
                    "FROM groups WHERE float_enabled=1"
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as e:
            logger.error("FloatRepository.get_float_enabled_groups failed: %s", e)
            return []

    @retry_on_busy()
    def get_float_group(self, group_id: int) -> dict[str, Any] | None:
        """Return a single group's float config or None."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id, name, float_enabled, float_mqtt_topic, "
                    "float_mqtt_server_id, float_mode, float_timeout_minutes, "
                    "float_debounce_seconds "
                    "FROM groups WHERE id=?",
                    (group_id,),
                ).fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            logger.error("FloatRepository.get_float_group(%s) failed: %s", group_id, e)
            return None

    @retry_on_busy()
    def pause_active_zones(self, group_id: int) -> list[int]:
        """Mark all active zones in the group as paused='float'.

        Returns the list of zone IDs that were paused. Intended for the
        float-sensor path when the sensor reports the tank empty.
        """
        paused: list[int] = []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, duration FROM zones WHERE group_id=? AND state='on'", (group_id,)
                ).fetchall()
                for row in rows:
                    zone_id = row["id"]
                    duration = row["duration"] or 0
                    conn.execute(
                        "UPDATE zones SET state='paused', pause_reason='float', pause_remaining_seconds=? WHERE id=?",
                        (duration, zone_id),
                    )
                    paused.append(zone_id)
                conn.commit()
        except sqlite3.Error as e:
            logger.error("FloatRepository.pause_active_zones(%s) failed: %s", group_id, e)
        return paused

    @retry_on_busy()
    def log_event(self, group_id: int, event_type: str, paused_zones: list[int]) -> bool:
        """Append a row to float_events (pause / resume / emergency_stop).

        Returns True on success, False on failure. Failures are logged
        but swallowed — the MQTT handler must not crash on DB errors.
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO float_events (group_id, event_type, paused_zones) VALUES (?, ?, ?)",
                    (group_id, event_type, str(paused_zones)),
                )
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(
                "FloatRepository.log_event(group=%s, type=%s) failed: %s",
                group_id,
                event_type,
                e,
            )
            return False
