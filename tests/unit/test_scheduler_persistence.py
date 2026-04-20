"""Tests for APScheduler persistence via SQLAlchemyJobStore (PHYS-2 / MASTER-H10).

These tests verify the safety-critical invariant that one-shot jobs such as
zone_stop:<id> survive a process restart. Before the fix APScheduler silently
fell back to MemoryJobStore (SQLAlchemy was missing from requirements.txt on
prod), so every `systemctl restart wb-irrigation` during an active watering
cycle left the valve open indefinitely.
"""
import os
os.environ['TESTING'] = '1'

import pytest
from datetime import datetime, timedelta


def _make_scheduler(db):
    """Create a fresh IrrigationScheduler bound to the given test DB."""
    from irrigation_scheduler import IrrigationScheduler
    s = IrrigationScheduler(db)
    return s


class TestJobStorePersistence:
    def test_sqlalchemy_jobstore_active(self, test_db):
        """Backend should report 'sqlalchemy' when SQLAlchemy is installed."""
        s = _make_scheduler(test_db)
        try:
            assert s.jobstore_backend == 'sqlalchemy', (
                f"Expected persistent jobstore, got {s.jobstore_backend!r} — "
                "PHYS-2 regression: APScheduler will lose zone_stop jobs on restart."
            )
            assert s.has_default_jobstore is True
            assert s.has_volatile_jobstore is True
        finally:
            try:
                if s.is_running:
                    s.stop()
            except (AttributeError, RuntimeError):
                pass

    def test_jobs_db_separate_file(self, test_db, tmp_path):
        """jobs.db must be a sibling of irrigation.db, NOT shared with it."""
        s = _make_scheduler(test_db)
        try:
            db_dir = os.path.dirname(os.path.abspath(test_db.db_path))
            expected_jobs_db = os.path.join(db_dir, 'jobs.db')
            # Starting scheduler writes the jobs table
            s.start()
            assert os.path.exists(expected_jobs_db), (
                "jobs.db missing — persistence not writing to dedicated file"
            )
            # irrigation.db must NOT contain apscheduler_jobs table
            import sqlite3
            con = sqlite3.connect(test_db.db_path)
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='apscheduler_jobs'"
            ).fetchall()
            con.close()
            assert rows == [], (
                "APScheduler wrote its jobs table into irrigation.db instead of jobs.db — "
                "violates backup/restore assumption"
            )
        finally:
            try:
                s.stop()
            except (AttributeError, RuntimeError):
                pass

    def test_misfire_policy_coalesce(self, test_db):
        """job_defaults must include coalesce=True, misfire_grace_time>=300."""
        s = _make_scheduler(test_db)
        try:
            defaults = s.scheduler._job_defaults
            assert defaults.get('coalesce') is True, (
                "coalesce must be True so that stacked misfires after a restart "
                "collapse into a single fire (we don't want 10 zone_stops)"
            )
            assert defaults.get('misfire_grace_time', 0) >= 300, (
                "misfire_grace_time must be >= 5 minutes so a late zone_stop "
                "still executes after brief restart-gap"
            )
            assert defaults.get('max_instances', 99) == 1, (
                "max_instances=1 prevents the same job running concurrently"
            )
        finally:
            try:
                s.stop()
            except (AttributeError, RuntimeError):
                pass

    def test_scheduled_job_survives_restart(self, test_db, tmp_path):
        """Core PHYS-2 invariant: a one-shot job scheduled in scheduler A must be
        visible in scheduler B (new process) provided both point at the same DB."""
        from apscheduler.triggers.date import DateTrigger
        from scheduler.jobs import job_stop_zone

        # Scheduler A: schedule a far-future job
        s_a = _make_scheduler(test_db)
        s_a.start()
        try:
            run_at = datetime.now() + timedelta(hours=2)
            job_id = 'test-persist-zone-stop-42'
            s_a.scheduler.add_job(
                job_stop_zone,
                DateTrigger(run_date=run_at),
                args=[42],
                id=job_id,
                replace_existing=True,
                jobstore='default',
            )
            # Assert it lives in the default (persistent) store
            job_in_default = s_a.scheduler.get_job(job_id, jobstore='default')
            assert job_in_default is not None, "Job not in default jobstore"
        finally:
            s_a.stop()

        # Scheduler B: fresh instance, same DB → job should be restored
        s_b = _make_scheduler(test_db)
        s_b.start()
        try:
            restored = s_b.scheduler.get_job('test-persist-zone-stop-42', jobstore='default')
            assert restored is not None, (
                "PHYS-2 failure: zone_stop job was lost across scheduler restart. "
                "This means a real systemctl restart mid-watering leaves the valve open."
            )
            assert list(restored.args) == [42]
            # Cleanup
            s_b.scheduler.remove_job('test-persist-zone-stop-42', jobstore='default')
        finally:
            s_b.stop()

    def test_volatile_job_does_not_persist(self, test_db):
        """Jobs scheduled on 'volatile' store are intentionally lost on restart."""
        from apscheduler.triggers.date import DateTrigger
        from scheduler.jobs import job_stop_zone

        s_a = _make_scheduler(test_db)
        s_a.start()
        try:
            s_a.scheduler.add_job(
                job_stop_zone,
                DateTrigger(run_date=datetime.now() + timedelta(hours=2)),
                args=[7],
                id='test-volatile-7',
                replace_existing=True,
                jobstore='volatile',
            )
        finally:
            s_a.stop()

        s_b = _make_scheduler(test_db)
        s_b.start()
        try:
            ghost = s_b.scheduler.get_job('test-volatile-7', jobstore='volatile')
            assert ghost is None, "Volatile job leaked into persistent store"
        finally:
            s_b.stop()
