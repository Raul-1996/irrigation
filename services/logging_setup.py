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
    """Setup root filters, file handlers, and TZ."""
    logging.basicConfig(level=logging.INFO)

    use_json = _use_json_logging()

    # Root PII filter
    try:
        root = logging.getLogger()
        has_filter = any(isinstance(f, PIIMaskingFilter) for f in getattr(root, 'filters', []))
        if not has_filter:
            root.addFilter(PIIMaskingFilter())
    except (KeyError, TypeError, ValueError) as e:
        pass

    # Apply JSON formatter to existing console handlers
    if use_json:
        root = logging.getLogger()
        for h in root.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setFormatter(JSONFormatter())

    # Test propagation
    try:
        _IN_TESTS = bool('PYTEST_CURRENT_TEST' in os.environ)
    except (KeyError, TypeError) as e:
        _IN_TESTS = False
    app_logger.propagate = not _IN_TESTS

    # File handlers
    try:
        from logging.handlers import TimedRotatingFileHandler
        log_dir = os.path.join(os.getcwd(), 'backups')
        os.makedirs(log_dir, exist_ok=True)

        # Main app log — always JSON
        fh = TimedRotatingFileHandler(os.path.join(log_dir, 'app.log'), when='midnight', interval=1, backupCount=7, encoding='utf-8', utc=False)
        fh.setLevel(logging.INFO)
        fh.setFormatter(JSONFormatter())
        fh.addFilter(PIIFilter())
        app_logger.addHandler(fh)

        # Import/export log
        imp_logger = logging.getLogger('import_export')
        imp_logger.setLevel(logging.INFO)
        if not any(isinstance(h, TimedRotatingFileHandler) and 'import-export.log' in getattr(h, 'baseFilename', '') for h in imp_logger.handlers):
            imp_fh = TimedRotatingFileHandler(os.path.join(log_dir, 'import-export.log'), when='midnight', interval=1, backupCount=7, encoding='utf-8', utc=False)
            imp_fh.setLevel(logging.INFO)
            imp_fh.setFormatter(JSONFormatter())
            imp_logger.addHandler(imp_fh)
    except ImportError as e:
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
