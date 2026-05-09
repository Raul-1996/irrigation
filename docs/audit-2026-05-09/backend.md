# Backend Architecture Audit — wb-irrigation

**Дата:** 2026-05-09
**Скоуп:** `routes/`, `services/`, `db/`, `scheduler/`, `app.py`, `irrigation_scheduler.py`, `run.py`, `database.py`, `utils.py`, `config.py`, `constants.py`
**Режим:** READ-ONLY, обзорный (10-15 мин), без правок и тестов
**Контекст:** коммерческий single-tenant продукт, Flask + APScheduler + paho-mqtt + SQLite, ~17K LOC Python

---

## Статус прошлых багов (BUGS-REPORT.md / REFACTOR-REPORT.md от 2026-04-02)

### BUGS-REPORT — статус

| # | Описание | Статус | Подтверждение |
|---|----------|--------|---------------|
| CRITICAL #1 | `settings.py:31,73` — `sqlite3.OperationalError` без импорта → NameError | **FIXED** | `routes/settings.py:2` содержит `import sqlite3` |
| HIGH #2 | `_is_status_action` дубликат определения в одном файле | **FIXED** | Единственное определение в `routes/settings.py:355` |
| HIGH #3 | Прямые `sqlite3.connect()` в `routes/weather_*` минуя репозитории | **FIXED** | Миграция в пакет `services/weather/` (adjustment, cache, client, merge, models, service, singletons) |
| HIGH #4 | Логика погоды размазана по `weather_adjustment.py`, `weather_merged.py` | **FIXED (с долгом)** | Файлы превращены в backward-compat stubs (8 LOC и 92 LOC), но **остались навсегда** — см. P1-5 |

### REFACTOR-REPORT — статус god-файлов

| Файл | Было (LOC) | Сейчас (LOC) | Статус |
|------|-----------|--------------|--------|
| `irrigation_scheduler.py` | 1365 | **1649** | **РЕГРЕССИЯ** — вырос вместо сокращения |
| `app.py` | 580+ | 611 | без изменений (ожидаемо после blueprints) |
| `database.py` | god-file | разбит | OK — `db/` пакет с базовыми репозиториями |
| `weather_*.py` | размазано | консолидировано | OK — `services/weather/` |
| `*_monitor.py` | размазано | консолидировано | OK — `services/monitors/` |

**Параллельная инфраструктура:** создан пакет `scheduler/` (1337 LOC, mixins: `zone_runner`, `program_runner`, `boot_recovery`, ...) — но **не используется в продакшене**. Импортируется только в тестах. Это незавершённый рефакторинг с риском дрейфа кода (см. P0-2).

---

## Новые архитектурные находки

### P0 — критично для коммерческого single-tenant

#### P0-1. Потеря состояния watering при SIGKILL — физический клапан остаётся открытым

**Где:** `irrigation_scheduler.py` (volatile MemoryJobStore), `services/zone_control.py` (`_PENDING_CLOSE_TIMERS`), `services/app_init.py:_boot_sync`

**Суть:** Все таймеры остановки зон и закрытия мастер-клапанов хранятся в **MemoryJobStore** APScheduler (`volatile`) или в module-level `dict[str, threading.Timer]`:
- `zone_stop:{id}:*` — мягкая остановка по длительности
- `zone_hard_stop:{id}` — watchdog-стоп
- `zone_cap_stop:{id}` — абсолютный лимит (240 мин default)
- `master_cap_close:{group}` — cap мастер-клапана (24ч)
- `_PENDING_CLOSE_TIMERS[topic]` — отложенное закрытие master valve через `threading.Timer`

При **SIGKILL** (OOM, kernel panic, выдернутый питон, `kill -9` оператором):
- graceful shutdown не отрабатывает → `shutdown_all_zones_off` не публикует OFF
- volatile jobs пропадают вместе с процессом
- threading.Timer'ы пропадают
- **Физические клапаны остаются открытыми до следующего boot**, который вызовет `_boot_sync` → `stop_all_in_group(force=True)` + retry-publish OFF (3 × 0.2*N сек)

**Окно риска:** время до запуска нового процесса (systemd restart) + до завершения boot_sync. При проблеме брокера или сетевом разрыве — окно расширяется (boot_sync `publish_mqtt_value` ретраит 10 раз connect-rc).

**Почему важно для коммерческого:** залив газона/теплицы при сбое = реальный ущерб клиенту. SLA на физическое закрытие клапана при крэше не определён.

**Рекомендация (без действия):** перенести `zone_cap_stop` и `master_cap_close` в персистентный jobstore (`default`/jobs.db) — они и так replace_existing=True, идемпотентны. Master close через Timer заменить на APScheduler-job в default-jobstore.

---

#### P0-2. Две параллельные реализации scheduler — drift и ложные тесты

**Где:** `irrigation_scheduler.py` (1649 LOC, монолит, used in prod) vs `scheduler/` пакет (1337 LOC, mixins, tests only)

**Суть:** Рефакторинг начат, но не завершён. В `app_init.py` / `app.py` создаётся именно монолитный `IrrigationScheduler`. Mixins из `scheduler/zone_runner.py`, `scheduler/program_runner.py` и т.д. **не подмешиваются** к основному классу. Тесты импортируют `from scheduler.zone_runner import ZoneRunnerMixin` и тестируют **не тот** код, который работает в проде.

**Подтверждение:** `scheduler/zone_runner.py` строки 28, 79-91 — содержит `_stop_zone`, `schedule_zone_stop` идентичные по интерфейсу методам в `irrigation_scheduler.py`, но это разные имплементации (например, в mixin есть аккуратный `early_off_seconds`-парсинг с clamp 0..15, в монолите — другой).

**Риск:** изменения, сделанные в одном месте, не отражаются в другом. Тесты могут зеленеть на mixins, а прод-код иметь регрессии.

**Рекомендация:** либо мигрировать `IrrigationScheduler` на использование mixins (composition), либо удалить `scheduler/` целиком как dead code. Текущее состояние — худший вариант.

---

#### P0-3. `recover_missed_runs` не покрывает interval/even-odd/manual group-seq

**Где:** `irrigation_scheduler.py:recover_missed_runs` + `_run_program_threaded` + `_run_group_sequence`

**Суть:** При рестарте посреди исполнения программы:
- **weekday cron-программа** — есть логика replay оставшихся зон (хотя её корректность под вопросом — replay по индексу зоны без учёта прогресса)
- **interval-программа** (каждые N дней) — не восстанавливается
- **even/odd-day программа** — не восстанавливается
- **manual group sequence** (`_run_group_sequence`, кнопка "Запустить группу" в UI) — **полностью теряется**

Если процесс упал во время manual group-seq, система не знает, что нужно было полить ещё 5 зон. Оператору придётся повторно нажимать кнопку.

**Подтверждение:** `cleanup_jobs_on_boot` слепо удаляет `group_seq:*` jobs (они в volatile, и так их нет после рестарта). Никакой записи "программа в процессе, осталось N зон" в БД нет.

**Рекомендация:** ввести таблицу `program_run_state` с прогрессом + recover на boot. Альтернатива: documented limitation в SLA.

---

#### P0-4. Master valve close через `threading.Timer` — не покрыт жёсткими гарантиями

**Где:** `services/zone_control.py:_PENDING_CLOSE_TIMERS` (module-level dict)

**Суть:** При штатной остановке последней зоны в группе с master valve в режиме `delayed_close` запускается `threading.Timer(delay_seconds, _close_master_valve_pending)`. Таймер хранится в module-level `dict`. При:
- SIGKILL — таймер пропадает (см. P0-1)
- падении модуля при инициализации — таймер не создаётся, зона на ON
- параллельном старте другой зоны в той же группе — отмена таймера через `cancel_pending_master_close()` корректна, но через `dict.pop` — race-условие если две зоны стартуют одновременно

**Защита:** есть `schedule_master_valve_cap` (24ч cap) — но он тоже в volatile (P0-1).

**Рекомендация:** перевести close на APScheduler default jobstore.

---

### P1 — высокий приоритет

#### P1-1. Утечка `services.locks._group_locks` / `_zone_locks`

**Где:** `services/locks.py`

**Суть:** Module-level `dict[int, RLock]` для group и zone locks. Никогда не очищаются. На длительно работающем процессе (uptime месяцы), если админ пересоздаёт зоны/группы (новые ID), словари растут без границ.

Не критично (RLock дешёвый, ID растут медленно), но technical debt и потенциальный RAM leak в worst case.

**Рекомендация:** WeakValueDictionary или явная очистка при удалении зоны/группы.

---

#### P1-2. `StateVerifier` создаёт новый MQTT-клиент на каждый verify

**Где:** `services/observed_state.py:StateVerifier.verify_async`

**Суть:** Для каждой проверки observed state (после publish ON/OFF) создаётся **новый** `mqtt.Client`, подписывается на zone topic, ждёт echo, отключается. При большой группе одновременно стартующих зон или частых stop/start — это десятки эфемерных TCP-коннектов к брокеру в секунду.

**Симптомы:** возможен exhaustion file descriptors / ephemeral ports под нагрузкой; нагрузка на брокер.

**Рекомендация:** общий long-lived subscriber + future-by-topic, переиспользовать клиент через `services.mqtt_pub` пул.

---

#### P1-3. Миграции — нет Alembic, авто-применение при старте, downgrade-логика своя

**Где:** `db/migrations.py` (1084 LOC), `migrations/versions/` (пустая папка), `db/IrrigationDB.__init__` → `init_database`

**Суть:**
- Alembic-структура есть (`migrations/`), но **`versions/` пустая** — Alembic не используется
- Реальные миграции — императивные методы в `db/migrations.py` с собственным `DOWNGRADE_REGISTRY` и `_down_*` функциями
- Применяются автоматически на каждом старте `IrrigationDB.__init__` без version pinning
- Нет offline-инструмента (`alembic upgrade --sql`) для DBA
- Нет gating: если миграция падает в середине — БД в неконсистентном состоянии без транзакционной обёртки на уровне всего набора

**Риски для коммерческого:** при апгрейде версии у клиента нет возможности проверить миграцию офлайн, нет аудит-trail версий схемы (есть `schema_version` в settings, но это самописное), нет совместимости с DBA-инструментами.

**Рекомендация:** мигрировать на Alembic с генерацией версий. Минимум — добавить транзакционную обёртку и `--check-only` режим.

---

#### P1-4. Каждый репозиторий открывает SQLite-коннект на каждый вызов

**Где:** `db/base.py:BaseRepository._connect`

**Суть:** Нет pooling. На каждый CRUD-вызов — `sqlite3.connect()`, PRAGMA setup (WAL, foreign_keys, busy_timeout, synchronous), запрос, close. Под нагрузкой (SSE-стрим, MQTT echo, scheduler tick) это десятки connect/close в секунду.

WAL + busy_timeout=30s сглаживают write-contention, но connection thrashing — реальный overhead. Под спайком (старт большой группы из 16 зон + параллельный SSE-клиент + MQTT echo) можно увидеть `database is locked` несмотря на retry.

**Рекомендация:** thread-local persistent connection или connection pool (`sqlite3.connect` cheap, но PRAGMA setup на каждом — нет).

---

#### P1-5. Backward-compat stubs живут вечно

**Где:** `weather_adjustment.py` (8 LOC), `weather_merged.py` (92 LOC)

**Суть:** После рефакторинга в `services/weather/` старые модули превращены в re-export stubs. Никакого deadline на их удаление, никаких deprecation warnings. Технический долг, который никогда не возьмут.

**Рекомендация:** добавить `DeprecationWarning` + tracker issue с датой удаления, либо удалить сразу (импорты по graphify грепаются).

---

#### P1-6. `dlog` / `_is_debug_logging_enabled` — DB-hit на каждый log call

**Где:** `utils.py` или `config.py` (точное место — функция `dlog`)

**Суть:** Helper для debug-логирования читает флаг из таблицы `settings` через репозиторий **на каждый вызов**. В горячих путях (MQTT echo, SSE, scheduler tick) это сотни-тысячи лишних SELECT в минуту.

**Рекомендация:** TTL-cache 5-10 секунд или event-driven invalidation при изменении настройки.

---

### P2 — средний приоритет / technical debt

#### P2-1. `services/sse_hub.py` — module-level state, сложно тестировать
Singleton на module-level, инжект через `init()` при старте app. Тесты вынуждены патчить module attributes.

#### P2-2. `services/float_monitor.py` — fallback на raw `sqlite3` при импорт-фейле
В коде есть `try: from db.repos import FloatRepository ... except: import sqlite3 ...`. PHYS-3 risk явно отмечен в комментарии. Dead path при правильной установке, но мина при сломанном пакете.

#### P2-3. `app.py:_start_single_zone_watchdog` — polling 1s
Daemon thread каждую секунду ходит в БД (`get_zones_by_state`) для проверки exclusivity. Нагрузка на SQLite постоянная. Лучше event-driven через SSE-bus или MQTT.

#### P2-4. Перекрывающиеся auth-хуки
`_auth_before_request` и `_require_admin_for_mutations` в `app.py` имеют пересекающуюся логику с тонкими отличиями. Риск: правка одной не синхронизируется с другой.

#### P2-5. `TESTING` ветки в production-коде
Например, `_run_group_sequence` имеет `if TESTING: ...` short-circuit. Прод-бинарник несёт мёртвые ветки. Лучше — DI / monkeypatch на уровне тестов.

#### P2-6. Backup discipline — `jobs.db` рядом с `irrigation.db`
APScheduler хранит persistent jobs в `jobs.db`. Если backup-скрипт копирует только `irrigation.db`, после restore APScheduler стартует с пустым jobstore — все зашедуленные программы пропадут до следующего планирования (есть `recover_missed_runs`, но он покрывает не всё, см. P0-3).

#### P2-7. `csrf.exempt` — manual список из 14+ роутов
Каждый guest endpoint отдельно exempt'ится. Хрупко: новый guest endpoint забыли — CSRF блокирует; старый удалили — exempt висит. Лучше — declarative через decorator.

#### P2-8. `routes/settings.py:14,17` — duplicate `logger = logging.getLogger(__name__)`
Два подряд redundant assignments после прошлого fix. Косметика, не баг.

#### P2-9. `_run_program_threaded` ≈ 70% дубликат `_run_group_sequence`
Скан тиков 1s, проверка cancel_event + _shutdown_event, последовательный запуск зон. Должно быть одной функцией с разными источниками "что лить".

#### P2-10. Hypercorn ASGI + WSGIMiddleware fallback (`run.py`)
`run.py` пытается Hypercorn, при ImportError — Flask dev server. В коммерческом install Flask dev server в проде = anti-pattern. Лучше hard-fail если Hypercorn не установлен.

---

## Сводка

**Прошлые баги (BUGS-REPORT):**
- 4/4 fixed (settings sqlite3 import, _is_status_action dedup, weather routes refactor, weather files split)
- Регрессия: `irrigation_scheduler.py` вырос с 1365 до 1649 LOC (god-file усугубился)
- Незавершённый рефакторинг: `scheduler/` пакет создан, но **не используется в проде** — drift risk

**Новые находки:**
- **P0:** 4 (volatile/timer-state loss при SIGKILL → physical valve open; параллельные scheduler-реализации; recover_missed_runs не покрывает interval/even-odd/manual group-seq; master close через `threading.Timer`)
- **P1:** 6 (unbounded lock dicts; ephemeral MQTT clients в StateVerifier; нет Alembic + auto-apply миграций; нет connection pool; вечные backward-compat stubs; dlog DB hit per call)
- **P2:** 10 (sse_hub module state; float_monitor sqlite3 fallback; 1s exclusivity poll; duplicate auth hooks; TESTING ветки в проде; jobs.db backup discipline; ручной csrf.exempt; duplicate logger; program/group dupe code; Flask dev fallback в проде)

**Топ-5 одной строкой:**
1. **P0-1** SIGKILL во время watering → master/zone valves физически открыты до boot_sync (volatile jobstore + threading.Timer не персистентны)
2. **P0-2** Параллельный `scheduler/` пакет (1337 LOC) тестируется, но не запускается в проде — drift между тестами и прод-кодом
3. **P0-3** `recover_missed_runs` восстанавливает только weekday-cron; interval/even-odd/manual-group-seq программы теряются на рестарте
4. **P1-3** Миграции — самописные императивные с DOWNGRADE_REGISTRY, без Alembic, авто-применяются на старте без gating (риск для коммерческого upgrade-flow)
5. **P1-2** `StateVerifier` создаёт новый MQTT-клиент на каждую verify — ephemeral connection churn под нагрузкой большой группы

**Общая оценка для коммерческого single-tenant:**
Система работает, защитные слои есть (boot_sync force-OFF, watchdog cap 240 мин, retry publish), но **graceful path** покрыт лучше **crash path**. Основной риск — SIGKILL/OOM/kernel-panic во время полива оставляет физические клапаны в неопределённом состоянии до следующего boot. Для prod-инсталляций критично:
1. Перевести stop/cap/master-close jobs в персистентный jobstore
2. Закрыть рефакторинг `scheduler/` пакета (либо использовать, либо удалить)
3. Добавить персистентное `program_run_state` для recovery всех типов программ
4. Мигрировать миграции на Alembic с offline-режимом для DBA
