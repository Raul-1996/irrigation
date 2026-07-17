# WB-Irrigation: Отчёт по декомпозиции крупных файлов

> Дата: 2026-04-02  
> Ветка: `refactor/v2`  
> Автор анализа: AI Architecture Review  
> Платформа: Flask на ARM (WirenBoard), ресурсы ограничены

---

## 1. Сводная таблица

| # | Файл | LOC | Сложность (1-5) | Приоритет (1-5) | Риск | Тип |
|---|------|-----|-----------------|-----------------|------|-----|
| 1 | `static/js/status.js` | 2187 | 5 | 5 | Высокий | Frontend JS |
| 2 | `static/js/zones.js` | 1964 | 4 | 4 | Высокий | Frontend JS |
| 3 | `templates/status.html` | 1819 | 3 | 3 | Средний | Jinja2 Template |
| 4 | `templates/programs.html` | 1463 | 3 | 3 | Средний | Jinja2 Template |
| 5 | `irrigation_scheduler.py` | 1365 | 5 | 5 | Высокий | Python Core |
| 6 | `db/migrations.py` | 1084 | 2 | 1 | Низкий | Python DB |
| 7 | `db/zones.py` | 610 | 2 | 2 | Низкий | Python DB |
| 8 | `routes/system_status_api.py` | 606 | 3 | 3 | Средний | Python Routes |
| 9 | `services/weather.py` | 605 | 2 | 2 | Низкий | Python Service |
| 10 | `services/float_monitor.py` | 603 | 2 | 1 | Низкий | Python Service |
| 11 | `services/telegram_bot.py` | 564 | 3 | 3 | Средний | Python Service |
| 12 | `services/monitors.py` | 562 | 3 | 4 | Средний | Python Service |
| 13 | `services/program_queue.py` | 510 | 2 | 1 | Низкий | Python Service |
| 14 | `services/weather_adjustment.py` | 508 | 2 | 2 | Низкий | Python Service |

**Условные обозначения:**
- **Сложность** — трудоёмкость рефакторинга (1 = тривиально, 5 = очень сложно)
- **Приоритет** — насколько срочно нужен рефакторинг (1 = можно отложить, 5 = делать первым)
- **Риск** — вероятность сломать функционал при рефакторинге

---

## 2. Детальный анализ каждого файла

---

### 2.1. `static/js/status.js` — 2187 LOC

**God-файл №1.** Содержит абсолютно всю логику главной страницы: загрузка данных, рендеринг, таймеры, виджет погоды, управление зонами, SSE, sidebar.

#### Текущие ответственности (8+):
1. **UI timing / performance** (строки 1–35) — обёртка fetch, замеры latency
2. **Глобальное состояние** (строки 37–45) — `statusData`, `zonesData`, `connectionError`
3. **Серверное время** (строки 47–60) — `syncServerTime`, `updateDateTime`
4. **Загрузка и обновление статуса** (строки 62–500) — `loadStatusData`, `loadZonesData`, `updateStatusDisplay`, `initGroupTimer`, группы карточки, мастер-клапан, давление, вода — **god-функция `updateStatusDisplay` ~200 LOC**
5. **Таймеры и countdown** (строки 501–650) — `tickCountdowns`, `refreshSingleGroup` — **god-функция `refreshSingleGroup` ~100 LOC** (полная копия логики из `updateStatusDisplay`)
6. **Действия пользователя** (строки 650–1000) — `delayGroup`, `cancelPostpone`, `startGroupFromFirst`, `stopGroup`, `startOrStopZone`, `toggleMasterValve`, `emergencyStop`, `resumeSchedule`
7. **Виджет погоды** (строки 1100–1400) — полный рендеринг погоды: summary, forecast 24h, forecast 3d, details, factors, history — **можно выделить целиком**
8. **Zones V2 карточки** (строки 1500–2187) — `renderGroupTabs`, `renderZoneCards`, run popup с circular dial, bottom sheet, toast — **ещё один «приложение в приложении»**

#### God-functions (>50 LOC):
| Функция | LOC | Проблема |
|---------|-----|----------|
| `updateStatusDisplay()` | ~200 | Рендерит ВСЕ: env, группы, MV, давление, воду, кнопки |
| `refreshSingleGroup()` | ~100 | Полная копипаста из `updateStatusDisplay` |
| `renderZoneCards()` | ~110 | Строит HTML-строки огромным конкатом |
| `renderGroupsGrid()` (в zones.js) | ~180 | Аналогичная проблема |
| `loadZonesData()` | ~65 | Смешивает fetch, render, async next-watering |

#### Нарушения SRP:
- Дублирование: `refreshSingleGroup` = копия `updateStatusDisplay` для одной группы
- Виджет погоды — самостоятельный модуль, не связан с основной логикой
- Zones V2 карточки — вторая система рендеринга параллельно таблице
- Circular dial для выбора длительности — UI-компонент, можно выделить

#### Предложенная декомпозиция:
```
static/js/
├── status/
│   ├── status-state.js          # Глобальное состояние (statusData, zonesData, flags)
│   ├── status-data.js           # loadStatusData, loadZonesData, syncServerTime
│   ├── status-groups.js         # updateStatusDisplay, refreshSingleGroup (унифицировать!)
│   ├── status-timers.js         # tickCountdowns, initGroupTimer, formatSeconds
│   ├── status-actions.js        # delayGroup, cancelPostpone, startOrStopZone, emergencyStop, ...
│   ├── status-master-valve.js   # toggleMasterValve + MV rendering
│   ├── status-weather.js        # Весь виджет погоды (~300 LOC)
│   ├── status-zones-v2.js       # renderGroupTabs, renderZoneCards, toggleZoneCard, zone search
│   ├── status-run-popup.js      # Circular dial popup (showRunPopup, initDialDrag, confirmRun)
│   ├── status-sidebar.js        # Active zone indicator, water meter, sidebar toggle
│   └── status-init.js           # DOMContentLoaded, SSR, intervals, SSE, exports
```

**Критическая оптимизация:** устранить дублирование `updateStatusDisplay` / `refreshSingleGroup` — выделить общую функцию рендеринга карточки группы.

**Сложность: 5** — тесная связь через глобальные переменные, отсутствие модульной системы (IIFE).  
**Рекомендация:** использовать простой namespace pattern `window.Status = {}` без бандлера (ARM!).

---

### 2.2. `static/js/zones.js` — 1964 LOC

**God-файл №2.** Страница настройки зон: CRUD зон, группы, модальные окна, датчики, CSV import/export, фото, water meter.

#### Текущие ответственности (7+):
1. **Загрузка данных** (строки 1–40) — `loadData`, parallel API calls
2. **Рендеринг таблицы зон** (строки 42–130) — `renderZonesTable` ~90 LOC — **god-function**
3. **Рендеринг сетки групп** (строки 132–310) — `renderGroupsGrid` ~180 LOC — **god-function** с 3 вложенными модальными окнами inline
4. **Настройки датчиков** (строки 310–730) — мастер-клапан, давление, вода, дождь, env — 8 функций toggle/save для каждого типа датчика
5. **Water meter** (строки 730–920) — digits, pulses, live polling, auto-save — **самостоятельный виджет**
6. **CRUD зон/групп** (строки 920–1400) — создание, удаление, сортировка, массовые действия, конфликты
7. **CSV import/export** (строки 1750–1960) — полная логика работы с CSV
8. **Фото** (строки 1580–1680) — upload, delete, rotate, modal

#### God-functions (>50 LOC):
| Функция | LOC | Проблема |
|---------|-----|----------|
| `renderGroupsGrid()` | ~180 | HTML 3-х модальных окон генерируется inline |
| `renderZonesTable()` | ~90 | Большой innerHTML с множеством вложенных обработчиков |
| `scheduleAutoSave()` | ~60 | Собирает payload из 12+ DOM-элементов |
| `applyBulkAction()` | ~80 | switch с 5 ветвями, вложенные API вызовы |

#### Предложенная декомпозиция:
```
static/js/
├── zones/
│   ├── zones-state.js           # zonesData, groupsData, modifiedZones, modifiedGroups
│   ├── zones-data.js            # loadData, loadGroupSelectors, updateZonesCount
│   ├── zones-table.js           # renderZonesTable, sortTable, updateZone, saveZone
│   ├── zones-groups.js          # renderGroupsGrid, autoSaveGroupName, deleteGroup
│   ├── zones-sensors.js         # toggleGroupUseMaster, toggleGroupUsePressure, toggleGroupUseWater
│   ├── zones-modals.js          # openMasterSettings, openPressureSettings, openWaterSettings + modal helpers
│   ├── zones-water-meter.js     # Весь water meter widget (digits, pulses, live, save)
│   ├── zones-rain-env.js        # initRainUi, saveRainConfig, initEnvUi, saveEnvConfig
│   ├── zones-bulk.js            # applyBulkAction, selectAll, updateSelectedCount
│   ├── zones-csv.js             # exportZonesCSV, importZonesCSV, handleCSVImport
│   ├── zones-photos.js          # uploadPhoto, deletePhoto, rotatePhoto, showPhotoModal
│   └── zones-init.js            # DOMContentLoaded, form handlers, SSE, exports
```

**Сложность: 4** — менее связан, чем status.js, но много inline HTML в JS.

---

### 2.3. `templates/status.html` — 1819 LOC

#### Текущая структура:
- **extra_css** (строки 6–1560) — ~1554 строк CSS! Огромный блок стилей
- **content** (строки 1561–1810) — HTML разметка (~250 LOC)
- **extra_js** (строки 1811–1819) — SSR данные + подключение status.js

#### Предложенная декомпозиция:
```
static/css/
├── status.css                   # Вынести ВСЕ CSS из шаблона (~1554 LOC)
│   или разбить на:
├── status/
│   ├── status-base.css          # Базовые стили карточек, layout
│   ├── status-groups.css        # Стили карточек групп
│   ├── status-zones-v2.css      # Стили Zone V2 cards
│   ├── status-weather.css       # Стили виджета погоды
│   ├── status-sidebar.css       # Desktop sidebar
│   └── status-responsive.css    # Media queries

templates/
├── status.html                  # Только HTML + SSR, ~250 LOC
├── partials/
│   ├── _status_header.html      # Env-блок (температура, влажность, дождь)
│   ├── _status_groups.html      # Container для групп
│   ├── _status_zones_v2.html    # Zone cards layout
│   └── _status_weather.html     # Weather widget skeleton
```

**Сложность: 3** — CSS extraction безрисковая, partials Jinja2 — стандарт.  
**Приоритет: 3** — делать параллельно с JS, чтобы CSS-классы были согласованы.

---

### 2.4. `templates/programs.html` — 1463 LOC

#### Текущая структура:
- **extra_css** (строки 6–852) — ~846 строк CSS
- **content** (строки 853–916) — HTML разметка (~63 LOC)
- **extra_js** (строки 917–1463) — ~546 строк JS inline

#### Предложенная декомпозиция:
```
static/css/programs.css          # ~846 LOC CSS
static/js/programs.js            # ~546 LOC JS (уже достаточно компактно)
templates/programs.html           # Только HTML (~63 LOC)
```

**Сложность: 3** — простой extract CSS и JS.  
**Риск: Низкий** — шаблон уже хорошо структурирован.

---

### 2.5. `irrigation_scheduler.py` — 1365 LOC

**God-класс №1.** `IrrigationScheduler` — 30+ методов, управление программами, зонами, группами, weather, boot recovery.

#### Текущие ответственности (6+):
1. **Инициализация и lifecycle** — `__init__`, `start`, `stop`, `load_programs`
2. **Управление программами** — `schedule_program`, `_schedule_single_time`, `cancel_program`
3. **Выполнение программ** — `_run_program_threaded` (~120 LOC god-function)
4. **Групповой последовательный полив** — `start_group_sequence`, `_run_group_sequence` (~130 LOC god-function)
5. **Управление отдельными зонами** — `_stop_zone`, `schedule_zone_stop`, `schedule_zone_hard_stop`, `schedule_zone_cap`
6. **Мастер-клапан** — `schedule_master_valve_cap`, `cancel_master_valve_cap`, `job_close_master_valve`
7. **Отложки** — `clear_expired_postpones`, `schedule_postpone_sweeper`
8. **Погода** — `_check_weather_skip`, `_get_weather_adjusted_duration`
9. **Boot recovery** — `recover_missed_runs`, `cleanup_jobs_on_boot`, `stop_on_boot_active_zones`

#### God-functions (>50 LOC):
| Функция | LOC | Проблема |
|---------|-----|----------|
| `_run_program_threaded()` | ~120 | Цикл с 8+ ответственностями: погода, отложки, MV, MQTT, логирование, таймеры |
| `_run_group_sequence()` | ~130 | Почти полная копия `_run_program_threaded` для групп |
| `start_group_sequence()` | ~70 | setup + очистка + планирование |
| `schedule_program()` | ~65 | Парсинг программы + pre-calculation стартов |
| `recover_missed_runs()` | ~55 | Восстановление пропущенных запусков |

#### God-objects:
- `IrrigationScheduler` — 30+ методов, 800+ LOC — **явный god-class**

#### Предложенная декомпозиция:
```
scheduler/
├── __init__.py                  # re-export init_scheduler, get_scheduler
├── core.py                      # IrrigationScheduler: init, start, stop, lifecycle (~100 LOC)
├── program_runner.py            # ProgramRunner: _run_program_threaded, weather checks (~200 LOC)
├── group_runner.py              # GroupRunner: start_group_sequence, _run_group_sequence (~200 LOC)
├── zone_jobs.py                 # schedule_zone_stop, schedule_zone_hard_stop, schedule_zone_cap (~120 LOC)
├── program_scheduler.py         # schedule_program, _schedule_single_time, cancel_program (~150 LOC)
├── master_valve_jobs.py         # schedule_master_valve_cap, job_close_master_valve (~60 LOC)
├── postpone.py                  # clear_expired_postpones, schedule_postpone_sweeper (~60 LOC)
├── recovery.py                  # recover_missed_runs, cleanup_jobs_on_boot, stop_on_boot_active_zones (~80 LOC)
└── jobs.py                      # Module-level job callables (job_run_program, etc.) — уже есть, оставить
```

**Ключевое улучшение:** `_run_program_threaded` и `_run_group_sequence` имеют ~70% общего кода (цикл зон, weather check, early off, cancel event). Выделить **общий `ZoneSequenceRunner`** — устранит дублирование.

**Сложность: 5** — многопоточность, APScheduler, глобальное состояние, тесная связь с DB.  
**Риск: Высокий** — ошибки = пропущенный/бесконечный полив.

---

### 2.6. `db/migrations.py` — 1084 LOC

#### Текущая структура:
- `MigrationRunner` — 1 класс с 35+ методов: `init_database`, `_insert_initial_data`, 25+ `_migrate_*`, 10+ `_down_*`
- Каждая миграция — отдельный метод 10-40 LOC

#### Оценка:
**НЕ рекомендую разбивать.** Файл большой, но:
1. Каждая миграция — изолированный метод без зависимостей
2. Добавление миграции = добавление одного метода + одна строка в `init_database`
3. Разбиение по файлам усложнит порядок применения миграций
4. Формат уже стандартен для SQLite-based migration runners

**Единственная рекомендация:** вынести `DOWNGRADE_REGISTRY` и `_down_*` методы в отдельный файл, если их станет >20:
```
db/
├── migrations.py                # MigrationRunner + _migrate_* methods
├── migrations_down.py           # DowngradeMixin + _down_* methods + DOWNGRADE_REGISTRY
```

**Сложность: 2** — простое разделение mixin.  
**Приоритет: 1** — не мешает разработке.

---

### 2.7. `db/zones.py` — 610 LOC

#### Текущая структура:
- `ZoneRepository` — CRUD зон, bulk operations, zone_runs, next_run calculation

#### Оценка:
Файл хорошо структурирован. Все методы — операции с зонами. **Не требует декомпозиции.**

Единственная рекомендация:
- `update_zone()` (~100 LOC) — много ручного маппинга полей → рефакторинг в generic field mapper
- `compute_next_run_for_zone()` + `reschedule_group_to_next_program()` можно вынести в `db/scheduling.py` если вырастет

**Сложность: 2** | **Приоритет: 2**

---

### 2.8. `routes/system_status_api.py` — 606 LOC

#### Текущие ответственности (5):
1. **Health / Scheduler endpoints** — `api_health_details`, `api_health_cancel_job`, `api_health_cancel_group`
2. **Scheduler management** — `api_scheduler_init`, `api_scheduler_status`, `api_scheduler_jobs`
3. **Health check** — `health_check`
4. **Status API** — `api_status` (~120 LOC god-function: собирает данные из 6+ источников)
5. **Logs & Water** — `api_logs`, `api_water`

#### God-functions:
| Функция | LOC | Проблема |
|---------|-----|----------|
| `api_status()` | ~120 | Собирает status из zones, groups, rain, env, MQTT, emergency, weather |

#### Предложенная декомпозиция:
```
routes/
├── system_status_api.py         # api_status (core), api_server_time (~200 LOC)
├── health_api.py                # health_check, api_health_details, cancel_job, cancel_group (~150 LOC)
├── scheduler_api.py             # api_scheduler_init, api_scheduler_status, api_scheduler_jobs (~80 LOC)
├── logs_api.py                  # api_logs (~50 LOC)
└── water_api.py                 # api_water (~60 LOC)
```

**Ключевое улучшение:** `api_status()` собирает group status inline — выделить `StatusBuilder` service, который будет собирать `groups_status` вне route handler.

**Сложность: 3** | **Приоритет: 3**

---

### 2.9. `services/weather.py` — 605 LOC

#### Текущие ответственности (3):
1. `WeatherData` — парсинг Open-Meteo response
2. `WeatherService` — fetch, cache, get_weather, get_forecast_24h/3d, get_summary
3. `get_weather_service()` — singleton factory

#### Оценка:
Хорошо структурирован. Единственная проблема — `WeatherData._parse()` (~100 LOC) с многими `_safe_get` вызовами.

**Рекомендация:** не разбивать. При необходимости:
```
services/
├── weather/
│   ├── __init__.py              # re-export
│   ├── models.py                # WeatherData, WeatherForecast dataclasses
│   ├── client.py                # Open-Meteo HTTP client + caching
│   └── service.py               # WeatherService (orchestration)
```

**Сложность: 2** | **Приоритет: 2**

---

### 2.10. `services/float_monitor.py` — 603 LOC

#### Текущие ответственности (3):
1. `_GroupState` — dataclass для per-group state
2. `FloatMonitor` — MQTT subscriptions, debounce, hysteresis, pause/resume logic
3. Internal methods — `_on_float_message`, `_apply_level_change`, `_handle_pause`, `_handle_resume`

#### Оценка:
Хорошо изолирован (одна ответственность — мониторинг поплавка). **Не требует декомпозиции.**

**Сложность: 2** | **Приоритет: 1**

---

### 2.11. `services/telegram_bot.py` — 564 LOC

#### Текущие ответственности (4):
1. `TelegramNotifier` — send_text, send_message, edit_message, answer_callback (HTTP + aiogram fallback)
2. `AiogramBotRunner` — thread management, event loop, dispatcher setup
3. Module-level helpers — `_load_routes_module`, `_redact_url`
4. Global state — `notifier`, `_aiogram_runner`, module init

#### Предложенная декомпозиция:
```
services/telegram/
├── __init__.py                  # re-export notifier, start_bot, stop_bot
├── notifier.py                  # TelegramNotifier (HTTP + aiogram bridge, ~200 LOC)
├── bot_runner.py                # AiogramBotRunner (thread, loop, dispatcher, ~250 LOC)
└── helpers.py                   # _load_routes_module, _redact_url, logger setup (~60 LOC)
```

**Сложность: 3** — asyncio + threading + aiogram интеграция.  
**Приоритет: 3** — мешает тестированию (глобальный state).

---

### 2.12. `services/monitors.py` — 562 LOC

#### Текущие ответственности (3 класса!):
1. `RainMonitor` — MQTT rain sensor monitoring, rain start/stop logic (~170 LOC)
2. `EnvMonitor` — temperature + humidity MQTT monitoring (~250 LOC)
3. `WaterMonitor` — water meter reading, flow calculation (~140 LOC)

#### God-functions:
| Функция | LOC | Проблема |
|---------|-----|----------|
| `_on_rain_start()` | ~50 | Останавливает полив + откладывает + отменяет программы |
| `_on_rain_stop()` | ~35 | Reverse logic |
| `EnvMonitor._start_temp()` | ~55 | MQTT client setup (дублируется в `_start_hum`) |

#### Предложенная декомпозиция:
```
services/monitors/
├── __init__.py                  # re-export rain_monitor, env_monitor, water_monitor
├── rain_monitor.py              # RainMonitor (~170 LOC)
├── env_monitor.py               # EnvMonitor (~250 LOC)
└── water_monitor.py             # WaterMonitor (~140 LOC)
```

**Ключевое улучшение:** `EnvMonitor._start_temp` и `_start_hum` — 90% общего кода. Выделить `_start_mqtt_sensor(topic, server_id, callback)`.

**Сложность: 3** | **Приоритет: 4** — три несвязанных класса в одном файле = частые merge conflicts.

---

### 2.13. `services/program_queue.py` — 510 LOC

#### Текущие ответственности (2):
1. `ProgramQueueManager` — per-group FIFO queue, worker threads, enqueue/cancel/status
2. `QueueEntry` / `GroupQueue` — dataclasses

#### Оценка:
Хорошо структурирован. SRP соблюдён. **Не требует декомпозиции.**

**Сложность: 2** | **Приоритет: 1**

---

### 2.14. `services/weather_adjustment.py` — 508 LOC

#### Текущие ответственности (3):
1. `WeatherAdjustment` — coefficient calculation, skip detection, factor analysis
2. Settings loading from DB
3. Logging adjustments to weather_log

#### Оценка:
Хорошо структурирован. Единственная рекомендация — вынести factor calculators в отдельные стратегии если появятся новые факторы:

```
services/weather/
├── adjustment.py                # WeatherAdjustment (main class)
├── factors.py                   # RainFactor, FreezeFactor, WindFactor, HumidityFactor, HeatFactor
```

**Сложность: 2** | **Приоритет: 2**

---

## 3. Порядок рефакторинга (рекомендуемая очередность)

### Фаза 1: Quick wins (низкий риск, высокий эффект)

| Шаг | Файл | Действие | Время | Риск |
|-----|------|----------|-------|------|
| 1.1 | `templates/status.html` | Вынести CSS в `static/css/status.css` | 1ч | Минимальный |
| 1.2 | `templates/programs.html` | Вынести CSS в `static/css/programs.css`, JS в `static/js/programs.js` | 1ч | Минимальный |
| 1.3 | `services/monitors.py` | Разделить на 3 файла в `services/monitors/` | 2ч | Низкий |

### Фаза 2: Frontend декомпозиция (средний риск)

| Шаг | Файл | Действие | Время | Риск |
|-----|------|----------|-------|------|
| 2.1 | `static/js/status.js` | Вынести виджет погоды в `static/js/status-weather.js` | 2ч | Низкий |
| 2.2 | `static/js/status.js` | Вынести zones V2 в `static/js/status-zones-v2.js` | 3ч | Средний |
| 2.3 | `static/js/status.js` | Вынести actions в `static/js/status-actions.js` | 2ч | Средний |
| 2.4 | `static/js/status.js` | Устранить дублирование `updateStatusDisplay`/`refreshSingleGroup` | 3ч | Средний |
| 2.5 | `static/js/zones.js` | Вынести water meter в `static/js/zones-water-meter.js` | 2ч | Низкий |
| 2.6 | `static/js/zones.js` | Вынести CSV в `static/js/zones-csv.js` | 1ч | Минимальный |
| 2.7 | `static/js/zones.js` | Вынести фото в `static/js/zones-photos.js` | 1ч | Минимальный |

### Фаза 3: Backend декомпозиция (высокий приоритет, высокий риск)

| Шаг | Файл | Действие | Время | Риск |
|-----|------|----------|-------|------|
| 3.1 | `irrigation_scheduler.py` | Выделить `ZoneSequenceRunner` (общий код program/group) | 4ч | Высокий |
| 3.2 | `irrigation_scheduler.py` | Вынести recovery в `scheduler/recovery.py` | 2ч | Средний |
| 3.3 | `irrigation_scheduler.py` | Вынести zone_jobs в `scheduler/zone_jobs.py` | 2ч | Средний |
| 3.4 | `routes/system_status_api.py` | Разделить на 4 blueprint-файла | 2ч | Средний |
| 3.5 | `services/telegram_bot.py` | Разделить на `notifier.py` + `bot_runner.py` | 2ч | Средний |

### Фаза 4: Polish (низкий приоритет)

| Шаг | Файл | Действие | Время | Риск |
|-----|------|----------|-------|------|
| 4.1 | `db/migrations.py` | Вынести downgrade registry в отдельный файл | 1ч | Минимальный |
| 4.2 | `db/zones.py` | Generic field mapper для update_zone | 2ч | Низкий |
| 4.3 | `services/weather.py` | Выделить dataclasses если вырастет | — | — |

---

## 4. Оценка рисков

### Высокий риск ⚠️
- **`irrigation_scheduler.py`** — любая ошибка = пропущенный или бесконечный полив. Требуется 100% тестовое покрытие перед рефакторингом. Особое внимание к threading, cancel events, APScheduler jobs.
- **`static/js/status.js`** (zones V2 + actions) — оптимистичное обновление UI, SSE/SSR, множество race conditions.

### Средний риск ⚡
- **`routes/system_status_api.py`** — `api_status` используется UI каждые 5 сек, любые регрессии сразу видны.
- **Frontend JS** — отсутствие модульной системы, все через `window.*` globals.

### Низкий риск ✅
- **CSS extraction** — не влияет на логику.
- **`services/monitors.py`** — три изолированных класса, разделение по файлам тривиально.
- **`db/migrations.py`** — append-only by nature.

---

## 5. Архитектурные замечания

### 5.1. Общие антипаттерны
1. **Copy-paste рендеринг** — `refreshSingleGroup` копирует 70% `updateStatusDisplay`. Нужна единая функция `renderGroupCard(group)`.
2. **Inline HTML в JS** — огромные строки HTML генерируются конкатенацией. На ARM без бандлера template literals — единственный вариант, но можно выделить `buildGroupCardHTML(group)`, `buildZoneCardHTML(zone)` helper-функции.
3. **God-функция `_run_program_threaded`** — цикл зон с 8+ вложенными try/except. Нужно выделить шаги: `_pre_check_zone()`, `_start_zone()`, `_wait_zone()`, `_stop_zone()`.
4. **Отсутствие модульной системы в JS** — на ARM без webpack/rollup можно использовать `<script>` ordering + namespace pattern.

### 5.2. Специфика ARM/WirenBoard
- **НЕ** использовать webpack/vite/esbuild — ARM слишком медленный для node.js tooling
- **НЕ** создавать слишком много мелких JS-файлов — каждый `<script src>` = HTTP request
- Оптимальный баланс: 5-8 JS файлов на страницу (вместо 1 монолита)
- Для production: простой `cat *.js > bundle.js` через Makefile

### 5.3. Метрики до/после (целевые)

| Метрика | Сейчас | Цель |
|---------|--------|------|
| Макс. LOC в файле (JS) | 2187 | <500 |
| Макс. LOC в файле (Python) | 1365 | <400 |
| Макс. LOC в функции | ~200 | <50 |
| CSS в HTML шаблонах | 2400 LOC | 0 |
| Inline JS в HTML | 546 LOC | 0 |
| Дублирование кода | ~300 LOC | <50 LOC |

---

## 6. Заключение

Проект wb-irrigation вырос органически и накопил технический долг в виде god-файлов. Основные проблемы:

1. **Frontend:** два JS-файла >2000 LOC с 8+ ответственностями каждый
2. **Backend:** `irrigation_scheduler.py` — god-class с дублированием между program/group runner
3. **Templates:** CSS inline в HTML — 2400+ строк
4. **Мониторинг:** 3 несвязанных класса в одном файле

Рекомендуемый подход — инкрементальная декомпозиция, начиная с безрисковых CSS extractions (фаза 1) и заканчивая рефакторингом scheduler (фаза 3). Каждый шаг должен сопровождаться тестами и проверкой на целевом ARM-устройстве.

**Общая оценка трудозатрат:** ~40-50 часов (без написания новых тестов).
