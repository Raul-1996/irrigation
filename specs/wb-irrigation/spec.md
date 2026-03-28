# Спецификация доработки wb-irrigation

**Версия:** 1.0  
**Дата:** 2026-03-28  
**Основание:** Результаты тестирования (stage1), code review 4.2/10 (stage2), security audit CRITICAL (stage3)

---

## 1. Общие сведения

- **Проект:** wb-irrigation — управление поливом через MQTT реле Wirenboard
- **Стек:** Python 3.9 / Flask 2.3.3 + SQLite + paho-mqtt 2.1 + APScheduler + aiogram 3.4 + Hypercorn
- **Инфраструктура:** Docker (wb_irrigation_app + wb_irrigation_mqtt), контроллер Wirenboard 10.2.5.244
- **Зоны:** 24 зоны, 4 модуля WB-MR6C v3
- **Текущий рейтинг:** Code Quality 4.2/10, Security CRITICAL

---

## 2. Требования (P0 — Critical Security + Physical Safety)

### REQ-P0-001: MQTT QoS 2 + retain для команд реле

**Источник:** Требование заказчика + SEC-008 + CR-016  
**Текущее состояние:** `publish_mqtt_value()` в `services/mqtt_pub.py:91` имеет `qos: int = 0` по умолчанию. В `services/zone_control.py` вызовы ON (строки 98, 102) идут без qos/retain. Только OFF (строка 231) использует `retain=True`, но без qos.

**Требования:**
- Все команды ON/OFF зон → `qos=2, retain=True`
- Все команды master-valve ON/OFF → `qos=2, retain=True`
- Peer-off команды (строки 123, 146) → `qos=2, retain=True`
- Дефолт `publish_mqtt_value()` изменить: `qos=2, retain=True` для управляющих команд
- Meta-сообщения (строка 172 mqtt_pub.py) остаются `qos=0, retain=False`
- Добавить ожидание подтверждения publish (wait_for_publish с таймаутом 5 сек)
- При неудаче publish → retry 3 раза с exponential backoff → логирование CRITICAL → Telegram alert

### REQ-P0-002: Проверка observed_state — подтверждение переключения реле

**Источник:** Требование заказчика + PHYS-001  
**Текущее состояние:** SSE-хаб (app.py:3887) обновляет `observed_state` по MQTT-сообщениям, но нет логики проверки "команда отправлена → реле реально переключилось".

**Требования:**
- После publish ON/OFF ждать observed_state change через MQTT subscription (таймаут 10 сек)
- Если observed_state не совпадает с expected через 10 сек → retry publish (до 3 раз)
- Если после 3 retry observed_state не совпал → логировать CRITICAL, отправить Telegram alert, выставить зону в статус `fault`
- Добавить поле `last_fault` и `fault_count` в таблицу zones
- UI: показывать badge "Нет подтверждения" если observed_state ≠ commanded_state

### REQ-P0-003: Генерация случайного SECRET_KEY

**Источник:** SEC-002 + CR-008  
**Текущее состояние:** `config.py:9` — `SECRET_KEY = os.environ.get('SECRET_KEY', 'wb-irrigation-secret')`

**Требования:**
- При первом запуске генерировать `secrets.token_hex(32)`, сохранять в `.secret_key` файл
- Файл `.secret_key` с правами `0o600`
- Если env `SECRET_KEY` задан и ≠ 'wb-irrigation-secret' → использовать его
- Убрать дефолт `wb-irrigation-secret` из `docker-compose.yml:8`
- Добавить `.secret_key` в `.gitignore` и `.dockerignore`

### REQ-P0-004: Шифрование MQTT паролей в SQLite

**Источник:** SEC-003 + CR-007  
**Текущее состояние:** `database.py:1648-1675` — MQTT password хранится plaintext. Telegram token шифруется через `encrypt_secret()` из `utils.py`.

**Требования:**
- При записи MQTT-сервера: `password = encrypt_secret(password)` в `create_mqtt_server()` (database.py:1651) и `update_mqtt_server()` (database.py:1688)
- При чтении: `password = decrypt_secret(password)` в `get_mqtt_server()` и `get_mqtt_servers()`
- Миграция существующих данных: одноразовый скрипт + новая миграция в database.py
- Бэкапы (database.py:1793) автоматически содержат зашифрованные пароли

### REQ-P0-005: Генерация случайного IRRIG_SECRET_KEY

**Источник:** SEC-007  
**Текущее состояние:** `utils.py:25-33` — ключ шифрования из `os.uname().nodename`, предсказуем для Wirenboard.

**Требования:**
- При первом запуске генерировать `secrets.token_bytes(32)`, сохранять в `.irrig_secret_key`
- Файл с правами `0o600`
- Миграция: перешифровать существующие данные (Telegram token, затем MQTT-пароли после REQ-P0-004)
- Если env `IRRIG_SECRET_KEY` задан → использовать его (приоритет)

### REQ-P0-006: Включить CSRF защиту

**Источник:** SEC-004 + CR-009  
**Текущее состояние:** `config.py:11` — `WTF_CSRF_CHECK_DEFAULT = False`, 27 `@csrf.exempt` в app.py.

**Требования:**
- `WTF_CSRF_CHECK_DEFAULT = True`
- В `templates/base.html` добавить `<meta name="csrf-token" content="{{ csrf_token() }}">`
- Во всех JavaScript fetch-вызовах добавить заголовок `X-CSRFToken`
- Удалить все 27 `@csrf.exempt` из app.py (включая двойной на строке 1283)
- Для SSE-endpoints и Telegram webhook — оставить exempt (они не используют session cookies)

### REQ-P0-007: Ограничить гостевой доступ

**Источник:** SEC-005  
**Текущее состояние:** `routes/auth.py:12-14` — guest login без пароля, получает role='guest', может управлять зонами через `_is_status_action()`.

**Требования:**
- Переименовать role `guest` → `viewer`
- Viewer может только: GET `/api/zones`, GET `/api/status`, GET `/api/programs`, просмотр UI
- Viewer НЕ может: start/stop зон, emergency, MQTT, settings, backup, группы start/stop
- Изменить `_is_status_action()` (app.py:940-960 и 1070-1090) — проверять role

### REQ-P0-008: MQTT ACL — запретить анонимный доступ

**Источник:** SEC-001  
**Текущее состояние:** `mosquitto.conf:2` — `allow_anonymous true`, нет ACL.

**Требования:**
- `allow_anonymous false` в mosquitto.conf
- Создать password file: `mosquitto_passwd -c /mosquitto/config/passwd irrigation_app`
- ACL file: приложение может read/write `/devices/#`, readonly для остальных
- Username/password для wb-irrigation app → хранить зашифрованным (REQ-P0-004)
- Обновить `docker-compose.yml`: volume для passwd/acl файлов
- Документация: как добавить/изменить MQTT пользователей

---

## 3. Требования (P1 — Reliability + Critical Bugs)

### REQ-P1-001: Watchdog аварийного отключения

**Источник:** PHYS-001 + PHYS-002  
**Текущее состояние:** Software watchdog в `_start_single_zone_watchdog` (app.py:1233-1249) проверяет exclusive constraint. `schedule_zone_cap()` (irrigation_scheduler.py:618) ограничивает время работы.

**Требования:**
- Фоновый поток (не before_request): каждые 30 сек проверяет все зоны со state='on'
- Если зона ON дольше cap (по умолчанию 240 мин) без подтверждения → аварийное OFF
- Если observed_state не обновлялся >60 сек для зоны ON → пометить как `stale`, логировать WARNING
- Глобальный лимит одновременно включённых зон (настройка, дефолт=4)
- При превышении → emergency stop + Telegram alert
- Cron-задача на хосте Wirenboard: проверить доступность wb-irrigation каждые 5 мин, при недоступности → отключить все реле через mosquitto_pub

### REQ-P1-002: Rate-limiting по IP

**Источник:** SEC-006  
**Текущее состояние:** `routes/auth.py:24-29` — rate-limit через session cookie, обходится без cookies.

**Требования:**
- Создать `services/rate_limiter.py` с in-memory хранением по IP
- 5 попыток за 5 минут → lockout на 15 минут
- Логировать lockout на WARNING с IP
- Применить к `/api/login` POST

### REQ-P1-003: Обновить зависимости (15 CVE)

**Источник:** Security audit CVE table  
**Текущее состояние:** Flask 2.3.3, Pillow 10.0.1, requests 2.32.3, aiohttp 3.9.5 (через aiogram).

**Требования:**
- Flask ≥ 3.1.3 (CVE-2026-27205)
- Pillow ≥ 10.3.0 (CVE-2023-50447, CVE-2024-28219)
- requests ≥ 2.33.0 (CVE-2024-47081, CVE-2026-25645)
- aiogram ≥ 3.8.0 (тянет aiohttp ≥ 3.13.3, 10 CVE)
- Проверить совместимость Flask 3.x с Flask-WTF 1.2.1 и Flask-Sock 0.7.0
- Запустить тесты после обновления

### REQ-P1-004: Исправить баги из тестов (stage1)

**Источник:** Stage1 testing report

**Требования:**
- **Зависающие тесты:** Добавить таймаут в conftest.py для MQTT/SSE тестов (10 сек на тест)
- **Database ops failures:** Исправить 12+ failed тестов в `test_database_ops.py`
- **MQTT mock failures:** Исправить 3 failed теста в `test_mqtt_mock.py`
- **Telegram webhook 404:** Исправить test_telegram_bot.py (assert 404 == 200)
- **Utils API error format:** Исправить test_utils_config.py (DID NOT RAISE Exception)
- Conftest: убрать port 8080 conflict, использовать random port

### REQ-P1-005: Минимальная длина пароля ≥ 8 символов

**Источник:** SEC-009  
**Текущее состояние:** Дефолт '1234', минимум 4 символа (app.py:1307).

**Требования:**
- Минимум 8 символов для нового пароля
- Запретить '1234', '0000', 'password', '12345678' и другие очевидные пароли
- При первом запуске принудительно требовать смену пароля (уже есть, но усилить проверку)

---

## 4. Требования (P2 — Architecture + Code Quality)

### REQ-P2-001: Декомпозиция app.py (4411 строк → модули)

**Источник:** CR-001 + CR-003 + CR-005 + CR-006  
**Текущее состояние:** 4411 строк, 138 функций, 66 маршрутов в одном файле.

**Требования:** (подробный план в plan.md)
- Вынести маршруты зон → `routes/zones_api.py` (~800 строк)
- Вынести маршруты групп → `routes/groups_api.py` (~400 строк)
- Вынести маршруты программ → `routes/programs_api.py` (~200 строк)
- Вынести маршруты MQTT → `routes/mqtt_api.py` (~500 строк)
- Вынести системные маршруты → `routes/system_api.py` (~1000 строк)
- Вынести SSE Hub → `services/sse_hub.py` (~200 строк)
- Удалить дубли RainMonitor/EnvMonitor из app.py (строки 581-857), оставить только в `services/monitors.py`
- Перенести `_init_scheduler_before_request` → `create_app()` + `services/app_init.py`
- После рефакторинга app.py ≤ 500 строк

### REQ-P2-002: Декомпозиция database.py (2359 строк → модули)

**Источник:** CR-002  
**Текущее состояние:** Один класс `IrrigationDB` с ~60 методами.

**Требования:**
- `db/base.py` — BaseRepository с `_connect()`, WAL, foreign_keys
- `db/zones.py` — CRUD зон, zone_runs, bulk operations
- `db/programs.py` — CRUD программ, конфликты, cancellations
- `db/groups.py` — CRUD групп
- `db/mqtt.py` — CRUD mqtt_servers (с шифрованием паролей)
- `db/settings.py` — настройки, rain/env/master config, пароли
- `db/telegram.py` — bot_users, subscriptions, audit, FSM
- `db/logs.py` — логи, water_usage, water_statistics
- `db/migrations.py` — все `_migrate_*` методы
- `IrrigationDB` остаётся как фасад, делегирующий в репозитории

### REQ-P2-003: Замена catch-all except Exception (713 штук)

**Источник:** CR-004  
**Текущее состояние:** 713 `except Exception` + 1 bare `except:` = 714 catch-all блоков.

**Требования по категориям:**
- Import-time: `logger.warning("X not available: %s", e)`
- MQTT operations: `except (ConnectionError, TimeoutError, OSError)`
- SQLite operations: `except sqlite3.Error`
- JSON parsing: `except (json.JSONDecodeError, KeyError, TypeError)`
- Математика/парсинг: `except (ValueError, TypeError)`
- Фоновые потоки: оставить `except Exception` с `logger.exception()`
- Молчаливый pass (~180 шт): добавить минимум `logger.debug()`
- Исправить copy-paste сообщения (app.py:508, app.py:593)
- Заменить bare `except:` в app.py:2375 на `except (ValueError, TypeError):`

### REQ-P2-004: Удалить дублирование

**Источник:** CR-003, CR-010, CR-013

**Требования:**
- Удалить RainMonitor/EnvMonitor из app.py (строки 581-850), оставить в `services/monitors.py`
- Удалить хак "Rebind monitors" (app.py:852-857)
- Объединить два определения `_is_status_action()` (app.py:940-960 и 1070-1090) в одну функцию
- Удалить двойной `@csrf.exempt` (app.py:1283)

### REQ-P2-005: Route-заглушки → реальные blueprints

**Источник:** CR-012  
**Текущее состояние:** `routes/zones.py`, `routes/programs.py`, `routes/groups.py`, `routes/mqtt.py` — по 12 строк, только render_template.

**Требования:**
- Перенести API-маршруты из app.py в соответствующие blueprint-файлы
- Существующие page-рендеринг маршруты оставить
- Добавить API-маршруты с правильными декораторами auth

### REQ-P2-006: Исправить api_water() — убрать рандомные данные

**Источник:** CR-011  
**Текущее состояние:** app.py:2530-2580 — `/api/water` генерирует `random.randint(20, 80)`.

**Требования:**
- Использовать реальные данные из `water_usage` и `zone_runs`
- Если реальных данных нет → возвращать пустой массив, не рандом
- Пометить endpoint как beta, если данные неполные

### REQ-P2-007: SQLite BUSY retry

**Источник:** CR-015  
**Требования:**
- Создать декоратор `@retry_on_busy(max_retries=3, backoff=0.1)` в `db/base.py`
- Применить ко всем write-операциям
- Логировать retry на WARNING

### REQ-P2-008: Probe клиенты отдельно от publisher pool

**Источник:** CR-017  
**Текущее состояние:** `_probe_env_values()` (app.py:4200-4260) перезаписывает `_MQTT_CLIENTS[sid]`.

**Требования:**
- Probe-клиенты создавать с отдельным client_id и не кешировать в `_MQTT_CLIENTS`
- Disconnect probe-клиент после получения данных

### REQ-P2-009: Non-root в Docker

**Источник:** SEC-015

**Требования:**
- Добавить в Dockerfile: `RUN adduser --disabled-password appuser && chown -R appuser:appuser /app`
- `USER appuser`
- Проверить права на volumes (media, backups, irrigation.db)

### REQ-P2-010: Security headers + MAX_CONTENT_LENGTH

**Источник:** SEC-017, SEC-022

**Требования:**
- `app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024`
- `@app.after_request`: X-Content-Type-Options: nosniff, X-Frame-Options: SAMEORIGIN

---

## 5. Требования (P3 — Minor + Polish)

### REQ-P3-001: Типизация (type hints, dataclasses)

**Источник:** CR-025, CR-029  
- Type hints для всех public методов
- Dataclasses для Zone, Program, Group, MqttServer с `from_dict()`/`to_dict()`

### REQ-P3-002: Обновить Python 3.9 → 3.11+

**Источник:** CR-025, SEC-020  
- Изменить `Dockerfile:2` — `FROM python:3.11-slim`
- Проверить совместимость всех зависимостей

### REQ-P3-003: CI/CD (GitHub Actions)

**Источник:** CR-026  
- pytest с таймаутами
- ruff lint
- pip-audit для CVE

### REQ-P3-004: .env.example

**Источник:** CR-024  
- Документировать: SECRET_KEY, IRRIG_SECRET_KEY, WB_TZ, PORT, TESTING, SESSION_COOKIE_SECURE, SCHEDULER_LOG_LEVEL

### REQ-P3-005: Удалить legacy файлы

**Источник:** CR-018, CR-019, SEC-021  
- Удалить из корня: `zones.html`, `status.html`, `programs.html`, `logs.html`, `water.html`
- Перенести `*_REPORT.md` в `reports/`

### REQ-P3-006: Версия приложения из файла, не из git

**Источник:** CR-020  
- Создать `VERSION` файл при сборке Docker
- `_compute_app_version()` (app.py:217-225) читает файл, не вызывает `subprocess`

### REQ-P3-007: Магические числа → константы

**Источник:** CR-021  
- 240 мин (cap ручного полива) → `MAX_MANUAL_WATERING_MIN`
- 60 сек (задержка master-valve) → `MASTER_VALVE_CLOSE_DELAY_SEC`
- 5 сек (anti-re-start) → `ANTI_RESTART_WINDOW_SEC`
- 300 сек (TTL кеша MQTT) → `MQTT_CACHE_TTL_SEC`
- 4096 (dedup set) → `DEDUP_SET_MAX_SIZE`
- 0.8 сек (антидребезг группы) → `GROUP_DEBOUNCE_SEC`
- Все в `config.py` или отдельный `constants.py`

### REQ-P3-008: Graceful shutdown для MQTT-клиентов

**Источник:** CR-030  
- `atexit.register()` для `loop_stop()` + `disconnect()` всех клиентов в `_MQTT_CLIENTS`

### REQ-P3-009: SQL format injection fix

**Источник:** CR-022  
- `database.py:2160-2180`: заменить `.format(days)` на параметризованный запрос

---

## 6. Вне scope (не делаем сейчас)

- Переход на Alembic (CR-027) — текущие inline-миграции работают, при 25 миграциях ещё управляемо
- OpenAPI/Swagger (CR-028) — после стабилизации архитектуры
- Физическая кнопка аварийной остановки (PHYS-003) — требует аппаратных изменений
- TLS для MQTT (внутри LAN на Wirenboard — избыточно)
