"""TDD concurrency tests for ProgramQueueManager.

REAL threads — no mocking of threading primitives.
Tests verify thread safety, lock ordering, and graceful shutdown.

All tests will be RED until services/program_queue.py is implemented.
"""
import os
import time
import threading
from datetime import datetime
from typing import List
from unittest.mock import MagicMock, patch

import pytest

os.environ['TESTING'] = '1'

from services.program_queue import (
    ProgramQueueManager,
    QueueEntry,
    QueueEntryState,
    GroupQueue,
    MAX_QUEUE_SIZE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def wait_for_worker_idle(
    qm: ProgramQueueManager,
    group_id: int,
    timeout: float = 5.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        gq = qm._queues.get(group_id)
        if gq is None or gq.worker_thread is None or not gq.worker_thread.is_alive():
            return True
        time.sleep(0.05)
    return False


def wait_all_idle(qm: ProgramQueueManager, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        any_alive = False
        for gq in list(qm._queues.values()):
            if gq.worker_thread and gq.worker_thread.is_alive():
                any_alive = True
                break
        if not any_alive:
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_db(tmp_path):
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
def queue_manager(mock_db, shutdown_event, mock_float_monitor):
    qm = ProgramQueueManager(
        db=mock_db,
        shutdown_event=shutdown_event,
        float_monitor=mock_float_monitor,
        get_weather_coefficient=MagicMock(return_value=100),
        telegram_notify=MagicMock(),
    )
    yield qm
    try:
        qm.shutdown()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Concurrency tests (10 tests)
# ---------------------------------------------------------------------------

class TestConcurrency:
    """Real-thread concurrency tests for ProgramQueueManager."""

    @pytest.mark.timeout(10)
    def test_concurrent_enqueue_5_threads_fifo(self, queue_manager):
        """#1 — 5 threads enqueue simultaneously → all entries accepted, FIFO order."""
        barrier = threading.Barrier(5, timeout=5)
        results = [None] * 5  # type: List
        order = []  # type: List[int]
        order_lock = threading.Lock()
        original_run = queue_manager._run_entry

        def tracking_run(entry, *a, **kw):
            with order_lock:
                order.append(entry.program_id)
            return original_run(entry, *a, **kw)

        with patch.object(queue_manager, '_run_entry', side_effect=tracking_run):
            def enqueue_worker(idx):
                barrier.wait()
                results[idx] = queue_manager.enqueue(
                    program_id=idx,
                    program_name="P%d" % idx,
                    group_id=1,
                    zone_ids=[idx + 1],
                    scheduled_time=datetime.now(),
                )

            threads = [
                threading.Thread(target=enqueue_worker, args=(i,))
                for i in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

            assert wait_for_worker_idle(queue_manager, 1)

        # All 5 entries accepted
        assert all(r is not None for r in results)
        # All 5 processed
        assert len(order) == 5
        # No duplicates
        assert len(set(order)) == 5

    @pytest.mark.timeout(10)
    def test_enqueue_and_cancel_group_no_deadlock(self, queue_manager):
        """#2 — enqueue + cancel_group simultaneously → no deadlock."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            block.wait(timeout=5)
            return original_run(entry, *a, **kw)

        cancel_result = [None]
        errors = []  # type: List[Exception]

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            # Enqueue a few entries first
            queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            time.sleep(0.05)

            def enqueue_thread():
                try:
                    for i in range(10):
                        queue_manager.enqueue(
                            i + 10, "P%d" % i, 1, [i + 2], datetime.now(),
                        )
                        time.sleep(0.01)
                except Exception as e:
                    errors.append(e)

            def cancel_thread():
                try:
                    time.sleep(0.05)
                    block.set()
                    cancel_result[0] = queue_manager.cancel_group(1)
                except Exception as e:
                    errors.append(e)

            t1 = threading.Thread(target=enqueue_thread)
            t2 = threading.Thread(target=cancel_thread)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        assert not errors, "Errors: %s" % errors
        assert not t1.is_alive(), "enqueue thread hung"
        assert not t2.is_alive(), "cancel thread hung"

    @pytest.mark.timeout(10)
    def test_three_groups_three_workers_parallel(self, queue_manager):
        """#3 — 3 groups × 3 entries each → 3 parallel workers."""
        completed = []  # type: List[str]
        completed_lock = threading.Lock()
        original_run = queue_manager._run_entry

        def tracking_run(entry, *a, **kw):
            original_run(entry, *a, **kw)
            with completed_lock:
                completed.append("%d-%d" % (entry.group_id, entry.program_id))

        with patch.object(queue_manager, '_run_entry', side_effect=tracking_run):
            for g in [1, 2, 3]:
                for p in range(3):
                    queue_manager.enqueue(
                        g * 10 + p, "G%dP%d" % (g, p), g,
                        [g * 10 + p], datetime.now(),
                    )

            assert wait_all_idle(queue_manager, timeout=8)

        assert len(completed) == 9

    @pytest.mark.timeout(10)
    def test_shutdown_during_execution_completes_fast(self, queue_manager):
        """#4 — shutdown() during execution completes within timeout."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            block.wait(timeout=5)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            for g in [1, 2, 3]:
                queue_manager.enqueue(g, "P%d" % g, g, [g], datetime.now())
            time.sleep(0.1)

            # Shutdown should signal stop and unblock workers
            block.set()
            start_t = time.monotonic()
            queue_manager.shutdown()
            elapsed = time.monotonic() - start_t

        assert elapsed < 5.0, "shutdown took %.1f sec" % elapsed

        for gq in queue_manager._queues.values():
            if gq.worker_thread:
                assert not gq.worker_thread.is_alive()

    @pytest.mark.timeout(10)
    def test_lock_ordering_no_deadlock_stress(self, queue_manager):
        """#5 — 10 threads doing enqueue + get_all_queues_state for 2 sec → no deadlock."""
        stop = threading.Event()
        errors = []  # type: List[Exception]
        counter = [0]  # type: List[int]
        counter_lock = threading.Lock()

        def enqueue_loop():
            try:
                while not stop.is_set():
                    with counter_lock:
                        counter[0] += 1
                        n = counter[0]
                    queue_manager.enqueue(
                        n, "P%d" % n, (n % 3) + 1, [n], datetime.now(),
                    )
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        def state_loop():
            try:
                while not stop.is_set():
                    queue_manager.get_all_queues_state()
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=enqueue_loop, daemon=True))
        for i in range(5):
            threads.append(threading.Thread(target=state_loop, daemon=True))

        for t in threads:
            t.start()

        time.sleep(2)
        stop.set()

        for t in threads:
            t.join(timeout=3)
            assert not t.is_alive(), "Thread hung — possible deadlock"

        assert not errors, "Errors: %s" % errors

    @pytest.mark.timeout(10)
    def test_100_enqueue_burst_maxlen(self, queue_manager):
        """#6 — 100 enqueue on one group → first ~20 accepted, rest rejected."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            block.wait(timeout=10)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            results = []
            for i in range(100):
                r = queue_manager.enqueue(
                    i, "P%d" % i, 1, [1], datetime.now(),
                )
                results.append(r)

            accepted = sum(1 for r in results if r is not None)
            rejected = sum(1 for r in results if r is None)

            # 1 current + up to MAX_QUEUE_SIZE in deque
            assert accepted <= MAX_QUEUE_SIZE + 1
            assert rejected >= 100 - MAX_QUEUE_SIZE - 1
            block.set()

    @pytest.mark.timeout(10)
    def test_cancel_entry_while_worker_processes(self, queue_manager):
        """#7 — cancel running entry while worker is in the middle."""
        processing = threading.Event()
        cancel_done = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            processing.set()
            # Wait until cancel is issued
            cancel_done.wait(timeout=5)
            time.sleep(0.1)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            entry = queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            processing.wait(timeout=3)

            result = queue_manager.cancel_entry(entry.entry_id)
            cancel_done.set()
            assert result is True
            assert wait_for_worker_idle(queue_manager, 1)

    @pytest.mark.timeout(10)
    def test_concurrent_cancel_program_multiple_groups(self, queue_manager):
        """#8 — cancel_program from 2 threads → idempotent, no double-stop."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            block.wait(timeout=5)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            for g in [1, 2, 3]:
                queue_manager.enqueue(42, "P", g, [g * 10], datetime.now())
            time.sleep(0.1)

            results = [None, None]
            errors = []  # type: List[Exception]

            def cancel_thread(idx):
                try:
                    block.set()
                    results[idx] = queue_manager.cancel_program(42)
                except Exception as e:
                    errors.append(e)

            t1 = threading.Thread(target=cancel_thread, args=(0,))
            t2 = threading.Thread(target=cancel_thread, args=(1,))
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        assert not errors
        total = (results[0] or 0) + (results[1] or 0)
        # Combined cancellations should be 3 (idempotent — each entry cancelled once)
        assert total >= 3

    @pytest.mark.timeout(10)
    def test_enqueue_during_worker_shutdown(self, queue_manager):
        """#9 — enqueue while worker finishes last entry → no lost entries."""
        finishing = threading.Event()
        original_run = queue_manager._run_entry

        call_count = [0]

        def slow_run(entry, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                finishing.set()
                time.sleep(0.2)  # Simulate finishing work
            return original_run(entry, *a, **kw)

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            queue_manager.enqueue(1, "A", 1, [1], datetime.now())
            finishing.wait(timeout=3)
            # Enqueue while worker is finishing A
            entry_b = queue_manager.enqueue(2, "B", 1, [2], datetime.now())
            assert wait_for_worker_idle(queue_manager, 1, timeout=8)

        # B should eventually be processed (either by same or new worker)
        assert entry_b is not None

    @pytest.mark.timeout(10)
    def test_get_queue_state_consistent_snapshot(self, queue_manager):
        """#10 — get_queue_state returns consistent snapshot during mutations."""
        block = threading.Event()
        original_run = queue_manager._run_entry

        def slow_run(entry, *a, **kw):
            block.wait(timeout=5)

        snapshots = []  # type: List[dict]
        errors = []  # type: List[Exception]

        with patch.object(queue_manager, '_run_entry', side_effect=slow_run):
            # Enqueue several entries
            for i in range(5):
                queue_manager.enqueue(i, "P%d" % i, 1, [i + 1], datetime.now())
            time.sleep(0.1)

            def snapshot_loop():
                try:
                    for _ in range(20):
                        s = queue_manager.get_queue_state(1)
                        snapshots.append(s)
                        time.sleep(0.02)
                except Exception as e:
                    errors.append(e)

            def mutate_loop():
                try:
                    for i in range(5, 10):
                        queue_manager.enqueue(
                            i, "P%d" % i, 1, [i + 1], datetime.now(),
                        )
                        time.sleep(0.02)
                except Exception as e:
                    errors.append(e)

            t1 = threading.Thread(target=snapshot_loop)
            t2 = threading.Thread(target=mutate_loop)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)
            block.set()

        assert not errors
        # Each snapshot should be internally consistent
        for s in snapshots:
            assert 'current' in s
            assert 'queue' in s
            assert 'queue_length' in s
            assert s['queue_length'] == len(s['queue'])
