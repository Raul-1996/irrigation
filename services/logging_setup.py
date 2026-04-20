"""Centralized logging configuration with structured JSON output."""
import os
import json as _json
import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


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
    """Structured JSON log formatter.

    Output format:
        {"timestamp": "2026-03-29T12:00:00", "level": "WARNING",
         "module": "zone_control", "message": "...", ...extra_fields}
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


_LOG_FORMAT = '%(asctime)s [%(levelname)s] [%(name)s] %(message)s'
_LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'


def _use_json_logging() -> bool:
    """Check if JSON logging is enabled via env var."""
    return os.environ.get('WB_LOG_FORMAT', 'json').lower() == 'json'


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
            sh.setFormatter(JSONFormatter())
        else:
            sh.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
        # Ensure PII filter is attached
        if not any(isinstance(f, PIIFilter) for f in sh.filters):
            sh.addFilter(PIIFilter())
        wlg = logging.getLogger('werkzeug')
        for h in (wlg.handlers or []):
            if isinstance(h, logging.StreamHandler):
                if _use_json_logging():
                    h.setFormatter(JSONFormatter())
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

    # Apply JSON formatter to existing console handlers
    if use_json:
        for h in root.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setFormatter(JSONFormatter())

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
            fh.setFormatter(JSONFormatter())
            fh.addFilter(PIIFilter())
            root.addHandler(fh)

        # Import/export log — dedicated named logger, keep handler local
        imp_logger = logging.getLogger('import_export')
        imp_logger.setLevel(logging.INFO)
        imp_path = os.path.join(log_dir, 'import-export.log')
        if not any(isinstance(h, TimedRotatingFileHandler) and getattr(h, 'baseFilename', '') == os.path.abspath(imp_path) for h in imp_logger.handlers):
            imp_fh = TimedRotatingFileHandler(imp_path, when='midnight', interval=1, backupCount=7, encoding='utf-8', utc=False)
            imp_fh.setLevel(logging.INFO)
            imp_fh.setFormatter(JSONFormatter())
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
