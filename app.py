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

if _os_early.environ.get("TESTING") != "1" and "PYTEST_CURRENT_TEST" not in _os_early.environ:
    from services.logging_setup import setup_logging as _setup_logging_early

    _setup_logging_early(logging.getLogger("app"))
logger = logging.getLogger(__name__)

import json
import os
import sqlite3
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request, session
from flask_wtf.csrf import CSRFProtect

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
from routes.admin_users import admin_users_bp
from routes.auth import auth_bp
from routes.files import files_bp
from routes.groups import groups_bp
from routes.programs import programs_bp
from routes.settings import settings_bp

# Page-rendering blueprints
from routes.status import status_bp
from routes.zones import zones_bp

try:
    from routes.telegram import telegram_bp
except ImportError as e:
    logging.getLogger(__name__).debug("telegram blueprint not loaded: %s", e)
    telegram_bp = None
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
app.db = db
csrf = CSRFProtect(app)

# Exempt login endpoints from CSRF — login page doesn't include CSRF token
from routes.auth import api_login as _api_login_view
from routes.auth import api_login_escalate as _api_login_escalate_view

csrf.exempt(_api_login_view)
csrf.exempt(_api_login_escalate_view)

# ── CSRF policy (SEC-003 fix) ──────────────────────────────────────────────
# Previously every API blueprint was `csrf.exempt(bp)`, leaving all mutating
# endpoints open to CSRF from any site the admin visits.  Now we exempt
# ONLY the endpoints that are meant to be callable by the nginx-basic-auth
# guest flow (the gardener who does not hold a Flask session / CSRF token),
# which corresponds 1-to-1 with `_ALLOWED_PUBLIC_POSTS` /
# `_ALLOWED_PUBLIC_PATTERNS` below.  Every other admin-only CRUD endpoint
# (photo upload/delete/rotate, zone/program/group create/update/delete,
# weather settings, MQTT config, etc.) now requires the X-CSRFToken header
# that `static/js/app.js` already attaches on all non-GET fetch calls.
#
# Guest-accessible endpoints (remain CSRF-exempt):
from routes.audit_api import api_audit_ui_event
from routes.groups_api import (
    api_master_valve_toggle,
    api_start_group_from_first,
    api_start_zone_exclusive,
    api_stop_group,
)
from routes.system_config_api import api_env_config, api_postpone
from routes.system_emergency_api import api_emergency_resume, api_emergency_stop
from routes.system_status_api import api_status
from routes.zones_crud_api import api_zones_next_watering_bulk
from routes.zones_watering_api import api_zone_mqtt_start, api_zone_mqtt_stop, start_zone, stop_zone

for _view in (
    api_env_config,  # /api/env
    api_postpone,  # /api/postpone
    api_emergency_stop,  # /api/emergency-stop
    api_emergency_resume,  # /api/emergency-resume
    api_status,  # /api/status
    start_zone,  # /api/zones/<id>/start
    stop_zone,  # /api/zones/<id>/stop
    api_zone_mqtt_start,  # /api/zones/<id>/mqtt/start
    api_zone_mqtt_stop,  # /api/zones/<id>/mqtt/stop
    api_zones_next_watering_bulk,  # /api/zones/next-watering-bulk
    api_stop_group,  # /api/groups/<id>/stop
    api_start_group_from_first,  # /api/groups/<id>/start-from-first
    api_start_zone_exclusive,  # /api/groups/<id>/start-zone/<zid>
    api_master_valve_toggle,  # /api/groups/<id>/master-valve/<action>
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
#   system_config_api_bp except the three guest endpoints above
#   system_status_api_bp except /api/status
#   groups_api_bp       (admin group CRUD; control endpoints exempt above)

_sse_hub.init(
    db=db,
    mqtt_module=mqtt,
    app_config=app.config,
    publish_mqtt_value=_publish_mqtt_value,
    normalize_topic=normalize_topic,
    get_scheduler=get_scheduler,
)

try:
    app.config.setdefault("SEND_FILE_MAX_AGE_DEFAULT", 60 * 60 * 24 * 7)
except (TypeError, ValueError) as e:
    logger.debug("SEND_FILE_MAX_AGE_DEFAULT config: %s", e)

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


try:
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    if not Config.TESTING and "SESSION_COOKIE_SECURE" not in app.config:
        app.config["SESSION_COOKIE_SECURE"] = bool(
            os.environ.get("SESSION_COOKIE_SECURE", "0") in ("1", "true", "True")
        )
    # Issue #52: long-lived session so iPhone Safari doesn't drop the cookie
    # after a few hours of inactivity (the original Basic Auth pain point).
    from datetime import timedelta as _td

    app.config.setdefault("PERMANENT_SESSION_LIFETIME", _td(days=365))
except (TypeError, KeyError) as e:
    logger.debug("Session cookie config: %s", e)


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


# Issue #52: guest access (nginx basic auth pass-through) removed.
# _ALLOWED_PUBLIC_POSTS / _ALLOWED_PUBLIC_PATTERNS / _is_status_action() were
# deleted — every mutating /api/* endpoint now requires an authenticated
# session via the in-app login flow. Cloudflare Worker handles edge auth
# for the bot; nothing inside Flask depends on the old whitelist anymore.

import re as _re  # noqa: F401  (kept — other parts of the file use _re)


# ── Auth before-request ────────────────────────────────────────────────────
# Issue #52: guest access removed. Any /api/* request that isn't /api/login,
# /api/login/escalate, /api/account/password (viewers) or /api/auth/status
# now requires an authenticated session (viewer or admin).
_PUBLIC_AUTH_PATHS = {"/api/login", "/api/login/escalate", "/api/auth/status"}


@app.before_request
def _auth_before_request():
    try:
        if not app.config.get("TESTING"):
            try:
                db.ensure_password_change_required()
            except (sqlite3.Error, OSError) as e:
                logger.debug("ensure_password_change_required: %s", e)
            if request.path.startswith("/api/"):
                pth = request.path or ""
                # GET endpoints stay read-accessible to anyone (legacy callers
                # like /api/status are scraped by the gardener LCD); harden
                # later if needed.
                if request.method == "GET":
                    return None
                role = session.get("role")
                # Public POST endpoints (login/escalate) — no session required.
                if pth in _PUBLIC_AUTH_PATHS:
                    return None
                # Anyone without a logged-in session is denied. No more guest fallback.
                if role not in ("viewer", "admin"):
                    return jsonify({"success": False, "message": "auth required", "error_code": "UNAUTHENTICATED"}), 401
                if role == "viewer" and request.method in ["POST", "PUT", "DELETE"]:
                    if pth != "/api/account/password":
                        return jsonify(
                            {"success": False, "message": "viewer role: read-only access", "error_code": "FORBIDDEN"}
                        ), 403
                if role == "admin" and request.method in ["POST", "PUT", "DELETE"]:
                    must = db.get_setting_value("password_must_change")
                    if str(must or "0") == "1" and request.path != "/api/password":
                        return jsonify(
                            {
                                "success": False,
                                "message": "password change required",
                                "error_code": "PASSWORD_MUST_CHANGE",
                            }
                        ), 403
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.warning("auth before_request error: %s", e)


# ── Blueprint registration ─────────────────────────────────────────────────
for bp in (status_bp, files_bp, zones_bp, programs_bp, groups_bp, auth_bp, settings_bp, admin_users_bp):
    app.register_blueprint(bp)
try:
    if telegram_bp:
        app.register_blueprint(telegram_bp)
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


# ── Mutation guard ─────────────────────────────────────────────────────────
# Issue #52: guest access removed. Any non-GET /api/* request must be made by
# an admin (the few viewer-allowed paths are handled in _auth_before_request).
@app.before_request
def _require_admin_for_mutations():
    try:
        if app.config.get("TESTING"):
            return None
        p = request.path or ""
        if not p.startswith("/api/") or request.method == "GET":
            return None
        role = session.get("role")
        if request.method in ["POST", "PUT", "DELETE"]:
            # Bypass list — same as _auth_before_request, plus /api/env and
            # /api/password kept for back-compat with older clients.
            if p in ("/api/login", "/api/login/escalate", "/api/account/password", "/api/password") or p.startswith(
                "/api/env"
            ):
                return None
            if role != "admin":
                return jsonify({"success": False, "message": "admin required", "error_code": "FORBIDDEN"}), 403
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.warning("mutation guard error: %s", e)
        return None


# ── Group exclusivity watchdog ─────────────────────────────────────────────
def _force_group_exclusive(group_id: int, reason: str = "group_exclusive") -> None:
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

        on_zones = [z for z in group_zones if str(z.get("state")) == "on" and not _is_mv(z)]
        if len(on_zones) <= 1:
            return

        def started_key(z):
            try:
                return datetime.strptime(z.get("watering_start_time") or "", "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                logger.debug("started_key parse failed for zone %s", z.get("id"))
                return datetime.min

        on_zones.sort(key=started_key, reverse=True)
        for z in on_zones[1:]:
            try:
                sid = z.get("mqtt_server_id")
                topic = (z.get("topic") or "").strip()
                if mqtt and sid and topic:
                    server = db.get_mqtt_server(int(sid))
                    if server:
                        _publish_mqtt_value(
                            server, normalize_topic(topic), "0", min_interval_sec=0.0, qos=2, retain=True
                        )
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.warning("group exclusive mqtt off for zone %s: %s", z.get("id"), e)
            try:
                # Use central stop_zone to ensure master close scheduling runs
                # alongside the DB transition (was: direct state='off' write).
                from services.zone_control import stop_zone as _stop_central_gex

                _stop_central_gex(int(z["id"]), reason="group_exclusive", force=True)
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, ImportError) as e:
                logger.error("group exclusive stop_zone for %s: %s", z.get("id"), e)
                # Fallback to direct DB update if central stop fails — but
                # still go through the audited helper so 'group_exclusivity'
                # transitions are visible to triage.  If even that path
                # blows up, drop to raw update_zone last.
                try:
                    # last_watering_time is no longer a column — derived
                    # from zone_runs at read time. Just transition state.
                    from services.zones_state import update_zone_state as _uzs

                    _uzs(int(z["id"]), {"state": "off", "watering_start_time": None}, audit_reason="group_exclusivity")
                except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, ImportError) as e2:
                    logger.error("group exclusive audited fallback for zone %s: %s", z.get("id"), e2)
                    try:
                        db.update_zone(int(z["id"]), {"state": "off", "watering_start_time": None})
                    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as e3:
                        logger.error("group exclusive db update fallback for zone %s: %s", z.get("id"), e3)
        try:
            db.add_log(
                "warning",
                json.dumps(
                    {
                        "type": "group_exclusive_fix",
                        "group_id": group_id,
                        "kept_zone": on_zones[0].get("id"),
                        "turned_off": [z.get("id") for z in on_zones[1:]],
                    }
                ),
            )
        except (sqlite3.Error, json.JSONDecodeError, OSError, ValueError, TypeError, KeyError) as e:
            logger.debug("group exclusive log: %s", e)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
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

            if sum(1 for z in arr if str(z.get("state")) == "on" and not _is_mv(z)) > 1:
                _force_group_exclusive(gid, "watchdog")
    except (ConnectionError, TimeoutError, OSError) as e:
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
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", host="0.0.0.0", port=8080)  # nosec B104
