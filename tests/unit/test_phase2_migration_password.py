"""Regression tests for administrator password bootstrap behavior."""

import sqlite3

from werkzeug.security import check_password_hash, generate_password_hash

from db.migrations import MigrationRunner


def _stored_password_hash(db_path):
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("password_hash",),
        ).fetchone()
    return row[0] if row else None


def test_fresh_database_does_not_seed_public_default_password(tmp_path):
    db_path = tmp_path / "irrigation.db"

    MigrationRunner(str(db_path)).init_database()

    assert _stored_password_hash(db_path) is None


def test_restart_with_empty_zones_preserves_existing_admin_password(tmp_path):
    db_path = tmp_path / "irrigation.db"
    runner = MigrationRunner(str(db_path))
    runner.init_database()

    custom_password_hash = generate_password_hash("changed-admin-password", method="pbkdf2:sha256")
    with sqlite3.connect(db_path) as conn:
        zone_count = conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0]
        assert zone_count == 0
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            ("password_hash", custom_password_hash),
        )

    runner.init_database()

    with sqlite3.connect(db_path) as conn:
        stored_hash = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("password_hash",),
        ).fetchone()[0]

    assert stored_hash == custom_password_hash
    assert check_password_hash(stored_hash, "changed-admin-password")
    assert not check_password_hash(stored_hash, "1234")


def test_restart_preserves_preexisting_legacy_default_hash(tmp_path):
    db_path = tmp_path / "irrigation.db"
    runner = MigrationRunner(str(db_path))
    runner.init_database()
    legacy_hash = generate_password_hash("1234", method="pbkdf2:sha256:120000")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            ("password_hash", legacy_hash),
        )

    runner.init_database()

    stored_hash = _stored_password_hash(db_path)
    assert stored_hash == legacy_hash
    assert check_password_hash(stored_hash, "1234")
