"""Admin users CRUD (issue #52, B9).

All endpoints require admin role (the global `_auth_before_request` gate
denies viewers POST/PUT/DELETE outside the viewer-allowlist).

Self-demotion / self-deactivate guard: cannot modify own role / is_active.
"""

import logging

from flask import Blueprint, jsonify, render_template, request, session

from services.audit import audit_log
from services.users_service import (
    create_user,
    delete_user,
    list_users,
    set_active,
    set_role,
)

logger = logging.getLogger(__name__)

admin_users_bp = Blueprint("admin_users_bp", __name__)


def _require_admin():
    """Return a 403 response if session role != admin, else None."""
    role = session.get("role")
    if role != "admin":
        return jsonify({"success": False, "message": "admin role required"}), 403
    return None


@admin_users_bp.route("/admin/users", methods=["GET"])
def page_admin_users():
    """Admin UI — XSS-safe rendering (B9)."""
    if session.get("role") != "admin":
        return jsonify({"success": False, "message": "admin role required"}), 403
    return render_template("admin_users.html")


@admin_users_bp.route("/api/admin/users", methods=["GET"])
def api_list_users():
    guard = _require_admin()
    if guard is not None:
        return guard
    return jsonify({"success": True, "users": list_users()})


@admin_users_bp.route("/api/admin/users", methods=["POST"])
@audit_log(
    "admin_user_create",
    target_extractor=lambda *a, **kw: f"user:{(request.get_json(silent=True) or {}).get('username')}",
)
def api_create_user():
    guard = _require_admin()
    if guard is not None:
        return guard
    data = request.get_json(silent=True) or {}
    username = data.get("username") or ""
    password = data.get("password") or ""
    role = data.get("role") or "viewer"
    ok, msg, uid = create_user(username, password, role)
    if not ok:
        return jsonify({"success": False, "message": msg}), 400
    return jsonify({"success": True, "user_id": uid})


@admin_users_bp.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@audit_log(
    "admin_user_delete",
    target_extractor=lambda *a, **kw: f"user:{kw.get('user_id')}",
)
def api_delete_user(user_id: int):
    guard = _require_admin()
    if guard is not None:
        return guard
    # Self-deletion guard
    if int(session.get("user_id") or 0) == int(user_id):
        return jsonify({"success": False, "message": "cannot delete own account"}), 400
    if not delete_user(user_id):
        return jsonify({"success": False, "message": "delete failed"}), 500
    return jsonify({"success": True})


@admin_users_bp.route("/api/admin/users/<int:user_id>/role", methods=["POST"])
@audit_log(
    "admin_user_role_change",
    target_extractor=lambda *a, **kw: f"user:{kw.get('user_id')}",
)
def api_set_role(user_id: int):
    guard = _require_admin()
    if guard is not None:
        return guard
    # Self-demotion guard
    if int(session.get("user_id") or 0) == int(user_id):
        return jsonify({"success": False, "message": "cannot modify own role/active"}), 400
    data = request.get_json(silent=True) or {}
    role = data.get("role") or ""
    ok, msg = set_role(user_id, role)
    if not ok:
        return jsonify({"success": False, "message": msg}), 400
    return jsonify({"success": True})


@admin_users_bp.route("/api/admin/users/<int:user_id>/active", methods=["POST"])
@audit_log(
    "admin_user_active_change",
    target_extractor=lambda *a, **kw: f"user:{kw.get('user_id')}",
)
def api_set_active(user_id: int):
    guard = _require_admin()
    if guard is not None:
        return guard
    # Self-deactivate guard
    if int(session.get("user_id") or 0) == int(user_id):
        return jsonify({"success": False, "message": "cannot modify own role/active"}), 400
    data = request.get_json(silent=True) or {}
    is_active = bool(data.get("is_active"))
    if not set_active(user_id, is_active):
        return jsonify({"success": False, "message": "db error"}), 500
    return jsonify({"success": True})
