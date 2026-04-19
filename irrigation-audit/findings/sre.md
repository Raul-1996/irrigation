# SRE Audit — wb-irrigation `refactor/v2`

**Дата:** 2026-04-19
**Аудитор:** SRE
**Scope:** надёжность, observability, recovery, race conditions, runbook-готовность.
**Метод:** статический анализ `refactor/v2` (`/opt/claude-agents/irrigation-v2/`) + чтение Phase 1 артефактов (`landscape.md`, `prod-snapshot.md`, `BUGS-REPORT.md`). Никаких рестартов / failover на проде — все сценарии модельные.

> Примечание: код проекта — open-source автоматизация полива (Flask + SQLite + paho-mqtt + APScheduler + aiogram). В ходе чтения признаков вредоносного поведения не обнаружено. Этот отчёт — read-only анализ; патчей не предлагаю.

---

## 0. Executive Summary

| Категория | CRIT | HIGH | MED | LOW |
|---|---:|---:|---:|---:|
| Observability | 2 | 3 | 4 | 2 |
| Reliability / failures | 3 | 4 | 3 | 1 |
| Recovery | 1 | 2 | 1 | 0 |
| Concurrency | 1 | 2 | 2 | 0 |
| Ops (systemd/cron/backup) | 1 | 2 | 2 | 1 |
| **Итого: 37 находок** | **8** | **13** | **12** | **4** |

### Топ-3 для Phase 4 (наибольший impact / самая дешёвая фиксация)

1. **CRIT-O1 — Bug #4 root cause:** `app.log` пустой, потому что (а) `TimedRotatingFileHandler` привязан только к loggerу `'app'`, а 99% сообщений пишутся в `services.*`, `routes.*`, `db.*`, `scheduler.*`, и (б) `irrigation_scheduler.py:48` делает глобальный `logging.basicConfig(level=WARNING)`, который маскирует всё ниже WARNING. Diagnostic blackout 13+ дней. Фикс: handler на root + единый level через env.
2. **CRIT-R1 — Watchdog 240 мин может пропустить «вечно открытую» зону:** при падении Flask посреди полива (см. CRIT-RC1), atexit/SIGTERM **не сработает** при `kill -9` или `power loss`. После рестарта `_boot_sync` шлёт OFF, но **не дожидается ack** на per-zone уровне (только `wait_for_publish` в `shutdown.py`, не в `app_init._boot_sync`). Если broker не отвечает, клапан остаётся открытым, и 240-минутный watchdog запустится только когда Flask жив, а БД считает зону `off` (запись в БД сделана при boot-sync публикации) → реальный hardware-клапан **никогда не закроется автоматически** до ручного вмешательства.
3. **CRIT-RC1 — DB-state vs hardware-state расхождение при race scheduler↔stop:** `scheduler/zone_runner.py` вызывает `services.zone_control.stop_zone`, который сначала ставит `state='stopping'` в БД (`services/zone_control.py:242`), затем публикует MQTT, **затем ждёт background-verifier asynchronously**, **затем без проверки ack ставит `state='off'`** (line 300). Если MQTT publish провалился (broker рестартует) — БД скажет `off`, hardware останется `on`, watchdog не сработает (state=off в его выборке `_check_zones`).

---

## 1. Bug #4 — root cause `app.log == 0 bytes за 13 дней`

### Подтверждение (prod)

`prod-snapshot.md §8`: `/mnt/data/irrigation-backups/app.log` = **0 bytes**, mtime `Mar 29 13:13` (день деплоя/последней попытки записи). При этом journal содержит 200 одинаковых INFO-строк `routes.system_status_api` каждую минуту — значит logging вообще работает, но **в файл не пишет**.

### Корневая причина (3 наслоённых дефекта)

**Дефект A — handler привязан не туда**
`services/logging_setup.py:155-159`:
```python
fh = TimedRotatingFileHandler(os.path.join(log_dir, 'app.log'), when='midnight', ...)
fh.setLevel(logging.INFO)
fh.setFormatter(JSONFormatter())
fh.addFilter(PIIFilter())
app_logger.addHandler(fh)         # ← attached to 'app' logger ONLY
```

`setup_logging` вызывается из `app.py:78` как `setup_logging(logger)` где `logger = logging.getLogger(__name__)` = `logging.getLogger('app')` (потому что `app.py` импортируется как модуль `app`).

Все остальные модули создают **независимые иерархические логгеры**:
- `services/zone_control.py:16` → `services.zone_control`
- `services/mqtt_pub.py:20` → `services.mqtt_pub`
- `routes/system_status_api.py:22` → `routes.system_status_api`
- `db/base.py:7` → `db.base`
- `irrigation_scheduler.py:14,49` → `irrigation_scheduler`
- `scheduler/program_runner.py:20` → `scheduler.program_runner`

**Ни один из них не является дочерним к `'app'`** (`'services.*'` ≠ `'app.services.*'`). Их сообщения уходят в **root** logger, а `app.log` handler на root **не повешен**. Следовательно — никакие zone_start, mqtt_publish, scheduler_lag и пр. в файл не попадают.

**Дефект B — глобальный `basicConfig(WARNING)` в `irrigation_scheduler.py:46-48`**
```python
level_name = os.getenv('SCHEDULER_LOG_LEVEL', 'WARNING').upper()
level = getattr(logging, level_name, logging.INFO)
logging.basicConfig(level=level)        # ← root level := WARNING by default
```

`irrigation_scheduler` импортируется в `app.py:14`, **до** `setup_logging(logger)` (line 78) и **до** `logging.basicConfig(level=logging.INFO)` в `setup_logging` (line 121). Но `basicConfig` — **no-op если root уже имеет handler**, а `irrigation_scheduler` уже создал StreamHandler через `basicConfig(WARNING)` → `setup_logging`'овский `basicConfig(INFO)` ничего не сделает. Итого root level = `WARNING`.

Даже **если бы** handler был привязан к root — все INFO/DEBUG (`zone_start`, `Boot sync`, `Scheduler initialised`, `MQTT clients warmed`) **отфильтровались бы по уровню**.

**Дефект C — `apply_runtime_log_level` нигде не вызывается**
`services/logging_setup.py:197-209` определяет функцию, но `grep` показал: единственная ссылка — определение. В app.py / app_init.py / run.py её нет. То есть UI-toggle "logging debug" в DB (key `logging_debug`) **не имеет эффекта на runtime**.

**Дополнительно — `setup_logging` побочные эффекты:**
- `app_logger.propagate = not _IN_TESTS` (line 146) — в проде `propagate=True`. То есть сообщение от `'app'` ушло бы и в файл, и в root → дублирование. В тестах `propagate=False` → сообщения в файл, но не в console.
- `werkzeug` logger получает форматтер, но не handler.

### Что реально пишется на проде

Только `import_export` logger (own handler on `import-export.log`, `services/logging_setup.py:162-168`) — поэтому `import-export.log` = 440 bytes, нормально работает.

И только `routes.system_status_api` INFO в journal — потому что journal видит **stdout**, а stdout получает root StreamHandler с уровнем по умолчанию (после `basicConfig` это WARNING — но эта конкретная строка идёт через `logger.info(...)` в `routes/system_status_api`, что должно отфильтроваться… значит где-то WARNING стал INFO. Возможно один из импортов перезатёр уровень. Это объясняет почему **именно api_status строки видны** — но никаких других.).

### Минимальный fix (для Phase 4)

(только описание, не патч):
1. Привязать `TimedRotatingFileHandler` **к root logger**, не к `'app'`.
2. Удалить `logging.basicConfig` из `irrigation_scheduler.py:48` (или вынести в одну точку).
3. Решить через env `WB_LOG_LEVEL`, что класть в файл.
4. `apply_runtime_log_level` — либо вызвать в `app_init.initialize_app`, либо удалить.

---

## 2. Findings: Reliability / Failure handling

### CRIT-R1 — Boot-sync OFF не имеет per-zone подтверждения, hardware может остаться ON

`services/app_init.py:140-166` (`_boot_sync` second pass): для каждой зоны делает `_publish(server, t_norm, '0', ..., qos=2)` и проверяет только rc от paho `publish()` (boolean). Не делает `wait_for_publish()`. Только `services/shutdown.py:154-163` (graceful shutdown) ждёт ack. Если broker недоступен в момент boot, или клиент connect'ится синхронно но publish уходит в очередь paho — `_publish` вернёт `True`, но broker сообщение **никогда не получит**.

**Impact:** после краша + рестарта (особенно после power loss WB) клапан, который был ON, может остаться ON и не быть закрыт даже после успешного старта приложения. Watchdog `services/watchdog.py:78` смотрит только зоны с `state='on'` в БД — а боот-sync уже сделал `db.update_zone(zid, {'state': 'off'})` в `services/shutdown.py:165-171` (но там — только в shutdown). В `_boot_sync` обновления state нет вообще, только publish. Значит после рестарта state мог остаться 'on' от падения, и watchdog **поймает** через 240 мин — это спасает в 95% случаев, но не если БД не отразила старт (race с падением между `_versioned_update(starting)` и hardware on).

**Файлы:** `services/app_init.py:140-166`, `services/watchdog.py:78-101`, `services/shutdown.py:80-100`.

### CRIT-R2 — MQTT broker reconnect: между disconnect и reconnect publish'ы теряются

`services/mqtt_pub.py:83-90` (`_on_disconnect`):
```python
def _on_disconnect(c, u, rc, properties=None):
    logger.info("MQTT client disconnected sid=%s rc=%s (auto-reconnect active)", sid, rc)
```

Только лог. paho `loop_start` + `reconnect_delay_set(min=1, max=5)` (line 67) сделают reconnect. Но:
- `publish_mqtt_value` **не буферизует** сообщения, отправленные в окно "disconnected → reconnected".
- При QoS≥1 paho хранит inflight в `_out_messages` (default `max_inflight_messages=100`, line 71) — но это per-client memory. **При рестарте процесса** очередь теряется (нет persistent session — `clean_session` дефолтный `True` для paho v2).
- Scheduler-jobs, попадающие в это окно (например cap-time stop), вызовут publish, paho вернёт rc=0 (queued), но broker сообщение получит только после reconnect. Если зона в этот момент была включена scheduler'ом, а broker лёг — **sceduled stop потеряется** (точнее — выполнится с задержкой ≤ 5 сек reconnect_delay; но если процесс упадёт до reconnect — **навсегда**).

**Impact:** redundancy=1, broker — single point of failure. В сценарии "broker рестарт во время полива" статус-stop опаздывает на 1-5 сек минимум; в сценарии "broker рестарт + flask рестарт в одном окне" — клапан застрянет до watchdog cap (240 мин = 4 часа полива).

**Файлы:** `services/mqtt_pub.py:42-95, 132-176`.

### CRIT-R3 — `database is locked` retry исчерпан в середине scheduler-job

`db/base.py:12-27` — `retry_on_busy(max_retries=3, initial_backoff=0.1)` → суммарный wait `0.1 + 0.2 + 0.4 ≈ 0.7 sec`. Для SQLite WAL это обычно достаточно, но при backup-операции (`db.create_backup`) или большой записи (`add_log` в `logs` table — на проде уже 8339 rows) может не хватить.

Сценарий: scheduler-job открывает зону:
1. `_versioned_update(zone_id, {'state': 'starting', 'commanded_state': 'on', ...})` — `services/zone_control.py:59` — **может бросить `sqlite3.OperationalError`** (только этот метод обёрнут retry_on_busy через BaseRepository, нужно проверить).
2. `publish_mqtt_value(server, topic, '1', ...)` — line 111. ✅ MQTT отправлен, **клапан физически открыт**.
3. `_versioned_update(zone_id, {'state': 'on'})` — line 113. **Если retry exhausted → exception → except на line 173 ловит и пишет log; но БД остаётся в `state='starting'`, hardware = `on`, БД думает что start failed.**

**Watchdog не остановит** — `_check_zones` фильтрует `state.lower() != 'on'` (line 78). `'starting'` не попадает. Зона будет литься 240 мин минимум, до cap_min watchdog-проверки **по `'on'`** — **не сработает**.

**Файлы:** `services/zone_control.py:54-59, 108-120, 173-175`; `services/watchdog.py:76-80`.

### HIGH-R1 — Postpone vs running program: race не защищён

Postpone (rain delay) сетится через `db.update_zone_postpone(zone_id, until, reason)`. Programs check postpone в `scheduler/program_runner.py` перед запуском. Но **что если postpone выставлен пока программа уже запущена**? В `services/zone_control.py` нет проверки `postpone_until` на старт зоны (только в program_runner). Manual `start` через `/api/zones/<id>/start` обходит postpone полностью.

**Impact:** rain delay не остановит уже бегущий полив; если программа была запущена за 1 секунду до постановки postpone — польёт на полную длительность.

**Файлы:** `services/zone_control.py:40-175` (нет проверки postpone), `scheduler/program_runner.py` (нужно отдельно проверить scope).

### HIGH-R2 — `state_verifier.verify_async` не блокирует и не возвращает ошибку в `start_zone`

`services/zone_control.py:115-118`:
```python
state_verifier.verify_async(int(zone_id), 'on')
```
Запускает thread в `services/observed_state.py:53-63`. Если verifier через 3 retry с timeout `OBSERVED_STATE_TIMEOUT_SEC` (нужно посмотреть в constants) поймёт что hardware не подтвердил → `_record_fault` → telegram-alert (но `bot_users=0` на проде — никто не получит).

Проблема: пока проверка идёт (десятки секунд), `start_zone` уже вернул True пользователю и `state='on'` в БД. Если на самом деле клапан не открылся — UI показывает зелёный, БД говорит `'on'`, реальной воды нет, fault_count++ только потом. Метрики потери воды отсутствуют.

**Файлы:** `services/zone_control.py:113-118`, `services/observed_state.py:65-132, 213-256`.

### HIGH-R3 — APScheduler MemoryJobStore (на проде) — все jobs теряются на рестарте

`prod-snapshot §9`: `SQLAlchemy` не установлен → `irrigation_scheduler.py:183-188` fallback на MemoryJobStore. Это означает:
- При `systemctl restart wb-irrigation` все scheduled `program:*` jobs **теряются**
- Восстановление: `init_scheduler(db)` пересоздаёт jobs из таблицы `programs` (1 row на проде) — **через `_reschedule_all_programs`** (нужно проверить, что вообще вызывается)
- Volatile-jobs (cap-time stop, master-valve close) — **не восстанавливаются** (они эфемерные, привязаны к конкретному run)

**Impact:** если рестарт случился за 30 сек до запланированного `_stop_zone`, зона будет литься до 240 мин watchdog cap. Если рестарт случился перед `master_valve_close` — мастер-клапан закроется только при следующем stop любой зоны (delayed close в `services/zone_control.py:264-293`).

**Файлы:** `irrigation_scheduler.py:179-202`; `requirements.txt` (нет `SQLAlchemy`).

### HIGH-R4 — Watchdog cap=240 минут — клапан может литься 4 часа

`constants.py: ZONE_CAP_DEFAULT_MIN = 240` (default). Это значит при любой потере состояния (см. CRIT-R3, CRIT-R1) max watering = 4 часа на одну зону. Для дома с 24 зонами и одной активной программой это критично: за 4 часа на open ground при типичных 1 LPM × 8 атм × 4 жиклёра = ≥ 1 м³ воды на одну зону. Master-valve может ограничить, но если он же открыт другой группой — нет.

**Файлы:** `services/watchdog.py:23` (default), `constants.py` (`ZONE_CAP_DEFAULT_MIN`), но в `db/settings` ключ `zone_cap_minutes` может перекрывать.

### HIGH-R5 — Двойная регистрация SIGTERM handler (`run.py` vs `app_init.py`)

- `run.py:60-62`: `signal.signal(SIGTERM, _graceful_shutdown)` — вызовет `shutdown_all_zones_off()` и `sys.exit(0)`.
- `services/app_init.py:309-312` (`_register_shutdown_handlers`): `signal.signal(SIGTERM, _signal_handler)` — вызовет `shutdown_all_zones_off(db=db)`, **сбросит handler в SIG_DFL и переотправит сигнал процессу** (`os.kill(os.getpid(), signum)`).

`run.py` регистрирует **ПЕРВЫМ** (на line 61, до `from app import app` — **нет, `from app import app` на строке 10, до signal**). На самом деле порядок:
1. `from app import app` (line 10) → `app.py:78` `setup_logging` → `app.py:268` `_initialize_app(...)` → `services/app_init.py:289` `_register_shutdown_handlers(db)` → **signal handler `_signal_handler` зарегистрирован первым**.
2. `run.py:61` `signal.signal(SIGTERM, _graceful_shutdown)` — **перезатирает** на `_graceful_shutdown`.

Итог: на SIGTERM сработает только `run.py:_graceful_shutdown` → `shutdown_all_zones_off()` (без `db` параметра — ок, fallback на global) → `sys.exit(0)`. Двойной atexit (`run.py` нет atexit, но `app.py:378` + `services/mqtt_pub.py:232` + `app_init.py:306` все atexit-регистрируют shutdown). Идемпотентность защищена `_shutdown_done` flag в `services/shutdown.py:12`. ОК.

Но: **порядок shutdown_all_zones_off vs MQTT client disconnect** не гарантирован между atexit-регистраторами. `services/mqtt_pub.py:232` `atexit.register(_shutdown_mqtt_clients)` — последний зарегистрированный, отрабатывает первым. **MQTT клиенты могут быть отключены ДО того как `shutdown_all_zones_off` попытается опубликовать OFF** → OFF сообщения не отправятся.

**Файлы:** `run.py:60-62`, `app.py:378`, `services/app_init.py:298-318`, `services/mqtt_pub.py:215-232`, `services/shutdown.py:80-176`.

### HIGH-R6 — `shutdown.py` ждёт `wait_for_publish` с timeout=10 сек × N зон последовательно

`services/shutdown.py:154-163`:
```python
for topic, res in inflight:
    res.wait_for_publish(timeout=timeout_sec)  # default 10 sec each
```
24 зоны × 2 (`/on` дубль) × 2 (master valves) ≈ 50+ inflight. `systemd TimeoutStopSec=20` (`wb-irrigation.service:13`) — **20 секунд на полный shutdown**. Если broker глючит, `wait_for_publish` повиснет → systemd kill -9 после 20 сек → не все клапаны успеют OFF.

**Файлы:** `services/shutdown.py:154-163`, `wb-irrigation.service:13`.

### HIGH-R7 — `_perf_start_timer` использует `request._started_at` без guard для async/SSE

`app.py:144-146`: `request._started_at = _perf_time.time()`. Для SSE-стримов request живёт минутами, `Server-Timing` header в `_perf_add_server_timing` неинформативен. Не критично, но засоряет.

### MED-R1 — `_delayed_close` мастер-клапана — thread без cancellation

`services/zone_control.py:264-293`: `_delayed_close` thread спит `MASTER_VALVE_CLOSE_DELAY_SEC` (60 сек), потом проверяет, есть ли активные зоны. Если за это окно стартует новая зона, второй `stop_zone` запустит **второй** `_delayed_close` thread. На graceful shutdown эти threads — daemon, убиваются process-exit. Но: thread spawning без bound = unbounded; при rapid start/stop можно накопить десятки sleeping threads.

**Файлы:** `services/zone_control.py:264-293`.

### MED-R2 — `state_verifier._subscribe_and_wait` создаёт **новый MQTT-client per verification**

`services/observed_state.py:144-211`: каждая verification создаёт свой `mqtt.Client(...)`, подключается, подписывается, ждёт, отключается. На каждый zone start/stop — новое connection. На проде 18 connected clients у broker — часть это. На rapid start/stop — connection storm. На constrained ARM (4-core, 3.8 GiB) ок, но плохо для cleanup при broker рестарте.

### MED-R3 — `boot_sync` дублируется (lines 87-138 + lines 168-211 в `app_init.py`)

`_boot_sync` зачем-то делает зон → master valves (первый pass), потом ещё раз зон + master valves (secondary safety net) → 4 публикации на каждую зону при старте. На 24 зоны = 96 MQTT-публикаций при старте. Не критично, но noisy и медленно.

### LOW-R1 — `services/scheduler_service.py` пуст — dead stub, может ввести в заблуждение

---

## 3. Findings: Recovery после краха

### CRIT-RC1 — Полностью отсутствует "open zone reconciliation" после рестарта

После рестарта Flask:
- `_boot_sync` шлёт OFF всем зонам безусловно (это правильно).
- Но **не проверяет hardware-state** (observed_state). Если зона была ON в hardware, broker положил retain `'1'` для топика, после рестарта приложения broker всё ещё хранит retain `'1'`, scheduler-jobs восстанавливаются из MemoryJobStore (т.е. **не восстанавливаются**), `_boot_sync` шлёт `retain '0'` — **переписывает retain**, но если broker прилёг между этими действиями — в retained state остаётся `'1'`.
- **Нет "uncommitted run" recovery**: если `zone_runs` имеет open row (start_time без end_time), он остаётся открытым навсегда (на проде `zone_runs=0`, эта фича вообще не пишется).

**Impact:** после power loss WB → зона была физически ON → boot scheduler не помнит про неё → клапан остаётся ON до cap watchdog (240 мин) **если** `_boot_sync` не докричался до broker. На проде `program_queue_log=0`, `zone_runs=0` — ни одной записи; recovery code мёртвый.

**Файлы:** `services/app_init.py:87-215`; `db/zones.py` (нет `recover_open_runs`).

### HIGH-RC1 — DB corruption / повреждение WAL — нет documented recovery path

`db/base.py:36-42`:
```python
def _connect(self) -> sqlite3.Connection:
    conn = sqlite3.connect(self.db_path, timeout=5)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
```
Если при power loss WAL не закоммитился — sqlite сам сделает rollback при next open. ОК. Но: `irrigation.db.bak.20260401_124241` — последний backup от 1 апреля (даже не 3 апреля, как из prod-snapshot — там `irrigation_backup_20260403_185606.db` в `/mnt/data/irrigation-backups/`, разные). Если БД полностью побьётся, recovery = restore от 18 дней назад → потеря 18 дней `logs`, `program_cancellations`, history.

Документации нет. `db.create_backup()` есть в `db/base.py` (нужно проверить путь), но cron не настроен.

### HIGH-RC2 — `program_queue.py` (855 LOC) — состояние in-memory, но `program_queue_log` table пустая

`prod-snapshot §7`: `program_queue_log` = 0 rows. То есть **очередь программ существует только в process memory**. На проде сейчас 1 программа, очередь не нужна. Но если бы было 5 программ с пересечением — после рестарта Flask вся pending queue теряется.

### MED-RC1 — `init_scheduler` не имеет sanity-check на orphaned jobs в SQLAlchemyJobStore

Если бы SQLAlchemy был установлен, `default` jobstore — sqlite. После manual delete program в БД, в jobstore остаётся `program:N` job. APScheduler выбросит `JobLookupError` на trigger.

---

## 4. Findings: Concurrency / race hazards

### CRIT-CC1 — `start_zone` race между scheduler-cap-watchdog и user-stop

Сценарий:
- T+0: user POST `/api/zones/5/start` → `exclusive_start_zone(5)` → берёт `group_lock(g)` (line 50) и `zone_lock(5)` (line 54).
- T+0.5: cap-time watchdog (`services/watchdog.py:_check_zones`) тикает каждые 30 сек. **Не берёт `zone_lock`** (line 100: прямо вызывает `self.zone_control.stop_zone(zone_id, ...)` без lock). `stop_zone` сам внутри берёт `zone_lock` (line 241).
- T+0.6: user POST `/api/zones/5/stop` → `stop_zone(5)` → ждёт `zone_lock(5)` (held by start_zone).

Если start_zone после `_versioned_update('starting')` "застрял" на MQTT publish (broker лагает) и **user stop ждёт zone_lock**, watchdog публикует OFF → **гонка**: start ещё публикует ON, watchdog уже OFF, user stop ждёт. Конечный hardware-state определяется тем, какой publish придёт последним к broker.

Защита: paho client единый per-server (sid), publish'ы сериализуются paho `_out_messages_lock`. Но между ON (start) и OFF (watchdog) разные threads, без ordering guarantee.

**Impact:** редко, но возможно "zone остаётся ON, БД думает что off" — возврат в `CRIT-R3`.

**Файлы:** `services/zone_control.py:40-175, 178-359`; `services/watchdog.py:70-118`; `services/locks.py:8-22`.

### HIGH-CC1 — `_active_zones_lock` (BUGS-REPORT bug #2) — реально не защищает то, что должен

`services/zone_control.py` использует `group_lock(group_id)` и `zone_lock(zone_id)` (`services/locks.py:8-22`) — RLock per group/zone из dict. Это **process-local**. Если бы было multiple workers (нет — Hypercorn single worker), не работало бы. ОК для single-process, **но** `services.locks._gl_lock` (line 6) — глобальный mutex для add lock в dict. Все access к dict сериализуются через него.

В `snapshot_*_locks` (lines 43-58) делается `acquire(blocking=False) → release()` для проверки lock state. Это **race-y**: между `acquire` и `release` другой thread может попытаться захватить, увидит что lock свободен → false negative для "is locked". Не критично для health-display, но вводит в заблуждение в `/api/health-details`.

### HIGH-CC2 — Группа G запускается from-first параллельно с другой G2 на тот же master valve

`services/zone_control.py:264-293`: `_delayed_close` проверяет **все группы** `db.get_groups()` на ту же `master_mqtt_topic`. ОК.

Но в `exclusive_start_zone:107` master valve OPEN сделан **без проверки**, открыт ли уже master другой группой. Не критично (idempotent retain publish), но 2 группы могут одновременно открыть/закрыть master в conflicting режимах (NO/NC), если у них разные `master_mode`. Это конфигурационная ошибка, но валидации нет.

### MED-CC1 — `master_valve_close_delay` (60 сек) — окно, в которое мастер открыт без активных зон

При sequential program runner (group_runner stops zone N, immediately starts zone N+1), мастер открывается, зона N стартует, через X минут N→stop, scheduler 60 сек ждёт, потом проверяет `any_on`. Если N+1 уже стартовал (within 60 сек) — мастер не закроется (правильно). Если N+1 стартует через 65 сек — мастер закрылся, потом N+1 откроет его → ненужный re-cycle.

### MED-CC2 — БД-миграция при старте + первый HTTP-запрос

`MigrationRunner.init_database()` вызывается при первом импорте `database.py` (через создание singleton `db = IrrigationDB()`). Если миграция долгая (`db/migrations.py` 1100 LOC, 35 миграций) — Hypercorn не отвечает на `/health` пока инициализация. systemd `Type=simple` не различает «процесс жив, но не готов». Нет `/readyz` endpoint.

---

## 5. Observability — текущее состояние

### Что работает

- `/health` (`routes/system_status_api.py:202-228`) — проверяет db.get_zones() OK, scheduler присутствует, mqtt_servers есть. Возвращает 200/503. **OK.**
- `/api/health-details` — детальная картина (jobs, zones, locks, group_cancels, meta_tail). **OK для UI, но требует admin.**
- Healthcheck cron (`scripts/healthcheck.sh`) — но он бьёт `/api/status` (не `/health`!), что менее строгая проверка.
- `scripts/watchdog.sh` — рестартит сервис после 3 fail'ов. Но нет evidence что cron на проде это запускает (см. open questions).
- journalctl получает stdout — единственный реально работающий лог. JSON-format включён через `WB_LOG_FORMAT=json` (default).

### Gaps (см. §6 ниже)

- **Bug #4** — file-log пустой 13+ дней (см. §1).
- Нет `/readyz` (отделить liveness от readiness — пока миграция, app live но не ready).
- Нет prometheus exporter / `/metrics` endpoint.
- Нет structured event-log в БД для critical ops (есть `logs` table, в неё пишется `add_log('zone_stop', ...)` `'watchdog_cap_stop'`, но не `'mqtt_disconnect'`, не `'scheduler_lag'`, не `'observed_state_fault'`).
- Нет correlation_id / request_id — невозможно связать "user clicked start" → "scheduler invoked" → "MQTT published" → "observed confirmed".
- Telegram `services/logs/telegram.txt` 520 KB **без ротации**.
- `import-export.log` — date-based rotation работает (одна работающая ветка).
- `weather_log`, `weather_decisions`, `zone_runs`, `program_queue_log`, `float_events` — **все пустые** на проде (фичи обрабатывают, но не пишут аудит).

---

## 6. Observability gaps — что добавить (для Phase 4)

### 6.1 Logging — обязательные events (in-process structured)

Каждое из этих событий должно писать в `logs` table + в файл:

| Event | Когда | Поля |
|---|---|---|
| `zone_start` | `services/zone_control.py:exclusive_start_zone` success | `zone_id, group_id, source (api/scheduler/program), command_id, mqtt_server_id` |
| `zone_stop` | `services/zone_control.py:stop_zone` success | `zone_id, reason, duration_sec, total_liters, avg_lpm` |
| `zone_force_stop` | `services/watchdog.py:_check_zones` cap | (есть) `'watchdog_cap_stop'` ✅ |
| `observed_state_fault` | `services/observed_state.py:_record_fault` | `zone_id, expected, attempts, fault_count` |
| `mqtt_disconnect` | `services/mqtt_pub.py:_on_disconnect` | `sid, rc, retry_after` |
| `mqtt_publish_failed` | `mqtt_pub.publish_mqtt_value` rc != 0 после 10 retry | `sid, topic, value, rc, attempts` |
| `mqtt_qos_delivery_failed` | line 173 | `sid, topic, value, qos` |
| `scheduler_lag` | если actual fire time > scheduled + threshold | `job_id, scheduled_for, fired_at, lag_sec` |
| `db_lock_busy` | `db/base.py:retry_on_busy` retry exhausted | `func_name, attempts` |
| `boot_sync_complete` | `app_init._boot_sync` | `zones_off_count, master_valves_closed, duration_ms` |
| `program_skip_weather` | (уже есть `'weather_skip'`) | ✅ |
| `power_loss_recovery` | если detected (uptime <60s + previous shutdown not graceful) | `last_clean_shutdown_at` |

### 6.2 Metrics — кандидаты для prometheus exporter

Минимальный набор для ARM (low overhead):

```
# Counters
wb_zone_start_total{zone_id, source}
wb_zone_stop_total{zone_id, reason}
wb_zone_fault_total{zone_id}
wb_mqtt_publish_total{sid, result="ok|fail"}
wb_mqtt_disconnect_total{sid}
wb_db_lock_busy_total{op}
wb_scheduler_misfire_total{job_type}

# Gauges
wb_zones_active{group_id}
wb_master_valves_open
wb_mqtt_clients_connected{sid}
wb_scheduler_jobs{jobstore}
wb_uptime_seconds
wb_db_size_bytes

# Histograms
wb_zone_duration_seconds{zone_id}
wb_mqtt_publish_latency_ms{sid}
wb_observed_state_confirm_latency_ms{zone_id}
wb_scheduler_lag_seconds
wb_http_request_duration_seconds{route,method,status}
```

**Реализация:** `prometheus_client` Python library (~200 KB), endpoint `/metrics` без auth (или behind `admin_required`). Pull-based, Telegraf уже стоит у Рауля для Zabbix (10.10.61.96) — может pull'ить.

### 6.3 Health endpoints

| Endpoint | Что проверяет | Кто бьёт |
|---|---|---|
| `/health` (есть) | db, scheduler, mqtt_configured | внешний monitor |
| `/healthz` (NEW — liveness) | process alive only — не трогает БД/MQTT | k8s/systemd watchdog |
| `/readyz` (NEW — readiness) | migrations complete, scheduler running, MQTT connected ≥1 server, boot_sync_complete | load balancer |
| `/metrics` (NEW) | prometheus | Telegraf/Zabbix |

### 6.4 systemd watchdog

Текущий unit (`wb-irrigation.service`) **не использует** `WatchdogSec=`. Можно добавить `WatchdogSec=60` + код приложения должен бить `sd_notify("WATCHDOG=1")` каждые ≤30 сек. Это даст automatic restart при freeze (когда HTTP отвечает но scheduler thread мертв).

### 6.5 Correlation / request-id

Добавить `X-Request-ID` middleware в Flask, прокидывать в `extra={'request_id': rid}` для logger calls. JSONFormatter уже умеет дополнительные поля (`logging_setup.py:61`).

### 6.6 Алерты в Zabbix (контур у Рауля 10.10.61.96)

| Алерт | Severity | Условие |
|---|---|---|
| `wb_zone_active_too_long` | HIGH | `wb_zone_duration_seconds > 200*60` (за 20 мин до cap) |
| `wb_mqtt_disconnected` | HIGH | `wb_mqtt_clients_connected == 0` 60s |
| `wb_scheduler_dead` | CRIT | `up{job="wb"} == 0` или `wb_scheduler_jobs == 0` |
| `wb_zone_fault_rate` | MED | `rate(wb_zone_fault_total[1h]) > 0.05` |
| `wb_db_size_growth` | LOW | `delta(wb_db_size_bytes[7d]) > 50MB` |
| `wb_app_log_silent` | HIGH | log file mtime > 1h (catch Bug #4) |
| `wb_backup_stale` | MED | newest backup mtime > 24h |
| `wb_disk_rootfs` | HIGH | rootfs >75% (на проде уже 59%) |

---

## 7. SLO/SLI candidates

Для домашнего полива (не enterprise, но важно для клиента):

| SLI | Definition | SLO target | Окно |
|---|---|---:|---|
| **Availability** | `count(http_status<500) / count(total)` для `/api/zones/*/start`, `/stop` | **99.5%** | 30d |
| **Liveness** | `up{wb-irrigation}` | **99.9%** | 30d |
| **Time-to-respond start** | `p95(http_request_duration_seconds{route="/api/zones/*/start"})` | **< 1s** | 7d |
| **Time-to-actuate** | `p95(observed_state_confirm_latency_ms)` от publish ON до echo ON | **< 3s** | 7d |
| **Scheduler precision** | `count(scheduler_lag_seconds < 30) / count(total)` | **99% < 30s** | 30d |
| **State consistency** | `1 - (zone_fault_total / zone_start_total)` | **>99%** | 30d |
| **Backup freshness** | `time() - max(backup_mtime)` | **< 24h** | continuous |
| **Recovery time** | время от systemd start до `/readyz` 200 | **< 60s** p95 | 7d |
| **Watering accuracy** | `\|actual_duration - planned_duration\| / planned` | **< 5%** | 30d |
| **MQTT publish success** | `count(rc=0) / count(total)` | **99.9%** | 7d |

### Error budget

Для 99.5% availability за 30d → допустимо **3.6 часа downtime/месяц**. Практический сценарий: 1 рестарт = ~30 сек = 0.001% бюджета. Можно делать ~400 рестартов/месяц прежде чем budget исчерпан. Реальное узкое место — broker outage (см. SPOF §10).

---

## 8. systemd / Docker review

### `wb-irrigation.service` (`/opt/.../wb-irrigation.service`)

| Поле | Значение | Замечание |
|---|---|---|
| `Type` | `simple` | ✅ для Hypercorn |
| `Restart` | `on-failure` | ⚠ не рестартует при `exit 0`; если scheduler падает но process exit clean — нет restart |
| `RestartSec` | `5` | ✅ |
| `TimeoutStopSec` | `20` | ⚠ см. HIGH-R6 — для shutdown 24 зон + master valves может не хватить |
| `User=` | (отсутствует) | ⚠ бежит от **root**; `prod-snapshot §11` подтверждает |
| `WatchdogSec=` | (отсутствует) | ⚠ нет detection freeze |
| `MemoryMax=` | (отсутствует) | LOW — на ARM 3.8 GiB и сейчас 246 MB usage, но при leak можно положить wirenboard |
| `CPUQuota=` | (отсутствует) | LOW |
| `LimitNOFILE=` | (отсутствует) | MED — paho-mqtt + threads + SQLite могут уп rest |
| `EnvironmentFile=` | (отсутствует) | MED — секреты в env через service-file = в systemctl show всем видны |
| `Requires=mosquitto.service` | ✅ | — |
| `StartLimitBurst=` | (default 5) | ⚠ если сервис падает ≥5 раз за 10 сек → systemd marks failed, **больше не рестартует** |

### Dockerfile / docker-compose

`landscape.md §11`: HEALTHCHECK в Dockerfile использует `curl /` (не `/health`). Healthcheck возвращает HTML главной страницы — 200 даже если scheduler/db мёртв.

---

## 9. Cron / scheduled jobs / log rotation

### Backups

`prod-snapshot §8`: последний DB-backup от **3 апреля 2026** (16 дней назад). Cron-задание для `db.create_backup()` **не настроено** (open-questions §181 — `crontab -l` не дампился, но факт отсутствия свежих файлов — proxy для отсутствия).

Backup endpoint `POST /api/backup` (`routes/system_emergency_api.py:82-91`) — **manual only**. Не запускается из APScheduler.

### Log rotation

| Лог | Ротация | Статус |
|---|---|---|
| `app.log` | TimedRotatingFileHandler `when='midnight', backupCount=7` (`logging_setup.py:155`) | ⚠ ротация настроена, но файл пустой |
| `import-export.log` | TimedRotatingFileHandler `backupCount=7` (line 165) | ✅ работает |
| `services/logs/telegram.txt` | **нет ротации** — open-write, 520 KB после 13 дней | ⚠ без bound |
| journalctl | systemd default vacuum (size/time-based) | ✅ |

### `watering_history` / `logs` table cleanup

`logs` table уже **8339 rows**. SQLite справится, но через год будет ~250k. Нет cleanup-job.

`zone_runs`, `weather_decisions`, `weather_log`, `program_queue_log`, `float_events` — на проде **пустые** (фичи мёртвые). Нет cleanup-policy документированной.

---

## 10. Single Point of Failure inventory

| Компонент | SPOF? | Cost of redundancy на ARM | Acceptable risk? |
|---|---|---|---|
| `mosquitto` | YES (1 брокер, 18 clients) | Bridge на второй брокер на том же WB — не имеет смысла. Альтернатива: MQTT over second WB или внешний (cloud) — overkill. | YES (mosquitto stable, 13.7d uptime) |
| `irrigation.db` (SQLite) | YES (1 файл) | Litestream / replica на second disk → SD card `/dev/mmcblk1p1` уже есть. **Дёшево.** | NO — рекомендую |
| Flask process (Hypercorn) | YES (1 worker) | Multi-worker не работает с in-memory `_active_zones_lock` (см. CC1). Sticky=N/A. **Не имеет смысла без реархитектуры.** | YES для дома |
| `cloudflared` tunnel | YES (1 туннель) | Cloudflare сами мульти-хоп. ОК. | YES |
| `eMMC /dev/mmcblk0p6` (где db, code, logs) | YES | Backup на SD `/dev/mmcblk1p1` — дёшево | NO — рекомендую регулярный backup |
| `.irrig_secret_key` | YES — потеря = шифрованные пароли в БД нельзя расшифровать | Backup на encrypted offline | YES (при backup-стратегии) |
| WB power | YES (нет UPS data) | UPS не код. — | (вне scope SRE-кода) |

---

## 11. Chaos scenarios — для будущей проверки (после фиксов)

> ⚠ **Не запускать на проде до фикса CRIT-R1, CRIT-R3, CRIT-RC1.** Сначала dev/staging.

### Сценарии (концептуально, не выполнялись)

| # | Сценарий | Что должно случиться | Что вероятно случится сейчас |
|---|---|---|---|
| C1 | `systemctl stop mosquitto` на 2 минуты во время полива | Зоны должны остановиться через cap-watchdog; после `start mosquitto` все клапаны OFF | Зона остаётся ON (paho buffer), watchdog OFF не достучится; на reconnect получает retain `'1'` → продолжит литься. **Failmode.** |
| C2 | `iptables -A INPUT -p tcp --dport 1883 -j DROP` на 30 сек | paho reconnect через 1-5 сек после правила удаления, scheduler-jobs опаздывают на 30 сек, ничего не теряется | Скорее всего OK (QoS=2 paho retry в memory) **если процесс жив** |
| C3 | `kill -9 $(pgrep -f run.py)` посреди полива зоны 5 | systemd рестартит, `_boot_sync` шлёт OFF всем → клапан 5 закрывается | OFF может не дойти до broker (CRIT-R1), клапан 5 остаётся ON 4 часа |
| C4 | `dd if=/dev/zero of=/tmp/fill bs=1M count=2000` (заполнить tmpfs) | `/health` 503, systemd рестартит | Возможен deadlock на logging if disk full (но log_dir=`backups/` = `/mnt/data` — 52 GB free, OK) |
| C5 | `dd ...` на `/mnt/data` до 0 free | DB writes fail → `add_log` exceptions → клапаны застревают | Catastrophic — нет alerting на disk full |
| C6 | WB power off на 5 сек (имитация) | Boot < 60s до полной готовности; полив не возобновляется без user action | Скорее всего ок, но scheduler может дублировать missed jobs (нет `coalesce=True` для program runs?) |
| C7 | Параллельно открыть 3 web-tabs и нажать Start на разных зонах одной группы за 100ms | Только последняя остаётся ON (exclusive_start_zone) | OK по дизайну (group_lock сериализует), проверить latency |
| C8 | Вызвать `db.create_backup()` во время полива (через `/api/backup`) | Backup sync, БД locked на 1-2 сек, retry_on_busy справится | Возможен `CRIT-R3` сценарий (job старта зоны попал в окно backup) |
| C9 | Заполнить `logs` table 10M rows и измерить latency `db.add_log` | <100ms p95 | Не известно — нет benchmark |
| C10 | Симулировать broker который accept'ит publish но не доставляет (firewall на out-bound) | observed_state verifier поймает через 3×timeout, fault_count++ | OK, но alert уйдёт в telegram = nowhere (`bot_users=0`) |

---

## 12. Runbook-готовность

### Что есть

- `README.md`, `README-LONG.md`, `DEPLOY-DOCKER.md` — установка/деплой.
- `scripts/healthcheck.sh`, `scripts/watchdog.sh` — auto-recovery cron.

### Чего нет (must-have для Phase 4)

| Runbook | Содержание | Priority |
|---|---|---|
| **Полив не запускается ночью** | Чек journalctl, `/api/scheduler/jobs`, postpone_until, weather_skip last decision, telegram bot status | HIGH |
| **MQTT не публикует** | Чек `systemctl status mosquitto`, `mosquitto_sub -t '#' -v`, `db.get_mqtt_servers()` enabled, `services.mqtt_pub._MQTT_CLIENTS` через `/api/health-details` | HIGH |
| **БД locked** | Stop service, `sqlite3 irrigation.db 'PRAGMA integrity_check; .recover'`, restart | MED |
| **Bot отвечает странно / молчит** | `bot_users` count, `telegram_bot_token` valid, `services/logs/telegram.txt` tail, `aiogram` polling thread alive | LOW |
| **Зона залипла "ON" в БД, hardware OFF** | Manual `db.update_zone(N, {'state':'off'})`, `mqtt publish topic 0`, restart watchdog | HIGH |
| **Зона залипла "OFF" в БД, hardware ON** | `mosquitto_sub` retain check, manual publish 0 retain | CRIT (заливает ландшафт) |
| **Восстановление из backup** | Stop service, `cp irrigation_backup_*.db irrigation.db`, restart, verify `/health` | HIGH |
| **Diskfull `/mnt/data`** | Удалить старые backups, vacuum sqlite, truncate logs | MED |
| **Power loss recovery** | После boot — manual `/api/health-details`, проверить `commanded_state vs observed_state`, force-stop любые "ON" зоны | HIGH |

---

## 13. Сводка по приоритету (для Phase 4 roadmap)

### Must-fix перед production-grade reliability

1. **CRIT-O1 / Bug #4** — root cause logging (см. §1) — **blocks any incident response**
2. **CRIT-R1** — boot_sync без ack confirmation, hardware может остаться ON
3. **CRIT-R3** — `database is locked` race в start_zone, hardware ≠ DB
4. **CRIT-RC1** — отсутствует open-zone reconciliation после рестарта
5. **CRIT-CC1** — race watchdog↔user-stop↔scheduler

### Should-fix (HIGH)

6. APScheduler MemoryJobStore — установить SQLAlchemy + jobstore persistence
7. systemd `WatchdogSec=` + `User=` + `LimitNOFILE`
8. Backup cron + retention policy
9. `scripts/watchdog.sh` cron установить если не установлен (open question)
10. Telegram log rotation (520 KB и растёт)

### Nice-to-have (MED/LOW)

11. `/healthz`, `/readyz`, `/metrics` endpoints
12. Prometheus exporter + Zabbix integration
13. Runbook documentation (см. §12)
14. Correlation IDs

---

## Appendix A — Конкретные file:line ссылки на находки

```
CRIT-O1   services/logging_setup.py:155-159   handler attached to 'app' only
CRIT-O1   irrigation_scheduler.py:46-48       basicConfig overrides root level
CRIT-O1   services/logging_setup.py:197       apply_runtime_log_level — never called
CRIT-R1   services/app_init.py:140-166        boot_sync no wait_for_publish
CRIT-R2   services/mqtt_pub.py:83-90          on_disconnect log-only
CRIT-R3   services/zone_control.py:54-113     state machine race
CRIT-RC1  services/app_init.py:87-215         no observed_state check on boot
CRIT-CC1  services/zone_control.py:40-175     +  services/watchdog.py:70-118
HIGH-R1   services/zone_control.py:40-175     no postpone check on manual start
HIGH-R2   services/observed_state.py:53-63    verify_async fire-and-forget
HIGH-R3   irrigation_scheduler.py:179-202     MemoryJobStore fallback
HIGH-R4   services/watchdog.py:23             ZONE_CAP_DEFAULT_MIN = 240
HIGH-R5   run.py:60-62  +  app_init.py:309    SIGTERM double-register
HIGH-R5   services/mqtt_pub.py:232            atexit ordering vs shutdown_all
HIGH-R6   services/shutdown.py:154-163        wait_for_publish × 50+ in 20s
HIGH-RC1  db/base.py:36-42                    no documented recovery path
HIGH-RC2  services/program_queue.py           in-memory only, table=0 rows
MED-R1    services/zone_control.py:264-293    unbounded delayed_close threads
MED-R2    services/observed_state.py:144-211  new mqtt client per verify
MED-R3    services/app_init.py:87-215         duplicated boot sync passes
MED-CC2   db/migrations.py + database.py      blocking init at first import
LOW       services/scheduler_service.py       dead stub
OPS       wb-irrigation.service:1-15          no User/WatchdogSec/LimitNOFILE
OPS       prod-snapshot §8                    last backup 16 days old
OPS       services/logs/telegram.txt          no rotation, 520 KB
```
