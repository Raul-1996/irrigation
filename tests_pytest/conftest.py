import os
import sys
import pytest

os.environ.setdefault("TESTING", "1")

# Ensure root on path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

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
