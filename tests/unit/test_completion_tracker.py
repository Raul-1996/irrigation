"""Tests for ProgramCompletionTracker — TDD spec section 3.5.

Контракт (из спеки):
    ProgramCompletionTracker:
        register(program_run_id, entry_ids, program_id, program_name)
        entry_finished(program_run_id, entry_id) -> bool
        is_program_complete(program_run_id) -> bool
        get_pending() -> Dict[str, Dict]
"""
import os
import threading
import pytest
from unittest.mock import MagicMock, patch

os.environ['TESTING'] = '1'


# ---------------------------------------------------------------------------
# Stub: ProgramCompletionTracker ещё не реализован.
# Тесты определяют контракт; реализация должна импортироваться из
# services.program_queue.  Пока модуль не создан, тесты xfail.
# ---------------------------------------------------------------------------
_tracker_cls = None
try:
    from services.program_queue import ProgramCompletionTracker
    _tracker_cls = ProgramCompletionTracker
except ImportError:
    pass


def _make_tracker():
    """Создаёт ProgramCompletionTracker (или помечает тест xfail если модуль отсутствует)."""
    if _tracker_cls is None:
        pytest.xfail("ProgramCompletionTracker ещё не реализован (services/program_queue.py)")
    return _tracker_cls()


# === Test 1: Single entry → complete ===

class TestCompletionTracker:
    """ProgramCompletionTracker — 6 тестов по спеке 3.5."""

    def test_single_entry_complete(self):
        """register 1 entry → entry_finished → True (программа завершена)."""
        tracker = _make_tracker()

        run_id = "run-001"
        entry_id = "e-aaa"
        tracker.register(run_id, [entry_id], program_id=1, program_name="Утро")

        result = tracker.entry_finished(run_id, entry_id)
        assert result is True, "Единственная entry завершена — программа должна быть complete"
        assert tracker.is_program_complete(run_id) is True

    # === Test 2: Three entries (3 groups) → complete after all ===

    def test_three_entries_complete_after_all(self):
        """register 3 entries → entry_finished по одной → True только после третьей."""
        tracker = _make_tracker()

        run_id = "run-002"
        entries = ["e1", "e2", "e3"]
        tracker.register(run_id, entries, program_id=5, program_name="Вечер")

        assert tracker.entry_finished(run_id, "e1") is False
        assert tracker.entry_finished(run_id, "e2") is False
        assert tracker.entry_finished(run_id, "e3") is True
        assert tracker.is_program_complete(run_id) is True

    # === Test 3: Cancelled entry still completes program ===

    def test_cancelled_entry_still_completes_program(self):
        """Отменённая entry тоже считается 'завершённой' для tracker (partial).

        entry_finished вызывается при любом финальном состоянии entry
        (completed, cancelled, expired, failed) — вызывающий код отвечает за вызов.
        """
        tracker = _make_tracker()

        run_id = "run-003"
        entries = ["e1", "e2", "e3"]
        tracker.register(run_id, entries, program_id=7, program_name="Ночь")

        # e1 completed
        assert tracker.entry_finished(run_id, "e1") is False
        # e2 cancelled — всё равно вызываем entry_finished
        assert tracker.entry_finished(run_id, "e2") is False
        # e3 completed
        assert tracker.entry_finished(run_id, "e3") is True

    # === Test 4: Expired entry completes program ===

    def test_expired_entry_completes_program(self):
        """register [e1, e2]. e1 expired → entry_finished(e1). e2 completed → True."""
        tracker = _make_tracker()

        run_id = "run-004"
        tracker.register(run_id, ["e1", "e2"], program_id=10, program_name="Полдень")

        # e1 expired
        assert tracker.entry_finished(run_id, "e1") is False
        # e2 completed
        assert tracker.entry_finished(run_id, "e2") is True

    # === Test 5: program_finish log on complete ===

    def test_program_finish_log_on_complete(self):
        """entry_finished() возвращает True → вызывающий код создаёт program_finish лог.

        Тест проверяет, что возвращаемое значение True позволяет вызывающему коду
        определить момент завершения программы.
        """
        tracker = _make_tracker()

        run_id = "run-005"
        tracker.register(run_id, ["e1", "e2"], program_id=3, program_name="Рассвет")

        tracker.entry_finished(run_id, "e1")
        all_done = tracker.entry_finished(run_id, "e2")

        assert all_done is True, (
            "entry_finished должен вернуть True когда все entries завершены — "
            "это сигнал вызывающему коду создать program_finish лог"
        )
        # После завершения программы — она не должна быть в pending
        pending = tracker.get_pending()
        assert run_id not in pending, "Завершённая программа не должна быть в get_pending()"

    # === Test 6: Double entry_finished → idempotent ===

    def test_double_entry_finished_idempotent(self):
        """entry_finished(run_id, e1) дважды → без ошибки, состояние корректно."""
        tracker = _make_tracker()

        run_id = "run-006"
        tracker.register(run_id, ["e1", "e2"], program_id=2, program_name="Закат")

        # Первый вызов
        result1 = tracker.entry_finished(run_id, "e1")
        assert result1 is False

        # Повторный вызов той же entry — идемпотентно
        result2 = tracker.entry_finished(run_id, "e1")
        assert result2 is False, "Повторный вызов не должен менять результат"

        # Программа ещё не complete (e2 не завершена)
        assert tracker.is_program_complete(run_id) is False

        # Завершаем e2
        result3 = tracker.entry_finished(run_id, "e2")
        assert result3 is True


class TestCompletionTrackerEdgeCases:
    """Дополнительные edge cases для ProgramCompletionTracker."""

    def test_unknown_run_id_returns_true(self):
        """is_program_complete для неизвестного run_id → True (не трекается)."""
        tracker = _make_tracker()
        assert tracker.is_program_complete("unknown-run") is True

    def test_entry_finished_unknown_run_id_returns_true(self):
        """entry_finished для неизвестного run_id → True (single-group, не трекается)."""
        tracker = _make_tracker()
        result = tracker.entry_finished("unknown-run", "some-entry")
        assert result is True

    def test_get_pending_shows_incomplete(self):
        """get_pending() показывает незавершённые программы."""
        tracker = _make_tracker()

        run_id = "run-pending"
        tracker.register(run_id, ["e1", "e2"], program_id=99, program_name="Тест")
        tracker.entry_finished(run_id, "e1")

        pending = tracker.get_pending()
        assert run_id in pending
        assert pending[run_id]['program_name'] == "Тест"

    def test_thread_safety_register_and_finish(self):
        """Параллельные register + entry_finished — без crash."""
        tracker = _make_tracker()
        errors = []

        def worker(idx):
            try:
                run_id = "run-ts-%d" % idx
                entries = ["e%d-1" % idx, "e%d-2" % idx]
                tracker.register(run_id, entries, program_id=idx, program_name="P%d" % idx)
                tracker.entry_finished(run_id, entries[0])
                tracker.entry_finished(run_id, entries[1])
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, "Thread safety violation: %s" % errors
