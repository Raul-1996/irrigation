"""Test configuration — Flask test_client approach, NO real HTTP server.

Key design decisions:
1. Redirect DB to temp file BEFORE importing app
2. Mock paho.mqtt.client.Client at module level BEFORE any app imports
3. Mock StateVerifier to skip real MQTT verification
4. Disable CSRF via TestConfig
5. Seed fresh data before each test
"""
import os
import sys
import sqlite3
import json
import tempfile
import pytest
from unittest.mock import MagicMock

# Set TESTING before any imports
os.environ['TESTING'] = '1'
os.environ['WB_BASE_URL'] = 'http://test'
os.environ['WB_PROTECT_LIVE'] = '0'

# Ensure project root on path
_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── Create temp DB path BEFORE importing anything ──────────────────────────
_TEMP_DB_DIR = tempfile.mkdtemp(prefix='wb_irrigation_test_')
_TEMP_DB_PATH = os.path.join(_TEMP_DB_DIR, 'irrigation_test.db')
os.environ['TEST_DB_PATH'] = _TEMP_DB_PATH

# ── Patch APScheduler before importing anything that uses it ───────────────
import apscheduler.schedulers.background as _real_bg_sched


class _MockBackgroundScheduler:
    """Drop-in BackgroundScheduler that never spawns threads."""

    def __init__(self, *args, **kwargs):
        self._jobs = {}
        self._running = False
        self.timezone = kwargs.get('timezone', None)

    def start(self, paused=False):
        self._running = True

    def shutdown(self, wait=True):
        self._running = False

    def add_job(self, func, trigger=None, **kwargs):
        job_id = kwargs.get('id', f'mock-{len(self._jobs)}')
        mock_job = MagicMock()
        mock_job.id = job_id
        mock_job.next_run_time = None
        self._jobs[job_id] = mock_job
        return mock_job

    def remove_job(self, job_id, jobstore='default'):
        self._jobs.pop(job_id, None)

    def get_jobs(self, jobstore=None):
        return list(self._jobs.values())

    def get_job(self, job_id, jobstore='default'):
        return self._jobs.get(job_id)

    def remove_all_jobs(self, jobstore=None):
        self._jobs.clear()

    def reschedule_job(self, job_id, **kwargs):
        return self._jobs.get(job_id, MagicMock())

    def pause_job(self, job_id, jobstore='default'):
        return self._jobs.get(job_id, MagicMock())

    def resume_job(self, job_id, jobstore='default'):
        return self._jobs.get(job_id, MagicMock())

    @property
    def running(self):
        return self._running


_orig_bg_sched_cls = _real_bg_sched.BackgroundScheduler
_real_bg_sched.BackgroundScheduler = _MockBackgroundScheduler

# Also patch irrigation_scheduler module directly since it uses
# `from apscheduler.schedulers.background import BackgroundScheduler`
# which creates a local reference that survives the module-level patch above.
import irrigation_scheduler as _isched_module
_isched_module.BackgroundScheduler = _MockBackgroundScheduler

# ── Patch MQTT before importing app ────────────────────────────────────────
import paho.mqtt.client as _real_mqtt

_orig_mqtt_client_cls = _real_mqtt.Client


class _MockMQTTClient:
    """Drop-in replacement for paho.mqtt.client.Client that never connects."""

    def __init__(self, *args, **kwargs):
        self._connected = False

    def connect(self, *a, **kw):
        self._connected = True
        return 0

    def disconnect(self, *a, **kw):
        self._connected = False
        return 0

    def publish(self, topic, payload=None, qos=0, retain=False, properties=None):
        m = MagicMock()
        m.rc = 0
        m.mid = 1
        m.is_published.return_value = True
        m.wait_for_publish = MagicMock()
        return m

    def subscribe(self, topic, qos=0, **kw):
        return (0, 1)

    def unsubscribe(self, topic, **kw):
        return (0, 1)

    def loop_start(self):
        pass

    def loop_stop(self, force=False):
        pass

    def loop_forever(self, *a, **kw):
        pass

    def loop(self, timeout=1.0, max_packets=1):
        return 0

    def is_connected(self):
        return self._connected

    def username_pw_set(self, *a, **kw):
        pass

    def tls_set(self, *a, **kw):
        pass

    def reconnect(self):
        return 0

    def enable_logger(self, *a, **kw):
        pass

    def will_set(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


# Monkey-patch paho.mqtt.client.Client globally
_real_mqtt.Client = _MockMQTTClient

# ── Now create isolated DB and import app ──────────────────────────────────
# Create the IrrigationDB with temp path BEFORE importing app module
from database import IrrigationDB
import database as database_module

# Replace the global db singleton with a test instance
_test_db = IrrigationDB(db_path=_TEMP_DB_PATH)
database_module.db = _test_db

# Now import app - it will pick up our patched database_module.db
import app as app_module
app_module.db = database_module.db
app_module.app.db = database_module.db

# ── Mock StateVerifier to skip MQTT verification ──────────────────────────
try:
    from services import observed_state as _obs_mod

    class _NoOpVerifier:
        """StateVerifier replacement that does nothing in tests."""

        def verify(self, zone_id=None, expected=None, timeout=1, retries=1):
            return True

        def verify_async(self, zone_id, expected):
            pass

        def schedule_verify(self, zone_id, expected, delay=0):
            pass

        def _safe_verify(self, zone_id, expected):
            pass

    _noop = _NoOpVerifier()
    _obs_mod.state_verifier = _noop

    try:
        from services import zone_control as _zc_mod
        _zc_mod.state_verifier = _noop
    except (ImportError, AttributeError):
        pass
except (ImportError, AttributeError):
    pass

# Speed up password hashing in tests
try:
    from werkzeug.security import generate_password_hash as _orig_gen_hash
    
    def _fast_generate_hash(password, method='pbkdf2:sha256:1000'):
        """Fast password hashing for tests (1K iterations instead of 260K)."""
        return _orig_gen_hash(password, method)
    
    import werkzeug.security
    werkzeug.security.generate_password_hash = _fast_generate_hash
    
    # Also patch the import in auth_service if it's already imported
    try:
        import services.auth_service as _auth_mod
        if hasattr(_auth_mod, 'generate_password_hash'):
            _auth_mod.generate_password_hash = _fast_generate_hash
    except ImportError:
        pass
except ImportError:
    pass

# Also patch mqtt_pub to clear any cached real clients
try:
    from services import mqtt_pub as _mqtt_pub_mod
    _mqtt_pub_mod._MQTT_CLIENTS = {}
except (ImportError, AttributeError):
    pass


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(scope='session', autouse=True)
def _session_setup():
    """Session-wide setup: ensure app config is correct."""
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    yield
    # Force-stop APScheduler instances that block teardown
    try:
        import irrigation_scheduler as _isched
        if _isched.scheduler and hasattr(_isched.scheduler, 'scheduler'):
            try:
                _isched.scheduler.scheduler.shutdown(wait=False)
            except Exception:
                pass
        _isched.scheduler = None
    except Exception:
        pass

    # Force-stop all daemon threads that might block teardown
    try:
        import services.mqtt_pub as _mp
        _mp._MQTT_CLIENTS.clear()
    except Exception:
        pass

    try:
        import services.watchdog as _wd
        if hasattr(_wd, '_watchdog_instance') and _wd._watchdog_instance:
            _wd._watchdog_instance._stop_event.set()
    except Exception:
        pass

    try:
        import services.sse_hub as _sh
        _sh._SSE_HUB_STARTED = False
        for sid, cl in list(_sh._SSE_HUB_MQTT.items()):
            try:
                cl.loop_stop()
                cl.disconnect()
            except Exception:
                pass
        _sh._SSE_HUB_MQTT.clear()
        _sh._SSE_HUB_CLIENTS.clear()
    except Exception:
        pass

    try:
        import services.telegram_bot as _tb
        # Stop any running bot instances
        for attr in ('_bot_instance', '_updater'):
            inst = getattr(_tb, attr, None)
            if inst and hasattr(inst, '_thread') and inst._thread:
                try:
                    inst._thread = None
                except Exception:
                    pass
    except Exception:
        pass

    try:
        # Force-stop any scheduler that might be running
        import irrigation_scheduler as _sched_mod
        if hasattr(_sched_mod, 'scheduler') and _sched_mod.scheduler:
            try:
                _sched_mod.scheduler.scheduler.shutdown(wait=False)
                _sched_mod.scheduler = None
            except Exception:
                pass
    except Exception:
        pass

    # Restore original classes
    _real_mqtt.Client = _orig_mqtt_client_cls
    _real_bg_sched.BackgroundScheduler = _orig_bg_sched_cls


def _reset_seed_data():
    """Seed fresh data into the test database."""
    target_path = _TEMP_DB_PATH
    conn = sqlite3.connect(target_path)
    c = conn.cursor()
    # Clean all main tables
    for tbl in ['zones', 'groups', 'programs', 'logs', 'water_usage',
                'mqtt_servers', 'settings', 'zone_runs']:
        try:
            c.execute(f'DELETE FROM {tbl}')
        except Exception:
            pass
    # One group
    c.execute("INSERT INTO groups(id, name) VALUES(1, 'Насос-1')")
    # MQTT server
    c.execute("INSERT INTO mqtt_servers(id, name, host, port, enabled) VALUES(1, 'local', '127.0.0.1', 1883, 1)")
    # 30 zones — all fields explicitly set to clean state
    for zid in range(1, 31):
        dev = 101 + (zid - 1) // 6
        ch = 1 + (zid - 1) % 6
        topic = f"/devices/wb-mr6cv3_{dev}/controls/K{ch}"
        c.execute(
            """INSERT INTO zones(id, state, name, icon, duration, group_id, topic, mqtt_server_id,
               postpone_until, postpone_reason, watering_start_time, last_watering_time,
               scheduled_start_time, watering_start_source, commanded_state, observed_state,
               version, fault_count, last_fault)
               VALUES(?, 'off', ?, '🌿', 1, 1, ?, 1,
               NULL, NULL, NULL, NULL,
               NULL, NULL, 'off', NULL,
               0, 0, NULL)""",
            (zid, f'Зона {zid}', topic))
    # Two programs
    all_z = json.dumps(list(range(1, 31)))
    days = json.dumps([0, 1, 2, 3, 4, 5, 6])
    c.execute("INSERT INTO programs(id, name, time, days, zones) VALUES(1, 'Утренний', '04:00', ?, ?)", (days, all_z))
    c.execute("INSERT INTO programs(id, name, time, days, zones) VALUES(2, 'Вечерний', '20:00', ?, ?)", (days, all_z))
    # Password default
    try:
        from werkzeug.security import generate_password_hash
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('password_hash', ?)",
                  (generate_password_hash('1234', method='pbkdf2:sha256'),))
    except Exception:
        pass
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def ensure_db():
    """Seed fresh data before each test."""
    _reset_seed_data()
    yield
    # Clean up any scheduler that may have started during the test
    try:
        import irrigation_scheduler as _sched_mod
        if hasattr(_sched_mod, 'scheduler') and _sched_mod.scheduler:
            try:
                _sched_mod.scheduler.scheduler.shutdown(wait=False)
                _sched_mod.scheduler = None
            except Exception:
                pass
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _cleanup_scheduler_after_test():
    """Shutdown any APScheduler instances created during a test."""
    yield
    try:
        import irrigation_scheduler as _isched
        if _isched.scheduler and hasattr(_isched.scheduler, 'scheduler'):
            try:
                _isched.scheduler.scheduler.shutdown(wait=False)
            except Exception:
                pass
            _isched.scheduler.is_running = False
        _isched.scheduler = None
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _cleanup_media_after_test():
    """Ensure media directories exist and clean up after tests."""
    try:
        from services.helpers import UPLOAD_FOLDER, MAP_DIR
    except ImportError:
        yield
        return
    try:
        yield
    finally:
        for folder in (MAP_DIR, UPLOAD_FOLDER):
            try:
                for name in os.listdir(folder):
                    path = os.path.join(folder, name)
                    try:
                        if os.path.isfile(path):
                            os.remove(path)
                    except Exception:
                        pass
            except Exception:
                pass


@pytest.fixture()
def client():
    """Flask test client — NO real server needed."""
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        yield c
