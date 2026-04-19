# Phase 1 — Landscape карта `wb-irrigation` (ветка `refactor/v2`)

> Read-only снапшот кода. Только факты с file:line. Без рекомендаций.

- **Локальный путь:** `/opt/claude-agents/irrigation-v2/`
- **Ветка:** `refactor/v2`
- **HEAD локальный:** `6a47153` (коммит — scaffold каталога `irrigation-audit/`)
- **HEAD прод:** `e37adb7` (`fix: watchdog forward reference — use lambda for lazy binding`)
- **Расхождение:** локально на 1 коммит впереди прода; делта — только новая папка `irrigation-audit/` (не код).
- **VERSION:** `2.0.186` (`/opt/claude-agents/irrigation-v2/VERSION:1`)
- **Origin:** `https://github.com/Raul-1996/irrigation.git`

Отчёт собран по локальному рабочему дереву refactor/v2 + read-only SSH-снапшот прода `WB-Techpom (10.2.5.244)`.

---

## 0. Что уже разобрано автором (3 авторских отчёта)

| Файл | Объём | Темы |
|---|---|---|
| `ARCHITECTURE-REPORT.md` | 501 LOC | Тип проекта (Flask+SQLite+MQTT+APScheduler), структура каталогов (старая, до refactor/v2), 4 «толстых» модуля (`weather.py 605`, `monitors.py 562`, `telegram_bot.py 564`, `weather_adjustment.py 508`), `app.py:1 → app.py:445` обзор, MQTT-flow, scheduler-flow, репозитории, инициализация, тестовый каталог, 12-пунктовый action plan. **На refactor/v2 устарел в части `services/` — см. §4.** |
| `BUGS-REPORT.md` | ~250 LOC | 7 багов: #1 `routes/settings.py` отсутствовал `import sqlite3` — **исправлен** (см. §11), #2 `services/zone_control.py` race condition в `_active_zones_lock`, #3 `routes/weather_api.py` прямой `sqlite3.connect` обходит facade, #4 `app.log` пустой 0 байт — **подтверждён на проде** (`/mnt/data/irrigation-backups/app.log` = 0), #5 broad `except Exception`, #6 циклический импорт app↔services — **исправлен** (commit `b899a5d`), #7 sqlite WAL без foreign_keys=ON — **частично подтверждён** (`PRAGMA foreign_keys=0` на проде, см. prod-snapshot). |
| `EXPERT-ANALYSIS.html` | 49 KB / ~700 LOC | Расширенный аудит: security (XSS, CSRF, MQTT no-TLS), performance, architecture smells, тесты. Не цитирую — формат HTML. |
| `CHANGELOG_desktop_sidebar.md` | ~80 LOC | UI changes only. Вне scope аудита. |

**Что в них УСТАРЕЛО (refactor/v2 поменял):**
- `services/weather.py` — было 605 LOC, **сейчас 1404** (консолидация всех weather-модулей)
- `services/weather_adjustment.py` — было 508, **сейчас 8** (stub re-export)
- `services/weather_merged.py` — было 425, **сейчас 92** (stub re-export)
- `services/monitors.py` (562 LOC) — **удалён**, заменён пакетом `services/monitors/` (4 файла, 619 LOC)
- Появился каталог `scheduler/` (6 файлов, 1258 LOC) — миксины для `IrrigationScheduler`
- Bug #6 «cycle app↔services» — уже починен в `b899a5d`
- Bug #1 «no `import sqlite3` в settings.py» — починен (см. `routes/settings.py:1`)

---

## 1. Точки входа

| Точка | Файл | Что делает |
|---|---|---|
| ASGI server | `run.py:55-77` | Hypercorn `serve()` на `0.0.0.0:${PORT:-8080}` через `_get_asgi_app(app)` (Flask 2.3+ native ASGI или WSGIMiddleware fallback). Регистрирует SIGTERM/SIGINT → `services.shutdown.shutdown_all_zones_off()` → `sys.exit(0)`. |
| Flask app factory | `app.py:81-89` | `app = Flask(__name__)`, `app.config.from_object(Config)` (или `TestConfig` если `TESTING=1`), `MAX_CONTENT_LENGTH=10MB`, `app.db = db`, `csrf = CSRFProtect(app)`. |
| Blueprint registration | `app.py:34-62` (импорты) + далее `app.register_blueprint(...)` | 18 blueprint'ов (см. §3). |
| App-init (one-time) | `services/app_init.py:23-80` `initialize_app(app, db, ...)` | 7 шагов: scheduler init → single-zone watchdog → cap-time watchdog → boot-sync (все клапаны OFF) → монiторы (rain/env/water) → MQTT warm-up → atexit/signal handlers. Защищён флагом `_INIT_DONE`. |
| systemd unit | `wb-irrigation.service:1-15` | `Type=simple`, `WorkingDirectory=/opt/wb-irrigation/irrigation`, `ExecStart=venv/bin/python run.py`, `Requires=mosquitto.service`, `Restart=on-failure`. |
| Telegram bot startup | `app.py:65-71` | `services.telegram_bot.subscribe_to_events()` + `start_long_polling_if_needed()` — синхронно при импорте app.py. |
| SSE Hub init | `app.py:111` | `_sse_hub.init(db=db, mqtt_module=mqtt, app_config=app.config, publish_mqtt_value=..., normalize_topic=..., get_scheduler=...)` — DI-инжекция, чтобы избежать circular imports. |

> **Нет** gunicorn config. Hypercorn вызывается inline из `run.py`.

---

## 2. Структура модулей по слоям

```
/
├── run.py (78)                    — entrypoint: hypercorn + signal handlers
├── app.py (445)                   — Flask app, blueprint registration, middleware
├── config.py (~30)                — Config / TestConfig (CSRF, SECRET_KEY, MAX_CONTENT_LENGTH)
├── constants.py (~50)             — APP-wide constants (PORT, MQTT_CACHE_TTL_SEC, и т.п.)
├── database.py (442)              — Facade: re-export IrrigationDB instance `db`
├── irrigation_scheduler.py (1750) — APScheduler core + IrrigationScheduler class (mixed-in)
├── utils.py (~600)                — encrypt/decrypt secrets, normalize_topic, helpers
├── routes/   (22 файла, ~6.5k LOC)— Flask blueprints (page + API)
├── services/ (27 файлов, ~10k LOC)— Business logic + integrations
│   └── monitors/  (4 файла, 619)  — rain, env, water monitors (subscribed to MQTT)
├── db/       (10 файлов, ~3.5k)   — Repository layer (BaseRepository pattern)
│   └── migrations.py (1100)       — 35 named migrations
├── scheduler/ (6 файлов, 1258)    — Mixins для IrrigationScheduler (jobs, runners, state)
├── templates/ (10 HTML, 2777)     — Jinja2 templates
├── static/                        — css/, js/, media/, photos/, sw.js
├── tools/                         — batch_replace.py, fix_exceptions.py, MQTT_emulator/
├── tests/                         — 114 test_*.py файлов в 7 подпапках (см. §7)
├── migrations/ (пусто)            — placeholder; реальные миграции в db/migrations.py
└── prototypes/                    — UI прототипы (не в production)
```

**Файлы > 500 LOC (топ-15 по LOC):**

| LOC | Файл | Слой | Заметка |
|---:|---|---|---|
| 1750 | `irrigation_scheduler.py` | scheduler core | + 6 mixin файлов в `scheduler/` (всего ~3000 LOC scheduler-логики) |
| 1404 | `services/weather.py` | service | Консолидирован из 3 модулей. **Содержит 7 прямых `sqlite3.connect()` обходящих repository** (line 332,381,708,737,756,779,883) |
| 1100 | `db/migrations.py` | db | 35 named migrations, downgrade методы для последних 10 |
| 1066 | `services/zone_control.py` | service | Центральный start/stop zones, `_active_zones_lock` (Bug #2 в BUGS-REPORT) |
| 855  | `services/program_queue.py` | service | Программная очередь + `ProgramCompletionTracker` |
| 749  | `services/security.py` | service | `admin_required`, `user_required` декораторы, basic-auth helpers |
| 681  | `services/zone_control.py` (ещё раз — двойной счёт исключён) | — | — |
| 656  | `scheduler/program_runner.py` | scheduler | Запуск программы по расписанию |
| 616  | `services/telegram_bot.py` | service | aiogram v3 long-polling |
| 603  | `services/float_monitor.py` | service | Поплавковый сенсор (защита от перелива) |
| 555  | `routes/system_status_api.py` | route api | health, status, scheduler/jobs, water — **главный noisy logger** на проде (см. prod-snapshot) |
| 549  | `services/observed_state.py` | service | State verifier для commanded vs observed клапанов |
| 549  | `db/zones.py` | db | Repository для zones |
| 548  | `routes/weather_api.py` | route api | Weather settings, decisions, log |
| 528  | `db/programs.py` | db | Repository для programs |

(Полный wc -l см. ниже в §10.)

---

## 3. Routes / Blueprints

22 файла в `routes/`, регистрируются в `app.py:34-62`.

### Page-rendering (HTML)

| Blueprint | Файл | URL |
|---|---|---|
| `status_bp` | `routes/status.py` | `/`, `/status` |
| `files_bp` | `routes/files.py` | `/map`, `/water` |
| `zones_bp` | `routes/zones.py` | `/zones` |
| `programs_bp` | `routes/programs.py` | `/programs` |
| `groups_bp` | `routes/groups.py` | `/logs` |
| `auth_bp` | `routes/auth.py` | `/login`, `/api/login` |
| `settings_bp` | `routes/settings.py` | `/settings`, `/api/settings/telegram[*]` |
| `mqtt_bp` | `routes/mqtt.py` | `/mqtt` |
| `telegram_bp` | `routes/telegram.py` | (необязательный, грузится try/except) |
| `reports_bp` | `routes/reports.py` | `/api/reports` |

### REST API (JSON)

| Blueprint | Файл | URL prefix |
|---|---|---|
| `zones_crud_api_bp` | `routes/zones_crud_api.py` | `/api/zones[*]` (GET/POST/PUT/DELETE, import, conflicts) |
| `zones_photo_api_bp` | `routes/zones_photo_api.py` | `/api/zones/<id>/photo` |
| `zones_watering_api_bp` | `routes/zones_watering_api.py` | `/api/zones/<id>/start`, `/stop`, `/postpone` |
| `groups_api_bp` | `routes/groups_api.py` | `/api/groups[*]`, master-valve actions |
| `programs_api_bp` | `routes/programs_api.py` | `/api/programs[*]`, duplicate, enabled, log, stats, check-conflicts |
| `mqtt_api_bp` | `routes/mqtt_api.py` | `/api/mqtt/servers[*]`, `/api/mqtt/<id>/probe`, `/scan-sse`, `/status` |
| `system_status_api_bp` | `routes/system_status_api.py` | `/api/health-details`, `/api/health/group/<id>/cancel`, `/api/health/job/<id>/cancel`, `/api/logs`, `/api/scheduler/{init,jobs,status}`, `/api/server-time`, `/api/status`, `/api/water`, `/health` |
| `system_config_api_bp` | `routes/system_config_api.py` | `/api/auth/status`, `/api/env[*]`, `/api/logging/debug`, `/api/map[*]`, `/api/password`, `/api/postpone`, `/api/rain`, `/api/settings/{early-off,system-name}`, `/logout` |
| `system_emergency_api_bp` | `routes/system_emergency_api.py` | `/api/backup`, `/api/emergency-stop`, `/api/emergency-resume` |
| `weather_api_bp` | `routes/weather_api.py` | `/api/weather[*]`, `/api/weather/{decisions,log,refresh}`, `/api/settings/{location,weather}` |
| `system_api_bp` (если есть) | `routes/system_api.py` | (есть файл, но не register'ится в app.py — потенциально dead code) |
| `zones_api_bp` | `routes/zones_api.py` | (тоже не находится в `app.register_blueprint` явно — нужно проверить, может legacy) |

### CSRF exemption (`app.py:96-109`)

ВСЕ API-blueprint'ы exempt'нуты от CSRF: `zones_watering_api_bp`, `groups_api_bp`, `system_emergency_api_bp`, `system_status_api_bp`, `system_config_api_bp`, `weather_api_bp`, `zones_crud_api_bp`, `zones_photo_api_bp`, `mqtt_api_bp`, `programs_api_bp`. Комментарий в коде (`app.py:96-99`): *"the service is behind nginx basic auth, so CSRF tokens are not needed for API calls. Guest users... must be able to control zones without a Flask session / CSRF token."*

---

## 4. services/ — слой бизнес-логики

27 файлов, ~10k LOC.

### Категории

**Координация запуска зон / программ:**
- `zone_control.py` (1066) — центральный API `start_zone`, `stop_zone`, `stop_all_in_group`, lock `_active_zones_lock`
- `program_queue.py` (855) — программная очередь, `ProgramCompletionTracker`
- `completion_tracker.py` (~80) — wrapper
- `observed_state.py` (549) — `state_verifier` сверка commanded vs observed (через MQTT subscribe)
- `watchdog.py` (~120) — cap-time watchdog (TASK-010): отключает зоны висящие сверх лимита

**MQTT слой:**
- `mqtt_pub.py` (233) — `get_or_create_mqtt_client(server)`, `publish_mqtt_value(server, topic, value, retain, qos)` с retry, dedup `_TOPIC_LAST_SEND`, TLS support, atexit shutdown
- `sse_hub.py` (~600) — централизованный SSE-хаб для realtime MQTT→браузер (DI через `init()`, `MAX_SSE_CLIENTS=5`)
- `monitors/` (пакет, 619 LOC) — см. ниже

**Мониторы (`services/monitors/`):**
- `__init__.py` (43) — re-exports + custom `__class__` чтобы тесты могли patch'ить `services.monitors.db` и оно пропагировалось в подмодули (lines 28-42)
- `rain_monitor.py` — RainMonitor + global `rain_monitor`
- `env_monitor.py` — EnvMonitor (temp/hum sensors через MQTT) + `probe_env_values`
- `water_monitor.py` — WaterMonitor (счётчики воды + поплавок)

**Уведомления / отчёты:**
- `telegram_bot.py` (616) — aiogram v3, long-polling (если включён в settings), FSM, ACL через bot_users table. Логи в `services/logs/telegram.txt` (на проде 520 KB)
- `reports.py` (~130) — `build_report_text` для telegram-отчётов
- `events.py` — pub-sub для in-process событий (telegram bot подписывается)

**Погода:**
- `weather.py` (1404) — Open-Meteo API client, WeatherData parser, WeatherService с SQLite-кешем (TTL 30 мин), WeatherAdjustment (Zimmerman + ET₀), `get_merged_weather`, daily/hourly forecast, sensor merge
- `weather_codes.py` (~200) — WMO weather codes → текст
- `weather_merged.py` (92) — backward-compat stub (re-exports)
- `weather_adjustment.py` (8) — backward-compat stub (re-exports)
- `et_calculator.py` (~150) — Penman-Monteith ET₀
- `irrigation_decision.py` — модель «поливать / не поливать»

**Безопасность / rate limit / shutdown:**
- `security.py` (749) — `admin_required` (5), `user_required` (16), basic-auth helpers
- `auth_service.py` (~80) — login/logout helpers
- `rate_limiter.py` (~60) — `login_limiter` (для /api/login)
- `api_rate_limiter.py` (~50) — `_is_allowed`, `rate_limit` декоратор для API
- `locks.py` (~120) — concurrency primitives
- `shutdown.py` (~130) — `shutdown_all_zones_off`, `reset_shutdown` (atexit + SIGTERM/SIGINT)

**Прочее:**
- `app_init.py` (320) — one-time bootstrap, `initialize_app`, `_boot_sync`, `_start_monitors`, `_warm_mqtt_clients`, `_register_shutdown_handlers`
- `helpers.py` — общие утилиты
- `logging_setup.py` — `setup_logging(logger)`, JSON-formatter
- `scheduler_service.py` (5) — **dead-code** stub (см. файл: *"Intentionally left empty"*)

### Дублирование / сомнительные паттерны

- `weather_merged.py` и `weather_adjustment.py` — оба stub'ы; импорты в кодбазе всё ещё ссылаются на старые имена.
- `routes/system_api.py`, `routes/zones_api.py` — есть файлы, но НЕ зарегистрированы в `app.py:34-62`. Возможно dead code (требует Phase 2 верификации).
- `services/scheduler_service.py` — пустой stub, упомянут в комментариях как deprecated.

---

## 5. db/ — Repository layer

10 файлов, паттерн BaseRepository.

| Файл | Назначение |
|---|---|
| `__init__.py` | пакет |
| `base.py` | `BaseRepository`, `retry_on_busy` декоратор для SQLite BUSY |
| `groups.py` | CRUD групп + master-valve / float settings |
| `logs.py` | append-only logs |
| `mqtt.py` | mqtt_servers (с TLS колонками + AES-encrypted password) |
| `programs.py` (528) | programs CRUD + 35 миграций programs_v2 (type, schedule_type, interval_days, even_odd, color, enabled, extra_times) |
| `settings.py` | key-value settings table |
| `telegram.py` | bot_users, bot_subscriptions, bot_audit, bot_idempotency |
| `zones.py` (549) | zones CRUD + watering state |
| `migrations.py` (1100) | `MigrationRunner.init_database()` — 35 named миграций, downgrade для последних 10 |

**Facade:** `database.py` экспортирует singleton `db` (`IrrigationDB`) с методами агрегирующими все репозитории. Используется как `from database import db` (20 раз в кодбазе — самый частый межмодульный импорт, см. §6).

**SQLite PRAGMA (db/migrations.py:21-26):** WAL, foreign_keys=ON, synchronous=NORMAL, wal_autocheckpoint=1000, cache_size=-4000, temp_store=MEMORY.

> **Расхождение:** на проде `PRAGMA foreign_keys = 0` (см. prod-snapshot). Возможно PRAGMA не персистентна между connection'ами — sqlite3 включает foreign_keys только per-connection.

---

## 6. scheduler/ — APScheduler runners

6 файлов, 1258 LOC. Пакет содержит **mixin'ы** для класса `IrrigationScheduler` (объявлен в `irrigation_scheduler.py:~140`).

| Файл | LOC | Что делает |
|---|---:|---|
| `__init__.py` | 1 | пакет |
| `jobs.py` | (модульные функции) | Module-level callables для APScheduler persistence (`job_run_program`, `job_run_group_sequence`, `job_stop_zone`, `job_close_master_valve`). Все они через `get_scheduler()` дёргают методы singleton'а. |
| `program_runner.py` | 656 | `ProgramRunnerMixin` — `_run_program_threaded`, `_run_group_sequence`, обработка отмен через `program_cancellations`-таблицу, очередь через `program_queue.py` |
| `zone_runner.py` | 193 | `ZoneRunnerMixin` — `_stop_zone` (zone_control facade + DB fallback), `schedule_zone_stop(zone_id, duration_minutes)` с учётом `early_off_seconds` |
| `state.py` | (~130) | `StateMixin` — `clear_expired_postpones`, `get_active_programs`, `get_active_zones` |
| `schedule_calc.py` | 185 | Расчёт ближайшего запуска программы по cron / interval / even-odd / weekdays |

**APScheduler config:** `BackgroundScheduler`, jobstore — `SQLAlchemyJobStore` (если установлен) или `MemoryJobStore` fallback. Триггеры: `CronTrigger`, `DateTrigger`, `IntervalTrigger`. Timezone: `ZoneInfo` (Python 3.9+). `apscheduler` logger подавлен до `ERROR` (`irrigation_scheduler.py:60-62`).

---

## 7. Граф зависимостей (импорты)

Топ межмодульных импортов (по `grep -rEh "^from (database|services|...)"`):

```
20× from database import db
 9× from utils import normalize_topic
 8× from services.security import admin_required
 8× from db.base import BaseRepository, retry_on_busy
 5× from irrigation_scheduler import get_scheduler
 4× from services.mqtt_pub import publish_mqtt_value
 4× from services import sse_hub as _sse_hub
 4× from services.api_rate_limiter import rate_limit
 2× from services.security import user_required
 2× from utils import encrypt_secret, decrypt_secret
```

### Циклы

- **`app ↔ services` цикл** — БЫЛ, исправлен в `b899a5d` (вынесли init в `services/app_init.py` с DI через параметры функции). Локальный grep подтверждает: ни один файл из `services/`, `db/`, `scheduler/`, `routes/` не делает `from app import` или `import app` на module-level.
- **Lazy imports внутри функций:**
  - `scheduler/zone_runner.py:29` — `from services.zone_control import stop_zone` (внутри `_stop_zone`)
  - `irrigation_scheduler.py:70-73` — `from irrigation_scheduler import get_scheduler` (внутри module-level job-callable; same module, защита от циркуляров при APScheduler unpickling)
  - `services/app_init.py:45,62,89-90,221-225,260-261` — все ленивые импорты scheduler/monitors/MQTT во время `initialize_app()`

### Hub-модули (наибольший fan-in)

- `database` (`from database import db`) — 20 импортов
- `utils` (`normalize_topic`, `encrypt/decrypt_secret`) — 12 импортов
- `db.base` (`BaseRepository`, `retry_on_busy`) — 8 импортов
- `irrigation_scheduler.get_scheduler` — 5 импортов
- `services.mqtt_pub.publish_mqtt_value` — 4 импорта (плюс ещё несколько aliased)

### Test-friendly DI

`services/sse_hub.py:36-58` — все зависимости (db, mqtt, app.config, publish_mqtt_value, normalize_topic, get_scheduler) инжектируются через `init(...)` чтобы избежать циклических импортов.

`services/monitors/__init__.py:28-42` — кастомный `__class__` для модуля: `setattr(services.monitors, 'db', X)` пропагируется в `rain_monitor.db`, `env_monitor.db`, `water_monitor.db`. Это backward-compat для тестов которые делают `patch('services.monitors.db', ...)`.

---

## 8. Внешние интеграции

### MQTT (`services/mqtt_pub.py` + `services/monitors/*`)

- **Клиент:** `paho-mqtt==2.1.0`, `mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)` (`services/mqtt_pub.py:45`)
- **Сервера хранятся в БД:** таблица `mqtt_servers` (id, name, host, port=1883, username, password, client_id, enabled, TLS columns). Пароль зашифрован AES-GCM через `utils.encrypt_secret` (миграция `encrypt_mqtt_passwords` — `db/migrations.py:146`).
- **Кеш клиентов:** in-process `_MQTT_CLIENTS: Dict[int, Client]` с lock + `_SERVER_CACHE` (TTL = `MQTT_CACHE_TTL_SEC` из constants).
- **Publish flow:** `publish_mqtt_value(server, topic, value, min_interval_sec=0.2, retain=False, qos=0)` → нормализует topic → дедуплицирует по `(sid, topic)` → publish base topic + дублирует в `topic+'/on'` (Wirenboard compat, `mqtt_pub.py:179-195`) → опциональный `topic+'/meta'` для diagnostics.
- **QoS≥1:** до 3 retry с backoff 1/2/4 сек (`mqtt_pub.py:155-174`).
- **Subscribe-стороны:**
  - SSE Hub (`services/sse_hub.py`) — slurp'ит retain MQTT → broadcast'ит в browser клиенты
  - `services/monitors/water_monitor.py` — счётчики воды
  - `services/monitors/rain_monitor.py` — датчик дождя
  - `services/monitors/env_monitor.py` — temp/hum
  - `services/float_monitor.py` (603 LOC) — поплавок (auto-pause при перетоке)
  - `services/observed_state.py` — verifier commanded vs observed (читает retained state клапанов)
- **TLS:** поддерживается (`mqtt_pub.py:48-61`), но в БД на проде `tls_enabled=0` (см. prod-snapshot — все listener'ы plain).

### Telegram (`services/telegram_bot.py` + `routes/telegram.py`)

- **Библиотека:** `aiogram>=3.8.0` (на проде `aiogram==3.22.0`)
- **Запуск:** `app.py:65-71` — при импорте app дёргается `subscribe_to_events()` + `start_long_polling_if_needed()`
- **Загрузка маршрутов бота:** `services/telegram_bot.py:21-39` — нестандартно через `importlib.util.spec_from_file_location('wb_routes_telegram', 'routes/telegram.py')` чтобы избежать конфликтов с одноимённым внешним пакетом
- **Хранение токена:** в таблице `settings` (key=`telegram_bot_token`?), зашифровано через `utils.decrypt_secret` (`telegram_bot.py:5`). Alt: env var.
- **Логи:** отдельный logger 'TELEGRAM' пишет в `services/logs/telegram.txt` (на проде 520 KB, ротации НЕТ)
- **DB:** таблицы `bot_users`, `bot_subscriptions`, `bot_audit`, `bot_idempotency` (см. schema в prod-snapshot).
- **На проде:** `bot_users=0` — Telegram-бот настроен, но никто не зарегистрирован.

### SSE (`services/sse_hub.py`)

- **Hub:** `services/sse_hub.py` — единый процесс-уровневый hub
- **MAX_SSE_CLIENTS = 5** (`sse_hub.py:23`) — жёсткий лимит подключений
- **Хранение:** `_SSE_HUB_CLIENTS: list[queue.Queue]`, `_SSE_META_BUFFER: deque(maxlen=100)`
- **Endpoint:** не нашёл явного `/sse` route в gpinge — возможно через `routes/system_status_api.py` или `routes/mqtt_api.py:/api/mqtt/<id>/scan-sse`
- **Авторизация:** через `services.security.user_required` (стандартный декоратор), плюс CSRF-exempt все API (`app.py:96-109`)

### Cloudflare Tunnel

- На проде запускается systemd unit `cloudflared-poliv.service` (см. prod-snapshot)
- Config: `/etc/cloudflared/config-poliv.yml` → `tunnel: 6b24575b-b98a-41e4-8478-a71731d03abe`, ingress `poliv-kg.ops-lab.dev → http://localhost:8080`
- В коде репо ничего про cloudflared нет — это deployment-уровень, не application.

### OpenMeteo / Weather API

- **URL:** `https://api.open-meteo.com/v1/forecast` (`services/weather.py:26`)
- **TTL кеша:** 30 мин (`_CACHE_TTL_SEC = 30 * 60`, `weather.py:27`)
- **Timeout:** 10 сек (`_REQUEST_TIMEOUT = 10`)
- **Кеш:** SQLite таблица `weather_cache` (lat, lon, data, fetched_at) — на проде 1 запись
- **Adjustment:** `WeatherAdjustment` использует Zimmerman формулу + ET₀, `weather_decisions` таблица для аудита решений (на проде 0 записей — фича включена, но не активирована)

---

## 9. Конфигурация и секреты

### Файлы

| Файл | Назначение |
|---|---|
| `config.py` | `Config`, `TestConfig` (CSRF disabled, SECRET_KEY fixed) |
| `constants.py` | APP-wide константы: `MQTT_CACHE_TTL_SEC`, `DEFAULT_PORT`, и т.п. |
| `.env.example` | Шаблон env vars: `SECRET_KEY`, `TESTING`, `SESSION_COOKIE_SECURE`, `PORT`, `TZ`, `WB_TZ`, `IRRIG_SECRET_KEY`, `SCHEDULER_LOG_LEVEL`, basic-auth-proxy vars |
| `.irrig_secret_key` | На проде: 32 байта, owner=1001:1001, mode 600. Используется для AES-GCM шифрования MQTT/Telegram паролей. Если отсутствует — auto-generates при импорте utils.py. |
| `wb-irrigation.service` | systemd unit (см. §1) |

### Загрузка env

`run.py:60` читает `os.environ.get('PORT', '8080')`.
`app.py` использует `os.environ.get('TESTING')`.
`config.py` (Config object) — Flask `from_object`.
**Нет** `python-dotenv` импорта в коде, хотя `python-dotenv>=1.0.1` в requirements.

### Секреты в коде / БД

- MQTT password: AES-GCM в `mqtt_servers.password` (после миграции `encrypt_mqtt_passwords`)
- Telegram token: в settings table, зашифрован
- App password (admin): `pbkdf2:sha256` хеш в `settings.password_hash`. Initial value `'1234'` (`db/migrations.py:176`).
- Flask SECRET_KEY: env `SECRET_KEY` или auto-generated в `.secret_key` (см. `.env.example:7-9`)

---

## 10. Тесты, CI/CD

### `tests/` (114 test_*.py файлов)

| Подпапка | Файлов | Назначение |
|---:|---|---|
| 54 | `tests/unit/` | unit-тесты (scheduler, monitors, mqtt_pub, observed_state, weather, completion_tracker, locks, watchdog, etc.) |
| 27 | `tests/api/` | route-level тесты (Flask test client) |
| 21 | `tests/db/` | repository тесты |
| 5  | `tests/integration/` | интеграционные |
| 3  | `tests/ui/` | Selenium UI |
| 2  | `tests/performance/` | smoke perf |
| 2  | `tests/e2e/` | end-to-end (отмечены `@pytest.mark.e2e`) |

`pytest.ini:1-13` маркеры: `mqtt_real` (требует broker на 10.2.5.244), `e2e`, `slow`. Selenium-плагин отключён (`-p no:selenium -p no:pytest_selenium`).

`pyproject.toml`:
- `[tool.pytest.ini_options]` (дублирует pytest.ini, что может конфликтовать)
- `[tool.coverage]` `fail_under = 30`
- `[tool.ruff]` `line-length = 120`, `target-version = "py311"`
- selectors: E, W, F, I, UP, B, SIM, RUF

### CI: `.github/workflows/`

`ci.yml` (ветки **`[main, master]`** — НЕ refactor/v2!):
1. **lint:** `ruff check .` + `ruff format --check .`
2. **test:** `pytest tests/unit tests/db tests/api --cov` (только 3 подпапки, e2e/integration/ui/performance НЕ в CI)
3. **security:** `bandit -r app.py database.py irrigation_scheduler.py services/ routes/ db/ utils.py config.py -ll`

`deploy.yml` (manual `workflow_dispatch` с подтверждением `"deploy"`):
- Через `appleboy/ssh-action@v1` с jump-host, `git pull origin **main**` + `pip3 install -r requirements.txt` + `systemctl restart wb-irrigation`. **Несовместимо с прод-веткой `refactor/v2`** — pull на main снесёт текущий код.

---

## 11. Сборка / деплой

| Файл | Назначение |
|---|---|
| `Dockerfile` | `FROM python:3.11-slim`, build-essential для Pillow, EXPOSE 8080, HEALTHCHECK `curl /` (а не `/health`!), USER appuser, `CMD python run.py` |
| `docker-compose.yml` | 2 сервиса: `app` (build .) + `mqtt` (eclipse-mosquitto:2 на порту 1884:1883), volume для irrigation.db, mosquitto.conf, passwd, acl |
| `mosquitto.conf` (в репо) | listener 1883, **`allow_anonymous false`**, password_file, acl_file (НО на проде применён ДРУГОЙ конфиг — см. prod-snapshot) |
| `mosquitto/` | acl, setup_auth.sh |
| `install_wb.sh` | Bare-metal installer для Wirenboard: apt install python3-venv git sqlite3, git clone, venv, pip install, seed mqtt_servers row, copy systemd unit, enable, healthcheck |
| `install_docker.sh` | Compose-based: `docker compose build` + `up -d` |
| `update_server.sh` | **`BRANCH=${BRANCH:-main}`** по умолчанию (строка 14) → `git fetch + git reset --hard origin/$BRANCH` → venv ensure → systemd reload + restart. **Нужно явно передать `BRANCH=refactor/v2`** иначе обновление снесёт прод-ветку. |
| `configs/nginx-rate-limit.conf` | nginx config (rate limit для basic-auth прокси) |
| `basic_auth_proxy.py` | Standalone Flask-прокси, использует env `UPSTREAM_PORT=8080`, `AUTH_USER`, `AUTH_PASS`, `AUTH_REALM`, `LISTEN_PORT=8081`. На проде запущен на 127.0.0.1:8011 (см. prod-snapshot ports). |

### Bug-fix verification (vs BUGS-REPORT)

- **Bug #1** «отсутствует import sqlite3 в routes/settings.py» — **ИСПРАВЛЕНО**: `routes/settings.py:1` теперь содержит `import sqlite3` (плюс 4 except'а sqlite3.Error на lines 32, 74, 108, 111).
- **Bug #3** «routes/weather_api.py делает прямой sqlite3.connect» — **частично**: в самом `routes/weather_api.py` прямого `sqlite3.connect()` НЕТ, но в `services/weather.py` (1404 LOC) есть **7 прямых `sqlite3.connect(self.db_path, timeout=5)`** (lines 332, 381, 708, 737, 756, 779, 883), обходящих repository facade.
- **Bug #4** «app.log пустой 0 байт» — **ПОДТВЕРЖДЁН на проде**: `/mnt/data/irrigation-backups/app.log` = 0 bytes (см. prod-snapshot).
- **Bug #6** «cycle app↔services» — **ИСПРАВЛЕНО** (commit `b899a5d`).

---

## 12. Что НЕ верифицировано / ушло в open-questions

См. `open-questions.md`. Главные пункты:
- `routes/system_api.py` и `routes/zones_api.py` — есть файлы, нет регистрации в app.py — dead code?
- `services/scheduler_service.py` — официально dead-stub, но кто-то может его импортировать?
- AES-GCM в `utils.encrypt_secret` — нужно посмотреть деталей, как XOR-fallback из BUGS-REPORT/EXPERT-ANALYSIS соотносится с текущей реализацией
- Откуда берётся SSE-endpoint URL — не нашёл `@blueprint.route('/sse')`
