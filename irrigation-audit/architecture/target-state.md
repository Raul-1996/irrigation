# Target-State Architecture — wb-irrigation

**Дата:** 2026-04-19
**Архитектор:** backend-architect
**Сценарий:** pragmatic evolution — не переписывать с нуля, двигаться малыми рефакторингами
**Ограничения:** ARM Wirenboard 7 (aarch64, 4-core, 3.8 GiB RAM, eMMC с wear-лимитом), Python 3.9.2 в проде, физические клапаны (ошибка = пролитая вода / сгоревший насос)
**Основан на:** `landscape.md`, `prod-snapshot.md`, 8 findings, `ARCHITECTURE-REPORT.md`, `architecture/current-state.md` (software-architect)

---

## Executive Summary

Текущее состояние — незаконченный рефакторинг с двойным владением (MQTT, scheduler, SSE, logging) и расхождением БД↔hardware. Целевое состояние — **не микросервисы, не PostgreSQL, не Kubernetes**. Это тот же моно-процесс Flask, но с чёткими контрактами между слоями, детерминистичной моделью состояния зоны и явным observability-контуром.

### Топ-5 архитектурных изменений по приоритету

1. **Command → State → Observation state machine** (P0). Единая модель `desired → commanded → observed → confirmed` для зон/master valves с reconciliation loop. Убирает CRIT-R1/R3, CRIT-RC1, CRIT-CC1 — физический клапан и БД больше не расходятся.
2. **MQTT contract с taxonomy + QoS matrix + command_id идемпотентностью** (P0). Чёткие топики `wb-irrigation/zone/{id}/command|desired|state|observed`, schema_version, ACL per-client identity. Уничтожает класс багов "потерянный publish при reconnect".
3. **SQLite остаётся, но через единую factory с PRAGMA** + APScheduler на SQLAlchemyJobStore (персистентный) (P0). FK=ON, `synchronous=NORMAL`, `busy_timeout=30s`, FK-декларации. Решает DB-001/004/007 и HIGH-R3.
4. **Domain layer как чистая логика** (P1). Из `services/` вынести pure-functions (zone state transitions, weather adjustment formulas, program expansion) в `domain/`. Остаётся на 3-layer (routes/services/db), но с чётким domain-ядром — не full DDD.
5. **Observability minimum для embedded** (P1). `/healthz`+`/readyz`+`/metrics` (prometheus-client, ~200 KB), structured JSON logging через root logger, correlation_id. Закрывает Bug #4 (диагностический blackout 13 дней).

### Что НЕ делаем (явно)

- Не мигрируем на PostgreSQL. RAM hit на ARM не оправдан для 24 зон/1 программы/8339 log rows.
- Не вводим микросервисы. Единый процесс, IPC через MQTT уже есть для контроллера.
- Не ставим Jaeger/OTLP traces. На ARM overhead не окупается; log correlation_id достаточно.
- Не делаем CQRS/Event Sourcing. Домен маленький, ACID SQLite справляется.
- Не вводим WebSocket. MQTT уже есть; для UI — либо SSE с heartbeat, либо polling (см. §11).
- Не переходим на Alembic. Для 35 миграций и стабильного домена свой runner проще, его нужно только починить (DB-002).

---

## 1. Module Structure (Target)

### 1.1 Целевая декомпозиция

Остаёмся на **улучшенной 3-layer** с выделенным `domain/`-ядром без I/O. Не full hexagonal/DDD — зоны и программы слишком стабильны чтобы окупить overhead.

```
/
├── app.py                       # Flask bootstrap (≤150 LOC, сейчас 445)
├── run.py                       # Hypercorn entry (≤30 LOC)
├── config.py                    # ВСЕ env + defaults, single source
│
├── domain/                      # NEW. Pure logic, zero I/O, zero framework
│   ├── zone/
│   │   ├── state_machine.py    # DesiredState, CommandedState, ObservedState, transitions
│   │   ├── commands.py         # StartZoneCommand, StopZoneCommand (dataclasses)
│   │   └── validators.py       # duration bounds, group membership
│   ├── program/
│   │   ├── expansion.py        # program → list[ZoneRun] (сейчас в db.zones.compute_next_run_for_zone)
│   │   ├── weather.py          # adjustment formulas (pure math из services/weather.py)
│   │   └── postpone.py         # postpone rules, rain delay
│   ├── schedule/
│   │   └── next_run.py         # cron expansion, DST handling
│   └── weather/
│       └── decision.py         # skip-rain, threshold calc (pure)
│
├── application/                 # NEW. Use-cases, orchestration (сейчас свалка services/)
│   ├── zone_service.py         # start_zone, stop_zone (coordinates domain + infra)
│   ├── program_service.py      # run_program use-case
│   ├── reconciler.py           # Command→Observed loop (см. §4)
│   └── boot_sync.py            # startup reconciliation
│
├── infrastructure/              # NEW. Adapters (что в services/ было I/O)
│   ├── mqtt/
│   │   ├── client_pool.py      # single per-broker paho-client, no per-request connect
│   │   ├── publisher.py        # publish с retry+command_id
│   │   ├── subscriber.py       # подписки observed_state, rain, float
│   │   └── contract.py         # topic taxonomy + schema serialisation
│   ├── scheduler/
│   │   ├── runner.py           # APScheduler wrapper, одна копия jobs
│   │   └── jobs.py             # job_run_program, job_stop_zone (единственная копия, см. CQ-005)
│   ├── telegram/
│   │   ├── bot.py              # aiogram adapter
│   │   └── handlers.py         # commands
│   └── weather_http/
│       └── open_meteo.py       # HTTP client, кеш
│
├── db/                          # Repositories (уже есть, доработать)
│   ├── base.py                 # _connect() с PRAGMA FK+sync+busy_timeout
│   ├── zones.py                # + FK cleanup в delete_zone
│   ├── programs.py
│   ├── weather.py              # NEW. Забрать прямые connects из services/weather.py (DB-006)
│   ├── migrations.py           # атомарные миграции (DB-002 fix)
│   └── ...
│
├── routes/                      # Thin controllers (≤50 LOC per handler)
│   ├── api/v1/                 # версионированный API
│   │   ├── zones.py
│   │   ├── programs.py
│   │   └── ...
│   └── pages.py                # HTML page routes
│
└── observability/               # NEW
    ├── metrics.py              # prometheus_client registry
    ├── health.py               # /healthz /readyz
    └── log_config.py           # единая точка setup_logging (убить basicConfig из scheduler)
```

### 1.2 Границы доменов

| Домен | Ответственность | Зависит от | Не знает про |
|---|---|---|---|
| **Zone** | state machine одной зоны, duration, weather adjustment ratio | — | MQTT, DB, scheduler |
| **Program** | expansion в runs, postpone, cron | Zone (ids) | MQTT, scheduler APIs |
| **Weather** | decision "skip/reduce/normal" per zone | — | HTTP, DB |
| **Schedule** | next_run_time calc | Program, Zone | APScheduler internals |
| **Telegram** | адаптер, не домен | Zone/Program read-only | — |

Правило: `domain/*` импортирует только `domain/*` и stdlib. Это даёт unit-тесты без sqlite/MQTT — сейчас weather/zone тесты тянут реальный `sqlite3.connect()` (tests/findings §1).

### 1.3 Разбивка god-modules

#### `services/weather.py` (1404 LOC) →

| Целевой модуль | LOC (прибл.) | Ответственность |
|---|---:|---|
| `domain/weather/decision.py` | ~180 | pure: threshold, rain-skip, frost-skip, adjustment ratio |
| `domain/program/weather.py` | ~120 | pure: apply ratio к ZoneRun duration |
| `infrastructure/weather_http/open_meteo.py` | ~250 | HTTP client, retry, timeout |
| `infrastructure/weather_http/cache.py` | ~80 | TTL cache в RAM + fallback в БД |
| `db/weather.py` | ~200 | repository: weather_cache, weather_log, weather_decisions |
| `application/weather_service.py` | ~300 | orchestration use-case, логирует в БД |
| `routes/api/v1/weather.py` | ~100 | endpoints |
| **Итого** | ~1230 | с убранной дупликацией |

#### `irrigation_scheduler.py` (1365 LOC) + `scheduler/` (1258 LOC) →

Сейчас — дубликат jobs (CQ-005) + `logging.basicConfig` в 3 местах (CQ-012). Целевое:

| Целевой модуль | LOC |
|---|---:|
| `infrastructure/scheduler/runner.py` — только init APScheduler, SQLAlchemyJobStore, один lifecycle | ~200 |
| `infrastructure/scheduler/jobs.py` — **единственная** копия module-level job functions (APScheduler требует dotted-path, см. CQ-005) | ~300 |
| `application/program_runner.py` — orchestration: зоны по порядку, master valve, cancellations | ~400 |
| `application/zone_runner.py` — scheduler callback → application/zone_service.py | ~150 |
| `domain/schedule/next_run.py` — cron expansion (pure) | ~200 |
| `application/watchdog.py` — cap-time monitor (сейчас `services/watchdog.py`) | ~150 |
| **Итого** | ~1400 (с удалением дубля) |

---

## 2. MQTT Contract

### 2.1 Topic Taxonomy

**Namespace root:** `wb-irrigation/` (единый префикс, изолирует от WB system topics `/devices/...` и mosquitto `$SYS/*`).

```
wb-irrigation/
├── zone/{zone_id}/
│   ├── command              # POST-like: app → controller. QoS=1, NOT retained
│   ├── desired              # app → app. QoS=1, retained (survive restart)
│   ├── state                # application-observed state. QoS=1, retained
│   └── observed             # controller echo (GPIO feedback). QoS=1, retained
│
├── group/{group_id}/
│   ├── master/command       # master valve open/close. QoS=1, not retained
│   ├── master/state         # app's view. QoS=1, retained
│   ├── master/observed      # echo from controller. QoS=1, retained
│   └── sensors/
│       ├── pressure         # pressure sensor telemetry. QoS=0, not retained
│       ├── water-meter      # pulse counter. QoS=1, retained (counter не теряем)
│       └── float            # float sensor. QoS=1, retained (критичная защита)
│
├── program/{program_id}/
│   ├── command              # start/stop/skip. QoS=1, not retained
│   └── state                # running/idle/queued. QoS=1, retained
│
├── weather/
│   ├── conditions           # current API cache snapshot. QoS=0, retained
│   ├── forecast             # 24h/48h rain. QoS=0, retained
│   └── decision/{date}      # per-day skip/reduce decision. QoS=1, retained (audit)
│
├── system/
│   ├── health               # app heartbeat. QoS=0, retained
│   ├── events               # boot, shutdown, bugreport. QoS=1, not retained
│   └── metrics/{name}       # optional push to Zabbix. QoS=0, not retained
│
└── bridge/                   # для будущих bridges (внешний broker, не используется)
```

**Key правила:**

- `/command` — всегда **not retained**. Старая команда не должна повторяться при переподключении подписчика. Иначе: Telegram-бот ребут → re-delivery "start zone 5" из недели назад.
- `/desired` — **retained**. Пережить рестарт приложения, дать контроллеру восстановить целевое состояние.
- `/state` и `/observed` — **retained**. Новый подписчик сразу получает последнее известное состояние без polling.
- Per-zone, не single topic с JSON массивом. ACL per-zone возможен, wildcards `wb-irrigation/zone/+/state` работают.

### 2.2 Payload Schema

Все payloads — JSON UTF-8. Обязательное поле `schema_version`.

#### `zone/{id}/command`
```json
{
  "schema_version": 1,
  "command_id": "01HX9K7T3Z4QW2-42",
  "command": "start",
  "zone_id": 42,
  "duration_sec": 600,
  "source": "scheduler|api|telegram|manual",
  "issued_at": "2026-04-19T14:23:05.123Z",
  "reason": "program:3:run",
  "correlation_id": "req-abc123"
}
```

`command` ∈ `{start, stop, force_stop}`. `command_id` — ULID или `{ulid}-{zone}`, монотонно растёт, uniqueness в `bot_idempotency`-подобной таблице `command_log`.

#### `zone/{id}/desired`
```json
{
  "schema_version": 1,
  "desired_state": "on|off",
  "version": 142,
  "until": "2026-04-19T14:33:05Z",
  "command_id": "01HX9K7T3Z4QW2-42",
  "updated_at": "2026-04-19T14:23:05.234Z"
}
```

`version` монотонно растёт per zone (optimistic lock). Подписчик игнорирует payload если `version ≤ last_seen_version`.

#### `zone/{id}/state` (application-observed)
```json
{
  "schema_version": 1,
  "state": "off|starting|on|stopping|fault",
  "version": 142,
  "desired_version": 142,
  "started_at": "2026-04-19T14:23:05.456Z",
  "expected_off_at": "2026-04-19T14:33:05Z",
  "source": "api|scheduler|program|watchdog",
  "program_id": 3,
  "run_id": "01HX9K7T3Z-run",
  "fault_count": 0
}
```

#### `zone/{id}/observed` (hardware echo)
```json
{
  "schema_version": 1,
  "hw_state": "0|1",
  "observed_at": "2026-04-19T14:23:06.123Z",
  "source": "wb-rule|gpio-driver"
}
```

Приходит от контроллера (WB rule engine). Приложение подписывается и сравнивает с `desired`.

#### `group/{id}/master/command`
```json
{
  "schema_version": 1,
  "command_id": "01HX9K7T4-m1",
  "command": "open|close",
  "mode": "NO|NC",
  "reason": "zone:42:start|zone:42:stop:delayed",
  "issued_at": "2026-04-19T14:23:05Z"
}
```

### 2.3 QoS Matrix

| Topic pattern | QoS | Retained | Rationale |
|---|:-:|:-:|---|
| `zone/+/command` | **1** | no | at-least-once, не duplicate-replay на reconnect |
| `zone/+/desired` | 1 | **yes** | survive restart, single-source для reconciler |
| `zone/+/state` | 1 | **yes** | UI и подписчики сразу видят актуал |
| `zone/+/observed` | 1 | **yes** | восстановление hardware-truth |
| `group/+/master/command` | 1 | no | критично, не retained |
| `group/+/master/state|observed` | 1 | yes | — |
| `group/+/sensors/pressure` | **0** | no | высокочастотная телеметрия, потеря ок |
| `group/+/sensors/water-meter` | 1 | yes | pulse counter — учёт воды |
| `group/+/sensors/float` | 1 | yes | **safety-critical**, не теряем |
| `program/+/command` | 1 | no | — |
| `program/+/state` | 1 | yes | — |
| `weather/conditions` | 0 | yes | |
| `weather/decision/+` | 1 | yes | audit trail |
| `system/health` | 0 | yes | heartbeat, потерять ок |
| `system/events` | 1 | no | события, не replay |
| `system/metrics/+` | 0 | no | push-metrics |

**QoS=2 нигде**. На ARM накладные расходы не оправданы; command_id идемпотентность закрывает дубли QoS=1.

### 2.4 MQTT Clients & ACL

Четыре identity, каждая со своим username + ACL файл (`mosquitto.acl`):

| Client ID | Username | Password storage | Публикует | Подписывается |
|---|---|---|---|---|
| `wb-irrigation-app` | `irrigation_app` | `secrets.env` (chmod 600) | `zone/+/command`, `zone/+/desired`, `zone/+/state`, `group/+/master/command`, `group/+/master/state`, `program/+/state`, `system/+/+`, `weather/+` | `zone/+/observed`, `group/+/master/observed`, `group/+/sensors/+`, `program/+/command`, `system/events` |
| `wb-rule-engine` | `wb_controller` | wb-rules config | `zone/+/observed`, `group/+/master/observed`, `group/+/sensors/+` | `zone/+/command`, `group/+/master/command` |
| `telegram-bot` | `irrigation_tg` | `secrets.env` | `program/+/command`, `zone/+/command` | `zone/+/state`, `program/+/state`, `system/events` |
| `monitoring` | `telegraf_ro` | telegraf config | — | `wb-irrigation/#` (read-only) |

**ACL файл (пример):**
```
user irrigation_app
topic readwrite wb-irrigation/#

user wb_controller
topic read wb-irrigation/zone/+/command
topic read wb-irrigation/group/+/master/command
topic write wb-irrigation/zone/+/observed
topic write wb-irrigation/group/+/master/observed
topic write wb-irrigation/group/+/sensors/+

user irrigation_tg
topic read wb-irrigation/zone/+/state
topic read wb-irrigation/program/+/state
topic read wb-irrigation/system/events
topic write wb-irrigation/program/+/command
topic write wb-irrigation/zone/+/command

user telegraf_ro
topic read wb-irrigation/#
```

Убирает текущую проблему (18 клиентов на брокере под одним anonymous user, нет изоляции).

### 2.5 Идемпотентность

**Проблема:** при QoS=1 + reconnect возможна re-delivery → "start zone" применяется дважды.

**Решение:**
1. `command_id` (ULID) в payload команды.
2. Таблица `command_log(command_id TEXT PRIMARY KEY, zone_id, received_at, outcome)`.
3. Handler: `INSERT OR IGNORE INTO command_log`; если `changes()=0` — команда уже обработана, no-op + log "duplicate".
4. TTL cleanup: rows старше 1 часа удаляются APScheduler-job'ой.

Аналог `bot_idempotency` таблицы, которая уже есть — переиспользовать pattern.

### 2.6 Connection Management

Убрать per-request `mqtt.Client().connect()` в `routes/system_status_api.py:464-478` (см. perf.md §3). Вместо:

- **Single pool**: `infrastructure/mqtt/client_pool.py` — dict `{broker_id: PahoClient}`, lazy init, `loop_start()` один раз, health-check через `is_connected()`.
- **Publish API**: `publish(broker_id, topic, payload, qos, retain) -> PublishResult` — возвращает статус + inflight handle.
- **Reconnect buffer**: на `on_disconnect` paho перезапустит; при QoS=1 inflight сохраняются paho внутри до `max_inflight_messages=100`. **Но**: если процесс упадёт до reconnect — inflight теряются (`clean_session=False` + `session_expiry` для MQTT5, см. 2.7).

### 2.7 MQTT5 vs 3.1.1

Mosquitto на WB поддерживает обе версии. Для контракта выбираем **MQTT 5.0**:

- `clean_start=False` + `session_expiry_interval=300` — session-level recovery inflight при рестарте (5 мин окно).
- Response-topic + correlation-data — не нужны (используем свой `correlation_id` в payload).
- User properties — опционально для `correlation_id` на transport-level (позже).
- `payload_format_indicator=1` + `content_type="application/json"` — UTF-8 marker.

Переход с 3.1.1 на 5 — флаг paho `protocol=MQTTv5`. Контроллер WB rule engine — проверить, подписан ли по v5 (обычно — 3.1.1; ok, v5 обратно совместим).

---

## 3. Command → State → Observation Pattern

Центральная архитектурная примитива. Закрывает CRIT-R1, CRIT-R3, CRIT-RC1, CRIT-CC1.

### 3.1 Модель состояний

Каждая зона имеет **четыре состояния** в разных слоях:

| Layer | Поле | Что означает | Storage |
|---|---|---|---|
| **Desired** | `desired_state ∈ {on, off}` + `desired_version` + `valid_until` | Чего мы хотим | `zones.desired_state`, `zones.desired_version`; mirror в MQTT retain `zone/+/desired` |
| **Commanded** | `commanded_state`, `command_id`, `commanded_at` | Что мы отправили через MQTT | `zones.commanded_state`, `zones.last_command_id` |
| **Observed** | `observed_state ∈ {0,1}`, `observed_at` | Что реально показывает hardware | `zones.observed_state` (updated from MQTT `zone/+/observed` subscriber) |
| **Confirmed** | `state ∈ {off, starting, on, stopping, fault}`, `fault_count` | Application view, используется UI/scheduler | `zones.state` |

**Инвариант:** `confirmed=on` только если `desired=on AND commanded=on AND observed=1 AND observed_at - commanded_at < ACK_TIMEOUT`.

### 3.2 State Machine

```
                  user/scheduler/telegram
                         │
                         ▼
                  [StartZoneCommand]
                         │
                         ▼
          ┌──────────────────────────────┐
          │ 1. BEGIN TRANSACTION          │
          │ 2. check desired_version      │
          │ 3. UPDATE desired_version++   │
          │    desired_state='on'         │
          │    state='starting'           │
          │ 4. INSERT command_log         │
          │ 5. COMMIT                     │
          └──────────────┬───────────────┘
                         │
                         ▼
          ┌──────────────────────────────┐
          │ publish zone/N/command QoS=1 │
          │ publish zone/N/desired       │  (retained)
          │   (idempotent, reconciler    │
          │    retries if no ack)        │
          └──────────────┬───────────────┘
                         │
                         ▼
          ┌──────────────────────────────┐
          │ wait_for_publish(timeout=2s) │
          │  ├─ ack OK → commanded=on    │
          │  └─ timeout → state=fault    │
          └──────────────┬───────────────┘
                         │
                         ▼
          ┌──────────────────────────────┐
          │ WB controller receives cmd   │
          │ ├─ GPIO open                 │
          │ └─ publish zone/N/observed=1 │
          └──────────────┬───────────────┘
                         │
                         ▼
          ┌──────────────────────────────┐
          │ app subscriber /observed     │
          │ ├─ UPDATE observed_state=1   │
          │ └─ if desired=on: state='on' │
          │    else:         reconcile   │
          └──────────────────────────────┘

                     Divergence:
          ┌──────────────────────────────┐
          │ Reconciler loop (every 10s): │
          │ for each zone:               │
          │   if desired != observed     │
          │     and now - commanded_at   │
          │         > ACK_TIMEOUT:       │
          │       re-publish command     │
          │       attempts++             │
          │       if attempts > 3:       │
          │         state='fault'        │
          │         raise alert          │
          └──────────────────────────────┘
```

### 3.3 Transition Table

| Current `state` | Event | Guard | Next `state` | Side-effects |
|---|---|---|---|---|
| `off` | `start_command(v, dur)` | `v > desired_version` | `starting` | publish `/command`, `/desired`; set `commanded_at` |
| `starting` | `observed=1 received` | `observed_at > commanded_at` | `on` | set `started_at`, schedule stop-job |
| `starting` | timeout 5s no observed | | `fault` | alert; retry reconciler |
| `on` | `stop_command(v)` | `v > desired_version` | `stopping` | publish `/command` off, `/desired` off |
| `on` | `observed=0 received` | | `fault` | unexpected off; close master, alert |
| `on` | `watchdog cap_exceeded` | `duration > cap` | `stopping` | force stop, log |
| `stopping` | `observed=0 received` | | `off` | set ended_at, clear jobs |
| `stopping` | timeout 5s no observed | | `fault` | retry reconciler |
| `fault` | `clear_fault_command` | manual/admin | `off` | reset fault_count |
| `*` | `boot_reconcile` | — | see §3.5 | |

### 3.4 Versioning (Optimistic Concurrency)

**Проблема:** race между user-stop и watchdog-stop (CRIT-CC1).

**Решение:** каждое изменение `desired_state` инкрементирует `desired_version`. UPDATE условный:

```sql
UPDATE zones
SET desired_state = :new_state,
    desired_version = desired_version + 1,
    last_command_id = :cmd_id,
    commanded_at = :now
WHERE id = :zone_id AND desired_version = :expected_version;
-- если rowcount=0 → conflict, retry или игнор (если уже желаемое состояние)
```

В MQTT payload `zone/+/desired` — `version` из БД. Подписчики сравнивают:

```python
def on_desired_message(msg):
    if msg.version <= last_seen_version[msg.zone_id]:
        return  # stale, ignore
    last_seen_version[msg.zone_id] = msg.version
    apply(msg)
```

Это убирает race: watchdog UPDATE с version=N, user UPDATE с version=N+1 — оба атомарны, "старый" не перезатрёт новое.

### 3.5 Boot Reconciliation

Закрывает CRIT-RC1 (power loss с открытым клапаном).

```python
def boot_reconcile(db, mqtt):
    # 1. Подписаться на zone/+/observed (retained) — получаем реальное hw-состояние
    observed = mqtt.await_retained_snapshot(
        'wb-irrigation/zone/+/observed',
        timeout=5.0
    )  # {zone_id: hw_state}

    # 2. Подписаться на zone/+/desired retained — восстанавливаем last-known desired
    desired = mqtt.await_retained_snapshot(
        'wb-irrigation/zone/+/desired',
        timeout=5.0
    )

    # 3. Для каждой зоны: решение
    for zone in db.get_zones():
        zid = zone['id']
        hw = observed.get(zid, 'unknown')
        db_desired = zone['desired_state']
        valid_until = zone['valid_until']

        # Safety-first policy: если сомнения — закрыть
        if hw == 1 and (
            db_desired != 'on'
            or (valid_until and parse(valid_until) < now())
        ):
            # Hardware ON но не должен быть
            log.warning('boot_reconcile.force_off', zone=zid, hw=hw, db=db_desired)
            publish_command(zid, 'stop', reason='boot_reconcile_safety',
                           command_id=generate_id())
            db.update_zone(zid, desired_state='off', state='stopping')
        elif hw == 1 and db_desired == 'on' and valid_until > now():
            # Legit: полив продолжается после рестарта
            remaining = valid_until - now()
            log.info('boot_reconcile.resume', zone=zid, remaining_sec=remaining)
            # re-schedule stop job в APScheduler
            schedule_zone_stop(zid, at=valid_until)
            db.update_zone(zid, state='on')
        elif hw == 0 and db_desired == 'off':
            # Normal idle
            continue
        elif hw == 0 and db_desired == 'on':
            # Desired был on, но hw off — фейл или успешный force-off
            log.warning('boot_reconcile.unexpected_off', zone=zid)
            db.update_zone(zid, desired_state='off', state='off',
                          fault_count=zone['fault_count'] + 1)

    # 4. Публикуем health event
    publish_event('boot_reconcile_complete',
                  zones_on=sum(1 for h in observed.values() if h == 1),
                  duration_ms=elapsed)
```

**Ключевое:** безопасность важнее продолжения полива. Если БД потеряла контекст (например, valid_until просрочен) — закрываем клапан.

### 3.6 Reconciliation Loop

APScheduler-job каждые **10 секунд**:

```python
def reconcile_tick(db, mqtt, metrics):
    for zone in db.get_zones_needing_reconcile():
        # query: WHERE desired_state != observed_state
        #        OR (state='starting' AND now-commanded_at > 5s AND observed_state != 1)
        #        OR (state='stopping' AND now-commanded_at > 5s AND observed_state != 0)

        if zone.attempts > 3:
            db.update_zone(zone.id, state='fault', fault_count=zone.fault_count+1)
            publish_event('zone_fault', zone_id=zone.id,
                         desired=zone.desired_state,
                         observed=zone.observed_state,
                         attempts=zone.attempts)
            metrics.wb_zone_fault_total.labels(zone_id=zone.id).inc()
            continue

        # Re-send command with same command_id (idempotent via command_log)
        publish_command(zone.id,
                       'start' if zone.desired_state == 'on' else 'stop',
                       command_id=zone.last_command_id,
                       reason='reconciler_retry')
        db.increment_reconcile_attempts(zone.id)
        metrics.wb_reconcile_retry_total.labels(zone_id=zone.id).inc()
```

Это **единственный путь** повторной отправки команды. Никаких `_delayed_close` thread'ов (CQ-007), никаких fire-and-forget `verify_async` (HIGH-R2).

### 3.7 Event Log (audit trail)

Каждое состояние-переход пишется в `zone_state_transitions`:

```sql
CREATE TABLE zone_state_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL REFERENCES zones(id),
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    event TEXT NOT NULL,              -- 'start_cmd', 'observed_on', 'watchdog_cap', ...
    command_id TEXT,
    correlation_id TEXT,
    source TEXT,                       -- 'api', 'scheduler', 'reconciler', 'watchdog'
    ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    meta JSON
);
CREATE INDEX idx_zst_zone_ts ON zone_state_transitions(zone_id, ts DESC);
```

Replacement для пустой `zone_runs` табличкой (которая на проде 0 rows — фича мёртвая).

---

## 4. Scheduler Architecture

### 4.1 JobStore: persistent SQLAlchemy

**Решение:** перевести APScheduler на `SQLAlchemyJobStore(url='sqlite:///jobs.db')`.

- `requirements.txt` +SQLAlchemy 1.4 (~3 MB на ARM, без alembic) +APScheduler 3.10.
- **Отдельный файл** `jobs.db`, не `irrigation.db` — не смешиваем application-data и scheduler-state, ускоряет backup (не нужно бекапить jobstore).
- `PRAGMA journal_mode=WAL; synchronous=NORMAL` на `jobs.db` для consistency с main DB.

**Trade-off vs MemoryJobStore:** +3 MB RSS, +1 SQLite файл, но закрывает HIGH-R3 (все jobs теряются при `systemctl restart`).

**НЕ** полагаемся на SQLAlchemyJobStore для critical-path: volatile one-shot jobs (zone-stop, master-close) **всё равно** должны восстанавливаться через `boot_reconcile` (§3.5). Jobstore — вторая линия defence, не первая.

### 4.2 Job types

```
┌──────────────┬───────────┬─────────────┬──────────────────────────┐
│ Job          │ Trigger   │ Jobstore    │ Restore on boot?         │
├──────────────┼───────────┼─────────────┼──────────────────────────┤
│ program:N    │ cron      │ default     │ Да, из SQLAlchemyJobStore│
│              │           │ (persist)   │ + idempotent verify vs DB│
│ zone_stop:N  │ date      │ default     │ Да (персистентно) +      │
│              │ (one-shot)│             │ cross-check с valid_until│
│ master_close │ date      │ default     │ Да +  проверка активных  │
│ reconcile    │ interval  │ default     │ Да, одна штука в приложе │
│ watchdog     │ interval  │ default     │ Да                       │
│ backup       │ cron 04:30│ default     │ Да                       │
│ log_cleanup  │ cron 03:00│ default     │ Да                       │
│ weather_poll │ interval  │ default     │ Да                       │
└──────────────┴───────────┴─────────────┴──────────────────────────┘
```

**Нет `volatile` jobstore.** Предыдущая попытка иметь два jobstore'а (HIGH-R3) создавала confusion. Вместо этого — все jobs персистентные, но с `coalesce=True, misfire_grace_time=60` для missed fires.

### 4.3 Разделение ответственности

```
┌───────────────────────────────┐
│ Planning layer (domain/)      │
│ - cron expansion              │  Pure, тестируется без APScheduler
│ - next_run calculation        │
│ - DST handling                │
└──────────────┬────────────────┘
               │ generates ScheduledRun objects
               ▼
┌───────────────────────────────┐
│ Execution layer (infra/)      │
│ - APScheduler runner          │  Infrastructure concern
│ - job persistence             │
│ - misfire handling            │
└──────────────┬────────────────┘
               │ fires callbacks
               ▼
┌───────────────────────────────┐
│ Orchestration (application/)  │
│ - program_runner              │  Use-case: run program,
│ - zone_runner                 │           sequence zones,
│ - open/close master valve     │           handle cancellations
└──────────────┬────────────────┘
               │ issues commands
               ▼
┌───────────────────────────────┐
│ Monitoring (application/)     │
│ - reconciler (10s)            │  Safety net
│ - watchdog (30s)              │
└───────────────────────────────┘
```

Planning не знает про APScheduler. Execution не знает про программы (получает callable + time). Orchestration не знает про cron parsing.

### 4.4 Устранение дубля `scheduler/jobs.py` vs `irrigation_scheduler.py`

**Решение (CQ-005):**

1. Оставить **одну** копию: `infrastructure/scheduler/jobs.py`.
2. Dotted path в jobstore: `infrastructure.scheduler.jobs:job_run_program`.
3. Миграционный скрипт однократно: `UPDATE apscheduler_jobs SET job_state = replace(...)` — перекодировать pickle, меняя dotted path. Либо — проще — на первом старте новой версии: прочитать `apscheduler_jobs`, очистить таблицу, пересоздать jobs из `db.programs` через `planning.next_run()`. Это надёжнее.

### 4.5 Misfire policy

```python
scheduler.add_job(
    func='infrastructure.scheduler.jobs:job_run_program',
    trigger='cron',
    args=[program_id],
    id=f'program:{program_id}',
    replace_existing=True,
    coalesce=True,              # пропущенные rollup в 1
    misfire_grace_time=300,     # 5 мин — если рестарт затянулся
    max_instances=1,            # одна программа одновременно
)
```

`misfire_grace_time=300`: если программа должна была запуститься 03:00, а app стартовал 03:02 — всё равно запустить (миссфайер 2 мин < 5 мин grace). Но если старт в 04:00 — skip, программа уже не актуальна.

---

## 5. Database Strategy

### 5.1 Решение: SQLite остаётся, через единую factory

**НЕ мигрируем на PostgreSQL.** Обоснование:

| Критерий | SQLite | PostgreSQL (Docker) |
|---|---|---|
| RAM overhead | ~4 MB (процесс sqlite нет, in-process) | ~100 MB postgres + 50 MB Docker runtime |
| eMMC wear | WAL + synchronous=NORMAL умеренно | аналогично + WAL postgres |
| FK enforcement | через PRAGMA (включить!) | native |
| Concurrent writes | 1 writer / N readers | true concurrent |
| Backup | `sqlite3 .backup` online | `pg_dump` |
| Для 24 зон / 1 программы / 8339 logs | **достаточно** | overkill |
| Ops complexity | +0 (builtin) | +Docker, +монитор, +port, +creds |

На проде: 0.27% CPU, 224 MB RSS. Нет симптомов SQLite bottleneck. Миграция на Postgres = +150 MB постоянно. Не оправдано.

### 5.2 НЕ переходим на SQLAlchemy ORM для app data

**Обоснование:** 60+ sqlite3 calls, ручной SQL, retry-decorator — работают. ORM дал бы миграции + session management, но:
- Для 35 миграций свой runner уже есть (нужно починить DB-002).
- Connection management закрывается `BaseRepository._connect()`.
- Performance: raw SQL быстрее, особенно для hot-path `db.update_zone` (CRIT-R3).
- Alembic добавил бы ещё один файл миграций и нарушил бы текущий registry.

Используем SQLAlchemy **только для APScheduler jobstore** (§4.1).

### 5.3 Целевая конфигурация SQLite

Единая factory `db/base.py`:

```python
class BaseRepository:
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        # Per-connection PRAGMA (каждый NEW connection)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')        # fix DB-004
        conn.execute('PRAGMA foreign_keys=ON')           # fix DB-001
        conn.execute('PRAGMA busy_timeout=30000')        # 30s wait-on-lock
        conn.execute('PRAGMA temp_store=MEMORY')
        conn.execute('PRAGMA cache_size=-8000')          # 8 MB
        conn.execute('PRAGMA wal_autocheckpoint=1000')
        conn.row_factory = sqlite3.Row
        return conn
```

**ВСЕ** repositories переходят на `with self._connect() as conn:` (fix DB-007). Прямые `sqlite3.connect()` в `services/weather.py` (7 мест) и `services/float_monitor.py` (1) заменяются на `db/weather.py` repo и `db/float.py` repo (fix DB-006).

### 5.4 FK декларации

SQLite требует пересоздания таблиц для добавления FK. План:

1. Новая миграция `add_foreign_keys_v2` — для каждой таблицы с FK:
   ```sql
   CREATE TABLE zones_new (... FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE SET NULL ...);
   INSERT INTO zones_new SELECT * FROM zones;
   DROP TABLE zones;
   ALTER TABLE zones_new RENAME TO zones;
   -- recreate indices
   ```
2. Пересоздание индексов.
3. `PRAGMA foreign_key_check` на каждом шаге.
4. Если check показывает orphans — логировать в `logs` и решать ручной командой (safety: не автоматически удалять).

Приоритет FK-деклараций:
- `zones.group_id → groups.id ON DELETE SET NULL`
- `zones.mqtt_server_id → mqtt_servers.id ON DELETE SET NULL`
- `groups.*_mqtt_server_id → mqtt_servers.id ON DELETE SET NULL` (4 колонки)
- `water_usage.zone_id → zones.id ON DELETE CASCADE`
- `weather_log.zone_id → zones.id ON DELETE CASCADE`
- `program_cancellations.*`, `program_queue_log.*`, `float_events.group_id`

### 5.5 Retention & archiving

| Таблица | Retention | Механизм |
|---|---|---|
| `logs` | 180 дней hot, архив 1 год | APScheduler job `log_cleanup` (03:15 ежедневно): `DELETE FROM logs WHERE ts < date('now','-180 days')` |
| `zone_state_transitions` | 90 дней | аналогично |
| `weather_cache` | 24 часа (current), 7 дней (forecast) | TTL-based, cleanup on write |
| `weather_log` | 365 дней | audit trail, долгоживущий |
| `command_log` | 1 час (idempotency window) | ежечасная cleanup job |
| `bot_audit` | 180 дней | ежемесячная |
| `migrations` | forever | — |

**Архивирование:** раз в квартал `sqlite3.backup()` → `/mnt/sdcard/archive/logs_YYYYQ.db`. 33 GB free на SD — хватит на годы.

### 5.6 Backup

APScheduler job ежедневно в **04:30** (после окна полива):

```python
def backup_job():
    src = db_path
    dst = f'/mnt/sdcard/backup/irrigation_{date.today():%Y%m%d}.db'
    with sqlite3.connect(src) as src_conn, sqlite3.connect(dst) as dst_conn:
        src_conn.backup(dst_conn)  # online backup API, WAL-safe
    # Keep 30 days (up from current keep_count=7 — DB-016)
    cleanup_old_backups(dir='/mnt/sdcard/backup', keep_days=30)
    # PASSIVE checkpoint, не TRUNCATE (не блокирует writers)
    with sqlite3.connect(src) as conn:
        conn.execute('PRAGMA wal_checkpoint(PASSIVE)')
```

### 5.7 Миграции — фикс атомарности (DB-002)

```python
def _apply_named_migration(self, conn, name, func):
    # Проверяем до транзакции
    if self._is_applied(conn, name):
        return

    try:
        conn.execute('BEGIN IMMEDIATE')
        func(conn)  # должна НЕ делать свой commit
        conn.execute('INSERT INTO migrations(name, applied_at) VALUES (?, ?)',
                     (name, datetime.utcnow().isoformat()))
        conn.execute('COMMIT')
        logger.info('migration.applied', name=name)
    except Exception as e:
        conn.execute('ROLLBACK')
        logger.error('migration.failed', name=name, error=str(e))
        raise  # fail fast, не swallow
```

+ все 35 миграций проверить: не делать внутренний `conn.commit()`. Для `ALTER TABLE ADD COLUMN` — всегда защищать `PRAGMA table_info` check.

---

## 6. Observability Stack

### 6.1 Logging — единая точка setup

**Проблема (Bug #4):** handler на logger `'app'` вместо root; `basicConfig(WARNING)` в scheduler перебивает.

**Решение:**

```python
# observability/log_config.py
def setup_logging(log_level='INFO', log_format='json'):
    root = logging.getLogger()
    root.setLevel(log_level)
    # Очистить всё что могли повесить basicConfig-ом
    root.handlers.clear()

    # File handler на root
    fh = TimedRotatingFileHandler(
        '/mnt/data/irrigation-logs/app.log',
        when='midnight', backupCount=14
    )
    fh.setFormatter(JSONFormatter() if log_format == 'json' else PlainFormatter())
    fh.addFilter(PIIFilter())
    fh.addFilter(CorrelationIdFilter())  # injects request_id/correlation_id
    root.addHandler(fh)

    # Console/stdout (для journal)
    sh = logging.StreamHandler()
    sh.setFormatter(JSONFormatter())
    root.addHandler(sh)

    # Отдельный handler для import-export (сохранить как было — рабочий)
    _setup_import_export_logger()
```

**Правила:**
- `logging.basicConfig()` удалить из `irrigation_scheduler.py`, `scheduler/jobs.py`, `database.py` (CQ-012).
- `setup_logging()` вызывается в `app.py` **до** любых других импортов, которые могут логировать.
- Каждый модуль: `logger = logging.getLogger(__name__)` — иерархия `services.zone_control` → `services` → root.
- Добавить `logger = logging.getLogger(__name__)` в `routes/settings.py` и `services/locks.py` (CQ-001, CQ-002) — критичные NameError-бомбы.

### 6.2 Structured JSON log fields

```json
{
  "ts": "2026-04-19T14:23:05.123Z",
  "level": "INFO",
  "logger": "application.zone_service",
  "msg": "zone_start",
  "zone_id": 42,
  "group_id": 3,
  "command_id": "01HX9K7T3Z4QW2-42",
  "correlation_id": "req-abc123",
  "source": "api",
  "duration_sec": 600,
  "pid": 2989014,
  "thread": "scheduler-worker-3"
}
```

Поля из §6.1 SRE-findings (обязательный event set): `zone_start`, `zone_stop`, `zone_force_stop`, `observed_state_fault`, `mqtt_disconnect`, `mqtt_publish_failed`, `scheduler_lag`, `db_lock_busy`, `boot_sync_complete`, `power_loss_recovery`.

### 6.3 Metrics — prometheus-client

Embedded, **~200 KB** библиотека. Endpoint `/metrics` без auth (internal), pull через Telegraf/Zabbix (10.10.61.96).

```python
# observability/metrics.py
from prometheus_client import Counter, Gauge, Histogram, CollectorRegistry

registry = CollectorRegistry()

wb_zone_start_total = Counter('wb_zone_start_total',
    'Zone start events', ['zone_id', 'source'], registry=registry)
wb_zone_fault_total = Counter('wb_zone_fault_total',
    'Zone fault events', ['zone_id'], registry=registry)
wb_mqtt_publish_total = Counter('wb_mqtt_publish_total',
    'MQTT publish attempts', ['broker', 'result'], registry=registry)
wb_zones_active = Gauge('wb_zones_active',
    'Currently active zones', ['group_id'], registry=registry)
wb_mqtt_clients_connected = Gauge('wb_mqtt_clients_connected',
    'Connected MQTT clients', ['broker'], registry=registry)
wb_zone_duration_seconds = Histogram('wb_zone_duration_seconds',
    'Zone watering duration', ['zone_id'],
    buckets=(60, 300, 600, 1200, 1800, 3600, 7200, 14400),
    registry=registry)
wb_observed_ack_latency_ms = Histogram('wb_observed_ack_latency_ms',
    'Time from command to observed ack', ['zone_id'],
    buckets=(50, 100, 250, 500, 1000, 2500, 5000),
    registry=registry)
wb_scheduler_lag_seconds = Histogram('wb_scheduler_lag_seconds',
    'Scheduler job lag', ['job_type'],
    buckets=(1, 5, 15, 30, 60, 120, 300),
    registry=registry)
wb_http_request_duration_seconds = Histogram(
    'wb_http_request_duration_seconds',
    'HTTP request duration',
    ['route', 'method', 'status'], registry=registry)
```

Полный список — см. SRE §6.2 (я их принимаю как-есть).

**Endpoint:**
```python
@app.route('/metrics')
def metrics():
    if not _is_internal_ip(request.remote_addr):
        abort(403)
    return Response(generate_latest(registry), mimetype=CONTENT_TYPE_LATEST)
```

### 6.4 Health endpoints

| Endpoint | Check | Response |
|---|---|---|
| `/healthz` (liveness) | process alive, main thread responsive | 200 always (если Flask отвечает) |
| `/readyz` (readiness) | MQTT ≥1 connected, DB query OK, scheduler running, boot_reconcile_complete | 200 если все; 503 иначе |
| `/health` (legacy) | keep as-is для обратной совместимости | 200/503 |
| `/metrics` | prometheus | text/plain |

```python
@app.route('/readyz')
def readyz():
    checks = {
        'db': _check_db(),
        'mqtt': _check_mqtt_clients(),
        'scheduler': _check_scheduler_running(),
        'boot_reconcile': _boot_reconcile_done.is_set(),
    }
    status = 200 if all(checks.values()) else 503
    return jsonify({'ready': status == 200, 'checks': checks}), status
```

### 6.5 Correlation ID

Flask middleware:

```python
@app.before_request
def inject_correlation_id():
    cid = request.headers.get('X-Request-ID') or f'req-{uuid.uuid4().hex[:12]}'
    g.correlation_id = cid
    # Для log-filter
    _log_context.correlation_id = cid

@app.after_request
def echo_correlation_id(resp):
    resp.headers['X-Request-ID'] = g.correlation_id
    return resp
```

Прокидывается:
- в `logger.*` через `CorrelationIdFilter` (добавляет в `extra`).
- в MQTT command payload → `correlation_id`.
- при scheduler-job trigger — новый correlation_id, но с prefix `sched-`.

### 6.6 Log rotation

```bash
# /etc/logrotate.d/wb-irrigation (для не-Python логов)
/mnt/data/irrigation-logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0644 root root
    sharedscripts
}

# telegram.txt (520KB, без ротации — HIGH SRE)
/opt/wb-irrigation/services/logs/telegram.txt {
    weekly
    rotate 4
    size 1M
    compress
    missingok
    copytruncate
}
```

### 6.7 Нет trace (OTLP/Jaeger)

На ARM overhead трейсинга (span creation, batch export, 1-2% CPU) не оправдан для 24 зон. Correlation_id в логах + request_id header дают 80% пользы за 0% цены.

### 6.8 systemd watchdog

```ini
[Service]
WatchdogSec=60
NotifyAccess=main
Type=notify
```

В приложении: background thread шлёт `sd_notify('WATCHDOG=1')` каждые 30 сек (только если reconcile-loop жив — иначе процесс возможно "жив но мёртв"). Закроет случай "HTTP отвечает но scheduler thread умер".

---

## 7. Configuration & Secrets

### 7.1 Источники конфигурации (target)

| Слой | Источник | Пример | Mutable runtime? |
|---|---|---|---|
| **Static defaults** | `config.py` | `ZONE_CAP_DEFAULT_MIN = 240` | нет |
| **Environment** | `/etc/wb-irrigation/env` (systemd `EnvironmentFile=`) | `WB_LOG_LEVEL=INFO` | только через restart |
| **Secrets** | `/etc/wb-irrigation/secrets.env` (chmod 600, root) | `MQTT_PASSWORD=...`, `TG_BOT_TOKEN=...` | только через restart |
| **Runtime settings** | `db.settings` | `logging_debug=true`, `weather.enabled=true` | да, через UI |
| **Generated** | `/var/lib/wb-irrigation/secret_key` | Flask `SECRET_KEY` | персистентно, генерится один раз |

**Правило:** static config не дублируется в `db.settings`. Если значение меняется в UI — оно в БД. Если нет — в env/config.py. Убирает текущее дублирование 5-ти источников (current-state.md §3).

### 7.2 systemd unit (target)

```ini
[Unit]
Description=wb-irrigation
After=network.target mosquitto.service
Requires=mosquitto.service

[Service]
Type=notify                                 # для WatchdogSec
User=wb-irrigation                          # убрать root (сейчас running as root)
Group=wb-irrigation
WorkingDirectory=/opt/wb-irrigation
EnvironmentFile=/etc/wb-irrigation/env
EnvironmentFile=/etc/wb-irrigation/secrets.env
ExecStart=/opt/wb-irrigation/venv/bin/python run.py
Restart=always                              # было on-failure — добавить always
RestartSec=5
TimeoutStopSec=45                           # было 20 (HIGH-R6: не хватало)
WatchdogSec=60
LimitNOFILE=4096
MemoryMax=512M                              # hard cap от leak
CPUQuota=200%                               # max 2 ядра
ProtectSystem=strict
ReadWritePaths=/opt/wb-irrigation /mnt/data/irrigation-logs /mnt/sdcard/backup /var/lib/wb-irrigation
PrivateTmp=true
NoNewPrivileges=true
StartLimitBurst=5
StartLimitIntervalSec=300

[Install]
WantedBy=multi-user.target
```

### 7.3 Secrets — storage

Решение: **`EnvironmentFile=` + chmod 600**. Не Docker secrets, не Vaultwarden fetch (overkill для домашнего сетапа).

```bash
# /etc/wb-irrigation/secrets.env
MQTT_PASSWORD_APP=xxxxx
MQTT_PASSWORD_TG=xxxxx
TG_BOT_TOKEN=xxxxx
SESSION_COOKIE_SECRET=xxxxx
DB_ENCRYPTION_KEY=xxxxx
```

`chmod 600`, owner `root:wb-irrigation`. systemd читает и передаёт только в процесс — не видно в `systemctl show` (если использовать `LoadCredential=`). Для максимума:

```ini
LoadCredential=mqtt-password:/etc/wb-irrigation/mqtt-password
```

Доступ через `$CREDENTIALS_DIRECTORY/mqtt-password`. Минус — код читает файл, не env. Баланс: использовать `EnvironmentFile` для стандартных, `LoadCredential` для наиболее чувствительных (MQTT password, encryption key).

### 7.4 `SECRET_KEY` persistence

Текущий `.irrig_secret_key` файл — ок, но **перенести** в `/var/lib/wb-irrigation/secret_key` (правильное FHS место), chmod 600. Генерация — на первом старте:

```python
def get_or_create_secret_key():
    path = Path('/var/lib/wb-irrigation/secret_key')
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_bytes(key)
    path.chmod(0o600)
    return key
```

### 7.5 Runtime settings (через UI)

Остаются в `db.settings`, но с чёткой типизацией:

```sql
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    type TEXT NOT NULL,           -- 'bool','int','float','str','json'
    category TEXT,                -- 'weather','logging','zones',...
    description TEXT,
    updated_at TIMESTAMP,
    updated_by TEXT
);
```

Категории для UI-секционирования. `category='hardcoded'` — нельзя изменять через UI (read-only, mirror of config.py).

---

## 8. Deploy Pipeline (refactor/v2)

### 8.1 Ветка и branching

- **Main branch для прода:** `refactor/v2` (текущий прод).
- `main` — легаси, frozen или удалить.
- Feature-ветки: `feat/*`, `fix/*`, мерджатся PR в `refactor/v2`.
- Тэги релизов: `v2.0.0`, `v2.0.1` — semver, применяются к HEAD `refactor/v2`.

### 8.2 CI на GitHub Actions

`.github/workflows/ci.yml`:

```yaml
name: CI
on:
  push:
    branches: [refactor/v2, main]
  pull_request:
    branches: [refactor/v2]

jobs:
  lint:
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.9' }   # match prod
      - run: pip install ruff mypy
      - run: ruff check .
      - run: mypy domain/ application/     # постепенное покрытие

  test:
    runs-on: ubuntu-22.04
    strategy:
      matrix:
        python-version: ['3.9', '3.11']
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: ${{ matrix.python-version }} }
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: pytest tests/unit tests/db tests/api --cov --cov-fail-under=60
      - uses: codecov/codecov-action@v4

  integration:
    runs-on: ubuntu-22.04
    services:
      mosquitto:
        image: eclipse-mosquitto:2
        ports: ['1883:1883']
    steps:
      - uses: actions/checkout@v4
      - run: pytest tests/integration

  build-arm:
    runs-on: ubuntu-22.04
    needs: [lint, test]
    if: github.ref == 'refs/heads/refactor/v2'
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-qemu-action@v3
      - uses: docker/setup-buildx-action@v3
      - run: docker buildx build --platform linux/arm64 --load .
      # Optional: push в GHCR как образ
```

Триггер для `refactor/v2` — **критичный фикс** (см. tests.md §1 — сейчас CI mapped на `main` only).

### 8.3 Deploy: pull-based

Pull более простой и безопасный для embedded:

```bash
# /opt/wb-irrigation/scripts/update_server.sh (обновлённый)
#!/bin/bash
set -euo pipefail

cd /opt/wb-irrigation

# Save current ref for rollback
git rev-parse HEAD > .last-good-ref

git fetch origin
git checkout refactor/v2
git pull --ff-only origin refactor/v2

# Install deps only if changed
if ! git diff --name-only HEAD@{1} HEAD | grep -q requirements; then
    echo "Dependencies unchanged"
else
    venv/bin/pip install -r requirements.txt
fi

# Migrations (idempotent, но логируем)
venv/bin/python -c "from db.migrations import MigrationRunner; MigrationRunner('/opt/wb-irrigation/irrigation.db').init_database()"

# Restart
systemctl restart wb-irrigation

# Wait for readiness
for i in {1..30}; do
    if curl -sf http://localhost:8080/readyz > /dev/null; then
        echo "READY"
        exit 0
    fi
    sleep 2
done

# Rollback
echo "READINESS TIMEOUT — ROLLBACK"
git checkout "$(cat .last-good-ref)"
systemctl restart wb-irrigation
exit 1
```

**Триггер:**
- Manual: `ssh wb 'sudo /opt/wb-irrigation/scripts/update_server.sh'`.
- Semi-auto: cron раз в день проверяет git `refactor/v2` tag `deploy` → если есть — pull и deploy.
- Не push-based runner: GitHub Actions self-hosted runner на WB = +100 MB RAM + сетевой attack surface. Не окупается.

### 8.4 Rollback

- `git checkout <previous_ref> && systemctl restart`.
- Миграции имеют `DOWNGRADE_REGISTRY` частично. Для релизов с миграциями — тэгировать `v2.0.1-migration` и не rollback автоматически (manual decision).
- Backup БД перед `update_server.sh` — встроенный (`sqlite3 .backup` до git pull). Если rollback нужен — restore БД из backup и git checkout.

### 8.5 Blue-green: не делаем

Embedded single-host, нет ресурсов на второй экземпляр. Downtime рестарта 5-15 сек — приемлемо (см. SRE SLO: 99.5%).

---

## 9. API Contract

### 9.1 Версионирование

**Текущие 18 routes без версии** → переход на `/api/v1/*`.

```
/api/v1/zones                   GET  — список
/api/v1/zones/{id}              GET  — деталь
/api/v1/zones/{id}/start        POST — запуск
/api/v1/zones/{id}/stop         POST — остановка
/api/v1/zones/{id}/state        GET  — текущее состояние
/api/v1/programs                GET
/api/v1/programs/{id}/run       POST
/api/v1/groups                  GET
/api/v1/weather/current         GET
/api/v1/system/health           GET
/api/v1/logs                    GET  — с пагинацией
```

Legacy `/api/*` (без версии) — 301 redirect на `/api/v1/*` на 6 месяцев, потом removal.

### 9.2 OpenAPI spec

`openapi.yaml` в репозитории, автогенерация через `flask-smorest` или ручная. Источник правды для:
- Telegram bot client
- web UI (fetch types)
- external integrators (Home Assistant, Zabbix)

Генерация Python клиента: `openapi-python-client generate --path openapi.yaml`.

### 9.3 Error format — RFC 7807 (problem+json)

```json
{
  "type": "https://wb-irrigation/errors/zone-busy",
  "title": "Zone is already running",
  "status": 409,
  "detail": "Zone 42 is currently running (started at 14:20). Stop it first.",
  "instance": "/api/v1/zones/42/start",
  "correlation_id": "req-abc123",
  "zone_id": 42,
  "current_state": "on"
}
```

Единый Flask error handler:

```python
@app.errorhandler(ApiError)
def handle_api_error(e):
    return jsonify({
        'type': e.type_uri,
        'title': e.title,
        'status': e.status,
        'detail': e.detail,
        'instance': request.path,
        'correlation_id': g.correlation_id,
        **e.extra,
    }), e.status, {'Content-Type': 'application/problem+json'}
```

### 9.4 Authentication — Hybrid

| Client | Auth | Rationale |
|---|---|---|
| Web UI (браузер) | Flask session cookie (stateful) + CSRF | UX; нет смысла в JWT для stateful app |
| Telegram bot (internal) | not HTTP — через MQTT | не HTTP API |
| API integrations (Zabbix, IoT) | API key header `X-API-Key` | простой, ревокация через `db.api_keys` |
| Internal (metrics, health) | IP allow-list (127.0.0.1, LAN) | нет секретов в метриках |

```python
@app.route('/api/v1/zones/<int:id>/start', methods=['POST'])
@require_auth(any_of=['session', 'api_key'])
@require_role(['admin', 'user'])
def start_zone_v1(id):
    ...
```

### 9.5 Pagination

`/api/v1/logs`:
```
GET /api/v1/logs?limit=100&cursor=01HX9K7T3Z
```
Cursor-based (не offset), cursor = `last_id` или `last_ts_encoded`. Response:
```json
{
  "items": [...],
  "next_cursor": "01HX9K7T4X",
  "has_more": true
}
```

### 9.6 CSRF

- Web forms (POST/PUT/DELETE from session) → CSRF token required.
- API-key authenticated → CSRF exempt (API key само по себе auth).
- Session + AJAX → CSRF token в header `X-CSRFToken` (уже есть в app.js).

Убирает current-state §9.3 — "app.js шлёт X-CSRFToken но API-blueprint'ы CSRF-exempt" — делаем exempt только для api-key paths.

---

## 10. Telegram Integration

### 10.1 Webhook vs polling

**Решение: polling (long-poll)**, не webhook.

Причины:
- Cloudflare Tunnel → `poliv-kg.ops-lab.dev` публичен, но TG webhook требует TLS + publicly reachable — CF tunnel это даёт, но добавляет одну точку отказа.
- Polling в aiogram — 15 KB RSS, 1 connection, нет внешнего trigger.
- Нет latency requirement — polling 10 сек ok для домашнего бота.
- **Минус webhook:** если CF tunnel упадёт — сообщения теряются (TG retry 24 часа, но в середине полива неудобно).

### 10.2 Token storage

Сейчас — SEC-010 (hardcoded/exposed). Цель:
- `/etc/wb-irrigation/secrets.env` → `TG_BOT_TOKEN=...` (chmod 600).
- **НЕ** в `db.settings` (сейчас там — доступно любому с admin-доступом к БД).
- Rotation: через `@BotFather` → обновить файл → `systemctl restart`.

### 10.3 Decoupling

Текущая схема: aiogram thread inside Flask process. Целевая:

**Вариант A (рекомендую): тот же процесс, но через MQTT contract**

Бот — адаптер:
- Подписан на `wb-irrigation/zone/+/state`, `wb-irrigation/program/+/state`, `wb-irrigation/system/events` — отображает пользователю.
- Публикует команды в `wb-irrigation/zone/+/command` — не вызывает `application.zone_service` напрямую.

Это даёт: бот **изолирован как клиент**. Вынести в отдельный процесс можно в любой момент (systemd unit `wb-irrigation-bot.service`). Сейчас — тот же процесс, но через чистый контракт.

**Вариант B (для будущего):** отдельный systemd unit. Доступ к БД только read (viewer role) для "статус" команд. Write только через MQTT command. Окупится если бот станет активнее.

### 10.4 Authorization

```sql
CREATE TABLE bot_users (
    chat_id INTEGER PRIMARY KEY,
    username TEXT,
    role TEXT NOT NULL CHECK(role IN ('admin','user','guest')),
    created_at TIMESTAMP,
    last_active TIMESTAMP,
    is_active BOOLEAN DEFAULT 1
);
```

Первый `/start` в боте → если `bot_users` пуст — первый user становится admin. Дальше — admin инвайтит: `/invite @user` → pending → `/approve <chat_id>`.

Команды бота делятся на:
- `admin` — `/stopall`, `/rain`, `/program`
- `user` — `/status`, `/zone`, `/start`, `/stop` (конкретной зоны)
- `guest` — только `/status`, read-only

### 10.5 Idempotency

`bot_idempotency` таблица уже есть — используем для предотвращения дублей команд (TG может ретраить callback_query).

---

## 11. SSE Decision

**Рекомендация: Вариант А — выпилить SSE полностью.**

### 11.1 Обоснование

**Вариант А (предпочтительно):** выпилить SSE, оставить polling 5s.

Pros:
- Устраняет мёртвый код (SSE-01, SSE-02, SSE-03, SSE-04 в performance.md).
- Устраняет бесконечный reconnect в браузере на 204 (frontend.md §3.1).
- Убирает запись в SQLite на каждое MQTT-сообщение в `sse_hub._on_message` (SSE-03).
- Polling /api/status с If-Modified-Since / ETag даёт 80% real-time без event loop complexity.
- Mobile battery: polling раз в 5 сек с `document.hidden` pause — дешевле, чем долгие connection'ы.

Cons:
- Задержка обновления 0-5 сек (сейчас при работающем SSE было <1 сек).
- Для домашнего UX — приемлемо.

**Вариант Б (отклоняю):** вернуть SSE с heartbeat + bounded queue + disconnect detection. Уже пробовали, event loop на ARM/Hypercorn умирал. Добавлять обходы (keepalive ping каждые 15 сек, `:retry:` header, bounded queue) — хрупко.

**Вариант В (отклоняю):** WebSocket. Двусторонний не нужен (команды идут через REST API, не через WS). ARM overhead такой же как SSE, плюс сложнее debug. Если нужен push — MQTT уже есть для контроллера, можно пробросить для UI (WASM MQTT client в браузере) — но для домашнего UI это чрезмерно.

### 11.2 План выпила

1. Удалить `static/js/zones.js:1468-1488` (EventSource).
2. Удалить `services/sse_hub.py` (363 LOC).
3. Удалить `/api/mqtt/zones-sse` endpoint (уже 204, но сам route).
4. В `status.js` убрать комментарии про SSE.
5. Улучшить polling: `ETag` на `/api/status`, `document.hidden → slow polling 30 сек`, кнопка "обновить сейчас".

### 11.3 Если real-time критичен

**Когда пересмотреть:** если пользователь жалуется на задержку UI при manual start/stop → добавить **MQTT.js в браузере** с подпиской на `wb-irrigation/zone/+/state`. Контракт уже есть (§2), браузер MQTT over WSS через `18883` (WS) уже слушает mosquitto. Это дороже в сетапе (creds для браузера, CF tunnel на 18883), но архитектурно чистее SSE.

---

## 12. Auth / AuthZ

### 12.1 Роли (остаются)

| Role | Права |
|---|---|
| `admin` | всё: mqtt config, users, settings, CRUD programs/zones, manual control |
| `user` | CRUD programs, manual control, просмотр settings (но не mqtt creds) |
| `guest` | read-only: статус, логи (без sensitive), manual start/stop разрешённых зон |

Сохранить текущую модель.

### 12.2 Session

- Server-side session (Flask default + `itsdangerous`).
- **НЕ** JWT. Приложение stateful, нет смысла в stateless token.
- Cookie: `HttpOnly`, `Secure` (только HTTPS), `SameSite=Lax`.
- TTL: 7 дней idle, 30 дней max.
- Storage: filesystem (`SESSION_FILE_DIR=/var/lib/wb-irrigation/sessions`).

### 12.3 Password hashing

Текущее: `werkzeug.generate_password_hash` (default scrypt с 2022, pbkdf2 до того).

Цель: **argon2id** (через `argon2-cffi` ~300 KB на ARM). Миграция:
- На login: если хэш начинается с `pbkdf2:` или `scrypt:` — проверить старым методом, при успехе rehash в argon2id.
- Через 6 месяцев — удалить support старого формата.

Parameters (ARM-friendly): `memory_cost=32768` (32 MB), `time_cost=2`, `parallelism=1`. Login займёт ~200 мс на WB — приемлемо.

### 12.4 MFA

**Рекомендация: НЕ вводить** для домашнего сервиса.

Обоснование:
- Пользователей 1-3 максимум.
- Доступ из LAN или через CF Access (см. §13) — внешний MFA layer.
- TOTP в приложении добавит 300 LOC + QR + backup codes + хранилище — overhead.
- Если хочется защиту — **выносим за периметр**: CF Access с GitHub/Google OAuth (§13).

### 12.5 Rate limiting

Новое: `flask-limiter` (~50 KB):
- `/api/v1/auth/login` — 5 попыток / мин / IP.
- `/api/v1/zones/*/start|stop` — 30 / мин / user (предотвращает dashboard-storm).
- `/api/v1/logs` — 60 / мин / user.

Storage: memory (single-process). Для WB хватит.

---

## 13. Trust Boundaries (Cloudflare)

### 13.1 Текущая проблема (SEC-002)

Cloudflare tunnel → `poliv-kg.ops-lab.dev` → **напрямую на Flask `:8080`**, минуя `basic_auth_proxy` на `:8011`. Security boundary нарушена: Flask login — единственная защита с прилётом из интернета.

### 13.2 Рекомендация: Cloudflare Access Application

Включить CF Access на `poliv-kg.ops-lab.dev`:

1. **Identity provider:** Google или GitHub OAuth.
2. **Access policy:** allow только емейлы из списка (владелец + родственник).
3. **Session:** 24 часа.
4. **Application type:** Self-hosted.
5. Flask получает `Cf-Access-Authenticated-User-Email` header → использует как identity (обходит login, SSO).

Это даёт **два слоя auth**:
- CF Access (внешний, MFA от Google/GitHub provider).
- Flask session (внутренний, для role-based authZ).

Bonus: CF Access логирует все попытки, блокирует bruteforce.

### 13.3 Альтернативы (не рекомендую как основное)

| Вариант | Pros | Cons |
|---|---|---|
| **basic_auth_proxy перед Flask** | простой, не зависит от CF | ещё один процесс, пароли в plain, нет MFA |
| **IP-restrict (только LAN + VPN)** | нулевой attack surface извне | требует VPN setup, нет доступа с мобилки без VPN |
| **CF Zero Trust Access (выбрано)** | MFA external, audit log, zero code change | зависит от CF availability (99.99% в prod) |

### 13.4 Network boundaries

```
          Internet
             │
             ▼
     ┌───────────────┐
     │ Cloudflare    │
     │  - WAF        │
     │  - Access     │ ← OAuth MFA
     │  - Tunnel     │
     └───────┬───────┘
             │ encrypted tunnel
             ▼
  ┌──────────────────────┐
  │ WB Device            │
  │                      │
  │  cloudflared ───┐    │
  │                 ▼    │
  │  ┌─────────────────┐ │
  │  │ nginx :8080     │ │← adds X-Forwarded-For,
  │  │                 │ │  verifies CF-Access-JWT
  │  └──────┬──────────┘ │
  │         │            │
  │  ┌──────▼──────────┐ │
  │  │ Hypercorn Flask │ │← reads CF-Access email
  │  │   :8080 (lo)    │ │  → session auth
  │  └──────┬──────────┘ │
  │         │            │
  │  ┌──────▼──────────┐ │
  │  │ mosquitto :1883 │ │← ACL per-user
  │  │   (lo + LAN)    │ │
  │  └─────────────────┘ │
  └──────────────────────┘
```

**LAN доступ:** Flask слушает `127.0.0.1:8080` (не 0.0.0.0). nginx слушает `0.0.0.0:443` с TLS (для LAN) и `127.0.0.1:8080` (для CF tunnel). LAN пользователи ходят на `https://wb.local` с self-signed certificate или local CA.

### 13.5 MQTT boundary

- `mosquitto :1883` — только `127.0.0.1` + LAN (WB-rules локально).
- `mosquitto :18883` (WS) — только `127.0.0.1` (если SSE / browser MQTT когда-либо).
- Не открывать 1883 через CF tunnel.
- ACL per-user (см. §2.4).

---

## 14. Migration Path

Пошаговый план без big-bang. Каждый шаг — отдельный PR, <500 LOC diff, deployable независимо.

### Phase 0 — Срочные фиксы (1 спринт, critical bugs)

| Шаг | Что | Finding | Effort |
|---|---|---|---|
| 0.1 | Добавить `import logging; logger = ...` в `routes/settings.py`, `services/locks.py`, `services/mqtt_pub.py`, `services/telegram_bot.py` | CQ-001..004 | 1 час |
| 0.2 | Починить logging: handler на root, удалить `basicConfig` из scheduler/jobs/database, вызвать `apply_runtime_log_level` | Bug #4, CRIT-O1 | 4 часа |
| 0.3 | Добавить `logger = logging.getLogger(__name__)` в `BaseRepository._connect()` + использовать везде | DB-007 | 4 часа |
| 0.4 | `PRAGMA foreign_keys=ON; synchronous=NORMAL; busy_timeout=30000` в `_connect()` | DB-001, DB-004 | 1 час |
| 0.5 | `requirements.txt` += `SQLAlchemy`; APScheduler на `SQLAlchemyJobStore` | HIGH-R3 | 2 часа |
| 0.6 | CI `.github/workflows/ci.yml` — trigger на `refactor/v2` | tests §1 | 30 мин |

**Результат:** приложение логирует в файл, миграции ран атомарно, scheduler jobs восстанавливаются.

### Phase 1 — MQTT Contract v1 (2-3 спринта)

| Шаг | Что | Finding | Effort |
|---|---|---|---|
| 1.1 | Создать `infrastructure/mqtt/client_pool.py` — single paho client per broker | perf §3 | 1-2 дня |
| 1.2 | Убрать per-request `mqtt.Client().connect()` в `routes/system_status_api.py` — через pool | perf §3 | полдня |
| 1.3 | Добавить `command_log` таблицу + idempotency check | §2.5 | 1 день |
| 1.4 | Миграция топиков: new namespace `wb-irrigation/zone/{id}/...` — **параллельно** со старыми (dual publish) | — | 3 дня |
| 1.5 | WB rule engine скрипт → слушать новые command topics, писать observed | — | 1 день (конфиг WB) |
| 1.6 | Убрать старые топики после 2 недель overlap | — | полдня |

### Phase 2 — State Machine + Reconciler (2 спринта)

| Шаг | Что | Finding | Effort |
|---|---|---|---|
| 2.1 | Добавить колонки: `desired_state`, `desired_version`, `commanded_state`, `observed_state`, `observed_at`, `last_command_id` | §3.1 | 1 день (миграция + тесты) |
| 2.2 | Subscriber на `zone/+/observed` → update БД | §3 | 1 день |
| 2.3 | `application/reconciler.py` + APScheduler 10-sec job | §3.6 | 2-3 дня |
| 2.4 | `boot_reconcile` с retained snapshot | §3.5, CRIT-RC1 | 2 дня |
| 2.5 | Переписать `start_zone`/`stop_zone` на версионированные UPDATE | CRIT-R3, CRIT-CC1 | 2 дня |
| 2.6 | Удалить `services/observed_state.py` `verify_async` fire-and-forget | HIGH-R2 | полдня |
| 2.7 | Удалить `_delayed_close` thread → date-job | CQ-007 | полдня |

**Результат:** hardware и БД больше не расходятся.

### Phase 3 — Observability (1 спринт)

| Шаг | Что | Finding | Effort |
|---|---|---|---|
| 3.1 | `prometheus-client` + `/metrics` endpoint | SRE §6.2 | 1 день |
| 3.2 | `/healthz` + `/readyz` | SRE §6.3 | полдня |
| 3.3 | Correlation ID middleware + `extra={}` в logger calls | §6.5 | 1 день |
| 3.4 | systemd `WatchdogSec` + `sd_notify` heartbeat | SRE §6.4 | полдня |
| 3.5 | `zone_state_transitions` таблица — audit trail | §3.7 | 1 день |
| 3.6 | Backup cron-job APScheduler 04:30 | DB-005 | полдня |
| 3.7 | Log rotation для `telegram.txt` (logrotate) | SRE | 1 час |
| 3.8 | Zabbix alerting (WB уже в 10.10.61.96) | SRE §6.6 | 1 день |

### Phase 4 — Domain Layer Extraction (2-3 спринта)

| Шаг | Что | Effort |
|---|---|---|
| 4.1 | Создать `domain/zone/state_machine.py` — pure state transitions | 2 дня |
| 4.2 | Создать `domain/program/expansion.py` — вынести из `db/zones.py:compute_next_run_for_zone` | 2 дня |
| 4.3 | Создать `domain/weather/decision.py` — pure threshold logic из `services/weather.py` | 2-3 дня |
| 4.4 | Создать `db/weather.py` repository; переключить `services/weather.py` (убрать 7 прямых connect) | 2 дня |
| 4.5 | Создать `db/float.py` repository; переключить `services/float_monitor.py` | 1 день |
| 4.6 | Разбить `irrigation_scheduler.py` на `infrastructure/scheduler/{runner,jobs}.py` | 3-4 дня |
| 4.7 | Удалить `scheduler/jobs.py` дубликат (CQ-005) | 1 день |
| 4.8 | Удалить `services/scheduler_service.py` dead stub | 15 мин |

### Phase 5 — API v1 + SSE removal (1 спринт)

| Шаг | Что | Effort |
|---|---|---|
| 5.1 | Переместить все routes в `routes/api/v1/` | 1 день |
| 5.2 | Legacy `/api/*` → 301 redirect на `/api/v1/*` | полдня |
| 5.3 | RFC 7807 error format | 1 день |
| 5.4 | OpenAPI spec + генерация | 2 дня |
| 5.5 | Удалить SSE (sse_hub, routes, JS) | 1 день |
| 5.6 | Polling улучшения: ETag, `document.hidden` | 1 день |

### Phase 6 — FK + Performance (1 спринт)

| Шаг | Что | Effort |
|---|---|---|
| 6.1 | FK декларации с пересозданием таблиц (миграция `add_fks_v2`) | 2-3 дня |
| 6.2 | Composite индексы (DB-009) | полдня |
| 6.3 | Prefetch в `compute_next_run_for_zone` (DB-012) | полдня |
| 6.4 | Retention job `logs` > 180 дней | полдня |

### Phase 7 — Security hardening (1 спринт)

| Шаг | Что | Effort |
|---|---|---|
| 7.1 | systemd `User=wb-irrigation` (создать user, chown файлы) | 1 день |
| 7.2 | Secrets в `/etc/wb-irrigation/secrets.env` chmod 600 | полдня |
| 7.3 | CF Access включить на `poliv-kg.ops-lab.dev` | 2 часа |
| 7.4 | Password hashing → argon2id with migration | 1 день |
| 7.5 | Rate limiting (flask-limiter) | полдня |

### Общий timeline

~**12-15 спринтов** (3-4 месяца) параллельно с регулярной разработкой. Каждая phase самодостаточна и deployable. Phase 0 — максимальный приоритет (критичные баги).

---

## 15. What Requires Decision From Owner

Вопросы, где архитектор не принимает решение сам — нужен Рауль:

### 15.1 Критичные (блокеры для Phase 0-1)

1. **SSE: подтвердить выпил** (§11). Ок ли 5-сек задержку обновления в UI? Если нет — Вариант Б (вернуть с heartbeat) или Вариант В (MQTT in browser).

2. **Secrets storage: LoadCredential vs EnvironmentFile** (§7.3). `LoadCredential` безопаснее (не в `systemctl show`), но требует рефактора — код читает файл `$CREDENTIALS_DIRECTORY/...`. `EnvironmentFile` проще. Выбор?

3. **Cloudflare Access** (§13.2). Включаем? Нужен OAuth identity provider (Google / GitHub). Список allow-email'ов.

4. **CI deploy trigger**: manual (ssh + скрипт) vs semi-auto (cron pull if tag)? (§8.3)

5. **Telegram bot: тот же процесс или отдельный unit?** (§10.3). Вариант A прост, вариант B более изолирован. Для старта — A, вынос позже по необходимости.

### 15.2 Архитектурные (Phase 2+)

6. **MQTT contract rollout**: dual publish 2 недели (параллельно старые и новые топики) или сразу cutover? Dual безопаснее, но удваивает публикации на окно миграции.

7. **MQTT protocol version**: v3.1.1 (совместимо со всеми WB-rules) или v5 (session_expiry, user properties)? Если WB rules v3.1.1 — не упирается, можно оба.

8. **Password rehash**: мигрировать argon2id on-login (lazy) или batch? Lazy проще.

9. **Backup retention**: 30 дней ежедневных на SD (33 GB свободно) достаточно, или нужен offsite (S3/Dropbox)?

10. **Guest role**: полезно или упразднить (упростить до admin/user)? Сейчас не используется на проде (`bot_users=0`).

### 15.3 Процессные

11. **Версионирование API**: `/api/v1` — ok, или сразу `/api/v2`? Если v1 включает breaking changes (command_id required) — стоит v2, оставить v1 legacy.

12. **MFA для CF Access** (если включаем §13.2): Google OAuth one-tap или required 2FA?

13. **Миграция БД: risk tolerance**. FK декларации через recreate-table (§5.4) требуют ~5 сек downtime и full DB rewrite (~1 MB). Выполнить ночью с backup + rollback план?

14. **WB rule engine**: кто пишет правило для публикации `zone/+/observed` (echo из GPIO)? Если вы сами — ок, если WB Techpom — нужно спросить; от этого зависит Phase 1.

15. **Prometheus scrape endpoint**: использовать Telegraf (уже стоит для Zabbix) через `inputs.prometheus` → Zabbix, или поднимать отдельный Prometheus? Telegraf проще.

### 15.4 Opinion / non-blocking

16. Argon2id parameters: предложенные `memory=32MB, time=2` дадут ~200 мс login latency на ARM. Ok или нужен более быстрый (`memory=16MB, time=1` ~100 мс)?

17. Runtime settings таблица: дополнить `category` + `type` (§7.5) — миграция всех существующих 22 rows вручную (маппинг). OK?

18. SSL/TLS на mosquitto для WS (18883) — нужно ли? Сейчас нет, но если браузер будет MQTT-clientом (§11.3 future) — понадобится cert на `poliv-kg.ops-lab.dev`.

---

## Summary

### Топ-5 архитектурных изменений (повтор из Executive Summary)

1. **Command → State → Observation state machine** с reconciler loop — уничтожает расхождение БД↔hardware (CRIT-R1, R3, RC1, CC1).
2. **MQTT contract v1** — taxonomy `wb-irrigation/zone/{id}/(command|desired|state|observed)`, QoS matrix, command_id идемпотентность, ACL per-identity.
3. **SQLite остаётся, PostgreSQL не нужен.** `BaseRepository._connect()` с `PRAGMA foreign_keys=ON, synchronous=NORMAL, busy_timeout=30s`. APScheduler на `SQLAlchemyJobStore` для персистентности jobs.
4. **Domain layer** (pure, zero I/O) вынесен из god-modules `weather.py` (1404 LOC) и `irrigation_scheduler.py` (1365 LOC). `services/` остаётся, но как `application/` + `infrastructure/`.
5. **Observability minimum**: `/healthz` + `/readyz` + `/metrics` (prometheus-client), root-logger file handler (fix Bug #4), correlation_id, systemd `WatchdogSec`, ежедневный backup.

### Файл

`/opt/claude-agents/irrigation-v2/irrigation-audit/architecture/target-state.md`

### 18 вопросов для владельца

См. §15. Блокирующие для Phase 0-1: #1 SSE, #2 Secrets storage, #3 CF Access, #4 Deploy trigger, #5 Telegram process. Остальные — для Phase 2+, могут быть решены итеративно.

### Приоритизация

- **Phase 0 (1 спринт)**: CQ-001..004, Bug #4 fix, PRAGMA, CI на refactor/v2 — **критичные баги продакшена**.
- **Phase 1 + 2 (4-5 спринтов)**: MQTT contract + state machine + reconciler — **ядро надёжности**.
- **Phase 3-7 (7-9 спринтов)**: observability, domain extraction, API v1, FK, security — **архитектурная зрелость**.

Ни один шаг не требует big-bang rewrite. Прод не останавливается.

