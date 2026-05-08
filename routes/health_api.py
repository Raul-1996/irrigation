"""Wave 2 F2 — observability endpoints: /healthz, /readyz, /metrics.

Liveness, readiness, and Prometheus metrics for SRE / monitoring.

Endpoint contract:
  * GET /healthz — liveness (200 if Flask event loop is not wedged).
  * GET /readyz  — readiness (aggregates 5 checks; 200 all-ok / 503 any-fail).
  * GET /metrics — Prometheus text exposition (dedicated CollectorRegistry).

Design doc: irrigation-audit/design/wave2-observability-design.md §3.
All three endpoints are GET-only, do not require session auth, and live
under a dedicated blueprint so they are trivially CSRF-exempt.

Future hardening (Wave 3, MASTER-M5): nginx IP allow-list for /metrics.
"""
from __future__ import annotations

import logging
import os
import platform
import sqlite3
import time
from typing import Any, Callable, Dict, List, Tuple

from flask import Blueprint, Response, jsonify

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from services.version import get_app_version as _get_app_version

logger = logging.getLogger(__name__)

health_api_bp = Blueprint('health_api', __name__)


# ── Prometheus registry & metrics ──────────────────────────────────────────
# Dedicated registry (not the global default) so process/GC collectors are
# opt-in and don't pollute our scrape output.
REGISTRY = CollectorRegistry()

WB_BUILD_INFO = Gauge(
    'wb_build_info',
    'Build info (value is always 1; labels carry version/commit metadata)',
    ['version', 'commit', 'python_version'],
    registry=REGISTRY,
)

WB_HTTP_REQUESTS = Counter(
    'wb_http_requests_total',
    'Total HTTP requests processed, labelled by method/endpoint/status_code',
    ['method', 'endpoint', 'status_code'],
    registry=REGISTRY,
)

WB_HTTP_DURATION = Histogram(
    'wb_http_request_duration_seconds',
    'HTTP request latency in seconds, labelled by method/endpoint',
    ['method', 'endpoint'],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

WB_HTTP_IN_FLIGHT = Gauge(
    'wb_http_requests_in_flight',
    'In-flight HTTP requests at any instant',
    registry=REGISTRY,
)

WB_PROCESS_START_TIME = Gauge(
    'wb_process_start_time_seconds',
    'Unix timestamp when the Flask process started',
    registry=REGISTRY,
)

WB_DB_QUERY_DURATION = Histogram(
    'wb_db_query_duration_seconds',
    'SQLite query duration in seconds, labelled by operation (read/write)',
    ['operation'],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
    registry=REGISTRY,
)

WB_MQTT_CLIENTS_CONNECTED = Gauge(
    'wb_mqtt_clients_connected',
    'Number of configured MQTT broker clients currently connected',
    registry=REGISTRY,
)

WB_MQTT_PUBLISH = Counter(
    'wb_mqtt_publish_total',
    'Total MQTT publish attempts, labelled by result (ok/fail)',
    ['result'],
    registry=REGISTRY,
)

WB_SCHEDULER_JOBS = Gauge(
    'wb_scheduler_jobs_total',
    'Number of scheduled jobs currently registered in APScheduler',
    registry=REGISTRY,
)

WB_SCHEDULER_RUNNING = Gauge(
    'wb_scheduler_running',
    'APScheduler running state (1=running, 0=stopped)',
    registry=REGISTRY,
)

WB_ZONES_TOTAL = Gauge(
    'wb_zones_total',
    'Number of zones by state (on/off) — populated on every /metrics scrape',
    ['state'],
    registry=REGISTRY,
)

WB_LOGGING_RECORDS = Counter(
    'wb_logging_records_total',
    'Count of log records emitted, labelled by level',
    ['level'],
    registry=REGISTRY,
)

WB_READYZ_CHECK_STATUS = Gauge(
    'wb_readyz_check_status',
    'Last /readyz check result per check (1=ok, 0=fail)',
    ['check'],
    registry=REGISTRY,
)

WB_WATCHDOG_HEARTBEATS = Counter(
    'wb_watchdog_heartbeats_total',
    'systemd sd_notify WATCHDOG=1 heartbeats sent (populated by F4)',
    registry=REGISTRY,
)

WB_ZONE_START = Counter(
    'wb_zone_start_total',
    'Zone start events, labelled by source (manual/scheduler/program/other)',
    ['source'],
    registry=REGISTRY,
)

WB_ZONE_STOP = Counter(
    'wb_zone_stop_total',
    'Zone stop events, labelled by source (manual/scheduler/program/other)',
    ['source'],
    registry=REGISTRY,
)


# ── Log-count handler: feeds wb_logging_records_total ──────────────────────
class _LogCountHandler(logging.Handler):
    """A logging.Handler that never formats — it just increments the
    wb_logging_records_total counter by level.  Attached to the root logger
    from :func:`init_metrics`.
    """

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        try:
            WB_LOGGING_RECORDS.labels(level=record.levelname).inc()
        except Exception:
            # Never let a metrics handler crash caller.
            pass


_LOG_COUNT_HANDLER_ATTACHED = False


# ── init_metrics — called once at app startup ──────────────────────────────

def init_metrics(app, db) -> None:
    """Populate one-shot gauges and install the log-count handler.

    Called from :func:`services.app_init.initialize_app` after _boot_sync.
    Safe to call multiple times: the log handler attachment is idempotent.
    """
    global _LOG_COUNT_HANDLER_ATTACHED
    try:
        version = app.config.get('APP_VERSION') or _get_app_version()
    except Exception:
        version = 'unknown'
    commit = os.environ.get('GIT_COMMIT', 'unknown')
    WB_BUILD_INFO.labels(
        version=version,
        commit=commit,
        python_version=platform.python_version(),
    ).set(1)
    WB_PROCESS_START_TIME.set(int(time.time()))

    # Seed log-level counters at 0 so they appear in /metrics from the first scrape.
    for lvl in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
        WB_LOGGING_RECORDS.labels(level=lvl)

    # Seed other label combinations so a fresh scrape shows them at 0.
    for r in ('ok', 'fail'):
        WB_MQTT_PUBLISH.labels(result=r)
    for s in ('manual', 'scheduler', 'program', 'other'):
        WB_ZONE_START.labels(source=s)
        WB_ZONE_STOP.labels(source=s)
    for st in ('on', 'off'):
        WB_ZONES_TOTAL.labels(state=st)

    # Install log-count handler on the ROOT logger (idempotent).
    if not _LOG_COUNT_HANDLER_ATTACHED:
        root = logging.getLogger()
        if not any(isinstance(h, _LogCountHandler) for h in root.handlers):
            root.addHandler(_LogCountHandler())
        _LOG_COUNT_HANDLER_ATTACHED = True

    logger.info('observability: init_metrics completed (version=%s commit=%s)', version, commit)


# ── Readiness checks ───────────────────────────────────────────────────────
# Each check returns a dict with at least 'status' ∈ {'ok','fail','skipped'}
# plus optional 'duration_ms', 'reason', and extra metadata.


def _check_boot_reconcile() -> Dict[str, Any]:
    try:
        from services import app_init
        done = bool(getattr(app_init, '_boot_sync_done', False))
    except ImportError:
        return {'status': 'fail', 'reason': 'services.app_init import failed'}
    if done:
        return {'status': 'ok'}
    return {'status': 'fail', 'reason': 'boot_sync not completed'}


def _check_disk_space(min_free_mb: int = 50) -> Dict[str, Any]:
    try:
        st = os.statvfs(os.getcwd())
    except (OSError, AttributeError) as e:
        return {'status': 'fail', 'reason': f'statvfs: {e}'}
    free_bytes = st.f_bavail * st.f_frsize
    free_mb = int(free_bytes // (1024 * 1024))
    if free_mb < min_free_mb:
        return {'status': 'fail', 'free_mb': free_mb,
                'reason': f'free {free_mb} MB < required {min_free_mb} MB'}
    return {'status': 'ok', 'free_mb': free_mb}


def _check_scheduler() -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        from irrigation_scheduler import get_scheduler
    except ImportError as e:
        return {'status': 'fail', 'reason': f'scheduler import: {e}',
                'duration_ms': int((time.perf_counter() - t0) * 1000)}
    sched = None
    try:
        sched = get_scheduler()
    except Exception as e:
        return {'status': 'fail', 'reason': f'get_scheduler: {e}',
                'duration_ms': int((time.perf_counter() - t0) * 1000)}
    dur_ms = int((time.perf_counter() - t0) * 1000)
    if sched is None:
        return {'status': 'fail', 'duration_ms': dur_ms,
                'reason': 'scheduler not initialised'}
    running = bool(getattr(sched, 'is_running', False))
    return {
        'status': 'ok' if running else 'fail',
        'duration_ms': dur_ms,
        'reason': None if running else 'scheduler.is_running is False',
    }


def _check_mqtt(db) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        servers = db.get_mqtt_servers() or []
    except (sqlite3.Error, OSError, AttributeError) as e:
        return {'status': 'fail', 'reason': f'db.get_mqtt_servers: {e}',
                'duration_ms': int((time.perf_counter() - t0) * 1000)}
    if not servers:
        # Fresh install / not yet configured — don't penalise.
        return {'status': 'skipped', 'reason': 'no brokers configured',
                'duration_ms': int((time.perf_counter() - t0) * 1000),
                'brokers': 0}
    try:
        from services.mqtt_pub import _MQTT_CLIENTS
    except ImportError as e:
        return {'status': 'fail', 'reason': f'mqtt_pub import: {e}',
                'duration_ms': int((time.perf_counter() - t0) * 1000)}
    connected = 0
    for cl in list(_MQTT_CLIENTS.values()):
        try:
            if cl is not None and bool(cl.is_connected()):
                connected += 1
        except Exception:
            pass
    dur_ms = int((time.perf_counter() - t0) * 1000)
    return {
        'status': 'ok' if connected >= 1 else 'fail',
        'duration_ms': dur_ms,
        'brokers': len(servers),
        'connected': connected,
        'reason': None if connected >= 1 else 'no broker client reports is_connected()',
    }


def _check_db(db_path: str = 'irrigation.db') -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            conn.execute('SELECT 1').fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as e:
        return {
            'status': 'fail',
            'duration_ms': int((time.perf_counter() - t0) * 1000),
            'reason': type(e).__name__,
        }
    return {
        'status': 'ok',
        'duration_ms': int((time.perf_counter() - t0) * 1000),
    }


# Registry of checks run on every /readyz call (cheap → slow).
def _readiness_checks(db) -> List[Tuple[str, Callable[[], Dict[str, Any]]]]:
    return [
        ('boot_reconcile', _check_boot_reconcile),
        ('disk_space', _check_disk_space),
        ('scheduler', _check_scheduler),
        ('mqtt', lambda: _check_mqtt(db)),
        ('db', _check_db),
    ]


# ── Endpoints ──────────────────────────────────────────────────────────────

@health_api_bp.route('/healthz', methods=['GET'])
def healthz() -> Tuple[Dict[str, str], int]:
    """Liveness probe.  200 iff the Flask event loop is alive enough to answer.

    Does NOT touch DB / MQTT / scheduler — those failures are handled by
    /readyz.  If this endpoint stops responding, the systemd watchdog (F4)
    will kill the process.
    """
    return {'status': 'ok'}, 200


@health_api_bp.route('/readyz', methods=['GET'])
def readyz() -> Response:
    """Readiness probe.  Aggregates all checks; 200 all-ok / 503 any-fail.

    Runs every check (no short-circuit) so operators see the full picture
    on failure.
    """
    from flask import current_app
    db = getattr(current_app, 'db', None)
    results: Dict[str, Dict[str, Any]] = {}
    for name, fn in _readiness_checks(db):
        try:
            res = fn()
        except Exception as e:  # pragma: no cover — defensive
            logger.exception('readyz check %s crashed', name)
            res = {'status': 'fail', 'reason': f'crash: {type(e).__name__}'}
        results[name] = res
        # Feed wb_readyz_check_status gauge (1=ok/skipped, 0=fail).
        val = 1 if res.get('status') in ('ok', 'skipped') else 0
        try:
            WB_READYZ_CHECK_STATUS.labels(check=name).set(val)
        except Exception:
            pass

    all_ok = all(r.get('status') in ('ok', 'skipped') for r in results.values())
    payload = {
        'status': 'ok' if all_ok else 'fail',
        'checks': results,
    }
    resp = jsonify(payload)
    resp.status_code = 200 if all_ok else 503
    return resp


@health_api_bp.route('/metrics', methods=['GET'])
def metrics() -> Response:
    """Prometheus text exposition.

    Populates scrape-time gauges (scheduler jobs/running, zones on/off,
    MQTT connected) just before rendering so consumers see current state
    without a background thread.

    Wave 2: no auth.  Wave 3 (MASTER-M5): nginx IP allow-list.
    """
    from flask import current_app
    db = getattr(current_app, 'db', None)

    # Scheduler lazy gauges
    try:
        from irrigation_scheduler import get_scheduler
        sched = get_scheduler()
        WB_SCHEDULER_JOBS.set(len(sched.get_jobs()) if sched else 0)
        WB_SCHEDULER_RUNNING.set(1 if (sched and getattr(sched, 'running', False)) else 0)
    except Exception as e:
        logger.debug('metrics scheduler snapshot: %s', e)

    # Zones gauges
    try:
        if db is not None:
            zones = db.get_zones() or []
            on = sum(1 for z in zones if str(z.get('state')) == 'on')
            WB_ZONES_TOTAL.labels(state='on').set(on)
            WB_ZONES_TOTAL.labels(state='off').set(len(zones) - on)
    except Exception as e:
        logger.debug('metrics zones snapshot: %s', e)

    # MQTT clients connected gauge
    try:
        from services.mqtt_pub import _MQTT_CLIENTS
        connected = 0
        for cl in list(_MQTT_CLIENTS.values()):
            try:
                if cl is not None and bool(cl.is_connected()):
                    connected += 1
            except Exception:
                pass
        WB_MQTT_CLIENTS_CONNECTED.set(connected)
    except Exception as e:
        logger.debug('metrics mqtt snapshot: %s', e)

    body = generate_latest(REGISTRY)
    return Response(body, status=200, content_type=CONTENT_TYPE_LATEST)


# ── Helpers exposed for app.py middleware & test fixtures ──────────────────

def record_request_metrics(method: str, endpoint: str, status_code: int, duration_s: float) -> None:
    """Called from app.after_request to increment per-request metrics.

    Keeps cardinality bounded by using Flask's blueprint.view_function
    endpoint name (not request.path) and falling back to "unknown" for 404s.
    """
    ep = endpoint or 'unknown'
    try:
        WB_HTTP_REQUESTS.labels(method=method, endpoint=ep, status_code=str(status_code)).inc()
        WB_HTTP_DURATION.labels(method=method, endpoint=ep).observe(duration_s)
    except Exception:
        pass
