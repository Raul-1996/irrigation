"""Audit log repository.

Two-tier logging: this is the principal-critical "who did what, when, how" trail.
Separate from the existing ``logs`` table — additive, not replacing.
"""
import sqlite3
import json
import logging
from typing import List, Dict, Any, Optional

from db.base import BaseRepository, retry_on_busy

logger = logging.getLogger(__name__)


class AuditRepository(BaseRepository):
    """Repository for the audit_log table (mutation actions only)."""

    @retry_on_busy()
    def add_audit(
        self,
        action_type: str,
        source: str = 'api',
        target: Optional[str] = None,
        payload: Any = None,
        result: str = 'success',
        error: Optional[str] = None,
        ip: Optional[str] = None,
        duration_ms: Optional[int] = None,
        actor: Optional[str] = None,
    ) -> Optional[int]:
        """Insert an audit-log row.  Best-effort: any DB error is logged and swallowed."""
        try:
            payload_json = None
            if payload is not None:
                try:
                    if isinstance(payload, (dict, list)):
                        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
                    elif isinstance(payload, str):
                        # If it's already a JSON string, keep as-is; else wrap
                        try:
                            json.loads(payload)
                            payload_json = payload
                        except (ValueError, TypeError):
                            payload_json = json.dumps({'raw': payload}, ensure_ascii=False)
                    else:
                        payload_json = json.dumps({'value': str(payload)}, ensure_ascii=False)
                except (TypeError, ValueError) as e:
                    logger.debug("audit payload serialize failed: %s", e)
                    payload_json = None

            with self._connect() as conn:
                cur = conn.execute(
                    '''INSERT INTO audit_log
                       (actor, source, action_type, target, payload_json,
                        result, error_msg, ip, duration_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (actor, source, action_type, target, payload_json,
                     result, error, ip, duration_ms)
                )
                row_id = cur.lastrowid
                conn.commit()
                return row_id
        except sqlite3.Error as e:
            logger.error("audit_log INSERT failed (action=%s target=%s): %s",
                         action_type, target, e)
            return None

    def get_audit_logs(
        self,
        since: Optional[str] = None,
        until: Optional[str] = None,
        action_type: Optional[str] = None,
        target: Optional[str] = None,
        actor: Optional[str] = None,
        source: Optional[str] = None,
        result: Optional[str] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Fetch audit-log rows with filters.  Defaults to newest 500."""
        try:
            try:
                limit = max(1, min(int(limit), 5000))
            except (TypeError, ValueError):
                limit = 500
            try:
                offset = max(0, int(offset))
            except (TypeError, ValueError):
                offset = 0

            query = (
                "SELECT id, "
                "strftime('%Y-%m-%d %H:%M:%S', ts, 'localtime') AS ts, "
                "actor, source, action_type, target, payload_json, "
                "result, error_msg, ip, duration_ms "
                "FROM audit_log WHERE 1=1"
            )
            params: List[Any] = []
            if since:
                query += ' AND ts >= ?'
                params.append(since)
            if until:
                query += ' AND ts <= ?'
                params.append(f"{until} 23:59:59" if len(str(until)) <= 10 else until)
            if action_type:
                query += ' AND action_type = ?'
                params.append(action_type)
            if target:
                query += ' AND target = ?'
                params.append(target)
            if actor:
                query += ' AND actor = ?'
                params.append(actor)
            if source:
                query += ' AND source = ?'
                params.append(source)
            if result:
                query += ' AND result = ?'
                params.append(result)
            query += ' ORDER BY id DESC LIMIT ? OFFSET ?'
            params.extend([limit, offset])

            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(query, params)
                return [dict(row) for row in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("audit_log SELECT failed: %s", e)
            return []

    def count_audit_logs(
        self,
        since: Optional[str] = None,
        until: Optional[str] = None,
        action_type: Optional[str] = None,
        target: Optional[str] = None,
        actor: Optional[str] = None,
        source: Optional[str] = None,
        result: Optional[str] = None,
    ) -> int:
        """Total rows matching filters (for pagination)."""
        try:
            query = "SELECT COUNT(*) FROM audit_log WHERE 1=1"
            params: List[Any] = []
            if since:
                query += ' AND ts >= ?'
                params.append(since)
            if until:
                query += ' AND ts <= ?'
                params.append(f"{until} 23:59:59" if len(str(until)) <= 10 else until)
            if action_type:
                query += ' AND action_type = ?'
                params.append(action_type)
            if target:
                query += ' AND target = ?'
                params.append(target)
            if actor:
                query += ' AND actor = ?'
                params.append(actor)
            if source:
                query += ' AND source = ?'
                params.append(source)
            if result:
                query += ' AND result = ?'
                params.append(result)
            with self._connect() as conn:
                cur = conn.execute(query, params)
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error as e:
            logger.error("audit_log COUNT failed: %s", e)
            return 0

    @retry_on_busy()
    def cleanup_audit_logs(self, older_than_days: int = 7) -> int:
        """Delete audit rows older than ``older_than_days``.  Returns rows deleted."""
        try:
            days = max(1, int(older_than_days))
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM audit_log WHERE ts < datetime('now', ?)",
                    (f'-{days} days',)
                )
                deleted = cur.rowcount or 0
                conn.commit()
                if deleted:
                    logger.info("audit_log cleanup: %d rows older than %d days deleted",
                                deleted, days)
                return int(deleted)
        except sqlite3.Error as e:
            logger.error("audit_log cleanup failed: %s", e)
            return 0

    def get_distinct_action_types(self) -> List[str]:
        """Return sorted list of distinct action_type values (for UI filters)."""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT DISTINCT action_type FROM audit_log "
                    "ORDER BY action_type ASC LIMIT 500"
                )
                return [str(r[0]) for r in cur.fetchall() if r[0]]
        except sqlite3.Error as e:
            logger.error("audit_log distinct action_types failed: %s", e)
            return []
