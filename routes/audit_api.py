"""Audit API blueprint — UI-event recorder + audit-log query endpoints.

Endpoints:
  POST /api/audit/ui      — record a UI click/intent event from the frontend.
                            NOT decorated with @audit_log itself (would recurse);
                            writes via record_audit() helper directly.
  GET  /api/audit         — paginated query of audit_log rows (admin only).
                            Filters: from, to, action_type, actor, q (substring).
  GET  /api/audit/types   — distinct action_types known to the system.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request, session

from database import db
from services.audit import _redact, _resolve_actor, _resolve_ip
from services.security import admin_required

logger = logging.getLogger(__name__)

audit_api_bp = Blueprint("audit_api", __name__)


# Cap UI-recorded payload sizes — defensive against runaway frontend bugs.
_MAX_UI_FIELD_LEN = 256


def _clip(value, n: int = _MAX_UI_FIELD_LEN) -> str:
    if value is None:
        return ""
    s = str(value)
    if len(s) > n:
        return s[:n] + "…"
    return s


_ANON_ALLOWED_ACTIONS = frozenset({"login_attempt"})


@audit_api_bp.route("/api/audit/ui", methods=["POST"])
def api_audit_ui_event():
    """Record a UI click/intent event sent by static/js/audit.js.

    Body schema (JSON):
        {
          "action": "zone_start_click",      # required, snake_case
          "target": "zone:5",                # optional
          "context": {...}                   # optional, free-form (redacted)
        }

    Behaviour:
      - Best-effort; even malformed bodies are stored as result='ignored'.
      - The endpoint is intentionally NOT wrapped with @audit_log because
        that would lose the original frontend `action` value.
      - Writes via record_audit() with source='ui'.
      - OQ3: anonymous (un-logged-in) callers may ONLY emit
        `login_attempt` — every other action_type returns 403.  This
        prevents an unauth scraper from filling audit_log with
        arbitrary noise (audit-spam DoS) while still letting the login
        page record its own attempts.
    """
    body = request.get_json(silent=True) or {}
    action = _clip((body.get("action") or "ui_event_unknown"), 64)
    target = _clip(body.get("target"), 128) or None
    ctx = body.get("context")
    if not isinstance(ctx, (dict, list)):
        ctx = None
    if ctx is not None:
        try:
            ctx = _redact(ctx)
        except (TypeError, ValueError) as e:
            logger.debug("audit-ui: _redact failed: %s", e)
            ctx = None

    actor = _resolve_actor(request)
    ip = _resolve_ip(request)

    # OQ3 — anonymous gate.  Authenticated sessions are identified by
    # ``logged_in=True`` (set by /api/login).  Anonymous = absence of
    # that flag.  We accept ``user`` / ``user_id`` as belt-and-braces
    # signals in case session schema evolves.
    is_authenticated = bool(session.get("logged_in") or session.get("user") or session.get("user_id"))
    if not is_authenticated and action not in _ANON_ALLOWED_ACTIONS:
        # Don't log the rejection itself via audit_log (would let an
        # attacker still fill the table at the same rate as 403s).  Just
        # a debug log with rate-limited info — _resolve_ip is rate-limit
        # friendly and intentionally avoids PII.
        logger.debug("api_audit_ui_event: rejected anon action=%s ip=%s", action, ip)
        return jsonify({"success": False, "error": "anonymous_action_not_allowed"}), 403

    try:
        db.add_audit(
            action_type=action,
            source="ui",
            target=target,
            payload=ctx,
            result="click",
            error=None,
            ip=ip,
            duration_ms=None,
            actor=actor,
        )
        return jsonify({"success": True}), 204
    except (RuntimeError, ValueError, TypeError) as exc:
        logger.warning("api_audit_ui_event: add_audit failed: %s", exc)
        # Still return 2xx — UI must never know audit ingestion failed
        return jsonify({"success": False}), 200


@audit_api_bp.route("/api/audit", methods=["GET"])
@admin_required
def api_audit_list():
    """Paginated list of audit_log rows.

    Query params:
        from        (ISO date, inclusive)
        to          (ISO date, inclusive)
        action_type (exact match)
        actor       (exact match)
        source      (exact match — api/ui/scheduler)
        q           (substring match across target/payload_json/error_msg)
        limit       (default 100, max 500)
        offset      (default 0)
    """
    try:
        limit = max(1, min(500, int(request.args.get("limit", 100))))
        offset = max(0, int(request.args.get("offset", 0)))
    except (ValueError, TypeError):
        limit, offset = 100, 0

    # Repository uses since/until field names; remap from API since/from-to.
    filters = {
        "since": request.args.get("from") or None,
        "until": request.args.get("to") or None,
        "action_type": request.args.get("action_type") or None,
        "actor": request.args.get("actor") or None,
        "source": request.args.get("source") or None,
    }
    # 'q' is a free-text substring filter applied client-side over the page.
    q_substr = (request.args.get("q") or "").strip().lower() or None

    try:
        rows = db.get_audit_logs(limit=limit, offset=offset, **filters)
        if q_substr:

            def _match(r):
                hay = " ".join(
                    str(r.get(k, ""))
                    for k in ("action_type", "target", "payload_json", "error_msg", "actor", "ip", "source")
                )
                return q_substr in hay.lower()

            rows = [r for r in rows if _match(r)]
        total = db.count_audit_logs(**filters)
        return jsonify(
            {
                "success": True,
                "rows": rows,
                "total": total,
                "limit": limit,
                "offset": offset,
                "filters": {**filters, "q": q_substr},
            }
        )
    except (RuntimeError, ValueError, TypeError) as exc:
        logger.exception("api_audit_list failed")
        return jsonify({"success": False, "message": str(exc)}), 500


@audit_api_bp.route("/api/audit/types", methods=["GET"])
@admin_required
def api_audit_action_types():
    """Distinct action_types (helps populate the UI filter dropdown)."""
    try:
        types = db.get_distinct_audit_action_types()
        return jsonify({"success": True, "types": types})
    except (RuntimeError, ValueError, TypeError) as exc:
        logger.warning("api_audit_action_types failed: %s", exc)
        return jsonify({"success": True, "types": []})
