"""Wave 2 F2 — unit tests for /metrics endpoint + Prometheus metrics.

Covers design doc §3.4 (Prometheus text exposition) and §9.3
(metric name + label expectations).
"""
import logging
import re
import sys

import pytest


@pytest.fixture
def health_api(client):
    """Return the live routes.health_api module used by the app.

    Mirrors test_health_endpoints.py fixture: the `app` fixture reloads
    routes.* via importlib, so a module-level ``from routes import health_api``
    captures a stale instance whose metric objects are NOT the ones read
    by `/metrics` at request time. Resolve via sys.modules post-reload.
    """
    return sys.modules['routes.health_api']


# ── Endpoint basics ────────────────────────────────────────────────────────

def test_metrics_returns_200_with_text_content_type(client):
    resp = client.get('/metrics')
    assert resp.status_code == 200
    ct = resp.headers.get('Content-Type', '')
    # Prometheus Python client 0.20+ serves text/plain; version=0.0.4; charset=utf-8
    assert ct.startswith('text/plain'), f"unexpected content-type {ct!r}"
    assert 'version=' in ct


def test_metrics_body_is_non_empty_text(client):
    resp = client.get('/metrics')
    body = resp.data.decode('utf-8')
    assert len(body) > 100, f"suspiciously short metrics body: {body!r}"


# ── Required metric names present ──────────────────────────────────────────

REQUIRED_METRICS = [
    'wb_build_info',
    'wb_http_requests_total',
    'wb_http_request_duration_seconds',
    'wb_http_requests_in_flight',
    'wb_process_start_time_seconds',
    'wb_db_query_duration_seconds',
    'wb_mqtt_clients_connected',
    'wb_mqtt_publish_total',
    'wb_scheduler_jobs_total',
    'wb_scheduler_running',
    'wb_zones_total',
    'wb_logging_records_total',
    'wb_readyz_check_status',
    'wb_watchdog_heartbeats_total',
    'wb_zone_start_total',
    'wb_zone_stop_total',
]


def test_metrics_contains_at_least_10_required_metrics(client, health_api):
    """Design §3.4.1 exit criterion: >= 10 metrics populated."""
    # Trigger init_metrics (normally called from app_init, but TESTING=1 skips it).
    from database import db as _db
    health_api.init_metrics(client.application, _db)
    resp = client.get('/metrics')
    body = resp.data.decode('utf-8')
    present = [m for m in REQUIRED_METRICS if re.search(rf'^# HELP {m}\b', body, re.MULTILINE)]
    assert len(present) >= 10, \
        f"only {len(present)} of {len(REQUIRED_METRICS)} metrics present: {present}"


def test_metrics_has_help_and_type_lines(client, health_api):
    """Each metric MUST have a # HELP and a # TYPE line (Prometheus spec)."""
    from database import db as _db
    health_api.init_metrics(client.application, _db)
    resp = client.get('/metrics')
    body = resp.data.decode('utf-8')
    # Sample 3 key metrics.
    for m in ('wb_build_info', 'wb_http_requests_total', 'wb_scheduler_running'):
        assert f'# HELP {m} ' in body, f"missing HELP for {m}"
        assert f'# TYPE {m} ' in body, f"missing TYPE for {m}"


def test_metrics_build_info_has_version_commit_python_labels(client, health_api):
    from database import db as _db
    health_api.init_metrics(client.application, _db)
    resp = client.get('/metrics')
    body = resp.data.decode('utf-8')
    # Format: wb_build_info{commit="...",python_version="...",version="..."} 1.0
    m = re.search(r'^wb_build_info\{([^}]+)\}\s+1\.0\s*$', body, re.MULTILINE)
    assert m, f"wb_build_info line missing or malformed: {body!r}"
    label_str = m.group(1)
    assert 'version=' in label_str
    assert 'commit=' in label_str
    assert 'python_version=' in label_str


def test_metrics_git_commit_env_picked_up(client, monkeypatch, health_api):
    """When GIT_COMMIT env var is set, wb_build_info carries that as commit label."""
    monkeypatch.setenv('GIT_COMMIT', 'deadbeef1234')
    from database import db as _db
    # Re-call init_metrics so the gauge sets the new label combo.
    health_api.init_metrics(client.application, _db)
    resp = client.get('/metrics')
    body = resp.data.decode('utf-8')
    assert 'deadbeef1234' in body, f"GIT_COMMIT not in metrics: {body!r}"


# ── Counter + Histogram semantics ──────────────────────────────────────────

def test_record_request_metrics_increments_counter(client, health_api):
    """record_request_metrics() bumps wb_http_requests_total by one."""
    # Take a snapshot of the counter, then call the helper, compare.
    # prometheus_client exposes internal _value.get() on Counter/Gauge.
    sample = health_api.WB_HTTP_REQUESTS.labels(
        method='GET', endpoint='test_endpoint', status_code='200',
    )
    before = sample._value.get()
    health_api.record_request_metrics('GET', 'test_endpoint', 200, 0.012)
    after = sample._value.get()
    assert after == before + 1


def test_record_request_metrics_observes_histogram(client, health_api):
    """Histogram receives a sample when record_request_metrics is called."""
    # We can't easily read histogram bucket state but we can ensure the call
    # does not raise and that _sum grows monotonically.
    h = health_api.WB_HTTP_DURATION.labels(method='GET', endpoint='probe')
    before_sum = h._sum.get()
    health_api.record_request_metrics('GET', 'probe', 200, 0.250)
    after_sum = h._sum.get()
    assert after_sum >= before_sum + 0.249  # tolerate float noise


def test_in_flight_gauge_increments_and_decrements(client, health_api):
    """GET /healthz through test client should leave in_flight at its baseline."""
    gauge = health_api.WB_HTTP_IN_FLIGHT
    baseline = gauge._value.get()
    r = client.get('/healthz')
    assert r.status_code == 200
    # After the request completes, gauge must return to baseline.
    assert gauge._value.get() == baseline, \
        f"in_flight leaked: baseline={baseline}, now={gauge._value.get()}"


# ── Logging records handler ────────────────────────────────────────────────

def test_log_count_handler_increments_on_log(client, health_api):
    """_LogCountHandler on the root logger increments wb_logging_records_total."""
    # Install handler explicitly (init_metrics would also attach it).
    root = logging.getLogger()
    orig_root_level = root.level
    h = health_api._LogCountHandler()
    h.setLevel(logging.DEBUG)
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    child = logging.getLogger('metrics_test')
    orig_child_level = child.level
    orig_propagate = child.propagate
    child.setLevel(logging.DEBUG)
    child.propagate = True
    try:
        counter = health_api.WB_LOGGING_RECORDS.labels(level='INFO')
        before = counter._value.get()
        child.info('counted')
        # Propagation to root → handler fires.
        after = counter._value.get()
        assert after >= before + 1
    finally:
        root.removeHandler(h)
        root.setLevel(orig_root_level)
        child.setLevel(orig_child_level)
        child.propagate = orig_propagate


# ── /readyz feeds wb_readyz_check_status ───────────────────────────────────

def test_readyz_updates_check_status_gauge(client, health_api):
    """After /readyz is called, wb_readyz_check_status reflects each check."""
    from unittest.mock import patch
    with patch.multiple(
        health_api,
        _check_boot_reconcile=lambda: {'status': 'ok'},
        _check_disk_space=lambda min_free_mb=50: {'status': 'ok', 'free_mb': 100},
        _check_scheduler=lambda: {'status': 'fail', 'reason': 'offline'},
        _check_mqtt=lambda db: {'status': 'skipped'},
        _check_db=lambda db_path='irrigation.db': {'status': 'ok'},
    ):
        client.get('/readyz')
    # scheduler=fail → 0, others ok/skipped → 1
    assert health_api.WB_READYZ_CHECK_STATUS.labels(check='scheduler')._value.get() == 0
    assert health_api.WB_READYZ_CHECK_STATUS.labels(check='boot_reconcile')._value.get() == 1
    assert health_api.WB_READYZ_CHECK_STATUS.labels(check='mqtt')._value.get() == 1


# ── init_metrics seed coverage ─────────────────────────────────────────────

def test_init_metrics_seeds_log_level_labels(client, health_api):
    from database import db as _db
    health_api.init_metrics(client.application, _db)
    resp = client.get('/metrics')
    body = resp.data.decode('utf-8')
    # Each of the 5 log levels must appear as a labeled series.
    for lvl in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
        assert f'wb_logging_records_total{{level="{lvl}"}}' in body, \
            f"level {lvl} not seeded in metrics"
