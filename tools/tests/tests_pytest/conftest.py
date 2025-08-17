import os
import sys
import pytest

os.environ.setdefault("TESTING", "1")

# Ensure project root on path (…/irrigation)
_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import app  # noqa: E402
from database import db  # noqa: E402

@pytest.fixture(autouse=True)
def ensure_db():
    # Force initialization by accessing DB
    db.get_zones()
    yield

@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c
