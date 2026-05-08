"""ProgramQueueManager + ProgramCompletionTracker.

Per-group FIFO queue with dedicated worker threads.
Python 3.9 compatible.
"""
import logging
import threading
import uuid
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Deque, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

MAX_QUEUE_SIZE = 20
DEFAULT_MAX_WAIT_MINUTES = 120


def _audit_queue_transition(entry, new_state, prev_state=None, extra=None):
    """Best-effort debug emit for QueueEntryState transitions.

    Gated behind ``settings.logging.debug`` so audit_log doesn't blow up
    under heavy program scheduling.
    """
    try:
        from services.audit import debug_audit
        payload = {
            'entry_id': getattr(entry, 'entry_id', None),
            'program_id': getattr(entry, 'program_id', None),
            'program_name': getattr(entry, 'program_name', None),
            'group_id': getattr(entry, 'group_id', None),
            'zone_ids': list(getattr(entry, 'zone_ids', None) or []),
            'from': prev_state.value if prev_state else None,
            'to': new_state.value if new_state else None,
        }
        if extra:
            payload.update(extra)
        debug_audit(
            action_type='program_queue_transition',
            source='scheduler',
            target=f'group:{getattr(entry, "group_id", "?")}',
            payload=payload,
        )
    except Exception:  # noqa: BLE001
        logger.exception("program_queue_transition audit failed")


def _audit_program_run(action, entry, extra=None):
    """Always-on audit for program_run_started / program_run_completed."""
    try:
        from services.audit import record_audit
        payload = {
            'entry_id': getattr(entry, 'entry_id', None),
            'program_id': getattr(entry, 'program_id', None),
            'program_name': getattr(entry, 'program_name', None),
            'group_id': getattr(entry, 'group_id', None),
            'zone_ids': list(getattr(entry, 'zone_ids', None) or []),
            'program_run_id': getattr(entry, 'program_run_id', None),
        }
        if extra:
            payload.update(extra)
        record_audit(
            action_type=action,
            source='scheduler',
            target=f'group:{getattr(entry, "group_id", "?")}',
            payload=payload,
            actor='system',
        )
    except Exception:  # noqa: BLE001
        logger.exception("program_run audit failed (action=%s)", action)


class QueueEntryState(Enum):
    WAITING = "waiting"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


@dataclass
class QueueEntry:
    entry_id: str
    program_id: int
    program_name: str
    group_id: int
    zone_ids: List[int]
    scheduled_time: Optional[datetime]
    state: QueueEntryState = QueueEntryState.WAITING
    enqueued_at: datetime = field(default_factory=datetime.now)
    excluded_wait_seconds: float = 0.0
    program_run_id: Optional[str] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class GroupQueue:
    group_id: int
    lock: threading.Lock = field(default_factory=threading.Lock)
    queue: Deque[QueueEntry] = field(default_factory=deque)
    current: Optional[QueueEntry] = None
    worker_thread: Optional[threading.Thread] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    new_item_event: threading.Event = field(default_factory=threading.Event)


class ProgramQueueManager:
    """Per-group FIFO queue manager with worker threads."""

    def __init__(
        self,
        db=None,
        shutdown_event=None,
        float_monitor=None,
        get_weather_coefficient=None,
        telegram_notify=None,
        max_wait_minutes=0,
        max_queue_size=MAX_QUEUE_SIZE,
    ):
        self._db = db
        self._shutdown_event = shutdown_event or threading.Event()
        self._float_monitor = float_monitor
        self._get_weather_coefficient = get_weather_coefficient or (lambda: 100)
        self._telegram_notify = telegram_notify
        self._max_wait_minutes = max_wait_minutes
        self._max_queue_size = max_queue_size

        self._global_lock = threading.Lock()  # protects _queues dict
        self._queues = {}  # type: Dict[int, GroupQueue]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(
        self,
        program_id,       # type: int
        program_name,     # type: str
        group_id,         # type: int
        zone_ids,         # type: List[int]
        scheduled_time=None,  # type: Optional[datetime]
        program_run_id=None,  # type: Optional[str]
    ):
        # type: (...) -> Optional[QueueEntry]
        """Add entry to the group queue. Returns None if queue full."""
        entry = QueueEntry(
            entry_id=str(uuid.uuid4()),
            program_id=program_id,
            program_name=program_name,
            group_id=group_id,
            zone_ids=list(zone_ids),
            scheduled_time=scheduled_time,
            program_run_id=program_run_id,
        )

        with self._global_lock:
            gq = self._queues.get(group_id)
            if gq is None:
                gq = GroupQueue(group_id=group_id)
                self._queues[group_id] = gq

        with gq.lock:
            # Count: current (if any) + queued
            total = len(gq.queue) + (1 if gq.current is not None else 0)
            if total >= self._max_queue_size:
                if self._telegram_notify:
                    try:
                        self._telegram_notify(
                            "Очередь группы %d переполнена (макс %d)" % (group_id, self._max_queue_size)
                        )
                    except Exception:
                        pass
                return None

            gq.queue.append(entry)
            gq.new_item_event.set()

            # Start worker if not running
            if gq.worker_thread is None or not gq.worker_thread.is_alive():
                gq.cancel_event.clear()
                t = threading.Thread(
                    target=self._worker,
                    args=(group_id,),
                    name="queue-worker-%d" % group_id,
                    daemon=True,
                )
                gq.worker_thread = t
                t.start()

        return entry

    def get_queue_state(self, group_id):
        # type: (int) -> dict
        """Return snapshot of a single group queue."""
        with self._global_lock:
            gq = self._queues.get(group_id)

        if gq is None:
            return {
                'group_id': group_id,
                'current': None,
                'queue': [],
                'queue_length': 0,
            }

        with gq.lock:
            current = gq.current
            queue_list = list(gq.queue)
            queue_length = len(queue_list)

        float_paused = False
        if self._float_monitor:
            try:
                float_paused = self._float_monitor.is_paused()
            except Exception:
                pass

        return {
            'group_id': group_id,
            'current': current,
            'queue': queue_list,
            'queue_length': queue_length,
            'float_paused': float_paused,
        }

    def get_all_queues_state(self):
        # type: () -> dict
        """Return snapshot of all group queues. K2: copy dict under _global_lock, iterate copy."""
        with self._global_lock:
            queues_copy = dict(self._queues)

        result = {}
        total_entries = 0
        active_workers = 0

        for gid, gq in queues_copy.items():
            with gq.lock:
                current = gq.current
                queue_list = list(gq.queue)
                n = len(queue_list) + (1 if current is not None else 0)
                worker_alive = gq.worker_thread is not None and gq.worker_thread.is_alive()

            result[gid] = {
                'group_id': gid,
                'current': current,
                'queue': queue_list,
                'queue_length': len(queue_list),
            }
            total_entries += n
            if worker_alive:
                active_workers += 1

        return {
            'queues': result,
            'total_entries': total_entries,
            'active_workers': active_workers,
        }

    def cancel_entry(self, entry_id):
        # type: (str) -> bool
        """Cancel a single entry by entry_id. Returns True if found."""
        with self._global_lock:
            queues_copy = dict(self._queues)

        for gid, gq in queues_copy.items():
            with gq.lock:
                # Check waiting entries in queue
                for entry in gq.queue:
                    if entry.entry_id == entry_id:
                        prev_state = entry.state
                        entry.state = QueueEntryState.CANCELLED
                        entry.cancel_event.set()
                        _audit_queue_transition(entry, QueueEntryState.CANCELLED,
                                                prev_state=prev_state,
                                                extra={'cancel_source': 'cancel_entry'})
                        return True

                # Check current running entry
                if gq.current is not None and gq.current.entry_id == entry_id:
                    prev_state = gq.current.state
                    gq.current.state = QueueEntryState.CANCELLED
                    gq.current.cancel_event.set()
                    _audit_queue_transition(gq.current, QueueEntryState.CANCELLED,
                                            prev_state=prev_state,
                                            extra={'cancel_source': 'cancel_entry_running'})
                    # Wake up worker if waiting on float resume
                    resume_ev = None
                    if self._float_monitor:
                        try:
                            resume_ev = self._float_monitor.get_resume_event()
                        except Exception:
                            pass
                    if resume_ev:
                        resume_ev.set()
                    return True

        return False

    def cancel_program(self, program_id):
        # type: (int) -> int
        """Cancel all entries for a program. Returns count of cancelled."""
        count = 0
        with self._global_lock:
            queues_copy = dict(self._queues)

        for gid, gq in queues_copy.items():
            with gq.lock:
                for entry in gq.queue:
                    if entry.program_id == program_id and entry.state == QueueEntryState.WAITING:
                        entry.state = QueueEntryState.CANCELLED
                        entry.cancel_event.set()
                        count += 1

                if (gq.current is not None
                        and gq.current.program_id == program_id
                        and gq.current.state == QueueEntryState.RUNNING):
                    gq.current.state = QueueEntryState.CANCELLED
                    gq.current.cancel_event.set()
                    count += 1

        return count

    def cancel_group(self, group_id):
        # type: (int) -> int
        """Cancel all entries in a group and wait for worker. K1: worker does OFF in finally."""
        count = 0
        worker = None

        with self._global_lock:
            gq = self._queues.get(group_id)

        if gq is None:
            return 0

        with gq.lock:
            gq.cancel_event.set()

            for entry in gq.queue:
                if entry.state == QueueEntryState.WAITING:
                    entry.state = QueueEntryState.CANCELLED
                    entry.cancel_event.set()
                    count += 1

            if gq.current is not None and gq.current.state == QueueEntryState.RUNNING:
                gq.current.state = QueueEntryState.CANCELLED
                gq.current.cancel_event.set()
                count += 1

            worker = gq.worker_thread

        # Wait for worker outside lock
        if worker is not None and worker.is_alive():
            worker.join(timeout=10.0)

        return count

    def shutdown(self, timeout=10.0):
        # type: (float) -> None
        """Stop all workers gracefully."""
        self._shutdown_event.set()

        with self._global_lock:
            queues_copy = dict(self._queues)

        for gid, gq in queues_copy.items():
            with gq.lock:
                gq.cancel_event.set()
                gq.new_item_event.set()
                for entry in gq.queue:
                    entry.cancel_event.set()
                if gq.current is not None:
                    gq.current.cancel_event.set()

        for gid, gq in queues_copy.items():
            worker = gq.worker_thread
            if worker is not None and worker.is_alive():
                worker.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Worker (runs in dedicated thread per group)
    # ------------------------------------------------------------------

    def _worker(self, group_id):
        # type: (int) -> None
        """FIFO worker for a single group. K2: never acquires _global_lock."""
        with self._global_lock:
            gq = self._queues.get(group_id)
        if gq is None:
            return

        while not self._shutdown_event.is_set() and not gq.cancel_event.is_set():
            entry = None
            with gq.lock:
                # Skip cancelled entries
                while gq.queue:
                    candidate = gq.queue[0]
                    if candidate.state == QueueEntryState.CANCELLED:
                        gq.queue.popleft()
                        continue
                    break

                if gq.queue:
                    entry = gq.queue.popleft()
                else:
                    gq.current = None
                    gq.new_item_event.clear()

            if entry is None:
                # Wait for new items or shutdown
                gq.new_item_event.wait(timeout=1.0)
                with gq.lock:
                    if not gq.queue:
                        gq.current = None
                        return  # Queue empty, worker exits
                    gq.new_item_event.clear()
                continue

            # Check max_wait expiration (K6)
            if self._max_wait_minutes > 0 and entry.state == QueueEntryState.WAITING:
                elapsed = (datetime.now() - entry.enqueued_at).total_seconds()
                effective_wait = elapsed - entry.excluded_wait_seconds
                if effective_wait > self._max_wait_minutes * 60:
                    prev_state = entry.state
                    entry.state = QueueEntryState.EXPIRED
                    _audit_queue_transition(entry, QueueEntryState.EXPIRED,
                                            prev_state=prev_state,
                                            extra={'effective_wait_sec': int(effective_wait)})
                    logger.info("Entry %s expired (waited %.0fs)", entry.entry_id, effective_wait)
                    continue

            # Check if cancelled while waiting
            if entry.state == QueueEntryState.CANCELLED:
                continue

            # Set as current and RUNNING
            prev_state = entry.state
            entry.state = QueueEntryState.RUNNING
            with gq.lock:
                gq.current = entry
            _audit_queue_transition(entry, QueueEntryState.RUNNING, prev_state=prev_state)
            # Always-on audit: a program run actually started.
            _audit_program_run('program_run_started', entry)

            try:
                self._run_entry(entry)
                if entry.state == QueueEntryState.RUNNING:
                    entry.state = QueueEntryState.COMPLETED
                    _audit_queue_transition(entry, QueueEntryState.COMPLETED,
                                            prev_state=QueueEntryState.RUNNING)
                    _audit_program_run('program_run_completed', entry,
                                       extra={'final_state': 'completed'})
                elif entry.state == QueueEntryState.CANCELLED:
                    # Cancellation already audited by canceler — emit completion
                    # so external observers see the run terminated.
                    _audit_program_run('program_run_completed', entry,
                                       extra={'final_state': 'cancelled'})
            except Exception as exc:
                logger.exception("Entry %s failed", entry.entry_id)
                if entry.state not in (QueueEntryState.CANCELLED, QueueEntryState.COMPLETED):
                    prev_state_f = entry.state
                    entry.state = QueueEntryState.FAILED
                    _audit_queue_transition(entry, QueueEntryState.FAILED,
                                            prev_state=prev_state_f,
                                            extra={'error': str(exc)[:256]})
                    _audit_program_run('program_run_completed', entry,
                                       extra={'final_state': 'failed',
                                              'error': str(exc)[:256]})
            finally:
                with gq.lock:
                    if gq.current is entry:
                        gq.current = None

        # Shutdown/cancel path: mark remaining as cancelled
        with gq.lock:
            while gq.queue:
                e = gq.queue.popleft()
                if e.state == QueueEntryState.WAITING:
                    e.state = QueueEntryState.CANCELLED
            gq.current = None

    def _run_entry(self, entry):
        # type: (QueueEntry) -> None
        """Execute all zones in an entry sequentially. K5: weather coefficient at zone start."""
        for zone_id in entry.zone_ids:
            if entry.cancel_event.is_set() or self._shutdown_event.is_set():
                return

            # K3: Float pause — worker waits for resume
            if self._float_monitor:
                while self._float_monitor.is_paused():
                    if entry.cancel_event.is_set() or self._shutdown_event.is_set():
                        return
                    resume_event = self._float_monitor.get_resume_event()
                    # Wait on resume_event OR cancel_event
                    # Poll with short timeout to check cancel
                    entry.cancel_event.wait(timeout=0.5)
                    if entry.cancel_event.is_set():
                        return

            # K5: get weather coefficient at zone start time
            coeff = 100
            if self._get_weather_coefficient:
                try:
                    coeff = self._get_weather_coefficient()
                except Exception:
                    pass

            # TODO: actual zone control integration
            # For now, entry completes immediately (mocked in tests)
            logger.debug(
                "Running zone %d for entry %s (coeff=%d)",
                zone_id, entry.entry_id, coeff,
            )


# ------------------------------------------------------------------
# ProgramCompletionTracker
# ------------------------------------------------------------------

class ProgramCompletionTracker:
    """Track completion of multi-group program runs.

    Thread-safe. A program run is complete when all its entries have finished.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # run_id -> {program_id, program_name, entry_ids: set, finished: set}
        self._runs = {}  # type: Dict[str, Dict]

    def register(self, program_run_id, entry_ids, program_id=None, program_name=None):
        # type: (str, List[str], Optional[int], Optional[str]) -> None
        """Register a program run with its expected entry IDs."""
        with self._lock:
            self._runs[program_run_id] = {
                'program_id': program_id,
                'program_name': program_name,
                'entry_ids': set(entry_ids),
                'finished': set(),
            }

    def entry_finished(self, program_run_id, entry_id):
        # type: (str, str) -> bool
        """Mark entry as finished. Returns True if ALL entries for the run are now complete."""
        with self._lock:
            run = self._runs.get(program_run_id)
            if run is None:
                return True  # Unknown run — treat as complete (single-group)

            run['finished'].add(entry_id)
            all_done = run['finished'] >= run['entry_ids']

            if all_done:
                del self._runs[program_run_id]

            return all_done

    def is_program_complete(self, program_run_id):
        # type: (str) -> bool
        """Check if a program run is complete (or unknown)."""
        with self._lock:
            return program_run_id not in self._runs

    def get_pending(self):
        # type: () -> Dict[str, Dict]
        """Return dict of pending (incomplete) program runs."""
        with self._lock:
            result = {}
            for run_id, data in self._runs.items():
                result[run_id] = {
                    'program_id': data['program_id'],
                    'program_name': data['program_name'],
                    'total': len(data['entry_ids']),
                    'finished': len(data['finished']),
                    'remaining': len(data['entry_ids'] - data['finished']),
                }
            return result

    def get_program_status(self, program_run_id):
        # type: (str) -> Optional[dict]
        """Get status of a specific program run."""
        with self._lock:
            data = self._runs.get(program_run_id)
            if data is None:
                return None
            return {
                'program_id': data['program_id'],
                'program_name': data['program_name'],
                'total': len(data['entry_ids']),
                'finished': len(data['finished']),
                'complete': data['finished'] >= data['entry_ids'],
            }
