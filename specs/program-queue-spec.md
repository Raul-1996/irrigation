# Спецификация: Очередь программ + Поплавок ёмкости

> Версия: 1.1
> Дата: 2026-03-30
> Статус: DRAFT
> Зависимости: [program-overlap-analysis.md](program-overlap-analysis.md)

---

## Changelog

### v1.1 (2026-03-30) — Исправления по результатам критического аудита

Исправлены **все 6 критических** и **9 из 10 серьёзных** замечаний из `program-queue-review.md`.

**Критические (К1–К6):**
- **К1.** Race condition cancel_group(): определён единый ответственный за OFF зон — worker в `finally`. `cancel_group()` только устанавливает event и ждёт завершения worker (секции 2.4, 2.8)
- **К2.** Deadlock: зафиксирован lock ordering `_global_lock → GroupQueue.lock`. Worker НИКОГДА не захватывает `_global_lock` (секция 2.7)
- **К3.** Float resume: FloatMonitor НЕ вызывает `exclusive_start_zone()`. Он только устанавливает `float_resume_event`. Worker — единственный, кто включает зоны (секции 3.5, 3.6)
- **К4.** Persist remaining: добавлено поле `pause_remaining_seconds` в таблицу zones. При boot — paused зоны безусловно OFF (секции 3.5, 7.2, 11)
- **К5.** Weather coefficient: явно зафиксировано — коэффициент вычисляется в момент фактического старта каждой зоны, НЕ при enqueue (секция 2.4, 5.3)
- **К6.** max_wait vs float pause: max_wait_time считает только время в состоянии WAITING. Добавлено поле `actual_wait_seconds` (секция 2.6)

**Серьёзные (С1–С10, кроме С7):**
- **С1.** Добавлен `ProgramCompletionTracker` для отслеживания мульти-группных программ (секция 2.10)
- **С2.** `entry_id` через `uuid4()` (секция 2.2)
- **С3.** MQTT down при float resume — retry с backoff, аварийный стоп при MQTT down > 30 сек (секция 3.5)
- **С4.** SQLite contention — `busy_timeout=30000`, batch writes (секция 7.6)
- **С5.** wb-rules tripped lifecycle: `tripped=1` при OFF, `tripped=0` при ON. FloatMonitor при `tripped=0` НЕ делает автоматический resume (секция 3.10)
- **С6.** Hysteresis поплавка: `min_run_time=60` сек после resume, аварийный стоп после 3 срабатываний за 5 мин (секция 3.11)
- **С7.** НЕ ИСПРАВЛЯЛОСЬ — это фича. Обновлено описание: группа = набор зон с общей очередью, не обязательно = один насос (секция 2.1)
- **С8.** shutdown во время float pause: `wait_for_resume_or_timeout()` принимает `shutdown_event`, Event composition (секция 3.6)
- **С9.** `deque(maxlen=20)`, при переполнении → reject + лог (секция 2.2)
- **С10.** `_run_entry()` в `try/finally`, `state='failed'` при exception (секция 5.3)

**Новые секции:**
- Секция 2.10: ProgramCompletionTracker
- Секция 3.11: Hysteresis поплавка
- Секция 11: Восстановление после перезагрузки (Boot Recovery)
- Тест-сценарии T19–T26

### v1.0 (2026-03-30) — Первая версия
- Начальная спецификация очереди программ и поплавка ёмкости

---

## 1. Обзор проблемы

### 1.1 Что сейчас не так

Подробный анализ — в `program-overlap-analysis.md`. Краткая сводка:

**Нет очереди программ.** Каждая программа запускается APScheduler в отдельном потоке. Если программа A ещё работает на группе 1, а программа B стартует на той же группе — оба потока начинают «бороться» за зоны: каждый выключает peer-зоны перед включением своей. Результат — хаотичное переключение клапанов, ни одна зона не получает полный полив, риск гидроударов и перегрева насоса.

**check_program_conflicts() не учитывает погоду.** Проверка конфликтов при создании/редактировании использует base durations из БД. При погодном коэффициенте 150% программа длится на 50% дольше — конфликт возникает в рантайме, хотя проверка показала «всё чисто».

**Нет защиты от сухого хода.** Если уровень воды в ёмкости упадёт — насос продолжит работать. Это прямой путь к сгоревшему мотору.

### 1.2 Что должно быть

1. **Per-group program queue** — очередь программ на каждую группу. Внутри группы — строго одна программа в один момент. Разные группы — параллельно.
2. **Поплавок ёмкости (per-group)** — MQTT-датчик уровня воды, привязанный к группе. При падении уровня — пауза зон этой группы с возобновлением.
3. **Расширенная проверка конфликтов** — учёт максимального погодного коэффициента (200%).

---

## 2. Архитектура очереди программ (Program Queue)

### 2.1 Принцип: Per-Group Queue

Каждая **группа** имеет собственную независимую очередь. Группа — это набор зон с общей очередью выполнения. Группа **не обязательно** соответствует одному насосу: пользователь может создать несколько групп с одним и тем же мастер-клапаном для параллельного полива нескольких зон от одного насоса.

Программы, использующие зоны из разных групп, разбиваются на «сегменты» по группам.

```
Группа 1 (Зоны 1-6):   [ProgA зоны 1,2,3] → [ProgB зоны 4,5] → idle
Группа 2 (Зоны 10-15):  [ProgC зоны 10,11] → idle
                          ↑ работают ПАРАЛЛЕЛЬНО ↑
```

**Все программы равноправны** — нет приоритетов. Кто первый занял группу — тот работает, остальные ждут в FIFO-очереди.

### 2.2 Структура данных

```python
# Новый класс: services/program_queue.py

import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Deque
from enum import Enum

MAX_QUEUE_SIZE = 20  # максимальный размер очереди на группу

class QueueEntryState(Enum):
    WAITING = 'waiting'      # в очереди, ждёт
    RUNNING = 'running'      # выполняется
    COMPLETED = 'completed'  # завершена
    CANCELLED = 'cancelled'  # отменена
    EXPIRED = 'expired'      # истёк max_wait_time
    FAILED = 'failed'        # завершена с ошибкой

@dataclass
class QueueEntry:
    program_id: int
    program_name: str
    group_id: int
    zone_ids: List[int]        # зоны ТОЛЬКО этой группы
    scheduled_time: datetime   # когда программа должна была стартовать
    enqueued_at: datetime      # когда встала в очередь
    state: QueueEntryState = QueueEntryState.WAITING
    started_at: Optional[datetime] = None
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))

@dataclass
class GroupQueue:
    group_id: int
    lock: threading.Lock = field(default_factory=threading.Lock)
    queue: Deque[QueueEntry] = field(default_factory=lambda: deque(maxlen=MAX_QUEUE_SIZE))
    current: Optional[QueueEntry] = None   # что сейчас выполняется
    worker_thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
```

**Переполнение очереди:** При `len(queue) >= MAX_QUEUE_SIZE` вызов `enqueue()` возвращает `None`, логирует `queue_overflow` и отправляет Telegram-уведомление: «⚠️ Очередь группы {name} переполнена ({MAX_QUEUE_SIZE} записей). Программа {name} отклонена.»

### 2.3 Класс ProgramQueueManager

```
ProgramQueueManager
├── _queues: Dict[int, GroupQueue]     # group_id → GroupQueue
├── _global_lock: threading.Lock       # для создания/удаления GroupQueue
├── _completion_tracker: ProgramCompletionTracker
│
├── enqueue(program_id, program_name, group_id, zone_ids, scheduled_time)
│   → QueueEntry | None  (None при переполнении)
├── get_queue_state(group_id) → dict
├── get_all_queues_state() → dict
├── cancel_entry(entry_id) → bool
├── cancel_program(program_id) → int   # отменяет все entries программы
├── cancel_group(group_id) → int       # отменяет всё в группе
├── shutdown()
│
└── _worker(group_id)                  # внутренний воркер-поток
```

### 2.4 Жизненный цикл entry

```
APScheduler fires job_run_program(prog_id, zones, name)
    │
    ▼
job_run_program() — НЕ запускает зоны напрямую!
    │
    ├── Разбивает zones по группам: {group_id: [zone_ids]}
    │
    ├── Регистрирует в ProgramCompletionTracker (если >1 группа)
    │
    ├── Для каждой группы: queue_manager.enqueue(prog_id, name, gid, zids, now)
    │   │
    │   ├── _global_lock.acquire() → получить/создать GroupQueue → release
    │   ├── GroupQueue.lock.acquire()
    │   ├── Проверка maxlen → reject если переполнение
    │   ├── Создаёт QueueEntry(state=WAITING)
    │   ├── Добавляет в deque
    │   ├── Если worker не запущен → запускает _worker(gid) в Thread
    │   └── GroupQueue.lock.release()
    │
    └── Возвращается (НЕ ждёт завершения)

_worker(group_id):
    while True:
        ├── lock → entry = deque.popleft() → unlock
        │   └── если deque пуста → break (поток завершается)
        │
        ├── Проверяет max_wait_time (считает ТОЛЬКО время в WAITING, см. 2.6)
        │   └── expired → entry.state=EXPIRED, log, continue
        │
        ├── entry.state = RUNNING, entry.started_at = now
        │
        ├── _run_entry(entry):   # обёрнут в try/finally (см. 5.3)
        │   │
        │   ├── for zone_id in entry.zone_ids:
        │   │   ├── Check cancellation, postpone
        │   │   ├── Weather adjust duration ← ЗДЕСЬ, в момент старта зоны (К5)
        │   │   ├── exclusive_start_zone(zone_id)
        │   │   ├── while remaining > 0:
        │   │   │   ├── check cancel_event
        │   │   │   ├── check shutdown_event
        │   │   │   ├── check float_pause → wait_for_resume
        │   │   │   └── sleep(1), remaining -= 1
        │   │   └── stop_zone(zone_id)  ← ВСЕГДА в finally (К1)
        │   │
        │   └── entry.state = COMPLETED
        │
        └── продолжаем цикл
```

**Важно (К5): Weather coefficient** вычисляется **в момент фактического старта каждой зоны** внутри `_run_entry()`, а НЕ при enqueue. Если программа ждала в очереди 90 минут — погодные условия за это время могли измениться, и зона получит актуальный коэффициент.

### 2.5 Разбивка программы на сегменты по группам

Программа может содержать зоны из разных групп. Пример:
- Программа «Утро»: зоны [1, 2, 3 (группа 1), 10, 11 (группа 2)]

При enqueue создаются **два** QueueEntry:
- `{program_id: 5, group_id: 1, zone_ids: [1, 2, 3]}`
- `{program_id: 5, group_id: 2, zone_ids: [10, 11]}`

Каждый попадает в свою GroupQueue и выполняется независимо. Это позволяет группам работать параллельно.

Если программа создаёт entries в нескольких группах, `ProgramCompletionTracker` (секция 2.10) отслеживает общее завершение программы.

### 2.6 Максимальное время ожидания в очереди

`MAX_QUEUE_WAIT_MINUTES` — настройка в settings (по умолчанию: **120 минут**).

**Правило подсчёта (К6):** max_wait_time считает только фактическое время в состоянии **WAITING** — то есть от момента `enqueued_at` до момента перехода в RUNNING. Время, проведённое текущей (впереди стоящей) entry в состоянии PAUSED (float pause), **не засчитывается** в ожидание следующих entries.

Реализация: при float pause записываем `pause_started_at`. При resume вычисляем `paused_duration`. Для каждой WAITING entry в очереди ведём поле `excluded_wait_seconds` — суммарное время float pause текущей entry. Проверка:

```python
actual_wait = (now - entry.enqueued_at).total_seconds() - entry.excluded_wait_seconds
if actual_wait > max_wait_seconds:
    entry.state = EXPIRED
```

Если entry простояла дольше max_wait_time:
- `entry.state = EXPIRED`
- Лог: `queue_entry_expired`
- Telegram-уведомление (если настроено)

Обоснование: если программа «Утро 06:00» ждёт 2+ часа (за вычетом пауз), то поливать уже бессмысленно (солнце, испарение).

### 2.7 Thread Safety и Lock Ordering

**Порядок захвата блокировок (К2) — СТРОГОЕ ПРАВИЛО:**

```
ВСЕГДА: _global_lock → GroupQueue.lock
НИКОГДА: GroupQueue.lock → _global_lock
```

- Каждая `GroupQueue` имеет свой `threading.Lock` для доступа к `deque` и `current`
- `_global_lock` защищает создание/удаление GroupQueue в `_queues` dict
- **Worker-поток НИКОГДА не захватывает `_global_lock`** — он работает только с `GroupQueue.lock` своей группы
- `get_all_queues_state()` копирует `dict(_queues)` под `_global_lock`, затем отпускает `_global_lock`, затем итерирует по копии, захватывая каждый `GroupQueue.lock` отдельно
- Worker-поток один на группу, создаётся при первом enqueue, завершается когда очередь пуста. Имя потока: `Thread(name=f"queue-worker-{group_id}", daemon=True)`
- `stop_event` per-group для graceful shutdown (используется при `scheduler.stop()`)
- `_shutdown_event` из IrrigationScheduler пробрасывается в ProgramQueueManager

### 2.8 Интеграция с cancel_group_jobs()

Текущий `cancel_group_jobs()` в scheduler должен вызывать `queue_manager.cancel_group(group_id)`:

**Логика cancel_group() (К1):**
1. Под `GroupQueue.lock`: все WAITING entries → CANCELLED
2. Устанавливает `group_cancel_events[group_id].set()`
3. **НЕ вызывает `stop_all_in_group()` напрямую** — это ответственность worker
4. **Ждёт завершения worker-потока** (`worker_thread.join(timeout=10)`)
5. Worker в `_run_entry()` обнаруживает cancel_event → выходит из цикла → **`finally` блок выключает текущую зону** (и только текущую — остальные уже OFF)
6. Если worker не завершился за timeout → лог `worker_join_timeout`, принудительное `stop_all_in_group()` как fallback

**Единственный ответственный за OFF зон — worker-поток (через `finally`).** `cancel_group()` только сигнализирует и ждёт. Это исключает race condition двойного stop.

### 2.9 Интеграция с recover_missed_runs()

При рестарте сервиса `recover_missed_runs()` создаёт entry через `enqueue()` вместо прямого запуска. Очередь автоматически сериализует восстановленные и плановые запуски.

### 2.10 ProgramCompletionTracker (С1)

Программа с зонами из нескольких групп создаёт несколько QueueEntry. Необходим механизм отслеживания общего завершения программы.

```python
class ProgramCompletionTracker:
    """Отслеживает завершение мульти-группных программ."""
    
    def __init__(self):
        self._lock = threading.Lock()
        # program_run_id → {entry_ids: set, completed: set, program_id, program_name}
        self._pending: Dict[str, dict] = {}
    
    def register(self, program_run_id: str, entry_ids: List[str],
                 program_id: int, program_name: str):
        """Регистрирует запуск программы с несколькими entries."""
        with self._lock:
            self._pending[program_run_id] = {
                'entry_ids': set(entry_ids),
                'completed': set(),
                'program_id': program_id,
                'program_name': program_name,
            }
    
    def entry_finished(self, program_run_id: str, entry_id: str) -> bool:
        """Сообщает о завершении entry. Возвращает True если ВСЕ entries программы завершены."""
        with self._lock:
            if program_run_id not in self._pending:
                return True  # single-group, не трекается
            rec = self._pending[program_run_id]
            rec['completed'].add(entry_id)
            if rec['completed'] >= rec['entry_ids']:
                del self._pending[program_run_id]
                return True  # все сегменты завершены
            return False
```

Когда `entry_finished()` возвращает `True`:
- Лог: `program_finish` с общим временем
- Telegram-уведомление о завершении программы

`program_run_id` — уникальный ID запуска программы (UUID), создаётся в `job_run_program()` и передаётся в каждый `QueueEntry`.

---

## 3. Поплавок ёмкости (Tank Float Valve)

### 3.1 Архитектура: per-group

Поплавок привязан к **группе**, не глобально. Каждая группа может иметь свой датчик уровня воды в своей ёмкости.

**Пример:**
- Группа 1 (ёмкость А): поплавок `/devices/wb-gpio/controls/A1_IN`
- Группа 2 (ёмкость Б): поплавок `/devices/wb-gpio/controls/A2_IN`
- Группа 3 (без ёмкости): поплавок не настроен

Если поплавок группы 1 = OFF → пауза только зон группы 1. Группы 2 и 3 продолжают работать.

### 3.2 Настройки группы (новые поля в таблице `groups`)

| Поле | Тип | По умолчанию | Описание |
|---|---|---|---|
| `float_enabled` | INTEGER (0/1) | 0 | Включена ли защита поплавком |
| `float_mqtt_topic` | TEXT | NULL | MQTT-топик дискретного входа |
| `float_mqtt_server_id` | INTEGER FK | NULL | Какой MQTT-сервер |
| `float_mode` | TEXT | 'NO' | NO = нормально разомкнутый (1=вода есть), NC = нормально замкнутый (0=вода есть) |
| `float_timeout_minutes` | INTEGER | 30 | Таймаут: если уровень не восстановился за N мин → аварийный стоп |
| `float_debounce_seconds` | INTEGER | 5 | Дебаунс: игнорируем кратковременные дребезги (<5 сек) |

### 3.3 MQTT-логика

```
Поплавок → Wirenboard дискретный вход → MQTT → wb-irrigation FloatMonitor

Пример MQTT:
  Топик:   /devices/wb-gpio/controls/A1_IN
  Payload: "1" (вода есть) или "0" (вода ушла)
  
  С учётом float_mode:
    NO: payload "1" = вода есть, "0" = вода ушла
    NC: payload "0" = вода есть, "1" = вода ушла (инвертировано)
```

### 3.4 Класс FloatMonitor

```
# services/float_monitor.py

FloatMonitor
├── _subscriptions: Dict[int, FloatSubscription]  # group_id → subscription
├── _lock: threading.Lock
├── _hysteresis: Dict[int, HysteresisState]       # group_id → hysteresis (С6)
│
├── start()                        # подписаться на MQTT для всех групп с float_enabled
├── stop()                         # отписаться
├── reload_group(group_id)         # перезагрузить подписку одной группы
├── get_state(group_id) → dict     # текущее состояние: level_ok, paused_since, timeout_at
├── get_all_states() → dict
│
├── _on_float_message(group_id, payload)
│   ├── Дебаунс (float_debounce_seconds)
│   ├── Определяет logical_level (с учётом NO/NC)
│   ├── level_ok=True  → _on_level_restored(group_id)
│   └── level_ok=False → _on_level_low(group_id)
│
├── _on_level_low(group_id)
│   ├── Проверяет hysteresis (С6, секция 3.11)
│   ├── Устанавливает float_pause_event для группы
│   ├── Worker сам выключает текущую зону (К3)
│   ├── Сохраняет remaining_seconds в БД: zones.pause_remaining_seconds (К4)
│   ├── DB: state='paused', pause_reason='float'
│   ├── Запускает таймер таймаута
│   ├── Обновляет excluded_wait_seconds для WAITING entries в очереди (К6)
│   ├── Лог: float_pause
│   └── Telegram: "⚠️ Группа X: уровень воды низкий, полив приостановлен"
│
├── _on_level_restored(group_id)
│   ├── Проверяет hysteresis min_run_time (С6)
│   ├── Устанавливает float_resume_event (К3)
│   ├── Worker сам возобновляет текущую зону (К3)
│   ├── Отменяет таймер таймаута
│   ├── Фиксирует paused_duration для excluded_wait_seconds (К6)
│   ├── Лог: float_resume
│   └── Telegram: "✅ Группа X: уровень восстановлен, полив возобновлён"
│
└── _on_timeout(group_id)
    ├── Аварийный стоп: cancel_group через ProgramQueueManager
    ├── Лог: float_timeout_emergency_stop
    └── Telegram: "🚨 Группа X: уровень не восстановился за N мин, АВАРИЙНЫЙ СТОП"
```

### 3.5 Пауза и возобновление — механика

**Ключевой принцип (К3): Worker — единственный, кто включает и выключает зоны.** FloatMonitor только устанавливает events, worker реагирует на них.

**Пауза:**
1. FloatMonitor получает low level для group_id
2. Дебаунс: ждём `float_debounce_seconds` (по умолч. 5 сек), подтверждаем что уровень стабильно низкий
3. FloatMonitor устанавливает `float_pause_event` для группы
4. Worker в цикле ожидания обнаруживает `float_pause_event`:
   - Считает `remaining_seconds = planned_end_time - now`
   - Сохраняет `remaining_seconds` в БД: `zones.pause_remaining_seconds` (К4)
   - Публикует MQTT OFF для текущей зоны
   - DB: `state = 'paused'`, `pause_reason = 'float'`
   - Закрывает мастер-клапан группы немедленно (без delayed close)
   - Блокируется на `float_resume_event.wait()` (с учётом shutdown/cancel, см. 3.6)
5. FloatMonitor запускает таймер: `_on_timeout` через `float_timeout_minutes`

**Возобновление:**
1. FloatMonitor получает level restored для group_id
2. Дебаунс: ждём `float_debounce_seconds`, подтверждаем что уровень стабильно есть
3. FloatMonitor отменяет таймер таймаута
4. FloatMonitor устанавливает `float_resume_event`
5. Worker просыпается из `float_resume_event.wait()`:
   - Проверяет cancel/shutdown/timeout
   - Если `remaining_seconds > 0`:
     - Публикует MQTT ON для текущей зоны (через `exclusive_start_zone`)
     - DB: `state = 'on'`, новый `planned_end_time = now + remaining`
     - Перепланирует watchdog hard_stop
   - Если `remaining_seconds <= 0` (зона «истекла» во время паузы):
     - Переходит к следующей зоне в очереди программы
6. Worker продолжает цикл `while remaining > 0` для текущей зоны

**MQTT retry при resume (С3):** Worker при возобновлении вызывает `exclusive_start_zone()` с retry:
```python
max_retries = 5
for attempt in range(max_retries):
    try:
        exclusive_start_zone(zone_id)
        if state_verifier.verify_async(zone_id, expected='on', timeout=5):
            break
    except Exception as e:
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)  # exponential backoff: 1, 2, 4, 8, 16 сек
            continue
        # MQTT down > 30 сек → аварийный стоп
        log('mqtt_resume_failed', zone_id=zone_id, error=str(e))
        telegram_notify("🚨 MQTT недоступен при возобновлении зоны {name}. Аварийный стоп.")
        queue_manager.cancel_group(group_id)
        return
```

### 3.6 Взаимодействие worker ↔ FloatMonitor

В цикле ожидания зоны (бывший `while remaining > 0` в `_run_program_threaded`):

```python
while remaining > 0:
    # 1. Проверяем отмену группы
    if cancel_event and cancel_event.is_set():
        break
    # 2. Проверяем shutdown
    if shutdown_event.wait(timeout=0):
        break
    # 3. Проверяем паузу поплавка
    if float_monitor.is_paused(group_id):
        # Сохраняем remaining в БД (К4)
        db_update_zone(zone_id, pause_remaining_seconds=remaining)
        # Выключаем зону
        stop_zone(zone_id)
        db_update_zone(zone_id, state='paused', pause_reason='float')
        # Ждём возобновления или таймаута (С8)
        _wait_for_resume_or_cancel(group_id, cancel_event, shutdown_event)
        # После возвращения: проверяем cancel/shutdown/timeout
        if cancel_event and cancel_event.is_set():
            break
        if shutdown_event.wait(timeout=0):
            break
        if float_monitor.is_timed_out(group_id):
            break
        # Возобновляем зону (К3 — worker единственный кто включает)
        exclusive_start_zone(zone_id)  # с retry (С3)
        db_update_zone(zone_id, state='on', pause_remaining_seconds=None)
        continue
    # 4. Обычный sleep 1 секунду
    time.sleep(1)
    remaining -= 1
```

**wait_for_resume_or_cancel() (С8):** Использует Event composition для корректного завершения при shutdown:

```python
def _wait_for_resume_or_cancel(group_id, cancel_event, shutdown_event):
    """Ждёт resume ИЛИ cancel ИЛИ shutdown ИЛИ timeout.
    Проверяет все события периодически (каждую секунду)."""
    while True:
        # Проверяем все события
        if float_monitor.is_resumed(group_id):
            return
        if float_monitor.is_timed_out(group_id):
            return
        if cancel_event and cancel_event.is_set():
            return
        if shutdown_event.wait(timeout=0):
            return
        # Ждём любое событие через float_resume_event с таймаутом
        float_monitor.get_resume_event(group_id).wait(timeout=1.0)
```

Это гарантирует, что worker завершится за **≤1 секунду** после любого сигнала (shutdown, cancel, resume, timeout).

### 3.7 Новое состояние зоны: `paused`

Текущие состояния: `off`, `starting`, `on`, `stopping`.

Добавляем: **`paused`** — зона была включена, но временно приостановлена (поплавок, или в будущем — другие причины).

UI отображает paused-зоны с отдельной иконкой (⏸️) и причиной паузы.

### 3.8 Таймаут и аварийный стоп

Если поплавок не вернулся в течение `float_timeout_minutes`:
1. `_on_timeout(group_id)` вызывается
2. Устанавливает `float_timeout_flag` для группы
3. Worker обнаруживает timeout → выходит из wait → `finally` блок выключает зоны
4. `queue_manager.cancel_group(group_id)` — отменяем всю очередь группы
5. Лог `float_timeout_emergency_stop`
6. Telegram: `"🚨 АВАРИЯ: Группа {name} — уровень воды не восстановился за {N} мин. Все программы группы отменены. Проверьте ёмкость и насос!"`

### 3.9 wb-rules watchdog (независимая защита)

FloatMonitor работает внутри веб-сервера wb-irrigation. Если сервер завис — защиты нет. Поэтому **параллельно** нужен wb-rules скрипт на контроллере Wirenboard.

**Файл: `/etc/wb-rules/float_watchdog.js`** (ECMAScript 5)

Логика:
1. Подписка на MQTT-топик поплавка (дискретный вход)
2. Если поплавок = OFF (уровень низкий):
   - Немедленно выключить ВСЕ реле полива этой группы
   - Закрыть мастер-клапан
   - Записать событие в виртуальное устройство `float-watchdog`
   - Публикует `tripped=1` (см. 3.10)
3. Если поплавок = ON (уровень восстановился):
   - Публикует `tripped=0` (см. 3.10)
   - **НЕ включает зоны обратно** — это делает ТОЛЬКО wb-irrigation
4. Не восстанавливать полив самостоятельно — это делает wb-irrigation

**Виртуальное устройство `float-watchdog`:**
- `enabled` (switch): вкл/выкл watchdog
- `last_event` (text): timestamp + описание последнего срабатывания
- `trip_count` (value): количество срабатываний с момента включения

**Почему wb-rules, а не только wb-irrigation:**
- wb-rules работает на контроллере напрямую, без зависимости от веб-сервера
- Реакция за миллисекунды (MQTT local), а не секунды (HTTP → Python → MQTT)
- Это **последний рубеж защиты** мотора насоса

**Важно:** wb-rules watchdog — это «грубый рубильник». Он не знает о программах, очередях, remaining seconds. Он просто выключает всё. Возобновление — ответственность wb-irrigation после восстановления уровня.

### 3.10 Конфликт wb-rules watchdog ↔ wb-irrigation (С5)

**Полный lifecycle топика `tripped`:**

wb-rules watchdog управляет топиками `/devices/float-watchdog/controls/group_{N}_tripped`:
- **`tripped=1`** — публикуется при поплавок OFF (реле уже выключены wb-rules)
- **`tripped=0`** — публикуется при поплавок ON (но реле НЕ включаются wb-rules)

FloatMonitor в wb-irrigation подписан на эти топики:
- При получении **`tripped=1`**:
  - Если FloatMonitor НЕ в состоянии паузы — принудительно входит в режим паузы (зоны уже OFF благодаря wb-rules)
  - Если FloatMonitor уже в паузе — игнорирует (уже обработано)
- При получении **`tripped=0`**:
  - FloatMonitor **НЕ делает автоматический resume**
  - Resume происходит ТОЛЬКО по собственной логике FloatMonitor: float ON + debounce подтверждён
  - Это предотвращает race condition между wb-rules и FloatMonitor

**Правила:**
- wb-rules **НИКОГДА** не включает зоны обратно (только OFF)
- FloatMonitor — единственный, кто принимает решение о resume (через worker)
- `tripped` — информационный сигнал для синхронизации состояний

### 3.11 Hysteresis поплавка (С6)

Защита от быстрых циклов pause/resume, которые вызывают гидроудары и износ оборудования.

**Параметры:**
- `FLOAT_MIN_RUN_TIME = 60` сек — минимальное время работы после resume перед повторной паузой
- `FLOAT_MAX_TRIPS = 3` — максимум срабатываний за период
- `FLOAT_TRIP_WINDOW = 300` сек (5 мин) — окно подсчёта срабатываний

**Логика:**

```python
@dataclass
class HysteresisState:
    last_resume_at: Optional[float] = None  # time.monotonic()
    trip_times: List[float] = field(default_factory=list)  # monotonic timestamps
```

**При _on_level_low (pause):**
1. Записать timestamp в `trip_times`
2. Очистить записи старше `FLOAT_TRIP_WINDOW`
3. Если `len(trip_times) >= FLOAT_MAX_TRIPS`:
   - **Аварийный стоп** всей группы
   - Лог: `float_hysteresis_emergency_stop`
   - Telegram: «🚨 Группа {name}: поплавок нестабилен ({N} срабатываний за 5 мин). АВАРИЙНЫЙ СТОП. Проверьте датчик и ёмкость!»
   - Очередь группы отменяется
   - **Не пытаться resume** до ручного вмешательства
4. Если `last_resume_at` и `(now - last_resume_at) < FLOAT_MIN_RUN_TIME`:
   - Лог: `float_pause_too_soon` (предупреждение, пауза всё равно происходит — безопасность приоритет)

**При _on_level_restored (resume):**
1. Записать `last_resume_at = time.monotonic()`
2. Resume происходит штатно (дебаунс уже обеспечивает минимальную паузу)

---

## 4. Расширение check_program_conflicts()

### 4.1 Учёт максимального погодного коэффициента

Текущая `check_program_conflicts()` в `db/programs.py` использует base durations. При погодном коэффициенте до 200% реальная длительность может удвоиться.

**Изменение:**

```
check_program_conflicts(program_id, time, zones, days, weather_factor=None)
```

- Если `weather_factor` не передан — берётся `max_weather_coefficient` из settings (по умолчанию 200)
- `total_duration = sum(base_durations) * weather_factor / 100`
- Проверка пересечения по увеличенным длительностям

### 4.2 Два уровня предупреждений

| Уровень | Условие | Действие в UI |
|---|---|---|
| **WARNING** (жёлтый) | Конфликт при weather_factor > 100% (т.е. только при жаре) | Показать предупреждение, разрешить сохранить |
| **ERROR** (красный) | Конфликт при base durations (weather_factor = 100%) | Показать ошибку, разрешить сохранить с подтверждением |

Ни WARNING, ни ERROR **не блокируют** сохранение программы. Очередь (секция 2) гарантирует безопасность в рантайме. Но предупреждения помогают пользователю оптимизировать расписание.

### 4.3 Новый эндпоинт для превью

Текущий `POST /api/programs/check-conflicts` дополняется:

**Запрос:**
```json
{
  "time": "06:00",
  "zones": [1, 2, 3],
  "days": [0, 2, 4],
  "program_id": null,
  "include_weather": true
}
```

**Ответ:**
```json
{
  "has_conflicts": true,
  "conflicts": [
    {
      "program_id": 2,
      "program_name": "Вечер",
      "level": "warning",
      "overlap_minutes": 9,
      "weather_factor": 150,
      "message": "При погодном коэффициенте 150%+ программы пересекутся на ~9 мин (группа Насос-1). Очередь обеспечит безопасность, но вторая программа начнёт позже."
    }
  ],
  "current_weather_coefficient": 120
}
```

---

## 5. Модификации scheduler

### 5.1 Методы, которые МЕНЯЮТСЯ

| Метод | Что меняется |
|---|---|
| `job_run_program()` | Вместо прямого вызова `_run_program_threaded` → разбивка по группам + `queue_manager.enqueue()` + регистрация в ProgramCompletionTracker |
| `_run_program_threaded()` | **Удаляется** как entry point. Логика переносится в worker `ProgramQueueManager._run_entry()` |
| `cancel_group_jobs()` | Добавляется вызов `queue_manager.cancel_group(group_id)` |
| `recover_missed_runs()` | Вместо прямого запуска → `queue_manager.enqueue()` |
| `init_scheduler()` | Создаёт `ProgramQueueManager`, запускает `FloatMonitor` |
| `stop()` | Вызывает `queue_manager.shutdown()`, `float_monitor.stop()` |

### 5.2 Новые методы / классы

| Что | Где | Описание |
|---|---|---|
| `ProgramQueueManager` | `services/program_queue.py` | Управление per-group очередями |
| `ProgramQueueManager.enqueue()` | — | Добавить entry в очередь группы (reject при переполнении) |
| `ProgramQueueManager._worker()` | — | Воркер-поток: извлекает и выполняет entries |
| `ProgramQueueManager._run_entry()` | — | Выполнение одного entry (обёрнуто в try/finally) |
| `ProgramCompletionTracker` | `services/program_queue.py` | Отслеживание мульти-группных программ |
| `FloatMonitor` | `services/float_monitor.py` | MQTT-подписка на поплавки, пауза/возобновление |
| `FloatMonitor._on_float_message()` | — | Обработка MQTT-сообщений от поплавка |

### 5.3 Как _run_entry() отличается от _run_program_threaded()

`_run_entry(entry: QueueEntry)` — это очищенная версия `_run_program_threaded()` с ключевыми отличиями:

1. **Работает с зонами одной группы** (не всех групп программы)
2. **Не разбивает по группам** — это уже сделано при enqueue
3. **Проверяет float_pause** в цикле ожидания (см. секцию 3.6)
4. **Не делает peer-OFF перед стартом** — очередь гарантирует, что на группе работает только одна entry. Peer-OFF остаётся только в `exclusive_start_zone()` как страховка
5. **Обновляет entry.state** при завершении
6. **Weather coefficient вычисляется в момент старта каждой зоны** (К5), а не при enqueue

**Обработка ошибок (С10):** `_run_entry()` обёрнут в `try/finally`:

```python
def _run_entry(self, entry: QueueEntry, cancel_event, shutdown_event):
    current_zone_id = None
    try:
        for zone_id in entry.zone_ids:
            current_zone_id = zone_id
            if cancel_event.is_set() or shutdown_event.is_set():
                break
            
            # Weather coefficient — в момент старта зоны (К5)
            weather_coeff = get_current_weather_coefficient()
            adjusted_duration = base_duration * weather_coeff / 100
            
            exclusive_start_zone(zone_id)
            remaining = adjusted_duration
            
            while remaining > 0:
                # ... цикл из секции 3.6 ...
                pass
            
            stop_zone(zone_id)
            current_zone_id = None
        
        entry.state = QueueEntryState.COMPLETED
    
    except Exception as e:
        entry.state = QueueEntryState.FAILED
        log('run_entry_failed', entry_id=entry.entry_id, error=str(e))
        telegram_notify(f"🚨 Ошибка выполнения программы {entry.program_name}: {e}")
    
    finally:
        # Гарантированное выключение текущей зоны (К1)
        if current_zone_id is not None:
            try:
                stop_zone(current_zone_id)
            except Exception:
                log('stop_zone_failed_in_finally', zone_id=current_zone_id)
        
        # Уведомляем ProgramCompletionTracker (С1)
        if entry.program_run_id:
            all_done = self._completion_tracker.entry_finished(
                entry.program_run_id, entry.entry_id
            )
            if all_done:
                log('program_finish', program_id=entry.program_id)
                telegram_notify(f"✅ Программа {entry.program_name} завершена")
```

**Worker после _run_entry() продолжает со следующей entry** — необработанное исключение не убивает worker.

### 5.4 Диаграмма нового потока

```
APScheduler CronTrigger fires
    │
    ▼
job_run_program(program_id, all_zones, name)
    │
    ├── Weather skip check (как раньше, на уровне программы)
    │   └── skip → return
    │
    ├── Группировка зон: zones_by_group = {gid: [zids]}
    │
    ├── program_run_id = str(uuid4())
    │
    ├── Для каждого (gid, zids):
    │   └── entry = queue_manager.enqueue(program_id, name, gid, zids, scheduled_time=now)
    │       │   └── None → переполнение, лог, Telegram, skip
    │       │
    │       ├── _global_lock → получить/создать GroupQueue → release
    │       ├── GroupQueue.lock
    │       ├── Проверка maxlen → reject если переполнение
    │       ├── Создать QueueEntry (entry_id = uuid4())
    │       ├── deque.append(entry)
    │       ├── Если worker не запущен → Thread(name=f"queue-worker-{gid}", daemon=True).start()
    │       └── release lock
    │
    ├── ProgramCompletionTracker.register(program_run_id, entry_ids) (если >1 группа)
    │
    └── return (не ждём!)

_worker(group_id):
    while True:
        ├── lock → entry = deque.popleft() → unlock
        │   └── если deque пуста → break (поток завершается)
        │
        ├── Проверка max_wait_time (только WAITING время, с учётом excluded_wait_seconds) (К6)
        │   └── expired → entry.state=EXPIRED, log, continue
        │
        ├── entry.state = RUNNING
        │
        ├── try:
        │       _run_entry(entry)  # try/finally внутри (С10)
        │   except Exception:
        │       entry.state = FAILED, log  # worker продолжает
        │
        └── продолжаем цикл
```

---

## 6. UI изменения

### 6.1 status.html — Индикатор очереди

На странице статуса (status.html) для каждой группы отображать:

**Когда очередь не пуста:**
```
┌──────────────────────────────────────────────┐
│ Группа 1                                     │
│ ▶ Выполняется: «Утро» (зоны 1→2→3)         │
│   Текущая зона: 2 «Газон» — 8:24 осталось   │
│ ⏳ В очереди: «Вечер» (зоны 4, 5)           │
│   Ожидает ~12 мин                            │
└──────────────────────────────────────────────┘
```

**Когда поплавок сработал:**
```
┌──────────────────────────────────────────────┐
│ Группа 1  ⚠️ ПАУЗА (низкий уровень воды)     │
│ ⏸ Приостановлено: «Утро» зона 2 «Газон»     │
│   Осталось: 8:24 (будет продолжено)          │
│   Пауза с: 06:23:15                         │
│   Таймаут через: 22 мин                      │
└──────────────────────────────────────────────┘
```

**Данные:** SSE endpoint `/api/events` уже существует. Добавить events:
- `queue_update` — при изменении состояния очереди
- `float_pause` — при срабатывании поплавка
- `float_resume` — при восстановлении уровня

### 6.2 settings.html — Настройки поплавка (per-group)

В разделе редактирования группы добавить блок «Защита уровня воды»:

```
┌── Защита уровня воды ──────────────────────────┐
│ [✓] Включить поплавковый датчик                 │
│                                                  │
│ MQTT-топик: [/devices/wb-gpio/controls/A1_IN  ] │
│ MQTT-сервер: [Локальный Wirenboard         ▾]   │
│ Тип датчика: (●) NO  (○) NC                     │
│ Таймаут (мин): [30  ]                           │
│   ℹ️ Установите с учётом времени заполнения      │
│   вашей ёмкости. Если уровень не                 │
│   восстановится за это время — аварийный стоп.   │
│ Дебаунс (сек): [5   ]                           │
│                                                  │
│ Текущий статус: 🟢 Уровень OK                   │
└──────────────────────────────────────────────────┘
```

### 6.3 settings.html — Настройки очереди

В разделе «Общие настройки» добавить:

```
┌── Очередь программ ────────────────────────────┐
│ Макс. ожидание в очереди (мин): [120 ]         │
│ Макс. погодный коэффициент (%):  [200 ]         │
│                                                  │
│ ℹ️ Если программа не смогла запуститься в        │
│   течение указанного времени, она будет          │
│   отменена.                                      │
└──────────────────────────────────────────────────┘
```

---

## 7. БД миграции

### 7.1 Новые поля в таблице `groups`

```sql
ALTER TABLE groups ADD COLUMN float_enabled INTEGER DEFAULT 0;
ALTER TABLE groups ADD COLUMN float_mqtt_topic TEXT DEFAULT NULL;
ALTER TABLE groups ADD COLUMN float_mqtt_server_id INTEGER DEFAULT NULL;
ALTER TABLE groups ADD COLUMN float_mode TEXT DEFAULT 'NO';
ALTER TABLE groups ADD COLUMN float_timeout_minutes INTEGER DEFAULT 30;
ALTER TABLE groups ADD COLUMN float_debounce_seconds INTEGER DEFAULT 5;
```

### 7.2 Новые поля в таблице `zones`

```sql
-- Причина паузы (NULL = не на паузе)
ALTER TABLE zones ADD COLUMN pause_reason TEXT DEFAULT NULL;
-- Оставшееся время полива при паузе, в секундах (К4)
ALTER TABLE zones ADD COLUMN pause_remaining_seconds INTEGER DEFAULT NULL;
```

Поле `state` уже TEXT, значение `'paused'` просто добавляется в допустимый набор.

### 7.3 Новая таблица `program_queue_log`

```sql
CREATE TABLE IF NOT EXISTS program_queue_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL,            -- UUID entry
    program_id INTEGER NOT NULL,
    program_run_id TEXT,               -- UUID запуска программы (для ProgramCompletionTracker)
    group_id INTEGER NOT NULL,
    zone_ids TEXT NOT NULL,            -- JSON array
    scheduled_time TEXT NOT NULL,      -- ISO datetime
    enqueued_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    state TEXT NOT NULL,               -- waiting/running/completed/cancelled/expired/failed
    wait_seconds INTEGER,              -- сколько ждала в очереди (только WAITING время)
    run_seconds INTEGER,               -- сколько выполнялась
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX idx_pql_program ON program_queue_log(program_id);
CREATE INDEX idx_pql_group ON program_queue_log(group_id);
CREATE INDEX idx_pql_created ON program_queue_log(created_at);
```

Назначение: история работы очереди для диагностики и отчётов. Запись создаётся при enqueue, обновляется при старте и завершении.

### 7.4 Новая таблица `float_events`

```sql
CREATE TABLE IF NOT EXISTS float_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,          -- 'low', 'restored', 'timeout', 'hysteresis_stop'
    paused_zones TEXT,                 -- JSON: [{zone_id, remaining_seconds}]
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX idx_fe_group ON float_events(group_id);
CREATE INDEX idx_fe_created ON float_events(created_at);
```

### 7.5 Новые настройки в `settings`

```sql
INSERT OR IGNORE INTO settings (key, value) VALUES ('max_queue_wait_minutes', '120');
INSERT OR IGNORE INTO settings (key, value) VALUES ('max_weather_coefficient', '200');
```

### 7.6 Миграция

Все изменения — в один файл миграции `db/migrations.py`, метод `_migrate_vXX_program_queue()`. Идемпотентно: `ALTER TABLE ... ADD COLUMN` с проверкой через `PRAGMA table_info`.

**SQLite contention (С4):**
- Подключение к БД: `sqlite3.connect(database, timeout=30)` (вместо дефолтных 5 сек)
- `PRAGMA busy_timeout=30000;` — при каждом подключении
- FloatMonitor при паузе/resume использует batch writes: один `executemany()` для обновления всех зон группы, а не N отдельных UPDATE
- WAL mode уже включён (`PRAGMA journal_mode=WAL`)

---

## 8. Тест-сценарии

### 8.1 Очередь программ

| # | Сценарий | Входные данные | Ожидаемый результат |
|---|---|---|---|
| T1 | Две программы на одной группе, пересечение по времени | Prog A: 06:00, зоны [1,2,3] (гр.1), 45 мин. Prog B: 06:30, зоны [4,5] (гр.1), 20 мин | A работает полностью. B встаёт в очередь, стартует в ~06:45. Обе завершаются успешно |
| T2 | Две программы на разных группах, одно время | Prog A: 06:00, зоны [1,2] (гр.1). Prog B: 06:00, зоны [10,11] (гр.2) | Обе стартуют одновременно, параллельно, без очереди |
| T3 | Программа с зонами из двух групп | Prog A: зоны [1(гр.1), 2(гр.1), 10(гр.2)] | Создаются 2 entry: {гр.1: [1,2]} и {гр.2: [10]}. Выполняются параллельно. ProgramCompletionTracker отслеживает общее завершение |
| T4 | Три программы на одной группе, каскадная очередь | Prog A: 06:00, 30 мин. Prog B: 06:15, 20 мин. Prog C: 06:20, 15 мин | A работает. B и C в очереди. После A → B (в ~06:30). После B → C (в ~06:50) |
| T5 | Программа в очереди превышает max_wait_time | Prog A: 06:00, 150 мин. Prog B: 06:30. max_wait=120 мин | A работает до 08:30. B ждёт с 06:30. В 08:30 B ждала 120 мин WAITING → EXPIRED |
| T6 | Отмена группы во время работы очереди | Prog A работает, Prog B в очереди. Пользователь нажимает «Стоп группа» | cancel_group() → event.set() → ждёт worker. Worker: finally выключает зону. B отменяется (CANCELLED). Нет двойного stop |
| T7 | recover_missed_runs через очередь | Сервис рестартнул в 06:25. Prog A запланирована на 06:00 (зоны по 15 мин). Prog B: 06:30 | Recovery enqueues A (оставшиеся зоны). APScheduler enqueues B в 06:30. A первая, B ждёт |
| T8 | Weather skip целой программы | Prog A: идёт дождь выше порога | enqueue не происходит (skip на уровне job_run_program). Очередь не затрагивается |

### 8.2 Поплавок ёмкости

| # | Сценарий | Входные данные | Ожидаемый результат |
|---|---|---|---|
| T9 | Поплавок OFF во время полива | Гр.1: зона 2 поливает, remaining=8 мин. Поплавок гр.1 → OFF | Worker обнаруживает float_pause_event. Сохраняет remaining=8 мин в БД. Зона 2 OFF (state=paused). Ждёт resume |
| T10 | Поплавок ON — возобновление | Зона 2 на паузе, remaining=8 мин. Поплавок → ON | FloatMonitor устанавливает float_resume_event. Worker просыпается, включает зону 2 (exclusive_start_zone), работает оставшиеся 8 мин |
| T11 | Поплавок не восстановился (таймаут) | Поплавок OFF. timeout=30 мин. Прошло 30 мин, поплавок всё ещё OFF | Аварийный стоп: worker выходит из wait → finally выключает зоны. Очередь гр.1 отменена. Telegram-алерт |
| T12 | Поплавок дребезг (<debounce) | Поплавок OFF на 2 сек, потом ON. debounce=5 сек | Дребезг отфильтрован. Пауза НЕ происходит |
| T13 | Поплавок одной группы, другая не затронута | Гр.1 и Гр.2 поливают. Поплавок гр.1 → OFF | Гр.1 на паузе. Гр.2 продолжает работать без изменений |
| T14 | Поплавок OFF, в очереди ждёт программа | Гр.1: Prog A работает, Prog B в очереди. Поплавок → OFF | Prog A паузится (worker). Prog B остаётся в очереди (excluded_wait_seconds увеличивается). После восстановления — A доливает, затем B |
| T15 | wb-rules watchdog сработал раньше wb-irrigation | wb-rules выключил реле (поплавок OFF). wb-irrigation получает tripped=1 через MQTT | FloatMonitor входит в режим паузы. Worker корректно обрабатывает. При tripped=0 resume НЕ автоматический |

### 8.3 check_program_conflicts

| # | Сценарий | Входные данные | Ожидаемый результат |
|---|---|---|---|
| T16 | Конфликт только при weather 150%+ | Prog A: 06:00, 45 мин base. Prog B: 07:00. weather_factor=150% → A длится 67.5 мин → до 07:07 | WARNING: «При погодном коэффициенте 150%+ пересечение ~7 мин» |
| T17 | Конфликт при base durations | Prog A: 06:00, 45 мин. Prog B: 06:30, та же группа | ERROR: «Пересечение 15 мин при базовых длительностях» |
| T18 | Нет конфликта даже при 200% | Prog A: 06:00, 20 мин base. Prog B: 07:00. 200% → A = 40 мин → до 06:40 | Нет конфликта. Программы сохраняются без предупреждений |

### 8.4 Новые тест-сценарии (v1.1)

| # | Сценарий | Входные данные | Ожидаемый результат |
|---|---|---|---|
| T19 | cancel_group() во время _run_entry() — нет двойного stop (К1) | Prog A работает (зона 3 ON). Пользователь нажимает «Стоп группа» | cancel_group() устанавливает event, ждёт worker. Worker в finally делает stop_zone(3). Зона гарантированно OFF. stop_zone вызван ровно 1 раз. cancel_group() НЕ вызывает stop_all_in_group() |
| T20 | Перезагрузка контроллера во время float pause (К4) | Зона 3 на паузе, remaining=12 мин, pause_remaining_seconds=720 в БД. Контроллер перезагрузился | При boot: stop_on_boot_active_zones() находит зоны с state='paused' → безусловно OFF (MQTT + DB). remaining_seconds в БД для диагностики. Зоны НЕ возобновляются автоматически |
| T21 | MQTT-брокер down при float resume (С3) | Зона 2 на паузе. Float resume. MQTT-брокер недоступен | Worker делает retry с backoff (1, 2, 4, 8, 16 сек). Если MQTT down > 30 сек → аварийный стоп группы. Telegram-алерт. Зоны НЕ зависают в state='on' |
| T22 | 3 float pause/resume подряд за 1 минуту — hysteresis (С6) | Поплавок дребезжит: OFF→ON→OFF→ON→OFF за 3 минуты | После 3-го OFF: hysteresis срабатывает → аварийный стоп группы. Telegram: «поплавок нестабилен». Очередь отменена. Ручное вмешательство |
| T23 | Программа с зонами из 3 групп — completion tracker (С1) | Prog «Утро» → 3 entry в 3 группы. Гр.1 завершилась за 20 мин, Гр.2 ждала 40 мин + 15 мин работы, Гр.3 за 10 мин | ProgramCompletionTracker: entry_finished() вызывается 3 раза. После 3-го → program_finish лог + Telegram «Программа Утро завершена» |
| T24 | max_wait_time во время float pause (К6) | Prog A стартует 06:00 (90 мин). Prog B enqueue 06:30 (max_wait=120). Float pause 07:00-07:30 (30 мин) | Float pause 30 мин → excluded_wait_seconds для B += 30. Фактическое ожидание B: (now - 06:30) - 30 мин. B НЕ получает EXPIRED в 08:30 (120 мин абсолютных), а только через 120 мин WAITING |
| T25 | Переполнение очереди (С9) | Worker застрял. 21 программ пытаются enqueue в одну группу (maxlen=20) | 20 записей принимаются. 21-я → reject (enqueue возвращает None). Лог queue_overflow. Telegram-уведомление |
| T26 | shutdown() во время float pause (С8) | Зоны на float pause. systemd посылает SIGTERM | _shutdown_event.set(). wait_for_resume_or_cancel() проверяет shutdown каждую секунду → возвращается. Worker в finally выключает зоны. Поток завершается за ≤2 сек. Нет SIGKILL |

---

## 9. Сравнение с Hunter / Rain Bird / Rachio / OpenSprinkler

### 9.1 Как они решают проблему

| Аспект | Hunter Pro-HC | Rain Bird ESP-ME3 | OpenSprinkler | Rachio | wb-irrigation (план) |
|---|---|---|---|---|---|
| **Очередь** | Глобальная. Одна станция одновременно | Глобальная. Одна программа одновременно | Per-sequential-group. Группы параллельны | Глобальная + smart scheduling | **Per-group queue.** Группы параллельны |
| **Приоритеты** | Нет (FIFO stacking) | Программы A>B>C>D | Нет | Smart reordering | **Нет (FIFO, равноправие)** |
| **Параллельность** | Нет | Нет | Да (Sequential Groups A-D + Parallel Group P) | Ограниченная | **Да (разные группы = параллельность)** |
| **Weather overlap** | Учитывает при Predictive | Не учитывает | Предупреждение в UI | Авто-пересчёт | **WARNING/ERROR в UI, очередь в рантайме** |
| **Tank/well protection** | Нет (внешний датчик + реле) | Нет | Нет встроенного | Нет | **Встроенный per-group float monitor** |
| **Независимый watchdog** | Нет (всё в контроллере) | Нет | Нет | Нет | **wb-rules на контроллере** |

### 9.2 Что берём

| От кого | Что | Почему |
|---|---|---|
| **OpenSprinkler** | Per-group sequential queues с параллельностью между группами | Идеально подходит к нашей архитектуре per-group |
| **Hunter** | FIFO stacking без приоритетов | Простота, предсказуемость. Нет причин усложнять приоритетами |
| **Hunter** | Max wait time (implicit в их системе — 24ч) | Защита от бесконечного накопления очереди |
| **Все** | Предупреждение о конфликтах при создании | Но не блокировка — очередь гарантирует безопасность |

### 9.3 Что НЕ берём

| Что | Почему нет |
|---|---|
| **Приоритеты программ** (Rain Bird A>B>C>D) | Заказчик явно указал: все программы равноправны |
| **Cycle and Soak** | Отдельная фича, не в scope этой спеки |
| **Smart reordering** (Rachio) | Overengineering для текущих задач. FIFO достаточно |
| **Глобальная одна станция** (Hunter) | У нас разные группы → разные очереди. Глобальная блокировка убьёт параллельность |

### 9.4 Наше уникальное преимущество

Ни один из коммерческих контроллеров не имеет **встроенной per-group защиты от сухого хода с wb-rules watchdog**:
- Hunter/Rain Bird: нужен внешний датчик + реле, отдельная проводка
- OpenSprinkler: нет встроенного, нужен скрипт
- Rachio: облачное решение, нет локальной защиты

У нас: MQTT-датчик → FloatMonitor (пауза/возобновление с remaining) + wb-rules watchdog (аппаратный kill switch). Два уровня защиты, полностью локальные, без облака.

---

## 10. Порядок реализации

### Этап 1: ProgramQueueManager (ядро)

**Файлы:**
- `services/program_queue.py` — новый

**Задачи:**
1. Реализовать `QueueEntry`, `GroupQueue`, `ProgramQueueManager`
2. `entry_id` через `uuid4()` (С2)
3. `deque(maxlen=MAX_QUEUE_SIZE)` с reject при переполнении (С9)
4. Метод `enqueue()` с FIFO-логикой
5. Метод `_worker()` — воркер-поток per-group (`Thread(name=..., daemon=True)`)
6. Метод `_run_entry()` — обёрнут в `try/finally` (С10), worker как единственный ответственный за OFF (К1)
7. `cancel_entry()`, `cancel_program()`, `cancel_group()` — cancel_group ждёт worker (К1), `shutdown()`
8. Lock ordering: `_global_lock → GroupQueue.lock` (К2), worker НИКОГДА не захватывает `_global_lock`
9. `get_queue_state()`, `get_all_queues_state()` — get_all копирует dict под global_lock, итерирует копию (К2)
10. `ProgramCompletionTracker` для мульти-группных программ (С1)
11. max_wait_time считает только WAITING время (К6)
12. Weather coefficient в момент старта зоны (К5)

**Зависимости:** Нет (можно делать изолированно)

**Тесты:** T1–T8, T19, T23, T24, T25

### Этап 2: Интеграция с scheduler

**Файлы:**
- `irrigation_scheduler.py` — модификация

**Задачи:**
1. `job_run_program()` → группировка зон + `queue_manager.enqueue()` + ProgramCompletionTracker
2. `cancel_group_jobs()` → `queue_manager.cancel_group()` (cancel_group ждёт worker, К1)
3. `recover_missed_runs()` → через `enqueue()`
4. `init_scheduler()` → создание `ProgramQueueManager`
5. `stop()` → `queue_manager.shutdown()` с корректной остановкой при float pause (С8)
6. Удаление/рефакторинг `_run_program_threaded()`

**Зависимости:** Этап 1

**Тесты:** T6, T26

### Этап 3: БД миграции

**Файлы:**
- `db/migrations.py` — новая миграция
- `db/groups.py` — новые поля в get/update

**Задачи:**
1. ALTER TABLE groups (float_* поля)
2. ALTER TABLE zones (pause_reason, pause_remaining_seconds) (К4)
3. CREATE TABLE program_queue_log (с program_run_id для completion tracker)
4. CREATE TABLE float_events (с event_type 'hysteresis_stop')
5. INSERT settings (max_queue_wait_minutes, max_weather_coefficient)
6. `PRAGMA busy_timeout=30000` при подключении (С4)
7. Batch writes для массовых обновлений зон (С4)

**Зависимости:** Нет (можно параллельно с Этапом 1)

### Этап 4: FloatMonitor

**Файлы:**
- `services/float_monitor.py` — новый

**Задачи:**
1. MQTT-подписка per-group
2. Дебаунс и определение logical_level
3. `_on_level_low()` — устанавливает float_pause_event, worker сам выключает зону (К3)
4. `_on_level_restored()` — устанавливает float_resume_event, worker сам включает зону (К3)
5. `_on_timeout()` — аварийный стоп
6. Сохранение remaining в БД при pause (К4)
7. MQTT retry с backoff при resume (С3)
8. `wait_for_resume_or_cancel()` с shutdown_event (С8)
9. Hysteresis: min_run_time=60 сек, 3 trips за 5 мин → аварийный стоп (С6)
10. Tripped lifecycle: tripped=1 при OFF, tripped=0 при ON, NO auto-resume при tripped=0 (С5)
11. Telegram-уведомления
12. excluded_wait_seconds для WAITING entries при float pause (К6)

**Зависимости:** Этап 1 (ProgramQueueManager), Этап 3 (БД)

**Тесты:** T9–T15, T20, T21, T22, T24

### Этап 5: check_program_conflicts() v2

**Файлы:**
- `db/programs.py` — модификация
- `routes/programs_api.py` — модификация

**Задачи:**
1. Параметр `weather_factor` в `check_program_conflicts()`
2. Два уровня предупреждений (WARNING/ERROR)
3. Обновить API-ответ

**Зависимости:** Этап 3 (settings)

**Тесты:** T16–T18

### Этап 6: UI

**Файлы:**
- `templates/status.html` — очередь и поплавок
- `templates/settings.html` — настройки поплавка и очереди
- `static/js/` — обработка SSE events
- `routes/groups_api.py` — API для float-настроек группы

**Задачи:**
1. Индикатор очереди на status.html
2. Индикатор паузы поплавка
3. Настройки поплавка в редактировании группы (с подсказкой по таймауту)
4. Настройки max_queue_wait, max_weather_coefficient
5. SSE events: queue_update, float_pause, float_resume

**Зависимости:** Этапы 1–5

### Этап 7: wb-rules watchdog

**Файлы:**
- `/etc/wb-rules/float_watchdog.js` — на контроллере

**Задачи:**
1. Виртуальное устройство `float-watchdog`
2. Подписка на MQTT-топик(и) поплавков
3. При LOW → выключить все реле группы
4. Публикация `tripped=1` при LOW, `tripped=0` при HIGH (С5)
5. **НИКОГДА** не включает зоны обратно
6. Интеграция FloatMonitor с `tripped` топиком (С5)

**Зависимости:** Этапы 3, 4 (для согласованности топиков)

**Тесты:** T15

### Этап 8: Queue API + логирование

**Файлы:**
- `routes/queue_api.py` — новый blueprint
- `db/queue.py` — новый репозиторий (опционально)

**Задачи:**
1. `GET /api/queue` — состояние всех очередей
2. `GET /api/queue/<group_id>` — очередь конкретной группы
3. `DELETE /api/queue/<entry_id>` — отменить entry
4. `GET /api/float` — состояние всех поплавков
5. Запись в `program_queue_log` при каждом изменении (с program_run_id)
6. Запись в `float_events` (включая hysteresis_stop)

**Зависимости:** Этапы 1, 4

---

## 11. Восстановление после перезагрузки (Boot Recovery)

### 11.1 Зоны в состоянии `paused` при boot

При старте сервиса `stop_on_boot_active_zones()` дополняется обработкой `paused` зон:

```python
def stop_on_boot_active_zones():
    """При boot выключить все зоны, которые не в состоянии 'off'."""
    active_zones = get_zones_by_states(['starting', 'on', 'stopping', 'paused'])
    for zone in active_zones:
        # Безусловно OFF — после перезагрузки нет гарантии уровня воды
        publish_mqtt_off(zone.mqtt_topic)
        update_zone(zone.id, state='off', pause_reason=None, pause_remaining_seconds=None)
        log('boot_zone_off', zone_id=zone.id, previous_state=zone.state)
```

**Почему безусловно OFF:** После перезагрузки контроллера:
- Уровень воды в ёмкости неизвестен (поплавок мог измениться)
- Очередь программ в памяти потеряна
- `remaining_seconds` сохранён в БД, но контекст программы утрачен
- Безопаснее выключить всё и дождаться планового запуска

### 11.2 Восстановление очереди

Очередь (`deque`) хранится в памяти и **не персистится** в БД при boot. Это by design:
- Программы с очереди могут быть неактуальны после перезагрузки
- `recover_missed_runs()` восстановит пропущенные запуски через `enqueue()`
- Это безопаснее, чем пытаться возобновить in-flight entries

### 11.3 Зоны с pause_remaining_seconds в БД

Поле `pause_remaining_seconds` в таблице zones сохраняется при float pause (К4). При boot:
- Используется для **диагностики** (лог: «зона X была на паузе, remaining={N} сек»)
- **НЕ используется** для автоматического возобновления
- Сбрасывается в NULL при boot cleanup

### 11.4 FloatMonitor при boot

1. Подписывается на MQTT-топики поплавков
2. Считывает текущее состояние поплавков
3. Если поплавок LOW при boot — **не паузирует** (нечего паузить, зоны уже OFF)
4. Состояние `level_ok=False` запоминается для будущих запусков
5. При следующем enqueue → worker проверит float state перед стартом зоны

---

## Приложение A: Граф зависимостей этапов

```
Этап 1 (QueueManager) ──┬──→ Этап 2 (Scheduler integration)
                         │
Этап 3 (DB миграции) ───┼──→ Этап 4 (FloatMonitor) ──→ Этап 7 (wb-rules)
                         │
                         ├──→ Этап 5 (Conflicts v2)
                         │
                         └──→ Этап 6 (UI) ← зависит от всех
                                               │
                                               └──→ Этап 8 (Queue API)
```

Минимальный жизнеспособный набор: **Этапы 1 + 2 + 3** — очередь работает, поплавок и UI добавляются позже.

## Приложение B: Оценка объёма

| Этап | Новый код (строк, оценка) | Модификации |
|---|---|---|
| 1. ProgramQueueManager | ~400 (+ProgramCompletionTracker, hysteresis) | — |
| 2. Scheduler integration | ~60 | ~150 (рефакторинг) |
| 3. DB миграции | ~50 (+ pause_remaining_seconds, busy_timeout) | ~40 (groups.py, zones.py) |
| 4. FloatMonitor | ~450 (+retry, hysteresis, event composition) | ~40 (worker loop) |
| 5. Conflicts v2 | ~50 | ~40 (programs.py) |
| 6. UI | ~200 (HTML/JS) | ~100 (templates) |
| 7. wb-rules | ~100 (JS, ES5, +tripped lifecycle) | — |
| 8. Queue API | ~120 | — |
| **Итого** | **~1430** | **~370** |

Общий объём: ~1800 строк нового/изменённого кода. Реалистичная оценка с учётом тестов: x1.5 → ~2700 строк.