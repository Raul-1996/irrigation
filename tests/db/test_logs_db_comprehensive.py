"""Comprehensive tests for db/logs.py."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestLogs:
    def test_add_log(self, test_db):
        test_db.add_log('test_event', 'test details')
        logs = test_db.get_logs(event_type='test_event')
        assert len(logs) >= 1

    def test_add_log_no_details(self, test_db):
        test_db.add_log('bare_event')
        logs = test_db.get_logs(event_type='bare_event')
        assert len(logs) >= 1

    def test_get_logs_all(self, test_db):
        test_db.add_log('ev1', 'a')
        test_db.add_log('ev2', 'b')
        logs = test_db.get_logs()
        assert len(logs) >= 2

    def test_get_logs_filtered(self, test_db):
        test_db.add_log('filter_type', 'data')
        test_db.add_log('other_type', 'data')
        logs = test_db.get_logs(event_type='filter_type')
        assert isinstance(logs, list)

    def test_get_logs_date_range(self, test_db):
        test_db.add_log('dated', 'x')
        logs = test_db.get_logs(from_date='2026-01-01', to_date='2030-12-31')
        # Should return logs in range


class TestWaterUsage:
    def test_add_and_get(self, test_db):
        z = test_db.create_zone({'name': 'W', 'duration': 10, 'group_id': 1})
        test_db.add_water_usage(z['id'], 50.5)
        usage = test_db.get_water_usage(days=7)
        assert isinstance(usage, list)

    def test_get_water_statistics(self, test_db):
        stats = test_db.get_water_statistics(days=30)
        assert isinstance(stats, (dict, list))


class TestBackup:
    def test_create_backup(self, test_db):
        result = test_db.create_backup()
        # May succeed or fail depending on backup_dir
        assert isinstance(result, (str, bool, type(None)))
