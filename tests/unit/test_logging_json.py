"""Wave 2 F1 — unit tests for WBJsonFormatter (structured JSON logs).

Covers design doc §2.7 acceptance matrix (9 tests):
    1. required fields present
    2. RFC3339 ms timestamp regex
    3. extra={} dict is flattened into top-level fields
    4. reserved LogRecord internals are dropped
    5. exception traceback captured on logger.exception()
    6. correlation_id picked up from ContextVar when set
    7. correlation_id key absent when ContextVar unset (not null)
    8. PII filter still redacts passwords through WBJsonFormatter
    9. TimedRotatingFileHandler still attached after setup_logging()
"""

import contextlib
import json
import logging
import re
from logging.handlers import TimedRotatingFileHandler

from services.logging_setup import PIIFilter, PIIMaskingFilter, WBJsonFormatter, setup_logging


def _format_record(
    formatter,
    name="svc.test",
    level=logging.INFO,
    msg="hello %s",
    args=("world",),
    extra=None,
    exc_info=None,
    func="some_fn",
    lineno=42,
    pathname="/tmp/x.py",
):
    """Build a LogRecord the same way logging.Logger.makeRecord would."""
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=pathname,
        lineno=lineno,
        msg=msg,
        args=args,
        exc_info=exc_info,
        func=func,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return formatter.format(record)


# ── 1. required fields ─────────────────────────────────────────────────────
def test_json_formatter_required_fields():
    fmt = WBJsonFormatter()
    out = _format_record(fmt)
    entry = json.loads(out)
    required = {
        "timestamp",
        "level",
        "logger",
        "message",
        "module",
        "funcName",
        "lineno",
        "v",
        "service",
        "app_version",
    }
    missing = required - set(entry.keys())
    assert not missing, f"missing required fields: {missing}; got {entry}"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "svc.test"
    assert entry["message"] == "hello world"
    assert entry["v"] == 1
    assert entry["service"] == "wb-irrigation"
    assert entry["funcName"] == "some_fn"
    assert entry["lineno"] == 42


# ── 2. RFC 3339 with ms + TZ offset ────────────────────────────────────────
def test_json_formatter_timestamp_rfc3339_ms():
    fmt = WBJsonFormatter()
    entry = json.loads(_format_record(fmt))
    ts = entry["timestamp"]
    # Accept either offset form (+03:00 / -05:00 / +00:00) or literal Z on fallback.
    pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}([+-]\d{2}:\d{2}|Z)$"
    assert re.match(pattern, ts), f"timestamp {ts!r} does not match RFC-3339-ms"


# ── 3. extra={} dict flattened ─────────────────────────────────────────────
def test_json_formatter_includes_extra_dict():
    fmt = WBJsonFormatter()
    out = _format_record(fmt, extra={"zone_id": 5, "command_id": "01HTV0001", "action": "start", "duration": 120})
    entry = json.loads(out)
    assert entry["zone_id"] == 5
    assert entry["command_id"] == "01HTV0001"
    assert entry["action"] == "start"
    assert entry["duration"] == 120


# ── 4. reserved internals dropped ──────────────────────────────────────────
def test_json_formatter_drops_reserved():
    fmt = WBJsonFormatter()
    out = _format_record(fmt, msg="payload %s", args=("x",))
    entry = json.loads(out)
    for k in ("args", "msg", "pathname", "processName", "threadName", "relativeCreated", "exc_text", "stack_info"):
        assert k not in entry, f"reserved key {k!r} leaked into output: {entry}"


# ── 5. exception traceback ─────────────────────────────────────────────────
def test_json_formatter_exception():
    fmt = WBJsonFormatter()
    try:
        1 / 0
    except ZeroDivisionError:
        import sys

        exc_info = sys.exc_info()
    out = _format_record(fmt, level=logging.ERROR, msg="boom", args=(), exc_info=exc_info)
    entry = json.loads(out)
    assert "exc_info" in entry or "exception" in entry or "exc_text" not in entry
    # python-json-logger stores the formatted traceback under 'exc_info' by default.
    traceback = entry.get("exc_info") or entry.get("exception") or ""
    assert "ZeroDivisionError" in traceback, f"traceback missing in {entry}"


# ── 6. correlation_id from ContextVar (when F3 available) ──────────────────
def test_json_formatter_correlation_id_from_contextvar():
    # Create a fake services.correlation module with get_correlation_id().
    import sys
    import types

    mod = types.ModuleType("services.correlation")
    mod.get_correlation_id = lambda: "abc123-def456-789"
    old = sys.modules.get("services.correlation")
    sys.modules["services.correlation"] = mod
    try:
        fmt = WBJsonFormatter()
        entry = json.loads(_format_record(fmt))
        assert entry.get("correlation_id") == "abc123-def456-789"
        assert entry.get("request_id") == "abc123-def456-789"
    finally:
        if old is not None:
            sys.modules["services.correlation"] = old
        else:
            sys.modules.pop("services.correlation", None)


# ── 7. correlation_id absent when unset ────────────────────────────────────
def test_json_formatter_correlation_id_omitted_when_unset():
    import sys

    # Ensure no services.correlation is registered — or it returns None.
    import types

    mod = types.ModuleType("services.correlation")
    mod.get_correlation_id = lambda: None
    old = sys.modules.get("services.correlation")
    sys.modules["services.correlation"] = mod
    try:
        fmt = WBJsonFormatter()
        entry = json.loads(_format_record(fmt))
        assert "correlation_id" not in entry, f"correlation_id should be omitted when unset; got {entry}"
        assert "request_id" not in entry
    finally:
        if old is not None:
            sys.modules["services.correlation"] = old
        else:
            sys.modules.pop("services.correlation", None)


# ── 8. PII filter still redacts ─────────────────────────────────────────────
def test_pii_filter_still_active():
    """Both PII filters (masking + quote-variant) still fire through
    WBJsonFormatter — the formatter does not interfere with filter output.

    This test only verifies the marker is present, proving the filter ran
    through the new formatter; value scrubbing itself is covered by
    test_pii_filters_scrub_secret_values.
    """
    fmt = WBJsonFormatter()

    # PIIMaskingFilter: `password=` becomes `password=[REDACTED]`.
    masking = PIIMaskingFilter()
    rec = logging.LogRecord(
        name="svc",
        level=logging.INFO,
        pathname="/x.py",
        lineno=1,
        msg="auth password=supersecretXY loaded",
        args=(),
        exc_info=None,
        func="f",
    )
    masking.filter(rec)
    out = fmt.format(rec)
    assert "[REDACTED]" in out, f"PIIMaskingFilter did not produce [REDACTED]: {out}"

    # PIIFilter: softer stars mask.  At minimum the *** marker must be present.
    soft = PIIFilter()
    rec2 = logging.LogRecord(
        name="svc",
        level=logging.INFO,
        pathname="/x.py",
        lineno=1,
        msg='payload password="v1" and old_password="v2"',
        args=(),
        exc_info=None,
        func="f",
    )
    soft.filter(rec2)
    out2 = fmt.format(rec2)
    assert "***" in out2, f"PIIFilter did not mask at all: {out2}"


# ── 8a. PII filters must scrub the secret VALUES, not just insert a marker ──
def test_pii_filters_scrub_secret_values():
    """Regression: the secret value itself must be absent from the output.

    Historically both filters only inserted a marker right after the key
    name (``password=[REDACTED]hunter2``) leaving the value in app.log.
    """

    def _mk(msg):
        return logging.LogRecord(
            name="svc",
            level=logging.INFO,
            pathname="/x.py",
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
            func="f",
        )

    masking = PIIMaskingFilter()
    rec = _mk("login password=hunter2 and \"token\":\"abc123\" and 'secret':'qwe456'")
    masking.filter(rec)
    assert "hunter2" not in rec.msg
    assert "abc123" not in rec.msg
    assert "qwe456" not in rec.msg
    assert "password=[REDACTED]" in rec.msg
    assert '"token":"[REDACTED]"' in rec.msg

    soft = PIIFilter()
    rec2 = _mk('payload {"password":"v1secret"} old_password=v2secret Authorization: Bearer tok789')
    soft.filter(rec2)
    assert "v1secret" not in rec2.msg
    assert "v2secret" not in rec2.msg
    assert "tok789" not in rec2.msg
    assert '"password":"***"' in rec2.msg
    assert "old_password=***" in rec2.msg


# ── 8b. PIIFilter must clear record.args after rewriting record.msg ────────
def test_pii_filter_does_not_break_args_formatting():
    """Regression for issue #46: PIIFilter must reset record.args after rewriting record.msg.

    Otherwise WBJsonFormatter calls getMessage() a second time and crashes on
    ``msg % args`` — record.msg has already been substituted (no %s placeholders
    left), but record.args still holds the original tuple. This blocked
    wb-irrigation boot_sync with TypeError in production.
    """
    pii = PIIFilter()
    fmt = WBJsonFormatter()
    rec = logging.LogRecord(
        name="services.zone_control",
        level=logging.INFO,
        pathname="/x.py",
        lineno=1,
        msg="stop_zone called: zone_id=%s reason=%s force=%s",
        args=(24, "boot_sync", True),
        exc_info=None,
        func="stop_zone",
    )
    # PIIFilter rewrites record.msg to the already-formatted string.
    assert pii.filter(rec) is True
    # After PIIFilter, the JSON formatter must not raise on its own getMessage() call.
    out = fmt.format(rec)
    entry = json.loads(out)
    assert "zone_id=24" in entry["message"]
    assert "boot_sync" in entry["message"]


# ── 9. TimedRotatingFileHandler still attached ─────────────────────────────
def test_timed_rotating_handler_still_attached(tmp_path, monkeypatch):
    # Run setup_logging from an isolated cwd so app.log lives under tmp_path.
    monkeypatch.chdir(tmp_path)
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    # Start with only null handler so we can detect the one setup_logging adds.
    root.handlers = [logging.NullHandler()]
    try:
        app_logger = logging.getLogger("app")
        setup_logging(app_logger)
        # At least one TimedRotatingFileHandler for app.log must be present.
        trh = [
            h
            for h in root.handlers
            if isinstance(h, TimedRotatingFileHandler) and getattr(h, "baseFilename", "").endswith("app.log")
        ]
        assert len(trh) == 1, (
            f"expected exactly 1 TimedRotatingFileHandler for app.log, got {len(trh)}: {root.handlers}"
        )
        # And its formatter must be a WBJsonFormatter (or JSONFormatter fallback).
        fmt = trh[0].formatter
        assert fmt is not None
        # When python-json-logger is installed, it's WBJsonFormatter; otherwise the alias.
        assert fmt.__class__.__name__ in ("WBJsonFormatter", "JSONFormatter"), f"unexpected formatter: {type(fmt)}"
    finally:
        # Restore state — close and detach new handlers to avoid test pollution.
        for h in list(root.handlers):
            if h not in saved_handlers:
                with contextlib.suppress(Exception):
                    h.close()
                root.removeHandler(h)
        root.handlers = saved_handlers
        root.setLevel(saved_level)
