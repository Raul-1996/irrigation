"""Tests for scheduler service."""
import pytest
import os
from unittest.mock import patch, MagicMock

os.environ['TESTING'] = '1'


class TestSchedulerInit:
    def test_scheduler_init_and_get(self, test_db):
        """Scheduler should be initializable."""
        from irrigation_scheduler import init_scheduler, get_scheduler
        sched = init_scheduler(test_db)
        assert sched is not None

    def test_scheduler_get_returns_something(self):
        """get_scheduler should not crash."""
        from irrigation_scheduler import get_scheduler
        # May return None or the existing scheduler
        get_scheduler()
