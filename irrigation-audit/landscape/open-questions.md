# Phase 1 — Открытые вопросы для Phase 2-4

> Список фактов, которые я НЕ смог однозначно подтвердить за один проход; ссылки — на конкретные file:line или на §-секцию `landscape.md` / `prod-snapshot.md`.
> Помечены приоритетом: 🔴 critical (security/safety), 🟠 high (correctness/data loss), 🟡 medium (operational), ⚪ low (cosmetic).

---

## Безопасность / Security

### 🔴 Q1. Mosquitto-брокер открыт в LAN без auth — это специально?

`prod-snapshot §4`: на проде effective config содержит `allow_anonymous true` на ВСЕХ трёх listener'ах (unix-socket, TCP 1883, websocket 18883). При этом файлы `/etc/mosquitto/passwd` и `/etc/mosquitto/acl` существуют (симлинки на `/mnt/data/etc/mosquitto/`) — но не подключены через `password_file`/`acl_file`.

**Вопросы:**
- Это сознательная конфигурация Wirenboard (для wb-rules / wb-mqtt-* демонов), или регрессия?
- Соответствует ли это компании-стандарту?
- Кто из 18 connected clients — wirenboard internal, а кто — внешний?
- Если wirenboard требует anonymous локально, можно ли ограничить bind 0.0.0.0:1883 → 127.0.0.1?

### 🔴 Q2. Flask app слушает 0.0.0.0:8080 без auth-front

`prod-snapshot §6`: процесс python (pid=2989014) биндится на `0.0.0.0:8080`. Cloudflare tunnel идёт **прямо в `localhost:8080`** (`prod-snapshot §5`), минуя `basic_auth_proxy.py` (на `127.0.0.1:8011`) и nginx (на `:80, :8042`).

**Вопросы:**
- Есть ли в LAN устройства, которые могут попасть на `:8080` напрямую, обходя любые прокси?
- На публичный домен `poliv-kg.ops-lab.dev` идёт **в обход basic-auth**? Зависим только от Flask session-based auth?
- Где гарантия, что `/api/zones/<id>/start` (CSRF-exempt в `app.py:96-109`) защищён? Какой декоратор стоит на blueprint'е?

### 🔴 Q3. CSRF снят со ВСЕХ API-blueprint'ов — обоснованно?

`app.py:96-109`: 10 blueprint'ов exempt'нуты от CSRF с комментарием *"behind nginx basic auth"*. Но из Q2 видно, что cloudflare tunnel может бить мимо nginx. Также SSE-hub принимает MQTT и шлёт в браузер — если SSE endpoint exempt от CSRF, какой механизм проверяет, что sender — авторизован?

**Вопросы для Phase 2 security-аудитора:**
- Поминутный аудит каждого CSRF-exempt blueprint'а — какой декоратор гарантирует auth?
- Есть ли guest-mode (см. `f041c50 fix: restore guest access to zone/group control`)?
- Какие именно эндпоинты доступны без login session?

### 🟠 Q4. AES-GCM шифрование секретов — какая реализация на refactor/v2?

`utils.py` содержит `encrypt_secret`/`decrypt_secret`. EXPERT-ANALYSIS упоминает «XOR-fallback». На refactor/v2 — что осталось от старой реализации?

**Вопросы:** прочитать `utils.py:encrypt_secret` целиком: чистый AES-GCM или есть fallback-ветка? Каковы последствия, если `.irrig_secret_key` отсутствует или короче 32 байт?

### 🟠 Q5. Прод-БД: `foreign_keys=0`

`prod-snapshot §7`: ad-hoc sqlite3 connection показал `PRAGMA foreign_keys = 0`. В `db/migrations.py:22` явно стоит `conn.execute('PRAGMA foreign_keys=ON')`.

**Вопросы:** SQLite `foreign_keys` — connection-уровневая. Каждое app-connection действительно делает `PRAGMA foreign_keys=ON` перед запросами? Проверь `db/base.py` и `database.py` — где открывается connection, который рутово используется?

---

## Данные / Целостность / Backups

### 🟠 Q6. Последний DB-бэкап от 3 апреля (16 дней назад)

`prod-snapshot §8`: `irrigation_backup_20260403_185606.db` — единственный backup, и тому 16 дней. На проде нет `cron`-задания для backups (не проверял `crontab -l` в этой фазе).

**Вопросы:**
- Есть ли cron / systemd-timer для бэкапа БД?
- Кто создал backup от 03.04 — `routes/system_emergency_api.py /api/backup` вручную?
- Стратегия retention?

### 🟠 Q7. Новые v2 таблицы пустые

`prod-snapshot §7`: `zone_runs=0`, `weather_decisions=0`, `weather_log=0`, `program_queue_log=0`, `float_events=0`, `water_usage=0`, `bot_users=0`, `bot_subscriptions=0`. При этом app работает 13.5 дней с 24-мя зонами и 1 программой.

**Вопросы:**
- Программа реально запускается? Запуски не фиксируются в `zone_runs` — почему?
- `program_queue_log` пустой — программная очередь не используется или пишется ТОЛЬКО при конфликтах?
- Telegram bot настроен в коде, но никто не зарегистрирован — токен в settings есть?

### 🟡 Q8. logs.id растёт — 8339 записей за 13.5 дней — нет ротации

`prod-snapshot §7`: таблица `logs` содержит 8339 записей. Нет видимой схемы cleanup'а.

**Вопросы:** есть ли retention policy в коде? Будет ли БД расти безгранично?

---

## Operational / DevOps

### 🟠 Q9. CI/CD на `main`, прод на `refactor/v2`

`landscape §10`: `.github/workflows/ci.yml` слушает `branches: [main, master]` — на refactor/v2 CI вообще не запускается.
`.github/workflows/deploy.yml` делает `git pull origin main` — снесёт прод-ветку.
`update_server.sh:14`: `BRANCH=${BRANCH:-main}` — то же самое.

**Вопросы:**
- Кто делает деплои на прод сейчас? Через что?
- Если кто-то запустит `update_server.sh` без `BRANCH=refactor/v2` — оно снесёт код. Это известная проблема?

### 🟠 Q10. Python 3.9 vs target py311

`prod-snapshot §2`: на проде Python 3.9.2.
`pyproject.toml:24`: `target-version = "py311"`.
`Dockerfile:2`: `FROM python:3.11-slim`.

**Вопросы:** какой синтаксис py3.10+ может прорваться в код через PR (например, `match`-statement, `X | Y` PEP-604 unions, `ParamSpec`, etc.)? Есть ли syntax check на CI? Бежит ли он на py3.9?

### 🟡 Q11. `routes/system_api.py`, `routes/zones_api.py` — dead code?

`landscape §3`: эти файлы есть в `routes/`, но НЕ зарегистрированы в `app.py:34-62` через `register_blueprint`. Возможно legacy.

**Вопросы:** удалить или зарегистрировать?

### 🟡 Q12. SSE endpoint URL — где?

`landscape §8`: SSE Hub присутствует (`services/sse_hub.py`), но я не нашёл в routes явного `@blueprint.route('/sse')`.

**Вопросы:** через какой URL клиенты подключаются к SSE? `/api/mqtt/<id>/scan-sse` — это разовый scan или persistent stream?

### 🟡 Q13. Telegram-бот: long-polling или webhook?

`app.py:65-71`: импорт + `start_long_polling_if_needed`. Условие `if_needed` — что это? settings.telegram_bot_enabled?

**Вопросы:** как переключение между polling и webhook? Где webhook URL хранится? Это конфликтует с CSRF-exempt blueprint'ами?

### 🟡 Q14. Hypercorn config: TLS, workers, timeout

`run.py:64-72`: создаётся `hypercorn.config.Config()` с **дефолтными** настройками + только `bind = ["0.0.0.0:8080"]`.

**Вопросы:**
- Один воркер? (проверь — hypercorn `workers` дефолт = 1)
- Нет HTTPS termination (полагается на cloudflared / nginx)
- `keep_alive_timeout`, `request_timeout` — дефолтные. Может ли SSE long-poll обрываться?

### 🟡 Q15. APScheduler без persistent jobstore

`prod-snapshot §9`: SQLAlchemy не установлен в venv → `irrigation_scheduler.py:21-29` fallback на `MemoryJobStore`.

**Вопросы:**
- При рестарте app все scheduled jobs теряются. Восстановление полагается полностью на `init_scheduler(db)` который читает программы из БД и пересоздаёт jobs?
- Если рестарт произошёл за минуту до scheduled-time программы — она запустится или потеряется?
- Manual-start jobs (через `schedule_zone_stop`) — теряются точно?

---

## Архитектура / Code smells (для Phase 2)

### ⚪ Q16. `services/weather.py` 1404 LOC и 7 прямых `sqlite3.connect()`

`landscape §4` + `landscape §11`: после консолидации `weather.py` стал 1404 LOC и обходит repository facade в 7 местах (lines 332, 381, 708, 737, 756, 779, 883).

**Вопросы для Phase 2 architecture-аудитора:**
- Это интенциональный design (e.g. weather-кеш изолирован, не должен делить транзакцию с zones)?
- Или код-долг, который надо вынести в `db/weather.py` repository?

### ⚪ Q17. `services/scheduler_service.py` пустой stub

`landscape §4`: файл объявлен как *"Intentionally left empty; use irrigation_scheduler.get_scheduler() directly."* Никто не должен его импортировать.

**Вопросы:** если никто не импортирует — удалить. Если кто-то импортирует, найти и переключить.

### ⚪ Q18. Дублирование mqtt-логики между `services/mqtt_pub.py` и `services/sse_hub.py`

`services/mqtt_pub.py` управляет publisher-клиентами. `services/sse_hub.py` управляет subscriber-клиентами (`_SSE_HUB_MQTT: dict`). Оба держат свои словари клиентов с lock'ами.

**Вопросы:** один Wirenboard MQTT — два независимых connection pool. Это намеренно (изоляция pub/sub), или можно унифицировать?

### ⚪ Q19. `pytest.ini` vs `pyproject.toml [tool.pytest.ini_options]`

`landscape §10`: оба файла объявляют pytest-настройки, частично пересекающиеся (testpaths, markers). pytest приоритезирует `pytest.ini` если он есть.

**Вопросы:** какой effective config? Не разъезжаются ли они со временем?

### ⚪ Q20. Тесты в CI — только 3 подпапки из 7

`landscape §10`: `ci.yml` гонит `pytest tests/unit tests/db tests/api`. Не запускает `tests/integration tests/e2e tests/ui tests/performance`.

**Вопросы:** где запускаются e2e/UI/perf тесты? Есть ли nightly job?

---

## Что НЕ снято (out of scope Phase 1)

| Позиция | Почему не снято | Кто должен снять |
|---|---|---|
| Содержимое `/usr/share/wb-configs/mosquitto/` (effective listener overrides) | Не дампилось в скрипте; вирtenboard-internal | Phase 2 security |
| `/etc/cloudflared/credentials-poliv.json` | Намеренно НЕ читал — credentials | Не нужно |
| `.git/config` на проде | Phase 2 при необходимости | Phase 2 ops |
| Полный `crontab -l` для root и для других пользователей | Не дампился | Phase 2 ops |
| `nginx -T` (полный effective config) | Не дампился | Phase 2 ops |
| Содержимое `basic_auth_proxy.py` на проде | Локальный код, не сравнен с прод-копией | Phase 2 |
| iptables / nftables | Не смотрел | Phase 2 security |
| `/etc/systemd/system/mosquitto.service.d/override.conf` | Не дампился | Phase 2 ops |
| `wb-rules` interactions с MQTT | Out of scope | Phase 2 wirenboard-эксперт |
| Объём DB до миграций (исходный размер) — нет baseline | Нет данных | — |
| Реальная форма данных в `logs` (8339 записей) — что туда пишется? | Намеренно не вытягивал данные | Phase 3 data quality |
| Realtime-нагрузка на MQTT (msg/sec) | Не замерял | Phase 2 perf |
| HTTP rate в Flask: количество запросов/мин | Не замерял | Phase 2 perf |
