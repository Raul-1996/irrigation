"""Admin user management routes (Issue #52).

UI:
    GET  /admin/users           — list/create/edit users (admin only)
    GET  /account               — current user's account page (login_required)

API (admin only):
    GET    /api/admin/users
    POST   /api/admin/users                       — {username, password, role}
    POST   /api/admin/users/<id>/password         — {new_password}
    POST   /api/admin/users/<id>/role             — {role}
    POST   /api/admin/users/<id>/deactivate
    POST   /api/admin/users/<id>/activate

API (any logged-in user):
    POST /api/account/password                    — {old_password, new_password}
"""

import logging

from flask import Blueprint, jsonify, render_template, request, session
from werkzeug.security import check_password_hash

from constants import MIN_PASSWORD_LENGTH
from routes.auth import admin_required, login_required
from services import users_service
from services.audit import audit_log

logger = logging.getLogger(__name__)

admin_users_bp = Blueprint("admin_users_bp", __name__)

# Same blocklist as /api/password
_PASSWORD_BLOCKLIST = {"1234", "12345678", "0000", "password", "admin", "qwerty"}


# ── helpers ───────────────────────────────────────────────────────────────
def _validate_password(pw: str) -> tuple[bool, str]:
    if not pw:
        return False, "Пароль обязателен"
    if len(pw) < MIN_PASSWORD_LENGTH:
        return False, f"Минимум {MIN_PASSWORD_LENGTH} символов"
    if len(pw) > 64:
        return False, "Максимум 64 символа"
    if pw.lower() in _PASSWORD_BLOCKLIST:
        return False, "Слишком простой пароль"
    return True, ""


# ── pages ─────────────────────────────────────────────────────────────────
@admin_users_bp.route("/admin/users", methods=["GET"])
@admin_required
def admin_users_page():
    return render_template("admin_users.html")


@admin_users_bp.route("/account", methods=["GET"])
@login_required
def account_page():
    return render_template("account.html")


# ── API: admin user CRUD ──────────────────────────────────────────────────
@admin_users_bp.route("/api/admin/users", methods=["GET"])
@admin_required
def api_list_users():
    users = users_service.list_users()
    return jsonify({"success": True, "users": [u.to_dict() for u in users]})


@admin_users_bp.route("/api/admin/users", methods=["POST"])
@admin_required
@audit_log(
    "user_create",
    target_extractor=lambda *a, **kw: f"user:{(request.get_json(silent=True) or {}).get('username', '?')}",
    payload_filter=lambda p: {k: v for k, v in p.items() if k != "password"},
)
def api_create_user():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    role = (data.get("role") or "viewer").strip()
    if not username:
        return jsonify({"success": False, "message": "username обязателен"}), 400
    if role not in ("viewer", "admin"):
        return jsonify({"success": False, "message": "role должен быть viewer|admin"}), 400
    ok, msg = _validate_password(password)
    if not ok:
        return jsonify({"success": False, "message": msg}), 400
    user = users_service.create_user(username, password, role)
    if user is None:
        return jsonify({"success": False, "message": "Не удалось создать (возможно, такой username уже есть)"}), 409
    return jsonify({"success": True, "user": user.to_dict()})


@admin_users_bp.route("/api/admin/users/<int:user_id>/password", methods=["POST"])
@admin_required
@audit_log(
    "user_password_change_by_admin",
    target_extractor=lambda *a, **kw: f"user:{kw.get('user_id', '?')}",
    payload_filter=lambda p: {"changed": True},
)
def api_admin_change_password(user_id: int):
    data = request.get_json(silent=True) or {}
    new_password = (data.get("new_password") or "").strip()
    ok, msg = _validate_password(new_password)
    if not ok:
        return jsonify({"success": False, "message": msg}), 400
    if users_service.get_by_id(user_id) is None:
        return jsonify({"success": False, "message": "Пользователь не найден"}), 404
    if not users_service.change_password(user_id, new_password):
        return jsonify({"success": False, "message": "Не удалось обновить пароль"}), 500
    return jsonify({"success": True})


@admin_users_bp.route("/api/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
@audit_log(
    "user_role_change",
    target_extractor=lambda *a, **kw: f"user:{kw.get('user_id', '?')}",
)
def api_change_role(user_id: int):
    data = request.get_json(silent=True) or {}
    role = (data.get("role") or "").strip()
    if role not in ("viewer", "admin"):
        return jsonify({"success": False, "message": "role должен быть viewer|admin"}), 400
    target = users_service.get_by_id(user_id)
    if target is None:
        return jsonify({"success": False, "message": "Пользователь не найден"}), 404
    # Protect: don't allow demoting the last active admin.
    if target.role == "admin" and role == "viewer" and users_service.count_active_admins() <= 1:
        return jsonify({"success": False, "message": "Нельзя понизить последнего активного админа"}), 400
    if not users_service.change_role(user_id, role):
        return jsonify({"success": False, "message": "Не удалось сменить роль"}), 500
    return jsonify({"success": True})


@admin_users_bp.route("/api/admin/users/<int:user_id>/deactivate", methods=["POST"])
@admin_required
@audit_log(
    "user_deactivate",
    target_extractor=lambda *a, **kw: f"user:{kw.get('user_id', '?')}",
)
def api_deactivate_user(user_id: int):
    target = users_service.get_by_id(user_id)
    if target is None:
        return jsonify({"success": False, "message": "Пользователь не найден"}), 404
    # Protect last admin.
    if target.role == "admin" and target.is_active and users_service.count_active_admins() <= 1:
        return jsonify({"success": False, "message": "Нельзя деактивировать последнего админа"}), 400
    if not users_service.set_active(user_id, False):
        return jsonify({"success": False, "message": "Не удалось деактивировать"}), 500
    return jsonify({"success": True})


@admin_users_bp.route("/api/admin/users/<int:user_id>/activate", methods=["POST"])
@admin_required
@audit_log(
    "user_activate",
    target_extractor=lambda *a, **kw: f"user:{kw.get('user_id', '?')}",
)
def api_activate_user(user_id: int):
    if users_service.get_by_id(user_id) is None:
        return jsonify({"success": False, "message": "Пользователь не найден"}), 404
    if not users_service.set_active(user_id, True):
        return jsonify({"success": False, "message": "Не удалось активировать"}), 500
    return jsonify({"success": True})


# ── API: account (self-service password change) ──────────────────────────
@admin_users_bp.route("/api/account/password", methods=["POST"])
@login_required
@audit_log(
    "account_password_change",
    target_extractor=lambda *a, **kw: f"user:{session.get('user_id', '?')}",
    payload_filter=lambda p: {"changed": True},
)
def api_account_change_password():
    data = request.get_json(silent=True) or {}
    old_password = (data.get("old_password") or "").strip()
    new_password = (data.get("new_password") or "").strip()
    ok, msg = _validate_password(new_password)
    if not ok:
        return jsonify({"success": False, "message": msg}), 400
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "message": "Нет user_id в сессии"}), 401
    user = users_service.get_by_id(int(user_id))
    if user is None:
        return jsonify({"success": False, "message": "Пользователь не найден"}), 404
    if not check_password_hash(user.password_hash, old_password):
        return jsonify({"success": False, "message": "Старый пароль неверен"}), 400
    if not users_service.change_password(user.id, new_password):
        return jsonify({"success": False, "message": "Не удалось сохранить новый пароль"}), 500
    return jsonify({"success": True})
