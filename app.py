"""WB Irrigation — main Flask application (core only).

All API routes live in routes/*_api.py blueprints.
This file handles: app creation, config, logging, middleware, blueprint registration, boot-init.
"""
import sqlite3
from flask import Flask, render_template, jsonify, request, session, Response
from datetime import datetime
import json
from database import db
from utils import normalize_topic
import os
import logging
from irrigation_scheduler import init_scheduler, get_scheduler
from services.mqtt_pub import publish_mqtt_value as _publish_mqtt_value
from flask_wtf.csrf import CSRFProtect
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
import time as _perf_time
import threading
import time
from config import Config
from services.logging_setup import setup_logging

# Page-rendering blueprints
from routes.status import status_bp
from routes.files import files_bp
from routes.zones import zones_bp
from routes.programs import programs_bp
from routes.groups import groups_bp
from routes.auth import auth_bp
from routes.settings import settings_bp
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
from routes.zones_crud_api import zones_crud_api_bp
from routes.zones_photo_api import zones_photo_api_bp
from routes.zones_watering_api import zones_watering_api_bp
from routes.groups_api import groups_api_bp
from routes.programs_api import programs_api_bp
from routes.mqtt_api import mqtt_api_bp
from routes.system_status_api import system_status_api_bp
from routes.system_config_api import system_config_api_bp
from routes.system_emergency_api import system_emergency_api_bp
from routes.weather_api import weather_api_bp


try:
    from services.telegram_bot import subscribe_to_events as _tg_subscribe
    _tg_subscribe()
    from services.telegram_bot import start_long_polling_if_needed as _tg_poll_start
    _tg_poll_start()
except ImportError as e:
    logging.getLogger(__name__).debug("Telegram bot init skipped: %s", e)
from services.api_rate_limiter import _is_allowed as _rate_check
from services import sse_hub as _sse_hub
from services.app_init import initialize_app as _initialize_app

# ── Logging ────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
setup_logging(logger)

# ── Flask app ──────────────────────────────────────────────────────────────
app = Flask(__name__)
# Use TestConfig when TESTING=1 to disable CSRF
if os.environ.get('TESTING') == '1':
    from config import TestConfig
    app.config.from_object(TestConfig)
else:
    app.config.from_object(Config)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB (route-level MAX_FILE_SIZE enforces 5MB)
app.db = db
csrf = CSRFProtect(app)

# Exempt login endpoint from CSRF — login page doesn't include CSRF token
from routes.auth import api_login as _api_login_view
csrf.exempt(_api_login_view)

_sse_hub.init(db=db, mqtt_module=mqtt, app_config=app.config, publish_mqtt_value=_publish_mqtt_value, normalize_topic=normalize_topic, get_scheduler=get_scheduler)

try:
    app.config.setdefault('SEND_FILE_MAX_AGE_DEFAULT', 60 * 60 * 24 * 7)
except (TypeError, ValueError) as e:
    logger.debug("SEND_FILE_MAX_AGE_DEFAULT config: %s", e)

# ── App version ────────────────────────────────────────────────────────────
def _compute_app_version() -> str:
    try:
        version_file = os.path.join(os.path.dirname(__file__), 'VERSION')
        with open(version_file, 'r') as f:
            return f.read().strip()
    except (IOError, OSError, PermissionError) as e:
        logger.debug("VERSION file read failed: %s", e)
        return '2.0.0'

APP_VERSION = _compute_app_version()

@app.context_processor
def _inject_app_version():
    try:
        sys_name = db.get_setting_value('system_name') or ''
        asset = lambda path: f"{path}?v={APP_VERSION}"
        return {'app_version': APP_VERSION, 'system_name': sys_name, 'asset': asset}
    except (sqlite3.Error, OSError) as e:
        logger.debug("context processor fallback: %s", e)
        return {'app_version': '1.0', 'system_name': '', 'asset': (lambda p: p)}



# ── Middleware ──────────────────────────────────────────────────────────────
@app.before_request
def _perf_start_timer():
    try:
        request._started_at = _perf_time.time()
    except AttributeError as e:
        logger.debug("perf timer start: %s", e)

@app.after_request
def _perf_add_server_timing(resp: Response):
    try:
        t0 = getattr(request, '_started_at', None)
        if t0 is not None:
            resp.headers['Server-Timing'] = f"app;dur={int((_perf_time.time() - t0) * 1000)}"
    except (TypeError, ValueError, AttributeError) as e:
        logger.debug("perf timer end: %s", e)
    return resp

@app.after_request
def add_security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'"
    )
    return resp

try:
    app.config.setdefault('SESSION_COOKIE_SAMESITE', 'Lax')
    app.config.setdefault('SESSION_COOKIE_HTTPONLY', True)
    if not Config.TESTING:
        if 'SESSION_COOKIE_SECURE' not in app.config:
            app.config['SESSION_COOKIE_SECURE'] = bool(os.environ.get('SESSION_COOKIE_SECURE', '0') in ('1', 'true', 'True'))
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

# ── Shared helper: check if a path is a "status action" allowed without admin ──
# Service runs behind nginx basic auth. Internal Flask auth for zone/group
# control is unnecessary — gardeners need start/stop without admin password.
import re as _re

_ALLOWED_PUBLIC_POSTS = {'/api/login', '/api/password', '/api/status', '/health', '/api/env', '/api/emergency-stop', '/api/emergency-resume', '/api/postpone', '/api/zones/next-watering-bulk'}

# Patterns for zone/group control endpoints that guests (nginx basic auth users) can access
_ALLOWED_PUBLIC_PATTERNS = [
    _re.compile(r'^/api/zones/\d+/mqtt/start$'),
    _re.compile(r'^/api/zones/\d+/mqtt/stop$'),
    _re.compile(r'^/api/zones/\d+/start$'),
    _re.compile(r'^/api/zones/\d+/stop$'),
    _re.compile(r'^/api/groups/\d+/start-from-first$'),
    _re.compile(r'^/api/groups/\d+/stop$'),
    _re.compile(r'^/api/groups/\d+/master-valve/\w+$'),
    _re.compile(r'^/api/groups/\d+/start-zone/\d+$'),
]

def _is_status_action(path):
    if path in _ALLOWED_PUBLIC_POSTS:
        return True
    for pat in _ALLOWED_PUBLIC_PATTERNS:
        if pat.match(path):
            return True
    return False

# ── Auth before-request ────────────────────────────────────────────────────
@app.before_request
def _auth_before_request():
    if 'role' not in session:
        session['role'] = 'guest'
    try:
        if not app.config.get('TESTING'):
            try:
                db.ensure_password_change_required()
            except (sqlite3.Error, OSError) as e:
                logger.debug("ensure_password_change_required: %s", e)
            if request.path.startswith('/api/'):
                if request.method == 'GET':
                    return None
                pth = request.path or ''
                if session.get('role') == 'viewer' and request.method in ['POST', 'PUT', 'DELETE']:
                    if pth != '/api/login':
                        return jsonify({'success': False, 'message': 'viewer role: read-only access', 'error_code': 'FORBIDDEN'}), 403
                if session.get('role') != 'admin' and not _is_status_action(pth):
                    return jsonify({'success': False, 'message': 'auth required', 'error_code': 'UNAUTHENTICATED'}), 401
                if session.get('role') == 'admin' and request.method in ['POST', 'PUT', 'DELETE']:
                    must = db.get_setting_value('password_must_change') if True else None
                    if str(must or '0') == '1' and request.path != '/api/password':
                        return jsonify({'success': False, 'message': 'password change required', 'error_code': 'PASSWORD_MUST_CHANGE'}), 403
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.warning("auth before_request error: %s", e)

# ── Blueprint registration ─────────────────────────────────────────────────
for bp in (status_bp, files_bp, zones_bp, programs_bp, groups_bp, auth_bp, settings_bp):
    app.register_blueprint(bp)
try:
    if telegram_bp: app.register_blueprint(telegram_bp)
    if reports_bp: app.register_blueprint(reports_bp)
except (ValueError, TypeError, KeyError) as e:
    logger.warning("Optional blueprint registration failed: %s", e)
try:
    from routes.mqtt import mqtt_bp
    app.register_blueprint(mqtt_bp)
except ImportError as _e:
    logger.warning(f"MQTT blueprint not registered: {_e}")

for bp in (zones_crud_api_bp, zones_photo_api_bp, zones_watering_api_bp, groups_api_bp, programs_api_bp, mqtt_api_bp, system_status_api_bp, system_config_api_bp, system_emergency_api_bp, weather_api_bp):
    app.register_blueprint(bp)

_initialize_app(app, db)

# ── Mutation guard ─────────────────────────────────────────────────────────
@app.before_request
def _require_admin_for_mutations():
    try:
        if app.config.get('TESTING'):
            return None
        p = request.path or ''
        if not p.startswith('/api/') or request.method == 'GET':
            return None
        role = session.get('role', 'guest')
        if role == 'viewer' and request.method in ['POST', 'PUT', 'DELETE'] and p != '/api/login':
            return jsonify({'success': False, 'message': 'viewer role: read-only access', 'error_code': 'FORBIDDEN'}), 403
        if request.method in ['POST', 'PUT', 'DELETE']:
            # SECURITY FIX (VULN-003): removed /api/mqtt/ from whitelist
            if p == '/api/login' or p.startswith('/api/env') or p == '/api/password':
                return None
            if role != 'admin' and not _is_status_action(p):
                return jsonify({'success': False, 'message': 'admin required', 'error_code': 'FORBIDDEN'}), 403
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.warning("mutation guard error: %s", e)
        return None

# ── Group exclusivity watchdog ─────────────────────────────────────────────
def _force_group_exclusive(group_id: int, reason: str = "group_exclusive") -> None:
    try:
        group_zones = db.get_zones_by_group(group_id)
        try:
            g = next((gg for gg in (db.get_groups() or []) if int(gg.get('id')) == int(group_id)), None)
            mv_topic = normalize_topic((g.get('master_mqtt_topic') or '').strip()) if g else ''
        except (TypeError, ValueError, StopIteration) as e:
            logger.debug("group exclusive mv_topic lookup: %s", e)
            mv_topic = ''
        def _is_mv(z):
            try: return bool(mv_topic) and normalize_topic((z.get('topic') or '').strip()) == mv_topic
            except (TypeError, ValueError) as e:
                logger.debug("_is_mv check: %s", e)
                return False
        on_zones = [z for z in group_zones if str(z.get('state')) == 'on' and not _is_mv(z)]
        if len(on_zones) <= 1:
            return
        def started_key(z):
            try: return datetime.strptime(z.get('watering_start_time') or '', '%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                logger.debug("started_key parse failed for zone %s", z.get('id'))
                return datetime.min
        on_zones.sort(key=started_key, reverse=True)
        for z in on_zones[1:]:
            try:
                sid = z.get('mqtt_server_id'); topic = (z.get('topic') or '').strip()
                if mqtt and sid and topic:
                    server = db.get_mqtt_server(int(sid))
                    if server: _publish_mqtt_value(server, normalize_topic(topic), '0', min_interval_sec=0.0, qos=2, retain=True)
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.warning("group exclusive mqtt off for zone %s: %s", z.get('id'), e)
            try: db.update_zone(int(z['id']), {'state': 'off', 'watering_start_time': None, 'last_watering_time': z.get('watering_start_time')})
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as e:
                logger.error("group exclusive db update zone %s: %s", z.get('id'), e)
        try: db.add_log('warning', json.dumps({'type': 'group_exclusive_fix', 'group_id': group_id, 'kept_zone': on_zones[0].get('id'), 'turned_off': [z.get('id') for z in on_zones[1:]]}))
        except (sqlite3.Error, json.JSONDecodeError, OSError, ValueError, TypeError, KeyError) as e:
            logger.debug("group exclusive log: %s", e)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Group exclusivity enforcement failed for group {group_id}: {e}")

def _enforce_group_exclusive_all_groups() -> None:
    try:
        zones = db.get_zones()
        zones_by_group = {}
        for z in zones:
            gid = int(z.get('group_id') or 0)
            if gid in (0, 999): continue
            zones_by_group.setdefault(gid, []).append(z)
        for gid, arr in zones_by_group.items():
            try:
                g = next((gg for gg in (db.get_groups() or []) if int(gg.get('id')) == int(gid)), None)
                mv_topic = normalize_topic((g.get('master_mqtt_topic') or '').strip()) if g else ''
            except (TypeError, ValueError, StopIteration) as e:
                logger.debug("enforce_all mv_topic: %s", e)
                mv_topic = ''
            def _is_mv(z):
                try: return bool(mv_topic) and normalize_topic((z.get('topic') or '').strip()) == mv_topic
                except (TypeError, ValueError) as e:
                    logger.debug("_is_mv check (enforce_all): %s", e)
                    return False
            if sum(1 for z in arr if str(z.get('state')) == 'on' and not _is_mv(z)) > 1:
                _force_group_exclusive(gid, 'watchdog')
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.exception("enforce_group_exclusive_all: %s", e)

_WATCHDOG_STARTED = False
_WATCHDOG_STOP_EVENT = threading.Event()

def _start_single_zone_watchdog():
    global _WATCHDOG_STARTED
    if _WATCHDOG_STARTED: return
    _WATCHDOG_STARTED = True
    def _run():
        while not _WATCHDOG_STOP_EVENT.is_set():
            try: _enforce_group_exclusive_all_groups()
            except (ConnectionError, TimeoutError, OSError, sqlite3.Error, ValueError, RuntimeError) as e:  # catch-all: intentional
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
    if app.config.get('TESTING'):
        return None
    p = request.path or ''
    if not p.startswith('/api/') or request.method == 'GET':
        return None
    # Skip paths that have their own decorators (mqtt_control, emergency, programs)
    # or non-mutable paths
    skip_paths = {'/api/login', '/api/password', '/api/status', '/health', '/api/env'}
    if p in skip_paths:
        return None
    # Specific groups already have their own limits applied via decorators
    if '/mqtt/start' in p or '/mqtt/stop' in p:
        return None
    if p in ('/api/emergency-stop', '/api/emergency-resume'):
        return None
    if p.startswith('/api/programs'):
        return None
    ip = request.remote_addr or '0.0.0.0'
    allowed, retry_after = _rate_check(ip, 'general_mutation', 30, 60)
    if not allowed:
        resp = jsonify({
            'success': False,
            'message': 'Too many requests',
            'error_code': 'RATE_LIMITED',
            'retry_after': retry_after,
        })
        resp.status_code = 429
        resp.headers['Retry-After'] = str(retry_after)
        return resp

_mark_zone_stopped = _sse_hub.mark_zone_stopped
_recently_stopped = _sse_hub.recently_stopped

# ── Misc routes ────────────────────────────────────────────────────────────
@app.errorhandler(404)
def _not_found(e):
    try: return render_template('404.html'), 404
    except (OSError, ValueError, RuntimeError) as e:  # catch-all: intentional
        logger.debug("404 template fallback: %s", e)
        return jsonify({'error': 'Not found'}), 404

@app.route('/sw.js')
def service_worker():
    return app.send_static_file('sw.js')

@app.route('/ws')
def ws_stub():
    resp = jsonify({'success': False, 'message': 'WebSocket not supported. Use SSE at /api/mqtt/zones-sse'})
    resp.headers['Cache-Control'] = 'no-store'
    return resp

def _publish_mqtt_async(server, topic, value, min_interval_sec=0.0):
    try:
        threading.Thread(target=lambda: _publish_mqtt_value(server, topic, value, min_interval_sec=min_interval_sec), daemon=True).start()
    except (RuntimeError, OSError) as e:
        logger.warning("_publish_mqtt_async thread start: %s", e)

# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_scheduler(db)
    app.run(debug=True, host='0.0.0.0', port=8080)
