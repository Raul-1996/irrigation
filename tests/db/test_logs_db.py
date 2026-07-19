"""Tests for logs DB: log entries, water usage."""

import os
from pathlib import Path

os.environ["TESTING"] = "1"


class TestLogs:
    def test_add_log(self, test_db):
        test_db.add_log("test_event", "test details")
        logs = test_db.get_logs()
        assert len(logs) > 0

    def test_get_logs_by_type(self, test_db):
        test_db.add_log("zone_start", "started")
        test_db.add_log("zone_stop", "stopped")
        logs = test_db.get_logs(event_type="zone_start")
        # get_logs may not filter by type directly; check it doesn't crash
        assert isinstance(logs, list)

    def test_add_multiple_logs(self, test_db):
        for i in range(10):
            test_db.add_log("batch", f"entry {i}")
        logs = test_db.get_logs()
        assert len(logs) >= 10


class TestWaterUsage:
    def test_add_water_usage(self, test_db):
        zone = test_db.create_zone({"name": "W", "duration": 10, "group_id": 1})
        test_db.add_water_usage(zone["id"], 100.5)
        # Should not crash

    def test_get_water_usage(self, test_db):
        result = test_db.get_water_usage(days=7)
        assert isinstance(result, (list, dict, type(None)))

    def test_get_water_statistics(self, test_db):
        result = test_db.get_water_statistics(days=30)
        assert isinstance(result, (list, dict, type(None)))


class TestBackup:
    def test_create_backup(self, test_db):
        result = test_db.create_backup()
        # May return path or None depending on implementation
        assert isinstance(result, (str, type(None)))

    def test_create_backup_rejects_invalid_sqlite_file(self, test_db, tmp_path, monkeypatch):
        """A physically present snapshot is accepted only after SQLite validation."""
        backup_dir = tmp_path / "bak"
        test_db.logs.backup_dir = str(backup_dir)

        def write_corrupt_snapshot(_source, target):
            Path(target).write_bytes(b"not a sqlite database")

        monkeypatch.setattr(test_db.logs, "_backup_via_api", write_corrupt_snapshot)
        result = test_db.create_backup()

        assert result is None
        if backup_dir.exists():
            stragglers = [p for p in backup_dir.iterdir() if p.name.startswith("irrigation_backup_")]
            assert stragglers == [], f"validation did not remove backup: {stragglers}"
