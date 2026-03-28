# Технический план доработки wb-irrigation

**Версия:** 1.0  
**Дата:** 2026-03-28  
**Основание:** spec.md

---

## 1. Архитектура ПОСЛЕ рефакторинга

### Целевая структура файлов

```
wb-irrigation/
├── app.py                          # ~400 строк: create_app(), config, blueprint registration, error handlers
├── config.py                       # Config class + load_or_generate_secret()
├── constants.py                    # Именованные константы (MAX_MANUAL_WATERING_MIN и т.д.)
├── database.py                     # IrrigationDB — фасад, делегирует в db/*
├── run.py                          # Точка входа Hypercorn
├── utils.py                        # encrypt_secret/decrypt_secret с безопасным ключом
├── irrigation_scheduler.py         # APScheduler логика (без импорта app)
│
├── db/
│   ├── __init__.py
│   ├── base.py                     # BaseRepository: _connect(), @retry_on_busy
│   ├── zones.py                    # ZoneRepository: CRUD, zone_runs, bulk
│   ├── programs.py                 # ProgramRepository: CRUD, conflicts
│   ├── groups.py                   # GroupRepository: CRUD
│   ├── mqtt.py                     # MqttRepository: CRUD + encrypt/decrypt passwords
│   ├── settings.py                 # SettingsRepository: get/set, rain/env config, password
│   ├── telegram.py                 # TelegramRepository: bot_users, subscriptions, FSM
│   ├── logs.py                     # LogRepository: logs, water_usage, water_stats
│   └── migrations.py              # Все _migrate_* методы
│
├── routes/
│   ├── __init__.py
│   ├── auth.py                     # Login/logout + rate_limiter (существует, доработать)
│   ├── zones.py                    # Page render (существует)
│   ├── zones_api.py                # API: /api/zones/*, /api/zones/<id>/mqtt/*, фото
│   ├── programs.py                 # Page render (существует)
│   ├── programs_api.py             # API: /api/programs/*
│   ├── groups.py                   # Page render (существует)
│   ├── groups_api.py               # API: /api/groups/*, master-valve
│   ├── mqtt.py                     # Page render (существует)
│   ├── mqtt_api.py                 # API: /api/mqtt/*, SSE scan
│   ├── system_api.py              # API: /api/status, /api/rain, /api/env, /api/emergency-*, 
│   │                               #       /api/backup, /api/water, /api/settings/*, /api/health/*
│   ├── settings.py                 # Page render (существует)
│   ├── status.py                   # Page render (существует)
│   ├── files.py                    # Static files (существует)
│   ├── reports.py                  # Reports (существует)
│   └── telegram.py                 # Telegram webhook (существует)
│
├── services/
│   ├── __init__.py
│   ├── zone_control.py             # Управление зонами (существует, доработать QoS/retain)
│   ├── mqtt_pub.py                 # MQTT publisher (существует, доработать QoS default)
│   ├── monitors.py                 # RainMonitor, EnvMonitor, WaterMonitor (существует, единственная копия)
│   ├── events.py                   # Event dedup (существует)
│   ├── security.py                 # admin_required, декораторы (существует)
│   ├── auth_service.py             # Auth logic (существует)
│   ├── locks.py                    # Locks (существует)
│   ├── telegram_bot.py             # Telegram bot (существует)
│   ├── scheduler_service.py        # Scheduler service (существует)
│   ├── reports.py                  # Reports service (существует)
│   ├── rate_limiter.py             # НОВЫЙ: IP-based rate limiting
│   ├── sse_hub.py                  # НОВЫЙ: SSE Hub (из app.py)
│   ├── app_init.py                 # НОВЫЙ: boot-sync, monitor init, scheduler init
│   ├── observed_state.py           # НОВЫЙ: проверка observed_state после publish
│   └── watchdog.py                 # НОВЫЙ: фоновый watchdog зон
│
├── mosquitto/
│   ├── mosquitto.conf              # С аутентификацией
│   ├── acl                         # ACL правила
│   └── passwd                      # Файл паролей (генерируется)
│
├── templates/                      # HTML шаблоны (без изменений)
├── static/                         # CSS/JS/media (без изменений)
├── tools/tests/                    # Тесты
├── reports/                        # Отчёты (stage1, stage2, stage3 + перенести *_REPORT.md)
├── backups/                        # Бэкапы БД
├── specs/                          # Спецификации
│
├── .env.example                    # НОВЫЙ: документация env-переменных
├── .secret_key                     # НОВЫЙ: автогенерируемый Flask SECRET_KEY (.gitignore)
├── .irrig_secret_key               # НОВЫЙ: автогенерируемый ключ шифрования (.gitignore)
├── VERSION                         # НОВЫЙ: версия приложения
├── Dockerfile                      # Обновить: python:3.11-slim + non-root
├── docker-compose.yml              # Обновить: убрать дефолт SECRET_KEY, volumes для mosquitto auth
├── requirements.txt                # Обновить: версии зависимостей
├── pytest.ini                      # Обновить: таймауты
└── .gitignore                      # Обновить: .secret_key, .irrig_secret_key
```

---

## 2. Модули: что входит, откуда выносится

### 2.1 `services/rate_limiter.py` (НОВЫЙ)

**Что входит:**
- Класс `LoginRateLimiter` с in-memory хранением по IP
- `check(ip) → (allowed, retry_after_sec)`
- `record_failure(ip)`, `reset(ip)`
- Thread-safe (threading.Lock)

**Откуда:** Заменяет session-based rate-limit в `routes/auth.py:24-29`

### 2.2 `services/sse_hub.py` (НОВЫЙ)

**Что входит:**
- `_ensure_hub_started()`, `_rebuild_subscriptions()`
- Глобальные `_SSE_HUB_*`, `_SSE_META_BUFFER`
- `_on_message` callback для SSE (app.py:3798-3899)
- `observed_state` update logic

**Откуда:** app.py строки ~3750-3920

### 2.3 `services/app_init.py` (НОВЫЙ)

**Что входит:**
- Boot-sync логика (остановка всех зон при старте)
- Инициализация scheduler
- Инициализация мониторов (один раз, не в before_request)
- MQTT warm-up
- Watchdog thread start

**Откуда:** app.py `_init_scheduler_before_request()` строки 858-1020

### 2.4 `services/observed_state.py` (НОВЫЙ)

**Что входит:**
- `verify_state_change(zone_id, expected_state, timeout=10, retries=3) → bool`
- Подписка на MQTT топик зоны, ожидание observed_state
- Retry с exponential backoff
- Telegram alert при failure
- Update `fault_count`, `last_fault` в БД

### 2.5 `services/watchdog.py` (НОВЫЙ)

**Что входит:**
- Фоновый daemon-поток
- Проверка всех зон со state='on' каждые 30 сек
- Проверка cap превышения, stale observed_state
- Глобальный лимит одновременных зон
- Emergency stop при аномалии

### 2.6 `routes/zones_api.py` (НОВЫЙ)

**Что входит** (из app.py):
- `api_zones()` — GET /api/zones
- `api_zone()` — GET/PUT/DELETE /api/zones/<id>
- `api_create_zone()` — POST /api/zones
- `api_import_zones_bulk()` — POST /api/zones/import
- `api_zone_next_watering()` — GET /api/zones/<id>/next-watering
- `api_zones_next_watering_bulk()` — POST /api/zones/next-watering-bulk
- `upload_zone_photo()`, `delete_zone_photo()`, `rotate_zone_photo()`, `get_zone_photo()`
- `start_zone()`, `stop_zone()`
- `api_zone_watering_time()`
- `api_zone_mqtt_start()`, `api_zone_mqtt_stop()`
- `api_check_zone_duration_conflicts()`, `api_check_zone_duration_conflicts_bulk()`

**Итого:** ~800 строк

### 2.7 `routes/groups_api.py` (НОВЫЙ)

**Что входит** (из app.py):
- `api_groups()`, `api_update_group()`, `api_create_group()`, `api_delete_group()`
- `api_stop_group()`, `api_start_group_from_first()`, `api_start_zone_exclusive()`
- `api_master_valve_toggle()`

**Итого:** ~400 строк

### 2.8 `routes/programs_api.py` (НОВЫЙ)

**Что входит** (из app.py):
- `api_programs()`, `api_program()`, `api_create_program()`, `check_program_conflicts()`

**Итого:** ~200 строк

### 2.9 `routes/mqtt_api.py` (НОВЫЙ)

**Что входит** (из app.py):
- CRUD mqtt_servers: list/create/get/update/delete
- `api_mqtt_probe()`, `api_mqtt_status()`
- `api_mqtt_scan_sse()`, `api_mqtt_zones_sse()` (SSE endpoints, используют sse_hub)

**Итого:** ~500 строк

### 2.10 `routes/system_api.py` (НОВЫЙ)

**Что входит** (из app.py):
- `api_status()` (~200 строк)
- `api_rain_config()`, `api_env_config()`, `api_env_values()`
- `api_postpone()`
- `api_emergency_stop()`, `api_emergency_resume()`
- `api_backup()`
- `api_water()` (без рандома — реальные данные или пустой массив)
- `api_server_time()`
- `api_scheduler_init/status/jobs()`
- `api_health_details()`, `api_health_cancel_job/group()`
- `api_logging_debug_toggle()`
- `api_setting_early_off()`, `api_setting_system_name()`
- `api_map()`, `api_map_delete()`

**Итого:** ~1000 строк

### 2.11 `db/*` модули

**`db/base.py`:**
- `BaseRepository.__init__(db_path)`, `_connect()` с WAL + FK
- `@retry_on_busy(max_retries=3, backoff=0.1)` декоратор

**`db/zones.py` (из database.py):**
- `get_zones()`, `get_zone()`, `create_zone()`, `update_zone()`, `delete_zone()`
- `bulk_update_zones()`, `bulk_upsert_zones()`
- `update_zone_versioned()`
- `get_zone_runs()`, `start_zone_run()`, `stop_zone_run()`

**`db/programs.py` (из database.py):**
- `get_programs()`, `create_program()`, `update_program()`, `delete_program()`
- `check_program_conflicts()`
- `get_program_cancellations()`, `add_cancellation()`

**`db/groups.py` (из database.py):**
- `get_groups()`, `create_group()`, `update_group()`, `delete_group()`

**`db/mqtt.py` (из database.py):**
- CRUD mqtt_servers
- `encrypt_secret()`/`decrypt_secret()` при записи/чтении password

**`db/settings.py` (из database.py):**
- `get_setting_value()`, `set_setting_value()`
- Rain/env/master config
- Password management

**`db/telegram.py` (из database.py):**
- bot_users, subscriptions, audit, FSM, idempotency

**`db/logs.py` (из database.py):**
- `get_logs()`, `add_log()`
- `get_water_usage()`, `get_water_statistics()`

**`db/migrations.py` (из database.py):**
- Все 25+ `_migrate_*` методов
- `_apply_named_migration()`, `_run_all_migrations()`

---

## 3. Порядок рефакторинга

### Принцип: каждый шаг — работающий коммит, тесты проходят.

```
Фаза 1: P0 Security (не ломает архитектуру)
  ├── Шаг 1.1: SECRET_KEY generation (config.py, docker-compose.yml)
  ├── Шаг 1.2: IRRIG_SECRET_KEY generation (utils.py)
  ├── Шаг 1.3: MQTT auth (mosquitto.conf, docker-compose.yml)
  ├── Шаг 1.4: Encrypt MQTT passwords (database.py + миграция)
  ├── Шаг 1.5: CSRF enable (config.py, templates, app.py JS)
  ├── Шаг 1.6: Guest → viewer (routes/auth.py, app.py)
  ├── Шаг 1.7: QoS 2 + retain (services/mqtt_pub.py, services/zone_control.py)
  └── Шаг 1.8: observed_state verification (services/observed_state.py, services/zone_control.py)

Фаза 2: P1 Reliability (минимальные архитектурные изменения)
  ├── Шаг 2.1: rate_limiter.py (новый файл + routes/auth.py)
  ├── Шаг 2.2: watchdog.py (новый файл + app.py integration)
  ├── Шаг 2.3: Update dependencies (requirements.txt + тестирование)
  ├── Шаг 2.4: Fix failing tests
  └── Шаг 2.5: Password policy (app.py, routes/auth.py)

Фаза 3: P2 Architecture (основной рефакторинг)
  ├── Шаг 3.1: Удалить дубли monitors из app.py
  ├── Шаг 3.2: Вынести SSE Hub → services/sse_hub.py
  ├── Шаг 3.3: Вынести app_init → services/app_init.py
  ├── Шаг 3.4: Вынести zones_api.py
  ├── Шаг 3.5: Вынести groups_api.py
  ├── Шаг 3.6: Вынести programs_api.py
  ├── Шаг 3.7: Вынести mqtt_api.py
  ├── Шаг 3.8: Вынести system_api.py
  ├── Шаг 3.9: Разбить database.py → db/*
  ├── Шаг 3.10: Fix catch-all exceptions (top-100)
  ├── Шаг 3.11: Fix api_water(), probe_env перезапись
  ├── Шаг 3.12: SQLite BUSY retry
  ├── Шаг 3.13: Non-root Docker + security headers
  └── Шаг 3.14: Resolve circular imports

Фаза 4: P3 Polish
  ├── Шаг 4.1: constants.py + magic numbers
  ├── Шаг 4.2: .env.example
  ├── Шаг 4.3: Delete legacy files
  ├── Шаг 4.4: VERSION file
  ├── Шаг 4.5: Python 3.11 + Dockerfile
  ├── Шаг 4.6: Type hints (основные модули)
  ├── Шаг 4.7: Dataclasses
  ├── Шаг 4.8: Graceful MQTT shutdown
  ├── Шаг 4.9: SQL format fix
  └── Шаг 4.10: CI/CD GitHub Actions
```

---

## 4. Зависимости между шагами

```
1.1 → 1.5 (SECRET_KEY нужен для CSRF tokens)
1.2 → 1.4 (IRRIG_SECRET_KEY нужен для шифрования MQTT паролей)
1.3 → 1.4 (MQTT auth нужен перед шифрованием паролей — чтобы знать credentials)
1.7 → 1.8 (QoS 2 нужен перед observed_state — для гарантии доставки)

2.3 → 2.4 (после обновления deps — fix тесты, т.к. могут быть новые failures)

3.1 → 3.2 → 3.3 (сначала убрать дубли, потом выносить hub и init)
3.3 → 3.4..3.8 (app_init нужен до выноса маршрутов — чтобы before_request не ломался)
3.4..3.8 — параллельно (маршруты независимы друг от друга)
3.9 — после 3.4..3.8 (database.py не менять пока маршруты ещё в app.py)
3.14 — после 3.4..3.9 (circular imports видны только после разделения)

4.5 → 4.6..4.7 (Python 3.11 type hints syntax)
```

---

## 5. Миграция данных

### 5.1 Шифрование MQTT-паролей

**Когда:** Шаг 1.4  
**Процесс:**
1. Добавить новую миграцию в database.py `_migrate_encrypt_mqtt_passwords()`
2. При запуске: `SELECT id, password FROM mqtt_servers WHERE password IS NOT NULL`
3. Для каждой записи: если password не начинается с marker зашифрованных данных → `encrypt_secret(password)`
4. `UPDATE mqtt_servers SET password=? WHERE id=?`
5. Логировать количество зашифрованных записей

**Marker:** `encrypt_secret()` возвращает base64 строку. Добавить prefix `ENC:` для различения:
```python
def encrypt_secret(plaintext: str) -> str:
    ...
    return 'ENC:' + base64.b64encode(ciphertext).decode()

def decrypt_secret(encrypted: str) -> str:
    if not encrypted.startswith('ENC:'):
        return encrypted  # legacy plaintext — совместимость
    raw = base64.b64decode(encrypted[4:])
    ...
```

### 5.2 Перегенерация ключа шифрования

**Когда:** Шаг 1.2  
**Процесс:**
1. Сохранить старый ключ (hostname-based) для расшифровки существующих данных
2. Генерировать новый случайный ключ → `.irrig_secret_key`
3. Перешифровать Telegram bot token: decrypt(old_key) → encrypt(new_key)
4. Логировать "Re-encrypted N secrets with new key"

**Важно:** Это разовая операция. Скрипт `migrations/reencrypt_secrets.py` с подтверждением.

### 5.3 Добавление полей fault в zones

**Когда:** Шаг 1.8  
**Миграция:**
```sql
ALTER TABLE zones ADD COLUMN last_fault TEXT;
ALTER TABLE zones ADD COLUMN fault_count INTEGER DEFAULT 0;
```

---

## 6. Разрешение циклических импортов

**Проблема (CR-006):**
- `irrigation_scheduler.py` → `from app import _publish_mqtt_value, dlog`
- `services/zone_control.py` → `from app import app as app_module`
- `app.py` → `from services.zone_control import ...`

**Решение:**
1. `_publish_mqtt_value` → перенести в `services/mqtt_pub.py` (уже есть `publish_mqtt_value`)
2. `dlog` → перенести в отдельный `services/debug_log.py` или использовать стандартный `logger.debug()`
3. `zone_control.py` проверяет `app.config.get('TESTING')` → передавать через параметр или env:
   ```python
   TESTING = os.environ.get('TESTING', '0') == '1'
   ```
4. После рефакторинга ни один модуль в `services/` или `routes/` не импортирует `app` напрямую

---

## 7. Стратегия тестирования при рефакторинге

1. **Перед каждым шагом:** `pytest tools/tests/tests_pytest/ --timeout=10 -x` — зафиксировать baseline
2. **После каждого шага:** тот же набор тестов — количество pass должно быть ≥ baseline
3. **Smoke-test на реальном окружении:** после каждой фазы деплоить на WB и проверить:
   - Логин работает
   - Зоны отображаются
   - Start/stop зоны через UI
   - SSE обновления приходят
   - MQTT подключение установлено
4. **Новые тесты:** каждый новый модуль (`services/rate_limiter.py`, `services/watchdog.py` и т.д.) должен иметь тест-файл
