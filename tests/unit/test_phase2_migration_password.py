"""Regression tests for administrator password bootstrap behavior."""

import sqlite3

from werkzeug.security import check_password_hash, generate_password_hash

from db.migrations import MigrationRunner


def test_restart_with_empty_zones_preserves_existing_admin_password(tmp_path):
    db_path = tmp_path / "irrigation.db"
    runner = MigrationRunner(str(db_path))
    runner.init_database()

    custom_password_hash = generate_password_hash("changed-admin-password", method="pbkdf2:sha256")
    with sqlite3.connect(db_path) as conn:
        zone_count = conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0]
        assert zone_count == 0
        default_hash = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("password_hash",),
        ).fetchone()[0]
        assert check_password_hash(default_hash, "1234")
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
