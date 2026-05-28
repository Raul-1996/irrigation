"""Auth routes (Issue #52 — in-app login, replaces nginx/CF basic auth).

Endpoints:
    GET  /login                — login page (HTML).
    POST /api/login            — username+password login. Backwards-compatible:
                                 if only `password` is sent, we look it up
                                 against the default admin account.
    POST /api/login/escalate   — viewer → admin two-step escalation.
    POST /api/logout           — clear session (GET also accepted, preserved
                                 by routes/system_config_api.api_logout).

Decorators exposed for the rest of the app:
    @login_required   — any active logged-in user (viewer or admin).
    @admin_required   — admin role only.
"""

from functools import wraps

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from services import users_service
from services.audit import audit_log
from services.rate_limiter import login_limiter

auth_bp = Blueprint("auth_bp", __name__)

# Will be set by app.py after csrf is created
csrf = None


def _regenerate_session(new_values: dict) -> None:
    """Invalidate the current session id and seed it with new values.

    Issue #52: also marks session.permanent=True so the 365-day
    PERMANENT_SESSION_LIFETIME applies — fixes iPhone Safari losing
    Basic Auth after a few hours.
    """
    session.clear()
    session.permanent = True
    for k, v in new_values.items():
        session[k] = v


def _seed_session(user) -> None:
    """Common session bootstrap used by login + escalate."""
    _regenerate_session(
        {
            "logged_in": True,
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
        }
    )


def login_required(view_func):
    """Allow any authenticated user (viewer or admin). 401 for anon on /api/*, 302 to /login otherwise."""

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if current_app.config.get("TESTING"):
            return view_func(*args, **kwargs)
        if not session.get("logged_in") or session.get("role") not in ("viewer", "admin"):
            if (request.path or "").startswith("/api/"):
                return jsonify({"success": False, "error_code": "UNAUTHENTICATED"}), 401
            return redirect(url_for("auth_bp.login_page"))
        return view_func(*args, **kwargs)

    return wrapper


def admin_required(view_func):
    """Allow only admins."""

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if current_app.config.get("TESTING"):
            return view_func(*args, **kwargs)
        if session.get("role") != "admin":
            if (request.path or "").startswith("/api/"):
                if not session.get("logged_in"):
                    return jsonify({"success": False, "error_code": "UNAUTHENTICATED"}), 401
                return jsonify({"success": False, "error_code": "FORBIDDEN"}), 403
            return redirect(url_for("auth_bp.login_page"))
        return view_func(*args, **kwargs)

    return wrapper


@auth_bp.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html")


def _resolve_username_from_payload(data: dict) -> str:
    """Back-compat: if `username` is omitted, fall back to 'admin' (the legacy
    single-account behaviour). New clients should always send username."""
    raw = data.get("username")
    if raw is None or str(raw).strip() == "":
        return "admin"
    return str(raw).strip()


@auth_bp.route("/api/login", methods=["POST"])
@audit_log(
    "login",
    target_extractor=lambda *a, **kw: "session",
    payload_filter=lambda p: {k: v for k, v in p.items() if k != "password"},
)
def api_login():
    data = request.get_json(silent=True) or {}
    password = (data.get("password") or "").strip()
    username = _resolve_username_from_payload(data)

    # IP-based rate limiting
    ip = request.remote_addr or "0.0.0.0"
    allowed, retry_after = login_limiter.check(ip, username=username)
    if not allowed:
        return jsonify({"success": False, "message": f"Слишком много попыток. Повторите через {retry_after}с"}), 429

    user = users_service.authenticate(username, password)
    if user is not None:
        login_limiter.reset(ip, username=username)
        _seed_session(user)
        users_service.mark_login(user.id)
        return jsonify({"success": True, "role": user.role, "username": user.username})

    login_limiter.record_failure(ip, username=username)
    return jsonify({"success": False, "message": "Неверный логин или пароль"}), 401
