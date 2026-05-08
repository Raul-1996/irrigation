"""Test the daily audit cleanup APScheduler job (job_audit_cleanup)."""
from __future__ import annotations

import os
import sqlite3

import pytest

os.environ['TESTING'] = '1'


class TestAuditCleanupJob:

    def test_job_invokes_cleanup_and_self_audits(self, test_db, monkeypatch):
        """The scheduled job must (1) delete >7d rows and (2) record itself."""
        # Repoint the global db used by the scheduler job to the test instance.
        import database as db_mod
        monkeypatch.setattr(db_mod, 'db', test_db, raising=False)

        # Seed: insert two old rows + one fresh row.
        for _ in range(2):
            test_db.add_audit(action_type='ancient')
        test_db.add_audit(action_type='fresh')
        with sqlite3.connect(test_db.db_path) as conn:
            conn.execute(
                "UPDATE audit_log SET ts = datetime('now','-30 days') "
                "WHERE action_type = 'ancient'"
            )
            conn.commit()

        from irrigation_scheduler import job_audit_cleanup
        job_audit_cleanup()

        # Two ancient rows gone + one self-audit row added → 1 + 1 = 2 remain.
        rows = test_db.get_audit_logs(limit=20)
        action_types = [r['action_type'] for r in rows]
        assert 'ancient' not in action_types
        assert 'fresh' in action_types
        assert 'audit_cleanup' in action_types

    def test_job_no_op_when_no_old_rows(self, test_db, monkeypatch):
        """Empty / fresh-only DB: no deletions, but self-audit row is still written."""
        import database as db_mod
        monkeypatch.setattr(db_mod, 'db', test_db, raising=False)
        test_db.add_audit(action_type='today')

        from irrigation_scheduler import job_audit_cleanup
        job_audit_cleanup()

        rows = test_db.get_audit_logs(limit=20)
        action_types = [r['action_type'] for r in rows]
        assert 'today' in action_types
        # Self-audit row recorded with deleted=0
        cleanup_rows = [r for r in rows if r['action_type'] == 'audit_cleanup']
        assert len(cleanup_rows) == 1
        assert '"deleted": 0' in cleanup_rows[0]['payload_json']

    def test_job_swallows_db_errors(self, monkeypatch):
        """A DB explosion inside the job must NOT propagate (cron resilience)."""
        import database as db_mod

        class _Boom:
            def cleanup_audit_logs(self, **kw):
                raise sqlite3.Error('disk gone')

        monkeypatch.setattr(db_mod, 'db', _Boom(), raising=False)
        from irrigation_scheduler import job_audit_cleanup
        # Must not raise
        job_audit_cleanup()
