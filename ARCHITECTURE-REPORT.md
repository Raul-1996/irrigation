# Архитектурный отчёт: wb-irrigation

**Дата:** 2026-04-02  
**Версия проекта:** ~2.0  
**Размер кодовой базы:** ~17 000 LOC Python (без тестов), 67 модулей  
**Стек:** Python 3.11+, Flask, SQLite, APScheduler, paho-mqtt, SSE  
**Целевая платформа:** WirenBoard (ARM), Docker

---

## 1. Структура проекта

```
wb-irrigation-src/
├── app.py                    (423 LOC) — Flask app, middleware, blueprint registration
├── run.py                    (75 LOC)  — точка входа (gunicorn/direct)
├── config.py                 (55 LOC)  — Config / TestConfig
├── constants.py              (35 LOC)  — magic numbers
├── database.py               (309 LOC) — facade-класс IrrigationDB
├── utils.py                  (128 LOC) — normalize_topic, encrypt/decrypt
├── irrigation_scheduler.py   (1365 LOC) — APScheduler + бизнес-логика полива ★
│
├── db/                       — Repository-слой (SQLite)
│   ├── base.py               (42 LOC)  — BaseRepository + retry_on_busy
│   ├── zones.py              (610 LOC)
│   ├── programs.py           (462 LOC)
│   ├── groups.py             (152 LOC)
│   ├── mqtt.py               (118 LOC)
│   ├── settings.py           (222 LOC)
│   ├── telegram.py           (240 LOC)
│   ├── logs.py               (202 LOC)
│   └── migrations.py         (1084 LOC)
│
├── routes/                   — Flask Blueprints
│   ├── auth.py, files.py, groups.py, mqtt.py, programs.py,
│   │   settings.py, status.py, zones.py  — page-rendering (thin)
│   ├── groups_api.py         (377 LOC)
│   ├── zones_crud_api.py     (541 LOC)
│   ├── zones_watering_api.py (475 LOC)
│   ├── zones_photo_api.py    (221 LOC)
│   ├── programs_api.py       (212 LOC)
│   ├── mqtt_api.py           (293 LOC)
│   ├── system_status_api.py  (606 LOC)
│   ├── system_config_api.py  (343 LOC)
│   ├── system_emergency_api.py (92 LOC)
│   ├── weather_api.py        (285 LOC)
│   ├── telegram.py           (247 LOC)
│   └── reports.py            (12 LOC)
│
├── services/                 — бизнес-логика
│   ├── app_init.py           (313 LOC) — одноразовая инициализация при старте
│   ├── zone_control.py       (378 LOC) — start/stop зон с блокировками
│   ├── mqtt_pub.py           (232 LOC) — MQTT publisher с кешем
│   ├── sse_hub.py            (362 LOC) — SSE-хаб для push в браузер
│   ├── monitors.py           (562 LOC) — rain/env/water мониторы
│   ├── float_monitor.py      (603 LOC) — мониторинг поплавкового датчика
│   ├── watchdog.py           (158 LOC) — cap-time watchdog
│   ├── program_queue.py      (510 LOC) — очередь программ
│   ├── telegram_bot.py       (564 LOC) — Telegram бот
│   ├── weather.py            (605 LOC) — OpenMeteo API
│   ├── weather_adjustment.py (508 LOC) — коэффициенты полива по погоде
│   ├── weather_merged.py     (425 LOC) — объединённый weather pipeline
│   ├── irrigation_decision.py(395 LOC) — ET-расчёты + решение о поливе
│   ├── et_calculator.py      (255 LOC) — ETo по Penman-Monteith
│   ├── observed_state.py     (261 LOC) — верификация MQTT state
│   ├── shutdown.py           (183 LOC) — graceful shutdown
│   ├── logging_setup.py      (209 LOC)
│   ├── events.py             (43 LOC)  — event bus
│   ├── locks.py              (59 LOC)  — named locks
│   ├── helpers.py            (50 LOC)  — api_error, parse_dt
│   ├── security.py           (41 LOC)  — @admin_required, @user_required
│   ├── auth_service.py       (40 LOC)  — verify_password
│   ├── rate_limiter.py       (86 LOC)  — login rate limiter
│   ├── api_rate_limiter.py   (114 LOC) — general API rate limiter
│   ├── reports.py            (37 LOC)  — текстовые отчёты
│   ├── scheduler_service.py  (5 LOC)   — DEPRECATED stub ★
│   ├── completion_tracker.py (4 LOC)   — re-export stub ★
│   └── weather_codes.py      (57 LOC)  — WMO weather codes
│
├── templates/                — Jinja2 шаблоны
├── static/                   — JS, CSS, images
├── basic_auth_proxy.py       (99 LOC)  — standalone proxy ★ неиспользуемый
├── ui_agent_demo.py          (295 LOC) — Selenium demo ★ неиспользуемый
└── tools/                    — dev-утилиты (batch_replace, fix_exceptions)
```

---

## 2. Диаграмма зависимостей

```
                              run.py
                                │
                                ▼
                    ┌──────── app.py ─────────┐
                    │   (Flask app, middleware,│
                    │    blueprints, watchdog) │
                    └──────────┬──────────────┘
                               │
           ┌───────────────────┼───────────────────┐
           │                   │                   │
           ▼                   ▼                   ▼
      routes/*_api.py    services/app_init.py   config.py
           │                   │
           │    ┌──────────────┼──────────────┐
           │    │              │              │
           ▼    ▼              ▼              ▼
      database.db ←── irrigation_scheduler.py  services/*
      (facade)        (1365 LOC GOD MODULE)     │
           │                   │                │
           ▼                   ▼                ▼
         db/*            services/zone_control  services/mqtt_pub
      (repositories)     services/monitors      services/sse_hub
                         services/watchdog       utils.py
                                                constants.py

    ┌──────────────────────────────────────────────────┐
    │           ЦИКЛИЧЕСКАЯ ЗАВИСИМОСТЬ                 │
    │  app.py ──imports──▶ services/app_init.py         │
    │  services/app_init.py ──imports──▶ app.py         │
    │  (runtime import app._start_single_zone_watchdog) │
    └──────────────────────────────────────────────────┘
```

### Ключевые потоки данных

```
Browser ──HTTP──▶ routes/*_api.py ──▶ database.db (direct)
                                  ──▶ services/zone_control ──▶ mqtt_pub ──▶ MQTT broker
                                  ──▶ irrigation_scheduler (direct)

MQTT broker ──▶ services/sse_hub ──SSE──▶ Browser
            ──▶ services/monitors ──▶ database.db

APScheduler ──▶ irrigation_scheduler ──▶ database.db + mqtt_pub + zone_control
```

---

## 3. Антипаттерны

### 3.1 🔴 God Module: `irrigation_scheduler.py` (1365 LOC)

**Критичность: ВЫСОКАЯ**

Самый большой модуль проекта совмещает:
- Управление APScheduler (создание/удаление задач)
- Бизнес-логику запуска/остановки программ
- Последовательный запуск зон в группе
- MQTT-публикацию
- Прямое обращение к БД
- Логику конфликтов расписаний
- Master-valve управление
- Singleton-паттерн через глобальные переменные

Должен быть разбит минимум на 3 модуля: scheduler_core, program_executor, schedule_manager.

### 3.2 🔴 Дублирование auth-логики в `app.py`

**Критичность: ВЫСОКАЯ**

Функция `_is_status_action()` определена **дважды** в одном файле (строки 203 и 256) с почти идентичной логикой — в `_auth_before_request` и `_require_admin_for_mutations`. Оба middleware проверяют роли и мутации по практически одинаковым правилам.

```python
# Строка 203 — первый раз
def _is_status_action(path):
    if path in allowed_public_posts or path == '/api/zones/next-watering-bulk':
        return True
    ...

# Строка 256 — второй раз, те же проверки
def _is_status_action(path):
    if path in ('/api/emergency-stop', '/api/emergency-resume', ...):
        return True
    ...
```

### 3.3 🔴 Циклическая зависимость: `app.py` ↔ `services/app_init.py`

**Критичность: ВЫСОКАЯ**

`app.py` импортирует `services/app_init.py`, который в свою очередь делает runtime-import `from app import _start_single_zone_watchdog` (строка 50). Это классический circular import, замаскированный ленивым импортом.

**Решение:** Перенести `_start_single_zone_watchdog` и весь watchdog-код из `app.py` в отдельный `services/group_watchdog.py`.

### 3.4 🟡 Нарушение слоёв: routes обращаются напрямую к БД

**Критичность: СРЕДНЯЯ**

Все 13 API-route файлов импортируют `from database import db` и вызывают методы БД напрямую (~195 вызовов `db.*` в routes/). Сервисный слой обходится. Примеры:

- `routes/weather_api.py:63` — **прямой `sqlite3.connect(db.db_path)`**, минуя даже facade
- `routes/settings.py` — прямые `db.get_setting_value()`, `db.set_setting_value()`
- `routes/zones_crud_api.py` — `db.create_zone()`, `db.update_zone()` без service-слоя

### 3.5 🟡 Глобальный синглтон `db = IrrigationDB()` на уровне модуля

**Критичность: СРЕДНЯЯ**

```python
# database.py, последняя строка
db = IrrigationDB()
```

База создаётся при первом импорте модуля. Это:
- Делает невозможным dependency injection
- Усложняет тестирование (нужны monkey-patches)
- Создаёт неявную связь: любой `from database import db` триггерит инициализацию БД

### 3.6 🟡 Толстый `app.py` со смешанными ответственностями

**Критичность: СРЕДНЯЯ**

`app.py` (423 LOC) совмещает:
- Создание Flask app
- Конфигурацию CSRF
- Регистрацию blueprints
- Security middleware (auth, rate limiting, mutation guard)
- Group exclusivity watchdog (бизнес-логика!)
- `_force_group_exclusive()` — 50 строк бизнес-логики
- Performance timing middleware
- Security headers middleware
- Debug logging helpers
- Graceful shutdown registration

### 3.7 🟡 Excessive exception catching

**Критичность: СРЕДНЯЯ**

По всему коду (33 except-блока только в `app.py`) встречаются чрезмерно широкие перехваты:

```python
except (ConnectionError, TimeoutError, OSError) as e:
    logger.warning("auth before_request error: %s", e)
```

```python
except (OSError, RuntimeError, ValueError):  # catch-all: intentional
```

Комментарии "catch-all: intentional" в нескольких местах — признак осознанного но плохого решения. Ошибки глотаются, что затрудняет отладку на embedded-системе.

### 3.8 🟡 Facade-антипаттерн в `database.py`

**Критичность: СРЕДНЯЯ**

`IrrigationDB` — это чистый прокси-класс на 309 строк, где каждый метод — однострочный проброс в repository:

```python
def get_zones(self, **kw):
    return self.zones.get_zones(**kw)

def get_zone(self, zone_id):
    return self.zones.get_zone(zone_id)
# ... ещё 60+ таких методов
```

Facade создавался для обратной совместимости при рефакторинге, но теперь тормозит дальнейшую декомпозицию и навигацию по коду.

### 3.9 🟢 SSE Hub с dependency injection через `init()`

**Критичность: НИЗКАЯ** (это скорее позитив)

`services/sse_hub.py` — единственный модуль, который корректно использует DI: `init(db=db, mqtt_module=mqtt, ...)`. Остальные сервисы таскают глобальный `db` напрямую.

---

## 4. Неиспользуемые / устаревшие файлы

| Файл | LOC | Статус | Обоснование |
|------|-----|--------|-------------|
| `ui_agent_demo.py` | 295 | ❌ Удалить | Selenium-демо, не используется в коде |
| `basic_auth_proxy.py` | 99 | ❌ Удалить | Standalone HTTP proxy, ни один модуль не импортирует |
| `templates/programs_old.html` | ? | ❌ Удалить | Ни один route не рендерит этот шаблон |
| `services/scheduler_service.py` | 5 | ❌ Удалить | Пустой deprecated-stub, никто не импортирует |
| `services/completion_tracker.py` | 4 | ⚠️ Оценить | Re-export из program_queue, можно убрать |
| `routes/system_api.py` | 22 | ⚠️ Оценить | Только re-export sub-blueprints, не регистрируется в app.py |
| `routes/zones_api.py` | 25 | ⚠️ Оценить | Только re-export sub-blueprints, не регистрируется в app.py |

---

## 5. Дублирование между модулями

### 5.1 Auth-проверки (КРИТИЧЕСКОЕ)

| Место | Что дублируется |
|-------|----------------|
| `app.py:_auth_before_request` | Проверка ролей, viewer read-only, `_is_status_action()` |
| `app.py:_require_admin_for_mutations` | То же самое, та же логика |
| `services/security.py:admin_required` | Декоратор `@admin_required` для routes |

Три механизма авторизации работают параллельно. Middleware в `app.py` покрывает то же, что и декораторы в routes.

### 5.2 Boot-sync дублирование в `services/app_init.py`

В `_boot_sync()` master-valve закрытие выполняется **дважды**:
- Строки 87-120: первый проход по master-valves
- Строки 152-193: второй проход с retries ("secondary safety net")

Идентичная логика с минимальными различиями. Можно объединить в один проход с retries.

### 5.3 Weather-модули

Три модуля с пересекающейся функциональностью:
- `services/weather.py` (605 LOC) — основной API + кеш
- `services/weather_adjustment.py` (508 LOC) — коэффициенты
- `services/weather_merged.py` (425 LOC) — "объединённый pipeline"

**1538 LOC** на weather-логику — 9% кодовой базы. `weather_merged.py` похож на попытку объединить первые два, но все три продолжают существовать.

### 5.4 Logging setup

- `database.py` строки 21-29: настройка logging
- `irrigation_scheduler.py` строки 47-54: аналогичная настройка
- `services/logging_setup.py`: отдельный модуль для logging setup

Настройка форматтера дублируется в трёх местах.

---

## 6. Оценка качеств архитектуры

### 6.1 Тестируемость: 4/10

**Проблемы:**
- Глобальный `db = IrrigationDB()` требует monkey-patching
- `irrigation_scheduler.py` использует глобальные `_scheduler`, `_db` — нет DI
- Бизнес-логика в `app.py` (watchdog, auth) не тестируема отдельно от Flask
- SSE hub — единственный модуль с нормальным DI

**Позитив:**
- Repository-слой (`db/*`) хорошо изолирован и тестируем
- `services/zone_control.py` выделен в отдельный модуль с чистым API
- TestConfig отключает CSRF

### 6.2 Масштабируемость: 3/10

**Проблемы:**
- SQLite как единственная БД (одновременная запись блокирует)
- Один процесс (Flask dev server / gunicorn single worker из-за SQLite)
- Глобальное состояние в модулях (SSE hub, scheduler, monitors)
- Нет API versioning

**Смягчающий фактор:** Для IoT-контроллера горизонтальная масштабируемость не нужна. SQLite адекватен для задачи.

### 6.3 Поддерживаемость: 5/10

**Позитив:**
- Чёткое разделение на routes / services / db
- `constants.py` — централизованные magic numbers
- Repository-паттерн в `db/*`
- Docstrings в ключевых модулях

**Проблемы:**
- `irrigation_scheduler.py` на 1365 строк — зона риска
- Дублирование auth-логики
- Циклическая зависимость
- 67 Python-модулей для относительно простой задачи

### 6.4 Пригодность для IoT/Embedded: 7/10

**Позитив:**
- SQLite — правильный выбор для embedded
- APScheduler — лёгкий, без внешних зависимостей
- Graceful shutdown с отключением всех зон (безопасность!)
- Boot-sync — все вентили OFF при старте (критично для полива)
- Observed state verification через MQTT
- Cap-time watchdog (защита от зависания зоны)
- Retry-логика для MQTT (embedded-сети нестабильны)

**Проблемы:**
- 17K LOC — многовато для ARM, но не критично
- `paho-mqtt` + `APScheduler` + `Flask` — приемлемый стек
- Нет watchdog на уровне systemd (или не виден в коде)

---

## 7. Общая оценка архитектуры

## **5 из 10**

**Обоснование:**

Проект прошёл через минимум один рефакторинг (decomposition `database.py` → `db/*`, вынос `services/app_init.py` из `app.py`), что хорошо. Видны следы задач TASK-010, TASK-015, TASK-016, TASK-028 — структурированная работа по улучшению.

Однако рефакторинг не завершён:
- `database.py` facade осталась как 309-строчный прокси
- Бизнес-логика в `app.py` не вычищена (watchdog, auth guards)
- `irrigation_scheduler.py` — основной God Module — не тронут
- Дублирование auth-логики — потенциальный источник багов безопасности
- Циклическая зависимость `app.py` ↔ `app_init.py`

Для IoT-проекта на контроллере архитектура функциональна и безопасна (boot-sync, shutdown, watchdogs). Но поддержка и расширение затруднены.

---

## 8. Приоритезированный план улучшений

### Фаза 1: Критические (1-2 недели)

#### 1.1 Устранить дублирование auth-логики
**Приоритет:** P0 (безопасность)  
**Усилия:** 2-4 часа  
**Действие:** Объединить `_auth_before_request` и `_require_admin_for_mutations` в один middleware. Вынести `_is_status_action()` в `services/security.py`.

#### 1.2 Разорвать циклическую зависимость
**Приоритет:** P0  
**Усилия:** 2-3 часа  
**Действие:** Перенести `_start_single_zone_watchdog`, `_force_group_exclusive`, `_enforce_group_exclusive_all_groups` из `app.py` в `services/group_watchdog.py`. Убрать `from app import` из `app_init.py`.

#### 1.3 Удалить мёртвый код
**Приоритет:** P1  
**Усилия:** 30 минут  
**Действие:** Удалить `ui_agent_demo.py`, `basic_auth_proxy.py`, `templates/programs_old.html`, `services/scheduler_service.py`.

### Фаза 2: Архитектурные (2-4 недели)

#### 2.1 Декомпозиция `irrigation_scheduler.py`
**Приоритет:** P1  
**Усилия:** 1-2 дня  
**Действие:** Разбить на:
- `services/scheduler_core.py` — управление APScheduler (add/remove jobs)
- `services/program_executor.py` — логика запуска программ и последовательности зон
- `irrigation_scheduler.py` — тонкий фасад для обратной совместимости

#### 2.2 Очистить `app.py`
**Приоритет:** P1  
**Усилия:** 4-6 часов  
**Действие:**
- Middleware → `services/middleware.py` (security headers, timing, auth)
- Watchdog → `services/group_watchdog.py` (уже из п.1.2)
- Blueprint registration → оставить
- Результат: `app.py` ≤ 100 LOC

#### 2.3 Постепенный отказ от `database.py` facade
**Приоритет:** P2  
**Усилия:** 1-2 дня (инкрементально)  
**Действие:** Новый код пишет `from db.zones import ZoneRepository` напрямую. Старые вызовы `db.get_zones()` работают через deprecation warnings. Через 2-3 релиза удалить facade.

### Фаза 3: Улучшения (1-2 месяца)

#### 3.1 Консолидация weather-модулей
**Приоритет:** P2  
**Усилия:** 1-2 дня  
**Действие:** Объединить `weather.py` + `weather_merged.py` в один модуль. `weather_adjustment.py` оставить отдельным (другая ответственность).

#### 3.2 Единый logging setup
**Приоритет:** P3  
**Усилия:** 1-2 часа  
**Действие:** Убрать дублирование logging setup из `database.py` и `irrigation_scheduler.py`. Оставить только `services/logging_setup.py`.

#### 3.3 Убрать прямой SQLite из routes
**Приоритет:** P2  
**Усилия:** 2-4 часа  
**Действие:** Заменить `sqlite3.connect(db.db_path)` в `routes/weather_api.py` на методы repository. Все `db.*` вызовы из routes должны проходить через service-слой.

#### 3.4 API контракт (OpenAPI)
**Приоритет:** P3  
**Усилия:** 2-3 дня  
**Действие:** Добавить `flask-smorest` или ручные JSON Schema валидации. Сейчас API контракт определяется только кодом — нет документации, нет валидации входных данных.

#### 3.5 Boot-sync дедупликация
**Приоритет:** P3  
**Усилия:** 1 час  
**Действие:** Объединить два прохода по master-valves в `_boot_sync()` в один с retry-логикой.

---

## Приложение A: Размер модулей (Top-15)

| Модуль | LOC | Роль |
|--------|-----|------|
| irrigation_scheduler.py | 1365 | 🔴 God Module |
| db/migrations.py | 1084 | Миграции (приемлемо) |
| db/zones.py | 610 | Repository |
| routes/system_status_api.py | 606 | API routes |
| services/weather.py | 605 | Weather API |
| services/float_monitor.py | 603 | Float sensor |
| services/telegram_bot.py | 564 | Telegram |
| services/monitors.py | 562 | Мониторы |
| routes/zones_crud_api.py | 541 | API routes |
| services/program_queue.py | 510 | Очередь программ |
| services/weather_adjustment.py | 508 | Weather коэффициенты |
| routes/zones_watering_api.py | 475 | API routes |
| db/programs.py | 462 | Repository |
| services/weather_merged.py | 425 | Weather merged |
| app.py | 423 | Flask app |

## Приложение B: Граф импортов (упрощённый)

```
constants.py ← services/*, routes/*
utils.py     ← app.py, routes/*, services/*, db/migrations.py, irrigation_scheduler.py

database.py  ← app.py, routes/* (13 файлов), services/* (5 файлов), irrigation_scheduler.py
  └── db/*   ← database.py

config.py    ← app.py

irrigation_scheduler.py ← app.py, routes/groups_api.py, routes/programs_api.py,
                          routes/system_config_api.py, routes/system_emergency_api.py,
                          routes/system_status_api.py, routes/zones_watering_api.py,
                          services/app_init.py

services/zone_control.py ← routes/groups_api.py, routes/zones_watering_api.py,
                           routes/telegram.py, routes/system_emergency_api.py,
                           services/app_init.py, services/watchdog.py

services/mqtt_pub.py ← app.py, routes/groups_api.py, routes/system_config_api.py,
                       routes/zones_watering_api.py, services/zone_control.py,
                       services/sse_hub.py (injected), irrigation_scheduler.py

services/sse_hub.py ← app.py (init + inject), routes/groups_api.py,
                      routes/zones_watering_api.py, routes/system_status_api.py
```
