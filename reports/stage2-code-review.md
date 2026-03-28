# Этап 2: Code Review wb-irrigation

**Дата:** 2026-03-28
**Ревьюер:** AI Code Reviewer (Claude Opus 4.6)
**Файлов проанализировано:** 64 .py + 15 .html (шаблоны) + конфигурация
**Строк Python:** ~17 400

## Общая оценка: 4.2/10

Проект — функционирующий прототип для управления поливом Wirenboard. Хорошая предметная экспертиза, грамотная работа с MQTT и SQLite. Однако архитектурно это **монолит, остановившийся на полпути к модуляризации**: 80% кода в двух файлах, blueprint-заглушки создают иллюзию модульности, дублирование классов между модулями, 713 catch-all except-блоков маскируют реальные ошибки. Без рефакторинга добавление новой фичи требует правки `app.py`, что всё ближе к точке, когда поддержка станет невозможной.

## Оценки по категориям

| Категория | Оценка | Вес | Комментарий |
|-----------|--------|-----|-------------|
| Архитектура | 3/10 | x3 | God Objects, циклические импорты, blueprint-заглушки |
| Качество кода | 4/10 | x2 | Дублирование, магические числа, мёртвый код |
| Error handling | 2/10 | x2 | 713 catch-all except Exception, ошибки проглатываются |
| Типизация | 4/10 | x1 | database.py имеет hints, app.py — частично, services — минимально |
| Логирование | 6/10 | x1 | PII-фильтр, ротация, раздельные логи — но уровни неадекватны |
| Документация | 3/10 | x1 | Нет README разработчика, docstrings фрагментарны |
| Тестируемость | 3/10 | x1 | Жёсткие зависимости, глобальные синглтоны, нет DI |
| **Итого (взвешенная)** | **4.2/10** | | |

---

## Критические замечания

### CR-001: God Object — app.py (4411 строк, 138 функций, 66 маршрутов)

- **Severity:** critical
- **Файл:** app.py:1-4411
- **Проблема:** Один файл содержит: все API-эндпоинты, SSE-хаб с MQTT-подписками, RainMonitor (250 строк), EnvMonitor (200 строк), image processing, scheduling init, safety watchdogs, auth middleware, boot-sync логику. Это нечитаемо, нетестируемо и любое изменение создаёт риск регрессии.
- **Пример:** Функция `_init_scheduler_before_request()` (строки 858-1020) — 160 строк в `@app.before_request`, инициализирующих scheduler, rain monitor, env monitor, water monitor, MQTT warm-up. Вызывается на **каждый HTTP-запрос**.
- **Рекомендация:** См. план декомпозиции ниже. Минимально — вынести маршруты в blueprints, мониторы в services.

### CR-002: God Database — database.py (2359 строк, ~60 методов)

- **Severity:** critical
- **Файл:** database.py:1-2359
- **Проблема:** Один класс `IrrigationDB` отвечает за: схему, миграции (25+), CRUD всех сущностей, настройки, бэкапы, Telegram-бота, water usage, конфликты программ. Класс невозможно тестировать по частям.
- **Пример:** `update_zone()` (строки 901-990) — 90 строк if-цепочки для формирования динамического SQL. Дублирование с `bulk_update_zones()` (строки 1000-1070) и `bulk_upsert_zones()` (строки 1080-1180).
- **Рекомендация:** Разделить на модули: `db/schema.py`, `db/zones.py`, `db/programs.py`, `db/mqtt.py`, `db/telegram.py`, `db/migrations/`.

### CR-003: Дублирование RainMonitor и EnvMonitor

- **Severity:** critical
- **Файл:** app.py:581-850 vs services/monitors.py:1-280
- **Проблема:** `RainMonitor` и `EnvMonitor` полностью реализованы дважды: в `app.py` (~270 строк) и в `services/monitors.py` (~280 строк). В `app.py` есть хак:
  ```python
  # Rebind monitors to consolidated implementations from services.monitors
  try:
      from services.monitors import rain_monitor as _svc_rain_monitor, env_monitor as _svc_env_monitor
      rain_monitor = _svc_rain_monitor
      env_monitor = _svc_env_monitor
  except Exception:
      pass
  ```
  При ошибке импорта используются версии из `app.py`, при успехе — из `services/`. Это неявное переключение реализации без механизма контроля.
- **Рекомендация:** Удалить реализации из `app.py`, оставить только в `services/monitors.py`. Заменить rebind на прямой import.

### CR-004: 713 catch-all `except Exception` во всём проекте

- **Severity:** critical
- **Файл:** app.py (356), database.py (124), services/ (137), irrigation_scheduler.py (96)
- **Проблема:** Почти все ошибки молча проглатываются. Отладка крайне сложна — при сбое нет stacktrace, нет информации о природе ошибки. Многие catch-all не логируют ничего (просто `pass`).
- **Пример:** app.py:17 — `import paho.mqtt.client` обёрнут в `except Exception: mqtt = None` — если paho установлен, но имеет ошибку синтаксиса/зависимости, это будет молча проигнорировано.
- **Рекомендация:** См. раздел "Error Handling: анализ паттернов".

### CR-005: Тяжёлая инициализация в `@app.before_request`

- **Severity:** critical
- **Файл:** app.py:858-1020 (`_init_scheduler_before_request`)
- **Проблема:** На **каждый** HTTP-запрос выполняется:
  1. Проверка инициализации планировщика
  2. Запуск watchdog-потока
  3. Проверка boot-sync (останов всех зон + MQTT publish)
  4. Проверка пароля / password_must_change
  5. Инициализация WaterMonitor
  6. Сверка конфигурации RainMonitor
  7. Сверка конфигурации EnvMonitor с подробным логированием
  8. Warm-up MQTT-клиентов
  
  Это добавляет ~50-200ms к каждому запросу на слабом железе Wirenboard, а EnvMonitor логирует статус check/decision на уровне INFO при каждом запросе.
- **Рекомендация:** Вынести инициализацию в `create_app()` или `@app.before_first_request`. Мониторы запускать один раз при старте. EnvMonitor check-логирование перевести на DEBUG.

### CR-006: Циклические импорты app ↔ services

- **Severity:** critical
- **Файл:** irrigation_scheduler.py:305, services/zone_control.py:258, services/monitors.py (implicit)
- **Проблема:** 
  - `irrigation_scheduler.py` импортирует `from app import _publish_mqtt_value as _pub` (строка ~360)
  - `irrigation_scheduler.py` импортирует `from app import dlog` (строка ~310)
  - `services/zone_control.py` импортирует `from app import app as app_module` для проверки TESTING
  - `app.py` импортирует `from services.zone_control import ...`
  
  Это порождает циклические зависимости, решаемые lazy-импортами внутри функций, что хрупко и нетестируемо.
- **Рекомендация:** Вынести `publish_mqtt_value` и `dlog` из `app.py` в отдельные модули (mqtt_pub уже существует). Передавать зависимости через DI или конфигурацию, а не через импорт `app`.

---

## Серьёзные замечания

### CR-007: MQTT пароли хранятся в открытом виде

- **Severity:** major
- **Файл:** database.py:334 (CREATE TABLE mqtt_servers), database.py:1630-1670 (create/update)
- **Проблема:** Поле `password TEXT` в таблице `mqtt_servers` хранится as-is. Telegram bot token шифруется через `encrypt_secret()`, но MQTT credentials — нет.
- **Рекомендация:** Использовать существующие `encrypt_secret()`/`decrypt_secret()` для MQTT-паролей.

### CR-008: Дефолтный SECRET_KEY

- **Severity:** major
- **Файл:** config.py:9
- **Проблема:** `SECRET_KEY = os.environ.get('SECRET_KEY', 'wb-irrigation-secret')` — предсказуемый ключ позволяет подделать Flask-сессии.
- **Рекомендация:** Генерировать случайный ключ при первом запуске, сохранять в файл. Отказываться запускаться без настоящего ключа в production.

### CR-009: CSRF фактически отключён

- **Severity:** major
- **Файл:** config.py:11, app.py (27 `@csrf.exempt`)
- **Проблема:** `WTF_CSRF_CHECK_DEFAULT = False` + 27 explicit exempts = CSRF-защита отсутствует для всего API.
- **Рекомендация:** Включить CSRF по умолчанию, пробросить CSRF-токен в JavaScript через meta-тег.

### CR-010: Дублирование `_is_status_action()` 

- **Severity:** major
- **Файл:** app.py:940-960 и app.py:1070-1090
- **Проблема:** Идентичная функция `_is_status_action()` определена дважды — в `_init_scheduler_before_request` и в `_require_admin_for_mutations`. Обе имеют одинаковую логику с одинаковыми дублирующимися путями.
- **Рекомендация:** Вынести в одну функцию-утилиту уровня модуля.

### CR-011: `api_water()` возвращает РАНДОМНЫЕ данные

- **Severity:** major  
- **Файл:** app.py:2530-2580
- **Проблема:** Эндпоинт `/api/water` генерирует случайные значения (`random.randint(20, 80)`) для каждого запроса. Пользователь видит «данные о воде», но это мусор.
- **Рекомендация:** Использовать реальные данные из `water_usage` и `zone_runs`, либо чётко пометить как заглушку и скрыть из UI.

### CR-012: Route-заглушки в routes/ — ложная модульность

- **Severity:** major
- **Файл:** routes/zones.py (12 строк), routes/programs.py (12 строк), routes/groups.py (12 строк), routes/mqtt.py (12 строк)
- **Проблема:** Blueprints зарегистрированы, но содержат только render_template вызов. Все API-маршруты остаются в `app.py`.
- **Пример:**
  ```python
  # routes/zones.py (полностью)
  from flask import Blueprint, render_template
  from services.security import admin_required
  zones_bp = Blueprint('zones_bp', __name__)
  @zones_bp.route('/zones')
  @admin_required
  def zones_page():
      return render_template('zones.html')
  ```
- **Рекомендация:** Перенести API-маршруты из app.py в соответствующие blueprints.

### CR-013: Двойной декоратор `@csrf.exempt`

- **Severity:** major
- **Файл:** app.py:1283
- **Проблема:** Маршрут `/api/password` имеет `@csrf.exempt` дважды подряд:
  ```python
  @csrf.exempt
  @csrf.exempt
  @app.route('/api/password', methods=['POST'])
  ```
- **Рекомендация:** Удалить дубль. Это симптом copy-paste разработки.

### CR-014: SQL-инъекция через column name (потенциальная)

- **Severity:** major
- **Файл:** database.py:747 (`set_bot_user_notif_toggle`)
- **Проблема:** 
  ```python
  conn.execute(f'UPDATE bot_users SET {col}=? WHERE chat_id=?', ...)
  ```
  `col` формируется из фиксированного `allowed` dict, но паттерн опасен: при рефакторинге кто-то может добавить пользовательский ввод.
- **Рекомендация:** Использовать whitelist-подход с assert: `assert col in allowed_columns`.

### CR-015: Нет retry-логики при SQLite BUSY

- **Severity:** major
- **Файл:** database.py (все методы)
- **Проблема:** `timeout=5` в `sqlite3.connect()` помогает, но при конкурентных записях (APScheduler threads + MQTT callbacks + Flask workers) возможны `SQLITE_BUSY`. Нет exponential backoff.
- **Рекомендация:** Добавить декоратор-ретраер для write-операций.

### CR-016: QoS 0 для критических MQTT-команд

- **Severity:** major
- **Файл:** services/mqtt_pub.py:85 (default qos=0)
- **Проблема:** Команды ON/OFF для реле публикуются с QoS 0 (fire-and-forget). Если сообщение потеряется, реле останется в неверном состоянии.
- **Рекомендация:** Для управляющих команд использовать минимум QoS 1.

### CR-017: `_probe_env_values()` перетирает глобальный кеш MQTT-клиентов

- **Severity:** major
- **Файл:** app.py:4200-4260
- **Проблема:** Функция `_probe_env_values()` создаёт MQTT-клиент и записывает его в `_MQTT_CLIENTS[sid]`, перетирая существующий publisher-клиент. При этом у нового клиента не настроен `on_message` для publish-целей. Это может сломать publish для этого server_id.
- **Рекомендация:** Probe-клиенты создавать отдельно и не кешировать в общий пул.

---

## Незначительные замечания

### CR-018: Дублирование HTML в корне проекта

- **Severity:** minor
- **Файл:** корень проекта: zones.html, status.html, programs.html, logs.html, water.html
- **Проблема:** 5 HTML-файлов дублируют (или являются legacy-версиями) файлов из `templates/`.
- **Рекомендация:** Удалить legacy-файлы из корня.

### CR-019: 7 отчётных файлов *_REPORT.md в корне

- **Severity:** minor
- **Файл:** *_REPORT.md, *_FIX_REPORT.md
- **Проблема:** Нетипичный формат ведения changelog.
- **Рекомендация:** Перенести в `reports/` или использовать git history.

### CR-020: `_compute_app_version()` вызывает `git rev-list` при импорте

- **Severity:** minor
- **Файл:** app.py:217-225
- **Проблема:** При каждом импорте `app.py` выполняется `subprocess.check_output(['git', 'rev-list', '--count', 'HEAD'])`. В Docker без git это будет фейлить молча.
- **Рекомендация:** Вычислять версию один раз при сборке и писать в файл.

### CR-021: Магические числа

- **Severity:** minor
- **Файл:** app.py, irrigation_scheduler.py
- **Примеры:**
  - `240` минут — cap для ручного полива (app.py:3230)
  - `60` секунд — задержка закрытия мастер-клапана (zone_control.py:200)
  - `5` секунд — окно анти-ре-старта (app.py:4280)
  - `300` секунд — TTL кеша MQTT-серверов (mqtt_pub.py:12)
  - `4096` — порог очистки dedup-set (events.py:27)
  - `0.8` секунды — антидребезг группы (app.py:1155)
- **Рекомендация:** Вынести в именованные константы или настройки.

### CR-022: `get_water_usage()` — SQL format string injection

- **Severity:** minor
- **Файл:** database.py:2160-2180
- **Проблема:** `.format(days)` в SQL-запросе. Хотя `days` приходит как int, паттерн опасен:
  ```python
  cursor = conn.execute('''
      SELECT ... WHERE w.timestamp >= datetime('now', '-{} days')
  '''.format(days), ...)
  ```
- **Рекомендация:** Использовать параметризованный запрос: `datetime('now', '-' || ? || ' days')`.

### CR-023: `bare except:` в api_logs фильтрации

- **Severity:** minor
- **Файл:** app.py:2375 
- **Проблема:** `except:` (bare) вместо `except Exception:` — ловит даже KeyboardInterrupt и SystemExit.
- **Рекомендация:** Заменить на `except (ValueError, TypeError):`.

### CR-024: Нет `.env.example`

- **Severity:** minor
- **Проблема:** Неясно какие env-переменные нужны (`SECRET_KEY`, `WB_TZ`, `IRRIG_SECRET_KEY`, `PORT`, `TESTING`, `SESSION_COOKIE_SECURE`, `SCHEDULER_LOG_LEVEL`).
- **Рекомендация:** Создать `.env.example` с комментариями.

---

## Предложения

### CR-025: Перевести на Python 3.11+

- **Severity:** suggestion
- **Проблема:** Dockerfile использует python:3.9-slim. Python 3.11+ даёт ~25% прирост производительности и лучшие error messages.

### CR-026: Добавить CI/CD

- **Severity:** suggestion
- **Проблема:** Нет Makefile, tox.ini, GitHub Actions. Тесты запускаются вручную.
- **Рекомендация:** GitHub Actions с pytest, ruff lint.

### CR-027: Миграции через Alembic

- **Severity:** suggestion
- **Проблема:** 25+ миграций как inline-методы в database.py. При росте схемы станет неуправляемо.
- **Рекомендация:** Alembic или аналог с файловыми миграциями.

### CR-028: OpenAPI/Swagger для API

- **Severity:** suggestion
- **Проблема:** 66 эндпоинтов без документации API.
- **Рекомендация:** flask-smorest или flasgger для автодокументации.

### CR-029: Dataclasses для Zone, Program, Group

- **Severity:** suggestion
- **Проблема:** Все сущности — raw dicts. Нет валидации типов, нет автодополнения в IDE.
- **Рекомендация:** `@dataclass` для Zone, Program, Group, MqttServer с from_dict/to_dict.

### CR-030: Graceful shutdown для MQTT-клиентов

- **Severity:** suggestion
- **Файл:** services/mqtt_pub.py
- **Проблема:** Клиенты кешируются с `loop_start()`, но нет `loop_stop()`/`disconnect()` при завершении. `atexit` не используется.
- **Рекомендация:** Добавить `atexit.register()` для корректного закрытия.

---

## Error Handling: анализ паттернов except Exception

**Всего:** 713 `except Exception` + 1 bare `except:` = **714 catch-all блоков**

### Паттерн 1: Молчаливый pass (самый опасный)
- **Встречается:** ~180 раз
- **Пример:** app.py:17 (`import paho.mqtt.client`), app.py:22 (`from services import events`), app.py:53 (Telegram subscribe)
- **Проблема:** Ошибка полностью проглатывается, нет ни лога, ни re-raise. При сбое модуля — непонятно почему фича не работает.
- **Рекомендация:** Минимум `logger.debug()` для всех, `logger.warning()` для важных. Для import-time — `logger.warning("module X not available: %s", e)`.

### Паттерн 2: Logger.exception в catch-all (хороший, но слишком широкий)
- **Встречается:** ~90 раз
- **Пример:** services/zone_control.py:110 (`logger.exception('exclusive_start_zone: mqtt on failed')`), irrigation_scheduler.py:280
- **Проблема:** Логируется stacktrace, но ловится Exception вместо конкретного. Маскирует programming errors (TypeError, ValueError).
- **Рекомендация:** Сузить до `(ConnectionError, TimeoutError, mqtt.MQTTException)` для MQTT, `sqlite3.Error` для БД.

### Паттерн 3: Logger.error без traceback
- **Встречается:** ~150 раз
- **Пример:** database.py:210 (`logger.error(f"Ошибка применения миграции {name}: {e}")`), app.py:1150
- **Проблема:** Логируется только `str(e)`, без stacktrace. Недостаточно для диагностики.
- **Рекомендация:** Использовать `logger.exception()` (добавляет traceback) или `logger.error(..., exc_info=True)`.

### Паттерн 4: Ошибка в except — обработчик с неправильным сообщением
- **Встречается:** 2 раза
- **Пример:** app.py:508 (`logger.exception('manual-start: db.update_zone failed')`) — это в `normalize_image()`, внутри `exif_transpose()`. Сообщение не соответствует контексту — скопировано из другого catch-блока.
- **Также:** app.py:593 (`logger.exception('manual-start: schedule_zone_stop failed')`) — в `RainMonitor.stop()`.
- **Рекомендация:** Исправить сообщения. Это явный copy-paste.

### Паттерн 5: Graceful degradation (правильное использование)
- **Встречается:** ~50 раз
- **Пример:** app.py:427 (`SEND_FILE_MAX_AGE_DEFAULT`), monitors.py:130 (RainMonitor.start fallback)
- **Комментарий:** Для опциональных фич (TLS setup, PWA cache, optional imports) catch-all оправдан. Но стоит логировать хотя бы на DEBUG.

### Паттерн 6: Двойной/тройной try-except вложенность
- **Встречается:** ~40 раз
- **Пример:** app.py:265-300 (`api_health_details`) — 4 уровня вложенных try/except, каждый с `except Exception: continue/pass`.
- **Проблема:** Код нечитаем, логика тонет в обработке ошибок. 
- **Рекомендация:** Вынести внутренние блоки в отдельные функции, ловить конкретные исключения.

### Сводная рекомендация по Error Handling

| Категория | Действие |
|-----------|----------|
| Import-time catch-all | `logger.warning("X not available", exc_info=True)` |
| MQTT operations | `except (ConnectionError, TimeoutError, OSError)` |
| SQLite operations | `except sqlite3.Error` |
| JSON parsing | `except (json.JSONDecodeError, KeyError, TypeError)` |
| Математика/парсинг | `except (ValueError, TypeError)` |
| Optional features | Оставить catch-all, но логировать на WARNING |
| Фоновые потоки | `except Exception` с `logger.exception()` — оправдано |

---

## План декомпозиции

### Фаза 1: app.py → модули (приоритет: ВЫСОКИЙ)

#### 1.1 Выделить мониторы из app.py

**Удалить из app.py (строки 581-850):**
- Класс `RainMonitor` (app.py:581-690)
- Класс `EnvMonitor` (app.py:692-850)
- Хак "Rebind monitors" (app.py:852-857)
- Функцию `_probe_env_values()` (app.py:4185-4260)

**Оставить только:**
```python
from services.monitors import rain_monitor, env_monitor, water_monitor
```

**Переместить** логику мониторинга из `_init_scheduler_before_request` в `services/monitor_manager.py` с методами `ensure_started()` и `check_config_changed()`.

#### 1.2 Вынести маршруты зон

**Создать:** `routes/zones_api.py`
**Перенести из app.py:**
- `api_zones()` — GET /api/zones
- `api_zone()` — GET/PUT/DELETE /api/zones/<id>
- `api_create_zone()` — POST /api/zones
- `api_import_zones_bulk()` — POST /api/zones/import
- `api_zone_next_watering()` — GET /api/zones/<id>/next-watering
- `api_zones_next_watering_bulk()` — POST /api/zones/next-watering-bulk
- `upload_zone_photo()`, `delete_zone_photo()`, `rotate_zone_photo()`, `get_zone_photo()` — фото-API
- `start_zone()`, `stop_zone()` — ручной старт/стоп
- `api_zone_watering_time()` — таймер
- `api_zone_mqtt_start()`, `api_zone_mqtt_stop()` — MQTT старт/стоп
- `api_check_zone_duration_conflicts()`, `api_check_zone_duration_conflicts_bulk()` — проверка конфликтов

**Итого:** ~800 строк из app.py → routes/zones_api.py

#### 1.3 Вынести маршруты программ

**Создать:** `routes/programs_api.py`
**Перенести из app.py:**
- `api_programs()` — GET /api/programs
- `api_program()` — GET/PUT/DELETE /api/programs/<id>
- `api_create_program()` — POST /api/programs
- `check_program_conflicts()` — POST /api/programs/check-conflicts

**Итого:** ~200 строк

#### 1.4 Вынести маршруты групп

**Создать:** `routes/groups_api.py`
**Перенести из app.py:**
- `api_groups()` — GET /api/groups
- `api_update_group()` — PUT /api/groups/<id>
- `api_create_group()` — POST /api/groups
- `api_delete_group()` — DELETE /api/groups/<id>
- `api_stop_group()` — POST /api/groups/<id>/stop
- `api_start_group_from_first()` — POST /api/groups/<id>/start-from-first
- `api_start_zone_exclusive()` — POST /api/groups/<id>/start-zone/<id>
- `api_master_valve_toggle()` — POST /api/groups/<id>/master-valve/<action>

**Итого:** ~400 строк

#### 1.5 Вынести маршруты MQTT

**Создать:** `routes/mqtt_api.py`
**Перенести из app.py:**
- `api_mqtt_servers_list/create/get/update/delete` — CRUD MQTT серверов
- `api_mqtt_probe()` — проба MQTT
- `api_mqtt_status()` — статус подключения
- `api_mqtt_scan_sse()` — SSE-скан
- `api_mqtt_zones_sse()` — SSE-хаб (крупнейший блок, ~200 строк)

**Итого:** ~500 строк

#### 1.6 Вынести общие маршруты

**Создать:** `routes/system_api.py`
**Перенести из app.py:**
- `api_status()` — /api/status (~200 строк!)
- `api_rain_config()` — /api/rain
- `api_env_config()`, `api_env_values()` — /api/env
- `api_postpone()` — /api/postpone
- `api_emergency_stop()`, `api_emergency_resume()` — аварийные
- `api_backup()` — бэкап
- `api_water()` — вода
- `api_server_time()` — время
- `api_scheduler_init/status/jobs()` — планировщик
- `api_health_details()`, `api_health_cancel_job/group()` — health
- `api_logging_debug_toggle()` — логирование
- `api_setting_early_off()`, `api_setting_system_name()` — настройки
- `api_map()`, `api_map_delete()` — карты

**Итого:** ~1000 строк

#### 1.7 Вынести SSE Hub

**Создать:** `services/sse_hub.py`
**Перенести из app.py:**
- Весь код SSE-хаба (`_ensure_hub_started`, `_rebuild_subscriptions`, глобальные `_SSE_HUB_*`)
- `_SSE_META_BUFFER`

**Итого:** ~200 строк

#### После фазы 1: app.py остаётся ~400-500 строк
- Flask app creation
- Config
- Blueprint registration
- Logging setup
- before_request middleware (урезанный)
- Error handlers
- Утилиты (compress_image, normalize_image)

### Фаза 2: database.py → модули (приоритет: СРЕДНИЙ)

#### 2.1 Выделить миграции
**Создать:** `db/migrations.py`
**Перенести:** все `_migrate_*` методы и `_apply_named_migration`

#### 2.2 Выделить CRUD по сущностям
- `db/zones.py` — get_zones, get_zone, create_zone, update_zone, delete_zone, bulk_*, update_zone_versioned, zone_runs
- `db/programs.py` — get_programs, create_program, update_program, delete_program, check_program_conflicts, cancellations
- `db/groups.py` — get_groups, create_group, update_group, delete_group, group fields
- `db/mqtt.py` — CRUD mqtt_servers
- `db/settings.py` — get/set_setting_value, rain/env/master config, password, early_off
- `db/telegram.py` — bot_users, bot_subscriptions, bot_audit, FSM, idempotency
- `db/logs.py` — get_logs, add_log, water_usage, water_statistics

#### 2.3 Общий базовый класс
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

### Фаза 3: Улучшение качества (приоритет: НИЗКИЙ)

1. Заменить catch-all на конкретные exceptions (итеративно, файл за файлом)
2. Добавить type hints ко всем public methods
3. Создать dataclasses для Zone, Program, Group, MqttServer
4. Добавить `.env.example`
5. Удалить мёртвый код (legacy HTML, неиспользуемые функции)
6. Перевести магические числа в конфиг/константы

### Порядок рефакторинга

| Шаг | Действие | Риск | Время |
|-----|----------|------|-------|
| 1 | Удалить дубли RainMonitor/EnvMonitor из app.py | Низкий | 1ч |
| 2 | Вынести `routes/zones_api.py` | Средний | 3ч |
| 3 | Вынести `routes/groups_api.py` | Средний | 2ч |
| 4 | Вынести `routes/programs_api.py` | Средний | 1ч |
| 5 | Вынести `routes/mqtt_api.py` + SSE Hub | Высокий | 4ч |
| 6 | Вынести `routes/system_api.py` | Средний | 3ч |
| 7 | Перенести before_request логику | Средний | 2ч |
| 8 | Разбить database.py | Средний | 4ч |
| 9 | Исправить error handling (top-50 критичных) | Низкий | 3ч |
| 10 | Type hints + dataclasses | Низкий | 4ч |

**Ключевое правило:** каждый шаг — отдельный коммит. После каждого шага — запуск тестов. Шаги 1-7 можно делать инкрементально, не ломая работающий код.

---

## Дополнительные наблюдения (не в аудит-отчёте)

### Архитектурный долг: "Scheduler в before_request"

Паттерн «инициализация тяжёлых ресурсов при первом HTTP-запросе» — вынужденная мера из-за ограничений Flask. Но текущая реализация проверяет ~8 условий при каждом запросе. Правильный подход: `create_app()` с `app.extensions['scheduler'] = ...`, а `before_request` — только для auth/session.

### Позитивные моменты (не повторяя аудит)

1. **Optimistic locking** в `update_zone_versioned()` — грамотная защита от race conditions
2. **Concurrent peer-off** в `exclusive_start_zone()` с ThreadPoolExecutor — хороший подход к снижению латентности
3. **Water meter integration** с zone_runs и снапшотами пульсов — продуманная бизнес-логика
4. **Anti-re-start window** (`_recently_stopped`) — защита от дребезга MQTT
5. **Delayed master valve close** (60 сек) — правильный подход для предотвращения гидроудара
6. **PII Masking** в логах — security best practice, редко встречается в embedded-проектах
