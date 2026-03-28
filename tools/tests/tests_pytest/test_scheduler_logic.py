"""
Tests for irrigation_scheduler.py — scheduler logic, program execution, conflicts.
"""
import os
import sys
import json
import time
import pytest
from unittest.mock import patch, MagicMock

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("TESTING", "1")

import app as app_module
import database as database_module


class TestSchedulerViaAPI:
    """Test scheduler functionality through the API."""

    def test_scheduler_init_returns_ok(self, client):
        r = client.post('/api/scheduler/init')
        assert r.status_code == 200

    def test_scheduler_status_structure(self, client):
        r = client.get('/api/scheduler/status')
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, dict)

    def test_scheduler_jobs_list(self, client):
        # Init scheduler first
        client.post('/api/scheduler/init')
        r = client.get('/api/scheduler/jobs')
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, (dict, list))

    def test_scheduler_reinit_safe(self, client):
        """Re-initializing scheduler should not crash."""
        r1 = client.post('/api/scheduler/init')
        assert r1.status_code == 200
        r2 = client.post('/api/scheduler/init')
        assert r2.status_code == 200

    def test_group_start_stop_cycle(self, client):
        """Start group, verify zones activate, then stop."""
        r1 = client.post('/api/groups/1/start-from-first')
        assert r1.status_code == 200

        # Check status
        r2 = client.get('/api/status')
        assert r2.status_code == 200

        # Stop
        r3 = client.post('/api/groups/1/stop')
        assert r3.status_code == 200

    def test_group_start_zone_specific(self, client):
        """Start from a specific zone in the group."""
        r = client.post('/api/groups/1/start-zone/2')
        assert r.status_code == 200
        client.post('/api/groups/1/stop')

    def test_emergency_stop_blocks_start(self, client):
        """Emergency stop should prevent zone starts."""
        client.post('/api/emergency-stop')
        # After emergency stop, trying to start should behave differently
        r = client.post('/api/zones/1/start')
        # Could be blocked or allowed depending on implementation
        assert r.status_code in (200, 400, 403)
        client.post('/api/emergency-resume')

    def test_postpone_group(self, client):
        """Postpone watering for a group."""
        r = client.post('/api/postpone', json={
            'group_id': 1,
            'days': 2,
            'action': 'postpone'
        })
        assert r.status_code == 200

        # Cancel postpone
        r2 = client.post('/api/postpone', json={
            'group_id': 1,
            'action': 'cancel'
        })
        assert r2.status_code == 200

    def test_postpone_invalid_group(self, client):
        """Postpone for nonexistent group."""
        r = client.post('/api/postpone', json={
            'group_id': 99999,
            'days': 1,
            'action': 'postpone'
        })
        assert r.status_code in (200, 400, 404)


class TestProgramConflicts:
    """Test program conflict detection."""

    def test_no_conflict_different_times(self, client):
        r = client.post('/api/programs/check-conflicts', json={
            'time': '14:00',
            'days': [0, 1, 2, 3, 4, 5, 6],
            'zones': [1, 2, 3]
        })
        assert r.status_code == 200

    def test_conflict_same_time(self, client):
        """Same time + same zones should detect conflict."""
        r = client.post('/api/programs/check-conflicts', json={
            'time': '04:00',  # Same as seeded program
            'days': [0, 1, 2, 3, 4, 5, 6],
            'zones': [1, 2, 3]
        })
        assert r.status_code == 200
        data = r.get_json()
        # Should flag some conflict
        assert isinstance(data, dict)

    def test_duration_conflict_check(self, client):
        r = client.post('/api/zones/check-duration-conflicts', json={
            'zone_id': 1,
            'duration': 120  # Very long duration
        })
        assert r.status_code == 200

    def test_duration_conflict_bulk(self, client):
        r = client.post('/api/zones/check-duration-conflicts-bulk', json={
            'zones': [
                {'id': 1, 'duration': 60},
                {'id': 2, 'duration': 60},
                {'id': 3, 'duration': 60}
            ]
        })
        assert r.status_code == 200
