# TDD-спека тестов: Очередь программ + Поплавок ёмкости

> Версия: 1.0
> Дата: 2026-03-30
> Зависимости: [program-queue-spec.md](program-queue-spec.md), [program-queue-review.md](program-queue-review.md)
> Python: 3.9 (без walrus, match/case, X|Y типов)

---

## 1. Философия TDD для этого проекта

### 1.1 Принципы

- **Тесты определяют контракты** — public API каждого модуля фиксируется тестами ДО написания кода
- **Каждый тест = реальный сценарий** — T1–T26 из спеки + расширения для edge cases
- **Мокаем:** MQTT (paho-mqtt), Telegram, `time.sleep` (ускорение), `time.time`/`time.monotonic` (детерминизм), SQLite — in-memory (`:memory:`) или `tmp_path`
- **НЕ мокаем:** threading (реальные потоки для concurrency-тестов)
- **Изоляция:** каждый тест — чистое состояние (fresh DB, fresh queue manager)

### 1.2 Стек тестирования

| Инструмент | Назначение |
|---|---|
| pytest | Фреймворк |
| pytest-timeout (10 сек) | Защита от зависших потоков |
| unittest.mock | Mock/patch |
| threading.Event, Barrier | Синхронизация в concurrency-тестах |
| sqlite3 `:memory:` / `tmp_path` | Тестовая БД |

### 1.3 Общие правила

- Python 3.9 типы: `Optional[]`, `Union[]`, `List[]`, `Dict[]`
- pytest-стиль: fixtures, parametrize где уместно
- Naming: `test_{module}_{scenario}_{expected_outcome}`
- Каждый тест < 5 сек (pytest-timeout = 10 сек)
- Threading-тесты: реальные потоки, но с `timeout` на join/wait

---

## 2. Контракты интерфейсов (PUBLIC API)

### 2.1 ProgramQueueManager (`services/program_queue.py`)

```python
from typing import Dict, List, Optional
from datetime import datetime
import threading

MAX_QUEUE_SIZE = 20

class QueueEntryState(Enum):
    WAITING = 'waiting'
    RUNNING = 'running'
    COMPLETED = 'completed'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'
    FAILED = 'failed'

class ProgramQueueManager:
    def __init__(
        self,
        db: 'IrrigationDB',
        shutdown_event: threading.Event,
        float_monitor: Optional['FloatMonitor'] = None,
        get_weather_coefficient: Optional[callable] = None,
        telegram_notify: Optional[callable] = None,
    ) -> None: ...

    def enqueue(
        self,
        program_id: int,
        program_name: str,
        group_id: int,
        zone_ids: List[int],
        scheduled_time: datetime,
        program_run_id: Optional[str] = None,
    ) -> Optional['QueueEntry']:
        """Добавляет entry в очередь группы.
        Возвращает QueueEntry или None при переполнении.
        """

    def get_queue_state(self, group_id: int) -> Dict:
        """Состояние очереди одной группы.
        Returns: см. секцию 6.1 (QueueStateResponse)
        """

    def get_all_queues_state(self) -> Dict:
        """Состояние всех очередей.
        Returns: см. секцию 6.2 (AllQueuesStateResponse)
        """

    def cancel_entry(self, entry_id: str) -> bool:
        """Отменяет конкретную entry. True если найдена и отменена."""

    def cancel_program(self, program_id: int) -> int:
        """Отменяет все entries программы. Возвращает кол-во отменённых."""

    def cancel_group(self, group_id: int) -> int:
        """Отменяет всё в группе, ждёт завершения worker.
        Возвращает кол-во отменённых entries."""

    def shutdown(self) -> None:
        """Graceful shutdown всех workers. Блокирует до завершения."""
```

### 2.2 FloatMonitor (`services/float_monitor.py`)

```python
from typing import Dict, Optional, List
import threading

class FloatMonitor:
    def __init__(
        self,
        db: 'IrrigationDB',
        queue_manager: Optional['ProgramQueueManager'] = None,
        telegram_notify: Optional[callable] = None,
    ) -> None: ...

    def start(self) -> None:
        """Подписывается на MQTT для всех групп с float_enabled=True."""

    def stop(self) -> None:
        """Отписывается от MQTT, останавливает таймеры."""

    def reload_group(self, group_id: int) -> None:
        """Переподписка на MQTT для одной группы (после изменения настроек)."""

    def get_state(self, group_id: int) -> Dict:
        """Returns: см. секцию 6.3 (FloatStateResponse)"""

    def get_all_states(self) -> Dict:
        """Returns: {group_id: FloatStateResponse}"""

    def is_paused(self, group_id: int) -> bool:
        """True если группа на паузе из-за поплавка."""

    def is_timed_out(self, group_id: int) -> bool:
        """True если таймаут истёк (аварийный стоп)."""

    def is_resumed(self, group_id: int) -> bool:
        """True если уровень восстановлен после паузы."""

    def get_resume_event(self, group_id: int) -> threading.Event:
        """Event для worker: wait() на resume/timeout/cancel."""

    def wait_for_resume_or_cancel(
        self,
        group_id: int,
        cancel_event: threading.Event,
        shutdown_event: threading.Event,
    ) -> str:
        """Блокирует до resume/cancel/shutdown/timeout.
        Returns: 'resumed' | 'cancelled' | 'shutdown' | 'timed_out'
        """
```

### 2.3 ProgramCompletionTracker (`services/program_queue.py`)

```python
from typing import List, Optional

class ProgramCompletionTracker:
    def __init__(self) -> None: ...

    def register(
        self,
        program_run_id: str,
        entry_ids: List[str],
        program_id: int,
        program_name: str,
    ) -> None:
        """Регистрирует запуск мульти-группной программы."""

    def entry_finished(
        self,
        program_run_id: str,
        entry_id: str,
    ) -> bool:
        """Сообщает о завершении entry.
        Returns True если ВСЕ entries программы завершены."""

    def is_program_complete(self, program_run_id: str) -> bool:
        """True если все entries программы завершены или program_run_id не трекается."""

    def get_pending(self) -> Dict[str, Dict]:
        """Возвращает незавершённые программы для диагностики."""
```

---

## 3. Тест-файлы

### 3.1 `tests/unit/test_program_queue.py` — ProgramQueueManager

**Минимум 30 тестов.**

#### Базовые операции (8 тестов)

| # | Тест | Входные данные | Ожидаемый результат |
|---|---|---|---|
| 1 | `test_enqueue_single_entry_lifecycle` | `enqueue(prog_id=1, name="Утро", group_id=1, zone_ids=[1,2,3], scheduled_time=now)` | Возвращает QueueEntry. entry.state=WAITING. Worker стартует. entry.state=RUNNING. Зоны включаются последовательно. entry.state=COMPLETED |
| 2 | `test_enqueue_creates_worker_on_empty_group` | `enqueue(...)` на группу без worker | GroupQueue.worker_thread is not None. worker_thread.is_alive() == True. Thread name = "queue-worker-{group_id}" |
| 3 | `test_enqueue_to_busy_group_waits` | `enqueue(entry_A)`, `enqueue(entry_B)` на ту же группу | entry_A.state=RUNNING. entry_B.state=WAITING. entry_B в deque |
| 4 | `test_get_queue_state_structure` | Группа с 1 running + 2 waiting | Возвращает dict: `{group_id, current: {...}, queue: [{...}, {...}], queue_length}`. Структура по секции 6.1 |
| 5 | `test_get_all_queues_state_all_groups` | 2 группы с entries | Возвращает dict с ключами group_id для каждой группы. Пустые группы отсутствуют |
| 6 | `test_entry_id_is_unique_uuid4` | 2x `enqueue(...)` | entry_1.entry_id != entry_2.entry_id. Формат UUID4 (regex: `^[0-9a-f]{8}-...`) |
| 7 | `test_deque_maxlen_rejects_overflow` | 21x `enqueue(...)` на одну группу (worker замокан/заблокирован) | Первые 20 → QueueEntry. 21-я → None. Лог `queue_overflow` |
| 8 | `test_shutdown_terminates_all_workers` | 3 группы с running entries. `shutdown()` | Все worker threads завершены (join timeout=5). Все текущие зоны OFF. Entries → state=CANCELLED |

#### Очередь FIFO (6 тестов)

| # | Тест | Входные данные | Ожидаемый результат |
|---|---|---|---|
| 9 | `test_fifo_two_entries_sequential` | entry_A (zone=[1], dur=2s), entry_B (zone=[2], dur=2s) в группу 1 | A завершается первой. B стартует после A. Порядок zone_start вызовов: [zone_1, zone_2] |
| 10 | `test_fifo_three_entries_cascade` | entry_A, entry_B, entry_C в группу 1 | Выполнение строго A→B→C. started_at: A < B < C |
| 11 | `test_different_groups_run_parallel` | entry_A в группу 1, entry_B в группу 2 | Обе стартуют одновременно (started_at разница < 1 сек). Два worker потока |
| 12 | `test_program_split_across_two_groups` | Программа с zone_ids [1(гр.1), 2(гр.1), 10(гр.2)]. job_run_program разбивает → 2 enqueue | 2 entry создаются. Обе в RUNNING параллельно (разные группы). ProgramCompletionTracker регистрирует |
| 13 | `test_worker_terminates_when_queue_empty` | 1 entry, завершается | После завершения entry: worker_thread завершён (is_alive()==False). При новом enqueue — новый worker |
| 14 | `test_entries_different_programs_same_group` | prog_A и prog_B в одну группу | Обе в очереди. FIFO: кто раньше enqueue — тот первый |

#### Cancel (5 тестов)

| # | Тест | Входные данные | Ожидаемый результат |
|---|---|---|---|
| 15 | `test_cancel_entry_waiting` | entry_A running, entry_B waiting. `cancel_entry(entry_B.entry_id)` | True. entry_B.state=CANCELLED. entry_B не выполняется. entry_A продолжает |
| 16 | `test_cancel_entry_running_stops_zones` | entry_A running (zone 3 ON). `cancel_entry(entry_A.entry_id)` | True. Зона 3 OFF (stop_zone вызван). entry_A.state=CANCELLED |
| 17 | `test_cancel_program_all_entries` | prog_id=5: entry_A(гр.1) running, entry_B(гр.1) waiting, entry_C(гр.2) running. `cancel_program(5)` | Возвращает 3. Все entry.state=CANCELLED. Зоны A и C OFF |
| 18 | `test_cancel_group_waits_for_worker` | entry_A running (гр.1), entry_B waiting (гр.1). `cancel_group(1)` | Возвращает 2. cancel_group() ждёт worker (join). Worker в finally → stop_zone. stop_all_in_group() НЕ вызывается напрямую. Worker завершён |
| 19 | `test_cancel_during_float_pause` | entry_A на паузе (float). `cancel_entry(entry_A.entry_id)` | wait_for_resume прерывается. Зона OFF. entry_A.state=CANCELLED |

#### max_wait_time (4 теста)

| # | Тест | Входные данные | Ожидаемый результат |
|---|---|---|---|
| 20 | `test_max_wait_expired` | entry ждала 130 мин (mock time), max_wait=120 | При извлечении из deque: entry.state=EXPIRED. Лог `queue_entry_expired`. Worker переходит к следующей |
| 21 | `test_max_wait_not_expired` | entry ждала 100 мин, max_wait=120 | entry стартует нормально (state=RUNNING) |
| 22 | `test_max_wait_excludes_float_pause_time` | entry ждала 100 мин абсолютных, float pause 40 мин → actual_wait=60 мин, max_wait=120 | entry НЕ expired (actual_wait=60 < 120). excluded_wait_seconds=2400 |
| 23 | `test_max_wait_zero_means_no_limit` | max_wait=0, entry ждала 500 мин | entry стартует нормально |

#### Weather adjustment (3 теста)

| # | Тест | Входные данные | Ожидаемый результат |
|---|---|---|---|
| 24 | `test_weather_coefficient_applied_at_zone_start` | Base duration=600s, coefficient=150%. Mock `get_weather_coefficient` → 150 | adjusted_duration = 900s. Применено в _run_entry, не при enqueue |
| 25 | `test_weather_coefficient_changes_while_in_queue` | entry enqueued при coeff=150%. К моменту старта coeff=80% | Используется 80% (актуальный на момент старта зоны) |
| 26 | `test_weather_skip_no_enqueue` | weather check → skip=True | `enqueue()` не вызывается. Очередь не затрагивается |

#### Error handling (4 теста)

| # | Тест | Входные данные | Ожидаемый результат |
|---|---|---|---|
| 27 | `test_exception_in_run_entry_sets_failed` | `exclusive_start_zone` бросает RuntimeError | entry.state=FAILED. Worker продолжает со следующей entry. Telegram уведомление |
| 28 | `test_mqtt_timeout_retry_then_fail` | `exclusive_start_zone` бросает timeout 3 раза | Retry attempts. После max_retries → entry.state=FAILED. Зона OFF в finally |
| 29 | `test_sqlite_locked_retries` | DB write raises `sqlite3.OperationalError: database is locked` 1 раз, потом OK | Повторная попытка через busy_timeout. Запись сохраняется |
| 30 | `test_worker_exception_does_not_kill_queue` | _run_entry() бросает unexpected exception | entry.state=FAILED. Worker продолжает. Следующая entry обрабатывается |

#### Дополнительные (3 теста)

| # | Тест | Входные данные | Ожидаемый результат |
|---|---|---|---|
| 31 | `test_enqueue_returns_none_on_overflow_with_telegram` | 21-й enqueue при maxlen=20 | None. telegram_notify вызван с текстом «переполнена» |
| 32 | `test_queue_state_during_float_pause` | entry в RUNNING, float pause → state='paused' в zone, entry всё ещё RUNNING | get_queue_state: current.state=RUNNING. Зона в БД: state='paused' |
| 33 | `test_program_run_id_propagated` | enqueue с program_run_id="abc-123" | entry.program_run_id == "abc-123". Передаётся в completion tracker |

---

### 3.2 `tests/unit/test_program_queue_concurrency.py` — Thread Safety

**Минимум 10 тестов. РЕАЛЬНЫЕ потоки.**

| # | Тест | Сценарий | Ожидаемый результат |
|---|---|---|---|
| 1 | `test_concurrent_enqueue_5_threads_fifo` | 5 потоков одновременно enqueue на одну группу (через `threading.Barrier`) | Все 5 entry в очереди. Нет потерь. Порядок выполнения = порядок enqueue (FIFO). Нет data corruption |
| 2 | `test_enqueue_and_cancel_group_no_deadlock` | Поток 1: enqueue 10 entries. Поток 2: cancel_group после первого enqueue | Нет deadlock (завершение за <5 сек). Нет data corruption в queue state |
| 3 | `test_three_groups_three_workers_parallel` | 3 группы × 3 entries. Все enqueue одновременно | 3 worker потока параллельно. Каждая группа обрабатывает свои entries последовательно. Все 9 entries завершены |
| 4 | `test_shutdown_during_execution_completes_fast` | 3 группы с running entries (sleep в run_entry). `shutdown()` | Все workers завершаются за <5 сек. Зоны OFF. Нет зависших потоков |
| 5 | `test_lock_ordering_no_deadlock_stress` | 10 потоков: 5 делают enqueue, 5 делают get_all_queues_state, непрерывно 2 секунды | Нет deadlock. Нет exception. _global_lock → GroupQueue.lock всегда |
| 6 | `test_100_enqueue_burst_maxlen` | 100 enqueue за <1 сек на одну группу | Первые 20 → OK (maxlen). Остальные → None (reject). Нет crash |
| 7 | `test_cancel_entry_while_worker_processes` | Worker обрабатывает entry_A (sleep). Другой поток: cancel_entry(entry_A) | cancel обнаруживается в цикле while remaining. Зона OFF. Нет race condition |
| 8 | `test_concurrent_cancel_program_multiple_groups` | Программа с entries в 3 группах. cancel_program из 2 разных потоков | Идемпотентно. Все entries cancelled. Нет double-stop |
| 9 | `test_enqueue_during_worker_shutdown` | Worker завершает последнюю entry. Одновременно новый enqueue на ту же группу | Либо новый worker запускается, либо entry в очереди ожидает нового worker. Нет потери entry |
| 10 | `test_get_queue_state_consistent_snapshot` | Группа с быстро меняющимися entries. get_queue_state из другого потока | Возвращает консистентный snapshot (current + queue не противоречат друг другу) |

---

### 3.3 `tests/unit/test_float_monitor.py` — FloatMonitor

**Минимум 25 тестов.**

#### Базовые (5 тестов)

| # | Тест | Входные данные | Ожидаемый результат |
|---|---|---|---|
| 1 | `test_float_off_is_paused_true` | Поплавок OFF (level_ok=False) для group_id=1 | `is_paused(1)` == True |
| 2 | `test_float_on_is_paused_false` | Поплавок ON (level_ok=True) для group_id=1 | `is_paused(1)` == False |
| 3 | `test_no_mode_payload_mapping` | float_mode='NO'. Payload "0" | logical_level = low (level_ok=False). Payload "1" → level_ok=True |
| 4 | `test_nc_mode_payload_inverted` | float_mode='NC'. Payload "1" | logical_level = low (level_ok=False). Payload "0" → level_ok=True |
| 5 | `test_get_state_structure` | Группа 1 на паузе с таймаутом | Возвращает dict по секции 6.3: `{group_id, level_ok, paused, paused_since, timeout_at, paused_zones}` |

#### Дебаунс (4 теста)

| # | Тест | Входные данные | Ожидаемый результат |
|---|---|---|---|
| 6 | `test_debounce_short_signal_ignored` | Поплавок OFF на 2 сек, потом ON. debounce=5s | `_on_level_low` НЕ вызван. Пауза НЕ происходит |
| 7 | `test_debounce_stable_signal_processed` | Поплавок OFF стабильно > 5 сек. debounce=5s | `_on_level_low` вызван. Пауза происходит |
| 8 | `test_debounce_bounce_pattern_no_pause` | OFF(2s)→ON(2s)→OFF(2s), debounce=5s | Ни один OFF не держится >= 5s → нет паузы |
| 9 | `test_debounce_stable_off_triggers_pause` | OFF стабильно 6s, debounce=5s | Пауза происходит через ~5s после первого OFF |

#### Пауза/Resume (6 тестов)

| # | Тест | Входные данные | Ожидаемый результат |
|---|---|---|---|
| 10 | `test_pause_sets_float_pause_event` | Float OFF → дебаунс пройден | float_pause_event для группы — set. Worker обнаруживает через `is_paused()` |
| 11 | `test_resume_sets_float_resume_event` | Float ON после паузы → дебаунс пройден | float_resume_event для группы — set. Worker просыпается из wait |
| 12 | `test_pause_saves_remaining_to_db` | Зона 3 работает, remaining=480s. Float pause | DB: zones.pause_remaining_seconds=480 для zone_id=3. zones.state='paused' |
| 13 | `test_resume_after_5min_remaining_unchanged` | Зона 3 на паузе, remaining=480s. Пауза 5 мин. Resume | Worker возобновляет с remaining=480s (worker спит, remaining не тикает) |
| 14 | `test_pause_when_no_active_zones_noop` | Float OFF, но нет активных зон в группе | is_paused=True (состояние запоминается). Но никакие зоны не затрагиваются |
| 15 | `test_resume_without_prior_pause_noop` | Float ON, не было паузы | Ничего не происходит. Нет ошибок |

#### Таймаут (4 теста)

| # | Тест | Входные данные | Ожидаемый результат |
|---|---|---|---|
| 16 | `test_float_timeout_emergency_stop` | Float OFF. timeout=30мин (mock time advance 31 мин). Float всё ещё OFF | _on_timeout вызван. cancel_group() вызван. Лог float_timeout_emergency_stop |
| 17 | `test_float_restored_before_timeout` | Float OFF. timeout=30мин. Float ON через 29 мин | Таймер отменён. _on_timeout НЕ вызван. Resume нормально |
| 18 | `test_timeout_calls_cancel_group` | Таймаут сработал | queue_manager.cancel_group(group_id) вызван |
| 19 | `test_timeout_sends_telegram` | Таймаут сработал | telegram_notify вызван с текстом содержащим "АВАРИЙНЫЙ СТОП" и имя группы |

#### Hysteresis (3 теста)

| # | Тест | Входные данные | Ожидаемый результат |
|---|---|---|---|
| 20 | `test_hysteresis_min_run_time_blocks_repause` | Resume → повторный OFF через 30 сек. min_run_time=60 | Пауза всё равно происходит (безопасность). Лог `float_pause_too_soon` |
| 21 | `test_hysteresis_after_min_run_time_allows` | Resume → повторный OFF через 90 сек. min_run_time=60 | Пауза происходит нормально. Нет warning |
| 22 | `test_hysteresis_max_trips_emergency_stop` | 3 паузы за 5 мин (3 × OFF→ON→OFF). FLOAT_MAX_TRIPS=3, FLOAT_TRIP_WINDOW=300 | После 3-го OFF: аварийный стоп. cancel_group. Telegram «поплавок нестабилен». Очередь отменена |

#### Per-group изоляция (3 теста)

| # | Тест | Входные данные | Ожидаемый результат |
|---|---|---|---|
| 23 | `test_float_pause_group1_not_affects_group2` | Float OFF группа 1 | is_paused(1)==True. is_paused(2)==False. Зоны группы 2 продолжают |
| 24 | `test_both_groups_paused_independently` | Float OFF группа 1, Float OFF группа 2 | is_paused(1)==True, is_paused(2)==True. Отдельные таймеры, отдельные events |
| 25 | `test_resume_group1_group2_still_paused` | Обе на паузе. Float ON группа 1 | is_paused(1)==False. is_paused(2)==True |

---

### 3.4 `tests/unit/test_float_monitor_mqtt.py` — MQTT интеграция FloatMonitor

**Минимум 8 тестов. Мокаем paho-mqtt.**

| # | Тест | Сценарий | Ожидаемый результат |
|---|---|---|---|
| 1 | `test_start_subscribes_to_correct_topics` | 2 группы: float_enabled=True, topics "/dev/gpio/A1_IN" и "/dev/gpio/A2_IN" | mqtt_client.subscribe вызван для обоих топиков |
| 2 | `test_stop_unsubscribes` | start(), потом stop() | mqtt_client.unsubscribe вызван для всех топиков |
| 3 | `test_reload_group_resubscribes` | start(), изменить float_mqtt_topic группы, reload_group(1) | Старый топик unsubscribe. Новый топик subscribe |
| 4 | `test_mqtt_reconnect_restores_subscriptions` | start(), simulate disconnect → reconnect callback | Все подписки восстановлены (subscribe вызван повторно) |
| 5 | `test_invalid_payload_ignored` | MQTT message с payload "garbage" | _on_level_low/_on_level_restored НЕ вызваны. Нет exception |
| 6 | `test_float_disabled_no_subscription` | Группа с float_enabled=False | mqtt_client.subscribe НЕ вызван для этой группы |
| 7 | `test_two_floats_two_subscriptions` | 2 группы, каждая с float_enabled=True | 2 подписки на разные топики. Сообщения маршрутизируются правильно |
| 8 | `test_tripped_topic_subscription` | Группа с float_enabled=True | Подписка на `/devices/float-watchdog/controls/group_{N}_tripped` дополнительно к float topic |

---

### 3.5 `tests/unit/test_completion_tracker.py` — ProgramCompletionTracker

**Минимум 6 тестов.**

| # | Тест | Сценарий | Ожидаемый результат |
|---|---|---|---|
| 1 | `test_single_entry_complete` | register(run_id, [entry_1], prog_id, name). entry_finished(run_id, entry_1) | Returns True. Program complete |
| 2 | `test_three_entries_complete_after_all` | register(run_id, [e1, e2, e3]). entry_finished(e1) → False. entry_finished(e2) → False. entry_finished(e3) → True | True только после e3 |
| 3 | `test_cancelled_entry_still_completes_program` | register(run_id, [e1, e2, e3]). e1 completed. e2 cancelled → entry_finished(e2). e3 completed → entry_finished(e3) | entry_finished(e2) → False. entry_finished(e3) → True. Программа "complete" (с partial) |
| 4 | `test_expired_entry_completes_program` | register(run_id, [e1, e2]). e1 expired → entry_finished(e1). e2 completed → entry_finished(e2) | True после e2 |
| 5 | `test_program_finish_log_on_complete` | Все entries завершены | entry_finished возвращает True → вызывающий код создаёт лог program_finish |
| 6 | `test_double_entry_finished_idempotent` | entry_finished(run_id, e1) вызван дважды | Второй вызов не меняет состояние. Нет exception. Программа complete status корректен |

---

### 3.6 `tests/unit/test_check_conflicts_v2.py` — Расширенная проверка конфликтов

**Минимум 8 тестов.**

| # | Тест | Входные данные | Ожидаемый результат |
|---|---|---|---|
| 1 | `test_conflict_at_base_durations_error` | Prog A: 06:00, 45 мин. Prog B: 06:30, та же группа. weather_factor=100 | `level='error'`. overlap_minutes=15 |
| 2 | `test_conflict_only_at_weather_150_warning` | Prog A: 06:00, 45 мин. Prog B: 07:00. weather_factor=150 → A до 07:07 | `level='warning'`. overlap_minutes≈7. weather_factor=150 |
| 3 | `test_no_conflict_even_at_200` | Prog A: 06:00, 20 мин. Prog B: 07:00. weather_factor=200 → A до 06:40 | `has_conflicts=False`. Пустой conflicts list |
| 4 | `test_different_groups_no_conflict` | Prog A: 06:00, гр.1. Prog B: 06:00, гр.2. Одно время | `has_conflicts=False` (разные группы, параллельно) |
| 5 | `test_same_group_small_gap_conflict` | Prog A: 06:00, 60 мин base, гр.1. Prog B: 07:05. Зазор 5 мин. weather_factor=100 | `has_conflicts=False` при base. При weather_factor=120 → overlap ≈7 мин → `level='warning'` |
| 6 | `test_include_weather_uses_settings` | include_weather=True. settings.max_weather_coefficient=200 | weather_factor берётся из settings (200) |
| 7 | `test_current_coefficient_in_response` | Текущий weather coefficient = 120 | Response содержит `current_weather_coefficient: 120` |
| 8 | `test_empty_zones_no_conflict` | Prog A: 06:00, zones=[] | `has_conflicts=False`. Нет ошибок |

---

### 3.7 `tests/unit/test_boot_recovery.py` — Восстановление после перезагрузки

**Минимум 6 тестов.**

| # | Тест | Входные данные | Ожидаемый результат |
|---|---|---|---|
| 1 | `test_boot_paused_zones_off` | DB: zone 3 state='paused', pause_reason='float' | stop_on_boot: MQTT OFF для zone 3. DB: state='off', pause_reason=None, pause_remaining_seconds=None |
| 2 | `test_boot_paused_with_remaining_still_off` | DB: zone 3 state='paused', pause_remaining_seconds=720 | Безусловно OFF (безопасность). remaining сбрасывается. Лог: boot_zone_off с previous_state='paused' |
| 3 | `test_boot_on_zones_off` | DB: zone 1 state='on' | OFF (текущее поведение). state='off' |
| 4 | `test_boot_float_events_cleanup` | DB: float_events с незавершённой паузой (event_type='low', нет парного 'restored') | Boot cleanup создаёт запись event_type='boot_reset'. Состояние сбрасывается |
| 5 | `test_boot_queue_log_running_interrupted` | DB: program_queue_log с state='running' | Boot: state обновлён на 'interrupted'. completed_at = now |
| 6 | `test_recover_missed_runs_via_enqueue` | Программа пропущена (scheduled 06:00, boot 06:25). misfire_grace_time позволяет | recover_missed_runs вызывает enqueue(), НЕ _run_program_threaded(). Entry в очереди |

---

### 3.8 `tests/integration/test_queue_scheduler_integration.py` — Интеграция

**Минимум 10 тестов.**

| # | Тест | Сценарий | Ожидаемый результат |
|---|---|---|---|
| 1 | `test_apscheduler_fires_enqueue_flow` | APScheduler cron fires → job_run_program | job_run_program группирует зоны → enqueue() вызван для каждой группы → worker стартует → зоны ON/OFF последовательно |
| 2 | `test_two_programs_same_group_queued` | Prog A: 06:00, гр.1. Prog B: 06:15, гр.1 | A стартует. B в очереди (WAITING). После A → B стартует |
| 3 | `test_weather_skip_no_enqueue` | job_run_program, weather check → skip | enqueue НЕ вызван. Лог weather_skip |
| 4 | `test_cancel_group_jobs_calls_queue_cancel` | cancel_group_jobs(1) | queue_manager.cancel_group(1) вызван. APScheduler jobs также отменены |
| 5 | `test_scheduler_stop_calls_shutdown` | scheduler.stop() | queue_manager.shutdown() вызван. float_monitor.stop() вызван |
| 6 | `test_float_pause_during_scheduler_run` | Программа работает через очередь. Float pause | Worker спит на wait_for_resume. Зона paused. После resume — продолжает |
| 7 | `test_recover_missed_runs_enqueues` | Boot: пропущенная программа | recover_missed_runs → enqueue(). Оставшиеся зоны в очереди |
| 8 | `test_graceful_shutdown_zones_off` | systemd SIGTERM → scheduler.stop() | Все текущие зоны OFF. Entries: CANCELLED. Worker threads завершены |
| 9 | `test_multi_group_program_completion` | Программа «Утро» → зоны в 2 группах. Обе завершены | ProgramCompletionTracker: program_finish лог создаётся после обоих entries |
| 10 | `test_scheduler_init_creates_queue_manager` | init_scheduler(db) | scheduler.queue_manager is not None. scheduler.float_monitor is not None |

---

## 4. Общие fixtures

### 4.1 conftest.py fixtures

```
mock_mqtt         — MagicMock paho.mqtt.client.Client. subscribe/publish/connect мокаются
mock_db           — IrrigationDB с in-memory SQLite. Все таблицы созданы включая новые
mock_telegram     — MagicMock для telegram_notify
mock_time         — patch time.time, time.monotonic, time.sleep для детерминизма и ускорения
queue_manager     — ProgramQueueManager(db=mock_db, shutdown_event=Event(), ...)
float_monitor     — FloatMonitor(db=mock_db, queue_manager=queue_manager, ...)
mock_scheduler    — MagicMock IrrigationScheduler с queue_manager
mock_zone_control — patch exclusive_start_zone, stop_zone → return True, track calls
```

### 4.2 Helper utilities

```
wait_for_state(entry, expected_state, timeout=5)  — poll entry.state до совпадения
wait_for_worker_idle(queue_manager, group_id, timeout=5)  — ждёт завершения worker
create_test_zones(db, group_id, count) → List[dict]  — создаёт N зон в группе
simulate_float_message(float_monitor, group_id, payload)  — эмулирует MQTT сообщение
advance_time(seconds)  — сдвигает mock time вперёд
```

---

## 5. Покрытие по ревью — Маппинг замечания → тест

### 5.1 Критические (К1–К6)

| Замечание | Суть | Тест(ы) покрывающие |
|---|---|---|
| **К1** | Race condition cancel_group(): двойной stop зон | `test_program_queue.test_cancel_group_waits_for_worker` (#18), `test_concurrency.test_enqueue_and_cancel_group_no_deadlock` (#2) |
| **К2** | Deadlock _global_lock + GroupQueue.lock | `test_concurrency.test_lock_ordering_no_deadlock_stress` (#5), `test_concurrency.test_concurrent_enqueue_5_threads_fifo` (#1) |
| **К3** | Float resume через exclusive_start — peer-зоны бьются. Worker единственный кто включает | `test_float_monitor.test_resume_sets_float_resume_event` (#11), `test_float_monitor.test_pause_sets_float_pause_event` (#10), `test_integration.test_float_pause_during_scheduler_run` (#6) |
| **К4** | Потеря remaining при перезагрузке | `test_float_monitor.test_pause_saves_remaining_to_db` (#12), `test_boot_recovery.test_boot_paused_with_remaining_still_off` (#2), `test_boot_recovery.test_boot_paused_zones_off` (#1) |
| **К5** | Weather coefficient при enqueue vs при старте зоны | `test_program_queue.test_weather_coefficient_applied_at_zone_start` (#24), `test_program_queue.test_weather_coefficient_changes_while_in_queue` (#25) |
| **К6** | max_wait vs float pause — необоснованный EXPIRED | `test_program_queue.test_max_wait_excludes_float_pause_time` (#22) |

### 5.2 Серьёзные (С1–С10)

| Замечание | Суть | Тест(ы) покрывающие |
|---|---|---|
| **С1** | Программа с зонами из 3+ групп — несогласованное завершение | `test_completion_tracker.test_three_entries_complete_after_all` (#2), `test_integration.test_multi_group_program_completion` (#9), `test_program_queue.test_program_split_across_two_groups` (#12) |
| **С2** | entry_id через uuid4() | `test_program_queue.test_entry_id_is_unique_uuid4` (#6) |
| **С3** | MQTT down при float resume — retry | `test_program_queue.test_mqtt_timeout_retry_then_fail` (#28) |
| **С4** | SQLite contention — busy_timeout | `test_program_queue.test_sqlite_locked_retries` (#29) |
| **С5** | wb-rules tripped lifecycle | `test_float_monitor_mqtt.test_tripped_topic_subscription` (#8) |
| **С6** | Hysteresis поплавка | `test_float_monitor.test_hysteresis_max_trips_emergency_stop` (#22), `test_float_monitor.test_hysteresis_min_run_time_blocks_repause` (#20), `test_float_monitor.test_hysteresis_after_min_run_time_allows` (#21) |
| **С7** | Две группы с одним насосом (не исправлялось — фича) | Нет теста (валидация в UI, не в backend) |
| **С8** | shutdown во время float pause | `test_concurrency.test_shutdown_during_execution_completes_fast` (#4), `test_integration.test_graceful_shutdown_zones_off` (#8) |
| **С9** | deque maxlen переполнение | `test_program_queue.test_deque_maxlen_rejects_overflow` (#7), `test_concurrency.test_100_enqueue_burst_maxlen` (#6) |
| **С10** | _run_entry() без try/finally — exception убивает worker | `test_program_queue.test_exception_in_run_entry_sets_failed` (#27), `test_program_queue.test_worker_exception_does_not_kill_queue` (#30) |

### 5.3 Дополнительные тест-сценарии из ревью (T19–T26)

| Сценарий из ревью | Тест(ы) покрывающие |
|---|---|
| **T19** cancel_group во время _run_entry — нет двойного stop | `test_program_queue.test_cancel_group_waits_for_worker` (#18) |
| **T20** Перезагрузка при float pause — зоны OFF при boot | `test_boot_recovery.test_boot_paused_zones_off` (#1), `test_boot_recovery.test_boot_paused_with_remaining_still_off` (#2) |
| **T21** MQTT down при float resume | `test_program_queue.test_mqtt_timeout_retry_then_fail` (#28) |
| **T22** 3 float pause/resume за 1 мин — hysteresis | `test_float_monitor.test_hysteresis_max_trips_emergency_stop` (#22) |
| **T23** Программа из 3 групп — completion tracker | `test_completion_tracker.test_three_entries_complete_after_all` (#2), `test_integration.test_multi_group_program_completion` (#9) |
| **T24** max_wait во время float pause — excluded_wait | `test_program_queue.test_max_wait_excludes_float_pause_time` (#22) |
| **T25** Переполнение очереди | `test_program_queue.test_deque_maxlen_rejects_overflow` (#7), `test_program_queue.test_enqueue_returns_none_on_overflow_with_telegram` (#31) |
| **T26** shutdown во время float pause | `test_concurrency.test_shutdown_during_execution_completes_fast` (#4), `test_integration.test_graceful_shutdown_zones_off` (#8) |

---

## 6. Контракты типов данных (Return values)

### 6.1 QueueStateResponse — `get_queue_state(group_id)`

```python
{
    "group_id": int,                     # ID группы
    "group_name": str,                   # Название группы
    "current": Optional[{                # Текущая выполняемая entry (None если idle)
        "entry_id": str,                 # UUID4
        "program_id": int,
        "program_name": str,
        "program_run_id": Optional[str], # UUID запуска программы
        "zone_ids": List[int],
        "state": str,                    # 'running'
        "scheduled_time": str,           # ISO datetime
        "enqueued_at": str,              # ISO datetime
        "started_at": str,              # ISO datetime
        "current_zone_id": Optional[int],  # какая зона сейчас работает
        "current_zone_remaining": Optional[int],  # секунды
    }],
    "queue": List[{                      # Entries в ожидании (FIFO order)
        "entry_id": str,
        "program_id": int,
        "program_name": str,
        "zone_ids": List[int],
        "state": str,                    # 'waiting'
        "enqueued_at": str,
        "estimated_wait_minutes": Optional[int],
    }],
    "queue_length": int,                 # len(queue)
    "worker_active": bool,               # worker_thread is alive
    "float_paused": bool,                # True если группа на float pause
}
```

### 6.2 AllQueuesStateResponse — `get_all_queues_state()`

```python
{
    "queues": Dict[int, QueueStateResponse],  # group_id → QueueStateResponse
    "total_entries": int,                      # суммарное кол-во entries (running+waiting)
    "active_workers": int,                     # кол-во живых worker потоков
}
```

### 6.3 FloatStateResponse — `get_state(group_id)`

```python
{
    "group_id": int,
    "float_enabled": bool,
    "level_ok": bool,                       # True = вода есть, False = вода ушла
    "paused": bool,                         # True = группа на float pause
    "paused_since": Optional[str],          # ISO datetime начала паузы
    "timeout_at": Optional[str],            # ISO datetime когда сработает таймаут
    "timeout_remaining_seconds": Optional[int],
    "paused_zones": Optional[List[{         # зоны на паузе
        "zone_id": int,
        "zone_name": str,
        "remaining_seconds": int,
    }]],
    "hysteresis": {
        "trip_count": int,                  # кол-во срабатываний в окне
        "trip_window_seconds": int,         # FLOAT_TRIP_WINDOW
        "last_resume_at": Optional[str],    # ISO datetime
        "emergency_stopped": bool,          # True если hysteresis emergency stop
    },
}
```

### 6.4 QueueEntry dataclass fields

```python
{
    "entry_id": str,                        # uuid4()
    "program_id": int,
    "program_name": str,
    "program_run_id": Optional[str],        # uuid4() для ProgramCompletionTracker
    "group_id": int,
    "zone_ids": List[int],
    "scheduled_time": str,                  # ISO datetime
    "enqueued_at": str,                     # ISO datetime
    "started_at": Optional[str],            # ISO datetime
    "state": str,                           # QueueEntryState.value
    "excluded_wait_seconds": float,         # суммарное время float pause для max_wait calc
}
```

### 6.5 check_program_conflicts v2 Response

```python
{
    "has_conflicts": bool,
    "conflicts": List[{
        "program_id": int,
        "program_name": str,
        "level": str,                       # 'error' | 'warning'
        "overlap_minutes": int,
        "weather_factor": int,              # при каком коэффициенте возникает конфликт
        "group_id": int,
        "group_name": str,
        "message": str,                     # человекочитаемое описание
    }],
    "current_weather_coefficient": int,     # текущий коэффициент (%)
}
```

### 6.6 program_queue_log DB row

```python
{
    "id": int,                              # AUTOINCREMENT
    "entry_id": str,                        # UUID
    "program_id": int,
    "program_run_id": Optional[str],        # UUID запуска программы
    "group_id": int,
    "zone_ids": str,                        # JSON array, e.g. "[1, 2, 3]"
    "scheduled_time": str,                  # ISO datetime
    "enqueued_at": str,                     # ISO datetime
    "started_at": Optional[str],            # ISO datetime
    "completed_at": Optional[str],          # ISO datetime
    "state": str,                           # waiting/running/completed/cancelled/expired/failed/interrupted
    "wait_seconds": Optional[int],          # фактическое время в WAITING (без float pause)
    "run_seconds": Optional[int],           # время выполнения
    "created_at": str,                      # ISO datetime
}
```

### 6.7 float_events DB row

```python
{
    "id": int,                              # AUTOINCREMENT
    "group_id": int,
    "event_type": str,                      # 'low' | 'restored' | 'timeout' | 'hysteresis_stop' | 'boot_reset'
    "paused_zones": Optional[str],          # JSON: [{"zone_id": 1, "remaining_seconds": 480}]
    "created_at": str,                      # ISO datetime
}
```

---

## 7. Сводная таблица: файлы тестов и количество

| Файл | Модуль | Тестов (мин) | Тестов (план) |
|---|---|---|---|
| `tests/unit/test_program_queue.py` | ProgramQueueManager | 30 | 33 |
| `tests/unit/test_program_queue_concurrency.py` | Thread Safety | 10 | 10 |
| `tests/unit/test_float_monitor.py` | FloatMonitor | 25 | 25 |
| `tests/unit/test_float_monitor_mqtt.py` | MQTT integration | 8 | 8 |
| `tests/unit/test_completion_tracker.py` | ProgramCompletionTracker | 6 | 6 |
| `tests/unit/test_check_conflicts_v2.py` | check_program_conflicts v2 | 8 | 8 |
| `tests/unit/test_boot_recovery.py` | Boot recovery | 6 | 6 |
| `tests/integration/test_queue_scheduler_integration.py` | Scheduler integration | 10 | 10 |
| **Итого** | | **103** | **106** |
