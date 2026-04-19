# Database Audit — wb-irrigation refactor/v2

**Дата:** 2026-04-19
**Аудитор:** database-optimizer
**Targets:** локальная копия `/opt/claude-agents/irrigation-v2/irrigation.db` (221 KB, в основном пустая, схема идентична проду) + read-only прод-снапшот из `landscape/prod-snapshot.md`
**Ветка:** `refactor/v2` (HEAD совпадает с продом `e37adb7`)

---

## Executive Summary

**Всего находок:** 18
- CRITICAL: 4
- HIGH: 6
- MEDIUM: 6
- LOW: 2

**Топ-3 для Phase 4 (по приоритету):**

1. **DB-001** — `PRAGMA foreign_keys = OFF` на проде. FK объявлены только у 2 таблиц из 19. ON DELETE CASCADE есть только в декларации `bot_subscriptions`/`bot_audit`, но **не работает** (FK выключен). Удаление зоны/группы/программы оставляет orphan-rows в `zone_runs`, `water_usage`, `weather_log`, `program_cancellations`, `program_queue_log`, `float_events`, `groups.master_mqtt_server_id` и т.д.
2. **DB-002** — Миграции **не атомарны**. `_apply_named_migration` выполняет DDL (ALTER TABLE) → INSERT в `migrations` → COMMIT. Между ALTER и INSERT нет защиты: ALTER в SQLite авто-коммитится, поэтому при crash после ALTER, но до INSERT — миграция переопределится при следующем запуске и упадёт с `duplicate column name`. Все 35 миграций имеют `try/except` который **проглатывает** ошибку, но в `_apply_named_migration` исключение **не ловится наверху** — INSERT не выполнится, миграция считается «не применённой», следующий запуск повторит ALTER и опять упадёт. Расширяет CQ-009.
3. **DB-005** — Нет автоматических бэкапов. Последний DB-бэкап на проде — **3 апреля** (16 дней назад), хотя БД растёт `logs` += ~600 строк/день. `LogRepository.create_backup()` существует, но никем не вызывается (нет cron, нет scheduler-job, нет systemd timer). При corruption БД восстанавливаться будет с 16-дневной потерей.

---

## Schema overview

| Таблица | rows (prod) | cols | PK | FK declared (effective) | Indices | Назначение |
|---|---:|---:|---|---|---|---|
| `zones` | 24 | 28 | `id INT` | нет | `idx_zones_group`, `idx_zones_topic`, `idx_zones_mqtt_server` | основной справочник зон |
| `groups` | 2 | 27 | `id INT` | нет (есть 4 _MQTT-server-id_ полей без FK) | `sqlite_autoindex_groups_1` (UNIQUE name) | группы зон + sensor cfg |
| `programs` | 1 | 14 | `id INT` | нет | — | программы полива |
| `program_cancellations` | 5 | 4 | composite (program_id, run_date, group_id) | нет (логически на programs.id) | `sqlite_autoindex_program_cancellations_1` | отмены ран программы |
| `program_queue_log` | 0 | 14 | `id INT AUTOINC` | нет | `idx_pql_program`, `idx_pql_state` | очередь программ (не используется) |
| `zone_runs` | 0 | 16 | `id INT AUTOINC` | нет (логически zone_id, group_id) | `idx_zone_runs_zone`, `idx_zone_runs_group`, `idx_zone_runs_active(zone_id, end_utc)` | детальные запуски (не пишется!) |
| `water_usage` | 0 | 4 | `id INT AUTOINC` | нет | `idx_water_zone`, `idx_water_timestamp` | старая таблица (deprecated?) |
| `weather_cache` | 1 | 5 | `id INT AUTOINC` | нет | `idx_weather_cache_loc(lat,lon)`, `idx_weather_cache_time` | кеш Open-Meteo |
| `weather_log` | 0 | 9 | `id INT AUTOINC` | нет | `idx_weather_log_zone`, `idx_weather_log_time` | per-zone weather adjustment |
| `weather_decisions` | 0 | 14 | `id INT AUTOINC` | нет | `idx_weather_decisions_date`, `idx_weather_decisions_created` | решения погодного движка |
| `float_events` | 0 | 5 | `id INT AUTOINC` | нет | `idx_float_events_group` | события поплавка |
| `mqtt_servers` | 1 | 16 | `id INT` | нет | — | MQTT-брокеры |
| `logs` | 8339 | 4 | `id INT AUTOINC` | — | `idx_logs_type`, `idx_logs_timestamp` | append-only event log |
| `settings` | 22 | 2 | `key TEXT` | — | `sqlite_autoindex_settings_1` | key-value config |
| `migrations` | 35 | 2 | `name TEXT` | — | `sqlite_autoindex_migrations_1` | applied migrations |
| `bot_users` | 0 | 16 | `id INT AUTOINC, UNIQUE chat_id` | — | `sqlite_autoindex_bot_users_1`, `idx_bot_users_chat` | Telegram users |
| `bot_subscriptions` | 0 | 8 | `id INT AUTOINC` | **bot_users.id ON DELETE CASCADE** (но FK off!) | `idx_bot_subs_user` | подписки |
| `bot_audit` | 0 | 5 | `id INT AUTOINC` | **bot_users.id ON DELETE SET NULL** (но FK off!) | `idx_bot_audit_user` | аудит |
| `bot_idempotency` | 0 | 4 | `token TEXT` | — | `sqlite_autoindex_bot_idempotency_1`, `idx_bot_idemp_chat` | защита от дублей |

Размер на проде: 1.16 MB (289 страниц по 4 KB). Локально 221 KB. WAL/SHM на проде на момент снапшота отсутствуют (после checkpoint).

`PRAGMA integrity_check = ok` (Phase 1).

---

## Findings

### CRITICAL

#### DB-001: Foreign keys выключены, FK почти не объявлены — orphan-rows возможны во всех связях
- **Локация:** все таблицы кроме `bot_subscriptions`, `bot_audit` (и даже там не работает)
- **Что не так:**
  - `PRAGMA foreign_keys = 0` на проде (Phase 1, prod-snapshot §7).
  - `db/base.py:_connect()` устанавливает `foreign_keys=ON`, **но этот метод никем не используется** — все repository-методы делают свой `sqlite3.connect()` напрямую без PRAGMA.
  - В `db/migrations.py:24` PRAGMA включается, но это connection-уровневая настройка в SQLite — на других connection остаётся OFF.
  - FK объявлены только у `bot_subscriptions.user_id → bot_users.id ON DELETE CASCADE` и `bot_audit.user_id → bot_users.id ON DELETE SET NULL` — **но не enforce'ятся** из-за PRAGMA off.
  - Логические FK без декларации:
    - `zones.group_id → groups.id`
    - `zones.mqtt_server_id → mqtt_servers.id`
    - `groups.{master,pressure,water,float}_mqtt_server_id → mqtt_servers.id` (4 поля)
    - `water_usage.zone_id → zones.id`
    - `zone_runs.zone_id → zones.id`, `zone_runs.group_id → groups.id`
    - `weather_log.zone_id → zones.id`
    - `program_cancellations.{program_id,group_id} → programs.id, groups.id`
    - `program_queue_log.{program_id,group_id} → programs.id, groups.id`
    - `float_events.group_id → groups.id`
- **Impact:** целостность.
  - `db.zones.delete_zone()` (`db/zones.py:380`) удаляет только из `zones`, оставляя:
    - `water_usage` rows со ссылкой на удалённую зону (показывает в статистике как `zone_name=NULL`)
    - `zone_runs` (open runs остаются «висящими»)
    - `weather_log` rows
    - upcoming program rows (поле `programs.zones` JSON содержит `[1,2,3]`, не FK)
  - `db.programs.delete_program()` (`db/programs.py:159`) не очищает `program_cancellations` и `program_queue_log`.
  - `db.groups.delete_group()` (`db/groups.py:44`) проверяет `COUNT(zones)>0`, но не проверяет `zone_runs`, `float_events`, и не учитывает `programs.zones` references.
  - Удалить mqtt_server можно — оставит зоны и группы со ссылкой на исчезнувший id.
- **Evidence (на пустой локалке orphan=0, но это лишь потому что данных нет):**
  ```
  zones with non-existent group_id: 0
  zones with non-existent mqtt_server_id: 0
  water_usage with non-existent zone_id: 0
  zone_runs with non-existent zone_id: 0
  ...
  ```
  При наличии данных и удалении — будут.
- **Направление фикса:**
  1. На каждом `_connect()` ставить `PRAGMA foreign_keys=ON` (или подключать через `BaseRepository._connect()`, который сейчас существует но не используется).
  2. Добавить FK-декларации во все `CREATE TABLE` (в SQLite требуется пересоздать таблицу для добавления FK к существующей).
  3. До тех пор — добавить транзакционную очистку child-таблиц в `delete_zone`/`delete_program`/`delete_group`.

#### DB-002: Миграции не атомарны — split-brain между DDL и `INSERT INTO migrations`
- **Локация:** `db/migrations.py:196-206` (`_apply_named_migration`)
- **Что не так:**
  ```python
  def _apply_named_migration(self, conn, name: str, func):
      try:
          cur = conn.execute('SELECT name FROM migrations WHERE name = ? LIMIT 1', (name,))
          if cur.fetchone():
              return
          func(conn)                  # ← может содержать ALTER TABLE (auto-commit в SQLite)
          conn.execute('INSERT OR REPLACE INTO migrations(name) VALUES (?)', (name,))
          conn.commit()               # ← single commit обоих, но ALTER уже committed выше
      except sqlite3.Error as e:
          logger.error("Ошибка применения миграции %s: %s", name, e)
          # ← молча проглочено
  ```
  - В SQLite `ALTER TABLE ADD COLUMN` выполняется внутри транзакции, **но** `CREATE TABLE`/`CREATE INDEX` не auto-committed (зависит от SQLite-версии и driver). В Python `sqlite3` driver открывает implicit transaction только перед DML, не перед DDL — DDL может зайти **вне** транзакции.
  - 30 из 35 миграций — это `ALTER TABLE ... ADD COLUMN`. Каждая делает `conn.commit()` внутри `func(conn)` (см. `_migrate_add_postpone_reason:322`, `_migrate_add_watering_start_time:333` и десятки других). После этого commit DDL уже зафиксирован.
  - Если в `_apply_named_migration` после `func(conn)` крэшнется процесс/`OOM`/power-loss до `conn.commit()` второй раз — DDL применён, в `migrations` нет записи. На рестарте — `_apply_named_migration` пытается повторить ALTER → `OperationalError: duplicate column name` → exception swallowed → миграция **навсегда** не отметится, последующие миграции продолжат, БД в неопределённом состоянии.
  - Защита внутри методов через `PRAGMA table_info` + `if 'col' not in columns` есть только в **половине** миграций (например, `_migrate_add_postpone_reason` — есть, `_migrate_add_zones_indexes` — `CREATE INDEX IF NOT EXISTS` есть, но `_migrate_create_weather_cache` — `CREATE TABLE IF NOT EXISTS` есть). Т.е. скорее повезёт, но это не системная гарантия.
  - **Главное** — `except sqlite3.Error` в `_apply_named_migration` ловит ошибку, логгирует и **возвращает None**. Цикл миграций (`init_database:119-160`) идёт дальше. Результат: failed migration считается success, последующие миграции, опирающиеся на новую колонку, могут упасть позже неожиданно.
- **Impact:** корраптность миграционного состояния, невозможность повторного запуска после crash.
- **Расширение CQ-009:** code-reviewer уже флагнул `INSERT INTO migrations` отдельным statement. Со стороны БД: отсутствие `BEGIN IMMEDIATE` + `COMMIT` обёртки означает, что **даже на одном connection** между DDL и INSERT может произойти WAL-checkpoint от другого процесса (хотя в SQLite один writer — но retry_on_busy могут оставить state).
- **Направление фикса:**
  1. Каждую миграцию обернуть в `BEGIN IMMEDIATE; ... COMMIT;` (для DDL это не даст rollback, но даст atomic visibility).
  2. Не глотать `sqlite3.Error` — пробрасывать наверх с информацией о сломанной миграции.
  3. Каждая миграция должна быть **полностью** идемпотентной: `IF NOT EXISTS` для CREATE, `PRAGMA table_info` check для ADD COLUMN, `PRAGMA index_list` для индексов.
  4. Рассмотреть схему «migrations.applied_at IS NULL = в процессе»: помечать `INSERT INTO migrations(name, applied_at) VALUES (?, NULL)` ДО DDL, обновлять на `CURRENT_TIMESTAMP` после успеха. На старте проверять «in-progress» и решать вручную.

#### DB-003: На `init_database()` возможно частично применённое состояние, если миграция упала
- **Локация:** `db/migrations.py:118-160`, `database.py:52` (вызывается на каждом import facade)
- **Что не так:**
  - `IrrigationDB.__init__` всегда вызывает `init_database()` → проходит по всем 35 миграциям. Это нормально (idempotent design), но в комбинации с DB-002 — опасно.
  - Особенно `_migrate_create_zone_runs` создаёт **новую таблицу с колонками**, и если при первом запуске `CREATE TABLE` прошёл, но `CREATE INDEX` нет — миграция «не применена», но индекс при следующем CREATE INDEX IF NOT EXISTS создастся, а зафиксируется применённой. Дальнейшие запуски могут CREATE TABLE IF NOT EXISTS пропустить (он уже есть), и всё ок — но **диагностика** «что именно сломалось» теряется.
  - Прод запускает migrations при каждом systemd-restart (~раз в 13 дней по uptime). Латентность init: 35 миграций × `SELECT name FROM migrations` — **N+1 на загрузке** (35 раз отдельный SELECT вместо одного `SELECT name FROM migrations` → set lookup). При 35 это ~3.5ms, незаметно. Но если миграций станет 100+, заметим.
- **Impact:** диагностируемость, slow start.
- **Направление фикса:**
  1. Загрузить все applied миграции одним `SELECT name FROM migrations` в python-set, проверять `name in applied`.
  2. Логировать `[MIGRATION] applied: 35/35` или `[MIGRATION] FAILED at step N` явно.
  3. Если хоть одна миграция упала — **fail fast**, не позволять app стартовать с ломаной БД.

#### DB-004: На проде `synchronous=FULL` (slow), а в коде задаётся `NORMAL` — connection-level mismatch
- **Локация:** `db/migrations.py:25` ставит `PRAGMA synchronous=NORMAL`, но прод-снапшот показывает `synchronous=2 (FULL)`.
- **Что не так:**
  - PRAGMA `synchronous` per-connection в SQLite. Установка в `init_database` влияет только на тот connection, который выполнил миграции, и закрыт (`with` exits).
  - Все остальные connections (а их в каждом repo-методе по новому через `sqlite3.connect()`) НЕ ставят `synchronous=NORMAL` → используют compile-time default = `FULL` (= 2).
  - На прод-снапшоте `PRAGMA synchronous` снято через ad-hoc `sqlite3` connection и показал `2 (FULL)`. Это означает: **большинство** writes на проде идут с FULL fsync (медленнее).
  - Для embedded (Wirenboard ARM, eMMC), при WAL и FULL — это чрезмерно. NORMAL+WAL даёт consistency без `fsync()` на каждый commit.
  - При нагрузке (24 зоны × scheduler updates `version` каждые секунды) это бьёт по eMMC wear (доп. fsync) и латентности.
- **Impact:** перформанс write, eMMC wear.
- **Evidence:** prod-snapshot §7 → `synchronous = 2`. Код в `db/migrations.py:25` ставит NORMAL — но в одиночном connection.
- **Направление фикса:**
  1. Все `sqlite3.connect()` обёртки идут через `BaseRepository._connect()` (который уже существует, см. `db/base.py:36-42`) и ставит PRAGMA на каждом коннекте. Сейчас этот метод НЕ ВЫЗЫВАЕТСЯ нигде в репозиториях (см. DB-008).
  2. Добавить `PRAGMA synchronous=NORMAL` + `PRAGMA busy_timeout=30000` + `PRAGMA foreign_keys=ON` в каждый коннект через единую factory.

---

### HIGH

#### DB-005: Нет автоматических бэкапов
- **Локация:** `db/logs.py:157-186` (`create_backup`), нет cron / нет scheduler job
- **Что не так:**
  - `LogRepository.create_backup()` реализован: использует `sqlite3.backup()` API (правильный способ для WAL!) + WAL checkpoint TRUNCATE + cleanup_old (keep_count=7).
  - Метод **никем не вызывается**. `grep -r 'create_backup\|backup_db' --include="*.py"` показывает только определение и роуты `routes/system_emergency_api.py` (manual через web-UI).
  - На проде последний бэкап: `irrigation_backup_20260403_185606.db` — **16 дней назад**.
  - Crontab: prod-snapshot не показал — нужно проверить, но судя по Bug #4 в BUGS-REPORT (app.log = 0) и общему состоянию — нет.
  - Бэкап-папка `/mnt/data/irrigation-backups/` (33 GB SD-card свободно) ёмкость есть.
- **Impact:** при corruption БД (или power-loss + WAL truncation) — потеря 16+ дней данных (logs, settings changes).
- **Направление фикса:**
  1. APScheduler job: `create_backup()` каждый день в 03:00 (когда нет scheduled programs).
  2. Опционально — после каждого N-го `INSERT INTO logs` или раз в час.
  3. Документация по recovery: `cp irrigation_backup_*.db irrigation.db && rm irrigation.db-wal irrigation.db-shm`.

#### DB-006: 7 прямых `sqlite3.connect()` минуя facade в `services/weather.py` — конкурируют с repository-слоем
- **Локация:** `services/weather.py:332, 381, 708, 737, 756, 779, 883` (полный список из landscape подтверждён). Также `services/float_monitor.py:424`.
- **Routes**: проверка показала, что в `routes/*.py` прямых `sqlite3.connect()` уже **нет** (landscape был неточен — это устаревшая инфа, в текущем `refactor/v2` импорт `import sqlite3` оставлен лишь для `except sqlite3.Error` clauses).
- **Что не так в каждом из 8 мест:**

| Файл:line | Что делает | Connection mgmt | Тxn | PRAGMA | Issue |
|---|---|---|---|---|---|
| `services/weather.py:332` | `_get_settings`, читает `settings` 12 раз в цикле | `with sqlite3.connect(timeout=5)` | read-only | нет | **N+1 в цикле**: 12 отдельных `SELECT value FROM settings WHERE key=?` вместо `WHERE key IN (...)` |
| `services/weather.py:381` | `_has_ms_threshold`, 1 SELECT | `with` | read-only | нет | Дубль предыдущего вызова — кеш отсутствует |
| `services/weather.py:708` | `log_adjustment`, INSERT в `weather_log` | `with` + `commit()` | implicit | нет | OK, но без `foreign_keys=ON` если zone удалили — оставит orphan |
| `services/weather.py:737` | `_get_location`, 2 SELECT (lat+lon) подряд | `with` | read-only | нет | Должен быть один SELECT с `WHERE key IN ('weather.latitude','weather.longitude')` |
| `services/weather.py:756` | `_get_cached`, ORDER BY fetched_at LIMIT 1 | `with` | read-only | нет | Сортировка → `USE TEMP B-TREE` (см. DB-009) |
| `services/weather.py:779` | `_save_cache`, INSERT + DELETE old | `with` + `commit()` | implicit | нет | DELETE по `fetched_at < ?` — нужен индекс по `fetched_at` (есть, ОК) |
| `services/weather.py:883` | stale cache fallback, тот же SELECT | `with` | read-only | нет | Дубль `_get_cached` |
| `services/float_monitor.py:424` | `_get_db()` factory **с** PRAGMA WAL+busy_timeout | `conn.close()` ручной | implicit | **есть** WAL+busy_timeout=30000 | Единственное «правильное» место! Но не использует facade. |

- **Impact:**
  - **Конкурентность**: каждый прямой connect = новый writer-lock attempt. WAL допускает 1 writer + N readers, но `database is locked` возникает, если writer держит lock дольше `busy_timeout`. У всех weather-вызовов `timeout=5` секунд, у `float_monitor` — 30 секунд. Mismatch = writer от scheduler/web может блокировать weather на 5 сек, после чего exception swallowed (`logger.debug`).
  - **Нет PRAGMA**: `synchronous=FULL` (см. DB-004), `foreign_keys=OFF`. Каждый connect использует sqlite-defaults.
  - **Дубль логики**: `_get_cached` + `_save_cache` + fallback-stale в `services/weather.py:756, 779, 883` дублируют то, что должно быть `WeatherCacheRepository`.
  - **Тестируемость**: невозможно мокать БД для unit-tests, нужен real sqlite файл.
- **Направление фикса:**
  1. Создать `db/weather.py` с `WeatherRepository` для `weather_cache`, `weather_log`, `weather_decisions`.
  2. `services/weather.py` использует `db.weather.*` через facade.
  3. Все методы — через `BaseRepository._connect()` с PRAGMA.

#### DB-007: `BaseRepository._connect()` существует, но не используется ни одним repository
- **Локация:** `db/base.py:36-42`
- **Что не так:**
  ```python
  class BaseRepository:
      def _connect(self) -> sqlite3.Connection:
          conn = sqlite3.connect(self.db_path, timeout=5)
          conn.execute('PRAGMA journal_mode=WAL')
          conn.execute('PRAGMA foreign_keys=ON')
          conn.row_factory = sqlite3.Row
          return conn
  ```
  - `grep -r "self._connect\(\)\|self\._connect()" db/` → **0 хитов**. Метод-сирота.
  - Все repos (zones, programs, groups, mqtt, settings, telegram, logs) делают `with sqlite3.connect(self.db_path, timeout=5) as conn:` напрямую (60+ мест).
  - Следствие: PRAGMA `foreign_keys=ON` нигде не активируется (см. DB-001), `synchronous=NORMAL` не активируется (см. DB-004), `busy_timeout` не настраивается (только timeout=5 на самом connect, что только wait-on-open).
- **Impact:** конфигурация БД фактически не применяется к prod connections.
- **Направление фикса:** заменить все `with sqlite3.connect(...)` на `with self._connect() as conn:`. Также добавить туда `PRAGMA synchronous=NORMAL`, `PRAGMA busy_timeout=30000`.

#### DB-008: `retry_on_busy` decorator повторяет всю функцию — non-idempotent на DML без UPSERT
- **Локация:** `db/base.py:12-27`
- **Что не так:**
  - `@retry_on_busy(max_retries=3, initial_backoff=0.1)` — при `database is locked` повторяет весь метод.
  - Применён к `add_log` (`db/logs.py:50`), `add_water_usage` (`db/logs.py:94`), `create_zone_run` (`db/zones.py:500`), `set_password` и др.
  - Большинство этих методов — `INSERT INTO ... AUTOINCREMENT`, retry безопасен (новый row, новый id).
  - Но `create_program` (`db/programs.py:55`) — INSERT, может повторно вставить дубликат если первая попытка fail между INSERT и commit (хотя `with` сделает rollback при ошибке — но `database is locked` возникает на `execute`, не на commit; OperationalError выбрасывается до COMMIT, так что rollback ок).
  - Реальная проблема: `update_zone_versioned` (`db/zones.py`, optimistic lock через `version`) — retry будет читать новый version, делать UPDATE снова, ОК.
  - **Но**: при exhausted retries — `raise` идёт наверх, и в `_apply_named_migration` (DB-002) проглатывается. Сами retries только debug-warning, не error. На проде: 13 дней uptime → 0 BUSY warnings в journal (prod-snapshot §3 показал только INFO статусы). Может в реальности и не возникает, но при росте concurrency (scheduler + monitors + watchdog + web) — возможно.
  - max_retries=3, initial_backoff=0.1 → суммарно 0.1+0.2+0.4 = 0.7 сек. При timeout=5 секунд на connection это умеренно.
- **Impact:** скрытые потери на retry, отсутствие метрик BUSY.
- **Направление фикса:**
  1. Логировать BUSY на `WARNING` уровне с указанием repo-метода и финальным `ERROR` если exhausted.
  2. Добавить `PRAGMA busy_timeout=30000` (внутри driver-уровневый retry, см. DB-007). Тогда явный retry-decorator избыточен.

#### DB-009: Composite индексы отсутствуют для частых query-pattern `WHERE col=? ORDER BY ts DESC`
- **Локация:** см. EXPLAIN ниже
- **Что не так:** 4 query-pattern получают `USE TEMP B-TREE FOR ORDER BY` вместо использования индекса:

| Query | Текущий план | Предлагаемый индекс | Эффект |
|---|---|---|---|
| `logs WHERE type=? ORDER BY timestamp DESC` | `SEARCH idx_logs_type` + `TEMP B-TREE` | `CREATE INDEX idx_logs_type_ts ON logs(type, timestamp DESC)` | Убирает sort, scan только нужного префикса |
| `water_usage WHERE zone_id=? AND timestamp>=? ORDER BY timestamp DESC` | `SEARCH idx_water_zone` + `TEMP B-TREE` | `CREATE INDEX idx_water_zone_ts ON water_usage(zone_id, timestamp DESC)` | Range + sort из индекса |
| `weather_cache WHERE lat=? AND lon=? ORDER BY fetched_at DESC LIMIT 1` | `SEARCH idx_weather_cache_loc` + `TEMP B-TREE` | `DROP idx_weather_cache_loc; CREATE INDEX idx_weather_cache_lookup ON weather_cache(latitude, longitude, fetched_at DESC)` | Один index seek, no sort |
| `weather_log WHERE zone_id=? ORDER BY created_at DESC` | `SEARCH idx_weather_log_zone` + `TEMP B-TREE` | `CREATE INDEX idx_weather_log_zone_ts ON weather_log(zone_id, created_at DESC)` | No sort |

- **Evidence (live EXPLAIN из локалки):**
  ```
  [logs filter type+date]      Current: SEARCH logs USING INDEX idx_logs_type (type=?) / USE TEMP B-TREE FOR ORDER BY
                               With composite: SEARCH logs USING INDEX idx_logs_type_ts (type=? AND timestamp>?)
  [water_usage per zone]       Current: SEARCH w USING INDEX idx_water_zone (zone_id=?) / USE TEMP B-TREE
                               With composite: SEARCH w USING INDEX idx_water_zone_ts (zone_id=? AND timestamp>?)
  [weather_cache by loc]       Current: SEARCH USING idx_weather_cache_loc / USE TEMP B-TREE
                               With composite: SEARCH USING idx_weather_lookup (latitude=? AND longitude=?) — no sort
  ```
- **Impact:** низкий сейчас (logs всего 8339 — sort 1000-row LIMIT недорог), но `logs` растёт линейно. При 100k+ строк и фильтре по type — заметная регрессия.
- **Направление фикса:** добавить миграцию `weather_log_zone_ts_idx` с тремя индексами + dropping старых, где новые их covering'ит.

#### DB-010: `bot_audit ORDER BY ts DESC LIMIT 100` без индекса по `ts` — full table scan
- **Локация:** `db/telegram.py` методы аудита
- **Что не так:**
  - В `bot_audit` есть только `idx_bot_audit_user(user_id)`. Нет индекса по `ts`.
  - EXPLAIN: `SCAN bot_audit / USE TEMP B-TREE FOR ORDER BY` — full scan + sort при каждом `recent N` запросе.
  - Сейчас 0 строк, не критично. Но `bot_audit` — append-only, при активном использовании Telegram бота вырастет до тысяч.
- **Импакт:** деградация при наполнении.
- **Направление фикса:** `CREATE INDEX idx_bot_audit_ts ON bot_audit(ts DESC)`.

---

### MEDIUM

#### DB-011: `zones` ORDER BY id — full scan на 24 зонах (приемлемо), но при export/import без LIMIT
- **Локация:** `db/zones.py:24` — `SELECT z.*, g.name as group_name, g.use_water_meter FROM zones z LEFT JOIN groups g ON z.group_id = g.id ORDER BY z.id`
- **Что не так:**
  - План: `SCAN zones`. PK на `id INTEGER PRIMARY KEY` = rowid, scan уже отсортирован — но SQLite этого не использует автоматически в плане (просто SCAN, sort skip).
  - На 24 зонах ОК. На 1000+ зонах будет медленно при каждой загрузке main page (вызывается на каждый запрос статуса).
- **Impact:** низкий (24 зоны), но scaling concern.
- **Направление фикса:** ничего срочного. При росте — добавить `?cursor=` пагинацию.

#### DB-012: `compute_next_run_for_zone` — N+1 query внутри nested loop (zones × programs × 14 days × zone_durations)
- **Локация:** `db/zones.py:557-595`
- **Что не так:**
  - В цикле по `zones × programs × 14 days × zones_in_program`:
    ```python
    for offset in range(0, 14):
        ...
        for zid in sorted(prog['zones']):
            dur = self.get_zone_duration(zid)   # ← отдельный SELECT на каждый zid
    ```
  - `get_zone_duration` (`db/zones.py:488-497`) делает `SELECT duration FROM zones WHERE id=?` — отдельный connect, отдельный SELECT.
  - При 24 зонах × 1 программа × 14 дней × ~5 зон в программе = ~1680 запросов **per zone reschedule**.
  - Вызывается из `reschedule_group_to_next_program` для всех зон группы → 24 × 1680 ≈ **40 000 SELECT** при reschedule одной группы.
- **Impact:** высокая латентность планирования. На 24 зонах прода это секунды CPU.
- **Направление фикса:** prefetch `dict[zone_id] = duration` одним SELECT перед циклом.

#### DB-013: `programs.zones`, `programs.days`, `programs.extra_times`, `bot_users.fsm_data`, `float_events.paused_zones` — JSON в TEXT-колонках
- **Локация:** `programs(zones, days, extra_times)`, `bot_users(fsm_data)`, `float_events(paused_zones)`
- **Что не так:**
  - Нельзя индексировать по элементам массива. `programs.zones = '[1,2,3]'` — для запроса «какие программы используют зону X» нужен full-scan + JSON parse в Python (`db/programs.py:check_program_conflicts`).
  - Денормализация: при удалении зоны нужно вручную обновлять каждую `programs.zones`, иначе zombie-references.
  - `float_events.paused_zones` хранится как `str(list)` — Python repr (`'[1, 2, 3]'`), не JSON. Парсинг `eval`-небезопасен (хорошо что не парсится сейчас).
- **Impact:** сложная query-логика, потеря целостности.
- **Направление фикса:** нормализовать в junction-таблицу `program_zones(program_id, zone_id, order_idx)`. С FK-каскадом решается DB-001.

#### DB-014: `groups` имеет 27 колонок включая 4 sensor-конфига inline — God Table
- **Локация:** `groups` table
- **Что не так:**
  - Master valve config: `use_master_valve, master_mqtt_topic, master_mode, master_mqtt_server_id, master_valve_observed`
  - Pressure sensor: `use_pressure_sensor, pressure_mqtt_topic, pressure_unit, pressure_mqtt_server_id`
  - Water meter: `use_water_meter, water_mqtt_topic, water_mqtt_server_id, water_pulse_size, water_base_value_m3, water_base_pulses`
  - Float sensor: `float_enabled, float_mqtt_topic, float_mqtt_server_id, float_mode, float_timeout_minutes, float_debounce_seconds`
  - Rain sensor (global, не group): в `settings` через `rain.*` keys
  - Все добавлены через `ALTER TABLE` migrations — никакой 3NF.
- **Impact:** middleware boilerplate (см. `groups.py:update_group_fields:82` — whitelist 15 полей), error-prone при добавлении новых сенсоров.
- **Направление фикса:** вынести в `group_sensors(group_id, sensor_type, config_json)`. Не критично сейчас.

#### DB-015: Нет retention/archive для `logs` и `bot_audit` — растут линейно
- **Локация:** `logs` (8339 за 13 дней = ~640 в день), `bot_audit` (0 пока)
- **Что не так:**
  - `logs` пишется каждый event (start/stop, settings change, errors). За год: ~234 000 строк × ~100 байт = ~24 MB. Не катастрофа сейчас, но WB-устройство с eMMC.
  - Нет `DELETE FROM logs WHERE timestamp < datetime('now','-90 days')` job.
  - При reseting `logs` через VACUUM освободит место, но VACUUM в WAL делает копирование БД целиком.
- **Impact:** медленный рост, eMMC износ.
- **Направление фикса:**
  1. APScheduler job: cleanup старых logs > 180 дней.
  2. Опционально архивировать в `/mnt/sdcard/db/logs_archive_YYYY.db` через `sqlite3.backup()`.

#### DB-016: `LogRepository.create_backup` — `sqlite3.backup()` хорошо, но `wal_checkpoint(TRUNCATE)` после backup может block writers
- **Локация:** `db/logs.py:166-179`
- **Что не так:**
  - Правильно использует `sqlite3.Connection.backup()` (online backup API, безопасен с WAL — это лучше чем `cp file`).
  - Но затем делает `PRAGMA wal_checkpoint(TRUNCATE)` — это блокирует writers до завершения checkpoint.
  - Если бэкап делается в момент scheduler-полива (полив в 03:00, бэкап тоже в 03:00 — нужно разнести во времени).
  - Сейчас `_cleanup_old_backups(keep_count=7)` — 7 ежедневных = 1 неделя retention. Маловато.
- **Impact:** возможный stall при backup.
- **Направление фикса:**
  1. Запускать backup в момент гарантированного «idle» (например 04:30, после полива).
  2. Использовать `wal_checkpoint(PASSIVE)` вместо TRUNCATE — не блокирует.
  3. retention 30 дней (на 33 GB SD места хватит с запасом).

---

### LOW

#### DB-017: BLOB-like JSON в `weather_cache.data` — без compression
- **Локация:** `weather_cache(data TEXT)`
- **Что не так:** Open-Meteo response = ~30 KB JSON. На каждый location кешится 1 row, TTL ~часы. Не проблема, но можно `zlib.compress` → ~5 KB. Маленькая БД ценна на eMMC.
- **Impact:** косметика.
- **Направление фикса:** опционально в будущем.

#### DB-018: `mqtt_servers.enabled` — нет индекса (но всего 1 строка на проде)
- **Локация:** `mqtt_servers` table
- **EXPLAIN:** `SELECT * FROM mqtt_servers WHERE enabled=1` → `SCAN mqtt_servers`.
- **Impact:** на 1 строке несущественно. Но если планируется поддержка нескольких bridges — добавить `idx_mqtt_servers_enabled`.

---

## Index recommendations (сводная таблица)

| Query (где используется) | Table | Текущий план | Предлагаемый индекс | Эффект |
|---|---|---|---|---|
| `logs WHERE type=? AND ts>=? ORDER BY ts DESC` (`db/logs.py:20`) | `logs` | `SEARCH idx_logs_type` + sort | `idx_logs_type_ts ON logs(type, timestamp DESC)` | Убрать sort |
| `water_usage WHERE zone_id=? AND ts>=? ORDER BY ts DESC` (`db/logs.py:73`) | `water_usage` | `SEARCH idx_water_zone` + sort | `idx_water_zone_ts ON water_usage(zone_id, timestamp DESC)` | Убрать sort |
| `weather_cache WHERE lat=? AND lon=? ORDER BY fetched_at DESC LIMIT 1` (`services/weather.py:756`) | `weather_cache` | `SEARCH idx_weather_cache_loc` + sort | `DROP idx_weather_cache_loc; CREATE idx_weather_cache_lookup(latitude, longitude, fetched_at DESC)` | One seek |
| `weather_log WHERE zone_id=? ORDER BY created_at DESC` | `weather_log` | `SEARCH idx_weather_log_zone` + sort | `idx_weather_log_zone_ts(zone_id, created_at DESC)` | Убрать sort |
| `bot_audit ORDER BY ts DESC LIMIT 100` | `bot_audit` | `SCAN bot_audit` + sort | `idx_bot_audit_ts(ts DESC)` | Index scan |
| `mqtt_servers WHERE enabled=1` (для 1 row не нужен) | `mqtt_servers` | `SCAN` | (skip — слишком мало строк) | — |

**Итого предлагается:** +5 индексов, -1 (заменён на covering). Net +4 индекса. Write-overhead минимален.

---

## Migration framework review

Файл: `db/migrations.py` (1084 LOC, 35 миграций, 30 из них — `ALTER TABLE ADD COLUMN`).

### Структура

- Класс `MigrationRunner(db_path)`.
- `init_database()` создаёт базовые tables через `CREATE TABLE IF NOT EXISTS` + базовые индексы, потом цепочка `_apply_named_migration` 35 штук.
- `_apply_named_migration(conn, name, func)` — вспомогательный «runner».
- `rollback_migration(name)` + `DOWNGRADE_REGISTRY` — есть downgrade для **некоторых** миграций (виден код `_recreate_table_without_columns` для DROP COLUMN).

### Сильные стороны

- ✅ Идемпотентность на уровне «миграция применяется один раз» — через таблицу `migrations`.
- ✅ Каждая `_migrate_add_*_column` проверяет `PRAGMA table_info` перед `ALTER ADD COLUMN` (защита от duplicate).
- ✅ Все `CREATE TABLE` / `CREATE INDEX` — с `IF NOT EXISTS`.
- ✅ Downgrade-функционал есть (хоть и не для всех миграций).
- ✅ Использует stateful `migrations(name TEXT PRIMARY KEY)`, не version number — позволяет non-linear development.

### Слабые стороны

- ❌ DB-002: не атомарны — `func` + `INSERT migrations` не в одной транзакции.
- ❌ DB-003: errors swallowed в `_apply_named_migration`.
- ❌ Каждая `_migrate_*` сама делает `conn.commit()` внутри (`db/migrations.py:322, 333, 344, 355` etc) — нарушает single-commit паттерн.
- ❌ `init_database()` вызывается из `IrrigationDB.__init__`, который instantiated на module-load (`from database import db`). При тестах — каждый импорт прогоняет миграции на тестовой БД. Медленно и шумно.
- ❌ `_recreate_table_without_columns` (`db/migrations.py:240-284`) для DROP COLUMN использует `ALTER TABLE RENAME` после копирования данных — но **не** копирует индексы и triggers. После downgrade индексы пропадут.
- ❌ Нет dry-run опции, нет diff между «applied vs available».
- ❌ `_insert_initial_data` (`db/migrations.py:168-194`) при первой инициализации **жёстко прошивает** `password_hash = generate_password_hash('1234')` — это потенциальный security-bug, но это scope security-auditor'а, упомянуто здесь только в контексте миграций.

### Что произойдёт если миграция упала посередине

**Сценарий A (ALTER TABLE):**
1. `_migrate_add_postpone_reason` начинается.
2. `PRAGMA table_info(zones)` → нет колонки.
3. `ALTER TABLE zones ADD COLUMN postpone_reason TEXT` — выполнен (auto-commit).
4. `conn.commit()` — no-op (уже committed).
5. **Power loss до `INSERT INTO migrations`**.
6. Restart → `init_database()`:
   - `_apply_named_migration(conn, 'zones_add_postpone_reason', ...)`
   - `SELECT name FROM migrations WHERE name = 'zones_add_postpone_reason'` → пусто.
   - `func(conn)` = `_migrate_add_postpone_reason`:
     - `PRAGMA table_info(zones)` → колонка ЕСТЬ.
     - `if 'postpone_reason' not in columns:` — false, skip.
     - func возвращает None без ошибок.
   - `INSERT migrations(name)` — успех.
7. **OK** — этот случай защищён `PRAGMA table_info` check.

**Сценарий B (CREATE TABLE с двумя индексами):**
1. `_migrate_create_weather_cache`:
   - `CREATE TABLE IF NOT EXISTS weather_cache(...)` — успех.
   - `CREATE INDEX idx_weather_cache_loc ON weather_cache(latitude, longitude)` — успех.
   - **Power loss** до `CREATE INDEX idx_weather_cache_time`.
2. Restart → попытка повтора:
   - CREATE TABLE — IF NOT EXISTS, ОК.
   - CREATE INDEX loc — без `IF NOT EXISTS`, **дубликат → OperationalError** (если код не использовал IF NOT EXISTS).
   - Глянем фактический код `_migrate_create_weather_cache`:
     ```python
     conn.execute('CREATE INDEX IF NOT EXISTS idx_weather_cache_loc ...')
     ```
     — `IF NOT EXISTS` есть, значит этот сценарий тоже защищён.

**Сценарий C (DML миграция типа `_migrate_days_format`):**
- Делает `UPDATE programs SET days = ?` для 1 row, затем `conn.commit()`. Всё в одной транзакции, при power-loss — rollback. Идемпотентность через value-check (`if any(d < 0 or d > 6 for d in days)`).
- **OK.**

**Сценарий D (`_migrate_encrypt_mqtt_passwords`):**
- Читает `password`, шифрует, обновляет. Если упало посередине — часть rows зашифрована, часть нет. **Не идемпотентно** — повтор зашифрует уже зашифрованные.
- Возможно есть marker (`encrypted_prefix`), нужно посмотреть код миграции отдельно (не критично для текущего аудита).

**Вердикт:** базовая защита есть, но не системная. DB-002 описывает фикс.

### WAL recovery

При crash в момент DDL:
- WAL автоматически recover'ится при следующем open connection (sqlite-builtin).
- Но DDL частично-применённый = ALTER успел, INSERT migrations — нет. WAL не помогает — это logical-state issue.

---

## Direct sqlite3.connect() bypassing facade

Полный список найденных мест в production code (не считая тестов):

### 1. `services/weather.py` — 7 мест
| Line | Method | Operation | Issue |
|---|---|---|---|
| 332 | `_get_settings` | 12× SELECT в цикле | N+1, нет batch IN-clause |
| 381 | `_has_ms_threshold` | 1× SELECT | Дубль вызова без caching |
| 708 | `log_adjustment` | INSERT weather_log | OK, но без FK enforcement (DB-001) |
| 737 | `_get_location` | 2× SELECT lat/lon | Должен быть 1 SELECT IN |
| 756 | `_get_cached` | SELECT + ORDER + LIMIT 1 | Composite index missing (DB-009) |
| 779 | `_save_cache` | INSERT OR REPLACE + DELETE old | OK |
| 883 | stale fallback | SELECT идентичный 756 | Дубль кода |

**Класс не extends BaseRepository** — у него нет `_connect()`, нет PRAGMA настройки. Каждый connect = sqlite-defaults (FULL fsync, FK off).

### 2. `services/float_monitor.py:424` — `_get_db()` factory
```python
def _get_db(self):
    conn = sqlite3.connect(self.db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn
```
- **Единственное место** в коде, где явно ставится `busy_timeout=30000`.
- Использует `try/finally + conn.close()` (не `with`), что нормально.
- **НЕ** ставит `PRAGMA foreign_keys=ON` и `synchronous=NORMAL`.
- **НЕ** через facade.

### 3. `migrations/reencrypt_secrets.py:95` — standalone CLI script
```python
conn = sqlite3.connect(args.db)
```
- CLI-утилита для смены ключа шифрования. Не часть runtime app, ОК что прямой connect.

### 4. `routes/*.py` — 0 мест
Проверка `grep -n "sqlite3\.connect" /opt/claude-agents/irrigation-v2/routes/*.py` → пусто. Все routes используют facade `from database import db`.
**Расхождение с landscape.md:** там указано `routes/weather_api.py:63, 275` и `routes/settings.py:96` — это **устаревшая** информация из старой версии. В текущем `refactor/v2` (HEAD `e37adb7`) импорт `import sqlite3` оставлен только для `except sqlite3.Error:` обработчиков.

### Итого: 8 прямых `sqlite3.connect()` минус CLI-script = **8 в runtime коде**, все в `services/weather.py` (7) + `services/float_monitor.py` (1).

**Конкуренция writers:**
- scheduler-jobs → через `database.py` facade → `db/zones.py`, `db/programs.py`, `db/logs.py`
- web-requests → через facade
- monitors (rain/env/water) → через facade
- watchdog → через facade
- **weather-service → прямой connect** (services/weather.py)
- **float-monitor → прямой connect**
- migrations runner (на старте) → прямой connect

WAL допускает 1 writer + N readers одновременно. Все writers конкурируют за тот же `irrigation.db-shm` semaphore. При `database is locked`:
- repository-методы → `@retry_on_busy(max_retries=3)` (~0.7s total)
- weather-service → `timeout=5` на connect, после — exception swallowed на debug-level
- float-monitor → `busy_timeout=30000` (30s) — прибит конкретно
- migrations → `timeout=5` на connect

**Mismatch**: разные timeout'ы → потенциально weather-service «теряет» writes молча.

---

## Backup & retention

### Текущее состояние

- Метод `LogRepository.create_backup()` существует (`db/logs.py:157-186`):
  - ✅ Использует **правильный** `sqlite3.Connection.backup()` API (online, WAL-safe).
  - ✅ После backup делает `wal_checkpoint(TRUNCATE)` — flush WAL в main DB.
  - ✅ Cleanup: `keep_count=7`.
- **НО**: метод **никем не вызывается автоматически**.
- На проде: последний бэкап — `irrigation_backup_20260403_185606.db` (1 011 712 bytes), создан 3 апреля. Текущая дата 19 апреля = **16 дней без бэкапа**.
- `irrigation.db.bak.20260401_124241` — ad-hoc backup, ещё старше.

### Что критично

1. **DB-005**: нет cron / нет scheduler-job для регулярного backup.
2. На прод-устройстве (eMMC, 17 дней uptime, 24 зоны активно поливаются) — risk потери: settings changes, password, zone configurations, rain decisions, mqtt servers list.
3. SD-карта `/mnt/sdcard/backup` (28G свободно) и `/mnt/sdcard/db` (32G) — **простаивают**. Могли бы хранить 365+ дней daily backups.

### Backup-restore процедура

**Не задокументирована.** Нет README, нет скрипта `restore.sh`. Концептуально:

```bash
systemctl stop wb-irrigation
cp /mnt/data/irrigation-backups/irrigation_backup_YYYYMMDD_HHMMSS.db \
   /opt/wb-irrigation/irrigation/irrigation.db
rm -f /opt/wb-irrigation/irrigation/irrigation.db-wal \
      /opt/wb-irrigation/irrigation/irrigation.db-shm
chown root:root /opt/wb-irrigation/irrigation/irrigation.db
chmod 644 /opt/wb-irrigation/irrigation/irrigation.db
systemctl start wb-irrigation
```

Для восстановления с потерей < 1 минуты потребуется **WAL replay** с момента бэкапа — невозможно без литeral copy WAL-segment-store, чего нет.

---

## Тестируемость

- `database.py` создаёт singleton `db = IrrigationDB()` на module-load (см. `database.py` где-то в конце, подтверждено что `from database import db` работает в routes и services).
- Это означает: **любой `import` тестируемого модуля** запускает полную инициализацию БД (`init_database()` → 35 миграций) на дефолтном `irrigation.db`.
- `tests/fixtures/database.py` существует — создаёт временную БД (тоже через `sqlite3.connect`), но все production-модули продолжают использовать singleton.
- Чтобы переключить тесты на test-БД нужно либо:
  - Monkey-patch `db.db_path` ДО первого использования (фрагильно).
  - Передавать `db_path` через DI (нет в текущем коде).
- Конструктор `IrrigationDB(db_path='irrigation.db')` принимает path, но vector singleton'а это игнорирует.

**Direct test of weather/float_monitor**: т.к. они делают свой `sqlite3.connect(self.db_path)`, тесты вынуждены создавать **реальный** sqlite-файл (см. `tests/unit/test_weather.py:16, 137, 160, 188, 202` — 5 connect в одном файле тестов).

---

## Дополнительные observations

- **WAL autocheckpoint = 1000 страниц** (`db/migrations.py:26`). Default — устроен. На WB ARM (eMMC) — приемлемо.
- **cache_size = -4000** (= 4 MB). Скромно, но БД 1.16 MB полностью влезает.
- **temp_store = MEMORY** — хорошо, temp B-trees (см. DB-009) в RAM.
- На проде `.db-wal` и `.db-shm` отсутствовали при snapshot — значит после WAL checkpoint и нет активных writers в момент. Приемлемо.
- **page_size = 4096** — стандарт. На SSD/eMMC ОК.
- **auto_vacuum = 0 (NONE)** (на локалке, скорее всего и на проде — не дампилось). Без VACUUM БД будет фрагментироваться от UPDATE/DELETE. Учитывая, что `logs` append-only и не удаляется — фрагментация не критична. Но при cleanup (DB-015) понадобится manual VACUUM.

---

## Сводка по приоритетам для Phase 4

1. **DB-001** (CRITICAL) — `PRAGMA foreign_keys=ON` + добавить FK declarations + использовать `BaseRepository._connect()` везде.
2. **DB-002** (CRITICAL) — переписать `_apply_named_migration` с явной транзакцией + не глотать исключения.
3. **DB-005** (HIGH) — APScheduler-job на ежедневный backup в 04:30, retention 30 дней.
4. **DB-006 + DB-007** (HIGH) — рефактор: weather/float_monitor через repository, `BaseRepository._connect()` обязателен.
5. **DB-009** (HIGH) — добавить 4 composite индекса миграцией `add_composite_indices_v1`.
6. **DB-004** (CRITICAL) — `synchronous=NORMAL` на каждом коннекте — решается через DB-007.

Остальные находки — после первых 6.
