"""Issue #2 — backfill_last_watering_from_zone_runs migration tests.

The migration repairs ``zones.last_watering_time`` rows that are NULL by
copying the most recent ``zone_runs.end_utc`` for that zone. Non-NULL
values must be left untouched (we cannot tell anymore which of those are
the buggy start-times vs. correct end-times).
"""
import os
import sqlite3

import pytest

os.environ['TESTING'] = '1'


def _seed_zone_run(db_path, zone_id, group_id, start_utc, end_utc):
    """Insert a finished zone_run row directly (bypasses repository APIs)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            'INSERT INTO zone_runs(zone_id, group_id, start_utc, end_utc, status) '
            'VALUES (?, ?, ?, ?, ?)',
            (zone_id, group_id, start_utc, end_utc, 'ok'),
        )
        conn.commit()
    finally:
        conn.close()


def _set_zone_last_watering(db_path, zone_id, value):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            'UPDATE zones SET last_watering_time = ? WHERE id = ?',
            (value, zone_id),
        )
        conn.commit()
    finally:
        conn.close()


def _read_zone_last_watering(db_path, zone_id):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            'SELECT last_watering_time FROM zones WHERE id = ?', (zone_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


class TestBackfillLastWatering:
    def test_null_zone_gets_filled_from_zone_runs(self, test_db_path):
        """A zone with NULL last_watering_time should be backfilled to the
        most recent finished zone_run.end_utc.
        """
        from database import IrrigationDB
        db = IrrigationDB(db_path=test_db_path)
        zone = db.create_zone({
            'name': 'Backfill', 'duration': 10, 'group_id': 1,
            'topic': '/test/backfill',
        })
        # Sanity: fresh zone has NULL last_watering_time.
        assert _read_zone_last_watering(test_db_path, zone['id']) is None

        _seed_zone_run(test_db_path, zone['id'], 1,
                       '2026-04-01 09:00:00', '2026-04-01 09:15:00')
        _seed_zone_run(test_db_path, zone['id'], 1,
                       '2026-04-02 09:00:00', '2026-04-02 09:30:00')

        # Re-run init to apply the backfill against the seeded data.
        # The migration is idempotent, but we need to first un-record it
        # so it actually runs again on this already-initialised DB.
        conn = sqlite3.connect(test_db_path)
        try:
            conn.execute(
                "DELETE FROM migrations WHERE name = ?",
                ('backfill_last_watering_from_zone_runs',),
            )
            conn.commit()
        finally:
            conn.close()
        db.init_database()

        # Most recent end_utc wins (id DESC).
        assert _read_zone_last_watering(test_db_path, zone['id']) == \
               '2026-04-02 09:30:00'

    def test_nonnull_zone_is_left_alone(self, test_db_path):
        """If last_watering_time is already set (whether correctly or
        from the issue-#2 buggy paths), the backfill must NOT overwrite it.
        """
        from database import IrrigationDB
        db = IrrigationDB(db_path=test_db_path)
        zone = db.create_zone({
            'name': 'Already', 'duration': 10, 'group_id': 1,
            'topic': '/test/already',
        })
        # Set a (possibly wrong, possibly start-time) value.
        prior = '2025-12-31 23:59:59'
        _set_zone_last_watering(test_db_path, zone['id'], prior)
        # Seed a more-recent zone_run that the migration would otherwise pick.
        _seed_zone_run(test_db_path, zone['id'], 1,
                       '2026-04-02 09:00:00', '2026-04-02 09:30:00')

        # Re-apply the migration.
        conn = sqlite3.connect(test_db_path)
        try:
            conn.execute(
                "DELETE FROM migrations WHERE name = ?",
                ('backfill_last_watering_from_zone_runs',),
            )
            conn.commit()
        finally:
            conn.close()
        db.init_database()

        assert _read_zone_last_watering(test_db_path, zone['id']) == prior, (
            'backfill must not overwrite an existing last_watering_time'
        )

    def test_zone_with_no_runs_stays_null(self, test_db_path):
        """A zone with NULL last_watering_time AND no finished zone_runs
        cannot be repaired — it should simply remain NULL (no crash).
        """
        from database import IrrigationDB
        db = IrrigationDB(db_path=test_db_path)
        zone = db.create_zone({
            'name': 'NoRuns', 'duration': 10, 'group_id': 1,
            'topic': '/test/noruns',
        })
        assert _read_zone_last_watering(test_db_path, zone['id']) is None

        conn = sqlite3.connect(test_db_path)
        try:
            conn.execute(
                "DELETE FROM migrations WHERE name = ?",
                ('backfill_last_watering_from_zone_runs',),
            )
            conn.commit()
        finally:
            conn.close()
        db.init_database()

        assert _read_zone_last_watering(test_db_path, zone['id']) is None

    def test_migration_recorded(self, test_db_path):
        """The named migration should be recorded in the migrations table."""
        from database import IrrigationDB
        IrrigationDB(db_path=test_db_path)

        conn = sqlite3.connect(test_db_path)
        try:
            cur = conn.execute(
                'SELECT name FROM migrations WHERE name = ?',
                ('backfill_last_watering_from_zone_runs',),
            )
            assert cur.fetchone() is not None, (
                'backfill migration must be in migrations table'
            )
        finally:
            conn.close()

    def test_idempotent(self, test_db_path):
        """Running the backfill twice must produce the same result and not
        crash.
        """
        from database import IrrigationDB
        db = IrrigationDB(db_path=test_db_path)
        zone = db.create_zone({
            'name': 'Idem', 'duration': 10, 'group_id': 1,
            'topic': '/test/idem-bf',
        })
        _seed_zone_run(test_db_path, zone['id'], 1,
                       '2026-04-02 09:00:00', '2026-04-02 09:30:00')

        for _ in range(3):
            conn = sqlite3.connect(test_db_path)
            try:
                conn.execute(
                    "DELETE FROM migrations WHERE name = ?",
                    ('backfill_last_watering_from_zone_runs',),
                )
                conn.commit()
            finally:
                conn.close()
            db.init_database()
            assert _read_zone_last_watering(test_db_path, zone['id']) == \
                   '2026-04-02 09:30:00'
