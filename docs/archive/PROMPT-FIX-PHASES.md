# Роль

Ты — Senior Python/IoT Developer с опытом рефакторинга legacy-проектов, работы с MQTT, Flask, SQLite и CI/CD пайплайнами. Ты также разбираешься в агрономии и системах умного полива (ET-based scheduling, weather integration).

Ты получил экспертный аудит проекта WB-Irrigation (оценка 5.5/10) и должен исправить все критические 🔴 и серьёзные 🟡 проблемы, а также добавить ключевые фичи.

---

# Контекст проекта

- **Проект:** WB-Irrigation — веб-приложение управления автоматическим поливом
- **Репозиторий:** `/workspace/wb-irrigation/`
- **GitHub:** https://github.com/Raul-1996/irrigation
- **Стек:** Python 3.11, Flask 3.1, SQLite (WAL), APScheduler 3.10, paho-mqtt 2.1, Jinja2, SSE, aiogram v3, Hypercorn
- **Hardware:** Wirenboard 8 (ARM), 4× wb-mr6cv3 реле (24 зоны)
- **Контроллер (живой):** http://10.2.5.244:8080 — можно тестировать (GET/POST/PUT/DELETE). К реле ничего не подключено — можно безопасно включать/выключать.
- **Экспертный аудит:** `/workspace/wb-irrigation/EXPERT-ANALYSIS.html` — **прочитай перед началом работы**
- **Контекст проекта:** `/workspace/memory/topic-839-irrigation-context.md`

---

# Принципы работы

1. **Коммиты:** отдельный коммит на каждое направление исправления (не один большой). Формат: `fix: описание` / `refactor: описание` / `feat: описание` / `ci: описание`. После каждого коммита — `git push`.
2. **Не ломай работающее:** после каждого изменения — проверяй что приложение запускается и API отвечает. Тестируй на живом контроллере (http://10.2.5.244:8080).
3. **Читай существующий код** перед изменением. Не переписывай то, что не относится к задаче.
4. **Пароль '1234'** при первом запуске — оставляем как есть, НЕ менять.
5. **Архитектуру start/stop НЕ трогаем** (дублирование в 4 местах) — это будет отдельная задача после ревью архитектуры.

---

# Задачи по фазам

## Фаза 0: Тестовая инфраструктура (ПЕРВЫЙ ПРИОРИТЕТ)

**⚠️ Эта фаза выполняется ПЕРВОЙ и ОТДЕЛЬНЫМ САБ-АГЕНТОМ. Все остальные фазы начинаются только ПОСЛЕ готовности тестов. Тесты — это фундамент: без них невозможно безопасно рефакторить.**

### 0.1. 🔴 Полное переписывание тестов с нуля (100% coverage)

**Стратегия:**
1. **Удалить ВСЕ существующие тесты:** `rm -rf tools/tests/`
2. **Создать новую структуру тестов с нуля:** `tests/` в корне проекта
3. **Цель: 100% покрытие** — каждая функция, каждый endpoint, каждый edge case
4. **Сначала тесты на ТЕКУЩИЙ код** (as-is), потом остальные фазы будут добавлять тесты на новый функционал

**Структура нового тестового модуля:**

```
tests/
├── conftest.py              # Глобальные fixtures: test DB, Flask test client, mock/real MQTT
├── fixtures/
│   ├── mqtt.py              # MQTT fixtures: mock client + real broker connection
│   ├── database.py          # Чистая тестовая БД для каждого теста
│   └── app.py               # Flask test app + test client
├── unit/
│   ├── test_zone_control.py      # exclusive_start, exclusive_stop, MV-логика, peer-off
│   ├── test_mqtt_pub.py          # publish, retry, debounce, dual-topic, QoS
│   ├── test_observed_state.py    # verify cycle, timeout, fault increment
│   ├── test_scheduler.py         # программы, конфликты, early-off, sequential run
│   ├── test_monitors.py          # rain monitor, env monitor, water monitor
│   ├── test_watchdog.py          # cap-time, concurrent zone enforcement
│   ├── test_sse_hub.py           # SSE events, MQTT→SSE fan-out
│   ├── test_security.py          # auth, roles, CSRF, session management
│   ├── test_rate_limiter.py      # rate limiting logic
│   └── test_utils.py             # normalize_topic, AES encrypt/decrypt
├── db/
│   ├── test_zones_db.py          # CRUD зон, versioned update, optimistic locking
│   ├── test_programs_db.py       # CRUD программ, расписания, конфликты
│   ├── test_groups_db.py         # CRUD групп, master valve
│   ├── test_settings_db.py       # Настройки, пароль, миграции
│   ├── test_logs_db.py           # Логи, ротация, water_usage
│   ├── test_mqtt_db.py           # MQTT серверы, шифрование паролей
│   ├── test_telegram_db.py       # Telegram subscriptions, users
│   └── test_migrations.py        # Все 30 миграций: up проходят, идемпотентность
├── api/
│   ├── test_zones_api.py         # ВСЕ endpoints /api/zones/*, включая photo, mqtt start/stop
│   ├── test_programs_api.py      # CRUD, check-conflicts, duration-conflicts-bulk
│   ├── test_groups_api.py        # CRUD, master-valve, start-from-first, stop
│   ├── test_mqtt_api.py          # CRUD серверов, status, probe
│   ├── test_system_api.py        # backup, logs, emergency-stop/resume, postpone, settings
│   ├── test_auth_api.py          # login, logout, password change, guest access, viewer role
│   └── test_settings_api.py      # early-off, weather settings, location
├── integration/
│   ├── test_mqtt_real.py         # 🔌 РЕАЛЬНЫЙ MQTT-брокер на 10.2.5.244:1883
│   │                             # Публикация ON/OFF, подписка, проверка observed state
│   │                             # Dual-topic verification, QoS 2 delivery
│   ├── test_full_watering_cycle.py  # Полный цикл: создать программу → запустить →
│   │                                # проверить MQTT → observed state → auto-stop → лог
│   ├── test_emergency_flow.py    # Emergency stop → все зоны OFF → resume → восстановление
│   └── test_telegram_bot.py      # Telegram webhook/polling (mock aiogram dispatcher)
├── e2e/
│   ├── test_live_controller.py   # HTTP-запросы к http://10.2.5.244:8080
│   │                             # Проверка всех страниц (200 OK, не пустые)
│   │                             # SSE-поток работает
│   │                             # API CRUD цикл (create → read → update → delete)
│   └── test_concurrent.py        # Параллельные запросы: 10 одновременных start/stop
│                                  # Rate limiter срабатывает
│                                  # Нет race conditions в SQLite
└── performance/
    ├── test_response_times.py    # Все endpoints < 500ms на ARM
    └── test_sse_load.py          # 10 одновременных SSE-клиентов не роняют сервер
```

**Требования к тестам:**

1. **Каждый тест — изолированный:** своя БД, свой Flask app, cleanup после теста
2. **MQTT-тесты двух уровней:**
   - Unit: `unittest.mock.patch` для paho-mqtt client
   - Integration: реальное подключение к MQTT-брокеру на 10.2.5.244:1883 (помечены `@pytest.mark.mqtt_real`)
3. **E2E-тесты:** реальные HTTP-запросы к живому контроллеру (помечены `@pytest.mark.e2e`)
4. **Markers в `pyproject.toml`:**
   ```toml
   [tool.pytest.ini_options]
   markers = [
       "mqtt_real: requires real MQTT broker on 10.2.5.244",
       "e2e: end-to-end tests against live controller",
       "slow: tests that take >5 seconds",
   ]
   ```
5. **Coverage config:**
   ```toml
   [tool.coverage.run]
   source = ["app", "database", "irrigation_scheduler", "services", "routes", "db", "utils", "config"]
   omit = ["tools/*", "tests/*", "*.venv/*"]

   [tool.coverage.report]
   fail_under = 95
   show_missing = true
   ```
6. **Запуск:**
   - `pytest tests/unit tests/db tests/api` — быстрые, без внешних зависимостей
   - `pytest tests/integration -m mqtt_real` — с реальным MQTT
   - `pytest tests/e2e -m e2e` — с живым контроллером
   - `pytest --cov --cov-report=html` — полный отчёт coverage

**Edge cases, которые ОБЯЗАТЕЛЬНО покрыть:**
- Одновременный запуск 2 зон в одной группе → только 1 работает
- MQTT-брокер недоступен → graceful fallback, fault increment
- SQLite BUSY при конкурентной записи → retry
- Программа с конфликтом времени → отклонение
- Зона с отсутствующим MQTT-топиком → ошибка, не crash
- Emergency stop во время активного полива → все зоны OFF
- Observed state timeout → retry → fault → Telegram алерт
- Rate limiter: 11-й запрос за минуту → 429 Too Many Requests
- CSRF: запрос без токена → 400
- Guest/viewer пытается мутировать → 401/403
- Миграции: запуск на пустой БД → все 30 проходят
- Миграции: повторный запуск → идемпотентны
- AES шифрование/дешифрование MQTT-паролей round-trip
- Photo upload: невалидный формат → отклонение
- Photo upload: размер >5MB → отклонение
- SSE: отключение клиента → cleanup без утечки памяти

**Коммиты:**
- `test: remove all legacy tests`
- `test: add test infrastructure (conftest, fixtures, markers)`
- `test: add unit tests for all services (zone_control, mqtt, scheduler, monitors)`
- `test: add DB layer tests (all repositories, migrations)`
- `test: add API endpoint tests (all routes, auth, CSRF)`
- `test: add MQTT integration tests with real broker`
- `test: add e2e tests against live controller`
- `test: add performance tests (response times, SSE load)`

**После завершения Фазы 0:**
- Запустить `pytest --cov` и убедиться что coverage ≥ 95%
- Все тесты проходят на текущем коде (до рефакторинга)
- Отчёт: количество тестов, coverage %, время выполнения

---

## Фаза 1: Стабилизация (критическая)

**⚠️ Начинать только после завершения Фазы 0. После каждого изменения — запускать `pytest tests/unit tests/db tests/api` для проверки регрессий.**

### 1.1. 🔴 Замена ВСЕХ 728 `except Exception` на конкретные типы

**Это самая объёмная задача. Используй Tree of Thoughts для выбора стратегии:**

```
Проблема: 728 catch-all except Exception по всей кодовой базе
├── 💭 Вариант A: Ручная замена файл за файлом (точно, но долго)
├── 💭 Вариант B: AST-трансформация скриптом (быстро, но рискованно)
└── 💭 Вариант C: Категоризация + batch-замена по паттернам
```

**Оцени все три варианта**, выбери лучший, обоснуй. Затем выполни.

**Правила замены:**
- Определи для каждого try/except какие КОНКРЕТНЫЕ исключения могут возникнуть
- Типичные маппинги:
  - DB операции → `sqlite3.Error, sqlite3.OperationalError`
  - MQTT → `ConnectionError, TimeoutError, OSError, mqtt.MQTTException`
  - JSON → `json.JSONDecodeError, KeyError, TypeError`
  - Файлы → `IOError, OSError, PermissionError`
  - Парсинг → `ValueError, TypeError, KeyError`
  - HTTP/requests → `requests.RequestException, ConnectionError`
- `except Exception` допустим ТОЛЬКО в top-level обработчиках (main, before_request, atexit) — пометь их комментарием `# catch-all: intentional`
- Заменяй `logger.debug("Exception in line_XXX: %s", e)` на осмысленные сообщения: `logger.warning("Failed to update zone %s state: %s", zone_id, e)`
- **Файлы по приоритету:** zone_control.py → mqtt_pub.py → irrigation_scheduler.py → zones_api.py → system_api.py → monitors.py → остальные

**Коммит:** `fix: replace catch-all except Exception with specific types (N files)`

### 1.2. 🔴 SSE-подписка: QoS 0 → QoS 1

**Файл:** `services/sse_hub.py`, строка ~281
**Что:** Заменить `client.subscribe(t, qos=0)` на `qos=1`
**Зачем:** Гарантия доставки состояний реле в браузер

**Коммит:** `fix: upgrade SSE MQTT subscription to QoS 1`

### 1.3. 🟡 Подключить rate_limiter.py к мутирующим API

**Файл:** `services/rate_limiter.py` (86 строк, уже существует, но не подключён)
**Что:** Подключить к эндпоинтам:
- `/api/zones/<id>/mqtt/start` и `/mqtt/stop` — макс. 10 req/min
- `/api/emergency-stop` и `/api/emergency-resume` — макс. 5 req/min
- `/api/programs` POST/PUT/DELETE — макс. 20 req/min
- Все остальные мутирующие POST/PUT/DELETE — макс. 30 req/min

**Коммит:** `fix: enable rate limiting on mutating API endpoints`

### 1.4. 🟡 Content-Security-Policy header

**Файл:** `app.py`, функция `add_security_headers`
**Что:** Добавить CSP header, разрешающий только свои ресурсы:
```
Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'
```
**Зачем:** Защита от XSS

**Коммит:** `fix: add Content-Security-Policy header`

---

## Фаза 2: Качество кода

### 2.1. 🟡 Разбить zones_api.py (1174 строк)

**Используй Tree of Thoughts:**

```
Проблема: zones_api.py — 1174 строк, слишком много ответственностей
├── 💭 A: Разделить на zones_crud_api.py + zones_watering_api.py + zones_photo_api.py
├── 💭 B: Разделить по HTTP-методу (zones_read_api.py + zones_write_api.py)
└── 💭 C: Оставить один файл, но вынести helper-функции в zones_helpers.py
```

**Оцени варианты**, выбери лучший. При разделении — все blueprints должны регистрироваться в app.py, все URL остаются прежними.

**Коммит:** `refactor: split zones_api.py into focused modules`

### 2.2. 🟡 Разбить system_api.py (1004 строки)

Аналогично zones_api — вынеси логику в модули по доменам (backup, diagnostics, settings, emergency).

**Коммит:** `refactor: split system_api.py into focused modules`

### 2.3. 🟡 Вынести JS из шаблонов в отдельные .js файлы

**Проблема:** status.html (1897 строк), zones.html (3113 строк) — бо́льшая часть это JavaScript.

**Что сделать:**
- Вынести JS из `status.html` → `static/js/status.js`
- Вынести JS из `zones.html` → `static/js/zones.js`
- Вынести JS из `programs.html` → `static/js/programs.js`
- Вынести общие helpers из `base.html` → `static/js/app.js`
- В шаблонах оставить только `<script src="{{ asset('static/js/...') }}"></script>`
- Убедиться что CSRF-token injection работает (передать через meta-tag → JS читает)

**Коммит:** `refactor: extract JS from templates into static files`

### 2.4. 🟡 Structured JSON logging

**Что:** Заменить текстовый формат логов на JSON (structlog или стандартный json formatter).
**Формат:**
```json
{"timestamp": "2026-03-29T08:41:00Z", "level": "WARNING", "module": "zone_control", "message": "Failed to start zone", "zone_id": 5, "error": "ConnectionTimeout"}
```
**Зачем:** Парсинг, мониторинг, фильтрация логов.

**Коммит:** `feat: structured JSON logging`

---

## Фаза 3: CI/CD и тесты

### 3.1. 🟡 GitHub Actions CI/CD pipeline

**Создай `.github/workflows/ci.yml`:**

```yaml
# Минимальный пайплайн:
# 1. Lint: ruff check (или flake8)
# 2. Type check: mypy --strict на critical-path файлах
# 3. Tests: pytest с coverage
# 4. Security: bandit scan
# 5. Deploy: SSH на Wirenboard (manual trigger only)
```

**Используй Tree of Thoughts для выбора стратегии деплоя:**

```
Проблема: как деплоить на Wirenboard (ARM, systemd)
├── 💭 A: git pull + systemctl restart на контроллере (простой, но ручной SSH)
├── 💭 B: Docker image → push → pull на WB (изолированно, но WB8 ресурсы)
└── 💭 C: GitHub Actions → SSH action → git pull + restart (автоматический)
```

**Оцени**, выбери, реализуй. Deploy должен быть **manual trigger** (workflow_dispatch), не автоматический.

**Коммиты:**
- `ci: add GitHub Actions CI pipeline (lint, test, security)`
- `ci: add manual deploy workflow for Wirenboard`

### 3.2. Тесты для нового функционала

**Тестовая инфраструктура уже создана в Фазе 0.** Здесь добавляем тесты на новый код из Фаз 1-3:

- Тесты на rate limiter (после 1.3)
- Тесты на CSP header (после 1.4)
- Тесты на JSON logging format (после 2.4)
- Тесты на CI lint/type rules

**Коммит:** `test: add tests for new functionality (rate limiter, CSP, JSON logging)`

### 3.3. Добавить `ruff.toml` / `pyproject.toml` с конфигом линтера

**Коммит:** `ci: add ruff linter configuration`

---

## Фаза 4: Weather-dependent watering (погодозависимый полив)

### 4.1. 🔴 Интеграция с Open-Meteo API (бесплатный, без ключа)

**Это ключевая фича, отсутствие которой отличает WB-Irrigation от ВСЕХ конкурентов.**

**Используй Tree of Thoughts для архитектуры:**

```
Проблема: как реализовать погодозависимый полив
├── 💭 A: ET-based (FAO Penman-Monteith) — научный подход, сложный
├── 💭 B: Zimmerman method (OpenSprinkler) — простой, проверенный
└── 💭 C: Гибрид: Zimmerman для начала + ET позже
```

**Оцени**, выбери, обоснуй.

**Компоненты:**

**4.1.1. Weather Service** (`services/weather.py`)
- Запрос Open-Meteo API: температура, влажность, осадки, ветер, ET₀ (они уже считают!)
- URL: `https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,et0_fao_evapotranspiration&daily=precipitation_sum,et0_fao_evapotranspiration&timezone=auto`
- Кеширование ответа (SQLite таблица `weather_cache`, TTL 30 мин)
- Координаты из настроек (`/api/settings/location`)

**4.1.2. Watering Adjustment** (`services/weather_adjustment.py`)
- На основе погоды рассчитывать **коэффициент полива** (0-200%):
  - Дождь (>5mm за последние 24ч или прогноз >5mm) → skip полив (0%)
  - Температура >35°C → увеличить до 150%
  - Температура <10°C → уменьшить до 50%
  - Высокая влажность (>80%) → уменьшить до 70%
  - Сильный ветер (>25 км/ч) → отложить (эффективность снижается)
  - ET₀-based: если Open-Meteo отдаёт ET₀, использовать как основу
- Формула: `adjustment = max(0, min(200, base * temp_factor * rain_factor * humidity_factor * wind_factor))`

**4.1.3. Rain Skip / Freeze Skip**
- **Rain Skip:** Если осадки за последние 24ч > порога (настраиваемый, дефолт 5mm) ИЛИ прогноз на ближайшие 6ч > порога → пропустить программу, Telegram-уведомление
- **Freeze Skip:** Если температура < 2°C → пропустить, Telegram-алерт
- Настройки через API: `/api/settings/weather`

**4.1.4. UI: секция Weather в Settings**
- Координаты (lat/lon) — input + кнопка "Определить по IP"
- Включить/выключить weather adjustment
- Пороги: rain skip (mm), freeze skip (°C), wind skip (km/h)
- Текущая погода на дашборде (status.html): температура, влажность, последний дождь, текущий коэффициент

**4.1.5. Интеграция с планировщиком**
- Перед запуском программы: `scheduler → weather_adjustment.should_skip()` → если skip → лог + Telegram + пропуск
- Если не skip: `scheduler → weather_adjustment.get_coefficient()` → длительность зоны × коэффициент
- Логирование: таблица `weather_log` (дата, погода, коэффициент, решение skip/adjust)

**Коммиты:**
- `feat: add weather service (Open-Meteo API integration)`
- `feat: add watering adjustment engine (rain/freeze/wind skip)`
- `feat: integrate weather adjustment into scheduler`
- `feat: add weather settings UI and dashboard widget`

### 4.2. Тесты для weather-функционала

**Добавить в существующую тестовую инфраструктуру (Фаза 0):**
- `tests/unit/test_weather.py` — weather service, adjustment engine, skip logic
- `tests/integration/test_weather_integration.py` — Open-Meteo API → adjustment → skip decision
- Edge cases: API недоступно → полив без adjustment; rain >5mm → skip; freeze <2°C → skip

**Коммит:** `test: add weather feature tests (unit + integration)`

### 4.3. Soil moisture sensor integration (подготовка)

**Что:** Добавить в DB и UI поддержку датчиков влажности почвы через MQTT-топики Wirenboard.
- Таблица `soil_sensors` (id, name, mqtt_topic, zone_id, threshold_dry, threshold_wet)
- API: `/api/sensors/soil` CRUD
- Логика: если влажность > threshold_wet → skip полив зоны
- UI: показать текущую влажность на карточке зоны

**Коммит:** `feat: add soil moisture sensor support (MQTT)`

---

## Фаза 5: HTTPS и production hardening

### 5.1. 🟡 HTTPS через reverse proxy

**Используй Tree of Thoughts:**

```
Проблема: как добавить HTTPS на Wirenboard
├── 💭 A: nginx reverse proxy + self-signed cert
├── 💭 B: Caddy (auto-TLS с Let's Encrypt) — если есть домен
└── 💭 C: Python ssl context (встроенный) — простой, но без auto-renew
```

**Оцени**, учитывая что Wirenboard в локальной сети без домена.

**Коммит:** `feat: add HTTPS reverse proxy configuration`

### 5.2. 🟡 Миграции «вниз» (downgrade)

**Что:** Для каждой из 30 миграций добавить метод `down()` (DROP COLUMN через пересоздание таблицы в SQLite).
**Минимум:** downgrade для последних 10 миграций.
**Зачем:** Возможность отката без восстановления из бэкапа.

**Коммит:** `feat: add migration downgrade support`

### 5.3. 🟡 time.sleep → asyncio-совместимые ожидания

**Файл:** `irrigation_scheduler.py`
**Что:** Заменить `time.sleep()` в потоках планировщика на `threading.Event.wait()` с возможностью прерывания.
**Зачем:** Graceful shutdown, отмена программ без ожидания sleep.

**Коммит:** `refactor: replace time.sleep with interruptible Event.wait in scheduler`

---

# Формат работы

1. **Перед началом** — прочитай EXPERT-ANALYSIS.html и ключевые файлы проекта
2. **Для каждой фазы:**
   - Объяви начало фазы
   - Для задач с ToT — проведи анализ вариантов, выбери лучший, обоснуй
   - Выполни изменения
   - Протестируй на живом контроллере (http://10.2.5.244:8080)
   - Коммит + push
   - Краткий отчёт: что сделано, что проверено, что дальше
3. **После всех фаз** — итоговый отчёт со списком всех коммитов и результатов тестирования

# Ограничения

- **НЕ трогай:** архитектуру start/stop (дублирование в 4 местах), пароль '1234', общую структуру URL API
- **НЕ удаляй** существующий функционал
- **Тесты на контроллере** — можно включать/выключать реле (ничего не подключено)
- **Git:** отдельный коммит на каждое направление + push
- **При сомнениях** — выбирай безопасный вариант, а не рискованный
