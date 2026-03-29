"""Centralized logging configuration."""
import os
import logging
import sqlite3

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
            logger.debug("Handled exception in filter: %s", e)
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
            logger.debug("Handled exception in filter: %s", e)
        return True


_LOG_FORMAT = '%(asctime)s [%(levelname)s] [%(name)s] %(message)s'
_LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'


def ensure_console_handler():
    """Ensure a StreamHandler with unified formatter on root logger."""
    try:
        root = logging.getLogger()
        sh = None
        for h in root.handlers:
            if isinstance(h, logging.StreamHandler):
                sh = h
                break
        if sh is None:
            sh = logging.StreamHandler()
            root.addHandler(sh)
        sh.setLevel(root.level)
        sh.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
        sh.addFilter(PIIFilter())
        wlg = logging.getLogger('werkzeug')
        for h in (wlg.handlers or []):
            if isinstance(h, logging.StreamHandler):
                h.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    except (KeyError, TypeError, ValueError) as e:
        logger.debug("Handled exception in ensure_console_handler: %s", e)


def setup_logging(app_logger):
    """Setup root filters, file handlers, and TZ."""
    logging.basicConfig(level=logging.INFO)

    # Root PII filter
    try:
        root = logging.getLogger()
        has_filter = any(isinstance(f, PIIMaskingFilter) for f in getattr(root, 'filters', []))
        if not has_filter:
            root.addFilter(PIIMaskingFilter())
    except (KeyError, TypeError, ValueError) as e:
        logger.debug("Handled exception in setup_logging: %s", e)

    # Test propagation
    try:
        _IN_TESTS = bool('PYTEST_CURRENT_TEST' in os.environ)
    except (KeyError, TypeError) as e:
        logger.debug("Exception in setup_logging: %s", e)
        _IN_TESTS = False
    app_logger.propagate = not _IN_TESTS

    # File handlers
    try:
        from logging.handlers import TimedRotatingFileHandler
        log_dir = os.path.join(os.getcwd(), 'backups')
        os.makedirs(log_dir, exist_ok=True)
        fh = TimedRotatingFileHandler(os.path.join(log_dir, 'app.log'), when='midnight', interval=1, backupCount=7, encoding='utf-8', utc=False)
        fh.setLevel(logging.INFO)
        fmt = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
        fh.setFormatter(fmt)
        fh.addFilter(PIIFilter())
        app_logger.addHandler(fh)
        imp_logger = logging.getLogger('import_export')
        imp_logger.setLevel(logging.INFO)
        if not any(isinstance(h, TimedRotatingFileHandler) and 'import-export.log' in getattr(h, 'baseFilename', '') for h in imp_logger.handlers):
            imp_fh = TimedRotatingFileHandler(os.path.join(log_dir, 'import-export.log'), when='midnight', interval=1, backupCount=7, encoding='utf-8', utc=False)
            imp_fh.setLevel(logging.INFO)
            imp_fh.setFormatter(fmt)
            imp_logger.addHandler(imp_fh)
    except ImportError as e:
        logger.debug("Handled exception in line_105: %s", e)

    # Set TZ from system timezone
    try:
        import time as _tz_time
        _tz_env = os.getenv('TZ')
        if not _tz_env:
            try:
                with open('/etc/timezone', 'r') as _f:
                    _tz_env = _f.read().strip()
            except (IOError, OSError, PermissionError) as e:
                logger.debug("Exception in line_116: %s", e)
                _tz_env = None
            if _tz_env:
                os.environ['TZ'] = _tz_env
                try:
                    _tz_time.tzset()
                except (IOError, OSError, ValueError) as e:
                    logger.debug("Handled exception in line_123: %s", e)
        try:
            if os.getenv('WB_TZ') != os.getenv('TZ'):
                os.environ['WB_TZ'] = os.getenv('TZ') or ''
        except (KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in line_128: %s", e)
    except (ValueError, TypeError, KeyError, OSError) as e:
        logger.debug("Handled exception in line_130: %s", e)


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
        logger.debug("Handled exception in apply_runtime_log_level: %s", e)
