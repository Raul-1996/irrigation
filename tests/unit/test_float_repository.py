"""PHYS-3 / MASTER-H3 tests for db.float.FloatRepository.

Core invariants:
  1. FloatRepository uses BaseRepository._connect() which sets
     PRAGMA foreign_keys=ON and journal_mode=WAL.
  2. FloatRepository sets PRAGMA busy_timeout=30000 for safety-critical
     writes (protects against WAL-checkpoint contention).
  3. FloatMonitor routes all DB I/O through the repository — no direct
     sqlite3.connect() on the primary path.
"""

import os
import sqlite3
from unittest.mock import MagicMock

os.environ["TESTING"] = "1"


class TestFloatRepositoryPRAGMA:
    """Contract: FloatRepository enforces safety PRAGMAs."""

    def test_busy_timeout_set_to_30s(self, test_db_path):
        """busy_timeout must be 30000ms — safety-critical write must
        wait on WAL checkpoint instead of failing fast."""
        # Ensure parent schema exists.
        from database import IrrigationDB
        from db.float import FloatRepository

        IrrigationDB(db_path=test_db_path)  # runs migrations

        repo = FloatRepository(test_db_path)
        conn = repo._connect()
        try:
            row = conn.execute("PRAGMA busy_timeout").fetchone()
            # row is sqlite3.Row with one column
            assert int(row[0]) == 30000, f"PHYS-3: busy_timeout must be 30000ms, got {row[0]}"
        finally:
            conn.close()

    def test_foreign_keys_enabled(self, test_db_path):
        """foreign_keys must be ON — inherited from BaseRepository._connect."""
        from database import IrrigationDB
        from db.float import FloatRepository

        IrrigationDB(db_path=test_db_path)

        repo = FloatRepository(test_db_path)
        conn = repo._connect()
        try:
            row = conn.execute("PRAGMA foreign_keys").fetchone()
            assert int(row[0]) == 1, "foreign_keys must be ON"
        finally:
            conn.close()

    def test_journal_mode_wal(self, test_db_path):
        """journal_mode must be WAL — inherited from BaseRepository._connect."""
        from database import IrrigationDB
        from db.float import FloatRepository

        IrrigationDB(db_path=test_db_path)

        repo = FloatRepository(test_db_path)
        conn = repo._connect()
        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            assert str(row[0]).lower() == "wal", f"journal_mode must be WAL, got {row[0]}"
        finally:
            conn.close()


class TestFloatRepositoryOps:
    """Contract: repository CRUD behaves correctly."""

    def test_log_event_persists(self, test_db, test_db_path):
        """log_event() inserts a row into float_events."""
        from db.float import FloatRepository

        repo = FloatRepository(test_db_path)

        ok = repo.log_event(group_id=1, event_type="float_pause", paused_zones=[10, 11])
        assert ok is True

        with sqlite3.connect(test_db_path) as conn:
            row = conn.execute(
                "SELECT group_id, event_type, paused_zones FROM float_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        assert row[0] == 1
        assert row[1] == "float_pause"
        assert "10" in row[2] and "11" in row[2]

    def test_pause_active_zones_transitions_state(self, test_db, test_db_path):
        """pause_active_zones() flips state='on' zones to 'paused'
        with reason='float' and records pause_remaining_seconds=duration."""
        # create zone in state='on'
        z = test_db.create_zone(
            {
                "name": "FZ",
                "duration": 120,
                "group_id": 7,
            }
        )
        # Set state='on' directly (create_zone default state is 'off')
        with sqlite3.connect(test_db_path) as conn:
            conn.execute("UPDATE zones SET state='on' WHERE id=?", (z["id"],))
            conn.commit()

        from db.float import FloatRepository

        repo = FloatRepository(test_db_path)
        paused = repo.pause_active_zones(group_id=7)
        assert z["id"] in paused

        after = test_db.get_zone(z["id"])
        assert after["state"] == "paused"
        assert after.get("pause_reason") == "float"
        assert int(after.get("pause_remaining_seconds") or 0) == 120

    def test_pause_active_zones_ignores_off_zones(self, test_db, test_db_path):
        """Zones not in state='on' must not be paused."""
        z = test_db.create_zone(
            {
                "name": "OFF",
                "duration": 60,
                "group_id": 8,
            }
        )
        # state is default 'off' — no pausing expected

        from db.float import FloatRepository

        repo = FloatRepository(test_db_path)
        paused = repo.pause_active_zones(group_id=8)
        assert paused == []

    def test_get_float_enabled_groups_filters(self, test_db, test_db_path):
        """get_float_enabled_groups returns only groups with float_enabled=1."""
        # groups table is pre-seeded in migrations; flip group 1 to enabled.
        with sqlite3.connect(test_db_path) as conn:
            conn.execute("UPDATE groups SET float_enabled=1 WHERE id=1")
            conn.commit()

        from db.float import FloatRepository

        repo = FloatRepository(test_db_path)
        enabled = repo.get_float_enabled_groups()
        ids = [g["id"] for g in enabled]
        assert 1 in ids
        # group 999 (postponed) must not be float-enabled
        assert 999 not in ids


class TestFloatMonitorUsesRepo:
    """Contract: FloatMonitor primary path goes through FloatRepository."""

    def test_monitor_constructs_repo_by_default(self, test_db_path):
        """When `repo` kwarg is omitted, FloatMonitor instantiates
        FloatRepository(db_path)."""
        from db.float import FloatRepository
        from services.float_monitor import FloatMonitor

        fm = FloatMonitor(
            db_path=test_db_path,
            mqtt_clients={},
            queue_manager=MagicMock(),
        )
        assert fm._repo is not None, "FloatMonitor._repo must be wired"
        assert isinstance(fm._repo, FloatRepository)

    def test_log_event_uses_repo(self, test_db_path):
        """_log_float_event delegates to repo.log_event()."""
        from services.float_monitor import FloatMonitor

        fake_repo = MagicMock()
        fm = FloatMonitor(
            db_path=test_db_path,
            mqtt_clients={},
            queue_manager=MagicMock(),
            repo=fake_repo,
        )
        fm._log_float_event(42, "float_pause", [1, 2, 3])
        fake_repo.log_event.assert_called_once_with(42, "float_pause", [1, 2, 3])

    def test_pause_active_zones_uses_repo(self, test_db_path):
        """_pause_active_zones_in_db delegates to repo.pause_active_zones()."""
        from services.float_monitor import FloatMonitor

        fake_repo = MagicMock()
        fake_repo.pause_active_zones.return_value = [7, 8]
        fm = FloatMonitor(
            db_path=test_db_path,
            mqtt_clients={},
            queue_manager=MagicMock(),
            repo=fake_repo,
        )
        result = fm._pause_active_zones_in_db(42)
        assert result == [7, 8]
        fake_repo.pause_active_zones.assert_called_once_with(42)

    def test_load_all_groups_uses_repo(self, test_db_path):
        """_load_all_groups delegates to repo.get_float_enabled_groups()."""
        from services.float_monitor import FloatMonitor

        fake_repo = MagicMock()
        fake_repo.get_float_enabled_groups.return_value = []
        fm = FloatMonitor(
            db_path=test_db_path,
            mqtt_clients={},
            queue_manager=MagicMock(),
            repo=fake_repo,
        )
        fm._load_all_groups()
        fake_repo.get_float_enabled_groups.assert_called_once()
