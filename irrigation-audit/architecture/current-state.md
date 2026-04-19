# Current State Architecture — wb-irrigation refactor/v2

**Дата:** 2026-04-19
**Архитектор:** software-architect
**Scope:** оценка качества декомпозиции + системные проблемы (не целевая архитектура)
**Source of truth:** findings/*.md, landscape/*, ARCHITECTURE-REPORT.md

---

## Executive Summary

### Общая оценка: **4.5 / 10** (против авторских 5/10)

Автор ставит 5/10 и это честно. Я ставлю чуть ниже — **4.5/10** — по одной причине: с точки зрения архитектора декомпозиция не оценивается только по LOC в `app.py` и числу извлечённых пакетов. Она оценивается по **завершённости контрактов между слоями** и **единственности источника правды для ключевых ресурсов**. По обоим критериям рефакторинг находится в состоянии "посреди реки" — старые структуры не снесены, новые не закрыты, и у большинства критических ресурсов (MQTT, scheduler, monitors, config, logging) **два владельца одновременно**.

При этом: прод работает, uptime 13.5 дней, RSS стабильный 224.8 MB, графы пользователей (zones, programs, weather) — функциональны. Это не архитектурная катастрофа. Это **незаконченный рефакторинг с хорошим фундаментом и плохими швами**.

### Топ-3 архитектурных root causes

1. **Незакрытые переходы / двойное владение ресурсами.**
   У каждого крупного компонента есть артефакт "во время рефакторинга": `scheduler/` пакет + `irrigation_scheduler.py` (1365 LOC, 89-LOC дубликат jobs), SSE hub живёт в бэкенде при отключённом фронте (бесконечный reconnect → 204), `mqtt_pub._MQTT_CLIENTS` кэш vs per-request `mqtt.Client().connect()` в `routes/system_status_api.py` (≈17k TCP-сессий/сут), `monitors.py` ↔ `monitors/` пакет, `database.py` Facade 309 LOC поверх `db/` репозиториев. Ни у одного из этих компонентов нет явного owner-module; любая фича попадает одновременно в два места.

2. **Границы слоёв текут вниз (layer violations).**
   Репозиторный слой не является единственной точкой входа в БД: 8 прямых `sqlite3.connect()` в обход `BaseRepository` (7 в `weather.py`, 1 в `float_monitor.py`). PRAGMA (FK, synchronous=NORMAL, busy_timeout) не применяются per-connection в `_connect()`, поэтому на проде FK=OFF и `synchronous=FULL`. Роуты местами содержат бизнес-логику (inline boot-sync в `services/app_init.py`, сохранение состояний в `routes/zone_control.py`). Абстракция "Repository" существует только для тех, кто её использует добровольно.

3. **Нет централизованной ownership инфраструктурного состояния.**
   Конфиг размазан по 5 источникам: `config.py`, `constants.py`, `.env`, `os.environ`, `db.settings` (application-level settings **в БД**, что ломает idempotency развёртывания). Logging конфигурируется и в `logging_setup.py`, и через early `basicConfig()` в scheduler, и точечно в модулях. MQTT-топология (18 клиентов на брокере на 1 процесс) не имеет координатора. CI/CD указывает на `main`, прод крутится на `refactor/v2`. Cloudflare tunnel напрямую на `localhost:8080` (Flask) в обход `basic_auth_proxy` на :8011 — security boundary нарушена архитектурно, не случайно.

### Что не трогать (короткий список)

- Репозиторный слой в `db/` — архитектурно корректный, просто неполный.
- Разделение `routes/` на page routes vs JSON API — правильное, можно продолжать.
- `monitors/` пакет — самый зрелый из выделенных (float, mqtt-status, weather).
- `services/locks.py` namespaced locks — хорошая абстракция concurrency.
- Graceful shutdown, SSR + asset versioning, MockMQTTClient тестовая инфраструктура, optimistic UI с rollback — все это работающие решения, их регресс будет больнее любой архитектурной чистки.

---

## 1. Decomposition Quality (по слоям)

### 1.1 `routes/` — 18 файлов, извлечено из `app.py` (4200 → 445 LOC)

**Хорошо:**
- Разделение page routes (`routes/pages.py`) и JSON API (`routes/*_api.py`) — правильный шаг к "thin controllers".
- `before_request`/`after_request` централизованы.
- Большинство эндпоинтов ≤ 50 LOC.

**Плохо:**
- **Бизнес-логика просачивается в роуты**: `routes/zone_control.py` (coverage 48%) содержит логику валидации и сохранения состояний; `routes/zones_watering_api.py` (49%) делает координацию scheduler+MQTT+DB прямо в handler'е. Это не "routes as adapters".
- **Прямое обращение в DB/MQTT поверх сервисов** — по findings есть случаи, когда роут тянет `db.*` минуя service facade.
- **`routes/system_status_api.py:464-478`** — создаёт **новый** MQTT-клиент per-request (`mqtt.Client().connect()`), в обход кэша `mqtt_pub._MQTT_CLIENTS`. Это не просто perf bug (API-01), это архитектурная течь: роут принимает решения об infrastructure-уровне.
- Нет единого формата ошибок/ответов между старыми (извлечёнными) и новыми роутами.

**Вердикт по слою:** 6/10. Скелет хороший, но thin-controller'ов пока нет.

### 1.2 `services/` — ~30 файлов

**Хорошо:**
- Есть попытки разделить infrastructure (`services/mqtt_pub.py`, `services/locks.py`) и application (`services/app_init.py`, `services/notifications.py`).
- `services/locks.py` namespaced locks — хорошая инкапсуляция concurrency.

**Плохо:**
- **`services/app_init.py` содержит boot-sync дубликат** с `irrigation_scheduler.py` (CQ-006, BUG 5.2): один и тот же код инициализации зон/программ живёт в двух местах. При старте оба могут выполниться — state drift детерминирован.
- **Слой не гомогенный**: одни сервисы — stateless утилиты (`services/notifications.py`), другие — singletons с in-memory state (`services/mqtt_pub.py` с `_MQTT_CLIENTS` dict), третьи — оркестраторы (`services/app_init.py`). Нет единой дисциплины lifecycle.
- **`services/` нет owner для MQTT-клиента**: `mqtt_pub` управляет pub-клиентами, но sub-клиенты (monitors, hubs) создаются независимо. Итог — 18 клиентов на брокере.

**Вердикт по слою:** 5/10. "Services" здесь — скорее "всё, что не роут и не БД", а не слой с контрактами.

### 1.3 `scheduler/` — 6 mixin-файлов, 1258 LOC **+** `irrigation_scheduler.py` 1365 LOC

**Это главная архитектурная дыра.**

- Автор начал расщеплять `irrigation_scheduler.py` (1365 LOC — god-module) на пакет `scheduler/` через mixin-паттерн (`scheduler/jobs.py`, `scheduler/control.py`, ...). Рефакторинг **не завершён**: старый модуль не удалён, **89 LOC дублируются** между `scheduler/jobs.py` и `irrigation_scheduler.py:67-155`.
- **MemoryJobStore fallback** — SQLAlchemy отсутствует на проде (landscape/prod-snapshot.md), при рестарте все запланированные задачи теряются. Это не архитектурное решение, это деградация в рантайме, которую никто не замечает.
- Mixin-паттерн сам по себе — красный флаг для архитектора: scheduler одновременно "наследник" и "компонент", тестировать/мокать сложно.
- **Owner ambiguity**: добавить новое расписание нужно одновременно трогать `scheduler/jobs.py` и `irrigation_scheduler.py` — гарантированный merge-conflict / state-drift.

**Вердикт по слою:** 3/10. Пакет существует, но god-module не удалён. Это состояние хуже, чем один god-module, — потому что теперь их **полтора**.

### 1.4 `db/` — 9 файлов, репозиторный паттерн

**Хорошо:**
- `BaseRepository` — правильная абстракция.
- Разделение репозиториев по агрегатам (zones, programs, weather, settings...) — корректно.
- WAL mode включён, busy_timeout настроен.

**Плохо:**
- **`database.py` (309 LOC)** — Facade поверх `db/`. Создаёт **двойную точку входа**: можно звать `database.get_zones()` или `db.zones.list_all()`. Два контракта на один аргегат.
- **8 прямых `sqlite3.connect()`** обходят репозитории (7 в `weather.py`, 1 в `float_monitor.py`) — репозиторная граница не обязательна.
- **PRAGMA не устанавливаются per-connection в `_connect()`**: прод работает с `FK=OFF` и `synchronous=FULL` (database.md). Это означает, что `BaseRepository._connect()` не является единственной точкой конфигурации соединения — кто-то где-то коннектится мимо.
- **Миграции не атомарны** (database.md) — схема может оказаться в промежуточном состоянии при прерывании.
- **`db.settings` хранит application-level настройки в БД** (config scatter). Неотделимо от data migrations.

**Вердикт по слою:** 5.5/10. Репозитории правильные, но не обязательные — в этом вся проблема.

### 1.5 `monitors/` — пакет + `monitors.py`

**Хорошо:**
- Самый зрелый extracted пакет: `float_monitor`, `mqtt_status_monitor`, `weather_monitor` каждый — единица с жизненным циклом.
- Явные start/stop, threads named.

**Плохо:**
- Сосуществует с legacy `monitors.py` (owner-ambiguity, как и со scheduler, только в меньшем масштабе).
- `float_monitor` использует **прямой `sqlite3.connect()`** — нарушение layer boundary (см. db/).
- Нет общего контракта `Monitor` (start/stop/health) — каждый свой интерфейс.

**Вердикт по слою:** 6.5/10. Лучший extracted package, но ещё не закрыт.

### 1.6 Frontend — Jinja SSR + vanilla JS

**Хорошо:**
- SSR-first подход — здравое решение для ARM-устройства.
- Asset versioning работает.
- Optimistic UI с rollback в zones.js — правильный UX-паттерн.

**Плохо (FE-CRIT-1, findings/frontend.md):**
- **`zones.js:1472` коннектится к `/api/mqtt/zones-sse`** → сервер отвечает 204 → клиент **бесконечно реконнектится**. SSE отключили на фронте, но код остался активным. Архитектурный рассинхрон.
- **SSE-хаб жив в бэкенде**, пишет в SQLite на каждое MQTT-событие — нагрузка без потребителя.
- **113 KB dead JS** (components/* не используются).
- **545 LOC inline `<script>` в `programs.html`** — дублирует логику из модулей, делает rollback невозможным.
- Нет bundler'а — выбор оправдан для edge-устройства, но отсутствие lint/format делает dead-code detection ручным.

**Вердикт по слою:** 4/10. UX-решения хорошие, но ключевой real-time канал (SSE) находится в "живом мёртвом" состоянии.

---

## 2. Systemic Problems (8 ключевых)

| # | Проблема | Root cause | Источник | Владелец-кандидат |
|---|----------|------------|----------|-------------------|
| SP-1 | **Два источника правды для MQTT-клиента** (`mqtt_pub` кэш vs per-request `mqtt.Client().connect()` в `system_status_api.py`) | Нет единой ownership infrastructure-ресурса; роут решает infra-вопрос | performance.md API-01; code-quality.md | `services/mqtt_pub` as sole entrypoint |
| SP-2 | **SSE архитектурный рассинхрон** (фронт disabled, бэк-хаб жив + 204-реконнект loop) | Незавершённое выключение фичи | frontend.md FE-CRIT-1; performance.md SSE-01 | frontend-lead + services/sse |
| SP-3 | **Scheduler dual-track** (`scheduler/` pkg + `irrigation_scheduler.py` 1365 LOC, 89 LOC дубль) | Незавершённая декомпозиция god-модуля | code-quality.md; sre.md | scheduler/ пакет, legacy удалить |
| SP-4 | **Config scattered across 5 sources** (`config.py`, `constants.py`, `.env`, `os.environ`, `db.settings`) | Нет единого config-owner, settings мигрировали в БД | code-quality.md; sre.md | новый `config/` модуль, pydantic-settings |
| SP-5 | **Logging reset множественно** (`logging_setup.py` + early `basicConfig()` + модульные overrides) | Нет идемпотентного init; race при import order | sre.md (logging root cause) | `logging_setup` как единственный init, guard |
| SP-6 | **CI/CD mismatch** (GitHub Actions hardcoded `main`, прод на `refactor/v2`) | Ветка-кандидат стала прод без миграции пайплайна | tests.md; prod-snapshot.md | CI пайплайн branch-aware |
| SP-7 | **Cloudflare tunnel bypass** (CF → `localhost:8080` Flask, в обход `basic_auth_proxy` на :8011) | Security boundary обошли быстрым решением | security.md | nginx / CF → :8011 only |
| SP-8 | **Layer bypass в БД** (8 прямых `sqlite3.connect()`, PRAGMA не per-connection) | Репозитории не обязательны, Facade `database.py` создаёт альтернативу | database.md; code-quality.md | `BaseRepository._connect()` как единственный путь |

Дополнительно (вторичные, но системные):
- **XPASS limbo**: 52 xfail-теста проходят (tests.md) — маркеры-зомби, нет процесса их утилизации.
- **Boot-sync дубликат** (`services/app_init.py` vs `irrigation_scheduler.py`) — частный случай SP-3.
- **Monitors dual-ownership** — частный случай SP-3 в миниатюре.

---

## 3. Module Ownership Matrix

Ключевые функции системы и их фактические "владельцы" (где живёт логика) + разрывы.

| Функция | Текущий путь (код) | Разрыв / Gap |
|---------|--------------------|---------------|
| Публикация MQTT-команд | `services/mqtt_pub.py` + per-request `mqtt.Client()` в `routes/system_status_api.py:464-478` | Два owner'а. Роут принимает infra-решение. |
| Подписка на MQTT-топики | `monitors/`, `services/sse/*`, `scheduler/` — каждый свой клиент | 18 клиентов на брокере на 1 процесс. Нет coordinator. |
| Запуск поливной задачи | `scheduler/jobs.py` **и** `irrigation_scheduler.py:67-155` | 89 LOC дубль. Оба могут выстрелить. |
| Boot-sync состояний | `services/app_init.py` + `irrigation_scheduler.py` | CQ-006 / BUG 5.2 — два init-пути. |
| Чтение/запись зоны | `db/zones.py` (правильно) **+** `database.py` facade **+** прямые `sqlite3.connect()` в weather.py | Три пути. Репозиторная граница не обязательна. |
| Сохранение weather-данных | `weather.py` напрямую через `sqlite3.connect()` x7 | Обход `db/weather.py`. PRAGMA не применяется. |
| Config / settings | `config.py` + `constants.py` + `.env` + `os.environ` + `db.settings` | 5 источников. Нет приоритетов. |
| Logging | `logging_setup.py` + early `basicConfig()` в scheduler + модульные | Init неидемпотентен. |
| Health / status API | `routes/system_status_api.py` (создаёт MQTT client сам) | Нарушение layered arch. |
| SSE real-time | `services/sse/hub` (жив) + `zones.js` (disabled, но реконнектится) | Owner никто. Нужно решение: включить или удалить. |
| Планирование (APScheduler) | APScheduler + MemoryJobStore fallback | Persistence отсутствует — SQLAlchemy не установлен. Деградация тихая. |
| Float/tank monitoring | `monitors/float_monitor.py` (прямой sqlite3) | Обход `db/`. |
| Deploy | CI/CD (main) + прод (refactor/v2) | Пайплайн не запускается. |
| Security boundary | nginx + `basic_auth_proxy` + CF tunnel (прямо на :8080) | CF bypass. |

---

## 4. Cohesion & Coupling (топ-модули)

Метрика качественная: LCOM-идея через fan-in/fan-out и "сколько ортогональных ответственностей в одном модуле".

| Модуль | LOC | Ответственности | Оценка cohesion | Coupling |
|--------|-----|-----------------|------------------|----------|
| `weather.py` | **1404** | HTTP-fetch провайдеров + парсинг + кэш + DB (прямой sqlite3) + business-rules для forecast + интеграция со scheduler | **Очень низкая cohesion**, **высокий coupling** (6+ ответственностей, прямой доступ в БД) | Масштаб god-module. Кандидат на раздел на `weather/{fetch,parse,cache,repo,rules}`. |
| `irrigation_scheduler.py` | **1365** | Scheduler-орchestration + boot-sync + dup jobs + state sync + зависимости на MQTT/DB | **Очень низкая cohesion** (mixing policy и mechanism) | Дублирует `scheduler/` пакет. |
| `services/app_init.py` | ~n/a | Bootstrap + boot-sync + init-сервисов + (частично) health | Средняя; смешивает DI-сборку и данные | Тянет на себя scheduler + mqtt + db одновременно. |
| `routes/system_status_api.py` | ~n/a | HTTP handler + MQTT-client lifecycle + аггрегация health | **Низкая cohesion** (роут содержит infra-lifecycle) | Создаёт MQTT-клиент сам. |
| `database.py` (facade) | 309 | Facade на db/ репозитории + историческая прямая работа | Средняя; ломает правило "одна точка входа" | Дублирует `db/`. |
| `mqtt_pub.py` | ~n/a | Кэш pub-клиентов + публикация | Высокая cohesion, но scope только pub (sub — у других) | Хороший пример компонента с чётким scope. |
| `db/base.py` (`BaseRepository`) | ~n/a | Соединение + базовые query | Высокая cohesion | Coupling низкий; НО PRAGMA не гарантированы. |
| `services/locks.py` | ~n/a | Namespaced locks | Высокая cohesion, низкий coupling | Хороший компонент. |
| `monitors/float_monitor.py` | ~n/a | Thread + MQTT sub + DB (прямой sqlite3) | Средняя; смешивает concurrency и persistence | Обходит `db/`. |

**Circular dependencies:** явных import-циклов в текущем срезе не видно (автор указывает на это в ARCHITECTURE-REPORT), **но логические циклы** есть: `services/app_init` → `irrigation_scheduler` → `services/mqtt_pub` → (через sub-monitors) обратно в `services/app_init` boot-sync. Физически разорвано lazy-импортами, логически — цикл.

---

## 5. Testability Root Causes

Coverage 60.78% (tests.md). Критические пути подкрыты слабо: `routes/zone_control.py` 48%, `routes/zones_watering_api.py` 49%.

**Почему тесты тяжело писать (архитектурные причины):**

1. **Невозможность замокать infra-ресурс через seam.** MQTT-клиент создаётся в трёх местах (`mqtt_pub`, per-request в роуте, в каждом монитор/scheduler). Нет единой точки DI — подменить клиент в тесте = лезть в 3 файла. Поэтому тесты критических роутов не пишут — дорого.
2. **God-модули.** `weather.py` 1404 LOC / `irrigation_scheduler.py` 1365 LOC невозможно покрыть unit-тестами; только integration. MockMQTTClient спасает, но integration тесты медленные и flaky.
3. **Нет границ: роуты делают инфраструктуру.** `system_status_api.py` создаёт MQTT внутри handler'а → чтобы протестировать роут, нужно замокать mqtt на уровне импорта.
4. **SQLite + прямые `sqlite3.connect()`.** Тесту сложно подменить БД — потому что часть модулей коннектится сама, в обход фикстур.
5. **XPASS limbo (52 теста).** Маркеры `xfail` на проходящих тестах сигнализируют, что никто не убирает "подкрученные" маркеры после фиксов. Это процессный, но по своей природе архитектурный симптом: код меняется, контракты тестов — нет.
6. **CI не запускается для `refactor/v2`** — обратная связь тестов не работает вовсе. Любой refactor летит вслепую.
7. **4 детерминированные SSE-регрессии** (tests.md) никто не чинит, потому что SSE в "мёртво-живом" состоянии — см. SP-2.

---

## 6. Evolvability Assessment

Насколько легко добавить типовую фичу?

| Сценарий | Сложность | Почему |
|----------|-----------|--------|
| Новая зона с расписанием | **Средне** (4-5 мест) | `db/zones.py`, `routes/zones_*`, `scheduler/jobs.py` **и** `irrigation_scheduler.py`, `services/app_init` boot-sync. |
| Новый MQTT-топик подписки | **Тяжело** | Нужно выбрать: новый монитор? расширить существующий? добавить SSE-хаб? Нет гайдлайна. 18 клиентов и так. |
| Новый HTTP-эндпоинт (JSON API) | **Легко** | Роуты + сервис — паттерн устоялся. |
| Изменить формат ошибок API | **Тяжело** | Нет единого error-envelope между старыми и новыми роутами. |
| Поменять провайдер погоды | **Тяжело** | `weather.py` 1404 LOC, всё смешано, DB inline. |
| Добавить метрики / Prometheus | **Тяжело** | Нет единого places, где infra lifecycle виден: где регистрировать экспортёры? |
| Выключить фичу через flag | **Нет FF-системы** | Feature flags отсутствуют, конфиг в БД частично выполняет роль, но не системно. |
| Сменить БД (PG) | **Очень тяжело** | 8 прямых `sqlite3.connect()` + `database.py` facade + `db/` — три слоя, которые придётся переписывать. |
| Отключить SSE полностью | **Легко на фронте (сделано), нетривиально на беке** | Хаб продолжает работать. |
| Включить SSE обратно | **Очень тяжело** | Тесты (4 регрессии), нагрузка, per-event SQLite writes — всё требует аудита. |

**Evolvability grade:** **4/10**. Легко добавлять CRUD. Любое изменение infrastructure-уровня (MQTT, scheduler, БД, конфиг) требует прыжков через 2-3 owner'а.

---

## 7. Comparison with Author Assessment (ARCHITECTURE-REPORT.md)

Автор: **5/10**. Honest self-assessment, согласуется с артефактами.

| Тема | Автор | Я | Расхождение |
|------|-------|---|--------------|
| Общая оценка | 5/10 | 4.5/10 | Minor. Мой взгляд строже, потому что учитываю незакрытый scheduler и SSE-рассинхрон. |
| Decomposition done | "app.py 4200→445 LOC" | Подтверждаю, но god-модули переехали в `weather.py` / `irrigation_scheduler.py` | Нет. Автор упоминает. |
| Основной долг | "незавершённый рефакторинг scheduler" | SP-3 + SP-1 + SP-5 (это трилогия одного паттерна — dual-track ownership) | Я обобщаю это как системный паттерн, автор видит точечно. |
| Config | "scattered" | SP-4, 5 источников, settings-in-DB | Совпадает. |
| Тесты | "coverage низкий на critical paths" | 60.78%, 48-49% на zone_control/watering_api, CI не запускается | Совпадает, я добавляю CI/CD mismatch. |
| Безопасность | упоминается | CF tunnel bypass — архитектурная, не случайная | Я выделяю сильнее. |
| SSE | не главный фокус | SP-2 — архитектурный рассинхрон, не просто bug | Я считаю критическим. |
| MQTT per-request | обычный perf bug | SP-1 — симптом отсутствия ownership | Я считаю архитектурным. |

**Итог:** автор адекватно оценил состояние. Мои +0.5 вниз — это архитектурный взгляд на **системность** проблем, которые автор перечислил точечно.

---

## 8. High-level Roadmap (архитектурный, не тактический)

Порядок важен. Каждый шаг разблокирует следующий.

### Этап 0 — Починить обратную связь (1-2 дня)
- CI/CD trigger на `refactor/v2` (SP-6). Без этого refactor летит вслепую.
- Убрать 52 XPASS или переклассифицировать.
- Починить 4 детерминированные SSE-регрессии или удалить их вместе с SSE-фичей.

### Этап 1 — Закрыть infra-ownership (1-2 недели)
- **MQTT** (SP-1): `services/mqtt_pub` — единственный entry. Удалить per-request `mqtt.Client()` в `system_status_api`. Свести 18 клиентов к ≤ 3 (pub / sub-monitors / sub-scheduler).
- **Logging** (SP-5): `logging_setup` idempotent guard, убрать early `basicConfig()`.
- **Config** (SP-4): один `config/` модуль с pydantic-settings; `db.settings` — только user-editable.
- **Security boundary** (SP-7): CF → `:8011` (basic_auth_proxy), не :8080. Feature-flag для переключения.

### Этап 2 — Закрыть переходы (2-3 недели)
- **Scheduler** (SP-3): удалить `irrigation_scheduler.py` после миграции 89 LOC дубля. APScheduler с SQLAlchemy jobstore (или осознанно принять MemoryJobStore как режим).
- **SSE** (SP-2): **решение** — включить или удалить. Оба варианта приемлемы архитектурно, "живой труп" — нет.
- **Monitors**: финализировать пакет, удалить `monitors.py`.
- **DB facade**: удалить `database.py`, переориентировать на `db/` репозитории. 8 прямых `sqlite3.connect()` → `BaseRepository`. PRAGMA в `_connect()`.

### Этап 3 — Разобрать god-модули (месяцы)
- `weather.py` 1404 LOC → `weather/{fetch,parse,cache,repo,rules}`.
- `routes/zone_control.py` / `routes/zones_watering_api.py` — thin controllers, логику в `services/watering/`.

### Этап 4 — Evolvability (не срочно, но оно того стоит)
- Единый error-envelope API.
- Feature flags (хоть бы простые).
- Monitor interface (`start/stop/health`).
- Prometheus /metrics.

**Принцип roadmap:** ни один этап не добавляет новых слоёв. Только **закрывает незакрытые** и **удаляет legacy**. Добавлять новое (event bus, CQRS, etc.) до закрытия этапов 0-2 — архитектурная ошибка.

---

## 9. What Works — Don't Break

Перечисление того, что нужно сохранить при любой чистке.

1. **`db/` репозиторный слой** — правильный, нужно сделать **обязательным**, не переписывать.
2. **Разделение `routes/` на page vs JSON API** — продолжать, не откатывать.
3. **`monitors/` пакет** — самый зрелый extract; образец для остальных.
4. **`services/locks.py` namespaced locks** — хорошая concurrency-примитив.
5. **Graceful shutdown** (prod uptime 13.5 дней подтверждает стабильность).
6. **SSR + Jinja + asset versioning** — правильный выбор для ARM edge-устройства. Не тянуть SPA/bundler.
7. **Optimistic UI с rollback в `zones.js`** — хороший UX-паттерн.
8. **MockMQTTClient + тестовая инфраструктура** — единственная опора для тестирования MQTT-слоя.
9. **WAL mode + busy_timeout на SQLite** — правильные defaults, просто не везде применяются.
10. **APScheduler как выбор движка** — адекватно для scale проекта; проблема не в APScheduler, а в отсутствии SQLAlchemy jobstore.
11. **Jinja SSR для `programs.html`** — сам подход правильный; проблема — 545 LOC inline, не SSR как таковой.
12. **basic_auth_proxy на :8011** — существует и работает; проблема — CF tunnel его не использует.

---

## 10. Summary (для финального отчёта)

- **Архитектурная оценка: 4.5/10** (автор: 5/10). Разница в системном взгляде, не в фактуре.
- **Top-3 root causes:**
  1. Незакрытые переходы / двойное владение ресурсами (scheduler dual-track, MQTT dual-owner, SSE живой труп, monitors.py vs monitors/, database.py vs db/).
  2. Границы слоёв текут вниз (репозитории необязательны, 8 прямых sqlite3.connect, роуты делают infra-lifecycle, PRAGMA не per-connection).
  3. Нет центральной ownership инфраструктурного состояния (config в 5 местах, logging reset дважды, MQTT 18 клиентов без координатора, CI/CD на чужой ветке, CF tunnel в обход security-прокси).
- **Что не трогать:** `db/` репозитории, routes split, `monitors/` пакет, `services/locks.py`, graceful shutdown, SSR, optimistic UI, MockMQTTClient, WAL.
- **Roadmap:** Этап 0 (CI/тесты) → Этап 1 (infra ownership) → Этап 2 (закрыть переходы) → Этап 3 (god-модули) → Этап 4 (evolvability).
- **Путь к артефакту:** `/opt/claude-agents/irrigation-v2/irrigation-audit/architecture/current-state.md`
