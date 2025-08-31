import os
import sys
import sqlite3
import json
import tempfile
import pytest
import threading
import atexit
try:
    from werkzeug.serving import make_server
except Exception:
    make_server = None


os.environ.setdefault("TESTING", "1")

# Ensure project root on path (…/irrigation)
_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import app as app_module  # noqa: E402
import database as database_module  # noqa: E402

# Initialize test DB and start HTTP server EARLY (module import time)
# so that tests reading WB_BASE_URL at import pick it up.
try:
    # Prepare isolated temporary database path
    _TMPDIR = tempfile.mkdtemp(prefix='wb_irrigation_pytest_')
    _TEST_DB_PATH = os.path.join(_TMPDIR, 'irrigation_test.db')
    try:
        database_module.db.db_path = _TEST_DB_PATH
        database_module.db.init_database()
    except Exception:
        from database import IrrigationDB
        _test_db = IrrigationDB(db_path=_TEST_DB_PATH)
        _test_db.init_database()
        database_module.db = _test_db

    # Bind app to test DB and testing mode
    app_module.app.config.update(TESTING=True)
    app_module.db = database_module.db

    # Start in-process HTTP server and set WB_BASE_URL
    if make_server is not None:
        try:
            _SRV = make_server('127.0.0.1', 8080, app_module.app)
        except Exception:
            _SRV = make_server('127.0.0.1', 0, app_module.app)
        _PORT = getattr(_SRV, 'server_port', 8080)
        os.environ['WB_BASE_URL'] = f'http://127.0.0.1:{_PORT}'
        _THREAD = threading.Thread(target=_SRV.serve_forever, daemon=True)
        _THREAD.start()

        def _shutdown_server():
            try:
                _SRV.shutdown()
            except Exception:
                pass

        atexit.register(_shutdown_server)
except Exception:
    # If early init fails, fixtures below will attempt again
    pass


@pytest.fixture(scope='session', autouse=True)
def _isolate_test_database(tmp_path_factory):
    """Route pytest to a temporary DB to protect the live configuration."""
    # Choose a temp DB path unless TEST_DB_PATH is provided
    test_db_path = os.environ.get('TEST_DB_PATH')
    if not test_db_path:
        tmpdir = tmp_path_factory.mktemp('pytest_db')
        test_db_path = str(tmpdir / 'irrigation_test.db')

    # Point global DB to temp path and init
    try:
        database_module.db.db_path = test_db_path
        database_module.db.init_database()
    except Exception:
        from database import IrrigationDB  # local import to avoid circulars
        test_db = IrrigationDB(db_path=test_db_path)
        test_db.init_database()
        database_module.db = test_db

    # Ensure Flask app uses same DB
    app_module.app.config.update(TESTING=True)
    app_module.db = database_module.db

    # Protect against accidental writes to a file named exactly 'irrigation.db'
    os.environ.setdefault('WB_PROTECT_LIVE', '1')

    yield

@pytest.fixture(scope='session', autouse=True)
def _start_http_server(_isolate_test_database):
    # If server already started at import-time, just yield
    if os.environ.get('WB_BASE_URL'):
        yield
        return
    if make_server is None:
        yield
        return
    try:
        srv = make_server('127.0.0.1', 8080, app_module.app)
    except Exception:
        srv = make_server('127.0.0.1', 0, app_module.app)
    port = getattr(srv, 'server_port', 8080)
    os.environ['WB_BASE_URL'] = f'http://127.0.0.1:{port}'
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass

def _reset_seed_data():
    # Seed ONLY the test DB referenced by database_module.db
    target_path = getattr(database_module.db, 'db_path', 'irrigation.db')
    if os.environ.get('WB_PROTECT_LIVE', '1') == '1' and os.path.basename(target_path) == 'irrigation.db':
        # Skip seeding when DB path looks like a live DB
        return
    conn = sqlite3.connect(target_path)
    c = conn.cursor()
    for tbl in ['zones','groups','programs','logs','water_usage','mqtt_servers','settings']:
        try:
            c.execute(f'DELETE FROM {tbl}')
        except Exception:
            pass
    # One group (normalized name)
    c.execute("INSERT INTO groups(id,name) VALUES(1,'Насос-1')")
    # MQTT server
    c.execute("INSERT INTO mqtt_servers(id,name,host,port,enabled) VALUES(1,'local','127.0.0.1',1883,1)")
    # 30 zones, duration 1, topics 101..105/K1..K6
    zones = []
    for zid in range(1,31):
        dev = 101 + (zid-1)//6
        ch = 1 + (zid-1)%6
        topic = f"/devices/wb-mr6cv3_{dev}/controls/K{ch}"
        zones.append((zid,'off',f'Зона {zid}','🌿',1,1,topic,1))
    c.executemany("INSERT INTO zones(id,state,name,icon,duration,group_id,topic,mqtt_server_id) VALUES(?,?,?,?,?,?,?,?)", zones)
    # two programs 04:00 and 20:00 with all zones
    all_z = json.dumps(list(range(1,31)))
    days = json.dumps([0,1,2,3,4,5,6])
    c.execute("INSERT INTO programs(id,name,time,days,zones) VALUES(1,'Утренний','04:00',?,?)", (days, all_z))
    c.execute("INSERT INTO programs(id,name,time,days,zones) VALUES(2,'Вечерний','20:00',?,?)", (days, all_z))
    # password default
    try:
        from werkzeug.security import generate_password_hash
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('password_hash',?)", (generate_password_hash('1234', method='pbkdf2:sha256'),))
    except Exception:
        pass
    conn.commit()
    conn.close()

@pytest.fixture(autouse=True)
def ensure_db():
    # Force initialization by accessing DB
    database_module.db.get_zones()
    _reset_seed_data()
    yield

@pytest.fixture(autouse=True)
def _cleanup_media_after_test():
    # Ensure media directories exist
    from app import UPLOAD_FOLDER, MAP_FOLDER
    try:
        yield
    finally:
        # Remove files created during tests (maps and zone photos)
        for folder in (MAP_FOLDER, UPLOAD_FOLDER):
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
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        yield c
