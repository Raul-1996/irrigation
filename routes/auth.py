"""Authentication routes (in-app, issue #52).

Endpoints:
    GET  /login                 — login page
    POST /api/login             — {username, password} -> session
    POST /api/login/escalate    — viewer re-auth as admin without session loss
    POST /api/logout            — clear session
    GET  /account               — self-service UI (password change)
    POST /api/account/password  — self-service password change
"""

import logging
import time
from typing import Any

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from services.audit import audit_log
from services.login_rate_limiter import (
    ip_login_limiter,
    send_telegram_alert,
)
from services.users_service import (
    authenticate,
    change_password,
)

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth_bp", __name__)


def _regenerate_session(new_values: dict[str, Any]) -> None:
    """Invalidate the current session and issue a fresh session id (SEC-006)."""
    session.clear()
    session.permanent = True  # honour PERMANENT_SESSION_LIFETIME (B7)
    for k, v in new_values.items():
        session[k] = v


def _client_ip() -> str:
    """Real client IP after ProxyFix (B2).

    The fallback string below is just a placeholder bucket key when
    remote_addr is somehow missing — it does NOT bind a socket. Bandit
    flags it B104; the nosec is intentional.
    """
    return request.remote_addr or "0.0.0.0"  # nosec B104


@auth_bp.route("/login", methods=["GET"])
def login_page():
    """Render the login form. NO guest-mode shortcut anymore (#52)."""
    return render_template("login.html")


@auth_bp.route("/api/login", methods=["POST"])
@audit_log(
    "login",
    target_extractor=lambda *a, **kw: "session",
    payload_filter=lambda p: {k: v for k, v in p.items() if k != "password"},
)
def api_login():
    """Authenticate user. Per-IP rate limit (B4), no per-username bucket."""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    # B12: empty username → 400. NO fallback to "admin".
    if not username:
        return jsonify({"success": False, "message": "username required"}), 400

    # B12: empty password → 400. Length is NOT validated here — Раул's seeded
    # defaults (admin/1234, Poliv/Poliv) are intentionally shorter than the
    # 8-char minimum that validate_password enforces for /api/account/password.
    if not password:
        return jsonify({"success": False, "message": "password required"}), 400

    ip = _client_ip()

    # B4: pre-check — if IP already over limit, reject without burning CPU.
    allowed, _ = ip_login_limiter.pre_check(ip)
    if not allowed:
        return jsonify({"success": False, "message": "Too many failed attempts. Retry later."}), 429

    user = authenticate(username, password)

    if user:
        ip_login_limiter.record_success(ip)
        try:
            from database import db as _db

            _db.users.touch_last_login(user["id"])
        except Exception as e:
            logger.debug("update_last_login: %s", e)
        _regenerate_session(
            {
                "logged_in": True,
                "user_id": user["id"],
                "username": user["username"],
                "role": user["role"],
            }
        )
        return jsonify({"success": True, "role": user["role"], "username": user["username"]})

    # Failure path: B4 progressive sleep + alert
    sleep_sec, fails_min, fails_hour = ip_login_limiter.record_failure(ip)
    if ip_login_limiter.should_alert(ip, fails_hour):
        try:
            send_telegram_alert(ip, fails_hour)
        except Exception as e:
            logger.warning("send_telegram_alert failed: %s", e)
    if sleep_sec > 0:
        time.sleep(sleep_sec)
    return jsonify({"success": False, "message": "invalid credentials"}), 401


@auth_bp.route("/api/login/escalate", methods=["POST"])
@audit_log(
    "login_escalate",
    target_extractor=lambda *a, **kw: "session",
    payload_filter=lambda p: {k: v for k, v in p.items() if k != "password"},
)
def api_login_escalate():
    """Viewer → admin re-auth without losing session id.

    Required for a viewer to perform an admin action without going through
    /api/logout + /api/login (which would burn correlation context).
    """
    if not session.get("logged_in"):
        return jsonify({"success": False, "message": "not logged in"}), 401

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username:
        return jsonify({"success": False, "message": "username required"}), 400
    if not password:
        return jsonify({"success": False, "message": "password required"}), 400

    ip = _client_ip()
    allowed, _ = ip_login_limiter.pre_check(ip)
    if not allowed:
        return jsonify({"success": False, "message": "Too many failed attempts. Retry later."}), 429

    user = authenticate(username, password)
    if not user:
        sleep_sec, _fm, fh = ip_login_limiter.record_failure(ip)
        if ip_login_limiter.should_alert(ip, fh):
            try:
                send_telegram_alert(ip, fh)
            except Exception as e:
                logger.warning("escalate send_telegram_alert failed: %s", e)
        if sleep_sec > 0:
            time.sleep(sleep_sec)
        return jsonify({"success": False, "message": "invalid credentials"}), 401
    if user["role"] != "admin":
        return jsonify({"success": False, "message": "target user is not admin"}), 403

    ip_login_limiter.record_success(ip)
    # Same session id preserved; just upgrade role + identity.
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = "admin"
    return jsonify({"success": True, "role": "admin"})


@auth_bp.route("/api/logout", methods=["POST"])
@audit_log("logout", target_extractor=lambda *a, **kw: "session")
def api_logout():
    """Clear the session — POST only (CSRF-protected at framework level)."""
    session.clear()
    return jsonify({"success": True})


@auth_bp.route("/logout", methods=["GET", "POST"])
def logout_redirect():
    """Backward-compat: GET /logout still clears and redirects to login.

    GET is preserved so existing sidebar links keep working.
    """
    session.clear()
    return redirect(url_for("auth_bp.login_page"))


# ── Self-service ──────────────────────────────────────────────────────────


@auth_bp.route("/account", methods=["GET"])
def account_page():
    """Self-service account page (password change)."""
    if not session.get("logged_in"):
        return redirect(url_for("auth_bp.login_page"))
    return render_template(
        "account.html",
        username=session.get("username", ""),
        role=session.get("role", ""),
    )


@auth_bp.route("/api/account/password", methods=["POST"])
@audit_log(
    "account_password_change",
    target_extractor=lambda *a, **kw: f"user:{session.get('user_id')}",
    payload_filter=lambda p: {"changed": True},
)
def api_account_password():
    """Any logged-in user changes THEIR OWN password."""
    if not session.get("logged_in"):
        return jsonify({"success": False, "message": "auth required"}), 401
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "message": "missing user_id in session"}), 400
    data = request.get_json(silent=True) or {}
    old_password = data.get("old_password") or ""
    new_password = data.get("new_password") or ""

    # Re-auth with current credentials (preserves PII boundary).
    username = session.get("username") or ""
    if not authenticate(username, old_password):
        return jsonify({"success": False, "message": "old password is incorrect"}), 400

    ok, msg = change_password(int(user_id), new_password)
    if not ok:
        return jsonify({"success": False, "message": msg}), 400
    return jsonify({"success": True})
