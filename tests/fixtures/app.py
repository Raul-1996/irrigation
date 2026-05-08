"""Flask test app and client fixtures."""
import os
import sys
import pytest


@pytest.fixture
def app(test_db_path):
    """Create a Flask test app with isolated DB."""
    os.environ['TESTING'] = '1'
    os.environ['SECRET_KEY'] = 'test-secret-key-for-testing-only'

    # We need to reload the database module with the test DB path
    # The simplest approach: patch the db_path before importing app
    import importlib

    # Reload-isolation strategy:
    #   We need to swap the singleton `database.db` for a per-test instance.
    #   Previously this fixture pop()-ed and reload()-ed *every* services./routes./db.
    #   submodule, then restored the saved copies on teardown.  That created two
    #   parallel module objects in memory: one referenced by already-imported
    #   names (e.g. `from services.zone_control import exclusive_start_zone` in
    #   another integration test), and one in sys.modules.  Subsequent tests
    #   that did `with patch('services.zone_control.db', test_db)` patched the
    #   sys.modules copy, but the cached function reference still resolved
    #   `db` against the *other* (un-patched) module — so writes silently
    #   went to the wrong DB.  See test_full_watering_cycle order-dependent
    #   regression.
    #
    #   Fix: only reload `database` and `app` (the modules whose top-level
    #   side-effects we actually care about — DB instantiation + Flask blueprint
    #   wiring).  Leave services.*, routes.*, and db.* alone — they read the
    #   patched `database.db` lazily, which keeps both pre-imported and post-
    #   imported references pointing at one consistent module object.
    saved_modules = {}
    for mod_name in list(sys.modules.keys()):
        if mod_name in ('database', 'app'):
            saved_modules[mod_name] = sys.modules.pop(mod_name)

    # Patch database path
    import database as db_mod
    # Create a test-specific DB instance
    from database import IrrigationDB
    test_db = IrrigationDB(db_path=test_db_path)

    # Monkey-patch the module-level db
    db_mod.db = test_db

    # Cross-module db rebind:
    #   `from database import db` (in 22+ routes/services modules) binds `db`
    #   into THAT module's namespace at import-time.  When a previous test ran,
    #   it set `database.db = test_db_a` BEFORE those modules were first
    #   imported, so they captured `test_db_a`.  Now in this test we set
    #   `database.db = test_db_b` — but already-loaded route/service modules
    #   STILL hold a reference to `test_db_a` because that bind happened at
    #   import time and Python doesn't auto-rebind `from-import` names when
    #   the source module's attribute changes later.
    #
    #   Fix: walk all loaded modules and rebind any `db` attribute that
    #   *was* an IrrigationDB instance to the new test_db.  Restricted to
    #   project modules (database, services.*, routes.*, irrigation_scheduler,
    #   scheduler.*, db.*) to avoid touching unrelated third-party modules.
    _PROJECT_PREFIXES = (
        'database', 'irrigation_scheduler', 'app',
        'services', 'services.', 'routes', 'routes.',
        'scheduler', 'scheduler.', 'db', 'db.',
    )
    # NOTE: we cannot use isinstance(cur_db, IrrigationDB) because the previous
    # test loaded a DIFFERENT IrrigationDB class (we popped+reloaded `database`
    # at fixture entry, so there are two distinct class objects in memory).
    # Instead we duck-type: rebind any module attribute named `db` that has the
    # IrrigationDB-shaped contract (db_path + get_zone callable).
    _rebound = []
    for mod_name, mod in list(sys.modules.items()):
        if not isinstance(mod_name, str):
            continue
        if not (mod_name in _PROJECT_PREFIXES or mod_name.startswith(('services.', 'routes.', 'scheduler.', 'db.'))):
            continue
        try:
            cur_db = getattr(mod, 'db', None)
            if cur_db is None or cur_db is test_db:
                continue
            # Duck-type IrrigationDB: has db_path str attribute and get_zone callable.
            if hasattr(cur_db, 'db_path') and callable(getattr(cur_db, 'get_zone', None)):
                mod.db = test_db
                _rebound.append(mod_name)
        except (AttributeError, TypeError):
            continue
    if os.environ.get('DEBUG_FIXTURE'):
        print(f"[FIXTURE] rebound {len(_rebound)} modules")

    # Now import and configure app
    try:
        import app as app_mod
        importlib.reload(app_mod)
        flask_app = app_mod.app
    except (ImportError, AttributeError, RuntimeError):
        # If app import fails, create a minimal Flask app
        from flask import Flask
        flask_app = Flask(__name__)
        flask_app.config['TESTING'] = True

    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = False
    flask_app.config['SECRET_KEY'] = 'test-secret-key-for-testing-only'
    flask_app.db = test_db

    # Reset the scheduler singleton so this test's view of `irrigation_scheduler.scheduler`
    # is not a stale reference inherited from the previous test (which still points
    # at the previous test's `test_db`).  Without this, endpoints that lazily call
    # `init_scheduler(db)` short-circuit on the existing global and end up writing
    # to the old DB — producing 400/500s only when tests run in a particular order
    # (e.g. test_group_stop → test_group_start_with_zones).
    try:
        import irrigation_scheduler as _is_mod
        prev_scheduler = getattr(_is_mod, 'scheduler', None)
        if prev_scheduler is not None:
            try:
                if hasattr(prev_scheduler, 'scheduler') and prev_scheduler.scheduler:
                    prev_scheduler.scheduler.shutdown(wait=False)
            except Exception:
                pass
            _is_mod.scheduler = None
    except (ImportError, AttributeError):
        pass

    yield flask_app

    # Reset scheduler again on teardown so the next test starts clean.
    try:
        import irrigation_scheduler as _is_mod
        cur = getattr(_is_mod, 'scheduler', None)
        if cur is not None:
            try:
                if hasattr(cur, 'scheduler') and cur.scheduler:
                    cur.scheduler.shutdown(wait=False)
            except Exception:
                pass
            _is_mod.scheduler = None
    except (ImportError, AttributeError):
        pass

    # Restore modules
    for mod_name, mod in saved_modules.items():
        sys.modules[mod_name] = mod


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture
def admin_client(app):
    """Flask test client logged in as admin."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['logged_in'] = True
        sess['role'] = 'admin'
    return client


@pytest.fixture
def viewer_client(app):
    """Flask test client logged in as viewer (read-only)."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['logged_in'] = True
        sess['role'] = 'viewer'
    return client


@pytest.fixture
def guest_client(app):
    """Flask test client with guest role."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['role'] = 'guest'
    return client
