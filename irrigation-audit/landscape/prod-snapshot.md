# Phase 1 — Prod-снапшот WB-Techpom (`10.2.5.244`)

> Strictly READ-ONLY. Только команды чтения (systemctl status/cat, journalctl, ls, cat, sqlite3 .schema/.tables/PRAGMA/COUNT, ss, ps, df, free, pip freeze).
> Снято: **2026-04-19 ~16:22 +0600** (UTC+6 / Asia/Yekaterinburg-equivalent).
> Метод: ssh `root@10.2.5.244` через `sshpass` + одноразовый агрегирующий скрипт `/tmp/prod_snapshot.sh`.

---

## 1. Хост и ОС

```
uname -a:   Linux WB-Techpom 6.8.0-wb153 #1 SMP Wed Mar 4 13:38:44 UTC 2026 aarch64 GNU/Linux
ОС:         Debian 11 (bullseye), aarch64 (Wirenboard 7-series)
Uptime:     17 days 1h 21m
Load avg:   1.12 / 1.25 / 1.24 (1m/5m/15m) на 4-ядерном устройстве
Mem total:  3.8 GiB  (used 636 MiB, free 1.3 GiB, buff/cache 1.9 GiB, available 3.0 GiB)
Swap:       255 MiB total, 1 MiB used
```

### Disk

```
/dev/root        2.0G  1.1G  762M  59% /            ← rootfs почти на грани (41% free)
/dev/mmcblk1p2    28G  280K   26G   1% /mnt/sdcard/backup
/dev/mmcblk0p6    55G  821M   52G   2% /mnt/data    ← где живёт код, БД, бэкапы
/dev/mmcblk1p1    32G  376M   30G   2% /mnt/sdcard/db
```

Все «реальные данные» на eMMC `/dev/mmcblk0p6` (`/mnt/data`) и SD-картах. Rootfs — тонкий.

---

## 2. Python и git

```
python3 --version:                                  Python 3.9.2
/opt/wb-irrigation/irrigation/venv/bin/python:      Python 3.9.2
```

> **Расхождение:** `pyproject.toml:24` объявляет `target-version = "py311"`, `Dockerfile:2` использует `python:3.11-slim`. На проде установлен **Python 3.9** (Debian 11 default). Любой код, использующий py3.10+ синтаксис (например, `match`, `X | Y` union types, parameter specifications), на проде упадёт.

### Git state

```
cwd:        /opt/wb-irrigation/irrigation/   (symlink → /mnt/data/wb-irrigation)
HEAD:       e37adb76a18e8e66564994d405a1035356028bf8
BRANCH:     refactor/v2
remote:     origin → https://github.com/Raul-1996/irrigation.git
```

Last 10 коммитов (соответствует локальному `git log` минус scaffold):

```
e37adb7 fix: watchdog forward reference — use lambda for lazy binding
4ac4398 security: harden rate-limiting — password endpoint + SSE connection limit
b899a5d fix: resolve circular import app.py <-> services/app_init.py
cacaa12 bump: version 2.0.183 — correct version numbering
bd6213e bump: version 2.0.4 — full v2 refactoring milestone
036ca2e fix: exempt API blueprints from CSRF — guests blocked by token mismatch
f041c50 fix: restore guest access to zone/group control (behind nginx basic auth)
3f7fc51 refactor: phase 2+3 — security fixes, backend + frontend decomposition
791ff0e refactor: full project audit — fix bugs, cleanup dead code, extract CSS
aaaebb4 revert: rollback to v4.2.1 - remove timer/DOM patching changes
```

`git status --short`:
```
?? backups
```
(только untracked symlink на `/mnt/data/irrigation-backups`)

> **Совпадение с локалью:** прод HEAD `e37adb7` = ровно тот, что указан в задании. Локально я работаю в `6a47153` — тот же коммит + scaffold каталога `irrigation-audit/` (один коммит сверху, без изменений в коде).

### Symlinks (важные)

```
/opt/wb-irrigation/irrigation       → /mnt/data/wb-irrigation         (весь код)
/opt/wb-irrigation/irrigation/backups → /mnt/data/irrigation-backups  (бэкапы)
/etc/mosquitto/passwd               → /mnt/data/etc/mosquitto/passwd  (есть, но не активен в effective config!)
/etc/mosquitto/acl                  → /mnt/data/etc/mosquitto/acl     (есть, но не активен)
/etc/mosquitto/conf.d/00default_listener.conf → /mnt/data/etc/mosquitto/conf.d/00default_listener.conf
/etc/mosquitto/conf.d/20bridges.conf → /mnt/data/etc/mosquitto/conf.d/20bridges.conf
```

---

## 3. Systemd: `wb-irrigation.service`

```
Loaded:     /etc/systemd/system/wb-irrigation.service; enabled
Active:     active (running) since Sun 2026-04-05 23:31:03 +06; 1 weeks 6 days ago  (=13d 16h)
Main PID:   2989014 (python)
Tasks:      15 (limit: 4406)
Memory:     224.8M
CPU time:   54min 36s (за 13.5 дней)
ExecStart:  /opt/wb-irrigation/irrigation/venv/bin/python run.py
```

### Unit-файл (точный текст с прода)

```ini
[Unit]
Description=WB-Irrigation Flask app
After=network-online.target mosquitto.service
Wants=network-online.target
Requires=mosquitto.service

[Service]
Type=simple
WorkingDirectory=/opt/wb-irrigation/irrigation
Environment=TESTING=0
Environment=UI_THEME=auto
ExecStart=/opt/wb-irrigation/irrigation/venv/bin/python run.py
TimeoutStopSec=20
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Совпадает с `wb-irrigation.service` в репо. **Замечание:** нет `EnvironmentFile=`, нет `User=`/`Group=` (бежит от root), нет `LimitNOFILE`, нет `MemoryMax`/`CPUQuota`.

### Process detail

```
PID     PPID    ELAPSED    RSS     VSZ     %CPU %MEM  CMD
2989014 1       13-16:51:31 246700 1840448  0.2  6.1  /opt/wb-irrigation/irrigation/venv/bin/python run.py
```

RSS ~241 MB, рост стабилен (за 13 дней), нет признаков memory leak.

### journalctl: первые наблюдения

`journalctl --no-pager -n 200` показал **исключительно одну строку** каждую минуту:

```
{"timestamp": "2026-04-19T13:03:01", "level": "INFO", "module": "routes.system_status_api",
 "message": "api_status: temp=None hum=None temp_enabled=False hum_enabled=False"}
```

— 200 одинаковых INFO-строк подряд. journal начинается с `2026-04-16 10:42:01` (rotation cap) и заканчивается текущим временем. Это означает:
- Никаких WARN/ERROR за последние ~3 дня в системном journal
- Polling `/api/status` (вероятно с дашборда) каждую минуту, который вызывает `routes.system_status_api` логирующий `temp=None hum=None`
- env_monitor отключён (`temp_enabled=False`, `hum_enabled=False`) — соответствует `bot_users=0`, weather_log=0 в БД (полная фича env-сенсоров не настроена)

---

## 4. MQTT-брокер (`mosquitto.service`)

```
Loaded:    /lib/systemd/system/mosquitto.service; enabled
Drop-in:   /etc/systemd/system/mosquitto.service.d/override.conf  (содержимое не дампилось)
Active:    active (running) since Sun 2026-04-05 23:31:03 +06   (тоже 13.5 дней — стартовали одновременно)
Main PID:  2989013
Memory:    8.0M
ExecStart: /usr/sbin/mosquitto -c /etc/mosquitto/mosquitto.conf
Version:   mosquitto 2.0.20
```

### Effective config (на проде, после include_dir)

`/etc/mosquitto/mosquitto.conf` — **дефолтный wirenboard**, ссылается на:
```
include_dir /usr/share/wb-configs/mosquitto
include_dir /etc/mosquitto/conf.d
include_dir /usr/share/wb-configs/mosquitto-post
```
(содержимое `wb-configs` не дампилось — Phase 2 может сюда смотреть).

`/etc/mosquitto/conf.d/00default_listener.conf` (через симлинк):
```
per_listener_settings true
listener 0 /var/run/mosquitto/mosquitto.sock
allow_anonymous true
```

`/etc/mosquitto/conf.d/10listeners.conf`:
```
listener 1883
allow_anonymous true

listener 18883 0.0.0.0
protocol websockets
allow_anonymous true
```

`/etc/mosquitto/conf.d/20bridges.conf` — пустой.

> **КРИТИЧНО:** все три effective listener'а (unix-socket, TCP 1883, websocket 18883) имеют **`allow_anonymous true`**. При этом `/etc/mosquitto/passwd` и `/etc/mosquitto/acl` существуют (симлинки на `/mnt/data/etc/mosquitto/`), но **не подключены** через `password_file`/`acl_file` директивы. Файл `mosquitto.conf` в репо проекта (`mosquitto.conf:1-7`) объявляет `allow_anonymous false` + `password_file /mosquitto/config/passwd` + `acl_file /mosquitto/config/acl` — но это **только для docker-варианта** (`docker-compose.yml:16-18`). На bare-metal проде применяется wirenboard'овский default.
>
> Следствие: брокер открыт на 0.0.0.0:1883 (см. §6) без auth. Любой в LAN может писать в любые топики, включая управление клапанами. Telegram-бот, web-UI и MQTT-published values — все идут через этого брокера.

### Broker stats (live)

```
$SYS/broker/version           → mosquitto version 2.0.20
$SYS/broker/clients/connected → 18
$SYS/broker/uptime            → 1183886 seconds  (≈13.7 дней)
```

Anonymous подписка `mosquitto_sub` сработала — подтверждает **нулевую auth**.

### Дополнительные mosquitto-listeners (loopback only — Wirenboard internal)

```
*:18884 [::ffff:127.0.0.1]   ← internal client
*:18885 [::ffff:127.0.0.1]
*:18886 [::ffff:127.0.0.1]
```

---

## 5. Cloudflare Tunnel (`cloudflared-poliv.service`)

```
Active:    active (running) since Thu 2026-04-02 14:57:33 +06; 2 weeks 3 days ago
PID:       2452 (cloudflared)
Memory:    56.6M
ExecStart: /usr/bin/cloudflared tunnel --no-autoupdate --config /etc/cloudflared/config-poliv.yml run
```

Config `/etc/cloudflared/config-poliv.yml`:
```yaml
tunnel: 6b24575b-b98a-41e4-8478-a71731d03abe
no-autoupdate: true

ingress:
  - hostname: poliv-kg.ops-lab.dev
    service: http://localhost:8080      ← напрямую на Flask app, минуя basic_auth_proxy
  - service: http_status:404
```

Файл `credentials-poliv.json` присутствует, не открывали.

> **Замечание:** ingress направлен напрямую в `localhost:8080` (Flask app), а **не** через `basic_auth_proxy.py` (который слушает `127.0.0.1:8011`, см. §6). Значит публичный домен `poliv-kg.ops-lab.dev` **не имеет basic-auth в front'е**. Проверка авторизации полагается исключительно на Flask `services.security` (login session).

---

## 6. Network — listening sockets (`ss -tlnp`)

| Local | Process | Назначение |
|---|---|---|
| `127.0.0.1:8011` | python3 (pid=1709) | вероятно `basic_auth_proxy.py` (`UPSTREAM_PORT=8080`, `LISTEN_PORT` стандартно 8081, но здесь 8011 — может быть кастомный) |
| `127.0.0.1:20241` | cloudflared | внутренний metrics |
| `0.0.0.0:22` | sshd | SSH |
| `0.0.0.0:80` | nginx (5 worker) | HTTP — вероятно reverse-proxy с basic-auth |
| `192.168.42.1:53` | dnsmasq | LAN DNS |
| `0.0.0.0:8042` | nginx | вероятно основной HTTP listener |
| `127.0.0.1:9090` | wb-rules | wirenboard wb-rules engine |
| `0.0.0.0:1883` | mosquitto | **MQTT plaintext, anonymous, ALL interfaces** |
| `0.0.0.0:8080` | python (pid=2989014) | **Flask app, ALL interfaces** |
| `*:6720` | knxd | KNX bus (wirenboard) |
| `*:18883` | mosquitto | **MQTT websocket, anonymous, ALL interfaces** |
| `[::ffff:127.0.0.1]:18884/85/86` | mosquitto | internal listeners |

> **Открытые порты в LAN (0.0.0.0):** SSH(22), HTTP nginx(80, 8042), MQTT(1883), Flask(8080), MQTT-WS(18883), KNX(6720). **Flask app и MQTT-брокер слушают ВСЕ интерфейсы** — доступны напрямую из LAN без nginx-front.

---

## 7. SQLite БД

### Файлы

```
/opt/wb-irrigation/irrigation/irrigation.db          1,183,744 bytes  (Apr 19 04:36)
/opt/wb-irrigation/irrigation/irrigation.db.bak.20260401_124241  1,011,712 bytes (Apr 1)
/mnt/data/wb-irrigation/irrigation.db                1,183,744 bytes  (тот же файл — symlink)
/mnt/data/wb-irrigation/irrigation.db.bak.20260401_124241        1,011,712 bytes
```

**WAL/SHM:** при снапшоте отсутствуют (значит между WAL checkpoint'ами). Размер БД ≈ 1.16 МБ.

### PRAGMA

```
journal_mode    = wal
foreign_keys    = 0           ← FK отключены на этой connection (sqlite3 включает per-connection)
synchronous     = 2           (FULL — нестандарт; миграции просили NORMAL)
page_count      = 289
page_size       = 4096
integrity_check = ok
```

> **Расхождение:** `db/migrations.py:21-26` устанавливает `PRAGMA synchronous=NORMAL`, но через ad-hoc sqlite3 connection видим `synchronous=2 (FULL)`. PRAGMA `synchronous` — connection-уровневая или persistent? В sqlite — устанавливается на каждом соединении заново, поэтому effective значение зависит от того, какое connection использует app. Для read-only audit это нормально.

### Таблицы (schema из prod)

19 таблиц + индексы. Главные:

| Таблица | rows | Назначение |
|---|---:|---|
| `migrations` | 35 | applied migration names (соответствует 35 `_apply_named_migration` в `db/migrations.py`) |
| `mqtt_servers` | 1 | один броkerзаписан (host=127.0.0.1, port=1883, TLS=0) |
| `zones` | 24 | 24 зоны полива (с водяными статами, fault tracking, postpone) |
| `groups` | 2 | 2 группы зон (с master valve, rain sensor, water meter, float sensor settings) |
| `programs` | 1 | 1 программа полива (с расширенным расписанием v2: type, schedule_type, color, enabled, extra_times) |
| `settings` | 22 | 22 ключа (password_hash, telegram_token, system_name, weather settings, и т.д.) |
| `logs` | **8339** | append-only event-лог (ввод данных, действия) |
| `program_cancellations` | 5 | отмены программ |
| `program_queue_log` | 0 | очередь программ — ни разу не использовалась |
| `zone_runs` | 0 | детальный учёт каждого запуска зоны (start/end UTC, raw pulses, total liters, status) — **пустая** (фича есть, не пишется) |
| `water_usage` | 0 | старая таблица учёта воды — **пустая** |
| `weather_cache` | 1 | один Open-Meteo response в кеше |
| `weather_decisions` | 0 | weather-based decisions audit — **пустая** |
| `weather_log` | 0 | weather adjustment per-zone — **пустая** |
| `bot_users` | 0 | Telegram users — **никто не зарегистрирован** |
| `bot_subscriptions` | 0 | — |
| `bot_audit` | 0 | — |
| `bot_idempotency` | 0 | — |
| `float_events` | 0 | поплавковые события — нет данных |

> **Главное наблюдение:** все «новые» v2 таблицы (`zone_runs`, `weather_decisions`, `program_queue_log`, `float_events`) — **пустые**. Либо фичи не активированы в settings, либо не работают. Это противоречит факту, что приложение работает 13.5 дней с 24-мя зонами и 1-й активной программой — значит запуски были, но в новые таблицы не пишутся.

> **Также:** `bot_users=0` — Telegram-бот объявлен в коде, импортируется при старте (`app.py:65-71`), но никто не зарегистрирован → проверь Phase 2: бот реально работает или включается conditional'но через settings.

### Schema highlights (полная схема — в prod-snapshot.txt)

`zones` — 28 колонок, добавленных через миграции: `state`, `name`, `icon`, `duration`, `group_id`, `topic`, `postpone_until`, `postpone_reason`, `photo_path`, `watering_start_time`, `scheduled_start_time`, `last_watering_time`, `mqtt_server_id`, `watering_start_source`, `planned_end_time`, `sequence_id`, `command_id`, `version`, `commanded_state`, `observed_state`, `last_avg_flow_lpm`, `last_total_liters`, `last_fault`, `fault_count`, `pause_remaining_seconds`, `pause_reason` + `created_at`, `updated_at`. Версионирование (`version`) и idempotency (`sequence_id`, `command_id`) встроены.

`groups` — поддержка master valve (с `master_mode` NO/NC), pressure sensor, water meter (pulse-based), float sensor (NO/NC, timeout, debounce).

`mqtt_servers` — TLS columns (`tls_enabled`, `tls_ca_path`, `tls_cert_path`, `tls_key_path`, `tls_insecure`, `tls_version`).

---

## 8. Логи приложения

```
/opt/wb-irrigation/irrigation/services/logs/
   telegram.txt                            520,880 bytes  (Apr 5 23:31 — last write при старте)
                                           Total: 516 KB

/opt/wb-irrigation/irrigation/backups/  (= /mnt/data/irrigation-backups/)
   app.log                                 0 bytes        (Mar 29 13:13 — пустой, BUGS-REPORT bug #4 ✓)
   import-export.log                       440 bytes      (Apr 3 19:57)
   import-export.log.2026-03-29..04-01     ротация по дате — работает
   irrigation_backup_20260403_185606.db    221,184 bytes  (последний DB-backup от 03 апреля!)
                                           Total: 1.6 MB
```

> **Подтверждено:**
> - Bug #4 «`app.log` пустой 0 байт» — есть; за 13+ дней работы ничего не записалось.
> - Telegram log — единичный 520 KB файл без ротации.
> - `import-export.log` имеет date-based rotation (работает).
> - **Последний DB-бэкап — 3 апреля 2026** (16 дней назад). За это время БД заметно выросла. Cron на бэкапы не настроен или не выполняется (Phase 2 проверить crontab).

---

## 9. venv / pip freeze (полный список — 50 пакетов)

Ключевые версии установлены:

| Пакет | Установлен | Совпадает с requirements.txt? |
|---|---|---|
| `Flask` | 3.1.3 | да (>=3.1.0) |
| `Flask-WTF` | 1.2.1 | да |
| `flask-sock` | 0.7.0 | да |
| `Pillow` | 11.3.0 | да (>=10.3.0) |
| `APScheduler` | 3.10.4 | строго `==3.10.4` |
| `paho-mqtt` | 2.1.0 | строго `==2.1.0` |
| `hypercorn` | 0.14.4 | да |
| `aiogram` | 3.22.0 | да (>=3.8.0) |
| `pycryptodome` | 3.21.0 | да |
| `requests` | 2.32.3 | да (>=2.28) |
| `python-dotenv` | 1.0.1 | да |
| `pytest` | 7.4.3 | dev — установлен в prod venv |
| `pydantic` | 2.5.3 | (косвенная dep aiogram) |

> **Замечание:** в prod venv установлен `pytest==7.4.3` — dev-зависимость утекла в продакшн venv (вероятно через `pip install -r requirements-dev.txt` в `update_server.sh:114-117`).
>
> **Отсутствуют:** `SQLAlchemy` (нет в pip freeze, но в `requirements-dev.txt`); `selenium`; `webdriver-manager`; `PyJWT`. То есть APScheduler использует **MemoryJobStore** fallback, а не SQLAlchemyJobStore (см. `irrigation_scheduler.py:21-29`). Это означает: при рестарте app **все scheduled jobs теряются** и восстанавливаются boot-init'ом из БД через `init_scheduler()`.

---

## 10. Обзор `/opt/wb-irrigation/`

```
drwxr-sr-x 3 root root 4096 Apr  5 21:19 .
drwxr-sr-x 3 root root 4096 Mar 28 22:34 ..
drwxr-sr-x 3 root root 4096 Mar 29 20:52 backups
-rwxr-xr-x 1 root root 2720 Mar 31 07:35 basic_auth_proxy.py
lrwxrwxrwx 1 root root   23 Apr  5 21:19 irrigation -> /mnt/data/wb-irrigation
```

`basic_auth_proxy.py` лежит на уровне `/opt/wb-irrigation/`, не внутри `irrigation/`. Запущен из этого расположения (см. процесс на 127.0.0.1:8011).

`/mnt/data/`:
```
etc/                  ← переопределённые конфиги (mosquitto)
irrigation-backups/   ← target симлинка backups
root/
uploads/
var/
wb-irrigation/        ← target симлинка irrigation/ (исходный код)
```

---

## 11. Permissions / ownership ключевых файлов

```
.irrig_secret_key   -rw-------  1001:1001  32 bytes   (правильно: 600, owner=app user)
irrigation.db       -rw-r--r--  root:root  1.16 MB    (mode 644 — world-readable!)
app.py              -rw-r--r--  root:root             (root-owned)
.git/               drwxr-xr-x  root:root             (root-owned, не app user)
```

> **Замечание:** Большинство файлов owned by root (uid 0), но app может бежать от root (см. unit-файл — нет `User=`). Симлинк `services/logs/`, `backups` — app-user (1001:1001). Несмешанная ownership увеличивает риск permission errors при автообновлениях.

---

## 12. Сводка прод-снапшота

| Параметр | Значение |
|---|---|
| ОС | Debian 11 bullseye, kernel 6.8.0-wb153, aarch64 |
| Python | 3.9.2 (несовместим с pyproject.toml `py311` target) |
| App | wb-irrigation, hypercorn, port 8080 (0.0.0.0) |
| Git | branch `refactor/v2`, HEAD `e37adb7`, чисто |
| Uptime app | 13d 16h |
| Mosquitto | 2.0.20, **anonymous=true**, port 1883 (0.0.0.0), 18 connected clients |
| Cloudflared | tunnel `6b24575b...`, hostname `poliv-kg.ops-lab.dev` → `localhost:8080` |
| nginx | listening 80, 8042 (basic-auth proxy?) |
| BD | sqlite3 1.16 MB, 19 таблиц, integrity OK, WAL включён |
| Active scheduler jobstore | **MemoryJobStore** (SQLAlchemy не установлен) — jobs теряются при рестарте |
| Telegram | bot активирован в коде, **bot_users=0** — никто не подписан |
| Backups | последний DB-бэкап **3 апреля** (16 дней назад) — нет автоматики |
| app.log | 0 bytes за 13+ дней (Bug #4 подтверждён) |
