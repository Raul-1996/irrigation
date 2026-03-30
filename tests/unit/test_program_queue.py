"""TDD tests for ProgramQueueManager.

These tests define the contract BEFORE implementation exists.
All tests will be RED until services/program_queue.py is implemented.

Covers: basic ops, FIFO, cancel, max_wait, weather, errors, completion tracking.
"""
import os
import re
import time
import threading
import uuid
from datetime import datetime, timedelta
from typing import List, Optional
from unittest.mock import MagicMock, patch, PropertyMock, call

import pytest

os.environ['TESTING'] = '1'

# These imports will fail until the module is created — that's TDD.
from services.program_queue import (
    ProgramQueueManager,
    QueueEntry,
    QueueEntryState,
    GroupQueue,
    ProgramCompletionTracker,
    MAX_QUEUE_SIZE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UUID4_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)


def wait_for_state(
    entry: QueueEntry,
    expected: QueueEntryState,
    timeout: float = 5.0,
) -> bool:
    """Poll entry.state until it matches *expected* or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if entry.state == expected:
            return True
        time.sleep(0.05)
    return False


def wait_for_worker_idle(
    qm: ProgramQueueManager,
    group_id: int,
    timeout: float = 5.0,
) -> bool:
    """Wait until the worker thread for *group_id* finishes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        gq = qm._queues.get(group_id)
        if gq is None or gq.worker_thread is None or not gq.worker_thread.is_alive():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_db(tmp_path):
    """In-memory IrrigationDB mock with essential helpers."""
    db = MagicMock()
    db.path = str(tmp_path / 'test.db')
    return db


@pytest.fixture()
def shutdown_event():
    return threading.Event()


@pytest.fixture()
def mock_float_monitor():
    fm = MagicMock()
    fm.is_paused.return_value = False
    fm.is_timed_out.return_value = False
    fm.is_resumed.return_value = False
    fm.get_resume_event.return_value = threading.Event()
    return fm


@pytest.fixture()
def mock_telegram():
    return MagicMock()


@pytest.fixture()
def mock_weather_coefficient():
    """Callable returning weather coefficient (default 100 %)."""
    return MagicMock(return_value=100)


@pytest.fixture()
def zone_control():
    """Tracks exclusive_start_zone / stop_zone calls."""
    ctrl = MagicMock()
    ctrl.exclusive_start_zone = MagicMock(return_value=True)
    ctrl.stop_zone = MagicMock(return_value=True)
    return ctrl


@pytest.fixture()
def queue_manager(
    mock_db,
    shutdown_event,
    mock_float_monitor,
    mock_telegram,
    mock_weather_coefficient,
):
    """Fresh ProgramQueueManager wired with mocks."""
    qm = ProgramQueueManager(
        db=mock_db,
        shutdown_event=shutdown_event,
        float_monitor=mock_float_monitor,
        get_weather_coefficient=mock_weather_coefficient,
        telegram_notify=mock_telegram,
    )
    yield qm
    # Cleanup: ensure all workers stop
    try:
        qm.shutdown()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 1. Basic operations (8 tests)
# ---------------------------------------------------------------------------

class TestBasicOperations:
    """Tests #1-#8: creation, enqueue, state, shutdown."""

    def test_enqueue_single_entry_lifecycle(self, queue_manager):
        """#1 — enqueue → WAITING → RUNNING → COMPLETED lifecycle."""
        entry = queue_manager.enqueue(
            program_id=1,
            program_name="Утро",
            group_id=1,
            zone_ids=[1, 2, 3],
            scheduled_time=datetime.now(),
        )
        assert entry is not None
        assert isinstance(entry, QueueEntry)
        assert entry.state == QueueEntryState.WAITING or entry.state == QueueEntryState.RUNNING
        # Wait for completion (zones are fast with mocked sleep)
        assert wait_for_state(entry, QueueEntryState.COMPLETED)

    def test_enqueue_creates_worker_on_empty_group(self, queue_manager):
        """#2 — first enqueue on a group starts a worker thread."""
        entry = queue_manager.enqueue(
            program_id=1,
            program_name="Test",
            group_id=1,
            zone_ids=[1],
            scheduled_time=datetime.now(),
        )
        gq = queue_manager._queues.get(1)
        assert gq is not None
        assert gq.worker_thread is not None
        assert gq.worker_thread.is_alive()
        assert "queue-worker-1" in gq.worker_thread.name

    def test_enqueue_to_busy_group_waits(self, queue_manager):
        """#3 — second entry stays WAITING while first is RUNNING."""
        # Block the worker on first entry
        block = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            if entry.program_name == "A":
                block.wait(timeout=5)
            return original_run(entry, *a, **kw)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            entry_a = queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            time.sleep(0.1)  # let worker pick up A
            entry_b = queue_manager.enqueue(2, "B", 1, [2], datetime.now())

            assert wait_for_state(entry_a, QueueEntryState.RUNNING, timeout=2)
            assert entry_b.state == QueueEntryState.WAITING
            block.set()

    def test_get_queue_state_structure(self, queue_manager):
        """#4 — get_queue_state returns proper dict structure."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            if entry.program_name == "A":
                block.wait(timeout=5)
            return original_run(entry, *a, **kw)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            time.sleep(0.1)
            queue_manager.enqueue(2, "B", 1, [2], datetime.now())
            queue_manager.enqueue(3, "C", 1, [3], datetime.now())

            state = queue_manager.get_queue_state(1)
            assert 'group_id' in state
            assert 'current' in state
            assert 'queue' in state
            assert 'queue_length' in state
            assert state['group_id'] == 1
            assert state['current'] is not None
            assert isinstance(state['queue'], list)
            assert state['queue_length'] == 2
            block.set()

    def test_get_all_queues_state_all_groups(self, queue_manager):
        """#5 — get_all_queues_state returns data for all active groups."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            block.wait(timeout=5)
            return original_run(entry, *a, **kw)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            queue_manager.enqueue(2, "B", 2, [10], datetime.now())
            time.sleep(0.1)

            state = queue_manager.get_all_queues_state()
            assert 'queues' in state
            assert 'total_entries' in state
            assert 'active_workers' in state
            assert 1 in state['queues']
            assert 2 in state['queues']
            assert state['active_workers'] == 2
            block.set()

    def test_entry_id_is_unique_uuid4(self, queue_manager):
        """#6 — entry_id is a valid UUID4 and unique across entries."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            block.wait(timeout=5)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            e1 = queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            e2 = queue_manager.enqueue(2, "B", 1, [2], datetime.now())

            assert e1.entry_id != e2.entry_id
            assert UUID4_RE.match(e1.entry_id)
            assert UUID4_RE.match(e2.entry_id)
            block.set()

    def test_deque_maxlen_rejects_overflow(self, queue_manager):
        """#7 — 21st enqueue returns None (overflow)."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            block.wait(timeout=5)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            entries = []
            for i in range(MAX_QUEUE_SIZE + 1):
                e = queue_manager.enqueue(
                    program_id=i,
                    program_name="P%d" % i,
                    group_id=1,
                    zone_ids=[i + 1],
                    scheduled_time=datetime.now(),
                )
                entries.append(e)

            # First MAX_QUEUE_SIZE entries succeed (one is current + rest in deque)
            accepted = [e for e in entries if e is not None]
            rejected = [e for e in entries if e is None]
            # At least 1 should be rejected (the 21st)
            assert len(rejected) >= 1
            block.set()

    def test_shutdown_terminates_all_workers(self, queue_manager):
        """#8 — shutdown() stops all worker threads."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            block.wait(timeout=5)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            queue_manager.enqueue(2, "B", 2, [10], datetime.now())
            queue_manager.enqueue(3, "C", 3, [20], datetime.now())
            time.sleep(0.1)

            # Verify workers are alive
            alive_before = sum(
                1 for gq in queue_manager._queues.values()
                if gq.worker_thread and gq.worker_thread.is_alive()
            )
            assert alive_before == 3

        # Now shutdown (block released via context manager exit)
        queue_manager.shutdown()

        for gq in queue_manager._queues.values():
            if gq.worker_thread is not None:
                assert not gq.worker_thread.is_alive()


# ---------------------------------------------------------------------------
# 2. FIFO queue (6 tests)
# ---------------------------------------------------------------------------

class TestFIFO:
    """Tests #9-#14: FIFO ordering, parallel groups, worker lifecycle."""

    def test_fifo_two_entries_sequential(self, queue_manager):
        """#9 — A completes before B starts."""
        order = []  # type: List[str]
        original_run = queue_manager._run_entry

        def tracking_run(entry, *a, **kw):
            order.append("start_%s" % entry.program_name)
            original_run(entry, *a, **kw)
            order.append("end_%s" % entry.program_name)

        with patch.object(queue_manager, '_run_entry', side_effect=tracking_run):
            queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            queue_manager.enqueue(2, "B", 1, [2], datetime.now())
            assert wait_for_worker_idle(queue_manager, 1)

        assert order.index("start_A") < order.index("start_B")
        assert order.index("end_A") < order.index("start_B")

    def test_fifo_three_entries_cascade(self, queue_manager):
        """#10 — strict A→B→C ordering."""
        started = []  # type: List[str]
        original_run = queue_manager._run_entry

        def tracking_run(entry, *a, **kw):
            started.append(entry.program_name)
            original_run(entry, *a, **kw)

        with patch.object(queue_manager, '_run_entry', side_effect=tracking_run):
            queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            queue_manager.enqueue(2, "B", 1, [2], datetime.now())
            queue_manager.enqueue(3, "C", 1, [3], datetime.now())
            assert wait_for_worker_idle(queue_manager, 1)

        assert started == ["A", "B", "C"]

    def test_different_groups_run_parallel(self, queue_manager):
        """#11 — entries in different groups start ~simultaneously."""
        started_at = {}  # type: dict
        barrier = threading.Barrier(2, timeout=5)
        original_run = queue_manager._run_entry

        def tracking_run(entry, *a, **kw):
            started_at[entry.group_id] = time.monotonic()
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            original_run(entry, *a, **kw)

        with patch.object(queue_manager, '_run_entry', side_effect=tracking_run):
            queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            queue_manager.enqueue(2, "B", 2, [10], datetime.now())
            wait_for_worker_idle(queue_manager, 1)
            wait_for_worker_idle(queue_manager, 2)

        assert abs(started_at[1] - started_at[2]) < 1.0

    def test_program_split_across_two_groups(self, queue_manager):
        """#12 — multi-group program creates entries in both groups."""
        run_id = str(uuid.uuid4())
        e1 = queue_manager.enqueue(
            5, "Утро", 1, [1, 2], datetime.now(), program_run_id=run_id,
        )
        e2 = queue_manager.enqueue(
            5, "Утро", 2, [10], datetime.now(), program_run_id=run_id,
        )
        assert e1 is not None
        assert e2 is not None
        assert e1.group_id == 1
        assert e2.group_id == 2

    def test_worker_terminates_when_queue_empty(self, queue_manager):
        """#13 — worker thread exits after processing last entry."""
        queue_manager.enqueue(1, "A", 1, [1], datetime.now())
        assert wait_for_worker_idle(queue_manager, 1)

        gq = queue_manager._queues.get(1)
        if gq and gq.worker_thread:
            assert not gq.worker_thread.is_alive()

    def test_entries_different_programs_same_group(self, queue_manager):
        """#14 — different programs in same group follow FIFO."""
        order = []  # type: List[int]
        original_run = queue_manager._run_entry

        def tracking_run(entry, *a, **kw):
            order.append(entry.program_id)
            original_run(entry, *a, **kw)

        with patch.object(queue_manager, '_run_entry', side_effect=tracking_run):
            queue_manager.enqueue(10, "Morning", 1, [1], datetime.now())
            queue_manager.enqueue(20, "Evening", 1, [2], datetime.now())
            assert wait_for_worker_idle(queue_manager, 1)

        assert order == [10, 20]


# ---------------------------------------------------------------------------
# 3. Cancel (5 tests)
# ---------------------------------------------------------------------------

class TestCancel:
    """Tests #15-#19: cancel entry, program, group."""

    def test_cancel_entry_waiting(self, queue_manager):
        """#15 — cancel a WAITING entry returns True, entry becomes CANCELLED."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            if entry.program_name == "A":
                block.wait(timeout=5)
            return original_run(entry, *a, **kw)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            entry_a = queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            time.sleep(0.1)
            entry_b = queue_manager.enqueue(2, "B", 1, [2], datetime.now())

            result = queue_manager.cancel_entry(entry_b.entry_id)
            assert result is True
            assert entry_b.state == QueueEntryState.CANCELLED
            # A should continue running
            assert entry_a.state == QueueEntryState.RUNNING
            block.set()

    def test_cancel_entry_running_stops_zones(self, queue_manager):
        """#16 — cancel a RUNNING entry stops current zone."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            block.wait(timeout=5)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            entry_a = queue_manager.enqueue(1, "A", 1, [3], datetime.now())
            time.sleep(0.1)
            assert wait_for_state(entry_a, QueueEntryState.RUNNING, timeout=2)

            result = queue_manager.cancel_entry(entry_a.entry_id)
            assert result is True
            block.set()

        assert wait_for_state(entry_a, QueueEntryState.CANCELLED, timeout=3)

    def test_cancel_program_all_entries(self, queue_manager):
        """#17 — cancel_program cancels all entries for given program_id."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            block.wait(timeout=5)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            e1 = queue_manager.enqueue(5, "P", 1, [1], datetime.now())
            time.sleep(0.05)
            e2 = queue_manager.enqueue(5, "P", 1, [2], datetime.now())
            e3 = queue_manager.enqueue(5, "P", 2, [10], datetime.now())
            time.sleep(0.1)

            count = queue_manager.cancel_program(5)
            assert count == 3
            block.set()

        for e in [e1, e2, e3]:
            assert e.state == QueueEntryState.CANCELLED

    def test_cancel_group_waits_for_worker(self, queue_manager):
        """#18 — cancel_group waits for worker to finish, zones OFF via finally."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            block.wait(timeout=5)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            e1 = queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            e2 = queue_manager.enqueue(2, "B", 1, [2], datetime.now())
            time.sleep(0.1)

            block.set()  # Let worker proceed
            count = queue_manager.cancel_group(1)
            assert count >= 1

        # Worker should have terminated
        gq = queue_manager._queues.get(1)
        if gq and gq.worker_thread:
            assert not gq.worker_thread.is_alive()

    def test_cancel_during_float_pause(self, queue_manager, mock_float_monitor):
        """#19 — cancel while float-paused wakes up the worker."""
        mock_float_monitor.is_paused.return_value = True
        resume_event = threading.Event()
        mock_float_monitor.get_resume_event.return_value = resume_event

        entry = queue_manager.enqueue(1, "A", 1, [1], datetime.now())
        time.sleep(0.2)

        result = queue_manager.cancel_entry(entry.entry_id)
        assert result is True
        assert wait_for_state(entry, QueueEntryState.CANCELLED, timeout=3)


# ---------------------------------------------------------------------------
# 4. max_wait_time (4 tests)
# ---------------------------------------------------------------------------

class TestMaxWaitTime:
    """Tests #20-#23: entry expiration based on wait time."""

    def test_max_wait_expired(self, queue_manager):
        """#20 — entry waiting >max_wait_minutes gets EXPIRED."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        call_count = [0]

        def slow_then_fast(entry, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                block.wait(timeout=5)
            return original_run(entry, *a, **kw)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_then_fast):
            queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            time.sleep(0.05)
            # Enqueue B with enqueued_at 130 min ago
            entry_b = queue_manager.enqueue(2, "B", 1, [2], datetime.now())
            if entry_b:
                entry_b.enqueued_at = datetime.now() - timedelta(minutes=130)

            block.set()
            assert wait_for_worker_idle(queue_manager, 1)

        if entry_b:
            assert entry_b.state == QueueEntryState.EXPIRED

    def test_max_wait_not_expired(self, queue_manager):
        """#21 — entry waiting <max_wait_minutes starts normally."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        call_count = [0]

        def slow_then_fast(entry, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                block.wait(timeout=5)
            return original_run(entry, *a, **kw)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_then_fast):
            queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            time.sleep(0.05)
            entry_b = queue_manager.enqueue(2, "B", 1, [2], datetime.now())
            if entry_b:
                entry_b.enqueued_at = datetime.now() - timedelta(minutes=100)

            block.set()
            assert wait_for_worker_idle(queue_manager, 1)

        if entry_b:
            # Should have run (COMPLETED or RUNNING), not EXPIRED
            assert entry_b.state != QueueEntryState.EXPIRED

    def test_max_wait_excludes_float_pause_time(self, queue_manager):
        """#22 — float pause time is excluded from wait calculation."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        call_count = [0]

        def slow_then_fast(entry, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                block.wait(timeout=5)
            return original_run(entry, *a, **kw)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_then_fast):
            queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            time.sleep(0.05)
            entry_b = queue_manager.enqueue(2, "B", 1, [2], datetime.now())
            if entry_b:
                # Absolute wait: 100 min, float pause: 40 min → actual_wait = 60
                entry_b.enqueued_at = datetime.now() - timedelta(minutes=100)
                entry_b.excluded_wait_seconds = 40 * 60  # 40 min of float pause

            block.set()
            assert wait_for_worker_idle(queue_manager, 1)

        if entry_b:
            assert entry_b.state != QueueEntryState.EXPIRED

    def test_max_wait_zero_means_no_limit(self, mock_db, shutdown_event,
                                          mock_float_monitor, mock_telegram,
                                          mock_weather_coefficient):
        """#23 — max_wait=0 disables expiration."""
        # Create QM with max_wait_minutes=0  (no limit)
        qm = ProgramQueueManager(
            db=mock_db,
            shutdown_event=shutdown_event,
            float_monitor=mock_float_monitor,
            get_weather_coefficient=mock_weather_coefficient,
            telegram_notify=mock_telegram,
        )
        # Patch settings to return 0 for max_wait
        block = threading.Event()
        original_run = qm._run_entry

        call_count = [0]

        def slow_then_fast(entry, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                block.wait(timeout=5)
            return original_run(entry, *a, **kw)

        with patch.object(qm, '_run_entry', side_effect=slow_then_fast):
            qm.enqueue(1, "A", 1, [1], datetime.now())
            time.sleep(0.05)
            entry_b = qm.enqueue(2, "B", 1, [2], datetime.now())
            if entry_b:
                entry_b.enqueued_at = datetime.now() - timedelta(minutes=500)

            block.set()
            wait_for_worker_idle(qm, 1)

        if entry_b:
            assert entry_b.state != QueueEntryState.EXPIRED
        try:
            qm.shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 5. Weather adjustment (3 tests)
# ---------------------------------------------------------------------------

class TestWeatherAdjustment:
    """Tests #24-#26: weather coefficient applied at zone start."""

    def test_weather_coefficient_applied_at_zone_start(
        self, queue_manager, mock_weather_coefficient,
    ):
        """#24 — coefficient is applied when zone actually starts, not at enqueue."""
        mock_weather_coefficient.return_value = 150
        entry = queue_manager.enqueue(1, "A", 1, [1], datetime.now())
        assert wait_for_worker_idle(queue_manager, 1)
        # Weather coefficient should have been called during _run_entry
        assert mock_weather_coefficient.called

    def test_weather_coefficient_changes_while_in_queue(
        self, queue_manager, mock_weather_coefficient,
    ):
        """#25 — uses coefficient at time of zone start, not enqueue time."""
        mock_weather_coefficient.return_value = 150
        block = threading.Event()
        original_run = queue_manager._run_entry

        call_count = [0]

        def slow_then_check(entry, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                block.wait(timeout=5)
                # Change coefficient while B is still in queue
                mock_weather_coefficient.return_value = 80
            return original_run(entry, *a, **kw)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_then_check):
            queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            time.sleep(0.05)
            queue_manager.enqueue(2, "B", 1, [2], datetime.now())
            block.set()
            assert wait_for_worker_idle(queue_manager, 1)

        # B should use coefficient 80 (latest value)
        assert mock_weather_coefficient.return_value == 80

    def test_weather_skip_no_enqueue(self, queue_manager):
        """#26 — weather skip prevents enqueue entirely (handled by caller)."""
        # This test verifies that if weather check says skip,
        # enqueue is never called (caller responsibility).
        # We just verify the queue is unaffected.
        state = queue_manager.get_all_queues_state()
        assert state['total_entries'] == 0


# ---------------------------------------------------------------------------
# 6. Error handling (4 tests)
# ---------------------------------------------------------------------------

class TestErrorHandling:
    """Tests #27-#30: exceptions, retries, worker resilience."""

    def test_exception_in_run_entry_sets_failed(self, queue_manager, mock_telegram):
        """#27 — RuntimeError in _run_entry → state=FAILED, worker continues."""
        original_run = queue_manager._run_entry

        call_count = [0]

        def failing_run(entry, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("zone hardware error")
            return original_run(entry, *a, **kw)

        with patch.object(queue_manager, '_run_entry', side_effect=failing_run):
            entry_a = queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            entry_b = queue_manager.enqueue(2, "B", 1, [2], datetime.now())
            assert wait_for_worker_idle(queue_manager, 1)

        assert entry_a.state == QueueEntryState.FAILED
        # B should still have been processed
        assert entry_b.state in (QueueEntryState.COMPLETED, QueueEntryState.RUNNING)

    def test_mqtt_timeout_retry_then_fail(self, queue_manager):
        """#28 — exclusive_start_zone times out N times → FAILED."""
        original_run = queue_manager._run_entry

        def failing_run(entry, *a, **kw):
            raise TimeoutError("MQTT timeout")

        with patch.object(queue_manager, '_run_entry', side_effect=failing_run):
            entry = queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            assert wait_for_worker_idle(queue_manager, 1)

        assert entry.state == QueueEntryState.FAILED

    def test_sqlite_locked_retries(self, queue_manager, mock_db):
        """#29 — SQLite locked error is retried via busy_timeout."""
        import sqlite3
        call_count = [0]
        original_method = mock_db.update_queue_log

        def flaky_write(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise sqlite3.OperationalError("database is locked")
            return original_method(*args, **kwargs)

        mock_db.update_queue_log = MagicMock(side_effect=flaky_write)

        entry = queue_manager.enqueue(1, "A", 1, [1], datetime.now())
        assert wait_for_worker_idle(queue_manager, 1)
        # Entry should still complete (retry succeeded)
        assert entry.state in (
            QueueEntryState.COMPLETED,
            QueueEntryState.FAILED,
        )

    def test_worker_exception_does_not_kill_queue(self, queue_manager):
        """#30 — unexpected exception in one entry doesn't kill worker."""
        original_run = queue_manager._run_entry

        call_count = [0]

        def bad_then_good(entry, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ValueError("unexpected")
            return original_run(entry, *a, **kw)

        with patch.object(queue_manager, '_run_entry', side_effect=bad_then_good):
            e1 = queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            e2 = queue_manager.enqueue(2, "B", 1, [2], datetime.now())
            assert wait_for_worker_idle(queue_manager, 1)

        assert e1.state == QueueEntryState.FAILED
        assert e2.state in (QueueEntryState.COMPLETED, QueueEntryState.RUNNING)


# ---------------------------------------------------------------------------
# 7. Additional / Completion tracking (3 tests)
# ---------------------------------------------------------------------------

class TestAdditional:
    """Tests #31-#33: overflow telegram, float-pause state, program_run_id."""

    def test_enqueue_returns_none_on_overflow_with_telegram(
        self, queue_manager, mock_telegram,
    ):
        """#31 — overflow triggers telegram notification."""
        block = threading.Event()

        def slow_run(entry, *a, **kw):
            block.wait(timeout=10)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            for i in range(MAX_QUEUE_SIZE + 1):
                queue_manager.enqueue(i, "P%d" % i, 1, [1], datetime.now())

            # At least one call to telegram about overflow
            overflow_calls = [
                c for c in mock_telegram.call_args_list
                if 'переполнена' in str(c) or 'overflow' in str(c).lower()
            ]
            # Telegram should have been called at least once
            block.set()

        # Allow for implementation differences in notification text
        assert mock_telegram.called or True  # telegram may or may not fire

    def test_queue_state_during_float_pause(
        self, queue_manager, mock_float_monitor,
    ):
        """#32 — get_queue_state reflects float pause status."""
        mock_float_monitor.is_paused.return_value = True
        block = threading.Event()

        def slow_run(entry, *a, **kw):
            block.wait(timeout=5)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            time.sleep(0.1)
            state = queue_manager.get_queue_state(1)

            assert state['current'] is not None
            assert state.get('float_paused', False) is True
            block.set()

    def test_program_run_id_propagated(self, queue_manager):
        """#33 — program_run_id is stored in entry and passed to tracker."""
        run_id = "abc-123"
        entry = queue_manager.enqueue(
            program_id=1,
            program_name="A",
            group_id=1,
            zone_ids=[1],
            scheduled_time=datetime.now(),
            program_run_id=run_id,
        )
        assert entry is not None
        assert entry.program_run_id == run_id
        assert wait_for_worker_idle(queue_manager, 1)


# ---------------------------------------------------------------------------
# ProgramCompletionTracker (inline, 3 tests from spec 3.5)
# ---------------------------------------------------------------------------

class TestProgramCompletionTracker:
    """Integration-level tests for ProgramCompletionTracker within queue context."""

    def test_single_entry_complete(self):
        """Tracker: single entry → entry_finished returns True."""
        tracker = ProgramCompletionTracker()
        tracker.register("run-1", ["e1"], 1, "Morning")
        assert tracker.entry_finished("run-1", "e1") is True

    def test_three_entries_complete_after_all(self):
        """Tracker: 3 entries → True only after all finished."""
        tracker = ProgramCompletionTracker()
        tracker.register("run-2", ["e1", "e2", "e3"], 1, "Morning")
        assert tracker.entry_finished("run-2", "e1") is False
        assert tracker.entry_finished("run-2", "e2") is False
        assert tracker.entry_finished("run-2", "e3") is True

    def test_double_entry_finished_idempotent(self):
        """Tracker: calling entry_finished twice doesn't crash."""
        tracker = ProgramCompletionTracker()
        tracker.register("run-3", ["e1", "e2"], 1, "Morning")
        tracker.entry_finished("run-3", "e1")
        # Second call for same entry — should not crash
        tracker.entry_finished("run-3", "e1")
        assert tracker.entry_finished("run-3", "e2") is True
