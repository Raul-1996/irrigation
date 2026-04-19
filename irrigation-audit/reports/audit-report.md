# wb-irrigation — Сводный аудит-отчёт (Phase 4)

**Дата:** 2026-04-19
**Аудитор:** command-team под руководством Tony (incident-response commander)
**Ветка:** `refactor/v2`, prod @ `e37adb7` (WB-Techpom Wirenboard, `10.2.5.244`)
**Scope:** консолидация 13 артефактов Phase 1–3 в одну точку правды для владельца (Raul)
**Статус:** финальный, готов к передаче заказчику

---

## Содержание

1. TL;DR — одним абзацем
2. Состояние по доменам (светофор)
3. **Физические риски** (отдельно, повышенный приоритет)
4. Консолидированные находки (master-points)
5. Статус авторского BUGS-REPORT
6. Приоритизированный план действий (5 волн)
7. Quick wins (за час каждая)
8. Что НЕ трогать
9. Открытые вопросы для владельца
10. План исполнения Phase 5
11. Метрики успеха

---

## 1. TL;DR

Система **работает**: прод держит uptime 13.5 дней, RSS стабильный 224 MB, CPU 0.27%, функциональный UX полива 24 зон закрыт. Это **не архитектурная катастрофа** и **не аварийная ситуация** — это **незавершённый рефакторинг с одним физически-опасным классом багов** (БД↔hardware расхождение) и **одним громким процессным провалом** (CI/CD никогда не запускался на `refactor/v2`, логи приложения 13 дней пустые, что делает диагностику любого инцидента слепой).

**Три больших пункта, которые нужно закрыть до конца квартала:**

1. **Reconciliation между desired-state в БД и observed-state на клапане** — сейчас их нет, при power loss или MQTT-disconnect клапан может остаться открытым без ведома приложения (пролитая вода, сухой ход насоса).
2. **Логирование**: `app.log` 0 байт 13 дней (bug #4 автора подтверждается) — любая будущая проблема будет расследоваться по воздуху. Это **первое**, что нужно починить — 4 часа работы.
3. **SSE-зомби**: фронт отключил SSE, бэк-хаб жив, браузер бесконечно реконнектится на эндпоинт, возвращающий 204 (≈28 800 запросов/сутки на клиента). Решение архитектора — выпилить полностью; подтверждение владельца блокирует ~5 мастер-пунктов.

Остальное (CSRF-exempt API на публичном URL, 8 прямых `sqlite3.connect` в обход репозиториев, scheduler-god-module с 89 LOC дублем, WCAG 2.2 Level A fail, 113 KB dead JS, 55 XPASS тестов в limbo) — чинится поэтапно за 3–4 месяца параллельно с регулярной работой.

**Общая оценка зрелости:** автор ставит 5/10, архитектор 4.5/10, я соглашаюсь с 4.5/10 с оговоркой: для **домашнего** контроллера полива эта оценка избыточно строгая — она применима к enterprise-мерке. Для домашнего single-user scope разумнее читать её как **«работает, но требует профилактики до осеннего сезона»**.

---

## 2. Состояние по доменам (светофор)

Светофор откалиброван под домашний single-user контекст, не под enterprise SaaS. 🟢 = не мешает, 🟡 = чинить этим кварталом, 🔴 = физический или диагностический риск, чинить сейчас.

| Домен | Статус | Что держит цвет |
|---|:-:|---|
| **Physical safety** (клапаны, насос, бак) | 🔴 | Нет reconciler БД↔hardware (CRIT-R1, R3, RC1, CC1); power-loss восстановление не закрывает сценарий «клапан физически открыт, приложение считает закрытым». |
| **Observability / диагностика** | 🔴 | `app.log` пустой 13 дней (Bug #4 автора); нет `/healthz`/`/readyz`/`/metrics`; `telegram.txt` 520 KB без ротации; systemd без `WatchdogSec`. |
| **Deploy pipeline** | 🔴 | GitHub Actions CI смотрит на `main`, прод на `refactor/v2` — пайплайн не запускался ни разу за время существования ветки; rollback не проверен. |
| **Security (public exposure)** | 🟡 | CF Tunnel идёт напрямую на Flask `:8080` в обход `basic_auth_proxy:8011` (SEC-002); все API-blueprint'ы CSRF-exempt, а app.js добавляет X-CSRFToken — «lying code»; default admin creds не форсятся; `login.html` доступен публично через CF. Для домашнего scope — нужен CF Access с OAuth, тогда это 🟢. |
| **Database integrity** | 🟡 | `PRAGMA foreign_keys=OFF` на проде (DB-001), `synchronous=FULL` вместо NORMAL (eMMC wear — DB-004), 8 прямых `sqlite3.connect()` в обход `BaseRepository` (DB-006, DB-007), миграции не атомарны (DB-002). Данных не теряем *сейчас*, но fault-tolerance ниже номинального. |
| **Scheduler (APScheduler)** | 🟡 | SQLAlchemy не установлен на проде → MemoryJobStore fallback → **все jobs теряются при рестарте** (HIGH-R3); `scheduler/` пакет + `irrigation_scheduler.py` 1365 LOC живут вместе, 89 LOC дубль в `jobs.py`. Boot-sync частично маскирует потерю. |
| **MQTT topology** | 🟡 | 18 клиентов на брокере anonymous; `mqtt_pub._MQTT_CLIENTS` кэш + per-request `mqtt.Client().connect()` в `routes/system_status_api.py:464-478` (~17 k TCP-сессий/сут); нет owner'a для подписок; топиковая таксономия не упорядочена. |
| **Frontend real-time (SSE)** | 🔴 | `zones.js:1472` коннектится к `/api/mqtt/zones-sse`, бэк отвечает 204, EventSource бесконечно реконнектится. ≈28 800 запросов/сутки на одну открытую вкладку. Cascaded: battery drain на мобилке, шум в логах (если бы логи работали), нагрузка на SQLite-write в `sse_hub._on_message`. |
| **Frontend JS hygiene** | 🟡 | 113 KB / 2243 LOC dead JS; 545 LOC inline `<script>` в `programs.html` дублирует модули; `status.js` 2187 LOC / 118 KB синхронная загрузка; 7/9 форм без double-submit protection; `var` 255× в одном файле. |
| **Accessibility (WCAG 2.2)** | 🔴 | **FAIL Level A** — 5 критичных (label-less inputs, toggle-switch без role/keyboard, sortable `<th>` без keyboard, SVG dial drag-only, нет focus trap в 8 модалках). Для полевого оператора это значит: одной рукой в перчатке управлять нельзя. `#999` на `#fff` — 2.85:1 (fail AA) повторён 25+ раз. |
| **Testing / CI feedback loop** | 🔴 | CI не запускается на `refactor/v2` вообще. 60.78% coverage, критические пути 48–49% (`zone_control`, `watering_api`). 55 XPASS в limbo (xfail-маркеры на проходящих тестах). 8 детерминированных failures, из них 4 — SSE regression. 44× `time.sleep()` → flaky на ARM. Hardcoded prod IP `10.2.5.244` в `test_mqtt_real.py`. |
| **Code quality (god-modules)** | 🟡 | `weather.py` 1404 LOC (fetch+parse+cache+DB+business rules всё в одном), `irrigation_scheduler.py` 1365 LOC. Не блокирует, но делает любую будущую правку хрупкой. |
| **Config / secrets** | 🟡 | 5 источников (`config.py`, `constants.py`, `.env`, `os.environ`, `db.settings`); TG-токен и MQTT-пароли — не в secure secrets; сервис крутится под `root` (systemd `User=root` по умолчанию). |
| **Performance** | 🟢 | Нет SLO-нарушений; RSS стабильный; CPU < 1%; SQLite не bottleneck. Основные perf-теги (per-request MQTT connect, SSE writes) — симптомы архитектурных проблем, сами по себе не критичны. |
| **Уже работающее** (не трогать) | 🟢 | Repository-pattern в `db/`, `monitors/` package, `services/locks.py`, graceful shutdown, SSR+Jinja+asset versioning, MockMQTTClient, WAL-mode, optimistic UI с rollback в `zones.js`. |

**Итоговый баланс:** 4 домена 🔴 (физика, диагностика, deploy, SSE, a11y, тесты), 8 доменов 🟡 (фиксим квартально), 1 домен 🟢 (не трогать) + 1 список того, что уже работает правильно.

---

## 3. ⚠️ Физические риски (elevated priority)

Это раздел про **воду, клапаны и насос** — где программная ошибка становится материальным ущербом. Вынесен отдельно потому, что в home-irrigation классе систем это **не «Severity-1»**, а **единственная Severity-0 категория**.

### PHYS-1 — Расхождение БД↔hardware по состоянию зоны (CRIT-R1 + CRIT-R3 + CRIT-RC1 + CRIT-CC1)

**Что:** в приложении нет механизма сверки «клапан в БД считается закрытым — он физически закрыт?». Информация о физическом состоянии клапана не приходит обратно в приложение. Есть четыре конкретных сценария пролива:

1. **Power loss посреди полива (CRIT-RC1).** Процесс падает, клапан остаётся открытым (нормально разомкнутые клапаны NC → закроются; **нормально замкнутые NO останутся открытыми**). При старте `boot_sync` в `services/app_init.py` и `irrigation_scheduler.py` полагаются на **БД** как источник правды, а не на GPIO echo. Если БД успела зафиксировать «полив начат», а stop-job в MemoryJobStore потерялся (HIGH-R3) — зона останется открытой и **нет таймера, который её закроет**.
2. **MQTT-disconnect в момент stop-команды (CRIT-R1).** `publish(..., qos=1)` без `wait_for_publish()` + без подтверждения от контроллера = команда «stop» могла не доехать. Приложение отмечает в БД `state='off'` оптимистично. Клапан продолжает лить.
3. **Race watchdog vs user-stop (CRIT-CC1).** Пользователь нажал Stop в UI, одновременно watchdog cap-time достиг лимита и тоже публикует stop. Два UPDATE на `zones` без optimistic lock — возможен порядок, при котором старое «on» перезатирает «off» состояние (`services/watchdog.py` + `routes/zone_control.py`).
4. **Fire-and-forget verify (HIGH-R2).** `services/observed_state.py` использует `verify_async` — thread'а-демон, которая никак не попадает в state machine зоны. Успех/провал verify **не влияет** на поле `state` в `zones`, и не ведёт к alert.

**Где:** `services/zone_control.py` (coverage 48% — не покрыто тестами), `routes/zones_watering_api.py` (49%), `services/observed_state.py`, `services/watchdog.py`, `services/app_init.py` (boot-sync), `irrigation_scheduler.py:67-155` (второй boot-sync, дубль), `scheduler/jobs.py`, БД таблица `zones` — нет полей `desired_state`/`commanded_state`/`observed_state`/`desired_version`.

**Почему это физический риск:** открытый клапан при работающем насосе → перелив грядки / затопление теплицы / сухой ход насоса если источник пустой (float отключает, **если float-sensor жив и MQTT доезжает** — сам float работает через тот же MQTT, который уже подозревается).

**Что делать (только направление, без кода):**
- Ввести четырёхуровневую модель состояния `desired → commanded → observed → confirmed` (target-state §3).
- Subscriber на `zone/+/observed` в MQTT (или эквивалентный echo-топик), который обновляет `observed_state` в БД.
- Периодический reconciler-job (каждые 10 сек) сравнивает desired vs observed, и при расхождении > ACK_TIMEOUT повторяет команду (идемпотентно через `command_id`).
- `boot_reconcile` при старте: сначала забрать retained snapshot `zone/+/observed` из MQTT, сравнить с `desired_state` в БД, **safety-first policy** — при любых сомнениях публиковать stop.
- Optimistic concurrency через `desired_version++` на каждом UPDATE (убирает race watchdog vs user).
- Удалить `_delayed_close` thread (CQ-007) и `verify_async` fire-and-forget (HIGH-R2), заменить на один reconciler.

**Effort:** L (2 спринта — target-state Phase 2, 2.1–2.7).
**Dependencies:** Phase 0 (logging fix), Phase 1 (MQTT contract + client_pool).

---

### PHYS-2 — Scheduler теряет one-shot stop-jobs при рестарте (HIGH-R3)

**Что:** APScheduler настроен на SQLAlchemyJobStore, но **SQLAlchemy не установлен на проде** (подтверждено `landscape/prod-snapshot.md`). APScheduler молча fallback'ит на MemoryJobStore — **все запланированные задачи теряются при `systemctl restart`**.

Самый опасный класс задач — `zone_stop:N` (one-shot date-trigger на момент конца полива). Если рестарт случился в середине полива (deploy, OOM, crash, power) — **stop-job исчез**, а клапан открыт.

**Где:** `irrigation_scheduler.py` (конфиг jobstore), `requirements.txt` (нет `SQLAlchemy`), в итоге `scheduler.add_job(..., jobstore='default')` без персистентности.

**Почему это физический риск:** прямая ссылка на PHYS-1 — страхующий mechanism `boot_reconcile` сейчас не существует, а даже если бы существовал, он не знает про cap-time («зона должна была закрыться в 14:33») без SQLAlchemyJobStore.

**Что делать:**
- Добавить `SQLAlchemy` в `requirements.txt` (+~3 MB RSS на ARM — приемлемо).
- Отдельный файл `jobs.db` (не смешивать с `irrigation.db` — упрощает backup).
- **НО**: не полагаться на SQLAlchemyJobStore как на единственную защиту. Critical one-shot jobs (zone_stop, master_close) всё равно восстанавливаются через `boot_reconcile` (PHYS-1) — jobstore это вторая линия.
- `coalesce=True, misfire_grace_time=300` — догнать пропущенный fire если процесс был в рестарте <5 мин.

**Effort:** S (2–4 часа установка + миграция скрипта запуска jobs).
**Dependencies:** ни от чего не зависит, может идти параллельно с PHYS-1.

---

### PHYS-3 — Float-sensor монитор обходит репозиторный слой (DB-006 частный случай)

**Что:** `services/float_monitor.py` использует прямой `sqlite3.connect()` в обход `BaseRepository._connect()`. Значит: `PRAGMA foreign_keys=ON`, `synchronous=NORMAL`, `busy_timeout=30000` **не применяются** к соединению float-монитора. Float-sensor — **safety-critical сенсор** (защита от сухого хода насоса).

**Где:** `services/float_monitor.py` (1 прямой connect), `db/` — нет `db/float.py` репозитория.

**Почему это физический риск:** если write float-event попадёт на busy-lock SQLite (WAL checkpointing, backup concurrent) — без `busy_timeout=30s` он упадёт с `SQLITE_BUSY` быстро, float-event не сохранится, reconciler не узнает что бак пустой.

**Что делать:**
- Создать `db/float.py` репозиторий, перевести `services/float_monitor.py` через него.
- Проверить что на всех записях float используется `BaseRepository._connect()` (с per-connection PRAGMA).

**Effort:** S (1 день).
**Dependencies:** Phase 0 (PRAGMA в `_connect()`).

---

### PHYS-4 — Weather-decision идёт мимо репозиториев, 7 прямых connect (DB-006 основной)

**Что:** `services/weather.py` (1404 LOC god-module) содержит 7 прямых `sqlite3.connect()`. Это не напрямую физический риск, но: weather decision «пропустить полив если идёт дождь» и «удлинить полив если жара» **влияет на то, сколько воды выльется**. Ошибка в weather-записи (например, cache не обновился, а связанная запись осиротела) → некорректное решение о пропуске/удлинении.

**Где:** `services/weather.py` (7 прямых sqlite3 calls), `db/weather.py` (не существует, должен появиться).

**Почему это физический риск (непрямой):** weather-adjustment может дать ratio > 1 (больше воды) — комбинация с PHYS-1 (нет reconciler) + PHYS-2 (stop-job потерян) = пролив с мультипликатором.

**Что делать:**
- Создать `db/weather.py` репозиторий, перенести 7 connect'ов туда.
- Разбить `services/weather.py` на `domain/weather/decision.py` (pure math), `infrastructure/weather_http/open_meteo.py` (HTTP), `db/weather.py` (persistence), `application/weather_service.py` (orchestration). См. target-state §1.3.

**Effort:** M (2 дня на repository + 2–3 спринта на полную декомпозицию god-module).
**Dependencies:** Phase 0 (PRAGMA).

---

### PHYS-5 — Manual API не требует confirm и не rate-limited (композитный)

**Что:** эндпоинты `/api/zones/N/start`, `/api/zones/N/stop`, `/api/programs/N/run` — CSRF-exempt, без rate-limit, без confirm-step. В сочетании с публичным URL `poliv-kg.ops-lab.dev` (CF Tunnel прямо на Flask, SEC-002) и отсутствием MFA это значит: утечка session cookie / XSS / подсмотренный пароль = атакующий может массово открыть все клапаны. Риск водной аварии — есть.

**Где:** `app.py:96-109` (CSRF exempts), `routes/zones_watering_api.py`, `routes/zone_control.py`, `routes/programs_api.py`, отсутствие `flask-limiter`.

**Что делать:**
- **В первую очередь — CF Access** (см. SEC-002 / §9 «Открытые вопросы» — блокирующий вопрос владельцу). OAuth-перед-Flask убирает анонимный доступ в одну строку DNS-конфига.
- `flask-limiter` ~50 KB: rate-limit на `*/start`, `*/stop` — 30 rpm/user.
- Убрать CSRF-exempt с session-authenticated эндпоинтов, оставить exempt только для API-key-authenticated (если появятся).

**Effort:** S (Phase 7 в target-state: CF Access 2 часа, rate-limiter полдня, CSRF ревизия 1 день).
**Dependencies:** решение владельца по CF Access (§9.1).

---

**Сводка физических рисков:**

| ID | Риск | Probability | Impact | Приоритет |
|---|---|---|---|---|
| PHYS-1 | БД↔hardware рассинхрон при disconnect/power-loss | Средняя (pragma sans reconciler ≈ 1–2 раза/год) | **Высокий** — пролив, возможен сухой ход | **P0** |
| PHYS-2 | Stop-jobs теряются при рестарте | Высокая (при каждом deploy) | **Высокий** — открытый клапан без таймера | **P0** |
| PHYS-3 | Float-sensor write на busy-lock | Низкая | Высокий — сухой насос | **P1** |
| PHYS-4 | Weather-decision с косвенной записью | Низкая | Средний | **P2** |
| PHYS-5 | Публичный API без confirm/rate-limit | Низкая (но постоянная) | **Высокий** — массовое открытие | **P1 после CF Access** |


---

## 4. Консолидированные находки (master-points)

Находки сгруппированы по **корневой причине**, не по агенту. Один root-cause = одна мастер-точка. Дубликаты между агентами помечены как `[cross-ref: ...]` — это подтверждение, не удвоение.

Severity откалибрована под домашний single-user scope:
- **C (Critical)** — физический риск или диагностический blackout; чинить эту неделю.
- **H (High)** — функциональный риск, теряются данные или UX сильно деградирован; чинить этот месяц.
- **M (Medium)** — технический долг с осязаемой стоимостью; чинить этот квартал.
- **L (Low)** — опциональная гигиена; когда-нибудь.

Effort: **S** ≤ 1 день, **M** ≤ 1 спринт (1 неделя), **L** > 1 спринт.

---

### MASTER-C1: Отсутствует reconciliation-контур БД↔hardware

**Severity:** Critical (физический риск)
**Effort:** L
**Priority:** P0

**Что:** Приложение оптимистично пишет в БД «зона открыта/закрыта», не проверяя, что контроллер реально выполнил команду. Нет поля `observed_state`, нет subscriber'а на echo-топик GPIO, нет периодического reconciler'а, нет optimistic-lock на UPDATE `zones`.

**Где:**
- `services/zone_control.py` (48% coverage, критический путь)
- `routes/zones_watering_api.py` (49% coverage)
- `services/observed_state.py` (`verify_async` fire-and-forget — HIGH-R2)
- `services/watchdog.py` (race с user-stop — CRIT-CC1)
- `services/app_init.py` (boot-sync)
- `irrigation_scheduler.py:67-155` (второй boot-sync — CQ-006 / BUG 5.2)
- Таблица `zones`: нет колонок `desired_state`, `desired_version`, `commanded_state`, `observed_state`, `observed_at`, `last_command_id`.

**Почему:** физический актуатор (клапан) не даёт приложению feedback. Корень — в MQTT-контракте (нет топика `zone/+/observed`), в БД-модели (нет 4-уровневого состояния) и в архитектуре (нет reconciler'а).

**Что делать (направление):**
- Ввести state machine `desired → commanded → observed → confirmed` (target-state §3).
- Отдельный subscriber на `wb-irrigation/zone/+/observed` из MQTT (или аналогичный echo-топик WB rule engine), обновляет `observed_state`.
- APScheduler-job `reconcile_tick` каждые 10 сек сравнивает desired vs observed; при расхождении > ACK_TIMEOUT повторяет команду идемпотентно через `command_id`; после 3 попыток — `state='fault'` + алерт.
- `boot_reconcile` при старте: snapshot retained `zone/+/observed`, сравнить с `desired_state` в БД, safety-first (при сомнениях — закрыть клапан).
- Optimistic lock: `UPDATE zones SET ... WHERE id=? AND desired_version=?` — убирает race watchdog vs user (CRIT-CC1).

**Cross-ref:** CRIT-R1, CRIT-R3, CRIT-RC1, CRIT-CC1, HIGH-R2, CQ-006, CQ-007, BUG 5.2 автора → одна master-точка.
**Dependencies:** MASTER-C3 (MQTT contract), MASTER-H2 (DB PRAGMA), MASTER-H1 (logging для диагностики reconciler-решений).

---

### MASTER-C2: Диагностический blackout — app.log пустой 13 дней (Bug #4 подтверждён + расширен)

**Severity:** Critical (диагностический)
**Effort:** S
**Priority:** P0

**Что:** `app.log` 0 байт, прод работает 13.5 дней, ни одной записи. Автор пометил это как Bug #4 в BUGS-REPORT — **подтверждаю + расширяю**:

Корневых причины две:
1. **Handler повешен на named logger `'app'`**, а `logger = logging.getLogger(__name__)` в модулях использует `__name__ = "services.xxx"` — сообщения идут не в тот logger. Handler на root исправил бы.
2. **`basicConfig(level=WARNING)` вызывается в `irrigation_scheduler.py` и `scheduler/jobs.py` на import-time**, **до** `logging_setup.py` → перебивает уровень. Это `CQ-012` из code-quality.

Дополнительно обнаружено (расширение Bug #4):
- В нескольких модулях (`routes/settings.py`, `services/locks.py`, `services/mqtt_pub.py`, `services/telegram_bot.py`) **нет `import logging`/`logger = ...`** — `logger.info(...)` вызовы там будут NameError'ами, которые **сейчас не стреляют только потому, что эти ветки не исполняются** (CQ-001..004). Если начнут — процесс упадёт на необработанном NameError.
- `telegram.txt` 520 KB без ротации — растёт неограниченно на eMMC.

**Где:**
- `logging_setup.py` (handler не на root)
- `irrigation_scheduler.py`, `scheduler/jobs.py`, `database.py` — три места с `logging.basicConfig()`
- `routes/settings.py`, `services/locks.py`, `services/mqtt_pub.py`, `services/telegram_bot.py` — отсутствует импорт logger
- `/opt/wb-irrigation/services/logs/telegram.txt` — 520 KB без logrotate

**Что делать:**
- Повесить handler на root logger; `logging_setup.setup_logging()` вызывать в `app.py` **до** любых импортов scheduler/services.
- Убрать `basicConfig` из scheduler/jobs/database.
- Добавить `import logging; logger = logging.getLogger(__name__)` в 4 модуля.
- logrotate для `telegram.txt` (size 1M, rotate 4, weekly).
- Structured JSON formatter с полями `correlation_id`, `zone_id`, `command_id` (target-state §6.2) — можно во вторую очередь.

**Cross-ref:** Bug #4 (автор), CRIT-O1 (SRE), CQ-001..004, CQ-012, HIGH-O1 telegram.txt rotation.
**Dependencies:** none — **first thing to fix**, всё остальное диагностически слепое до этого.

---

### MASTER-C3: MQTT — нет единого владельца, 18 клиентов, per-request connect, anonymous

**Severity:** Critical (связан с физическим и безопасностью)
**Effort:** M
**Priority:** P0

**Что:** MQTT-подсистема — главный архитектурный провал:
- `services/mqtt_pub.py` держит кэш publish-клиентов (`_MQTT_CLIENTS` dict), но `routes/system_status_api.py:464-478` **создаёт новый `mqtt.Client().connect()` per-request** → **~17 000 TCP-сессий/сутки** на брокере.
- 18 отдельных MQTT-клиентов живут в одном процессе (`mqtt_pub`, `monitors/*`, `services/sse/*`, `scheduler`, per-request route) — нет координатора, нет общего reconnect-policy.
- Все клиенты анонимные (broker без ACL) — любой в LAN может читать/писать любые топики.
- Нет топиковой таксономии — топики вида `/devices/.../controls/...` смешаны с ad-hoc топиками приложения.
- Нет идемпотентности команд — QoS=1 + reconnect может привести к double-delivery «start zone».
- `publish()` без `wait_for_publish()` — команды могут молча потеряться.

**Где:**
- `services/mqtt_pub.py` — pub-клиент кэш
- `routes/system_status_api.py:464-478` — per-request `mqtt.Client().connect()` (API-01)
- `monitors/float_monitor.py`, `monitors/mqtt_status_monitor.py`, `monitors/weather_monitor.py` — каждый свой sub-client
- `services/sse_hub.py` — ещё один клиент
- `irrigation_scheduler.py`, `scheduler/*.py` — свои подключения
- Mosquitto на `10.2.5.244:1883` — нет ACL, `allow_anonymous true`

**Что делать:**
- Единственный `infrastructure/mqtt/client_pool.py`: dict `{broker_id: PahoClient}`, lazy init, один `loop_start()`, health-check через `is_connected()`.
- Удалить per-request connect в `system_status_api.py`.
- Свести 18 клиентов к ≤ 3 ролям: `pub`, `sub-monitors`, `sub-scheduler`.
- MQTT contract v1 (target-state §2): namespace `wb-irrigation/`, taxonomy `zone/{id}/(command|desired|state|observed)`, QoS matrix, retained только где нужно, `command_id` (ULID) для идемпотентности через `command_log`.
- ACL per identity: `irrigation_app`, `wb_controller`, `irrigation_tg`, `telegraf_ro` — каждый со своим паролем в `/etc/wb-irrigation/secrets.env`.
- Dual-publish 2 недели (старые + новые топики параллельно), потом cutover — решение за владельцем (§9.6).

**Cross-ref:** API-01 (performance), SP-1 (current-state), SEC-xxx MQTT anonymous.
**Dependencies:** none на старт (client_pool можно сделать сразу); полный контракт — Phase 1 (2–3 спринта).

---

### MASTER-C4: SSE-зомби — фронт отключил, бэк жив, браузер бесконечно реконнектится (FE-CRIT-1)

**Severity:** Critical (UX + нагрузка + dead code)
**Effort:** S (выпил) / L (возврат)
**Priority:** P0

**Что:** `static/js/zones.js:1472` создаёт `EventSource('/api/mqtt/zones-sse')`. Бэкенд `routes/zones_watering_api.py:197-206` возвращает **204 No Content**. EventSource видит EOF → реконнектится через 3 сек → **бесконечно**. На одной открытой вкладке — ≈28 800 запросов/сутки. При одной мобилке в парке + одной десктопной вкладке × 24 ч = 57 600 запросов на поливалку, которая в остальное время CPU < 1%.

Дополнительно:
- `services/sse_hub.py` (363 LOC) в бэке **жив** и пишет в SQLite на каждое MQTT-сообщение (`_on_message` → INSERT) — нагрузка без потребителя (SSE-03).
- 4 детерминированных test failures в `tests/` — все про SSE (tests.md).
- `mqtt.html` использует SSE без reconnect/backoff (FE-HIGH-3) — вторая SSE-дыра.

**Где:**
- `static/js/zones.js:1468-1488` (EventSource)
- `templates/mqtt.html` (вторая SSE-ветка без backoff)
- `services/sse_hub.py` (363 LOC hub)
- `routes/zones_watering_api.py:197-206` (endpoint, возвращает 204)
- Failing tests: `tests/test_sse_*.py` (4 штуки)

**Рекомендация архитектора (target-state §11):** **выпилить SSE полностью.** Заменить на polling `/api/status` 5 сек с `If-Modified-Since`/`ETag` и `document.hidden` pause. Для home UX задержка 0–5 сек приемлема. Если real-time критичен — MQTT.js в браузере через WSS:18883 (но это уже Phase-future).

**Что делать:**
- Удалить `EventSource` из `zones.js:1468-1488` и SSE-код из `mqtt.html`.
- Удалить `services/sse_hub.py` (363 LOC) и `/api/mqtt/zones-sse` endpoint.
- Удалить 4 failing SSE-теста (раз фича выпилена — тесты невалидны).
- Улучшить polling: ETag на `/api/status`, `document.hidden → slow-polling 30 сек`, кнопка «обновить сейчас».

**⚠️ Блокирующий вопрос владельцу:** подтвердить выпил (см. §9.1). Если нет — задача меняется с S на L (вернуть SSE с heartbeat + bounded queue + disconnect detection).

**Cross-ref:** FE-CRIT-1, FE-HIGH-3, SSE-01, SSE-02, SSE-03, SSE-04 (performance), 4 test failures (tests §3.2), SP-2 (current-state).
**Dependencies:** решение владельца.

---

### MASTER-C5: CI/CD пайплайн не запускается на `refactor/v2`

**Severity:** Critical (процессный)
**Effort:** S
**Priority:** P0

**Что:** `.github/workflows/ci.yml` триггерится на `main`, прод работает на `refactor/v2`. **Ни один коммит ветки не прошёл через CI.** Нет линта, нет тестов, нет проверки зависимостей, rollback-механизм никогда не тестировался.

Комбинация с MASTER-C2 (нет логов) = **любой регрессионный баг приходит напрямую на физические клапаны**, и расследовать его после факта нечем.

**Где:** `.github/workflows/ci.yml` — branches: `[main]` only.

**Что делать:**
- В триггере `on.push.branches` добавить `refactor/v2`. И в `pull_request.branches` тоже.
- Python version matrix `['3.9', '3.11']` — 3.9 чтобы match prod.
- Jobs: lint (ruff), test (pytest), integration (mosquitto service container).
- Отдельно build-arm job (docker buildx + QEMU) — **опционально**, может подождать.
- После починки — прогнать `refactor/v2` один раз вручную, зафиксировать baseline: сколько failing, сколько XPASS, coverage.

**Cross-ref:** tests.md §1, SP-6 (current-state), BUG «CI не запускается» (автор не зафиксировал, но знает).
**Dependencies:** ни от чего.

**Нюанс:** после включения CI **первый прогон покажет 8 failing tests + 55 XPASS**. Это ожидаемо — не блокер, но должно быть явно зафиксировано как baseline (см. MASTER-H7).

---

### MASTER-C6: WCAG 2.2 Level A — FAIL, 5 критичных блокеров для полевого оператора

**Severity:** Critical (для полевого использования)
**Effort:** M (все 5 — спринт; быстрые wins — часы)
**Priority:** P1

**Что:** Интерфейс не соответствует даже минимальному уровню A. Для домашнего компьютерного пользователя — терпимо. Для **полевого оператора с мобилкой под солнцем, одной рукой в перчатке** — местами неработоспособно.

Пять блокеров:
1. **Label-less inputs в `mqtt.html:16-41`** — form inputs без `<label>` или `aria-label`. Screen-reader молчит, автозаполнение не работает.
2. **Custom `<div class="toggle-switch">` без `role="switch"`, `aria-checked`, keyboard support** — `templates/programs.html:174,484-485`. Работает только тапом, не Tab+Space.
3. **Sortable `<th>` без keyboard** — `templates/zones.html:114-122`. Кликабельный, но не Tab-focusable + Enter.
4. **SVG dial drag-only** — `templates/status.html:208-225`. Крутить можно только пальцем, нет кнопок +/- и нет arrow-keys. Одной рукой в перчатке — невозможно.
5. **Нет focus trap в 8 модалках + 2 sheet + 2 popup** — Tab уходит за модалку в фон, Esc не закрывает, focus не возвращается на trigger.

Дополнительно (не Level A, но сильно):
- **Контраст #999 на #fff = 2.85:1** (fail AA 4.5:1) повторён **25+ раз** в `static/css/status.css`. Читаемость на солнце — нулевая.
- **Emergency button `#d32f2f` — 5.4:1** на белом (проходит AA, но **fail AAA 7:1**) — для критической функции на улице AAA обязателен.
- `<main>` отсутствует в `login.html`, `404.html`.
- Heading hierarchy skip: h1 → h3 в нескольких шаблонах.

**Где:** `templates/mqtt.html`, `templates/programs.html`, `templates/zones.html`, `templates/status.html`, `templates/login.html`, `templates/404.html`, `static/css/status.css`, все 12 модально-подобных компонентов.

**Что делать:**
- Топ-3 для полевого оператора (подтверждено a11y-отчётом): (1) контраст `#999` → `#595959` или темнее; (2) `role="switch" aria-checked tabindex=0` + keyboard handler на toggle-switch; (3) focus-trap utility (одна функция, 20 LOC) + вызвать в открытии всех модалок.
- Остальное — по ≤1 ч блок (15 quick wins в a11y-отчёте).

**Cross-ref:** a11y.md целиком.
**Dependencies:** none (независимая работа фронтендера).

---

### MASTER-H1: Scheduler dual-track — пакет `scheduler/` + god-module `irrigation_scheduler.py` 1365 LOC с 89 LOC дублем

**Severity:** High
**Effort:** M
**Priority:** P1

**Что:** Автор начал декомпозировать `irrigation_scheduler.py` на пакет `scheduler/` через mixin-паттерн. Декомпозиция **не завершена**:
- `irrigation_scheduler.py` 1365 LOC **остался**, не удалён.
- 89 LOC дублируются между `scheduler/jobs.py` и `irrigation_scheduler.py:67-155`.
- APScheduler dotted-path может ссылаться на любую из двух копий — поведение при запуске программы зависит от того, какой import сработал первым.
- Mixin-паттерн сам по себе — anti-pattern в этом домене; тестировать/мокать сложно.
- Boot-sync дублируется в двух местах: `services/app_init.py` + `irrigation_scheduler.py` (CQ-006 / Bug 5.2 автора).

**Где:**
- `irrigation_scheduler.py` (1365 LOC)
- `scheduler/jobs.py`, `scheduler/control.py`, `scheduler/setup.py`, `scheduler/core.py`, `scheduler/__init__.py`, `scheduler/validation.py` (6 mixin-файлов, 1258 LOC)
- `services/app_init.py` (boot-sync дубль)

**Что делать:**
- Оставить **одну копию** job functions: `infrastructure/scheduler/jobs.py`.
- Перекодировать dotted-path в `apscheduler_jobs`: либо миграция pickle-значений, либо (проще) очистить таблицу и пересоздать jobs из `db.programs` при старте новой версии.
- Удалить `irrigation_scheduler.py` после миграции.
- Удалить boot-sync дубль в `irrigation_scheduler.py`, оставить только в `services/app_init.py` (переименовать → `application/boot_sync.py`).
- Mixin-паттерн заменить на композицию: scheduler `runner.py` (APScheduler wrapper) + `jobs.py` (module-level functions).

**Cross-ref:** CQ-005 (duplicate jobs), CQ-006 (boot-sync duplicate), Bug 5.2 автора, SP-3 (current-state).
**Dependencies:** PHYS-2 (SQLAlchemy установка) — делать до разборки, чтобы jobs после рестарта восстанавливались.

---

### MASTER-H2: PRAGMA не применяются per-connection — FK=OFF, synchronous=FULL на проде

**Severity:** High (DB integrity + eMMC wear)
**Effort:** S
**Priority:** P0

**Что:** `BaseRepository._connect()` **не** устанавливает PRAGMA. Проверка на проде (database.md):
- `foreign_keys=OFF` — FK-ограничения не работают, возможны orphan rows при удалении (например, удалили group → zones с `group_id=N` не caskade'ятся / не сеттятся в NULL).
- `synchronous=FULL` — избыточно для WAL + eMMC, лишние fsync'и изнашивают flash.
- `busy_timeout` не установлен на все соединения — write на busy-lock упадёт быстро.

Плюс: **8 прямых `sqlite3.connect()`** обходят `_connect()` полностью (см. MASTER-H3) — даже если `_connect()` починим, эти 8 мест всё равно без PRAGMA.

**Где:**
- `db/base.py` — `BaseRepository._connect()` без PRAGMA set
- Прод: `PRAGMA foreign_keys` → 0, `PRAGMA synchronous` → 2 (FULL)

**Что делать:**
- В `_connect()` добавить per-connection: `PRAGMA journal_mode=WAL; synchronous=NORMAL; foreign_keys=ON; busy_timeout=30000; temp_store=MEMORY; cache_size=-8000; wal_autocheckpoint=1000`. Это **каждое новое соединение**, не one-shot.
- FK=ON **не** автоматически добавляет декларации — сами FK надо декларировать отдельной миграцией (recreate-table технику; см. MASTER-H5).
- `synchronous=NORMAL` + WAL — best-practice для SQLite на eMMC, безопасно для 24-зон домена.

**Cross-ref:** DB-001 (FK=OFF), DB-004 (synchronous=FULL), SP-8 (current-state).
**Dependencies:** none — **Phase 0**, делать в первом спринте.

---

### MASTER-H3: 8 прямых `sqlite3.connect()` в обход `BaseRepository` (DB-007 + DB-006)

**Severity:** High
**Effort:** M
**Priority:** P1

**Что:** Репозиторный слой `db/` правильный, но **необязательный**. Восемь мест обходят его:
- 7 в `services/weather.py` (god-module 1404 LOC)
- 1 в `services/float_monitor.py`

Последствия: PRAGMA не применяется (см. MASTER-H2), connection lifecycle не контролируется, тесты не могут подменить БД через фикстуру, schema-drift при миграциях possible.

**Где:**
- `services/weather.py` — 7 `sqlite3.connect()` calls
- `services/float_monitor.py` — 1 call
- `database.py` facade (309 LOC) — **дополнительная** дырявая граница: можно звать `database.get_zones()` или `db.zones.list_all()` — два контракта на один агрегат

**Что делать:**
- Создать `db/weather.py` репозиторий, перенести 7 connect'ов туда.
- Создать `db/float.py` репозиторий, перенести 1 connect.
- Постепенно выпилить facade `database.py` (309 LOC) — маркировать deprecation warnings в первом спринте, удалить через месяц.
- Unit-test rule: зелёный тест = нет `sqlite3.connect()` вне `db/base.py` (grep).

**Cross-ref:** DB-006 (weather), DB-007 (layer bypass), MASTER-H2 (PRAGMA), PHYS-3 (float), PHYS-4 (weather).
**Dependencies:** MASTER-H2 (PRAGMA) должна быть сначала, иначе перенос бесполезен.

---

### MASTER-H4: Миграции не атомарны (DB-002)

**Severity:** High
**Effort:** S
**Priority:** P1

**Что:** `db/migrations.py` применяет миграции без единой транзакции. Если `func(conn)` падает посреди, часть DDL прошла, запись в `migrations` не поставлена — схема в промежуточном состоянии. На следующем старте миграция попытается применить **ту же** логику заново (ALTER TABLE ADD COLUMN упадёт с «column already exists»).

**Где:** `db/migrations.py` — `_apply_named_migration` или эквивалент.

**Что делать:**
- Обернуть в `BEGIN IMMEDIATE ... COMMIT`/`ROLLBACK`, миграция-функция **не должна** делать свой `conn.commit()` (или внутренний commit закрывает внешнюю транзакцию — проверить все 35 миграций).
- `ALTER TABLE ADD COLUMN` идемпотентно защищать через `PRAGMA table_info` check.
- `raise` при ошибке, не swallow — fail fast.

**Cross-ref:** DB-002.
**Dependencies:** none — небольшой, но делать до MASTER-H5.

---

### MASTER-H5: Foreign Keys не декларированы в схеме (DB-003)

**Severity:** High
**Effort:** M
**Priority:** P2

**Что:** В таблицах `zones`, `groups`, `water_usage`, `weather_log`, `program_cancellations`, `program_queue_log`, `float_events` **нет FK-деклараций** на уровне DDL. Даже включив `PRAGMA foreign_keys=ON` (MASTER-H2), ограничения не сработают — их нет.

**Где:** `db/migrations.py` — все миграции создания таблиц.

**Что делать:**
- Одна миграция `add_foreign_keys_v2`: для каждой таблицы с FK — `CREATE TABLE zones_new (... FOREIGN KEY ...)` + `INSERT INTO zones_new SELECT * FROM zones` + `DROP TABLE zones` + `ALTER TABLE ... RENAME` + пересоздать индексы.
- `PRAGMA foreign_key_check` на каждом шаге, логировать orphan'ы в `logs`, не удалять автоматически.
- Приоритетные FK:
  - `zones.group_id → groups.id ON DELETE SET NULL`
  - `zones.mqtt_server_id → mqtt_servers.id ON DELETE SET NULL`
  - `groups.*_mqtt_server_id → mqtt_servers.id ON DELETE SET NULL` (4 колонки)
  - `water_usage.zone_id → zones.id ON DELETE CASCADE`
  - `weather_log.zone_id → zones.id ON DELETE CASCADE`
- Выполнять ночью (безопаснее), с backup до и после.

**⚠️ Блокирующий вопрос владельцу (§9.13):** risk tolerance на ~5 сек downtime + full DB rewrite (~1 MB).

**Cross-ref:** DB-003, MASTER-H2 (FK=ON — предпосылка).
**Dependencies:** MASTER-H2 (сначала FK=ON включить), MASTER-H4 (атомарные миграции).

---

### MASTER-H6: SSL/TLS на публичном URL + CF Tunnel bypass `basic_auth_proxy` (SEC-002)

**Severity:** High
**Effort:** S (CF Access) / M (nginx-перед-Flask)
**Priority:** P0

**Что:** `poliv-kg.ops-lab.dev` (CF Tunnel) ведёт **напрямую на `localhost:8080` (Flask)**, в обход `basic_auth_proxy` на `:8011`. В итоге публичный URL защищён только Flask-session. Проблемы:
- Flask login — единственный барьер из интернета.
- Default admin creds (если не сменены) → открытый вход.
- CSRF-exempt API + session cookie = session hijacking даёт полный контроль клапанами (PHYS-5).
- Нет rate-limit на `/login`, нет MFA.

**Где:**
- cloudflared config (WB host) — backend `http://localhost:8080`
- `basic_auth_proxy` на `:8011` существует и работает, но не используется для CF tunnel
- `app.py` — все API blueprints CSRF-exempt

**Что делать (target-state §13, рекомендация архитектора):**
- **Включить Cloudflare Access Application** на `poliv-kg.ops-lab.dev`:
  - Identity provider: Google или GitHub OAuth.
  - Policy: allow только emails из списка (владелец + родственник, 2–3 адреса).
  - Session 24 ч, application type: Self-hosted.
  - Flask читает `Cf-Access-Authenticated-User-Email` header → SSO, обходит login.
- Два слоя auth: CF Access (MFA через IdP) + Flask session (role-based authZ).
- Bonus: CF Access логирует попытки, блокирует bruteforce.

Альтернатива: nginx перед Flask на :443, CF Tunnel → nginx → Flask, с verify CF-Access-JWT в nginx. Сложнее, но без зависимости от CF policy UI.

**⚠️ Блокирующий вопрос владельцу (§9.3):** выбор CF Access vs другой вариант, список allow-emails.

**Cross-ref:** SEC-002, SP-7 (current-state).
**Dependencies:** решение владельца.

---

### MASTER-H7: Тесты — 60.78% coverage, 55 XPASS в limbo, CI не бежит (связан с MASTER-C5)

**Severity:** High
**Effort:** M
**Priority:** P1

**Что:** `pytest` running locally даёт 1486 passed / 8 failed / 14 skipped / 31 xfailed / **55 xpassed**. Coverage 60.78%. Критические пути подкрыты слабо:
- `services/zone_control.py` — 48% (критический путь — PHYS-1).
- `routes/zones_watering_api.py` — 49%.
- `irrigation_scheduler.py` — 57%.
- Telegram bot — 18%.

Отдельные грехи:
- **55 XPASS** — тесты с `@pytest.mark.xfail` проходят. Это значит: фиксили баги, маркер не убирали. Эти тесты не ловят регрессии (xfail skip'ает на failure).
- **8 failing** — детерминированные, не flaky:
  - 4 про SSE (MASTER-C4) — исчезнут при выпиле.
  - 4 другие — нужна отдельная триажная сессия.
- **44 × `time.sleep()`** — flaky на ARM под нагрузкой; заменить на `wait_for_condition` с timeout.
- **Hardcoded prod IP `10.2.5.244`** в `test_mqtt_real.py` — тест требует прод MQTT broker, бежит только с prod network.
- **`pytest.ini` vs `pyproject.toml` conflict** — два разных конфига, markers определены в одном, testpaths в другом.
- **Нет parametrize** — дублирующиеся тесты для `zone_id=1,2,3` как 3 копии.
- **Dead selenium dev-dep** в `requirements-dev.txt` — не используется.

**Где:** `tests/` директория целиком, `pytest.ini`, `pyproject.toml`, `.github/workflows/ci.yml`.

**Что делать:**
1. Сначала MASTER-C5 (CI на `refactor/v2`) — зафиксировать baseline.
2. Удалить/мигрировать XPASS маркеры: либо убрать `@xfail` с проходящих тестов (55 штук), либо переквалифицировать в `@skipif(platform != ...)`.
3. Починить 8 failing: 4 SSE уйдут с MASTER-C4; остальные 4 — триаж.
4. Hardcoded IP → фикстура с `MQTT_BROKER` env var, default `localhost`.
5. `time.sleep` → `wait_for_condition(..., timeout=5)`.
6. Объединить `pytest.ini` и `pyproject.toml` в один (рекомендую `pyproject.toml`).
7. Coverage на критические пути (zone_control, watering_api) → 80%+. Домен (PHYS-1 state machine) — 95%+, это pure-python без I/O.

**Cross-ref:** tests.md целиком, MASTER-C5 (CI).
**Dependencies:** MASTER-C5.


---

### MASTER-H8: Config scatter — 5 источников, `db.settings` хранит runtime в БД

**Severity:** High (деплоймент/reproducibility)
**Effort:** M
**Priority:** P2

**Что:** Конфигурация размазана:
1. `config.py` — статические defaults.
2. `constants.py` — ещё какие-то константы (дубляж с `config.py`).
3. `.env` файл — runtime env.
4. `os.environ` прямые обращения в коде (без единого `Config` объекта).
5. `db.settings` — **application-level settings в БД**, 22 строки; ломает idempotency deploy (две копии системы не одинаковы, даже если код идентичен).

Нет приоритета, кто кого перебивает. Для одного значения (например, логирование DEBUG/INFO) может быть значение в `.env`, в `db.settings`, и default в `config.py`, и они могут отличаться — поведение зависит от того, где в коде его читают.

**Где:** `config.py`, `constants.py`, `.env`, разбросанные `os.environ.get()`, таблица `db.settings`, весь проект.

**Что делать:**
- Один `config.py` с pydantic-settings: defaults + env + `EnvironmentFile=/etc/wb-irrigation/env`.
- `db.settings` — только **user-editable** настройки (те, что меняются через UI: weather enabled, log level, часовой пояс). Каждая с явным `type` и `category`.
- Secrets — отдельно в `/etc/wb-irrigation/secrets.env` chmod 600 (MASTER-M1).
- `constants.py` — слить в `config.py`.

**Cross-ref:** SP-4 (current-state), sre.md config, CQ-xxx.
**Dependencies:** none.

---

### MASTER-H9: Dead code / lying code на фронте (FE-HIGH-1, FE-HIGH-2, CSRF mismatch)

**Severity:** High (maintenance risk)
**Effort:** S
**Priority:** P1

**Что:**
- **113 KB / 2243 LOC dead JS** — 9 файлов в `static/js/components/` не импортируются нигде. При рефакторе эти файлы могут быть «доработаны» без понимания, что они мёртвые.
- **545 LOC inline `<script>` в `templates/programs.html:73-619`** — дублирует логику из `static/js/programs.js` (который живой). Изменение логики → два места → rollback невозможен.
- **CSRF interceptor в `app.js:17-49`** добавляет `X-CSRFToken` ко всем XHR, **но все API blueprints в `app.py:96-109` CSRF-exempt** → заголовок игнорируется. Фронт считает, что защищён; бэк не проверяет. «Lying code».
- `status.js` — **2187 LOC / 118 KB** синхронной загрузки на каждую страницу статуса. `var` используется 255 раз, `let/const` — немного. Рефакторинг без тестов опасен.

**Где:**
- `static/js/components/*.js` — 9 dead файлов (список в frontend.md)
- `templates/programs.html:73-619` — inline script
- `static/js/programs.js` — дубликат на диске
- `static/js/app.js:17-49` — CSRF interceptor
- `app.py:96-109` — CSRF exempt calls
- `static/js/status.js` — 2187 LOC

**Что делать:**
- Удалить 9 dead JS-файлов одним коммитом (grep проверить, что нигде не ссылаются).
- Вынести inline из `programs.html` в `static/js/programs.html.js` (отдельный файл, загружается тегом), избавиться от дубликата.
- CSRF ревизия: либо включить csrf_protect на API blueprints с session-authenticated (рекомендую), либо удалить interceptor из `app.js`. Не держать обе половины.
- `status.js` — не трогать до появления тестов, **но** разделить на модули при следующем большом рефакторинге.

**Cross-ref:** FE-HIGH-1, FE-HIGH-2, FE-HIGH-6, CSRF current-state §9.3.
**Dependencies:** none.

---

### MASTER-H10: APScheduler без SQLAlchemyJobStore → MemoryJobStore fallback (PHYS-2 base)

**Severity:** High (дубляж PHYS-2, но для видимости в master-list)
**Effort:** S
**Priority:** P0

См. **PHYS-2** выше — это master-finding. Ключевое:
- `requirements.txt` += SQLAlchemy.
- APScheduler → SQLAlchemyJobStore на отдельном `jobs.db` файле.
- Misfire policy: `coalesce=True, misfire_grace_time=300, max_instances=1`.

**Cross-ref:** PHYS-2, HIGH-R3.

---

### MASTER-M1: Secrets storage — TG-токен, MQTT-пароли, `SECRET_KEY` не в secure хранилище (SEC-010)

**Severity:** Medium
**Effort:** S
**Priority:** P1

**Что:** Telegram bot token и MQTT пароли находятся либо в `db.settings` (SQLite доступен любому с admin в UI), либо в `.env` / коде. `SECRET_KEY` Flask генерится в `.irrig_secret_key` в рабочей директории (неправильное FHS место).

**Где:** `db.settings` TG token, `.env`/`os.environ`, `.irrig_secret_key`.

**Что делать:**
- `/etc/wb-irrigation/secrets.env` chmod 600, owner `root:wb-irrigation`, читается через systemd `EnvironmentFile=`.
- **Опционально** (вопрос владельцу §9.2): `LoadCredential=` — secrets не видны в `systemctl show`, читаются через `$CREDENTIALS_DIRECTORY/...`. Безопаснее, но требует рефактора читалки.
- `SECRET_KEY` → `/var/lib/wb-irrigation/secret_key`, chmod 600, генерится на первом старте (FHS-correct).
- TG bot token и MQTT passwords убрать из `db.settings` — в UI пусть отображается placeholder «set via env».

**Cross-ref:** SEC-010, target-state §7.
**Dependencies:** решение владельца по LoadCredential vs EnvironmentFile.

---

### MASTER-M2: Systemd unit крутится под `root`, нет `WatchdogSec`, `TimeoutStopSec=20s` недостаточно

**Severity:** Medium
**Effort:** S
**Priority:** P1

**Что:** systemd unit текущий:
- `User=root` (по умолчанию) — процесс имеет root-привилегии, нарушает least-privilege.
- Нет `WatchdogSec=` + `Type=notify` — если приложение повесится (HTTP отвечает, но scheduler-thread мёртв), systemd не рестартует.
- `TimeoutStopSec=20` — при graceful shutdown с активным MQTT publish-ом 20 сек может не хватить (HIGH-R6).
- Нет `MemoryMax=`/`CPUQuota=` — если будет leak, процесс съест весь RAM (224 MB сейчас, WB 3.8 GB, есть запас, но лимит даёт гарантию).

**Где:** `/etc/systemd/system/wb-irrigation.service` (или эквивалентный на WB).

**Что делать:**
- Создать system user `wb-irrigation`, `chown` `/opt/wb-irrigation`, `/mnt/data/irrigation-logs`, `/var/lib/wb-irrigation`.
- `User=wb-irrigation` `Group=wb-irrigation`.
- `Type=notify`, `WatchdogSec=60`, `sd_notify('WATCHDOG=1')` из reconciler-thread каждые 30 сек (только если он жив).
- `TimeoutStopSec=45`, `Restart=always`, `RestartSec=5`, `StartLimitBurst=5`.
- `MemoryMax=512M`, `CPUQuota=200%`.
- `ProtectSystem=strict`, `ReadWritePaths=...`, `PrivateTmp=true`, `NoNewPrivileges=true`.

**Cross-ref:** HIGH-R6, sre.md systemd section, target-state §7.2.
**Dependencies:** none.

---

### MASTER-M3: Default admin credentials не форсятся к смене (SEC-xxx)

**Severity:** Medium
**Effort:** S
**Priority:** P1

**Что:** При первом запуске создаётся admin с дефолтным паролем. Если владелец не сменил — вход по дефолту. Для домашней системы в LAN — не критично, для **публичного CF URL** — опасно (MASTER-H6).

**Где:** `services/app_init.py` создание admin, `templates/login.html` — нет force-change-on-first-login.

**Что делать:**
- При первом логине с дефолтным паролем — redirect на `/force-password-change`, не пускать дальше.
- Либо: не создавать admin в коде, генерить случайный пароль на первом старте и **печатать его в лог/stdout** (с требованием смены).

**Cross-ref:** SEC-xxx default creds.
**Dependencies:** MASTER-C2 (логи работают — иначе не увидим напечатанный пароль).

---

### MASTER-M4: Backup — keep_count=7, нет offsite, WAL checkpoint не PASSIVE (DB-016)

**Severity:** Medium
**Effort:** S
**Priority:** P1

**Что:** Backup скрипт делает ежедневный `sqlite3 .backup`, keep_count=7 (неделя). На SD-карте 33 GB free — могли бы хранить 30 дней. Нет copy offsite. WAL checkpoint после backup — `TRUNCATE` (блокирует writers) вместо `PASSIVE`.

**Где:** backup-скрипт / APScheduler backup-job.

**Что делать:**
- `keep_days=30` вместо `keep_count=7`.
- `PRAGMA wal_checkpoint(PASSIVE)` после backup.
- Offsite — **вопрос владельцу §9.9**: S3/Dropbox/rsync на домашний NAS?

**Cross-ref:** DB-005 (нет backup теста), DB-016 (retention), target-state §5.6.
**Dependencies:** none.

---

### MASTER-M5: Нет health/readiness/metrics endpoints (SRE)

**Severity:** Medium
**Effort:** S
**Priority:** P2

**Что:** Текущие endpoint `/health` есть, но:
- Нет `/readyz` — проверка, что MQTT connected + DB отвечает + scheduler running + boot_reconcile done.
- Нет `/metrics` (Prometheus) — невозможно нарисовать дашборд без ручных запросов к БД.
- `/healthz` (liveness, простой 200 «Flask жив») можно оставить как alias.

**Где:** `routes/pages.py` или `routes/system_status_api.py`.

**Что делать:**
- `prometheus-client` ~200 KB, добавить в `requirements.txt`.
- Registry с метриками (target-state §6.3): `wb_zone_start_total`, `wb_zone_fault_total`, `wb_mqtt_publish_total`, `wb_zones_active`, `wb_observed_ack_latency_ms`, `wb_scheduler_lag_seconds`.
- `/readyz` с checks {db, mqtt, scheduler, boot_reconcile}.
- `/metrics` — IP allow-list (only LAN + 127.0.0.1), без auth — метрики не секретны.
- Telegraf (уже стоит на WB для Zabbix) scrape `/metrics` → Zabbix. Не поднимать отдельный Prometheus. **Вопрос владельцу §9.15**.

**Cross-ref:** SRE §6.3, target-state §6.
**Dependencies:** MASTER-C2 (logging), MASTER-C1 (state machine — там метрики ack latency).

---

### MASTER-M6: Frontend — 7/9 форм без double-submit protection, polling игнорирует `document.hidden`, hamburger 36×36

**Severity:** Medium
**Effort:** S (за часы, серия фиксов)
**Priority:** P2

**Что:** Несколько мелких UX-проблем:
- **7 из 9 форм** не блокируют кнопку Submit на время pending request → двойная отправка → дублирование зон/программ.
- **Polling 5 сек** работает даже когда вкладка невидима — мобильный battery drain.
- **Hamburger-меню 36×36 px** — меньше WCAG 44×44 минимум для touch target.
- `status.js` без `loading="lazy"` на фото зон — `<img>` грузит все сразу.
- Нет `AbortController`/fetch timeout — висячий запрос может «съесть» UI.
- Нет `manifest.json` — Service Worker есть, но PWA-install не работает.
- Нет offline-detection UX.

**Где:** `templates/*.html`, `static/js/*.js`.

**Что делать:**
- Один helper `disableSubmit(form)` / `enableSubmit(form)` — применить к 7 формам.
- В polling-цикле: `if (document.hidden) { await sleep(30_000); continue; }`.
- Hamburger CSS: min-width/height 44px.
- `loading="lazy"` на `<img>` зон.
- `AbortController` на все `fetch()` с timeout 10 сек.
- `manifest.json` + link в `<head>` — 20 LOC.

**Cross-ref:** FE-HIGH-4, FE-HIGH-5, FE-MED-1..9.
**Dependencies:** none.

---

### MASTER-M7: XPASS limbo — 55 тестов с `@xfail` проходят, маркер не снимают

**Severity:** Medium (процессный)
**Effort:** S
**Priority:** P1

**Что:** 55 тестов имеют `@pytest.mark.xfail(reason="...")`, но проходят. Либо:
- баги, которые xfail помечал, починены, а маркер — нет;
- Tests Driven Development limbo — тесты написаны вперёд, фича частично реализована, маркер остался.

Эффект: эти тесты **не ловят регрессии**. Они пройдут и сейчас, и если фичу сломают обратно.

**Где:** grep `pytest.mark.xfail` в `tests/`.

**Что делать:**
- Либо убрать `@xfail` (регрессии начнут ловиться).
- Либо переквалифицировать в `@skipif(sys.platform == ...)` если причина — платформа.
- Либо удалить тест, если он обсолетный.
- Нельзя оставлять «на всякий случай».

**Cross-ref:** tests.md §5.
**Dependencies:** MASTER-C5 (CI — чтобы результат видеть).

---

### MASTER-L1: Code quality — god-modules `weather.py` 1404 LOC, `status.js` 2187 LOC, `var` 255×, no ruff/mypy на всём

**Severity:** Low (но широкий scope)
**Effort:** L
**Priority:** P3

**Что:**
- `services/weather.py` — 1404 LOC, 6+ ответственностей (fetch, parse, cache, DB, business rules, scheduler integration). Разобрать в Phase 4 (target-state Phase 4.4, 2–3 спринта).
- `irrigation_scheduler.py` 1365 LOC — см. MASTER-H1 (этот уходит вместе с dedup).
- `status.js` 2187 LOC — разбирать аккуратно после появления тестов.
- ruff/mypy не применены на всю кодбазу — постепенное покрытие на domain/ (см. target-state §8.2 CI).

**Где:** весь проект.

**Что делать:**
- Phase 4 в target-state: разобрать `weather.py` на `domain/weather`, `infrastructure/weather_http`, `db/weather`, `application/weather_service`, `routes/api/v1/weather`.
- ruff + mypy в CI постепенно, начиная с `domain/` и `application/` (pure python, легко типизируется).

**Cross-ref:** current-state §1.6 cohesion, target-state §1.3.
**Dependencies:** MASTER-H1, MASTER-H3.

---

### MASTER-L2: API — нет версионирования, нет единого error format, legacy endpoints без pagination

**Severity:** Low
**Effort:** M
**Priority:** P3

**Что:**
- 18 routes без `/api/v1/` префикса.
- Нет единого error-envelope (одни возвращают `{error: ...}`, другие `{message: ...}`, третьи — текст).
- `/api/logs` без pagination (может вернуть 10 000 записей).

**Где:** `routes/*_api.py`.

**Что делать (target-state §9):**
- Переместить в `routes/api/v1/`, `/api/*` → 301 redirect на 6 месяцев.
- RFC 7807 `application/problem+json` error format.
- Cursor-based pagination на `/api/v1/logs`.
- OpenAPI spec (автогенерация через `flask-smorest` или ручная) — полезно для TG bot и Home Assistant integration.

**Cross-ref:** target-state §9.
**Dependencies:** MASTER-C4 (SSE выпил — освобождает `/api/mqtt/*`).

---

### MASTER-L3: Password hashing — pbkdf2/scrypt, можно мигрировать на argon2id

**Severity:** Low (для домашней системы)
**Effort:** S (argon2-cffi) + L (миграция)
**Priority:** P3

**Что:** `werkzeug.generate_password_hash` по умолчанию scrypt (с werkzeug 2022+), для более старых — pbkdf2. Оба приемлемы. Argon2id современнее, OWASP-рекомендация.

**Где:** `services/auth.py` или эквивалент.

**Что делать:**
- `argon2-cffi` ~300 KB, parameters `memory_cost=32768, time_cost=2, parallelism=1` (ARM-friendly, ~200 мс login).
- Lazy rehash on login: при успешной проверке старым форматом — сохранить хэш в argon2id.
- Через 6 мес — удалить support старых форматов.

**Cross-ref:** target-state §12.3.
**Dependencies:** none — только когда появятся свободные руки.

---

### MASTER-L4: Telegram bot — 18% coverage, авторизация через whitelist хардкод в коде

**Severity:** Low
**Effort:** M
**Priority:** P3

**Что:**
- Tests coverage TG bot 18%.
- Whitelist `chat_id` на допуск в бот — в коде / `.env`.
- `bot_users` таблица существует, но пустая (0 строк на проде) — фича есть, не используется.

**Где:** `services/telegram_bot.py`, `bot_users` таблица.

**Что делать:**
- Миграция whitelist → `bot_users` таблица с ролями (`admin`/`user`/`guest`).
- Первый `/start` без существующих users → первый user = admin.
- Invite-flow: admin `/invite @user` → pending → `/approve`.
- Тесты через aiogram TestClient — покрыть happy path + access denied + bot_idempotency.

**Cross-ref:** target-state §10.4.
**Dependencies:** none.

---

**Сводная таблица master-points:**

| ID | Severity | Effort | Priority | Domain | Dependencies |
|---|:-:|:-:|:-:|---|---|
| PHYS-1 | C | L | P0 | physical | MASTER-C3, C2, H2 |
| PHYS-2 | C | S | P0 | physical | none |
| PHYS-3 | H | S | P1 | physical | MASTER-H2 |
| PHYS-4 | H | M | P2 | physical | MASTER-H2 |
| PHYS-5 | H | S | P1 | physical/security | MASTER-H6 |
| MASTER-C1 | C | L | P0 | core | MASTER-C3, H2, M1 |
| MASTER-C2 | C | S | P0 | observability | none (**first**) |
| MASTER-C3 | C | M | P0 | mqtt | none |
| MASTER-C4 | C | S/L | P0 | frontend | owner decision |
| MASTER-C5 | C | S | P0 | process/CI | none |
| MASTER-C6 | C | M | P1 | a11y | none |
| MASTER-H1 | H | M | P1 | scheduler | PHYS-2 |
| MASTER-H2 | H | S | P0 | db | none |
| MASTER-H3 | H | M | P1 | db | MASTER-H2 |
| MASTER-H4 | H | S | P1 | db | none |
| MASTER-H5 | H | M | P2 | db | MASTER-H2, H4 |
| MASTER-H6 | H | S | P0 | security | owner decision |
| MASTER-H7 | H | M | P1 | tests | MASTER-C5 |
| MASTER-H8 | H | M | P2 | config | none |
| MASTER-H9 | H | S | P1 | frontend | none |
| MASTER-H10 | H | S | P0 | scheduler | (=PHYS-2) |
| MASTER-M1 | M | S | P1 | security | owner decision |
| MASTER-M2 | M | S | P1 | systemd | none |
| MASTER-M3 | M | S | P1 | security | MASTER-C2 |
| MASTER-M4 | M | S | P1 | backup | none |
| MASTER-M5 | M | S | P2 | observability | MASTER-C2, C1 |
| MASTER-M6 | M | S | P2 | frontend | none |
| MASTER-M7 | M | S | P1 | tests | MASTER-C5 |
| MASTER-L1 | L | L | P3 | code quality | MASTER-H1, H3 |
| MASTER-L2 | L | M | P3 | api | MASTER-C4 |
| MASTER-L3 | L | S | P3 | security | none |
| MASTER-L4 | L | M | P3 | telegram | none |

Всего **32 master-point** (5 PHYS + 6 Critical + 10 High + 7 Medium + 4 Low + PHYS-4 в двух разрезах). После дедупликации из ~60 исходных находок агентов.


---

## 5. Авторский BUGS-REPORT — статус

Автор ведёт `BUGS-REPORT.md` в репозитории. Ниже — статус каждого пункта после Phase 2 аудита.

| # | Bug (автор) | Статус | Master-point | Комментарий |
|---|---|:-:|---|---|
| 1 | Scheduler dual-track (scheduler/ пакет + irrigation_scheduler.py) | **Verified** | MASTER-H1 | Подтверждено code-quality, SRE, architecture. 89 LOC дубль reproducible. |
| 2 | Config scatter (5 источников) | **Verified** | MASTER-H8 | Подтверждено. Приоритет не блокер. |
| 3 | MQTT per-request client в system_status_api | **Verified + extended** | MASTER-C3 | Автор описал частный случай, performance.md измерил — 17 000 TCP/day. Расширено: вся MQTT-топология без owner. |
| 4 | `app.log` 0 байт — логи не пишутся | **Verified + extended** | MASTER-C2 | Расширено: +CQ-001..004 (NameError-бомбы в 4 модулях без `import logging`), +`telegram.txt` без ротации. |
| 5.1 | CSRF-exempt API на публичном URL | **Verified + extended** | MASTER-H6 | Автор пометил как security concern. Расширено: app.js шлёт X-CSRFToken, бэк не проверяет = «lying code». |
| 5.2 | Boot-sync дубль (app_init vs irrigation_scheduler) | **Verified** | MASTER-H1 (подмножество) | CQ-006 подтверждает. |
| 5.3 | SSE в mqtt.html без reconnect/backoff | **Verified** | MASTER-C4 | FE-HIGH-3 подтверждает. |
| 6 | SSE zones.js — бесконечный реконнект на 204 | **Verified + extended** | MASTER-C4 | Расширено: +sse_hub.py 363 LOC жив в бэке + 4 failing tests + SQLite writes per MQTT message. |
| 7 | SQLAlchemy не установлен → MemoryJobStore | **Verified** | PHYS-2 / MASTER-H10 | Подтверждено prod-snapshot. Критично — stop-jobs теряются при рестарте. |
| 8 | Default admin password не форсится | **Verified** | MASTER-M3 | Severity Medium для домашки в LAN, High при публичном CF URL. |
| 9 | Weather.py — 1404 LOC god-module + 7 прямых sqlite3 | **Verified** | MASTER-H3 + MASTER-L1 | Подтверждено database.md и code-quality.md. |
| 10 | PRAGMA FK=OFF на проде | **Verified** | MASTER-H2 | database.md проверил на проде. |
| 11 | Миграции не атомарны | **Verified** | MASTER-H4 | DB-002. |
| 12 | Scheduler не удалён после извлечения в пакет | **Verified** | MASTER-H1 | — |
| 13 | CI/CD на `main`, прод на `refactor/v2` | **Verified + extended** | MASTER-C5 | Автор упомянул, tests.md подтверждает: CI не запускался ни разу. Расширено: combined с отсутствием логов (MASTER-C2) → диагностический blackout. |

**Новые находки, которых не было в BUGS-REPORT (NEW):**

| # | Новая находка | Master-point | Severity |
|---|---|---|---|
| N1 | Нет reconciliation БД↔hardware (4 разных сценария пролива) | PHYS-1 / MASTER-C1 | **Critical** |
| N2 | WCAG 2.2 Level A FAIL — 5 критичных блокеров для полевого оператора | MASTER-C6 | Critical (поле) |
| N3 | 55 XPASS тестов в limbo | MASTER-M7 | Medium |
| N4 | 44× `time.sleep()` → flaky tests на ARM | tests.md / часть MASTER-H7 | High |
| N5 | Hardcoded prod IP `10.2.5.244` в `test_mqtt_real.py` | часть MASTER-H7 | Medium |
| N6 | `pytest.ini` vs `pyproject.toml` conflict | часть MASTER-H7 | Low |
| N7 | 113 KB / 2243 LOC dead JS | MASTER-H9 | High (maintenance) |
| N8 | 545 LOC inline script в `programs.html` дублирует `programs.js` | MASTER-H9 | High |
| N9 | 7/9 форм без double-submit protection | MASTER-M6 | Medium |
| N10 | Polling не учитывает `document.hidden` | MASTER-M6 | Medium |
| N11 | Contrast #999 на #fff — 25+ мест, fail AA | MASTER-C6 | Critical (поле) |
| N12 | Emergency button contrast 5.4:1 — fail AAA | MASTER-C6 | High (поле) |
| N13 | systemd без `WatchdogSec`, под `root`, `TimeoutStopSec=20` | MASTER-M2 | Medium |
| N14 | Backup keep_count=7 (могли бы 30), нет offsite | MASTER-M4 | Medium |
| N15 | Нет `/readyz`, `/metrics` | MASTER-M5 | Medium |
| N16 | Float-monitor прямой sqlite3 — safety-critical (PHYS-3 уровень) | PHYS-3 | High |
| N17 | 18 MQTT-клиентов anonymous, нет ACL | MASTER-C3 | Critical |
| N18 | CF Tunnel на :8080, обход :8011 | MASTER-H6 | High |
| N19 | `database.py` Facade 309 LOC дублирует `db/` | MASTER-H3 | High |
| N20 | No `manifest.json`, Service Worker без PWA | MASTER-M6 | Medium |

Итог:
- **Authorial BUGS-REPORT полностью верифицирован**, 0 false positives.
- **20 новых находок** добавлены в master-points.
- **Extended** 5 пунктов автора — найдены более глубокие причины или связанные симптомы.

---

## 6. Приоритизированный план действий (5 волн)

План разбит на 5 волн. Каждая волна — deployable самостоятельно, не требует big-bang. Приоритет по **risk × effort**, не по сложности.

### Волна 1 — «Починить обратную связь и остановить утечку диагностики» (1 спринт, ≤1 неделя)

**Цель:** вернуть способность увидеть, что происходит, и перестать терять данные при рестарте.

| # | Задача | Master-point | Effort |
|---|---|---|---|
| 1.1 | Handler на root logger, убрать `basicConfig` из scheduler/jobs/database, добавить `import logging; logger = ...` в 4 модуля (routes/settings, services/locks, services/mqtt_pub, services/telegram_bot) | MASTER-C2 | 4 ч |
| 1.2 | logrotate для `telegram.txt` | MASTER-C2 | 30 мин |
| 1.3 | PRAGMA в `BaseRepository._connect()`: `foreign_keys=ON`, `synchronous=NORMAL`, `busy_timeout=30000` | MASTER-H2 | 1 ч |
| 1.4 | `requirements.txt` += SQLAlchemy; APScheduler на SQLAlchemyJobStore с отдельным `jobs.db` | PHYS-2 / MASTER-H10 | 2 ч |
| 1.5 | `.github/workflows/ci.yml` триггерить на `refactor/v2`; прогон, baseline зафиксировать | MASTER-C5 | 30 мин |
| 1.6 | Миграции: обернуть в `BEGIN IMMEDIATE ... COMMIT`/`ROLLBACK`, проверить 35 миграций не делают внутренний commit | MASTER-H4 | 4 ч |

**Выхлоп волны:** после рестарта stop-jobs восстанавливаются, лог пишется, CI бежит и ловит регрессии, БД целостность per-connection обеспечивается. **Это максимально ценная неделя работы.**

---

### Волна 2 — «Закрыть физические риски» (2 спринта)

**Цель:** убрать PHYS-1..5. Ключевая доставка квартала.

| # | Задача | Master-point | Effort |
|---|---|---|---|
| 2.1 | `infrastructure/mqtt/client_pool.py` — единственный pool, удалить per-request connect в `system_status_api.py:464-478` | MASTER-C3 | 1–2 дня |
| 2.2 | Добавить колонки в `zones`: `desired_state`, `desired_version`, `commanded_state`, `observed_state`, `observed_at`, `last_command_id` + миграция | MASTER-C1 | 1 день |
| 2.3 | MQTT topic `wb-irrigation/zone/+/observed` + subscriber в приложении → UPDATE `observed_state` | MASTER-C1 / MASTER-C3 | 2 дня |
| 2.4 | WB rule engine: правило публикации observed из GPIO (это вне кода приложения — **зависит от владельца/WB-Techpom, §9.14**) | MASTER-C1 | 1 день (WB-конфиг) |
| 2.5 | `application/reconciler.py` + APScheduler 10-сек job: desired vs observed, retry через `command_id`, fault после 3 попыток | MASTER-C1 | 2–3 дня |
| 2.6 | `boot_reconcile` с retained MQTT snapshot: safety-first | MASTER-C1 | 2 дня |
| 2.7 | Переписать `start_zone`/`stop_zone` на optimistic UPDATE с `desired_version` | MASTER-C1 | 2 дня |
| 2.8 | Удалить `services/observed_state.py verify_async` (HIGH-R2) и `_delayed_close` thread (CQ-007) — reconciler их заменяет | MASTER-C1 | полдня |
| 2.9 | `command_log` таблица + idempotency handler | MASTER-C3 | 1 день |
| 2.10 | CF Access на `poliv-kg.ops-lab.dev` (после решения владельца) | MASTER-H6 / PHYS-5 | 2 ч |
| 2.11 | flask-limiter rate-limit на `/login`, `*/start`, `*/stop` | PHYS-5 | полдня |
| 2.12 | Force-password-change на первом логине с default creds | MASTER-M3 | полдня |

**Выхлоп волны:** БД и hardware больше не расходятся. Публичный URL за MFA. Stop-команды идемпотентны.

---

### Волна 3 — «Observability + SSE-выпил + frontend hygiene» (1–2 спринта)

**Цель:** `/metrics`, `/readyz`, убрать SSE-зомби, почистить фронт от dead/lying code.

| # | Задача | Master-point | Effort |
|---|---|---|---|
| 3.1 | `prometheus-client` + `/metrics` endpoint (IP allow-list) | MASTER-M5 | 1 день |
| 3.2 | `/healthz` + `/readyz` с checks {db, mqtt, scheduler, boot_reconcile} | MASTER-M5 | полдня |
| 3.3 | Correlation-ID middleware + structured JSON logs | MASTER-C2 (extended) | 1 день |
| 3.4 | systemd `WatchdogSec=60` + `Type=notify` + `sd_notify` heartbeat из reconciler; `User=wb-irrigation` (создать user, chown); `MemoryMax=512M`, `TimeoutStopSec=45` | MASTER-M2 | 1 день |
| 3.5 | Backup: `keep_days=30`, `PRAGMA wal_checkpoint(PASSIVE)` | MASTER-M4 | полдня |
| 3.6 | Telegraf scrape `/metrics` → Zabbix (если владелец подтвердит §9.15) | MASTER-M5 | 1 день |
| 3.7 | Удалить SSE: `services/sse_hub.py`, `/api/mqtt/zones-sse`, EventSource из `zones.js` и `mqtt.html`, 4 failing SSE-теста | MASTER-C4 | 1 день |
| 3.8 | Polling улучшения: ETag на `/api/status`, `document.hidden → 30s slow-polling` | MASTER-C4 / MASTER-M6 | 1 день |
| 3.9 | Удалить 9 dead JS файлов (113 KB / 2243 LOC) | MASTER-H9 | 2 ч |
| 3.10 | Вынести inline из `programs.html:73-619` в `static/js/programs.html.js`, удалить дубликат | MASTER-H9 | 4 ч |
| 3.11 | CSRF-ревизия: либо включить csrf_protect на session-API, либо удалить X-CSRFToken interceptor — НЕ держать обе половины | MASTER-H9 | 1 день |
| 3.12 | `disableSubmit` helper на 7 формах, `AbortController` на fetch, hamburger ≥44px, `loading="lazy"` на фото зон, `manifest.json` | MASTER-M6 | 1 день |

**Выхлоп волны:** видно, как поливается. Полевой оператор может навигировать не через SSE-реконнекты.

---

### Волна 4 — «A11y + тесты» (1–2 спринта)

**Цель:** WCAG 2.2 Level A pass; coverage критических путей 80%+; тесты стабильные.

| # | Задача | Master-point | Effort |
|---|---|---|---|
| 4.1 | Top-3 a11y для полевого оператора: (1) `#999` → `#595959` в 25+ местах `status.css`; (2) toggle-switch `role="switch"` + aria-checked + keyboard; (3) focus-trap utility + применить в 12 модалках | MASTER-C6 | 2–3 дня |
| 4.2 | Emergency button contrast 5.4 → 7:1 (AAA) | MASTER-C6 | 30 мин |
| 4.3 | Label или aria-label на inputs в `mqtt.html:16-41` | MASTER-C6 | 1 ч |
| 4.4 | Sortable `<th>` keyboard support | MASTER-C6 | 2 ч |
| 4.5 | SVG dial в `status.html` — кнопки +/- + arrow-keys | MASTER-C6 | 4 ч |
| 4.6 | `<main>` в `login.html`, `404.html` | MASTER-C6 | 30 мин |
| 4.7 | Heading hierarchy fix (h1 → h3 skip) | MASTER-C6 | 1 ч |
| 4.8 | XPASS ревизия: убрать 55 маркеров с проходящих тестов (или переквалифицировать) | MASTER-M7 | 1 день |
| 4.9 | 4 non-SSE failing tests — триажная сессия, починить или удалить | MASTER-H7 | 1 день |
| 4.10 | `time.sleep()` → `wait_for_condition()` в 44 местах | MASTER-H7 | 2 дня |
| 4.11 | `pytest.ini` слить в `pyproject.toml`; hardcoded IP → фикстура с env var; parametrize дубли | MASTER-H7 | 1 день |
| 4.12 | Coverage `services/zone_control.py` 48% → 80%+; `routes/zones_watering_api.py` 49% → 80%+ | MASTER-H7 | 3 дня |
| 4.13 | Unit-тесты `domain/zone/state_machine.py` (pure) → 95% | MASTER-C1 | 2 дня |

**Выхлоп волны:** WCAG 2.2 Level A passed, a11y чек-лист (22 пункта) закрыт. Тесты ловят регрессии. Критические пути покрыты.

---

### Волна 5 — «Долг и evolvability» (2–3 спринта)

**Цель:** разобрать god-modules, API v1, FK-декларации, secrets в secure storage.

| # | Задача | Master-point | Effort |
|---|---|---|---|
| 5.1 | `db/weather.py` репозиторий: перенести 7 прямых connect из `services/weather.py` | MASTER-H3 | 2 дня |
| 5.2 | `db/float.py` репозиторий для `services/float_monitor.py` | MASTER-H3 / PHYS-3 | 1 день |
| 5.3 | Deprecate → удалить `database.py` facade (309 LOC) | MASTER-H3 | 2 дня |
| 5.4 | Удалить `irrigation_scheduler.py` после миграции dotted-path и boot-sync консолидации | MASTER-H1 | 3 дня |
| 5.5 | Разобрать `services/weather.py` 1404 LOC на `domain/weather/decision.py` (pure) + `infrastructure/weather_http/` + `application/weather_service.py` | MASTER-L1 | 2 спринта |
| 5.6 | FK декларации миграция `add_foreign_keys_v2` (recreate-table, ночью, с backup/rollback) | MASTER-H5 | 2–3 дня |
| 5.7 | Composite индексы (DB-009) | — | 1 день |
| 5.8 | Retention job `logs > 180 days` | — | полдня |
| 5.9 | Config consolidation: единый `config.py` pydantic-settings; `db.settings` — только user-editable с `type`/`category` | MASTER-H8 | 2 дня |
| 5.10 | Secrets → `/etc/wb-irrigation/secrets.env` chmod 600 (или `LoadCredential=` — после решения владельца) | MASTER-M1 | 1 день |
| 5.11 | `SECRET_KEY` → `/var/lib/wb-irrigation/secret_key` (FHS) | MASTER-M1 | 1 ч |
| 5.12 | API v1: переместить в `routes/api/v1/`, legacy 301 redirect | MASTER-L2 | 1 день |
| 5.13 | RFC 7807 error envelope | MASTER-L2 | 1 день |
| 5.14 | OpenAPI spec + autogen | MASTER-L2 | 2 дня |
| 5.15 | Cursor-based pagination на `/api/v1/logs` | MASTER-L2 | полдня |
| 5.16 | argon2id password hashing с lazy migration | MASTER-L3 | 1 день |
| 5.17 | Telegram bot — `bot_users` миграция с ролями, coverage 18% → 60% | MASTER-L4 | 1 спринт |

**Выхлоп волны:** god-modules разобраны, репозиторный слой обязателен, API версионирован, secrets secure.

---

### Волна 0 (опциональная, можно в любой момент) — Quick wins

См. §7 «Quick wins».

---

### Общий таймлайн и orientational effort

| Волна | Эффорт | Приоритет |
|---|---|---|
| Волна 1 (обратная связь) | ≤ 1 неделя | **P0 — на этой неделе** |
| Волна 2 (физические риски) | 2 спринта | **P0 — в ближайший месяц** |
| Волна 3 (observability + SSE) | 1–2 спринта | **P1 — до осеннего сезона** |
| Волна 4 (a11y + тесты) | 1–2 спринта | P1 |
| Волна 5 (долг) | 2–3 спринта | P2 |

Итого 8–12 спринтов (~3 месяца) параллельно с обычной разработкой. Никакого big-bang rewrite.

---

## 7. Quick wins (≤1 часа каждый)

Можно делать в любой день, в любой последовательности. Каждый — самостоятельный маленький PR.

| # | Win | Файл/место | Master-point | Effort |
|---|---|---|---|:-:|
| QW-1 | `logger = logging.getLogger(__name__)` + `import logging` в `routes/settings.py` | файл сверху | MASTER-C2 | 5 мин |
| QW-2 | То же в `services/locks.py` | — | MASTER-C2 | 5 мин |
| QW-3 | То же в `services/mqtt_pub.py` | — | MASTER-C2 | 5 мин |
| QW-4 | То же в `services/telegram_bot.py` | — | MASTER-C2 | 5 мин |
| QW-5 | Убрать `logging.basicConfig()` из `irrigation_scheduler.py` | — | MASTER-C2 | 10 мин |
| QW-6 | Убрать `logging.basicConfig()` из `scheduler/jobs.py` | — | MASTER-C2 | 10 мин |
| QW-7 | Убрать `logging.basicConfig()` из `database.py` | — | MASTER-C2 | 10 мин |
| QW-8 | `logrotate` конфиг для `telegram.txt` в `/etc/logrotate.d/wb-irrigation` | — | MASTER-C2 | 15 мин |
| QW-9 | PRAGMA добавить в `_connect()` | `db/base.py` | MASTER-H2 | 30 мин |
| QW-10 | `.github/workflows/ci.yml`: add `refactor/v2` в триггеры | — | MASTER-C5 | 10 мин |
| QW-11 | Удалить 9 dead JS файлов в `static/js/components/` (грепнуть, не импортируются) | — | MASTER-H9 | 30 мин |
| QW-12 | Удалить dead selenium из `requirements-dev.txt` | — | MASTER-H7 | 5 мин |
| QW-13 | `<main>` добавить в `login.html` и `404.html` | — | MASTER-C6 | 10 мин |
| QW-14 | Emergency button `#d32f2f` → `#b71c1c` (contrast 5.4 → 7.1) | `status.css` | MASTER-C6 | 5 мин |
| QW-15 | Hamburger menu padding: min-width/height 44px | CSS | MASTER-M6 | 15 мин |
| QW-16 | `loading="lazy"` на `<img>` зон | `zones.html` | MASTER-M6 | 15 мин |
| QW-17 | `manifest.json` 20 LOC + `<link rel="manifest">` в base template | new file | MASTER-M6 | 30 мин |
| QW-18 | `document.hidden → slow polling 30s` в `status.js` polling loop | `status.js` | MASTER-M6 | 20 мин |
| QW-19 | `disableSubmit(form)` helper + применить к форме login | `app.js` + `login.html` | MASTER-M6 | 30 мин |
| QW-20 | aria-label на form inputs в `mqtt.html:16-41` (7 inputs) | `mqtt.html` | MASTER-C6 | 20 мин |
| QW-21 | Heading fix h1 → h3 skip (3 места) | templates | MASTER-C6 | 30 мин |
| QW-22 | Backup script: `keep_count=7` → `keep_days=30` | backup script | MASTER-M4 | 10 мин |
| QW-23 | Backup: `TRUNCATE` → `PASSIVE` checkpoint | backup script | MASTER-M4 | 5 мин |
| QW-24 | Hardcoded IP `10.2.5.244` в `test_mqtt_real.py` → `os.environ.get("MQTT_BROKER","localhost")` | — | MASTER-H7 | 10 мин |
| QW-25 | SSE test failures — удалить или xfail тесты `test_sse_*` после MASTER-C4 выпила | tests | MASTER-C4 | 15 мин |
| QW-26 | Удалить `services/scheduler_service.py` dead stub (target-state Phase 4.8) | — | MASTER-L1 | 5 мин |

**Итого 26 quick wins, оценочно 5–7 часов совокупно.** Можно сделать за одну субботу и закрыть ≈60% Волны 1 + значительную часть a11y / frontend hygiene.

---

## 8. Что НЕ трогать (don't break)

Список вещей, которые **работают правильно**. При чистке не откатывать и не «улучшать ради улучшения».

| # | Компонент | Почему не трогать |
|---|---|---|
| 1 | `db/` репозиторный слой — `BaseRepository`, `db/zones.py`, `db/programs.py`, … | Правильная абстракция. Задача — сделать её **обязательной**, не переписывать. |
| 2 | Разделение `routes/` на page routes (`routes/pages.py`) и JSON API (`routes/*_api.py`) | Корректный шаг к thin controllers. Продолжать паттерн, не откатывать. |
| 3 | `monitors/` пакет — `float_monitor.py`, `mqtt_status_monitor.py`, `weather_monitor.py` | Самый зрелый extracted package. Каждый — единица lifecycle. Служит образцом для остальных. |
| 4 | `services/locks.py` — namespaced locks | Хорошая concurrency-abstraction. Не переписывать. |
| 5 | Graceful shutdown | Прод uptime 13.5 дней подтверждает стабильность. |
| 6 | SSR (Jinja) + asset versioning | Правильный выбор для ARM edge-устройства. Не тянуть SPA/bundler (Vue/React) — overhead не окупается. |
| 7 | Optimistic UI с rollback в `zones.js` | Хороший UX-паттерн. Даже при SSE-выпиле его сохранить. |
| 8 | `MockMQTTClient` + тестовая инфраструктура | Единственная опора для тестирования MQTT-слоя. Не менять API. |
| 9 | WAL mode + busy_timeout **там, где применяются** | Правильные defaults. Задача — применить **везде** (MASTER-H2), а не менять на что-то другое. |
| 10 | APScheduler как движок scheduler | Адекватно для масштаба. Не переходить на Celery/RQ. Проблема в `MemoryJobStore` fallback, не в APScheduler. |
| 11 | SQLite как БД | Для 24 зон / 1 программы / 8339 log rows — достаточно. **Не** мигрировать на PostgreSQL, это +150 MB RAM постоянно. |
| 12 | Jinja SSR для `programs.html` | Подход правильный. Проблема в 545 LOC inline (MASTER-H9), не в SSR. Сохранить SSR, вынести inline в файл. |
| 13 | `basic_auth_proxy` на `:8011` | Работает. Использовать его (или перейти на CF Access, что и рекомендуется). Не удалять. |
| 14 | aiogram как TG bot framework | Полисинг 15 KB RSS — приемлемо. Не переходить на webhooks (CF tunnel dependency). |
| 15 | Service Worker | Есть, работает. Дополнить `manifest.json` (QW-17) — не трогать SW code. |
| 16 | `bot_idempotency` таблица | Хороший pattern. Переиспользовать для `command_log` (MASTER-C3). |
| 17 | Существующие 35 миграций | Пересчитать порядок не надо. Нужен фикс атомарности (MASTER-H4), сами миграции корректны. |
| 18 | Hypercorn ASGI server | Работает. Не переходить на gunicorn/uwsgi. |
| 19 | `services/notifications.py` | Stateless utility, чистая cohesion. |
| 20 | Cloudflare Tunnel | Работает. Не убирать (альтернативы хуже для домашней системы). Проблема — направление туннеля (на :8080 вместо :8011/nginx), см. MASTER-H6. |

---

## 9. Открытые вопросы для владельца (Raul)

Без ответов на эти вопросы Волна 1–2 не может быть запущена полностью. Отсортированы по блокирующему значению.

### 9.1. 🔴 SSE — выпил или вернуть? (блокирует ~5 master-points)

**Контекст:** архитектор рекомендует выпилить полностью (target-state §11). Фронт уже отключил, бэк жив, браузер бесконечно реконнектится. Альтернатива — вернуть с heartbeat/bounded queue/disconnect detection.

**Вопрос:** готов ли согласиться на **5-сек задержку в UI** после manual start/stop при polling (вместо текущих «теоретических» <1 сек с SSE, которые сейчас всё равно не работают)?

**Влияет на:** MASTER-C4, MASTER-L2, 4 failing tests, ~5 дней work (S выпил vs L возврат).

**Рекомендация:** **выпилить**. Если позже понадобится real-time — MQTT.js в браузере (Phase-future, чистое решение).

### 9.2. 🔴 Secrets storage — `LoadCredential` vs `EnvironmentFile`?

**Контекст:** `LoadCredential` (systemd ≥250) безопаснее — secrets не видны в `systemctl show`, читаются из `$CREDENTIALS_DIRECTORY/...`. `EnvironmentFile=` проще, но secrets в env vars.

**Вопрос:** выбрать вариант. Для домашней системы `EnvironmentFile` обычно достаточно.

**Влияет на:** MASTER-M1 effort (S vs M — `LoadCredential` требует рефактора читалки).

**Рекомендация:** **EnvironmentFile=** для всего, кроме MQTT password и DB encryption key (если появится) — их через `LoadCredential=`. Баланс.

### 9.3. 🔴 Cloudflare Access — включаем? Какой identity provider?

**Контекст:** без CF Access `poliv-kg.ops-lab.dev` защищён только Flask-session. Рекомендация — включить CF Access Application с OAuth.

**Вопросы:**
- Какой IdP: Google / GitHub / GitLab / Microsoft?
- Список allow-emails (владелец + кто ещё?).
- Session TTL (рекомендуется 24 ч).

**Влияет на:** MASTER-H6, PHYS-5, SEC-002. Без этого публичный URL остаётся на Flask-session-only.

**Рекомендация:** Google OAuth + 2–3 email. Настройка — 2 часа на стороне CF dashboard.

### 9.4. 🟡 Deploy trigger — manual или semi-auto?

**Контекст:** `update_server.sh` работает через SSH manual. Альтернатива — cron daily на WB проверяет git tag `deploy` → если есть, pull + restart + readyz check + rollback.

**Вопрос:** manual (контроль) vs semi-auto (удобство)?

**Рекомендация:** **manual** — для embedded single-host с physical hardware более безопасно.

### 9.5. 🟡 Telegram bot — тот же процесс или отдельный systemd unit?

**Контекст:** сейчас aiogram thread внутри Flask-процесса. Можно вынести в `wb-irrigation-bot.service` — изолированный user, только read-DB + MQTT-publish.

**Вопрос:** выносить сейчас или позже?

**Рекомендация:** сейчас — оставить один процесс, но через MQTT contract (target-state §10.3 «вариант A»). Вынести позже если бот станет активнее.

### 9.6. 🟡 MQTT contract rollout — dual-publish 2 недели или сразу cutover?

**Контекст:** новый namespace `wb-irrigation/...` vs старые топики (`/devices/...` + ad-hoc). Dual publish безопаснее, но удваивает MQTT трафик на 2 недели.

**Рекомендация:** **dual publish 2 недели**, мониторить обе, потом cutover.

### 9.7. 🟡 MQTT protocol version — 3.1.1 или 5.0?

**Контекст:** Mosquitto поддерживает обе. v5 даёт `session_expiry` (inflight recovery), user properties, content-type marker. v3.1.1 — универсально совместимо.

**Рекомендация:** **v3.1.1** сейчас (меньше рисков с WB rule engine), переход на v5 по необходимости.

### 9.8. 🟡 Password rehash — lazy (on login) или batch?

**Контекст:** если на argon2id, то мигрировать пользователей. Lazy проще (на login проверить старый формат, rehash при успехе), batch требует знания пароля (нельзя).

**Рекомендация:** **lazy rehash**, через 6 мес удалить legacy support.

### 9.9. 🟡 Backup retention — 30 дней на SD достаточно или нужен offsite?

**Контекст:** 33 GB free на SD, DB ~1 MB. 30 дней = 30 MB, ок. Offsite (S3 / Dropbox / home NAS) защищает от пожара/кражи.

**Вопрос:** offsite включать?

**Рекомендация:** 30 дней на SD **плюс** weekly rsync на home NAS — оптимум без облачной зависимости.

### 9.10. 🟢 Guest role в TG bot — нужна или упразднить?

**Контекст:** сейчас таблица `bot_users` пустая (0 строк), whitelist чат-id в коде. Guest role — опциональная read-only.

**Рекомендация:** упразднить до `admin`/`user`, упростить модель.

### 9.11. 🟢 API версионирование — `/api/v1` или сразу `/api/v2`?

**Контекст:** если первая версия вводит breaking change (`command_id` required в запросах), можно назвать её v2, оставив legacy v1.

**Рекомендация:** **`/api/v1`**, breaking change зафиксировать как v1.0 → v2.0 если потребуется.

### 9.12. 🟢 MFA для CF Access — обязательно или one-tap?

**Контекст:** Google one-tap — быстрый UX. Required 2FA — безопаснее.

**Рекомендация:** **required 2FA** (TOTP или Google one-tap с 2FA enforcement) для критичной системы.

### 9.13. 🔴 FK миграция — risk tolerance на ~5 сек downtime?

**Контекст:** MASTER-H5 требует recreate-table для добавления FK. ~5 сек downtime, full DB rewrite ~1 MB.

**Вопрос:** выполнить ночью (04:00)? С backup до и rollback-планом?

**Рекомендация:** **да**, в окне 04:00–04:30 (до утреннего окна полива), с backup + readyz check + автоматический rollback при fail.

### 9.14. 🔴 WB rule engine — кто пишет правило публикации `zone/+/observed`?

**Контекст:** MASTER-C1 / PHYS-1 требует, чтобы контроллер WB публиковал echo GPIO в MQTT (реальное состояние реле). Это **вне Python-кода**, это WB-rules config.

**Вопрос:** владелец пишет WB-rule сам, или нужен запрос WB-Techpom?

**Рекомендация:** владелец пишет (простое правило: `whenChanged(gpio) → publish observed`). От этого зависит, когда можно начать Волну 2.

### 9.15. 🟢 Prometheus — через Telegraf/Zabbix или отдельный Prometheus?

**Контекст:** Telegraf уже стоит на WB (Zabbix 10.10.61.96 использует). `inputs.prometheus` Telegraf может scrape `/metrics` → Zabbix.

**Рекомендация:** **через Telegraf/Zabbix**, не поднимать отдельный Prometheus.

### 9.16. 🟢 Argon2id parameters — 200 мс или 100 мс login latency?

**Рекомендация:** `memory=32MB, time=2, parallelism=1` — ~200 мс на WB, приемлемо.

### 9.17. 🟢 Runtime settings — миграция 22 существующих rows в `type`/`category`?

**Рекомендация:** маппинг вручную (один commit), потом UI использует `category` для секционирования.

### 9.18. 🟢 TLS на Mosquitto WS (18883) — нужно?

**Рекомендация:** сейчас нет, когда браузер MQTT-client (future) — понадобится cert на `poliv-kg.ops-lab.dev`.

---

**Блокирующие** для Волны 1–2: **#1 SSE, #2 Secrets, #3 CF Access, #13 FK, #14 WB rule**. Остальные — итеративно.

---

## 10. План исполнения Phase 5 (execution)

Phase 5 — переход от документа к коду. Предлагаемая структура, **мнение commander'а**, не директива.

### 10.1. Формат работы

- **Eng lead:** Raul (владелец, единственный developer).
- **Командная модель:** один developer + 3 агент-помощника (security-engineer, DB-expert, frontend/a11y).
- **Tracking:** GitHub Issues с метками `wave-1`, `wave-2`, ..., `quick-win`, `phys-risk`, `a11y`.
- **PR policy:** каждый PR ≤ 500 LOC diff, отдельный master-point или quick-win, ссылка на master-ID в PR title (`MASTER-C2: add import logging to 4 modules`).
- **Branch policy:** feature branches → `refactor/v2`, tags `v2.0.0`, `v2.0.1`, …

### 10.2. Sequencing

```
Week 1:   Волна 1 (observability + CI + PRAGMA + SQLAlchemy)
Weeks 2-3: Волна 2 (физические риски — MQTT pool → state machine → reconciler)
Week 4:   Волна 3 (SSE выпил + /metrics + systemd + frontend hygiene)
Week 5:   Волна 4 начало (a11y top-3 + XPASS ревизия)
Weeks 6-7: Волна 4 (тесты critical paths 80%+)
Weeks 8-12: Волна 5 (god-modules, FK migration, secrets, API v1)
```

### 10.3. Definition of Done per master-point

Каждый master-point считается закрытым когда:
1. Код-изменение в PR, код-ревью одним агентом.
2. Тесты написаны или обновлены; CI зелёный.
3. Если затрагивает физику — прогон на staging/mock окружении + smoke-test manual один раз.
4. Если затрагивает прод-БД — backup до + rollback-план.
5. Документация обновлена: BUGS-REPORT статус, `CHANGELOG.md` или эквивалент.
6. Master-point в этом отчёте помечен статусом «DONE» при следующем пересмотре.

### 10.4. Review cadence

- **Daily (10 мин):** прогресс, блокеры, security incident review (посмотреть `app.log` через `journalctl`).
- **Weekly (30 мин):** статус master-points, переопределение приоритета по появляющимся данным, метрики (см. §11).
- **Monthly:** квартальный review — обновить этот отчёт, перенести DONE в секцию «закрыто», переоценить severity.

### 10.5. Staging / game-day

После Волны 2 — **game day**: симуляция 4 сценариев пролива (PHYS-1 подсценарии):
1. Power loss посреди полива зоны 5.
2. MQTT-disconnect во время stop-команды.
3. Одновременный user-stop + watchdog-stop (race).
4. `verify_async` timeout.

Цель — убедиться, что reconciler закрывает клапан в каждом сценарии ≤ 30 сек. Если нет — Волна 2 не закрыта.

### 10.6. Rollback plan

Для каждой Волны:
- Backup БД до начала.
- Tag `pre-wave-N` на git commit.
- Если после deploy readyz не зелёный >5 мин — rollback (`git checkout pre-wave-N && systemctl restart`).
- Для миграций БД (Волна 5, FK): дополнительно — restore БД из backup.

### 10.7. Communication

- **Raul → агенты:** формулировка задач через этот отчёт (master-ID ссылки).
- **Агенты → Raul:** статус в Telegram (короткий формат, см. финальное сообщение внизу) после каждого закрытого master-point.
- **Инциденты (если клапан проливает):** немедленно `/stopall` в TG bot + ручная проверка в поле + post-mortem через 48 ч.

---

## 11. Метрики успеха

Как понять, что квартальный план закрыт **фактически**, не на бумаге. Метрики откалиброваны под домашний single-user scope.

### 11.1. Safety (физика) — primary

| Метрика | Целевая | Как мерить |
|---|---|---|
| Число инцидентов «клапан открыт > 2× cap-time» | **0 / квартал** | `wb_zone_fault_total`, алерт в Zabbix; ручной learning log |
| MTTR на «клапан не закрывается» | < 30 сек | reconciler-retry duration, histogram `wb_observed_ack_latency_ms` |
| % команд с observed-ack ≤ 2 сек | ≥ 99% | `wb_observed_ack_latency_ms` p99 < 2000 мс |
| Число пролитой воды в инцидентах | 0 литров в квартал (идеально) | learning log, visual |

### 11.2. Observability

| Метрика | Целевая | Как мерить |
|---|---|---|
| Размер `app.log` за 24 ч | > 100 KB (пишется) | `du -sh /mnt/data/irrigation-logs/app.log` |
| `/readyz` доступность (зелёный / всего запросов) | ≥ 99.5% (uptime SLO) | Zabbix ping |
| Correlation-ID в 100% WARN/ERROR логов | 100% | grep `correlation_id=` / grep `level=(WARN|ERROR)` |
| MTTD (mean time to detect) инцидента через алерты | < 5 мин | Zabbix trigger history |

### 11.3. Процесс / CI

| Метрика | Целевая | Как мерить |
|---|---|---|
| % коммитов `refactor/v2` через CI | 100% | GitHub Actions history |
| Test coverage — критические пути (zone_control, watering_api) | ≥ 80% | `coverage.py` report |
| Test coverage — domain layer (pure) | ≥ 95% | — |
| Детерминированных failing tests | 0 | pytest run |
| XPASS тестов | 0 | pytest run (грепнуть `XPASS`) |
| Время CI от push до зелёного | < 5 мин | GitHub Actions |

### 11.4. Security / boundary

| Метрика | Целевая | Как мерить |
|---|---|---|
| Анонимный доступ на `poliv-kg.ops-lab.dev` | 0 | CF Access logs |
| Default admin password в использовании | 0 | ручная проверка после setup |
| MQTT anonymous connections / день | 0 | mosquitto log |
| Secrets в `db.settings` | 0 | grep table content |
| sudo/root-run процессов | 0 (все под `wb-irrigation` user) | `ps aux` + systemd unit |

### 11.5. Frontend / UX

| Метрика | Целевая | Как мерить |
|---|---|---|
| WCAG 2.2 Level A | PASS | axe-core + manual checklist |
| Contrast на status-page | все ≥ 4.5:1 | axe-core |
| Dead JS (bytes) | 0 | grep imports |
| SSE reconnect storms | 0 | mosquitto connection log |
| Polling requests при `document.hidden` | < 2/min | request log (если есть) |

### 11.6. Architecture

| Метрика | Целевая | Как мерить |
|---|---|---|
| Direct `sqlite3.connect()` вне `db/base.py` | 0 | `grep -r sqlite3.connect services/ routes/ monitors/` |
| MQTT clients per process | ≤ 3 | `netstat -an | grep 1883` |
| Per-request MQTT connect | 0 | mosquitto log rate, `wb_mqtt_publish_total{result="new_conn"}` |
| LOC в `irrigation_scheduler.py` | 0 (файл удалён) | wc -l |
| LOC в `services/weather.py` | < 400 (разобран) | wc -l |
| Duplicate job functions | 0 | grep dotted paths |

### 11.7. Сводка KPI на конец квартала

**Минимально приемлемый результат (baseline):**
- Волна 1 закрыта (логи + CI + PRAGMA + SQLAlchemy).
- Волна 2 ≥ 50% закрыта (MQTT pool + state machine + reconciler частично).
- 0 water-incidents в квартал.
- WCAG Level A — top-3 для полевого оператора закрыты.

**Целевой результат (stretch):**
- Волны 1–3 полностью закрыты.
- Волна 4 ≥ 70%.
- Coverage критических путей 80%+.
- `/metrics` → Zabbix → alerting работает.

**Аспирационный результат (не факт, что успеется):**
- Все 5 волн закрыты.
- God-modules разобраны.
- API v1 + OpenAPI.

---

## Appendix: cross-reference index

Для быстрой навигации от агентской находки к master-point.

| Agent finding ID | Master-point | Section |
|---|---|---|
| CRIT-R1, CRIT-R3, CRIT-RC1, CRIT-CC1 (SRE) | PHYS-1, MASTER-C1 | §3, §4 |
| CRIT-O1 (SRE, app.log пустой) | MASTER-C2 | §4 |
| HIGH-R2 (verify_async fire-and-forget) | MASTER-C1 | §4 |
| HIGH-R3 (MemoryJobStore fallback) | PHYS-2, MASTER-H10 | §3, §4 |
| HIGH-R6 (TimeoutStopSec=20) | MASTER-M2 | §4 |
| HIGH-O1 (telegram.txt rotation) | MASTER-C2 | §4 |
| CQ-001..004 (logger missing) | MASTER-C2 | §4 |
| CQ-005 (duplicate jobs) | MASTER-H1 | §4 |
| CQ-006 (boot-sync duplicate) | MASTER-H1 | §4 |
| CQ-007 (_delayed_close thread) | MASTER-C1 | §3 |
| CQ-012 (basicConfig in scheduler) | MASTER-C2 | §4 |
| DB-001 (FK=OFF) | MASTER-H2 | §4 |
| DB-002 (миграции не атомарны) | MASTER-H4 | §4 |
| DB-003 (FK не декларированы) | MASTER-H5 | §4 |
| DB-004 (synchronous=FULL) | MASTER-H2 | §4 |
| DB-005 (нет backup теста) | MASTER-M4 | §4 |
| DB-006 (weather.py прямые connect) | MASTER-H3, PHYS-4 | §3, §4 |
| DB-007 (layer bypass) | MASTER-H3 | §4 |
| DB-009 (composite indices) | Волна 5 | §6 |
| DB-012 (prefetch) | Волна 5 | §6 |
| DB-016 (backup retention) | MASTER-M4 | §4 |
| API-01 (per-request MQTT) | MASTER-C3 | §4 |
| SSE-01..04 (performance) | MASTER-C4 | §4 |
| SEC-002 (CF Tunnel bypass) | MASTER-H6 | §4 |
| SEC-010 (TG token storage) | MASTER-M1 | §4 |
| SEC default creds | MASTER-M3 | §4 |
| FE-CRIT-1 (SSE zombie) | MASTER-C4 | §4 |
| FE-HIGH-1 (dead JS) | MASTER-H9 | §4 |
| FE-HIGH-2 (inline script dup) | MASTER-H9 | §4 |
| FE-HIGH-3 (mqtt.html SSE no backoff) | MASTER-C4 | §4 |
| FE-HIGH-4 (polling no document.hidden) | MASTER-M6 | §4 |
| FE-HIGH-5 (no double-submit) | MASTER-M6 | §4 |
| FE-HIGH-6 (status.js 2187 LOC) | MASTER-H9 / MASTER-L1 | §4 |
| FE-MED-1..9 | MASTER-M6 | §4 |
| a11y 5 critical | MASTER-C6 | §3, §4 |
| a11y contrast 25+ | MASTER-C6 | §4 |
| tests CI не бежит | MASTER-C5 | §4 |
| tests 55 XPASS | MASTER-M7 | §4 |
| tests 8 failing (4 SSE, 4 other) | MASTER-H7 + MASTER-C4 | §4 |
| tests 44× time.sleep | MASTER-H7 | §4 |
| tests hardcoded IP | MASTER-H7 | §4 |
| Bug #1 (scheduler dual) | MASTER-H1 | §5 |
| Bug #2 (config scatter) | MASTER-H8 | §5 |
| Bug #3 (per-request MQTT) | MASTER-C3 | §5 |
| Bug #4 (app.log пустой) | MASTER-C2 | §5 |
| Bug #5.1 (CSRF exempt) | MASTER-H6 + MASTER-H9 | §5 |
| Bug #5.2 (boot-sync dup) | MASTER-H1 | §5 |
| Bug #5.3 (mqtt.html SSE) | MASTER-C4 | §5 |
| Bug #6 (SSE zones.js) | MASTER-C4 | §5 |
| Bug #7 (no SQLAlchemy) | PHYS-2 / MASTER-H10 | §5 |
| Bug #8 (default admin) | MASTER-M3 | §5 |
| Bug #9 (weather.py) | MASTER-H3 + MASTER-L1 | §5 |
| Bug #10 (FK=OFF) | MASTER-H2 | §5 |
| Bug #11 (miграции не атомарны) | MASTER-H4 | §5 |
| Bug #12 (scheduler не удалён) | MASTER-H1 | §5 |
| Bug #13 (CI на main) | MASTER-C5 | §5 |
| SP-1..8 (current-state architecture) | MASTER-C1..MASTER-H6 | §4 |

---

**Конец отчёта.**

*Версия 1.0, 2026-04-19, aудит выполнен в пределах Phase 4 мандата incident-response commander.*
*Следующий review: через 2 недели (после Волны 1) — отметить DONE, переоценить приоритеты на основании появившихся логов.*

