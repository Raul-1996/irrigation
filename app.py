"""WB Irrigation — main Flask application (core only).

All API routes live in routes/*_api.py blueprints.
This file handles: app creation, config, logging, middleware, blueprint registration, boot-init.
"""

# ── Logging MUST be configured FIRST (MASTER-C2) ───────────────────────────
# setup_logging() attaches the rotating file handler to the ROOT logger and
# force-resets basicConfig so that subsequent `logging.getLogger(__name__)`
# calls in any imported module route records into backups/app.log via
# propagation. Keep this block above every other `from services...`/
# `from irrigation_scheduler...` import.
#
# Under pytest (TESTING=1) we SKIP setup_logging here: adding the root
# PIIMaskingFilter + file handler at import time breaks pytest's own
# LogCaptureHandler and the numerous suites that assume a pristine root
# logger. Tests that exercise setup_logging do so explicitly via
# tests/unit/test_logging_setup.py.
import logging
import os as _os_early
import time as _time_early

from dotenv import load_dotenv as _load_dotenv_early


def _load_runtime_environment(dotenv_path: str | None = None) -> str | None:
    """Load deployment env before logging and scheduler imports.

    ``config.py`` also calls ``load_dotenv()``, but importing it after the
    scheduler is too late for import-time timezone/logging setup.  WB_TZ is
    the scheduler-specific override; when it is the only configured timezone
    we also use it as the process timezone so logs and local timestamps agree.
    The explicit WB_TZ value is returned because legacy logging setup may
    otherwise replace it with the generic TZ value.
    """
    if dotenv_path is None:
        _load_dotenv_early()
    else:
        _load_dotenv_early(dotenv_path=dotenv_path)

    scheduler_tz = (_os_early.environ.get("WB_TZ") or "").strip() or None
    process_tz = (_os_early.environ.get("TZ") or "").strip() or None
    if scheduler_tz and not process_tz:
        process_tz = scheduler_tz
        _os_early.environ["TZ"] = scheduler_tz
    if process_tz and hasattr(_time_early, "tzset"):
        try:
            _time_early.tzset()
        except (OSError, ValueError):
            pass
    return scheduler_tz


_EXPLICIT_WB_TZ = _load_runtime_environment()

if _os_early.environ.get("TESTING") != "1" and "PYTEST_CURRENT_TEST" not in _os_early.environ:
    from services.logging_setup import setup_logging as _setup_logging_early

    _setup_logging_early(logging.getLogger("app"))
    if _EXPLICIT_WB_TZ:
        # setup_logging historically synchronises WB_TZ from TZ.  Restore the
        # documented scheduler-specific override before irrigation_scheduler
        # is imported below.
        _os_early.environ["WB_TZ"] = _EXPLICIT_WB_TZ
logger = logging.getLogger(__name__)

import json
import os
import sqlite3
from datetime import datetime

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from database import db
from irrigation_scheduler import get_scheduler, init_scheduler
from services.mqtt_pub import publish_mqtt_value as _publish_mqtt_value
from utils import normalize_topic

try:
    import paho.mqtt.client as mqtt
except ImportError:
    logging.getLogger(__name__).debug("paho.mqtt not available")
    mqtt = None
try:
    from services import events as _events
except ImportError:
    logging.getLogger(__name__).debug("services.events not available")
    _events = None
import threading
import time as _perf_time

from config import Config
from routes.auth import auth_bp
from routes.files import files_bp
from routes.groups import groups_bp
from routes.programs import programs_bp
from routes.settings import settings_bp

# Page-rendering blueprints
from routes.status import status_bp
from routes.zones import zones_bp

try:
    from routes.reports import reports_bp
except ImportError as e:
    logging.getLogger(__name__).debug("reports blueprint not loaded: %s", e)
    reports_bp = None

# API blueprints
from routes.audit_api import audit_api_bp
from routes.groups_api import groups_api_bp
from routes.health_api import (
    WB_HTTP_IN_FLIGHT as _WB_HTTP_IN_FLIGHT,
)
from routes.health_api import (
    health_api_bp,
)
from routes.health_api import (
    record_request_metrics as _record_request_metrics,
)
from routes.mqtt_api import mqtt_api_bp
from routes.programs_api import programs_api_bp
from routes.system_config_api import system_config_api_bp
from routes.system_emergency_api import system_emergency_api_bp
from routes.system_status_api import system_status_api_bp
from routes.weather_api import weather_api_bp
from routes.zones_crud_api import zones_crud_api_bp
from routes.zones_history_api import zones_history_api_bp
from routes.zones_photo_api import zones_photo_api_bp
from routes.zones_watering_api import zones_watering_api_bp

try:
    from services.telegram_bot import subscribe_to_events as _tg_subscribe

    _tg_subscribe()
    from services.telegram_bot import start_long_polling_if_needed as _tg_poll_start

    _tg_poll_start()
except ImportError as e:
    logging.getLogger(__name__).debug("Telegram bot init skipped: %s", e)
from services import sse_hub as _sse_hub
from services.api_rate_limiter import _is_allowed as _rate_check
from services.app_init import initialize_app as _initialize_app
from services.security import HttpTransportConfig as _HttpTransportConfig
from services.security import HttpTransportConfigurationError as _HttpTransportConfigurationError
from services.security import parse_strict_bool as _parse_strict_bool
from services.security import resolve_http_transport as _resolve_http_transport

# ── Logging already configured at the top of the file (MASTER-C2) ──────────

# ── Flask app ──────────────────────────────────────────────────────────────
app = Flask(__name__)
# Use TestConfig when TESTING=1 to disable CSRF
from config import TESTING as _TESTING_FLAG

if _TESTING_FLAG:
    from config import TestConfig

    app.config.from_object(TestConfig)
else:
    app.config.from_object(Config)
app.config["MAX_CONTENT_LENGTH"] = (
    22 * 1024 * 1024
)  # 22MB (issue #11: route-level MAX_FILE_SIZE enforces 20MB; +2MB for multipart envelope)


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return int(default)


# Hypercorn's WSGI bridge runs each request in the asyncio default executor.
# Long-lived SSE generators therefore need a cap strictly below that pool so
# readiness and emergency/control requests always retain worker capacity.
_default_http_workers = max(8, min(32, (os.cpu_count() or 1) + 4))
_http_workers = max(2, _positive_int_env("WB_HTTP_EXECUTOR_WORKERS", _default_http_workers))
_control_reserve = min(
    _http_workers - 1,
    _positive_int_env("WB_HTTP_CONTROL_WORKER_RESERVE", 2),
)
_sse_http_cap = min(
    _http_workers - _control_reserve,
    _positive_int_env("WB_SSE_HTTP_MAX_CLIENTS", 4),
)
app.config["HTTP_EXECUTOR_WORKERS"] = _http_workers
app.config["HTTP_CONTROL_WORKER_RESERVE"] = _control_reserve
app.config["SSE_HTTP_MAX_CLIENTS"] = _sse_http_cap
app.db = db
csrf = CSRFProtect(app)

# Exempt login endpoint from CSRF — login page doesn't include CSRF token
from routes.auth import api_login as _api_login_view

csrf.exempt(_api_login_view)

# ── CSRF policy (SEC-003 fix) ──────────────────────────────────────────────
# Previously every API blueprint was `csrf.exempt(bp)`, leaving all mutating
# endpoints open to CSRF from any site the admin visits.  Now we exempt
# ONLY read-only POSTs and fail-safe OFF endpoints that must remain callable
# without an authenticated session.  Native production deploy has no
# authenticating reverse proxy.  Every ON/resume action and admin-only CRUD
# (photo upload/delete/rotate, zone/program/group create/update/delete,
# weather settings, MQTT config, etc.) now requires the X-CSRFToken header
# that `static/js/app.js` already attaches on all non-GET fetch calls.
#
# CSRF-exempt read-only/fail-safe endpoints. Authentication remains a
# separate policy: the next-watering projection requires an explicit viewer.
from routes.audit_api import api_audit_ui_event
from routes.groups_api import api_stop_group
from routes.system_config_api import api_postpone
from routes.system_emergency_api import api_emergency_stop
from routes.system_status_api import api_status
from routes.zones_crud_api import api_zones_next_watering_bulk
from routes.zones_watering_api import api_zone_mqtt_stop, stop_zone

for _view in (
    # /api/postpone remains view-exempt so guest "postpone" can be a fail-safe
    # action; `_auth_before_request` manually requires admin + CSRF for
    # action="cancel", which re-enables future watering.
    api_postpone,
    api_emergency_stop,  # /api/emergency-stop
    api_status,  # /api/status
    stop_zone,  # /api/zones/<id>/stop
    api_zone_mqtt_stop,  # /api/zones/<id>/mqtt/stop
    api_zones_next_watering_bulk,  # /api/zones/next-watering-bulk
    api_stop_group,  # /api/groups/<id>/stop
    api_audit_ui_event,  # /api/audit/ui — guests/viewers must record clicks
):
    csrf.exempt(_view)

# NOTE: the following blueprints are NOT globally csrf-exempt any more —
# every non-GET request to them must carry X-CSRFToken (attached by app.js)
# or return 400 CSRF:
#   zones_crud_api_bp   (admin zone CRUD + bulk import)
#   zones_photo_api_bp  (photo upload/delete/rotate)
#   programs_api_bp     (program CRUD)
#   weather_api_bp      (weather settings / location)
#   mqtt_api_bp         (MQTT server config)
#   system_config_api_bp except /api/postpone above
#   system_status_api_bp except /api/status
#   groups_api_bp       (admin group CRUD/ON/master; group OFF exempt above)

_sse_hub.init(
    db=db,
    mqtt_module=mqtt,
    app_config=app.config,
    publish_mqtt_value=_publish_mqtt_value,
    normalize_topic=normalize_topic,
    get_scheduler=get_scheduler,
)

try:
    # Issue #50: Flask initialises SEND_FILE_MAX_AGE_DEFAULT=None at app
    # construction, so `setdefault` is a no-op (key already exists, value is
    # None). Use direct assignment so Werkzeug emits
    # `Cache-Control: public, max-age=604800` on /static/* instead of the
    # `no-cache` default that defeats browser caching.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 60 * 60 * 24 * 7
except (TypeError, ValueError) as e:
    logger.debug("SEND_FILE_MAX_AGE_DEFAULT config: %s", e)

# Issue #50: register image/webp MIME so Werkzeug serves *.webp with
# Content-Type: image/webp. Python 3.11 stdlib already maps it, but the
# WB-target Debian 11 base image and some minimal containers ship an older
# /etc/mime.types — defensive registration keeps behaviour consistent.
import mimetypes as _mimetypes

_mimetypes.add_type("image/webp", ".webp")

# ── App version ────────────────────────────────────────────────────────────
# Resolution: git describe → VERSION file → 'unknown'. See services/version.py.
from services.version import get_app_version as _get_app_version

APP_VERSION = _get_app_version()


@app.context_processor
def _inject_app_version():
    try:
        sys_name = db.get_setting_value("system_name") or ""

        def asset(path):
            return f"{path}?v={APP_VERSION}"

        return {"app_version": APP_VERSION, "system_name": sys_name, "asset": asset}
    except (sqlite3.Error, OSError) as e:
        logger.debug("context processor fallback: %s", e)
        return {"app_version": "1.0", "system_name": "", "asset": (lambda p: p)}


# ── Middleware ──────────────────────────────────────────────────────────────
# Wave 2 F3 — correlation ID middleware.  `_assign_correlation_id` MUST run
# BEFORE auth/rate-limit hooks so every log line emitted during auth carries
# the ID.  It comes right after `_perf_start_timer` (which only stamps
# `request._started_at`).
from services.correlation import (
    correlation_id_var as _correlation_id_var,
)
from services.correlation import (
    extract_or_generate as _extract_or_generate_cid,
)
from services.correlation import (
    reset_correlation_id as _reset_correlation_id,
)


@app.before_request
def _perf_start_timer():
    try:
        request._started_at = _perf_time.time()
    except AttributeError as e:
        logger.debug("perf timer start: %s", e)
    # F2 — in-flight gauge increment
    try:
        _WB_HTTP_IN_FLIGHT.inc()
        request._wb_inflight_counted = True
    except Exception:
        pass


@app.before_request
def _assign_correlation_id():
    """Extract X-Request-ID / X-Correlation-ID or generate UUIDv4.

    Bind result to the ContextVar so WBJsonFormatter (F1) sees it on every
    log record emitted during this request.  The teardown_request hook
    below resets the token.
    """
    try:
        cid = _extract_or_generate_cid(request.headers)
        token = _correlation_id_var.set(cid)
        request._correlation_id = cid
        request._correlation_id_token = token
    except (AttributeError, KeyError, TypeError) as e:
        logger.debug("correlation_id assign: %s", e)


@app.after_request
def _perf_add_server_timing(resp: Response):
    try:
        t0 = getattr(request, "_started_at", None)
        if t0 is not None:
            resp.headers["Server-Timing"] = f"app;dur={int((_perf_time.time() - t0) * 1000)}"
    except (TypeError, ValueError, AttributeError) as e:
        logger.debug("perf timer end: %s", e)
    # F2 — record request-level Prometheus metrics
    try:
        t0 = getattr(request, "_started_at", None)
        dur = (_perf_time.time() - t0) if t0 is not None else 0.0
        _record_request_metrics(
            method=request.method,
            endpoint=request.endpoint or "unknown",
            status_code=resp.status_code,
            duration_s=dur,
        )
    except Exception as e:
        logger.debug("record request metrics: %s", e)
    # F2 — in-flight gauge decrement (only if we incremented it in before_request)
    try:
        if getattr(request, "_wb_inflight_counted", False):
            _WB_HTTP_IN_FLIGHT.dec()
            request._wb_inflight_counted = False
    except Exception:
        pass
    return resp


@app.after_request
def _propagate_correlation_id(resp: Response):
    """Echo the correlation ID back in `X-Request-ID` response header.

    Single canonical outbound name even when caller used `X-Correlation-ID`
    on the way in — clients that want to trace a request through the logs
    grab the header from the response verbatim.
    """
    try:
        cid = getattr(request, "_correlation_id", None)
        if cid:
            resp.headers["X-Request-ID"] = cid
    except (AttributeError, TypeError) as e:
        logger.debug("correlation_id echo: %s", e)
    return resp


@app.teardown_request
def _reset_correlation_id_on_teardown(exc):
    """Reset the ContextVar so no leakage across requests on the same thread.

    Runs even on exception paths (Flask guarantees teardown hooks fire).
    """
    token = getattr(request, "_correlation_id_token", None)
    if token is not None:
        # Streaming responses can cause Flask/Werkzeug to run teardown more
        # than once for the same request context.  Clear the request-owned
        # reference before reset so the same Token is never reused.
        request._correlation_id_token = None
        _reset_correlation_id(token)


@app.after_request
def add_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'"
    )
    if app.config.get("HTTP_TLS_ENABLED") or request.is_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


@app.after_request
def _strip_conditional_revalidation(resp):
    """Workaround for hypercorn AsyncioWSGIMiddleware 304/HEAD bug.

    Hypercorn's WSGI->ASGI bridge (0.14-0.18) raises
    UnexpectedMessageError ("http.response.body given the state
    ASGIHTTPState.REQUEST") whenever Werkzeug emits a no-body response
    (304 Not Modified, or HEAD). Stripping ETag/Last-Modified prevents
    browsers from issuing If-None-Match/If-Modified-Since, so 304 is
    never returned. Cache-busting is already handled via ?v=<sha> in
    asset URLs.
    """
    resp.headers.pop("ETag", None)
    resp.headers.pop("Last-Modified", None)
    return resp


def _configure_session_cookie_secure(flask_app: Flask) -> _HttpTransportConfig:
    """Resolve cookie security from the validated listener transport.

    TLS always implies a Secure cookie and cannot be weakened by an env typo
    or explicit false. A loopback/reverse-proxy deployment may explicitly set
    ``SESSION_COOKIE_SECURE=1`` when TLS terminates in front of Flask.
    """
    profile = _resolve_http_transport()
    raw = os.environ.get("SESSION_COOKIE_SECURE")
    explicit_secure = _parse_strict_bool(raw, name="SESSION_COOKIE_SECURE") if raw is not None else None
    if profile.tls_enabled and explicit_secure is False:
        raise _HttpTransportConfigurationError("SESSION_COOKIE_SECURE cannot be disabled for WB HTTP TLS")
    flask_app.config["HTTP_BIND_HOST"] = profile.bind_host
    flask_app.config["HTTP_TLS_ENABLED"] = profile.tls_enabled
    flask_app.config["HTTP_INSECURE_EXTERNAL_ACKNOWLEDGED"] = profile.insecure_external_acknowledged
    flask_app.config["SESSION_COOKIE_SECURE"] = profile.tls_enabled or explicit_secure is True
    return profile


def _configure_trusted_proxy(
    flask_app: Flask,
    profile: _HttpTransportConfig | None = None,
) -> _HttpTransportConfig:
    """Trust forwarding headers only for an explicit loopback proxy chain.

    ``ProxyFix`` selects the right-most configured values, so untrusted values
    prepended by a client cannot become the rate-limit identity. The transport
    resolver prevents clients from reaching this WSGI listener directly when
    forwarding headers are enabled.
    """
    profile = profile or _resolve_http_transport()
    trusted_proxy_hops = profile.trusted_proxy_hops
    configured_hops = flask_app.config.get("_WB_TRUSTED_PROXY_HOPS_CONFIGURED")
    if configured_hops is not None:
        if configured_hops != trusted_proxy_hops:
            raise _HttpTransportConfigurationError(
                "trusted proxy middleware is already configured with a different hop count"
            )
        return profile

    flask_app.config["HTTP_TRUSTED_PROXY_HOPS"] = trusted_proxy_hops
    flask_app.config["_WB_TRUSTED_PROXY_HOPS_CONFIGURED"] = trusted_proxy_hops
    if trusted_proxy_hops:
        flask_app.wsgi_app = ProxyFix(
            flask_app.wsgi_app,
            x_for=trusted_proxy_hops,
            x_proto=trusted_proxy_hops,
        )
    return profile


# Flask pre-populates these keys, so ``setdefault`` would silently preserve
# SESSION_COOKIE_SAMESITE=None and SESSION_COOKIE_SECURE=False.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
_http_transport_profile = _configure_session_cookie_secure(app)
_configure_trusted_proxy(app, _http_transport_profile)


# ── Debug logging helpers ──────────────────────────────────────────────────
def _is_debug_logging_enabled() -> bool:
    try:
        return bool(db.get_logging_debug())
    except (sqlite3.Error, OSError) as e:
        logger.debug("debug logging check: %s", e)
        return False


def dlog(msg: str, *args) -> None:
    if _is_debug_logging_enabled():
        try:
            logger.info("DBG: " + msg, *args)
        except (TypeError, ValueError) as e:
            logger.debug("dlog format error: %s", e)


# ── Shared helper: check paths allowed without an admin session ────────────
# Only read-only POSTs and fail-safe OFF actions are public.  Native WB deploy
# exposes Flask directly on the LAN and does not install an auth proxy.
import re as _re

_ALLOWED_PUBLIC_POSTS = {
    "/api/login",
    "/api/status",
    "/api/emergency-stop",
    "/api/postpone",
    "/api/audit/ui",
}

_ALLOWED_AUTHENTICATED_READ_POSTS = {"/api/zones/next-watering-bulk"}

# Anonymous callers get one identity/introspection endpoint only. Rich status,
# topology, sensor configuration and history require the explicit viewer link
# (/login?guest=1) or an authenticated admin session. Liveness/readiness live
# outside /api and have their own deliberately minimal contracts.
_ALLOWED_PUBLIC_GETS = {"/api/auth/status"}

# Explicit OFF actions remain available for incident response.
_ALLOWED_PUBLIC_PATTERNS = [
    _re.compile(r"^/api/zones/\d+/mqtt/stop$"),
    _re.compile(r"^/api/zones/\d+/stop$"),
    _re.compile(r"^/api/groups/\d+/stop$"),
]


def _is_status_action(path):
    if path in _ALLOWED_PUBLIC_POSTS:
        return True
    for pat in _ALLOWED_PUBLIC_PATTERNS:
        if pat.match(path):
            return True
    return False


def _is_postpone_cancel_request(path: str) -> bool:
    """Return whether this request removes a watering postponement."""
    if path != "/api/postpone" or request.method != "POST":
        return False
    payload = request.get_json(silent=True)
    return isinstance(payload, dict) and payload.get("action") == "cancel"


# ── Auth before-request ────────────────────────────────────────────────────
@app.before_request
def _auth_before_request():
    # Issue #50: skip all session/auth processing for /static/* so Flask doesn't
    # mark the session dirty (which would emit Set-Cookie + Cache-Control: no-cache
    # and defeat browser caching of /static/media/maps/*.webp etc.).
    # Static paths require no auth — early return is safe.
    if request.path.startswith("/static/"):
        return None
    # Exact, safe-method-only bypass: liveness/readiness must never allocate a
    # session or block behind password-policy SQLite work. Their own handlers
    # expose only a minimal public contract.
    if request.method in {"GET", "HEAD"} and request.path in {"/healthz", "/readyz"}:
        return None
    if "role" not in session:
        session["role"] = "guest"
    try:
        if not app.config.get("TESTING"):
            pth = request.path or ""
            logged_in = session.get("logged_in") is True
            role = session.get("role")
            # The shipped login button historically points at `/?guest=1`.
            # Route that legacy URL through the canonical guest-login handler,
            # which regenerates the session and refuses to downgrade admins.
            if pth in {"/", "/status"} and request.args.get("guest") == "1":
                return redirect(url_for("auth_bp.login_page", guest="1"))
            if pth.startswith("/api/") and request.method in {"GET", "HEAD"}:
                if not logged_in and pth not in _ALLOWED_PUBLIC_GETS:
                    return jsonify({"success": False, "message": "auth required", "error_code": "UNAUTHENTICATED"}), 401
                return None
            try:
                db.ensure_password_change_required()
            except (sqlite3.Error, OSError) as e:
                logger.debug("ensure_password_change_required: %s", e)
            if pth.startswith("/api/"):
                postpone_cancel = _is_postpone_cancel_request(pth)
                is_admin = logged_in and role == "admin"
                is_viewer = logged_in and role == "viewer"
                is_authenticated_read = request.method == "POST" and pth in _ALLOWED_AUTHENTICATED_READ_POSTS
                if is_authenticated_read:
                    if logged_in and role in {"viewer", "user", "admin"}:
                        return None
                    return jsonify(
                        {
                            "success": False,
                            "message": "auth required",
                            "error_code": "UNAUTHENTICATED",
                        }
                    ), 401
                if is_viewer and request.method in ["POST", "PUT", "DELETE"]:
                    if pth != "/api/login":
                        return jsonify(
                            {"success": False, "message": "viewer role: read-only access", "error_code": "FORBIDDEN"}
                        ), 403
                is_public_action = _is_status_action(pth) and not postpone_cancel
                if not is_admin and not is_public_action:
                    return jsonify({"success": False, "message": "auth required", "error_code": "UNAUTHENTICATED"}), 401
                if is_admin and request.method in ["POST", "PUT", "DELETE"]:
                    must = db.get_setting_value("password_must_change")
                    if str(must or "0") == "1" and request.path != "/api/password":
                        return jsonify(
                            {
                                "success": False,
                                "message": "password change required",
                                "error_code": "PASSWORD_MUST_CHANGE",
                            }
                        ), 403
                    if postpone_cancel and app.config.get("WTF_CSRF_ENABLED"):
                        # api_postpone is view-exempt for the fail-safe delay
                        # action.  Force validation here for cancel, without
                        # applying the view exemption.
                        csrf.protect()
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.warning("auth before_request error: %s", e)


# ── Blueprint registration ─────────────────────────────────────────────────
for bp in (status_bp, files_bp, zones_bp, programs_bp, groups_bp, auth_bp, settings_bp):
    app.register_blueprint(bp)
try:
    if reports_bp:
        app.register_blueprint(reports_bp)
except (ValueError, TypeError, KeyError) as e:
    logger.warning("Optional blueprint registration failed: %s", e)
try:
    from routes.mqtt import mqtt_bp

    app.register_blueprint(mqtt_bp)
except ImportError as _e:
    logger.warning(f"MQTT blueprint not registered: {_e}")

for bp in (
    zones_crud_api_bp,
    zones_photo_api_bp,
    zones_watering_api_bp,
    groups_api_bp,
    programs_api_bp,
    mqtt_api_bp,
    system_status_api_bp,
    system_config_api_bp,
    system_emergency_api_bp,
    weather_api_bp,
    audit_api_bp,
    zones_history_api_bp,
):
    app.register_blueprint(bp)

# F2 — observability endpoints: /healthz, /readyz, /metrics.
# All GET-only; CSRF-exempt as defence-in-depth in case POST endpoints are
# added later.  init_metrics() is called from services.app_init.initialize_app().
csrf.exempt(health_api_bp)
app.register_blueprint(health_api_bp)


# ── Group exclusivity watchdog ─────────────────────────────────────────────
def _zone_may_be_energized(zone: dict) -> bool:
    """Conservatively classify one row for exclusivity enforcement."""
    state = str(zone.get("state") or "").lower()
    commanded = str(zone.get("commanded_state") or "").lower()
    physical = bool(zone.get("mqtt_server_id") and str(zone.get("topic") or "").strip())
    if not physical:
        return state in {"on", "starting"}
    observed = str(zone.get("observed_state") or "").lower()
    if observed == "on":
        return True
    if commanded == "on" and state in {"on", "starting"}:
        return True
    return observed != "off" and state in {"on", "starting", "stopping", "fault"}


def _force_group_exclusive(group_id: int, reason: str = "group_exclusive") -> None:
    """Repair one group from a fresh snapshot under its serialization lock."""
    try:
        from services.locks import group_lock

        with group_lock(int(group_id)):
            _force_group_exclusive_locked(int(group_id), reason)
    except (RuntimeError, TypeError, ValueError, sqlite3.Error, OSError, ImportError) as e:
        logger.error("Group exclusivity lock failed for group %s: %s", group_id, e)


def _force_group_exclusive_locked(group_id: int, reason: str) -> None:
    try:
        group_zones = db.get_zones_by_group(group_id)
        try:
            g = next((gg for gg in (db.get_groups() or []) if int(gg.get("id")) == int(group_id)), None)
            mv_topic = normalize_topic((g.get("master_mqtt_topic") or "").strip()) if g else ""
        except (TypeError, ValueError, StopIteration) as e:
            logger.debug("group exclusive mv_topic lookup: %s", e)
            mv_topic = ""

        def _is_mv(z):
            try:
                return bool(mv_topic) and normalize_topic((z.get("topic") or "").strip()) == mv_topic
            except (TypeError, ValueError) as e:
                logger.debug("_is_mv check: %s", e)
                return False

        on_zones = [z for z in group_zones if _zone_may_be_energized(z) and not _is_mv(z)]
        if len(on_zones) <= 1:
            return

        def started_key(z):
            try:
                return datetime.strptime(z.get("watering_start_time") or "", "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                logger.debug("started_key parse failed for zone %s", z.get("id"))
                return datetime.min

        on_zones.sort(key=started_key, reverse=True)
        turned_off: list[int] = []
        physically_off: list[int] = []
        faulted: list[int] = []
        failed: list[int] = []
        unresolved: list[int] = []
        group_shutdown = False
        zones_to_stop = list(on_zones[1:])
        keeper_id = int(on_zones[0]["id"])

        def _require_group_shutdown(zone_id: int) -> None:
            nonlocal group_shutdown
            if zone_id not in unresolved:
                unresolved.append(zone_id)
            if not group_shutdown:
                group_shutdown = True
                zones_to_stop.append(on_zones[0])

        for z in zones_to_stop:
            zone_id = int(z["id"])
            shutting_down_keeper = group_shutdown and zone_id == keeper_id
            stopped = False
            try:
                from services.zone_control import stop_zone as _stop_central_gex

                stopped = bool(
                    _stop_central_gex(
                        zone_id,
                        reason="group_exclusive",
                        force=True,
                        master_close_immediately=shutting_down_keeper,
                        skip_master_close=not shutting_down_keeper,
                        require_observed_confirmation=True,
                    )
                )
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, ImportError) as e:
                logger.error("group exclusive stop_zone for %s: %s", z.get("id"), e)

            current = db.get_zone(zone_id)
            if (
                stopped
                and current
                and str(current.get("state") or "").lower() == "off"
                and not _zone_may_be_energized(current)
            ):
                turned_off.append(zone_id)
                continue
            if stopped:
                logger.error(
                    "group exclusive stop reported success without OFF state zone=%s state=%s",
                    zone_id,
                    (current or {}).get("state"),
                )
            if current and not _zone_may_be_energized(current):
                physically_off.append(zone_id)
                continue

            # A failed or internally inconsistent stop must never be rewritten
            # as a successful OFF.  Pin the exact still-current generation to
            # FAULT so it cannot be scheduled again, while retaining activation
            # timestamps for incident recovery and truthful run history.
            if (
                current
                and str(current.get("state") or "").lower() == "fault"
                and str(current.get("commanded_state") or "").lower() == "off"
            ):
                faulted.append(zone_id)
                _require_group_shutdown(zone_id)
                continue
            try:
                from services.zones_state import update_zone_state as _uzs_strict

                snapshot = current or db.get_zone(zone_id)
                if not snapshot:
                    failed.append(zone_id)
                    _require_group_shutdown(zone_id)
                    continue
                fallback_fields = {
                    "state": "fault",
                    "commanded_state": "off",
                    "fault_count": int(snapshot.get("fault_count") or 0) + 1,
                    "last_fault": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                applied, _conflict = _uzs_strict(
                    zone_id,
                    fallback_fields,
                    expected_version=snapshot["version"],
                    audit_reason=f"group_exclusivity_{reason}",
                    db=db,
                )
                if applied:
                    faulted.append(zone_id)
                else:
                    failed.append(zone_id)
                    logger.error("group exclusive fault fallback CAS conflicted zone=%s", zone_id)
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, ImportError) as e:
                failed.append(zone_id)
                logger.error("group exclusive audited fallback for zone %s: %s", z.get("id"), e)
            # A FAULT transition records uncertainty; it is not physical OFF
            # proof.  Once any loser reaches this path, remove the elected
            # keeper as well so the group fails closed as a whole.
            _require_group_shutdown(zone_id)
        try:
            db.add_log(
                "warning",
                json.dumps(
                    {
                        "type": "group_exclusive_fix",
                        "group_id": group_id,
                        "kept_zone": None if group_shutdown else on_zones[0].get("id"),
                        "group_shutdown": group_shutdown,
                        "turned_off": turned_off,
                        "physically_off": physically_off,
                        "faulted": faulted,
                        "failed": failed,
                        "unresolved": unresolved,
                    }
                ),
            )
        except (sqlite3.Error, json.JSONDecodeError, OSError, ValueError, TypeError, KeyError) as e:
            logger.debug("group exclusive log: %s", e)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, sqlite3.Error, OSError, ImportError) as e:
        logger.error(f"Group exclusivity enforcement failed for group {group_id}: {e}")


def _enforce_group_exclusive_all_groups() -> None:
    try:
        zones = db.get_zones()
        zones_by_group = {}
        for z in zones:
            gid = int(z.get("group_id") or 0)
            if gid in (0, 999):
                continue
            zones_by_group.setdefault(gid, []).append(z)
        for gid, arr in zones_by_group.items():
            try:
                g = next((gg for gg in (db.get_groups() or []) if int(gg.get("id")) == int(gid)), None)
                mv_topic = normalize_topic((g.get("master_mqtt_topic") or "").strip()) if g else ""
            except (TypeError, ValueError, StopIteration) as e:
                logger.debug("enforce_all mv_topic: %s", e)
                mv_topic = ""

            def _is_mv(z):
                try:
                    return bool(mv_topic) and normalize_topic((z.get("topic") or "").strip()) == mv_topic
                except (TypeError, ValueError) as e:
                    logger.debug("_is_mv check (enforce_all): %s", e)
                    return False

            if sum(1 for z in arr if _zone_may_be_energized(z) and not _is_mv(z)) > 1:
                _force_group_exclusive(gid, "watchdog")
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error, ImportError, TypeError, ValueError) as e:
        logger.exception("enforce_group_exclusive_all: %s", e)


_WATCHDOG_STARTED = False
_WATCHDOG_STOP_EVENT = threading.Event()


def _start_single_zone_watchdog():
    global _WATCHDOG_STARTED
    if _WATCHDOG_STARTED:
        return
    _WATCHDOG_STARTED = True

    def _run():
        while not _WATCHDOG_STOP_EVENT.is_set():
            try:
                _enforce_group_exclusive_all_groups()
            except (
                ConnectionError,
                TimeoutError,
                OSError,
                sqlite3.Error,
                ValueError,
                RuntimeError,
            ) as e:  # catch-all: intentional
                logger.exception("watchdog loop: %s", e)
            _WATCHDOG_STOP_EVENT.wait(1.0)

    threading.Thread(target=_run, daemon=True).start()


import atexit

atexit.register(lambda: _WATCHDOG_STOP_EVENT.set())

# ── Graceful shutdown: atexit fallback ──────────────────────────────────────
from services.shutdown import shutdown_all_zones_off

atexit.register(shutdown_all_zones_off, timeout_sec=5)


# ── General API rate limiter ────────────────────────────────────────────────
@app.before_request
def _general_api_rate_limit():
    """Apply a general 30 req/min rate limit to all mutating API endpoints
    not already covered by endpoint-specific rate_limit decorators."""
    if app.config.get("TESTING"):
        return None
    p = request.path or ""
    if not p.startswith("/api/") or request.method == "GET":
        return None
    # Skip paths that have their own decorators (mqtt_control, emergency, programs)
    # or non-mutable paths
    skip_paths = {"/api/login", "/api/password", "/api/status", "/health", "/api/env"}
    if p in skip_paths:
        return None
    # Explicit OFF actions are incident-response controls.  They must reach
    # the controller even after unrelated general mutations exhaust the bucket.
    if any(pattern.match(p) for pattern in _ALLOWED_PUBLIC_PATTERNS):
        return None
    # Specific groups already have their own limits applied via decorators
    if "/mqtt/start" in p or "/mqtt/stop" in p:
        return None
    if p in ("/api/emergency-stop", "/api/emergency-resume"):
        return None
    if p.startswith("/api/programs"):
        return None
    ip = request.remote_addr or "0.0.0.0"
    allowed, retry_after = _rate_check(ip, "general_mutation", 30, 60)
    if not allowed:
        resp = jsonify(
            {
                "success": False,
                "message": "Too many requests",
                "error_code": "RATE_LIMITED",
                "retry_after": retry_after,
            }
        )
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry_after)
        return resp


_mark_zone_stopped = _sse_hub.mark_zone_stopped
_recently_stopped = _sse_hub.recently_stopped


# ── Misc routes ────────────────────────────────────────────────────────────
@app.errorhandler(404)
def _not_found(e):
    try:
        return render_template("404.html"), 404
    except (OSError, ValueError, RuntimeError) as e:  # catch-all: intentional
        logger.debug("404 template fallback: %s", e)
        return jsonify({"error": "Not found"}), 404


@app.route("/sw.js")
def service_worker():
    sw_path = os.path.join(app.static_folder, "sw.js")
    # Sanitize: CACHE_NAME is a JS string literal — strip anything that could
    # break the quote or thrash cache between dev runs (e.g. spaces, '+dirty').
    safe_version = _re.sub(r"[^A-Za-z0-9._-]", "-", APP_VERSION)
    with open(sw_path, encoding="utf-8") as f:
        body = f.read().replace("__APP_VERSION__", safe_version)
    resp = app.response_class(body, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.route("/ws")
def ws_stub():
    resp = jsonify({"success": False, "message": "WebSocket not supported. Use SSE at /api/mqtt/zones-sse"})
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _publish_mqtt_async(server, topic, value, min_interval_sec=0.0):
    try:
        threading.Thread(
            target=lambda: _publish_mqtt_value(server, topic, value, min_interval_sec=min_interval_sec), daemon=True
        ).start()
    except (RuntimeError, OSError) as e:
        logger.warning("_publish_mqtt_async thread start: %s", e)


_initialize_app(app, db, start_watchdog_fn=lambda: _start_single_zone_watchdog())

# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_scheduler(db)
    # Direct `python app.py` is dev-only. Production uses run.py via hypercorn.
    # debug flag follows FLASK_DEBUG env var (default off).
    _dev_profile = _resolve_http_transport()
    _dev_ssl_context = (_dev_profile.tls_certfile, _dev_profile.tls_keyfile) if _dev_profile.tls_enabled else None
    app.run(
        debug=os.environ.get("FLASK_DEBUG") == "1",
        host=_dev_profile.bind_host,
        port=_dev_profile.port,
        ssl_context=_dev_ssl_context,
    )
