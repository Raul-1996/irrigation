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

    # Save original modules
    saved_modules = {}
    for mod_name in list(sys.modules.keys()):
        if mod_name in ('database', 'app') or mod_name.startswith('routes.') or mod_name.startswith('services.') or mod_name.startswith('db.'):
            saved_modules[mod_name] = sys.modules.pop(mod_name)

    # Patch database path
    import database as db_mod
    importlib.reload(db_mod)
    # Create a test-specific DB instance
    from database import IrrigationDB
    test_db = IrrigationDB(db_path=test_db_path)

    # Monkey-patch the module-level db
    db_mod.db = test_db

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

    yield flask_app

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
