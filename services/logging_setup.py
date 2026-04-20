"""Centralized logging configuration with structured JSON output.

Wave 2 (F1): replaces the hand-rolled JSONFormatter with WBJsonFormatter
built on python-json-logger. Adds RFC-3339 milliseconds + TZ timestamps,
funcName/lineno, schema version, app_version, and correlation_id lookup
from services.correlation (F3) when available.
"""
import os
import json as _json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

try:
    from pythonjsonlogger import jsonlogger as _jsonlogger
    _HAS_JSONLOGGER = True
except ImportError:  # graceful degradation — fall back to legacy JSONFormatter
    _jsonlogger = None
    _HAS_JSONLOGGER = False

logger = logging.getLogger(__name__)

_APP_VERSION_CACHED: 'str | None' = None


def _get_app_version() -> str:
    """Read VERSION file (cached) — used by WBJsonFormatter for app_version field."""
    global _APP_VERSION_CACHED
    if _APP_VERSION_CACHED is None:
        try:
            vf = Path(__file__).resolve().parent.parent / 'VERSION'
            _APP_VERSION_CACHED = vf.read_text(encoding='utf-8').strip() or 'unknown'
        except (OSError, UnicodeDecodeError):
            _APP_VERSION_CACHED = 'unknown'
    return _APP_VERSION_CACHED


class PIIMaskingFilter(logging.Filter):
    SENSITIVE_KEYS = (
        'authorization', 'password', 'passwd', 'pwd', 'secret', 'token', 'api_key', 'mqtt', 'client_secret'
    )
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = str(record.getMessage())
            for key in self.SENSITIVE_KEYS:
                k = key.lower()
                msg = msg.replace(f"{k}=", f"{k}=[REDACTED]")
                msg = msg.replace(f'"{k}":"', f'"{key}":"[REDACTED]')
                msg = msg.replace(f"'{k}':'", f"'{key}':'[REDACTED]")
            record.msg = msg
            record.args = ()
        except (ValueError, TypeError, KeyError) as e:
            pass  # avoid recursion
        return True


class PIIFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = str(record.getMessage())
            for key in ("password", "old_password", "new_password"):
                msg = msg.replace(f'"{key}":"', f'"{key}":"***').replace(f"{key}=", f"{key}=***")
            if 'Authorization' in msg:
                msg = msg.replace('Authorization', 'Authorization: ***')
            record.msg = msg
        except (ValueError, TypeError, KeyError) as e:
            pass  # avoid recursion
        return True


class JSONFormatter(logging.Formatter):
    """Legacy structured JSON log formatter (kept for backwards compat).

    Output format:
        {"timestamp": "2026-03-29T12:00:00", "level": "WARNING",
         "module": "zone_control", "message": "...", ...extra_fields}

    New code paths in Wave 2 use :class:`WBJsonFormatter`, which extends this
    schema with RFC-3339 milliseconds, funcName/lineno, correlation_id (from F3),
    schema version and app_version fields.
    """

    def format(self, record: logging.LogRecord) -> str:
        try:
            entry = {
                'timestamp': datetime.fromtimestamp(record.created).strftime('%Y-%m-%dT%H:%M:%S'),
                'level': record.levelname,
                'module': record.name,
                'message': record.getMessage(),
            }
            # Attach extra structured fields if set via extra={}
            for key in ('zone_id', 'group_id', 'program_id', 'action', 'topic', 'duration', 'source', 'error'):
                val = getattr(record, key, None)
                if val is not None:
                    entry[key] = val
            # Include exception info
            if record.exc_info and record.exc_info[1]:
                entry['exception'] = self.formatException(record.exc_info)
            return _json.dumps(entry, ensure_ascii=False, default=str)
        except (ValueError, TypeError, KeyError):
            # Fallback to simple format
            return _json.dumps({
                'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
                'level': getattr(record, 'levelname', 'ERROR'),
                'module': getattr(record, 'name', 'unknown'),
                'message': str(getattr(record, 'msg', '')),
            }, ensure_ascii=False)


if _HAS_JSONLOGGER:
    class WBJsonFormatter(_jsonlogger.JsonFormatter):  # type: ignore[misc]
        """Wave 2 (F1) structured JSON formatter built on python-json-logger.

        Schema (always present when applicable):
            timestamp   RFC 3339 with ms + TZ offset, e.g. "2026-04-20T14:22:11.482+03:00"
            level       record.levelname (INFO/WARNING/...)
            logger      record.name
            message     record.getMessage()
            module      record.module
            funcName    record.funcName
            lineno      record.lineno
            v           1  — schema version
            service     "wb-irrigation"
            app_version from VERSION file

        Optional (pass-through from ``extra=`` kwargs):
            correlation_id / request_id  (Feature 3, via services.correlation)
            zone_id / group_id / program_id / command_id
            action / topic / duration / source / error
            exception (on exc_info)
        """

        # Standard LogRecord internals we never want to emit.
        RESERVED = {
            'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename',
            'levelno', 'msecs', 'msg', 'pathname', 'process', 'processName',
            'relativeCreated', 'stack_info', 'thread', 'threadName',
            # Handled explicitly below (renamed) — drop auto-added copies.
            'name', 'levelname',
        }

        def add_fields(self, log_record, record, message_dict):
            super().add_fields(log_record, record, message_dict)

            # RFC 3339 timestamp with milliseconds + TZ offset.
            # astimezone() without arg uses local TZ (respects $TZ env var).
            try:
                ts = datetime.fromtimestamp(record.created).astimezone()
                log_record['timestamp'] = ts.isoformat(timespec='milliseconds')
            except (ValueError, OSError, OverflowError):
                log_record['timestamp'] = datetime.utcnow().isoformat(timespec='milliseconds') + 'Z'

            log_record['level'] = record.levelname
            log_record['logger'] = record.name
            log_record['module'] = record.module
            log_record['funcName'] = record.funcName
            log_record['lineno'] = record.lineno
            log_record['v'] = 1
            log_record['service'] = 'wb-irrigation'
            log_record['app_version'] = _get_app_version()

            # Exception info — emit formatted traceback under 'exception' key.
            if record.exc_info:
                try:
                    log_record['exception'] = self.formatException(record.exc_info)
                except Exception:  # pragma: no cover
                    pass

            # Correlation ID from ContextVar (Feature 3).  Graceful no-op when F3
            # is not merged yet (e.g. during standalone F1 review) or when no
            # request is in flight (CLI jobs, tests).
            try:
                from services.correlation import get_correlation_id  # type: ignore
                cid = get_correlation_id()
                if cid:
                    log_record['correlation_id'] = cid
                    log_record['request_id'] = cid
            except ImportError:
                pass
            except Exception:  # pragma: no cover — never let logging crash callers
                pass

            # Drop reserved internals so output stays compact.
            for k in list(log_record.keys()):
                if k in self.RESERVED:
                    log_record.pop(k, None)
            # Also drop python-json-logger internal helpers that leak through.
            for k in ('taskName', 'exc_info'):
                log_record.pop(k, None)
else:
    # Fallback alias so callers can unconditionally reference WBJsonFormatter.
    WBJsonFormatter = JSONFormatter  # type: ignore[misc,assignment]


_LOG_FORMAT = '%(asctime)s [%(levelname)s] [%(name)s] %(message)s'
_LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'


def _use_json_logging() -> bool:
    """Check if JSON logging is enabled via env var.

    Applies only to the CONSOLE handler.  The file handler is ALWAYS JSON
    so rotated log files remain machine-parseable (jq/Grafana/Loki).
    Owner decision Q6 (Wave 2): console defaults to plain-text in prod
    (journald already wraps stdout; JSON-in-JSON hurts readability).
    """
    return os.environ.get('WB_LOG_FORMAT', 'plain').lower() == 'json'


def ensure_console_handler():
    """Ensure a StreamHandler with unified formatter on root logger."""
    try:
        root = logging.getLogger()
        sh = None
        for h in root.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                sh = h
                break
        if sh is None:
            sh = logging.StreamHandler()
            root.addHandler(sh)
        sh.setLevel(root.level)
        if _use_json_logging():
            sh.setFormatter(WBJsonFormatter())
        else:
            sh.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
        # Ensure PII filter is attached
        if not any(isinstance(f, PIIFilter) for f in sh.filters):
            sh.addFilter(PIIFilter())
        wlg = logging.getLogger('werkzeug')
        for h in (wlg.handlers or []):
            if isinstance(h, logging.StreamHandler):
                if _use_json_logging():
                    h.setFormatter(WBJsonFormatter())
                else:
                    h.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    except (KeyError, TypeError, ValueError) as e:
        pass  # avoid logging recursion


def setup_logging(app_logger):
    """Setup root filters, file handlers, and TZ.

    Fixes (MASTER-C2 / CQ-012):
      * File handler is attached to the ROOT logger (not `app`), so messages from
        `services.*`, `routes.*`, `db.*`, `irrigation_scheduler`, etc. end up in
        `backups/app.log`. Previously the handler was on `logging.getLogger('app')`
        only — any module using `logging.getLogger(__name__)` silently bypassed it.
      * Idempotent: re-calling `setup_logging()` does not duplicate handlers.
      * `force=True` on `basicConfig` guarantees our root level wins over any
        earlier `basicConfig(level=WARNING)` calls performed at import time by
        `irrigation_scheduler.py` / `scheduler/jobs.py` / `database.py`.
    """
    use_json = _use_json_logging()

    # Force-reset root logger level to INFO even if a prior basicConfig
    # (from import-time modules) set it to WARNING.
    logging.basicConfig(level=logging.INFO, force=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Under pytest, remove any root StreamHandler attached to sys.stdout/stderr.
    # Pytest replaces stdout with a buffered captor that closes between tests;
    # background threads (APScheduler, atexit hooks) that log after pytest has
    # finalized the capture will otherwise raise `ValueError: I/O operation on
    # closed file`. File handlers on disk are fine and remain in place.
    try:
        _in_tests = bool('PYTEST_CURRENT_TEST' in os.environ) or os.environ.get('TESTING') == '1'
    except (KeyError, TypeError):
        _in_tests = False
    if _in_tests:
        for _h in list(root.handlers):
            if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logging.FileHandler):
                try:
                    root.removeHandler(_h)
                except (ValueError, TypeError):
                    pass
        # Ensure root still has at least one handler so logging internals don't
        # emit "No handlers could be found" warnings during teardown.
        if not root.handlers:
            root.addHandler(logging.NullHandler())

    # Root PII filter
    try:
        has_filter = any(isinstance(f, PIIMaskingFilter) for f in getattr(root, 'filters', []))
        if not has_filter:
            root.addFilter(PIIMaskingFilter())
    except (KeyError, TypeError, ValueError):
        pass

    # Apply JSON formatter to existing console handlers (only when WB_LOG_FORMAT=json).
    # Otherwise the console keeps plain `asctime [LEVEL] [name] message` per Q6.
    if use_json:
        for h in root.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setFormatter(WBJsonFormatter())

    # Test propagation: in pytest we keep propagate=False on the named app logger
    # to avoid writes to a closed stdout; in prod we keep it True so app-logger
    # records flow up to root where the console handler lives.
    try:
        _IN_TESTS = bool('PYTEST_CURRENT_TEST' in os.environ)
    except (KeyError, TypeError):
        _IN_TESTS = False
    app_logger.propagate = not _IN_TESTS

    # File handlers — attach to ROOT so every logger (services.*, routes.*, etc.)
    # that uses `logging.getLogger(__name__)` writes to app.log via propagation.
    try:
        from logging.handlers import TimedRotatingFileHandler
        log_dir = os.path.join(os.getcwd(), 'backups')
        os.makedirs(log_dir, exist_ok=True)

        app_log_path = os.path.join(log_dir, 'app.log')
        # Idempotence: do not add a second TimedRotatingFileHandler for app.log
        already_attached = any(
            isinstance(h, TimedRotatingFileHandler) and getattr(h, 'baseFilename', '') == os.path.abspath(app_log_path)
            for h in root.handlers
        )
        if not already_attached:
            fh = TimedRotatingFileHandler(app_log_path, when='midnight', interval=1, backupCount=7, encoding='utf-8', utc=False)
            fh.setLevel(logging.INFO)
            # File handler is ALWAYS JSON — rotated files must stay machine-parseable.
            fh.setFormatter(WBJsonFormatter())
            fh.addFilter(PIIFilter())
            root.addHandler(fh)

        # Import/export log — dedicated named logger, keep handler local
        imp_logger = logging.getLogger('import_export')
        imp_logger.setLevel(logging.INFO)
        imp_path = os.path.join(log_dir, 'import-export.log')
        if not any(isinstance(h, TimedRotatingFileHandler) and getattr(h, 'baseFilename', '') == os.path.abspath(imp_path) for h in imp_logger.handlers):
            imp_fh = TimedRotatingFileHandler(imp_path, when='midnight', interval=1, backupCount=7, encoding='utf-8', utc=False)
            imp_fh.setLevel(logging.INFO)
            imp_fh.setFormatter(WBJsonFormatter())
            imp_logger.addHandler(imp_fh)
    except ImportError:
        pass
    except (OSError, PermissionError) as e:
        # Log directory not writable — degrade to console-only, don't crash startup
        try:
            root.warning("app.log file handler not attached: %s", e)
        except (ValueError, TypeError):
            pass

    # Set TZ from system timezone
    try:
        import time as _tz_time
        _tz_env = os.getenv('TZ')
        if not _tz_env:
            try:
                with open('/etc/timezone', 'r') as _f:
                    _tz_env = _f.read().strip()
            except (IOError, OSError, PermissionError):
                _tz_env = None
            if _tz_env:
                os.environ['TZ'] = _tz_env
                try:
                    _tz_time.tzset()
                except (IOError, OSError, ValueError):
                    pass
        try:
            if os.getenv('WB_TZ') != os.getenv('TZ'):
                os.environ['WB_TZ'] = os.getenv('TZ') or ''
        except (KeyError, TypeError, ValueError):
            pass
    except (ValueError, TypeError, KeyError, OSError):
        pass


def apply_runtime_log_level(db):
    """Apply runtime log level from DB setting."""
    try:
        is_debug = db.get_logging_debug()
        level = logging.DEBUG if is_debug else logging.WARNING
        root = logging.getLogger()
        root.setLevel(level)
        ensure_console_handler()
        for lg_name in ('app', 'app', 'apscheduler', 'werkzeug', 'database', 'irrigation_scheduler'):
            lg = logging.getLogger(lg_name)
            lg.setLevel(level if lg_name in ('app', 'database', 'irrigation_scheduler') else (logging.ERROR if not is_debug else logging.INFO))
    except (sqlite3.Error, OSError) as e:
        pass
