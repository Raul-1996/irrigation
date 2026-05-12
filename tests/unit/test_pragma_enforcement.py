"""Wave 3 PRAGMA enforcement tests.

Validates that BaseRepository._connect() applies the full PRAGMA
contract uniformly across every repository subclass:

    PRAGMA journal_mode = WAL
    PRAGMA foreign_keys = ON
    PRAGMA busy_timeout = 30000

These PRAGMAs were previously scattered / partially applied at raw
sqlite3.connect() call sites, leading to inconsistent behaviour
(e.g. FK enforcement silently disabled on some write paths).

The FK-integrity test for zones.group_id is marked xfail/skip
because the current `zones` schema declares `group_id INTEGER
DEFAULT 1` with no `REFERENCES groups(id)` clause — see
db/migrations.py. Adding the FK requires the `add_foreign_keys_v2`
migration described in irrigation-audit/architecture/target-state.md
(zones_new rebuild). Logged as future work.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile

import pytest

from db.base import BaseRepository
from db.float import FloatRepository
from db.groups import GroupRepository
from db.zones import ZoneRepository


@pytest.fixture
def tmp_db_path():
    """Return a path to a throwaway sqlite file; clean up after test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # Init minimal schema so row_factory + fetch paths work.
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS zones (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            group_id INTEGER DEFAULT 1
        );
        """
    )
    conn.commit()
    conn.close()
    try:
        yield path
    finally:
        for ext in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(path + ext)


# ---------------------------------------------------------------------------
# Core PRAGMA contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repo_cls",
    [BaseRepository, ZoneRepository, GroupRepository, FloatRepository],
)
def test_foreign_keys_enabled(tmp_db_path, repo_cls):
    """Every repo's _connect() must set PRAGMA foreign_keys=ON (=1)."""
    repo = repo_cls(tmp_db_path)
    with repo._connect() as conn:
        value = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert value == 1, f"{repo_cls.__name__}: foreign_keys should be 1, got {value}"


@pytest.mark.parametrize(
    "repo_cls",
    [BaseRepository, ZoneRepository, GroupRepository, FloatRepository],
)
def test_busy_timeout_30000(tmp_db_path, repo_cls):
    """Every repo's _connect() must set busy_timeout=30000 (30s)."""
    repo = repo_cls(tmp_db_path)
    with repo._connect() as conn:
        value = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert value == 30000, f"{repo_cls.__name__}: busy_timeout should be 30000, got {value}"


@pytest.mark.parametrize(
    "repo_cls",
    [BaseRepository, ZoneRepository, GroupRepository, FloatRepository],
)
def test_journal_mode_wal(tmp_db_path, repo_cls):
    """Every repo's _connect() must put the DB in WAL journal mode."""
    repo = repo_cls(tmp_db_path)
    with repo._connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal", f"{repo_cls.__name__}: journal_mode should be wal, got {mode!r}"


# ---------------------------------------------------------------------------
# FK enforcement (bonus, skipped due to schema)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "zones.group_id has no FK declaration in current schema "
        "(db/migrations.py: `group_id INTEGER DEFAULT 1`, no REFERENCES). "
        "Adding the FK requires the add_foreign_keys_v2 migration — "
        "see irrigation-audit/architecture/target-state.md. Future work."
    )
)
def test_zones_group_id_fk_integrity_future(tmp_db_path):
    """Insert zone with nonexistent group_id → expect IntegrityError.

    Will start failing (correctly) once the add_foreign_keys_v2
    migration is applied; that's the signal to unskip this test.
    """
    repo = ZoneRepository(tmp_db_path)
    with repo._connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO zones (id, name, group_id) VALUES (?, ?, ?)",
            (999, "ghost-zone", 424242),
        )
        conn.commit()
