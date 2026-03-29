"""Database fixtures: isolated test DB for each test."""
import os
import tempfile
import pytest


@pytest.fixture
def test_db_path(tmp_path):
    """Return a fresh temp DB path for each test."""
    return str(tmp_path / "test_irrigation.db")


@pytest.fixture
def test_db(test_db_path):
    """Create a fresh IrrigationDB instance for each test."""
    os.environ['TESTING'] = '1'
    # Import here to avoid circular imports at module load
    from database import IrrigationDB
    db_instance = IrrigationDB(db_path=test_db_path)
    yield db_instance
    # Cleanup: close any lingering connections
    try:
        import sqlite3
        conn = sqlite3.connect(test_db_path, timeout=1)
        conn.close()
    except Exception:
        pass
