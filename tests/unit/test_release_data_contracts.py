"""Scheduler release regressions for unsupported program semantics."""

from unittest.mock import patch

import pytest


@pytest.fixture
def test_scheduler(test_db):
    from irrigation_scheduler import IrrigationScheduler

    scheduler = IrrigationScheduler(test_db)
    scheduler.start()
    yield scheduler
    scheduler.stop()


def test_smart_program_never_produces_scheduler_jobs(test_scheduler):
    zone = test_scheduler.db.create_zone({"name": "No alias", "duration": 10, "group_id": 1})
    program = test_scheduler.db.create_program(
        {
            "name": "Unsupported smart",
            "time": "06:00",
            "days": [0],
            "zones": [zone["id"]],
            "type": "smart",
            "enabled": True,
        }
    )

    with patch.object(test_scheduler.scheduler, "add_job", wraps=test_scheduler.scheduler.add_job) as add_job:
        assert test_scheduler.schedule_program(program["id"], program) is False

    add_job.assert_not_called()
    assert not [job for job in test_scheduler.scheduler.get_jobs() if f"program:{program['id']}:" in job.id]
