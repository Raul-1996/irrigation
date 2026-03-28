# Задачи доработки wb-irrigation

**Версия:** 1.0  
**Дата:** 2026-03-28  
**Основание:** spec.md + plan.md

---

## Фаза 1: P0 Security + Physical Safety

### TASK-001: Генерация случайного SECRET_KEY
- **Приоритет:** P0
- **Файлы:** `config.py`, `docker-compose.yml`, `.gitignore`
- **Описание:**
  1. В `config.py` создать функцию `_load_or_generate_secret()`:
     - Если env `SECRET_KEY` задан и ≠ 'wb-irrigation-secret' → использовать
     - Иначе → читать `.secret_key` файл
     - Если файла нет → `secrets.token_hex(32)`, записать в `.secret_key`, chmod 0o600
  2. `Config.SECRET_KEY = _load_or_generate_secret()`
  3. В `docker-compose.yml:8` убрать `:-wb-irrigation-secret` → оставить `${SECRET_KEY:-}`
  4. Добавить `.secret_key` в `.gitignore` и `.dockerignore`
- **Acceptance criteria:**
  - При первом запуске создаётся `.secret_key` с 64-символьным hex
  - При повторном запуске используется тот же ключ
  - `grep -r 'wb-irrigation-secret' config.py docker-compose.yml` → 0 результатов
  - Flask session cookies не воспроизводимы с известным ключом
- **Оценка:** 1 час
- **Зависимости:** нет

### TASK-002: Генерация случайного IRRIG_SECRET_KEY
- **Приоритет:** P0
- **Файлы:** `utils.py`, `.gitignore`
- **Описание:**
  1. В `utils.py:25-33` (`_get_secret_key()`) заменить hostname fallback:
     - Если env `IRRIG_SECRET_KEY` задан → использовать (base64 decode)
     - Иначе → читать `.irrig_secret_key` файл
     - Если файла нет → `secrets.token_bytes(32)`, записать, chmod 0o600
  2. Добавить `.irrig_secret_key` в `.gitignore`
  3. **Миграция:** создать скрипт `migrations/reencrypt_secrets.py`:
     - Вычислить старый ключ (из hostname)
     - Расшифровать все существующие секреты (Telegram token в bot_settings)
     - Зашифровать новым ключом
     - Обновить записи в БД
- **Acceptance criteria:**
  - `.irrig_secret_key` создаётся при первом запуске, 32 байта
  - Telegram bot token расшифровывается корректно с новым ключом
  - `os.uname().nodename` больше не используется для генерации ключа
- **Оценка:** 2 часа
- **Зависимости:** нет

### TASK-003: MQTT аутентификация (mosquitto)
- **Приоритет:** P0
- **Файлы:** `mosquitto.conf`, `docker-compose.yml`, новый `mosquitto/acl`, скрипт `mosquitto/setup_auth.sh`
- **Описание:**
  1. `mosquitto.conf`:
     ```
     listener 1883
     allow_anonymous false
     password_file /mosquitto/config/passwd
     acl_file /mosquitto/config/acl
     persistence true
     persistence_location /mosquitto/data/
     log_dest file /mosquitto/log/mosquitto.log
     ```
  2. Создать `mosquitto/acl`:
     ```
     user irrigation_app
     topic readwrite /devices/#
     topic readwrite #
     ```
  3. Создать `mosquitto/setup_auth.sh` — генерация passwd файла
  4. `docker-compose.yml`: добавить volume для `./mosquitto/passwd:/mosquitto/config/passwd:ro` и `./mosquitto/acl:/mosquitto/config/acl:ro`
  5. Обновить MQTT-серверы в БД: добавить username/password для подключения
- **Acceptance criteria:**
  - `mosquitto_sub -h localhost -p 1884` без credentials → Connection Refused
  - `mosquitto_sub -h localhost -p 1884 -u irrigation_app -P <pass>` → OK
  - Приложение подключается с credentials, SSE hub работает
- **Оценка:** 2 часа
- **Зависимости:** нет

### TASK-004: Шифрование MQTT паролей в SQLite
- **Приоритет:** P0
- **Файлы:** `database.py` (create_mqtt_server, update_mqtt_server, get_mqtt_server, get_mqtt_servers)
- **Описание:**
  1. Добавить prefix `ENC:` к `encrypt_secret()` output в `utils.py` для различения шифрованных/plain значений
  2. В `database.py:1651` (`create_mqtt_server`): `password = encrypt_secret(data.get('password'))` если password не пустой
  3. В `database.py:1688` (`update_mqtt_server`): аналогично
  4. В `get_mqtt_server()` и `get_mqtt_servers()`: `password = decrypt_secret(password)` если password начинается с `ENC:`
  5. Миграция `_migrate_encrypt_mqtt_passwords()`:
     - `SELECT id, password FROM mqtt_servers WHERE password IS NOT NULL AND password != ''`
     - Для каждого: если не начинается с `ENC:` → encrypt → update
  6. Добавить миграцию в `_run_all_migrations()`
- **Acceptance criteria:**
  - `sqlite3 irrigation.db "SELECT password FROM mqtt_servers"` → показывает `ENC:...`
  - Приложение подключается к MQTT → пароль расшифровывается корректно
  - Бэкап содержит зашифрованные пароли
- **Оценка:** 2 часа
- **Зависимости:** TASK-002 (нужен безопасный ключ шифрования)

### TASK-005: Включить CSRF защиту
- **Приоритет:** P0
- **Файлы:** `config.py`, `app.py`, `templates/base.html`, JavaScript файлы в `static/`
- **Описание:**
  1. `config.py:11`: `WTF_CSRF_CHECK_DEFAULT = True`
  2. В `templates/base.html` добавить: `<meta name="csrf-token" content="{{ csrf_token() }}">`
  3. В JavaScript (static/js/): добавить глобальный interceptor для fetch/XMLHttpRequest:
     ```javascript
     const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
     // Добавить X-CSRFToken header ко всем POST/PUT/DELETE
     ```
  4. Удалить ВСЕ 27 `@csrf.exempt` из app.py (найти: `grep -n 'csrf.exempt' app.py`)
  5. Удалить двойной `@csrf.exempt` на строке 1283
  6. Оставить `@csrf.exempt` только для:
     - Telegram webhook endpoint (не использует session cookies)
     - SSE endpoints (GET, не требуют CSRF)
- **Acceptance criteria:**
  - POST запрос без X-CSRFToken → 400 Bad Request
  - POST запрос с X-CSRFToken из meta → 200 OK
  - UI работает: login, start/stop zone, create program — всё с CSRF token
  - `grep -c 'csrf.exempt' app.py` ≤ 3 (только telegram webhook + SSE)
- **Оценка:** 3 часа
- **Зависимости:** TASK-001 (SECRET_KEY нужен для CSRF token generation)

### TASK-006: Ограничить гостевой доступ (guest → viewer)
- **Приоритет:** P0
- **Файлы:** `routes/auth.py`, `app.py` (функции `_is_status_action` × 2)
- **Описание:**
  1. `routes/auth.py:12-14`: при guest login выставлять `session['role'] = 'viewer'` вместо `'guest'`
  2. В app.py найти обе `_is_status_action()` (строки ~940-960 и ~1070-1090):
     - Объединить в одну функцию уровня модуля
     - Для role='viewer': разрешить только GET-запросы к `/api/*`
     - Запретить: start/stop зон, emergency, MQTT publish, settings change, backup
  3. В `_require_admin_for_mutations` (app.py:~1086): viewer → 403 Forbidden для всех POST/PUT/DELETE
- **Acceptance criteria:**
  - Login с `?guest=1` → session role = 'viewer'
  - Viewer: GET `/api/zones` → 200
  - Viewer: POST `/api/zones/1/mqtt/start` → 403
  - Viewer: POST `/api/emergency-stop` → 403
  - Admin: все операции → 200
- **Оценка:** 2 часа
- **Зависимости:** нет

### TASK-007: MQTT QoS 2 + retain для команд реле
- **Приоритет:** P0
- **Файлы:** `services/mqtt_pub.py`, `services/zone_control.py`
- **Описание:**
  1. `services/mqtt_pub.py:91`: изменить сигнатуру:
     ```python
     def publish_mqtt_value(..., retain: bool = False, qos: int = 0) -> bool:
     ```
     Оставить дефолт 0, но явно передавать qos=2 из zone_control.
  2. `services/mqtt_pub.py:129`: после `cl.publish()` добавить `res.wait_for_publish(timeout=5.0)`:
     ```python
     res = cl.publish(t, payload=value, qos=max(0, min(2, int(qos or 0))), retain=retain)
     if qos >= 1:
         res.wait_for_publish(timeout=5.0)
     ```
  3. При исключении в wait_for_publish → retry до 3 раз с backoff 1/2/4 сек
  4. При неудаче → `logger.critical("MQTT publish failed after 3 retries: %s", topic)` + Telegram alert
  5. `services/zone_control.py` — обновить ВСЕ вызовы `publish_mqtt_value`:
     - Строка 98 (master valve ON): добавить `qos=2, retain=True`
     - Строка 102 (zone ON): добавить `qos=2, retain=True`
     - Строка 123, 146 (peer OFF): добавить `qos=2, retain=True`
     - Строка 231 (zone OFF): добавить `qos=2` (retain=True уже есть)
     - Строка 263 (master valve OFF): добавить `qos=2` (retain=True уже есть)
- **Acceptance criteria:**
  - `mosquitto_sub -v -t '#'` → сообщения ON/OFF приходят с QoS 2
  - При потере связи с брокером → retry в логах, CRITICAL при полном сбое
  - Retained messages: `mosquitto_sub -t '/devices/+/controls/+/on' -v` при подключении показывает последние состояния
- **Оценка:** 2 часа
- **Зависимости:** нет

### TASK-008: Проверка observed_state после publish
- **Приоритет:** P0
- **Файлы:** `services/observed_state.py` (новый), `services/zone_control.py`, `database.py` (миграция)
- **Описание:**
  1. Создать `services/observed_state.py`:
     ```python
     class StateVerifier:
         def verify(self, zone_id: int, expected: str, timeout: float = 10.0, retries: int = 3) -> bool:
             """Подписывается на MQTT топик зоны, ждёт observed_state == expected.
             При неудаче: retry publish, при полном сбое: fault."""
     ```
  2. Подключаться к MQTT брокеру, subscribe на топик зоны, ждать сообщение
  3. При timeout → retry publish (через mqtt_pub), снова ждать
  4. После 3 неудач → update zone: `fault_count += 1`, `last_fault = datetime.now()`
  5. Отправить Telegram alert: "⚠️ Зона {name}: реле не подтвердило переключение в {expected}"
  6. Миграция в database.py:
     ```sql
     ALTER TABLE zones ADD COLUMN last_fault TEXT;
     ALTER TABLE zones ADD COLUMN fault_count INTEGER DEFAULT 0;
     ```
  7. Интегрировать в `zone_control.py`:
     - После `publish_mqtt_value(..., '1', ...)` (ON) → `verifier.verify(zone_id, 'on')`
     - После `publish_mqtt_value(..., '0', ...)` (OFF) → `verifier.verify(zone_id, 'off')`
     - Verification запускать в отдельном потоке (не блокировать HTTP)
- **Acceptance criteria:**
  - При успешном переключении → observed_state обновляется, fault_count не растёт
  - При отключённом реле (эмуляция) → через 30 сек fault_count += 1, Telegram alert отправлен
  - В UI видно зоны с fault_count > 0 (badge)
- **Оценка:** 3 часа
- **Зависимости:** TASK-007 (QoS 2 нужен для гарантии доставки)

---

## Фаза 2: P1 Reliability

### TASK-009: Rate-limiting по IP
- **Приоритет:** P1
- **Файлы:** `services/rate_limiter.py` (новый), `routes/auth.py`
- **Описание:**
  1. Создать `services/rate_limiter.py`:
     ```python
     class LoginRateLimiter:
         def __init__(self, max_attempts=5, window_sec=300, lockout_sec=900):
         def check(self, ip: str) -> tuple[bool, int]:
         def record_failure(self, ip: str):
         def reset(self, ip: str):
     ```
  2. Thread-safe через `threading.Lock`
  3. In-memory dict `{ip: [timestamps]}`
  4. В `routes/auth.py:24-29` заменить session-based проверку:
     ```python
     from services.rate_limiter import login_limiter
     ip = request.remote_addr or '0.0.0.0'
     allowed, retry_after = login_limiter.check(ip)
     if not allowed:
         return jsonify({'success': False, 'message': f'Заблокировано. Повторите через {retry_after}с'}), 429
     ```
  5. При успешном логине → `login_limiter.reset(ip)`
  6. При неудаче → `login_limiter.record_failure(ip)`
- **Acceptance criteria:**
  - 5 неудачных попыток с одного IP → 429 с retry_after
  - 6-я попытка без cookies → всё равно 429
  - Успешный логин → сброс счётчика
  - С другого IP → работает нормально
- **Оценка:** 1.5 часа
- **Зависимости:** нет

### TASK-010: Watchdog фоновый поток
- **Приоритет:** P1
- **Файлы:** `services/watchdog.py` (новый), `app.py` (интеграция запуска)
- **Описание:**
  1. Создать `services/watchdog.py`:
     ```python
     class ZoneWatchdog(threading.Thread):
         daemon = True
         interval = 30  # секунд
         max_concurrent_zones = 4  # настройка из settings
         
         def run(self):
             while True:
                 self._check_zones()
                 time.sleep(self.interval)
         
         def _check_zones(self):
             # 1. Получить все зоны со state='on'
             # 2. Проверить cap (240 мин по умолчанию)
             # 3. Проверить observed_state freshness (>60 сек = stale)
             # 4. Проверить concurrent count > max_concurrent_zones
             # 5. При аномалии → emergency_stop() + Telegram alert
     ```
  2. Запуск: в `create_app()` или `services/app_init.py` один раз при старте
  3. Глобальный лимит `max_concurrent_zones` — читать из settings (db)
- **Acceptance criteria:**
  - Watchdog запускается при старте приложения (лог "Watchdog started")
  - Если зона ON > cap → автоматическое OFF + лог CRITICAL
  - Если > max_concurrent_zones → emergency stop + Telegram alert
  - Watchdog не падает при ошибках БД (catch + log + continue)
- **Оценка:** 2 часа
- **Зависимости:** TASK-008 (observed_state нужен для проверки freshness)

### TASK-011: Обновить зависимости
- **Приоритет:** P1
- **Файлы:** `requirements.txt`, `Dockerfile` (если нужна сборка)
- **Описание:**
  1. Обновить `requirements.txt`:
     ```
     Flask>=3.1.3
     Pillow>=10.3.0
     APScheduler==3.10.4
     Flask-WTF>=1.2.1
     python-dotenv>=1.0.1
     paho-mqtt==2.1.0
     Flask-Sock>=0.7.0
     hypercorn>=0.14.4
     aiogram>=3.8.0
     pycryptodome>=3.21.0
     requests>=2.33.0
     ```
  2. Проверить совместимость Flask 3.x:
     - `before_first_request` удалён в Flask 2.3+ (уже не используется? проверить)
     - Werkzeug 3.x changes
  3. `pip install -r requirements.txt` в Docker build
  4. Запустить все тесты
  5. Деплой на staging и smoke-test
- **Acceptance criteria:**
  - `pip-audit -r requirements.txt` → 0 known vulnerabilities
  - Все существующие тесты проходят
  - Приложение стартует без ошибок
- **Оценка:** 2 часа
- **Зависимости:** нет

### TASK-012: Исправить failing тесты
- **Приоритет:** P1
- **Файлы:** `tools/tests/tests_pytest/test_database_ops.py`, `test_mqtt_mock.py`, `test_telegram_bot.py`, `test_utils_config.py`, `conftest.py`
- **Описание:**
  1. **test_database_ops.py** (12 failures): проанализировать каждый failed test, исправить assertions или fixture setup
  2. **test_mqtt_mock.py** (3 failures): исправить API validation expectations
  3. **test_telegram_bot.py** (1 failure): `assert 404 == 200` — исправить expected status code или endpoint URL
  4. **test_utils_config.py** (1 failure): `DID NOT RAISE Exception` — исправить тест или функцию
  5. **conftest.py**: использовать random port вместо hardcoded 8080, добавить `--timeout=10` в pytest.ini
  6. **Зависающие тесты**: добавить `@pytest.mark.timeout(10)` к MQTT/SSE тестам
- **Acceptance criteria:**
  - `pytest tools/tests/tests_pytest/ --timeout=10 -x` → 0 failures (допускаются skips)
  - Ни один тест не зависает дольше 10 секунд
- **Оценка:** 3 часа
- **Зависимости:** TASK-011 (после обновления deps — перезапуск тестов)

### TASK-013: Усилить парольную политику
- **Приоритет:** P1
- **Файлы:** `app.py` (endpoint `/api/password`), `routes/auth.py`
- **Описание:**
  1. В endpoint `/api/password` (app.py:~1283-1310):
     - Минимальная длина: 8 символов (сейчас 4)
     - Запретить список слабых паролей: `['1234', '12345678', '0000', 'password', 'admin', 'qwerty']`
     - Вернуть 400 с описанием ошибки
  2. При первом запуске: dефолтный пароль '1234' (database.py:181) → `secrets.token_urlsafe(12)`
  3. Записывать сгенерированный пароль в лог (один раз, уровень WARNING) для первого входа
- **Acceptance criteria:**
  - Смена пароля на '1234' → 400 "Пароль слишком простой"
  - Смена пароля на '12345678' → 400
  - Смена пароля на 'MyStr0ngP@ss' → 200
  - Первый запуск: в логах виден временный пароль
- **Оценка:** 1 час
- **Зависимости:** нет

---

## Фаза 3: P2 Architecture

### TASK-014: Удалить дубли monitors из app.py
- **Приоритет:** P2
- **Файлы:** `app.py` (строки 581-857), `services/monitors.py`
- **Описание:**
  1. Удалить из app.py:
     - Класс `RainMonitor` (строки ~581-690, ~110 строк)
     - Класс `EnvMonitor` (строки ~692-850, ~160 строк)
     - Хак "Rebind monitors" (строки ~852-857)
  2. Заменить на прямой import:
     ```python
     from services.monitors import rain_monitor, env_monitor, water_monitor
     ```
  3. Проверить что `services/monitors.py` содержит полную реализацию (сравнить с app.py версией)
  4. Удалить `_probe_env_values()` из app.py (строки ~4185-4260), перенести в `services/monitors.py`
- **Acceptance criteria:**
  - `grep -n 'class RainMonitor' app.py` → 0 результатов
  - `grep -n 'class EnvMonitor' app.py` → 0 результатов
  - Rain monitor работает (включить rain delay → зоны не запускаются)
  - Env monitor работает (проверить UI /api/env/values)
- **Оценка:** 2 часа
- **Зависимости:** нет

### TASK-015: Вынести SSE Hub → services/sse_hub.py
- **Приоритет:** P2
- **Файлы:** `services/sse_hub.py` (новый), `app.py` (строки ~3750-3920)
- **Описание:**
  1. Создать `services/sse_hub.py`:
     - Перенести все `_SSE_HUB_*` глобальные переменные
     - Перенести `_ensure_hub_started()`, `_rebuild_subscriptions()`
     - Перенести `_on_message` callback (app.py:3798-3899)
     - Перенести `_SSE_META_BUFFER`
  2. Экспортировать функции: `ensure_hub_started()`, `get_sse_stream()`, `get_meta_buffer()`
  3. SSE Hub не должен импортировать `app` — получать `db` через параметр
  4. observed_state update (строка 3887) оставить в hub, но через callback
- **Acceptance criteria:**
  - SSE endpoint `/api/mqtt/zones/sse` работает — UI обновляется при изменении MQTT
  - `grep -c '_SSE_HUB_' app.py` → 0
  - SSE scan endpoint работает
- **Оценка:** 3 часа
- **Зависимости:** TASK-014 (monitors убраны из app.py)

### TASK-016: Вынести app_init → services/app_init.py
- **Приоритет:** P2
- **Файлы:** `services/app_init.py` (новый), `app.py` (строки ~858-1020)
- **Описание:**
  1. Создать `services/app_init.py`:
     ```python
     def initialize_app(app, db):
         """Один раз при старте: boot-sync, scheduler, monitors, MQTT warm-up."""
     ```
  2. Перенести из `_init_scheduler_before_request()`:
     - Boot-sync (остановка всех зон + MQTT publish) → `boot_sync(db)`
     - Scheduler init → `init_scheduler(db)`
     - Monitor start → `start_monitors(db)`
     - MQTT warm-up → `warmup_mqtt_clients(db)`
  3. Вызывать `initialize_app()` в `create_app()` один раз
  4. `before_request` оставить минимальный: auth check + password_must_change
  5. Перенести EnvMonitor check-логирование на DEBUG уровень
- **Acceptance criteria:**
  - `_init_scheduler_before_request` удалён или содержит только auth логику
  - Приложение стартует, scheduler работает, мониторы запущены
  - Второй HTTP-запрос не инициализирует повторно (проверить логи: "init" один раз)
  - Latency per-request уменьшилась (нет 50-200ms overhead)
- **Оценка:** 3 часа
- **Зависимости:** TASK-014, TASK-015 (monitors и SSE уже вынесены)

### TASK-017: Вынести routes/zones_api.py
- **Приоритет:** P2
- **Файлы:** `routes/zones_api.py` (новый), `app.py`
- **Описание:**
  1. Создать `routes/zones_api.py` с Blueprint `zones_api_bp`
  2. Перенести из app.py (~800 строк):
     - `api_zones()` — GET /api/zones
     - `api_zone()` — GET/PUT/DELETE /api/zones/<id>
     - `api_create_zone()` — POST /api/zones
     - `api_import_zones_bulk()` — POST /api/zones/import
     - `api_zone_next_watering()`, `api_zones_next_watering_bulk()`
     - `upload_zone_photo()`, `delete_zone_photo()`, `rotate_zone_photo()`, `get_zone_photo()`
     - `start_zone()`, `stop_zone()`
     - `api_zone_watering_time()`
     - `api_zone_mqtt_start()`, `api_zone_mqtt_stop()`
     - `api_check_zone_duration_conflicts()`, `api_check_zone_duration_conflicts_bulk()`
  3. Зарегистрировать blueprint в `create_app()`
  4. Все helper-функции для зон перенести вместе
  5. Imports: db, mqtt_pub, zone_control — через текущие модули, БЕЗ `from app import`
- **Acceptance criteria:**
  - Все endpoints `/api/zones/*` работают (curl + UI)
  - Photo upload/delete работает
  - Start/stop zone через UI работает
  - `grep -c 'def api_zone' app.py` → 0
- **Оценка:** 3 часа
- **Зависимости:** TASK-016 (app_init вынесен, before_request упрощён)

### TASK-018: Вынести routes/groups_api.py
- **Приоритет:** P2
- **Файлы:** `routes/groups_api.py` (новый), `app.py`
- **Описание:**
  1. Создать `routes/groups_api.py` с Blueprint `groups_api_bp`
  2. Перенести из app.py (~400 строк):
     - `api_groups()`, `api_update_group()`, `api_create_group()`, `api_delete_group()`
     - `api_stop_group()`, `api_start_group_from_first()`, `api_start_zone_exclusive()`
     - `api_master_valve_toggle()`
  3. Зарегистрировать blueprint
- **Acceptance criteria:**
  - Все endpoints `/api/groups/*` работают
  - Master valve toggle работает
  - Start/stop group работает
- **Оценка:** 2 часа
- **Зависимости:** TASK-016

### TASK-019: Вынести routes/programs_api.py
- **Приоритет:** P2
- **Файлы:** `routes/programs_api.py` (новый), `app.py`
- **Описание:**
  1. Blueprint `programs_api_bp`
  2. Перенести (~200 строк): `api_programs()`, `api_program()`, `api_create_program()`, `check_program_conflicts()`
- **Acceptance criteria:**
  - CRUD программ работает через UI
  - Check conflicts работает
- **Оценка:** 1.5 часа
- **Зависимости:** TASK-016

### TASK-020: Вынести routes/mqtt_api.py
- **Приоритет:** P2
- **Файлы:** `routes/mqtt_api.py` (новый), `app.py`
- **Описание:**
  1. Blueprint `mqtt_api_bp`
  2. Перенести (~500 строк):
     - CRUD mqtt_servers
     - `api_mqtt_probe()`, `api_mqtt_status()`
     - `api_mqtt_scan_sse()` — использует `services/sse_hub.py`
     - `api_mqtt_zones_sse()` — главный SSE endpoint
  3. SSE endpoints: используют `sse_hub.ensure_hub_started()` и `sse_hub.get_sse_stream()`
- **Acceptance criteria:**
  - MQTT servers CRUD работает
  - SSE stream работает (UI обновляется в реальном времени)
  - MQTT probe работает
- **Оценка:** 3 часа
- **Зависимости:** TASK-015 (SSE Hub вынесен), TASK-016

### TASK-021: Вынести routes/system_api.py
- **Приоритет:** P2
- **Файлы:** `routes/system_api.py` (новый), `app.py`
- **Описание:**
  1. Blueprint `system_api_bp`
  2. Перенести (~1000 строк):
     - `api_status()` (~200 строк)
     - `api_rain_config()`, `api_env_config()`, `api_env_values()`
     - `api_postpone()`
     - `api_emergency_stop()`, `api_emergency_resume()`
     - `api_backup()`
     - `api_water()` (исправленный — без random)
     - `api_server_time()`
     - `api_scheduler_init/status/jobs()`
     - `api_health_details()`, `api_health_cancel_job/group()`
     - `api_logging_debug_toggle()`
     - `api_setting_early_off()`, `api_setting_system_name()`
     - `api_map()`, `api_map_delete()`
  3. При переносе `api_water()` — заменить `random.randint(20, 80)` на реальные данные из `db.get_water_usage()` или пустой массив
- **Acceptance criteria:**
  - Status page работает
  - Emergency stop/resume работает
  - Backup создаётся
  - Settings работают
  - `wc -l app.py` ≤ 500
- **Оценка:** 3 часа
- **Зависимости:** TASK-016, TASK-017-020 (после основных route-выносов)

### TASK-022: Разбить database.py → db/*
- **Приоритет:** P2
- **Файлы:** `db/` (новая директория), `database.py`
- **Описание:**
  1. Создать `db/base.py`:
     ```python
     class BaseRepository:
         def __init__(self, db_path: str):
             self.db_path = db_path
         def _connect(self):
             conn = sqlite3.connect(self.db_path, timeout=5)
             conn.execute('PRAGMA journal_mode=WAL')
             conn.execute('PRAGMA foreign_keys=ON')
             conn.row_factory = sqlite3.Row
             return conn
     ```
  2. Создать `db/zones.py` (ZoneRepository): get_zones, get_zone, create_zone, update_zone, delete_zone, bulk_update_zones, bulk_upsert_zones, update_zone_versioned, zone_runs
  3. Создать `db/programs.py` (ProgramRepository): CRUD + conflicts + cancellations
  4. Создать `db/groups.py` (GroupRepository): CRUD
  5. Создать `db/mqtt.py` (MqttRepository): CRUD + encrypt/decrypt
  6. Создать `db/settings.py` (SettingsRepository): get/set_setting_value, configs, password
  7. Создать `db/telegram.py` (TelegramRepository): bot_users, subscriptions, audit, FSM
  8. Создать `db/logs.py` (LogRepository): logs, water_usage, water_stats
  9. Создать `db/migrations.py`: все 25+ migrate методов
  10. `database.py`: `IrrigationDB` остаётся как фасад:
      ```python
      class IrrigationDB:
          def __init__(self, db_path):
              self.zones = ZoneRepository(db_path)
              self.programs = ProgramRepository(db_path)
              ...
          # Proxy methods для обратной совместимости:
          def get_zones(self, **kw): return self.zones.get_zones(**kw)
          ...
      ```
  11. Добавить `@retry_on_busy` декоратор для write-операций
- **Acceptance criteria:**
  - `wc -l database.py` ≤ 200 (фасад + proxy)
  - Все существующие вызовы `db.get_zones()` работают без изменений
  - Тесты проходят
  - Каждый db-модуль ≤ 400 строк
- **Оценка:** 4 часа
- **Зависимости:** TASK-017-021 (маршруты вынесены, database.py стабилен)

### TASK-023: Fix catch-all exceptions (top-100 критичных)
- **Приоритет:** P2
- **Файлы:** `app.py`, `services/zone_control.py`, `services/mqtt_pub.py`, `irrigation_scheduler.py`, `database.py`
- **Описание:**
  Заменить top-100 наиболее критичных `except Exception` на конкретные:
  1. **MQTT operations** (services/mqtt_pub.py, zone_control.py): `except (ConnectionError, TimeoutError, OSError, paho.mqtt.client.MQTTException)`
  2. **SQLite** (database.py, все модули): `except sqlite3.Error`
  3. **JSON** (app.py endpoints): `except (json.JSONDecodeError, KeyError, TypeError)`
  4. **Import-time** (app.py top-level): добавить `logger.warning("X not available: %s", e)`
  5. **Молчаливый pass** (top-50 по критичности): добавить `logger.debug()`
  6. **Copy-paste сообщения**: app.py:508 (normalize_image, не manual-start), app.py:593 (RainMonitor, не manual-start)
  7. **Bare except**: app.py:2375 → `except (ValueError, TypeError):`
  8. **Фоновые потоки**: оставить `except Exception` с `logger.exception()`
- **Acceptance criteria:**
  - `grep -c 'except Exception' app.py` уменьшилось на ≥100
  - Нет `except:` (bare) нигде в проекте
  - Все тесты проходят
  - Логи при ошибках содержат stacktrace (logger.exception)
- **Оценка:** 3 часа
- **Зависимости:** TASK-017-021 (после выноса маршрутов, чтобы менять меньше кода)

### TASK-024: Fix api_water() + probe_env перезапись
- **Приоритет:** P2
- **Файлы:** `routes/system_api.py` (или app.py), `app.py` (_probe_env_values)
- **Описание:**
  1. `api_water()`: заменить `random.randint(20, 80)` на реальные данные из `db.get_water_usage()` и `db.get_water_statistics()`. Если данных нет → пустой массив `[]`.
  2. `_probe_env_values()` (app.py:4200-4260): создавать probe-клиент с уникальным client_id `probe_{sid}_{uuid}`, НЕ сохранять в `_MQTT_CLIENTS`, disconnect после получения данных.
- **Acceptance criteria:**
  - `/api/water` → стабильные данные (два запроса → одинаковый результат при тех же данных)
  - Probe env values не ломает publisher для данного server_id
- **Оценка:** 1.5 часа
- **Зависимости:** TASK-021 (если api_water уже перенесён в system_api)

### TASK-025: SQLite BUSY retry decorator
- **Приоритет:** P2
- **Файлы:** `db/base.py`
- **Описание:**
  1. Создать декоратор:
     ```python
     def retry_on_busy(max_retries=3, initial_backoff=0.1):
         def decorator(func):
             @functools.wraps(func)
             def wrapper(*args, **kwargs):
                 for attempt in range(max_retries + 1):
                     try:
                         return func(*args, **kwargs)
                     except sqlite3.OperationalError as e:
                         if 'database is locked' in str(e) and attempt < max_retries:
                             time.sleep(initial_backoff * (2 ** attempt))
                             logger.warning("SQLite BUSY retry %d/%d for %s", attempt+1, max_retries, func.__name__)
                         else:
                             raise
             return wrapper
         return decorator
     ```
  2. Применить к write-методам в db/zones.py, db/programs.py и т.д.
- **Acceptance criteria:**
  - При concurrent writes → retry в логах, операция проходит
  - При 4+ consecutive BUSY → raise (не молчать)
- **Оценка:** 1 час
- **Зависимости:** TASK-022 (db/ модули созданы)

### TASK-026: Non-root Docker + security headers
- **Приоритет:** P2
- **Файлы:** `Dockerfile`, `app.py` (after_request)
- **Описание:**
  1. Dockerfile:
     ```dockerfile
     RUN adduser --disabled-password --gecos '' appuser
     RUN chown -R appuser:appuser /app
     USER appuser
     ```
  2. Проверить: volumes (media, backups, irrigation.db) доступны для appuser
  3. `app.py` (или `services/app_init.py`):
     ```python
     app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2MB
     
     @app.after_request
     def add_security_headers(resp):
         resp.headers['X-Content-Type-Options'] = 'nosniff'
         resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
         return resp
     ```
- **Acceptance criteria:**
  - `docker exec wb_irrigation_app whoami` → `appuser`
  - Photo upload работает (appuser может писать в media/)
  - Backup работает
  - Response headers содержат X-Content-Type-Options и X-Frame-Options
  - POST с body > 2MB → 413 Request Entity Too Large
- **Оценка:** 1.5 часа
- **Зависимости:** нет

### TASK-027: Resolve circular imports
- **Приоритет:** P2
- **Файлы:** `irrigation_scheduler.py`, `services/zone_control.py`, `services/mqtt_pub.py`
- **Описание:**
  1. `irrigation_scheduler.py`: заменить `from app import _publish_mqtt_value as _pub` → `from services.mqtt_pub import publish_mqtt_value`
  2. `irrigation_scheduler.py`: заменить `from app import dlog` → `import logging; logger = logging.getLogger(__name__); logger.debug()`
  3. `services/zone_control.py`: заменить `from app import app as app_module` для TESTING check → `TESTING = os.environ.get('TESTING', '0') == '1'`
  4. Проверить что ни один файл в `services/` или `routes/` не содержит `from app import` (кроме `from app import app` для blueprint registration, что допустимо через `current_app`)
- **Acceptance criteria:**
  - `grep -rn 'from app import' services/ irrigation_scheduler.py` → 0 результатов
  - Приложение стартует без ImportError
  - Тесты проходят
- **Оценка:** 2 часа
- **Зависимости:** TASK-017-021 (маршруты вынесены, _publish_mqtt_value не нужен в app.py)

---

## Фаза 4: P3 Polish

### TASK-028: constants.py + замена магических чисел
- **Приоритет:** P3
- **Файлы:** `constants.py` (новый), `app.py`, `irrigation_scheduler.py`, `services/zone_control.py`, `services/mqtt_pub.py`, `services/events.py`
- **Описание:**
  Создать `constants.py`:
  ```python
  MAX_MANUAL_WATERING_MIN = 240
  MASTER_VALVE_CLOSE_DELAY_SEC = 60
  ANTI_RESTART_WINDOW_SEC = 5
  MQTT_CACHE_TTL_SEC = 300
  DEDUP_SET_MAX_SIZE = 4096
  GROUP_DEBOUNCE_SEC = 0.8
  ZONE_CAP_DEFAULT_MIN = 240
  WATCHDOG_INTERVAL_SEC = 30
  OBSERVED_STATE_TIMEOUT_SEC = 10
  OBSERVED_STATE_MAX_RETRIES = 3
  MAX_CONCURRENT_ZONES = 4
  MIN_PASSWORD_LENGTH = 8
  LOGIN_MAX_ATTEMPTS = 5
  LOGIN_WINDOW_SEC = 300
  LOGIN_LOCKOUT_SEC = 900
  ```
  Заменить hardcoded числа во всех файлах на импорт из constants.
- **Acceptance criteria:**
  - `grep -rn '= 240' app.py irrigation_scheduler.py` → 0 (кроме комментариев)
  - Все тесты проходят
- **Оценка:** 2 часа
- **Зависимости:** TASK-017-021

### TASK-029: .env.example
- **Приоритет:** P3
- **Файлы:** `.env.example` (новый)
- **Описание:**
  ```env
  # Flask
  SECRET_KEY=           # Auto-generated if empty. Set for multi-instance deployments.
  
  # Encryption key for secrets in DB (Telegram token, MQTT passwords)
  IRRIG_SECRET_KEY=     # Auto-generated if empty. Base64-encoded 32 bytes.
  
  # Timezone
  WB_TZ=Asia/Yekaterinburg
  
  # Server
  PORT=8080
  TESTING=0
  
  # Session
  SESSION_COOKIE_SECURE=0  # Set to 1 if behind HTTPS reverse proxy
  
  # Logging
  SCHEDULER_LOG_LEVEL=INFO
  ```
- **Acceptance criteria:** Файл существует, все env-переменные документированы
- **Оценка:** 0.5 часа
- **Зависимости:** нет

### TASK-030: Удалить legacy файлы
- **Приоритет:** P3
- **Файлы:** корень проекта
- **Описание:**
  1. Удалить: `zones.html`, `status.html`, `programs.html`, `logs.html`, `water.html` из корня
  2. Перенести в `reports/`: `DYNAMIC_GROUPS_FIX_REPORT.md`, `FINAL_FIXES_REPORT.md`, `FIXES_REPORT.md`, `GROUP_CONFLICTS_FIX_REPORT.md`, `LATEST_FIXES_REPORT.md`, `PHOTO_FUNCTIONALITY_REPORT.md`, `SEQUENTIAL_DURATION_FIX_REPORT.md`, `WATERING_TIME_FIXES_REPORT.md`
  3. Удалить `mini_broker.out` если не нужен
- **Acceptance criteria:**
  - Нет .html файлов в корне (кроме templates/)
  - Нет *_REPORT.md в корне
  - `ls *.html` в корне → 0
- **Оценка:** 0.5 часа
- **Зависимости:** нет

### TASK-031: VERSION файл вместо git subprocess
- **Приоритет:** P3
- **Файлы:** `VERSION` (новый), `app.py` (функция `_compute_app_version()`), `Dockerfile`
- **Описание:**
  1. Создать файл `VERSION` с текущей версией (e.g., `1.0.0`)
  2. В `Dockerfile`: при сборке записать git commit count + hash:
     ```dockerfile
     RUN echo "$(git rev-list --count HEAD 2>/dev/null || echo 0).$(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')" > /app/VERSION
     ```
  3. `app.py:217-225` (`_compute_app_version()`): читать файл `VERSION`, не вызывать subprocess
- **Acceptance criteria:**
  - `cat VERSION` → версия
  - Нет `subprocess.check_output(['git'...])` в рантайме
  - Версия показывается в UI
- **Оценка:** 0.5 часа
- **Зависимости:** нет

### TASK-032: Python 3.11 + Dockerfile update
- **Приоритет:** P3
- **Файлы:** `Dockerfile`
- **Описание:**
  1. `FROM python:3.11-slim` (вместо python:3.9-slim)
  2. Обновить requirements если нужно
  3. Тестирование совместимости
- **Acceptance criteria:**
  - `python --version` в контейнере → 3.11.x
  - Все тесты проходят
  - Приложение работает
- **Оценка:** 1 час
- **Зависимости:** TASK-011 (deps обновлены)

### TASK-033: Type hints для основных модулей
- **Приоритет:** P3
- **Файлы:** `services/zone_control.py`, `services/mqtt_pub.py`, `services/monitors.py`, `db/*.py`
- **Описание:**
  Добавить type hints ко всем public методам:
  ```python
  def exclusive_start_zone(zone_id: int) -> bool: ...
  def publish_mqtt_value(server: dict, topic: str, value: str, ...) -> bool: ...
  def get_zones(self, group_id: Optional[int] = None) -> list[dict]: ...
  ```
- **Acceptance criteria:**
  - `mypy services/ db/ --ignore-missing-imports` → 0 errors (или minimal)
  - Все тесты проходят
- **Оценка:** 3 часа
- **Зависимости:** TASK-022 (db/ модули)

### TASK-034: Dataclasses для Zone, Program, Group, MqttServer
- **Приоритет:** P3
- **Файлы:** `models.py` (новый)
- **Описание:**
  ```python
  @dataclass
  class Zone:
      id: int
      name: str
      group_id: int
      mqtt_topic: str
      state: str = 'off'
      observed_state: str = ''
      fault_count: int = 0
      ...
      
      @classmethod
      def from_dict(cls, d: dict) -> 'Zone': ...
      def to_dict(self) -> dict: ...
  ```
  Аналогично для Program, Group, MqttServer.
- **Acceptance criteria:**
  - Dataclasses создаются из dict и конвертируются обратно
  - Используются в db/ модулях для возвращаемых значений
- **Оценка:** 2 часа
- **Зависимости:** TASK-033

### TASK-035: Graceful MQTT shutdown
- **Приоритет:** P3
- **Файлы:** `services/mqtt_pub.py`
- **Описание:**
  1. Добавить `atexit.register(_shutdown_mqtt_clients)`
  2. В `_shutdown_mqtt_clients()`:
     ```python
     for sid, cl in _MQTT_CLIENTS.items():
         try:
             cl.loop_stop()
             cl.disconnect()
         except Exception:
             pass
     _MQTT_CLIENTS.clear()
     ```
- **Acceptance criteria:**
  - При graceful shutdown → логи "MQTT client disconnected for server X"
  - Нет zombie MQTT connections на брокере после рестарта
- **Оценка:** 0.5 часа
- **Зависимости:** нет

### TASK-036: SQL format injection fix
- **Приоритет:** P3
- **Файлы:** `database.py` (или `db/logs.py` после TASK-022)
- **Описание:**
  `database.py:2160-2180` (`get_water_usage`): заменить `.format(days)` на параметризованный запрос:
  ```python
  # Было:
  WHERE w.timestamp >= datetime('now', '-{} days'.format(days))
  # Стало:
  WHERE w.timestamp >= datetime('now', '-' || CAST(? AS TEXT) || ' days')
  # Параметр: (days,)
  ```
- **Acceptance criteria:**
  - `.format(` не используется в SQL-запросах (grep)
  - `/api/water` возвращает те же данные что и до фикса
- **Оценка:** 0.5 часа
- **Зависимости:** нет

### TASK-037: CI/CD GitHub Actions
- **Приоритет:** P3
- **Файлы:** `.github/workflows/ci.yml` (новый)
- **Описание:**
  ```yaml
  name: CI
  on: [push, pull_request]
  jobs:
    test:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
        - uses: actions/setup-python@v5
          with:
            python-version: '3.11'
        - run: pip install -r requirements.txt -r requirements-dev.txt
        - run: pytest tools/tests/tests_pytest/ --timeout=10 -x
    lint:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
        - run: pip install ruff
        - run: ruff check .
    audit:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
        - run: pip install pip-audit
        - run: pip-audit -r requirements.txt
  ```
- **Acceptance criteria:**
  - Push → GitHub Actions запускает tests + lint + audit
  - Badge в README.md
- **Оценка:** 1 час
- **Зависимости:** TASK-012 (тесты проходят), TASK-011 (deps обновлены)

---

## Сводка

| Фаза | Задач | Часов | Фокус |
|------|-------|-------|-------|
| 1: P0 Security | 8 | 17 | MQTT QoS 2, encryption, CSRF, auth |
| 2: P1 Reliability | 5 | 9.5 | Rate-limit, watchdog, deps, tests |
| 3: P2 Architecture | 14 | 29 | Декомпозиция app.py + database.py |
| 4: P3 Polish | 10 | 11.5 | Types, CI, cleanup |
| **Итого** | **37** | **67** | |

### Критический путь
```
TASK-001 → TASK-005 (SECRET_KEY → CSRF)
TASK-002 → TASK-004 (IRRIG_KEY → encrypt MQTT)
TASK-007 → TASK-008 (QoS 2 → observed_state)
TASK-014 → TASK-015 → TASK-016 → TASK-017..021 → TASK-022 (архитектурная цепочка)
```
