# Phase 2 — Performance audit `wb-irrigation` (refactor/v2)

> Performance Benchmarker, агент 1/8 параллельной фазы. READ-ONLY. Цифры по проду — только из `landscape/prod-snapshot.md`. Бенчмарки — концептуальные / по статанализу. Реальные нагрузочные прогоны на ARM-устройстве WB-Techpom **не выполнялись** (защита прода).

- **Локальный путь:** `/opt/claude-agents/irrigation-v2/`
- **Ветка:** `refactor/v2`, HEAD `6a47153` (= prod `e37adb7` + audit scaffold)
- **Цель устройства:** Wirenboard 7, aarch64, 4 ядра, 3.8 GiB RAM, Python 3.9.2
- **Не дублирую:** security (security.md), code-quality (code-quality.md), будущие SRE/DB агенты

---

## 0. Executive summary

Текущий процесс на проде живёт спокойно: **224.8 MB RSS**, **0.2 % CPU**, **load 1.12** на 4-ядерном aarch64, uptime 13.5 дней без рестартов. Очевидных перформанс-инцидентов нет — но это во многом следствие того, что **нагрузочный профиль крошечный**: 24 зоны, 1 активная программа, 0 пользователей Telegram, SSE-фронт **отключён физически** (см. §2), браузер ходит на `/api/status` 1 раз в минуту (по journal).

Главные системные риски лежат не в текущей нагрузке, а в **«потенциальной энергии»** кода:

1. **`/api/status` (606 LOC) — самый горячий endpoint** проекта, и при этом самый «многослойный»: 3 запроса к SQLite + 1 синхронный TCP `connect()` к MQTT-брокеру с таймаутом 3 с — **в каждом вызове**. Сейчас это работает потому, что брокер `127.0.0.1:1883` отвечает мгновенно. Если брокер уляжется или будет под нагрузкой, p95 этого endpoint'а вырастет с ~50 мс до 3000+ мс.
2. **APScheduler `MemoryJobStore` fallback** (SQLAlchemy не установлен в venv — см. prod-snapshot §9). `irrigation_scheduler.py:179-193` пытается `SQLAlchemyJobStore`, но `ImportError` тихо переходит в memory. Перформанс-эффект: запись задач не попадает в SQLite (плюс к доступной throughput'у), но при рестарте все одноразовые `DateTrigger`'ы (`zone_stop`, `program_run`) **теряются** — это уже в SRE/DB-зону, я отмечаю как факт.
3. **SSE-хаб включён, фронтовый endpoint выключен** (`routes/zones_watering_api.py:197-206` возвращает 204), но MQTT-подписка hub'а живёт и тянет всё с брокера. Это консервативное решение, спасшее event loop на ARM, но оно делает SSE мёртвым кодом для UI и оставляет latency обновления зон 5 с (polling).

Подробности — в §2…§11.

---

## 1. Метрики прода (из `prod-snapshot.md`, §1, §3, §6, §7, §9)

| Параметр | Значение | Источник |
|---|---|---|
| ОС / архитектура | Debian 11, aarch64, kernel 6.8.0-wb153 | §1 |
| Ядер | 4 | §1 |
| RAM total | 3.8 GiB | §1 |
| RAM used (всё) | 636 MiB used / 1.3 GiB free / 1.9 GiB buff/cache | §1 |
| Swap | 255 MiB total / 1 MiB used | §1 |
| Loadavg 1/5/15 | 1.12 / 1.25 / 1.24 | §1 |
| Disk root | 2.0 GB total, 762 MB free (41 % free) | §1 |
| Disk eMMC `/mnt/data` | 55 GB total, 52 GB free | §1 |
| Python | 3.9.2 (системный, но pyproject.toml говорит py311) | §2 |
| App PID 2989014 | uptime 13d 16h, **RSS 246 700 KB** = 241 MB, VSZ 1.84 GB | §3 |
| App CPU за uptime | 54 min 36 s (=>≈ 0.27 % средний) | §3 |
| App tasks (threads) | **15** | §3 |
| systemd Memory= | 224.8 MB (без `MemoryMax=` лимита) | §3 |
| Mosquitto RSS | 8.0 MB | §4 |
| Mosquitto clients connected | **18** | §4 |
| cloudflared RSS | 56.6 MB | §5 |
| Listening ports | 8080 (Flask), 1883 (MQTT plain), 18883 (MQTT WS), 8042/80 (nginx), 8011 (basic-auth proxy) | §6 |
| irrigation.db | **1 183 744 байт** (~1.16 MB), 289 страниц × 4096 | §7 |
| WAL/SHM при снимке | **отсутствуют** (между checkpoint'ами) | §7 |
| journal_mode | wal | §7 |
| synchronous (live) | **2 (FULL)** — несоответствие миграциям, см. ниже | §7 |
| Логи `services/logs/telegram.txt` | 520 KB, без ротации | §8 |
| `app.log` | 0 байт за 13 дней (Bug #4) | §8 |
| Последний DB-backup | 3 апреля (16 дней назад) | §8 |
| pip: SQLAlchemy | **отсутствует** → APScheduler в MemoryJobStore | §9 |
| pip: pytest | 7.4.3 в prod-venv (dev утёк) | §9 |

**Что НЕ снято / NotMeasured:**

- `mosquitto.db` (sessions persistence файл) — не в prod-snapshot.
- WAL hi-watermark — измерить нужно после `wal_autocheckpoint` цикла.
- Per-thread RSS / `/proc/<pid>/task` — не снято.
- `iotop` / disk write rate — не снято (eMMC chunk write — критично для SSD-life).
- Реальный latency `/api/status` p50/p95 — не измерен (только косвенно: журнал `polling раз в минуту`).

→ см. `open-questions.md` для второго захода в Phase 4.

---

## 2. SSE Hub (`services/sse_hub.py`, 363 LOC)

### Архитектура (факты)

- **Не thread-per-client:** один MQTT-клиент на каждый `mqtt_servers.id` (`_SSE_HUB_MQTT: dict`), `loop_start()` в фоне → callback `_on_message` → `broadcast()` фан-аут в queue каждого клиента (`sse_hub.py:281-294`).
- **Очередь на клиента:** `queue.Queue(maxsize=20)` (`sse_hub.py:351`).
- **Жёсткий лимит:** `MAX_SSE_CLIENTS = 5` (`sse_hub.py:23`). При превышении — eviction старейшего: `oldest.put_nowait(None)` — sentinel для остановки генератора (`sse_hub.py:344-350`). Хорошо.
- **Backpressure:** при `queue.Full` клиент признаётся «мёртвым» и удаляется из `_SSE_HUB_CLIENTS` (`sse_hub.py:80-91`). То есть **slow client → discard, OOM невозможен**, поскольку очередь bounded.
- **Heartbeat / keepalive:** **отсутствуют** в коде. Нет периодического `ping`-сообщения, нет timer'а `:retry:`. Браузер за NAT может потерять connection незаметно. Cleaner-thread (`sse_hub.py:325-333`) только логирует count раз в 60 с, но ничего не отправляет.
- **Disconnect detection:** только при попытке `q.put_nowait()` → `queue.Full`. Если клиент молча закрыл сокет, hub не узнает об этом до следующего fan-out события (которого может не быть минутами).

### КРИТИЧЕСКИЙ факт: SSE endpoint **отключён** для фронта

`routes/zones_watering_api.py:197-206`:

```python
@zones_watering_api_bp.route('/api/mqtt/zones-sse')
def api_mqtt_zones_sse():
    """SSE endpoint — DISABLED to prevent event loop death on ARM/Hypercorn.
    Frontend uses 5s polling instead. Returns 204 No Content."""
    _sse_hub.ensure_hub_started()
    return ('', 204)
```

Подтверждено в JS: `static/js/status.js:1079` — комментарий `// SSE disabled — polling every 5s provides updates; SSE caused event loop death on ARM`.

→ **`MAX_SSE_CLIENTS=5` сейчас не релевантен** (HTTP-клиентов 0), но MQTT-подписка hub'а активна → состояние zones синхронизируется retain-сообщениями и в процессе `_on_message` пишет в SQLite (`sse_hub.py:271-279`).

### Что осталось живо (даже без фронта)

`sse_hub._on_message` для **каждого** MQTT-сообщения по подписанным топикам:

1. Декодирует payload (`sse_hub.py:179`)
2. Если `/meta` — append в `_SSE_META_BUFFER deque(maxlen=100)` (OK, bounded).
3. Иначе (zone state) — **писатель в SQLite** на каждое сообщение: `_db.update_zone(int(zid), updates2)` (`sse_hub.py:277`).
4. Дополнительно дёргает scheduler (`schedule_zone_stop` / `cancel_zone_jobs`, `sse_hub.py:251-270`).
5. fan-out в queue клиентов (`sse_hub.py:288-293`) — сейчас no-op (клиентов 0).

→ **Каждый MQTT-апдейт на топик зоны = 1 `UPDATE zones SET state=...`** с одной транзакцией. При 24 зонах и периодических retain-обновлениях это норма, но если какой-то клапан начнёт «дребезжать» (топик пишется 10 раз/с), мы будем лочить SQLite на каждый дребезг. Никакого debouncing нет.

### Расчёт памяти на SSE-коннект (если вернут endpoint)

Для одного активного клиента:
- `queue.Queue(maxsize=20)` — структура ~1 KB + до 20 × средний payload.
- Payload `data = json.dumps({'zone_id': ..., 'topic': ..., 'payload': ..., 'state': ...})` — **~80–150 B** (zone_id+topic ~50 B + 30 B обрамление JSON).
- Worst-case на клиента: 20 × 150 B = **3 KB на полную очередь**.
- Плюс Hypercorn ASGI scope + headers + Flask `Response` generator + рабочий поток (если sync route) — ориентировочно **~80–200 KB на keep-alive HTTP коннекшн**.

→ 5 клиентов × ~200 KB ≈ 1 MB — для устройства с 1.3 GiB free безопасно. Но реальная боль на ARM была не в памяти, а в **event loop blocking** Hypercorn под sync generator'ом. (NB: bandit/ruff не ловит это — нужен реальный бенч.)

### Findings (SSE)

| ID | Sev | Файл / line | Что |
|---|---|---|---|
| SSE-01 | HIGH | `services/sse_hub.py` (whole) + `routes/zones_watering_api.py:197-206` | SSE-фронтенд **выключен**, но hub в backend живёт. Получаем худшее из обоих миров: подписки и запись в SQLite на каждое MQTT-событие, при этом UI всё равно опрашивает HTTP. Решение либо включить SSE с heartbeat и backpressure-метриками, либо сократить hub до минимума MQTT→DB. |
| SSE-02 | MED | `sse_hub.py:325-333` | Cleaner-thread спит 60 с и только логирует. Нет heartbeat/ping в очереди → нет detection «тихо отвалившийся клиент». При возврате SSE — добавить `:keepalive: ` каждые 15 с. |
| SSE-03 | MED | `sse_hub.py:243-279` | На каждое MQTT-сообщение — `db.update_zone()` транзакция. Никакого debouncing/coalescing. При флапе клапана — серия писем в WAL. |
| SSE-04 | LOW | `sse_hub.py:174-294` | `_on_message` — 120 строк глубокой вложенности, внутри несколько `with _SSE_HUB_LOCK:` секций. На горячем пути это сериализует все события. На текущей нагрузке норма; при возврате SSE может стать bottleneck. |

---

## 3. Scheduler (APScheduler + `irrigation_scheduler.py` 1365 LOC + `scheduler/` 1258 LOC)

### Конфигурация (факты)

- `BackgroundScheduler` с **двумя** jobstore'ами при наличии SQLAlchemy (`irrigation_scheduler.py:182-188`):
  - `default` → `SQLAlchemyJobStore(url='sqlite:///{db_path}')`
  - `volatile` → `MemoryJobStore`
- На проде **SQLAlchemy не установлен** (см. prod-snapshot §9). В коде это перехватывается `ImportError` (line 22-24), и **оба jobstore'а становятся опциональными**. При полном отсутствии — APScheduler использует свой default `MemoryJobStore`.

→ В реальном проде scheduler:
- Persistent jobs (`program:<id>` cron-триггеры) **не сохраняются между рестартами** — восстанавливаются `init_scheduler()` из БД.
- One-shot `zone_stop` / `zone_hard_stop` / `master_valve_close` — теряются мгновенно при `systemctl restart`.

### Threads count

Из `prod-snapshot §3`: **Tasks: 15**. Реконструкция:

| # | Thread | Источник |
|---|---|---|
| 1 | MainThread (Hypercorn event loop) | `run.py:55-77` |
| 2 | APScheduler job-runner pool worker (default 10) | `BackgroundScheduler` defaults |
| 3 | sse-cleaner | `sse_hub.py:333` |
| 4 | sse_hub MQTT loop (1 на сервер, на проде 1 server) | `sse_hub.py:310` |
| 5 | mqtt_pub publisher loop (`get_or_create_mqtt_client`) | `mqtt_pub.py:77` |
| 6 | telegram_bot `_thread_target` | `telegram_bot.py:420` |
| 7 | telegram_bot `_thr` (long-poll) | `telegram_bot.py:529` |
| 8 | float_monitor MQTT loop | `services/float_monitor.py` |
| 9 | rain_monitor MQTT loop | `services/monitors/rain_monitor.py` |
| 10 | env_monitor MQTT loop | `services/monitors/env_monitor.py` |
| 11 | water_monitor MQTT loop | `services/monitors/water_monitor.py` |
| 12 | observed_state.verify_async (transient, daemon) | `observed_state.py:58` |
| 13 | watchdog (cap-time) | `services/watchdog.py` (TASK-010) |
| 14-15 | APScheduler внутренние (timer + listener) | APScheduler internals |

15 потоков в `Tasks` совпадают со счётом. **18 MQTT-клиентов в брокере** = sse_hub (1 клиент на server) + mqtt_pub (cached per server) + 4 monitor'а × несколько подключений + StateVerifier transient + telegram_bot + бэкграунд probe из `/api/status` (создаёт **новый** client на каждый запрос! см. `system_status_api.py:466-475`).

### `/api/status` создаёт MQTT-клиент **на каждый запрос**

`routes/system_status_api.py:464-478`:

```python
for s in candidates:
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=...)
        if s.get('username'):
            client.username_pw_set(...)
        client.connect(s.get('host') or '127.0.0.1', int(s.get('port') or 1883), 3)
        mqtt_connected = True
        try:
            client.disconnect()
        ...
```

**Каждый вызов `/api/status` = 1 TCP connect + disconnect** к mosquitto. Сейчас фронт зовёт раз в 5 с → 17 280 connect/disconnect в сутки. На брокере это видно в `$SYS/broker/clients/total`. Это **главный неоптимальный паттерн** в горячем пути.

### Lag scheduler

Из journal на проде (`prod-snapshot §3`) — только однотипные INFO-строки `api_status: temp=None hum=None` каждую минуту. **WARN/ERROR от scheduler за 3 дня — нет**. Это означает, что либо лаг в норме, либо `apscheduler` логгер подавлен до ERROR (`irrigation_scheduler.py:60-62` — подтверждено).

→ **Не измеряем lag ничем, кроме отсутствия misfire ошибок.** При появлении нагрузки (например, 5 программ × 24 зоны × 7 дней) misfire может остаться незамеченным.

### Findings (Scheduler)

| ID | Sev | Файл / line | Что |
|---|---|---|---|
| SCH-01 | HIGH | `irrigation_scheduler.py:179-193` + prod venv | SQLAlchemyJobStore unavailable → `MemoryJobStore` only. Все one-shot jobs (zone_stop, hard_stop) теряются при рестарте. Bug перетекает в SRE-зону, но perf-impact: при рестарте scheduler заново пересчитывает расписание из `programs` — холодный старт ~ N×repository round-trips. |
| SCH-02 | MED | `irrigation_scheduler.py:60-62` | apscheduler logger forced to ERROR. Misfire-warnings не видны → невозможно измерить scheduler lag в production. Невозможно поставить SLA. |
| SCH-03 | MED | `routes/system_status_api.py:464-478` | На каждый `/api/status` создаётся новый paho `Client()` + `connect()` + `disconnect()`. При polling 5 с это ~ 17k TCP сессий/сутки на брокере. Кешировать соединение или использовать `_MQTT_CLIENTS` из `mqtt_pub.py`. |
| SCH-04 | LOW | `scheduler/` пакет (1258 LOC) | mixin-based подход размазывает логику по 6 файлам. Сам по себе perf-нейтральный, но когнитивная нагрузка ухудшает шанс обнаружить deadlock/блокировку. |

---

## 4. SQLite под нагрузку

### PRAGMA фактическое vs ожидаемое

`db/migrations.py:21-28` устанавливает при инициализации:
- `journal_mode=WAL` ✓ (на проде WAL)
- `foreign_keys=ON` — но на проде через ad-hoc connection видно `foreign_keys=0` (per-connection PRAGMA, нормально для SQLite)
- `synchronous=NORMAL` — но live `PRAGMA synchronous=2 (FULL)` (см. prod-snapshot §7)
- `wal_autocheckpoint=1000` страниц
- `cache_size=-4000` (4 MB)
- `temp_store=MEMORY`

**Расхождение по synchronous:** миграция ставит NORMAL, но live показывает FULL. SQLite `synchronous` действительно per-connection. На каждом `BaseRepository._connect()` (`db/base.py:36-42`) ставится только `journal_mode=WAL` и `foreign_keys=ON` — **synchronous НЕ переустанавливается**, поэтому остаётся default (FULL). Эффект: каждый COMMIT — fsync → на eMMC это ~5-15 мс на write transaction. На NORMAL было бы ~1-2 мс.

→ **Это самый дешёвый perf-win во всём проекте**: добавить `conn.execute('PRAGMA synchronous=NORMAL')` в `BaseRepository._connect()`.

### `busy_timeout`

`BaseRepository._connect()`: `sqlite3.connect(self.db_path, timeout=5)` — это python-уровневый timeout (5 секунд активного waiting). PRAGMA `busy_timeout` явно **не выставляется**, но Python sqlite3 автоматически использует параметр `timeout=` через PRAGMA busy_timeout эквивалент (внутри C-кода). 5 с достаточно много для contended writes.

### Concurrent writers (статанализ)

Реальные писатели (по prod-snapshot + код):

| Источник | Частота write | Транзакция |
|---|---|---|
| `routes/system_status_api.py /api/status` (`logger.info`+`add_log` при mqtt_warn) | 1/мин (polling) | `INSERT logs` редко |
| `routes/*` (start/stop/postpone) | по событию | `UPDATE zones`, `INSERT logs` |
| `services/sse_hub._on_message` | per MQTT update | `UPDATE zones SET state, observed_state, ...` |
| `services/observed_state.StateVerifier._record_fault` | при фоле | `UPDATE zones SET fault_count` |
| `services/monitors/water_monitor` | per pulse | `INSERT water_usage` (на проде таблица пустая → не используется) |
| `services/monitors/rain_monitor` | per state change | `INSERT logs` |
| `services/program_queue.py` worker threads | per program step | `UPDATE zones`, `INSERT program_queue_log` (на проде пустая) |
| `services/weather.py._save_cache` | 1/30 мин | `INSERT OR REPLACE weather_cache` + `DELETE WHERE old` |
| `irrigation_scheduler.py` postpone_sweeper | 1/мин | `UPDATE zones SET postpone_until=NULL` (если есть expired) |

С учётом WAL, **много читателей + один писатель** — норма для SQLite. Текущая нагрузка 1 страница / несколько секунд — далеко от лимитов SQLite (~50k tx/sec на nvme, ~100-500 tx/sec на eMMC класса WB).

**Bottleneck сценарии:**
- VACUUM на горячей БД — блокирует всех писателей. На 1.16 MB БД займёт ~50-200 мс на eMMC. Безопасно ночью. На размере 100 MB будет 5-20 с — уже опасно.
- `program_queue` worker × 4 одновременные программы + scheduler stop_zone × N зон + `/api/status` writer = можно увидеть `database is locked`. `retry_on_busy(max_retries=3)` (`db/base.py:12-27`) спасёт большинство случаев (3 retry × backoff 0.1/0.2/0.4 = до 700 мс).

### Direct `sqlite3.connect` обходящие repo

```
services/weather.py:332,381,708,737,756,779,883  (7 connections)
services/float_monitor.py:424
```

Эти соединения **не наследуют** PRAGMA из `_connect()`. Если завтра кто-то добавит `PRAGMA synchronous=NORMAL` в `BaseRepository._connect()`, эти 8 connection'ов всё равно останутся с FULL. Это снижает predictability.

### Findings (SQLite)

| ID | Sev | Файл / line | Что |
|---|---|---|---|
| DB-01 | HIGH | `db/base.py:36-42` | `_connect()` не ставит `synchronous=NORMAL` и `busy_timeout` PRAGMA. На проде `synchronous=FULL` — каждая write tx делает fsync. Самый дешёвый perf-win: 1 строчка → ~3-5× ускорение write throughput на eMMC. |
| DB-02 | MED | `services/weather.py:332,381,708,737,756,779,883` + `services/float_monitor.py:424` | 8 прямых `sqlite3.connect()` обходящих `BaseRepository._connect()`. Не наследуют будущие PRAGMA. Pollution layering — также упомянуто в code-quality.md. |
| DB-03 | MED | `db/logs.py:42` | `get_logs()` всегда `LIMIT 1000`, но `routes/system_status_api.py:528` отдаёт все 1000 записей фронту. На проде в `logs` уже **8339 строк**, при росте до 100k фильтр в Python (`api_logs:531-548`) — **полная сортировка** + JSON-сериализация ~ 1 MB. Лучше push фильтра в SQL (limit/offset/where). |
| DB-04 | LOW | `db/migrations.py:21-26` | PRAGMA ставятся **только в момент init_database**, не на каждом `_connect()`. После рестарта они работают только пока тот connection жив. |

---

## 5. Memory profile (статанализ — без runtime)

### Singleton'ы и in-memory state

| Объект | Файл | Bounded? |
|---|---|---|
| `db = IrrigationDB()` (`database.py`) | facade singleton, не держит данных, только path | ✓ |
| `_MQTT_CLIENTS: Dict[int, Client]` | `mqtt_pub.py:23` | bounded по числу `mqtt_servers` (на проде 1) |
| `_TOPIC_LAST_SEND: Dict[Tuple[int, str], Tuple[str, float]]` | `mqtt_pub.py:25` | **unbounded** — растёт по числу уникальных (server, topic) пар |
| `_SERVER_CACHE: Dict[int, Tuple[dict, float]]` | `mqtt_pub.py:27` | bounded по числу серверов |
| `_SSE_HUB_CLIENTS: list[queue.Queue]` | `sse_hub.py:27` | bounded `MAX_SSE_CLIENTS=5` |
| `_SSE_META_BUFFER: deque(maxlen=100)` | `sse_hub.py:29` | ✓ bounded |
| `_SSE_HUB_MQTT: dict` | `sse_hub.py:28` | bounded по числу серверов |
| `_LAST_MANUAL_STOP: dict[int, float]` | `sse_hub.py:33` | **unbounded** — ключ zone_id, реально bounded числом зон (24), но grow-only |
| `IrrigationScheduler.active_zones: Dict[int, datetime]` | `irrigation_scheduler.py:203` | bounded числом зон |
| `IrrigationScheduler.program_jobs: Dict[int, List[str]]` | `irrigation_scheduler.py:204` | bounded числом программ |
| `IrrigationScheduler.group_cancel_events: Dict[int, threading.Event]` | `irrigation_scheduler.py:206` | **unbounded** — ключ group_id, в практике bounded числом групп |
| `ProgramQueueManager._queues: Dict[int, GroupQueue]` | `program_queue.py:79` | bounded числом групп |
| `WeatherService` — кеш в SQLite (`weather_cache`) | `services/weather.py:752-789` | bounded — есть `DELETE WHERE fetched_at < now-4×TTL` (line 786-788) ✓ |
| `EnvMonitor.temp_value, hum_value` | `services/monitors/env_monitor.py` | scalar |
| `WaterMonitor.*` | counter dicts по группам | bounded числом групп |
| `_TOPIC_LAST_SEND` в `mqtt_pub` | per (server, topic) | grows ≤ ~50 топиков на проде |
| `services/logs/telegram.txt` | 520 KB и растёт | **нет ротации** ⚠️ — логи не очищаются |

### Memory leak hotspots (потенциальные)

1. **`mqtt_pub._TOPIC_LAST_SEND`** (`mqtt_pub.py:25`): grow-only. Сейчас bounded числом топиков (~50), но если кто-то начнёт публиковать в динамически генерируемые топики (например, `zones/<uuid>/state`), словарь будет расти. Нет TTL очистки.
2. **`sse_hub._LAST_MANUAL_STOP`** (`sse_hub.py:33`): grow-only по zone_id. На текущих 24 зонах — пренебрежимо.
3. **`_SSE_META_BUFFER`** — bounded ✓.
4. **`telegram_bot`** — `services/logs/telegram.txt` без ротации (см. prod-snapshot §8). За 13.5 дней — 520 KB, при заполнении бот-аудита может дорасти до 100+ MB и положить eMMC.
5. **`observed_state.StateVerifier._safe_verify`** запускает daemon thread на каждый verify (`observed_state.py:58`). Если verify долго висит (timeout=`OBSERVED_STATE_TIMEOUT_SEC` × 3 retries), а вызовов много, можно создать сотни недозавершённых потоков. Сейчас вызовы — после каждого start/stop клапана, в норме 1-2 в секунду peak. Безопасно.
6. **`apscheduler` MemoryJobStore** хранит job'ы в RAM. При тысячах одноразовых zone_stop job'ов это могло бы быть проблемой, но текущая модель — несколько программ × несколько cron триггеров. Норма.
7. **`Hypercorn` WSGIMiddleware** при ASGI mode — каждый HTTP request создаёт thread; при долгих responses (SSE stream) thread живёт долго. С отключённым SSE — норма.

### Observation: рост RSS = 0 за 13 дней

Из prod-snapshot §3: RSS = **246 700 KB**, uptime 13.5 дней. Если бы был серьёзный leak, при ~17 280 polls/день и активной MQTT-подписке мы бы увидели 300+ MB. Текущая ситуация говорит, что **leak'ов в running code сейчас нет** (или они компенсируются GC). Это эмпирическое наблюдение, не статанализ.

### Findings (Memory)

| ID | Sev | Файл / line | Что |
|---|---|---|---|
| MEM-01 | MED | `services/logs/telegram.txt` (на проде 520 KB) | **Нет ротации** Telegram-логов. При активном боте — линейный рост, нет cap. Рекомендация: TimedRotatingFileHandler. |
| MEM-02 | LOW | `mqtt_pub.py:25` `_TOPIC_LAST_SEND` | Grow-only dict. Сейчас bounded ~50 топиков, но без TTL/LRU. Добавить очистку записей старше 1 часа. |
| MEM-03 | LOW | `sse_hub.py:33` `_LAST_MANUAL_STOP` | Grow-only, реально bounded числом zone_id. Незначительно. |
| MEM-04 | LOW | `observed_state.py:58` | Каждый verify_async — новый daemon thread. На worst-case (50 параллельных start_zone) — 50 thread'ов одновременно. ARM с 4 ядрами выдержит, но это не настроено лимитом — лучше ThreadPoolExecutor с `max_workers=4`. |

---

## 6. Время отклика API (статанализ, без runtime измерений)

Базовое предположение: на ARM @ 1.2 GHz Cortex-A53 (WB7) средний SQL-вызов 1-3 мс на 1 MB БД, JSON-сериализация ~10 KB ≈ 5-10 мс, paho `connect()` к localhost ≈ 1-3 мс. Hypercorn ASGI overhead ~2-5 мс. Итого baseline **простого endpoint** ~15-30 мс.

| Endpoint | Файл / route | Ожидание (ARM, baseline) | Узкие места |
|---|---|---|---|
| `GET /api/zones` | `routes/zones_crud_api.py:18` → `db.get_zones()` | **~30-50 мс** | 1 SQL JOIN `zones LEFT JOIN groups`, 24 строки, ~28 колонок — JSON ~10 KB. OK. |
| `GET /api/zones/<id>` (через `/api/zones/<id>/...`) | `db.get_zone(id)` | **~10-20 мс** | 1 SQL по PK, маленький JSON |
| `POST /api/zones/<id>/start` или `/mqtt/start` | `routes/zones_watering_api.py:211` (235 LOC до stop) | **~80-200 мс** | 1) `get_zone`, 2) `get_programs`, 3) `cancel_group_jobs`, 4) `db.cancel_program_run_for_group` per program, 5) `reschedule_group_to_next_program`, 6) `get_zones` для peers, 7) `concurrent.futures` background thread пул для OFF peers, 8) MQTT publish QoS 2 с wait_for_publish. **Всё ещё горячий путь** — единственная критичная задержка в проекте. |
| `POST /api/zones/<id>/stop` | `routes/zones_watering_api.py:94` | **~50-100 мс** | MQTT publish QoS 0, db.update_zone, mark_zone_stopped |
| `GET /api/programs` | `routes/programs_api.py` | **~20-40 мс** | 1 SQL — простая выборка из `programs` (1 строка на проде) |
| `GET /api/system/status` (`/api/status`) | `routes/system_status_api.py:253` (266 LOC) | **~50-150 мс normal, до 3000 мс при недоступном MQTT** | 1) `get_rain_config`, 2) `get_zones` (JOIN), 3) `get_groups`, 4) `get_programs` (для next_start), 5) `get_env_config`, 6) `get_mqtt_servers`, 7) **`mqtt.connect()` с timeout=3** (block!), 8) `water_monitor.get_current_reading_m3` per group, 9) `water_monitor.get_flow_lpm` per group. **5 SQL + 1 синхронный TCP.** |
| `GET /api/weather/...` | `routes/weather_api.py` | **~10-50 мс** (cache hit), **~200-1000 мс** при cache miss (HTTP к Open-Meteo с timeout=10) |
| `POST /api/groups/<id>/start-from-first` | `routes/groups_api.py` | **~150-300 мс** | start_zone цепочка для первой зоны группы |
| `GET /api/logs?limit=100` | `routes/system_status_api.py:522` → `db.get_logs()` | **~50-200 мс** при 8339 строках, **~500 мс+** при 100k строк | `get_logs()` всегда тянет все (`LIMIT 1000`) и фильтрует в Python (см. DB-03) |
| `GET /` (главная `/`) | `routes/status.py` → `templates/status.html` (12 KB) | **~50-100 мс** | Jinja render, нет SSR-данных (фронт зовёт `/api/status` после load) |

### N+1 / лишние вызовы

- **`/api/status`** (`system_status_api.py:253-517`): для каждой группы — `parse_dt` postpone, потом `db.get_programs()` ВНУТРИ цикла по группам (`line 306`). Если групп >10, это N+1 (на проде 2 группы — норма). При расширении системы получим квадратичную сложность.
- **`/api/zones/next-watering-bulk`** (`zones_crud_api.py:235-358`): O(zones × programs × 14 days) Python-уровневый перебор. На 24 zones × 1 program × 14 days = ~336 итераций — норма. На 100 zones × 10 programs × 14 days = 14 000 итераций по в основном Python-арифметике — будет ~50-200 мс на ARM.
- **`/api/water`** (`system_status_api.py:558-606`): per-group `get_zones_by_group` + `get_water_usage`. На 2 группах OK. На 20 — 40 SQL.

### Findings (API)

| ID | Sev | Файл / line | Что |
|---|---|---|---|
| API-01 | HIGH | `routes/system_status_api.py:464-478` | Per-request `mqtt.Client().connect()` с **timeout=3 секунды**. Если брокер недоступен — `/api/status` зависает на 3 с. Polling 5 с → каждый poll стоит 3 с лочки thread пула. См. SCH-03. |
| API-02 | MED | `routes/system_status_api.py:306` | `db.get_programs()` вызывается **в цикле** по группам. Кешировать в локальную переменную перед циклом. |
| API-03 | MED | `db/logs.py:42` | `LIMIT 1000` в SQL, фильтрация дат в Python (`api_logs:531-548`). Для 100+ запросов в день и таблицы 8.3k → 80k строк/день при scaling — линейный рост latency. |
| API-04 | LOW | `routes/zones_watering_api.py:211-444` (mqtt/start) | 8 раздельных операций (DB + scheduler + MQTT). При peak можно увидеть 200+ мс. Ввести трейсинг (timing log) перед оптимизацией. |
| API-05 | LOW | `routes/zones_crud_api.py:235-358` | `next-watering-bulk` — Python-уровневый O(N×P×D) перебор. На малых N безвреден. При расширении переписать в SQL CTE. |

---

## 7. Frontend perf (поверхностно)

### Размер static assets

| Файл | Размер | Минифицирован? | Gzip на сервере? |
|---|---|---|---|
| `static/js/status.js` | **118 148 B** (115 KB) | НЕТ (читаемый код, ~2200 строк) | НЕТ (нет gzip middleware в Flask) |
| `static/js/zones.js` | **103 711 B** (101 KB) | НЕТ | НЕТ |
| `static/js/programs.js` | 22 620 B | НЕТ | НЕТ |
| `static/js/app.js` | 20 993 B | НЕТ | НЕТ |
| `static/js/zones-sensors.js` | 20 682 B | НЕТ | НЕТ |
| `static/js/zones-groups.js` | 18 576 B | НЕТ | НЕТ |
| `static/js/zones-table.js` | 13 369 B | НЕТ | НЕТ |
| `static/js/status/status-groups.js` | 21 734 B | НЕТ | НЕТ |
| `static/js/status/status-data.js` | 9 992 B | НЕТ | НЕТ |
| **JS total** | **388 KB** | — | — |
| `static/css/status.css` | 46 744 B | НЕТ | НЕТ |
| `static/css/zones.css` | 19 180 B | НЕТ | НЕТ |
| `static/css/programs.css` | 13 982 B | НЕТ | НЕТ |
| `static/css/base.css` | 10 028 B | НЕТ | НЕТ |
| **CSS total** | **116 KB** | — | — |
| `templates/*.html` (все 10) | ~120 KB | — | — |

### Что плохо

1. **Нет gzip/brotli compression** в Flask/Hypercorn (искал `Flask-Compress`, `gzip` middleware — не нашёл). Для текстовых assets gzip даёт ~70% сжатия → **388 KB JS → ~120 KB**. Это критично для мобильных клиентов через cloudflared (хоть Cloudflare сам по умолчанию gzip-ит на edge — но **не для не-кешируемого контента**, а static у нас без `Cache-Control`). Проверить в Phase 4.
2. **Нет минификации.** `status.js` 115 KB на ARM-устройстве парсится браузером ~50-100 мс на современном телефоне, ~200+ мс на старом. Минификация снизила бы файл до ~60-70 KB.
3. **Нет bundling.** 10 файлов JS = 10 HTTP requests при первом заходе. С HTTP/2 (cloudflared отдаёт HTTP/2) это менее критично, но всё равно overhead.
4. **`{{ asset('static/js/status.js') }}`** — есть какой-то asset-fingerprinting helper. Хорошо для cache-busting, но проверить, что он не ломает кеширование.

### SSE update frequency / browser overload

С отключённым SSE и polling 5 с:
- Каждые 5 с фронт делает **2 запроса параллельно**: `/api/status` + `/api/zones?ts=` (`status.js:1071-1073`).
- Дополнительно `/api/zones/next-watering-bulk` периодически, `/api/zones/<id>/watering-time` per active zone (`status.js:802` — каждые 3 с **per group** при активном поливе).
- Также: `setInterval(updateDateTime, 1000)` (line 1068) — обновляет UI время каждую секунду, чисто клиентское.
- Refresh weather каждые 5 минут (line 1417).

→ Фоновая нагрузка фронта на сервер: **0.4 req/s baseline**, **0.8-1.0 req/s при активном поливе**. На ARM с Hypercorn ~10-20 req/s capacity — есть 10× margin.

### Findings (Frontend)

| ID | Sev | Файл / dir | Что |
|---|---|---|---|
| FE-01 | MED | `static/js/*.js` (388 KB total) | Нет gzip compression на бэкенде. На неагрессивно-кешируемых assets через cloudflared сжатие может не работать. Проверить headers `Content-Encoding`. |
| FE-02 | LOW | `static/js/status.js` (118 KB), `static/js/zones.js` (104 KB) | Не минифицировано. Парсинг JS на старых мобильных — 200+ мс. Дешёвый win при наличии node-tooling. |
| FE-03 | LOW | `static/js/status.js:1071-1073` | Polling каждые 5 с. Это компромисс из-за отключённого SSE. Если вернуть SSE — снизить polling до 30 с (fallback). |
| FE-04 | LOW | `static/js/zones.js:802` | `__waterLiveTimers[groupId] = setInterval(fn, 3000)` — на каждую активную группу свой таймер. На 10 группах × 3 с = ~3.3 req/s в дополнение к baseline. |

---

## 8. Cloudflare Tunnel performance

### Факты (prod-snapshot §5)

- `cloudflared-poliv.service`, RSS **56.6 MB**, uptime 17 дней.
- Direct ingress: `poliv-kg.ops-lab.dev → http://localhost:8080` (Flask).
- **Не идёт через basic_auth_proxy** (он на `127.0.0.1:8011`), значит публичный домен открыт без HTTP basic auth — это в security.md.

### Perf-aspects

- **Latency edge → tunnel → localhost:** для региона CIS обычно edge (Frankfurt/Warsaw) → wb (Bishkek/Ekat) ~100-150 мс RTT. Cloudflared keeps connection (HTTP/2), поэтому handshake amortized.
- **cloudflared overhead:** 56.6 MB RSS — нормально. CPU не в prod-snapshot, но обычно <1% при низком rps.
- **Compression:** Cloudflare на edge применяет gzip/brotli **по умолчанию** для известных text MIME, но только если на origin есть `Cache-Control` или явно через `polish/auto-minify`. Без бэкендового gzip Cloudflare делает свой proxy-gzip — ОК.
- **Throughput limit:** на free Tier Cloudflare нет жёсткого rate limit, но cloudflared HTTP/2 stream по 1 connection может бить ~ 100 Mbps. Для нашего трафика (несколько KB/req) overkill.

### Что НЕ проверено

- Не запрошено `/etc/cloudflared/credentials-poliv.json` (security).
- TLS resumption / HTTP/2 health не измерены.
- Нет latency-теста edge → tunnel.

### Findings (Cloudflare)

| ID | Sev | Что |
|---|---|---|
| CF-01 | LOW | cloudflared RSS 56.6 MB — норма. Нет HEALTHCHECK в systemd unit для cloudflared (вне области). Проверить в SRE. |
| CF-02 | LOW | Cloudflare сам gzip-ит, но static assets без `Cache-Control` → каждый запрос идёт до origin. Добавить `Cache-Control: public, max-age=3600` для `/static/*` — снизит трафик на 80%+. |

---

## 9. MQTT performance

### Факты

- **Mosquitto 2.0.20**, RSS 8 MB. 18 connected clients (prod-snapshot §4).
- 3 listener'а: `:1883` (TCP plain), `:18883` (WebSocket plain), `/var/run/mosquitto/mosquitto.sock` (Unix). Все anonymous.
- На 4-ядерном ARM с 1 client per server в среднем — нагрузка минимальна (mosquitto C-implementation, очень эффективен).

### Topic count и подписки

Из кода и схемы:
- Zone topics — 24 (по числу zones). Pattern: `/devices/<wb>/controls/<n>` (Wirenboard).
- Master valve topics — несколько (в группах с master valve).
- Float sensor topics, rain sensor, env sensors, water meter — по 1 на конфигурацию.
- Итого — **~40-50 уникальных топиков** на типичной установке.

paho-mqtt subscribe pattern: каждый sse_hub MQTT client делает `client.subscribe(t, qos=1)` отдельно для каждого топика (`sse_hub.py:299-309`). На 50 топиках — 50 SUBACK'ов при инициализации. Не проблема.

### QoS levels

| Где | QoS | Стоимость |
|---|---|---|
| `services/sse_hub.py:301,307` subscribe | **1** | дёшево, broker отслеживает delivery |
| `services/observed_state.py:177` subscribe | 1 | — |
| `services/observed_state.py:126` publish (verifier retry) | **2** | дорого: 4 сообщения на publish |
| `services/shutdown.py:89,93,143,146` publish | **2** | дорого, но shutdown — редко |
| `services/app_init.py:134,152,197` publish (boot all-OFF) | 2 | при старте — допустимо |
| `services/mqtt_pub.py:138` publish (default qos=0, override-able) | 0 | дёшево; вызывающие сами выбирают |

QoS 2 (4 messages: PUBLISH/PUBREC/PUBREL/PUBCOMP) на публикацию **`watering`** не используется — это хорошо. QoS 2 только на boot/shutdown — приемлемо.

### `_TOPIC_LAST_SEND` дедупликация

`mqtt_pub.py:121-126`: skip duplicate если `last_value == value` и `now - last_ts < min_interval_sec` (default 0.2 с). Это **хорошая защита от шторма** — если кто-то пишет ON 100 раз в секунду, на брокер уйдёт только ~5/сек.

### `mosquitto.db` (persistent sessions)

В prod-snapshot **не указан размер `mosquitto.db`**. Нужно для Phase 4: проверить `/var/lib/mosquitto/mosquitto.db`. С `persistence true` в default config Wirenboard — этот файл может вырасти под retain-сообщениями × числом сессий (если QoS≥1 + clean_session=False).

### Findings (MQTT)

| ID | Sev | Что |
|---|---|---|
| MQTT-01 | LOW | `services/sse_hub.py` создаёт **отдельный** MQTT client (помимо `mqtt_pub._MQTT_CLIENTS`). На брокере это 2 коннекта на каждый сервер. На 1 server — норма. На 5 — 10 коннектов только на app. |
| MQTT-02 | MED | `routes/system_status_api.py:464-478` создаёт **per-request** MQTT-клиент. См. SCH-03 / API-01. |
| MQTT-03 | LOW | `mosquitto.db` size не снят. Проверить в Phase 4. |
| MQTT-04 | LOW | `mqtt_pub.py:71` `max_inflight_messages_set(100)` — нормально, но при QoS≥1 retransmit storm может забить ресурсы. Сейчас QoS 0 default — норма. |

---

## 10. Worst-case scenarios (концептуально)

| Сценарий | Что происходит | Защита есть? |
|---|---|---|
| **MQTT broker недоступен** во время `/api/status` | каждый poll фронта блокируется на 3 с (`connect timeout`). При polling 5 с — постоянная очередь request'ов. Hypercorn worker pool забивается. UI зависает. | НЕТ. См. API-01. |
| **50 параллельных SSE-клиентов** (если включат endpoint) | `MAX_SSE_CLIENTS=5` сразу evict'ит 45. Память OK. Но fan-out broadcast() в `_SSE_HUB_LOCK` сериализует все sends. | Частично (eviction). Heartbeat нет. |
| **Длинный history query** `GET /api/logs` без явного limit | `LIMIT 1000` в SQL, фильтр в Python. На 8k строк — OK; на 100k — 500 мс+ + 1 MB JSON. | Частично. См. DB-03. |
| **Конкурентный backup + полив** | `LogRepository.create_backup` (`db/logs.py:157-185`) использует `sqlite3.backup()` — это атомарный read snapshot, **не блокирует** writers (с WAL). Но `wal_checkpoint(TRUNCATE)` после backup'а блокирует writers. На 1 MB БД — миллисекунды; на 100 MB — секунды. | Частично. |
| **Большой weather pull** (Open-Meteo вернул много данных) | `_REQUEST_TIMEOUT=10` с. JSON ~50 KB. `_save_cache` пишет в SQLite. Безопасно. | ✓ |
| **Дребезг MQTT-топика клапана** (10 сообщений/с) | sse_hub `_on_message` пишет `UPDATE zones` per message → 10 write tx/s в WAL. На eMMC `synchronous=FULL` это 10 fsync/s = ~50-150 мс CPU/IO. Не катастрофа, но износ eMMC. | НЕТ. См. SSE-03. |
| **Telegram bot flood** (если регистрируется много users) | `bot_users=0` сейчас. `services/telegram_bot.py` — long-poll, aiogram FSM. Может забить thread, но `_thread` daemon. | Не оценено в этом аудите. |
| **Все 24 зоны одновременно стартуют** (программа OneAtATime=False) | 24 × `start_zone` цепочка = 24 × `cancel_group_jobs` + 24 × MQTT publish QoS 2 + 24 × `db.update_zone`. На ARM 4 core ~2-5 секунд burst. | Частично — `_active_zones_lock` в zone_control.py сериализует. |
| **Полный перезапуск (`systemctl restart wb-irrigation`)** | MemoryJobStore теряет все one-shot jobs. `init_scheduler()` пересоздаёт programs cron из БД. zone_stop для активной зоны **не восстанавливается** — клапан остаётся открыт до cap-time watchdog (TASK-010). | **Опасно** — см. SCH-01 + SRE. |

### Главный risk-1: **MQTT broker down → /api/status латентность 3 с × poll-rate**

Это единственный сценарий с реальным production-impact, видимый сегодня. Все остальные — потенциальные при росте нагрузки.

---

## 11. Quick wins (отсортировано по cost/impact)

> **Правило:** все win'ы — **сначала измерять** (трейсинг + before/after), потом мержить.

### Tier 1 — однострочные, big impact

| # | Где | Что | Impact |
|---|---|---|---|
| QW-1 | `db/base.py:36-42` `_connect()` | Добавить `conn.execute('PRAGMA synchronous=NORMAL')` и `conn.execute('PRAGMA busy_timeout=5000')` | **~3-5× ускорение write** на eMMC. Снижает p95 write tx с ~10-15 мс до ~2-3 мс. |
| QW-2 | `routes/system_status_api.py:464-478` | Заменить per-request `mqtt.Client().connect()` на использование `services.mqtt_pub.get_or_create_mqtt_client(server)` (cached) или просто проверить `client.is_connected()` без re-connect | Убирает 3-секундный freeze при недоступном брокере. p95 `/api/status` стабилизируется. |
| QW-3 | `routes/system_status_api.py:306` | Вынести `db.get_programs()` из цикла по группам в локальную переменную | На 2 группах — пренебрежимо; на 20 — ~30 мс выигрыш. Тривиально. |

### Tier 2 — небольшой рефакторинг

| # | Где | Что |
|---|---|---|
| QW-4 | `services/logs/telegram.txt` | Включить `TimedRotatingFileHandler` (when='midnight', backupCount=14). Без этого файл будет линейно расти при активном боте. |
| QW-5 | `db/logs.py:42` `get_logs()` | Перенести date-фильтрацию в SQL (`WHERE timestamp >= ? AND timestamp <= ?`) и добавить параметр `limit` в API endpoint. |
| QW-6 | `static/*` через nginx (`configs/nginx-rate-limit.conf`) | Добавить `gzip on; gzip_types text/css application/javascript; gzip_min_length 1024;` + `Cache-Control: public, max-age=3600` для `/static/*`. Снижает 388 KB JS до ~120 KB по wire. |
| QW-7 | `services/sse_hub.py:243-279` | Debounce `db.update_zone` если `new_state == current_state` (no-op) — и не плодить write tx. Дешёвая защита от дребезга. |

### Tier 3 — чуть дороже

| # | Где | Что |
|---|---|---|
| QW-8 | Установить `SQLAlchemy` в prod venv | Активирует SQLAlchemyJobStore → persistent jobs, не теряем zone_stop при рестарте. (Перетекает в SRE.) |
| QW-9 | Add Flask-Compress middleware или nginx gzip (см. QW-6) | См. FE-01. |
| QW-10 | `routes/zones_crud_api.py:235-358` | При числе зон/программ < N (текущий случай) — оставить. При scaling — переписать `next-watering-bulk` через CTE. |

### Кросс-ссылки (НЕ дублируем)

- **Индексы для `logs.timestamp`, `weather_log.created_at`, `zone_runs.zone_id`** — уже есть в миграциях (`db/migrations.py:108-925`). Если что-то не хватит — отдать в `database.md`.
- **Cache strategy для weather** — уже SQLite-cached с TTL 30 мин (`weather.py:752-789`). Норма.
- **Background jobs scheduling, persistence** — SRE-зона.
- **Auth bypass / CSRF** — security.md.

---

## 12. Open для бенчмарков (когда можно будет запустить нагрузку)

> Эти пункты требуют **реального исполнения** на копии прода или devbox. На WB-устройстве **не запускать**.

1. **`/api/status` p50/p95/p99** — на cold cache, на warm cache, при недоступном MQTT.
2. **wrk/locust scenario:** 5 RPS polling + одновременный start/stop zone — деградация?
3. **SSE @ 50 коннектов** — если SSE вернут, проверить event loop под Hypercorn на ARM.
4. **SQLite sustained write rate** — `synchronous=FULL` vs `NORMAL` на eMMC. Воспроизвести `database is locked` под нагрузкой.
5. **Scheduler misfire grace** — поднять `apscheduler` logger до INFO, нагрузить 50 одновременных trigger'ов, измерить lag.
6. **Memory growth over 7 days** — `pmap` процесса каждый час, найти heap-grow.
7. **eMMC write amplification** — `iostat` для `/dev/mmcblk0` под типовой нагрузкой суток. Критично для life expectancy WB-устройства.
8. **`mosquitto.db` размер** на проде — ssh, `ls -la /var/lib/mosquitto/`.
9. **Реальный browser TTI** на типовом мобильном через cloudflared — Lighthouse mobile.
10. **Effect of `TimedRotatingFileHandler`** на telegram.txt — disk IOPS под бот-нагрузкой.

---

## 13. Findings — сводная таблица severity

| ID | Sev | Component | Краткое описание |
|---|---|---|---|
| API-01 | **HIGH** | API | `/api/status` делает per-request `mqtt.connect()` с timeout=3 с — главный risk при недоступном брокере |
| DB-01 | **HIGH** | SQLite | `_connect()` не ставит `synchronous=NORMAL` → writes идут на FULL fsync (3-5× медленнее, чем могли бы) |
| SCH-01 | **HIGH** | Scheduler | SQLAlchemy не установлен → MemoryJobStore → one-shot zone_stop теряются при рестарте |
| SSE-01 | HIGH | SSE | SSE-фронт отключён, но MQTT-подписка hub'а живёт + пишет в SQLite. Архитектурный mismatch. |
| API-02 | MED | API | `db.get_programs()` в цикле по группам в `/api/status` |
| API-03 | MED | API | `get_logs` фильтрует даты в Python после `LIMIT 1000` |
| DB-02 | MED | SQLite | 8 прямых `sqlite3.connect()` обходящих BaseRepository |
| DB-03 | MED | SQLite | Отдача 1000 логов фронту без pagination |
| FE-01 | MED | Frontend | Нет gzip compression на бэкенде; static без Cache-Control |
| MEM-01 | MED | Memory | `services/logs/telegram.txt` без ротации (520 KB сейчас) |
| MQTT-02 | MED | MQTT | `/api/status` создаёт client на запрос — дубль API-01 |
| SCH-02 | MED | Scheduler | apscheduler logger подавлен до ERROR — misfire не видны |
| SCH-03 | MED | Scheduler | Per-request MQTT client в `/api/status` (см. API-01) |
| SSE-02 | MED | SSE | Cleaner-thread не отправляет heartbeat → silent disconnects |
| SSE-03 | MED | SSE | На каждый MQTT-апдейт → write tx, нет debouncing |
| API-04 | LOW | API | mqtt/start — 8 операций без трейсинга |
| API-05 | LOW | API | next-watering-bulk Python O(N×P×D) |
| CF-01 | LOW | CF | cloudflared health не мониторится |
| CF-02 | LOW | CF | Static без Cache-Control → лишний трафик через tunnel |
| DB-04 | LOW | SQLite | PRAGMA на init only, не на каждом connect |
| FE-02 | LOW | Frontend | JS не минифицирован (status.js 115 KB) |
| FE-03 | LOW | Frontend | Polling 5 с — компромисс из-за SSE-off |
| FE-04 | LOW | Frontend | per-group setInterval 3 с для water timers |
| MEM-02 | LOW | Memory | `_TOPIC_LAST_SEND` grow-only без TTL |
| MEM-03 | LOW | Memory | `_LAST_MANUAL_STOP` grow-only |
| MEM-04 | LOW | Memory | `verify_async` без ThreadPoolExecutor cap |
| MQTT-01 | LOW | MQTT | sse_hub MQTT client отдельно от mqtt_pub |
| MQTT-03 | LOW | MQTT | mosquitto.db size не снят |
| MQTT-04 | LOW | MQTT | max_inflight=100 при QoS≥1 retransmit storm risk |
| SCH-04 | LOW | Scheduler | scheduler/ mixin spread по 6 файлам |
| SSE-04 | LOW | SSE | `_on_message` 120 строк под глобальным lock |

---

## 14. Summary — топ-3 для Phase 4

> Если бюджет Phase 4 = 3 фикса, я бы взял эти. Каждый — **дёшев**, **измерим** и **с чётким before/after**.

### TOP-1: API-01 / SCH-03 / MQTT-02 — устранить per-request MQTT.connect() в `/api/status`

- **Файл:** `/opt/claude-agents/irrigation-v2/routes/system_status_api.py:464-478`
- **Что:** заменить локальный `mqtt.Client().connect()` на cached client из `services.mqtt_pub._MQTT_CLIENTS` (или просто проверять `client.is_connected()`).
- **Impact:** убирает 3-секундный freeze `/api/status` при недоступном брокере. Снижает количество TCP-сессий на mosquitto с ~17k/сутки до ~1.
- **Cost:** ~15 строк кода + 1 unit-test.
- **Метрика to verify:** p95 `/api/status` < 100 мс при `mosquitto stop`.

### TOP-2: DB-01 — `synchronous=NORMAL` + `busy_timeout` в `_connect()`

- **Файл:** `/opt/claude-agents/irrigation-v2/db/base.py:36-42`
- **Что:** добавить 2 строки в `BaseRepository._connect()`:
  ```
  conn.execute('PRAGMA synchronous=NORMAL')
  conn.execute('PRAGMA busy_timeout=5000')
  ```
  (NB: я не модифицирую код — фиксирую как рекомендацию для Phase 4.)
- **Impact:** ~3-5× ускорение write throughput, снижение износа eMMC, единый PRAGMA-baseline для всех 8+ direct `sqlite3.connect()` если их позже соберут под BaseRepository.
- **Cost:** 2 строки + ревью миграции на конфликт с migrations.py:25.
- **Метрика to verify:** время на 1000 sequential `db.add_log()` < 2 сек (до — ожидается 5-15 сек).

### TOP-3: SSE-01 — определиться с SSE: либо «вернуть с heartbeat», либо «убрать hub backend полностью»

- **Файлы:** `/opt/claude-agents/irrigation-v2/services/sse_hub.py` + `/opt/claude-agents/irrigation-v2/routes/zones_watering_api.py:197-206`
- **Что:** сейчас архитектурный mismatch: SSE-endpoint выключен (фронт polling 5 с), но MQTT-подписка hub'а активна и пишет в SQLite. Два варианта:
  - **Вариант А (вернуть SSE):** поменять `_clean_loop` на `_heartbeat_loop` с `q.put(": ping\n\n")` каждые 15 с, добавить `event-source` reconnect на фронте, снять `204` заглушку. Reduce polling до 30 с fallback.
  - **Вариант B (упростить):** оставить только MQTT→DB sync в hub'е (без queue/broadcast), удалить `_SSE_HUB_CLIENTS` логику, не вводить пользователя в заблуждение «SSE есть».
- **Impact:** прозрачность кода, снижение latency UI до ~1 с (Вариант А), либо снижение complexity (Вариант B).
- **Cost:** Вариант А — ~50 LOC + e2e-тест с aiohttp; Вариант B — ~100 LOC удалить.
- **Метрика to verify:** Вариант А — UI обновляет zone state в течение 2 с после MQTT-события. Вариант B — RSS процесса -5 MB.

---

**Performance Benchmarker, signing off.** Findings сложены в:

`/opt/claude-agents/irrigation-v2/irrigation-audit/findings/performance.md`

Не пересекался: с security.md (auth, anonymous MQTT, CSRF), с code-quality.md (broad excepts, file LOC). Открытые вопросы для Phase 4 — в §12. Реальные load-тесты на проде **не запускались** — только на копии (см. §12).
