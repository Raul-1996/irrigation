# DevOps / Infra Audit — wb-irrigation (2026-05-09)

**Auditor:** DevOps Automator (READ-ONLY)
**Scope:** install/update/backup/monitoring scripts, Dockerfile, systemd unit, MQTT config, Cloudflare perimeter, migration runner.
**Контекст:** single-tenant продукт на Wirenboard 8 (ARM, Debian 11, ~1GB RAM). Готовится к коммерциализации.
**Не проверялось вживую:** SSH на 10.2.5.244 запрещён скоупом — отчёт по содержимому репозитория и `devops/servers/wb-techpom.yaml`.

---

## Находки

### [P0] Захардкоженный дефолтный пароль `1234` без принудительной смены
**File:** `db/migrations.py:195,208` — `generate_password_hash('1234', method='pbkdf2:sha256:120000')`
**Описание:** На каждой свежей установке (и после восстановления БД на новом контроллере) единственный admin-аккаунт получает пароль `1234`. Сменa пароля не форсируется UI.
**Risk for commercial deployment:** Любой клиент, не сменивший пароль — открыт всем, кто знает /login (а это любой в LAN, плюс все за Cloudflare Worker). Риск умножается на количество клиентов: 50 контроллеров = 50 потенциальных backdoor.
**Recommendation:** При first-boot генерировать рандомный пароль и (а) выводить в systemd journal + (б) форсить смену при первом логине. Альтернатива — required env `WB_INITIAL_ADMIN_PASS` в `/opt/wb-irrigation/.env`, иначе сервис не стартует.

### [P0] Cloudflare Worker — креды захардкожены и одинаковы для всех клиентов
**File:** `devops/cloudflare/workers/wb-irrigation-auth.js:13-17`
**Описание:** `BASIC_USER='Poliv'`, `BASIC_PASS='Poliv'`, HMAC `SECRET` — литералы в коде. Один и тот же Worker по всему домену `*.ops-lab.dev` (или per-route в будущем) = одни креды на всех клиентов. Cookie живёт 90 дней.
**Risk for commercial deployment:**
- Креды утекают в git/логах CF/любом скриншоте → скомпрометировано всё разом, ротация требует одновременного редеплоя на всех клиентах и инвалидации всех cookie (UX-регрессия для всех).
- При увольнении сотрудника поддержки нет способа отозвать только его доступ.
- `Poliv:Poliv` — энтропия 0, brute-force тривиален. WAF от Cloudflare на free-tier не помогает.
**Recommendation:** На клиента — отдельный Worker namespace или per-customer creds в Workers Secrets / KV. Лучше — выкинуть Basic Auth и поставить Cloudflare Access (Zero Trust, OIDC/email) — он бесплатен до 50 пользователей и решает обе проблемы. Минимум до фикса в коде (VULN-001) — поднять энтропию пароля до 32+ chars.

### [P0] Нет идентификации контроллера / клиента в коде
**File:** `services/version.py`, `irrigation_scheduler.py`, нет `customer_id` / `controller_id` ни в `.env`, ни в БД.
**Описание:** В коде ничто не говорит "это контроллер №X у клиента Y". Версия — git sha, и всё. Логи и метрики (Prometheus `wb_build_info`) одинаковые на всех контроллерах.
**Risk for commercial deployment:** Без `controller_id` невозможен централизованный сбор метрик/алертов (нельзя отличить упавший контроллер клиента А от контроллера клиента Б), невозможен таргетированный rollout/rollback, и в саппорте по логу не понять чей это инцидент.
**Recommendation:** Поле `CONTROLLER_ID` (UUID, сгенерированный на установке) и `CUSTOMER_ID` (введённый при инсталле) в `/opt/wb-irrigation/.env` → подмешивать в Prometheus labels, в JSON-логи (через `WBJsonFormatter`), в Telegram alerts.

### [P0] Atomicity update_server.sh — нет
**File:** `update_server.sh:85-167`
**Описание:** Скрипт делает `git reset --hard origin/main` → `pip install` → `systemctl restart`. Если **миграция БД** упадёт после рестарта (а миграции применяются в `MigrationRunner.init_database()` при старте Flask) — старая БД уже изменена частично, новый код не работает, старый код тоже не вернётся (БД мигрирована).
- Бэкап БД делается ДО кода — отлично. Но restore **руками** (нет `--rollback` флага в скрипте).
- Нет verification, что новые миграции имеют `_down_*` метод. По `DOWNGRADE_REGISTRY` — только 15 миграций имеют downgrade, в `init_database()` зарегистрировано ~20+ named migrations. Часть необратима без warning.
- `git reset --hard` уничтожает локальные правки (это норма для CI, но рискованно для саппорта, который мог `vim` что-то на проде "временно").
**Risk for commercial deployment:** Сломанный апдейт = ручная работа техника на месте (или SSH через Cloudflare tunnel) для каждого клиента. На 50 контроллерах один кривой релиз = неделя пожара.
**Recommendation:**
1. `update_server.sh` должен делать `pre-migration backup → migrate dry-run → миграция → код → restart → smoke → если smoke упал, авто-rollback DB+code из backup`.
2. CI-проверка: для каждой новой миграции должен существовать `_down_*` (либо явный `@no_downgrade` декоратор с обоснованием).
3. Добавить флаг `--rollback-to <stamp>` в `update_server.sh`.

### [P0] Backups — только на самом контроллере, нет offsite
**File:** `update_server.sh:62-82`, `db/logs.py:160-185`
**Описание:**
- Backup делается в `/opt/wb-irrigation/backups/<stamp>/` на том же диске (eMMC контроллера).
- При смерти eMMC (известная проблема Wirenboard, особенно когда логи активно пишутся) — backup умирает вместе с продом.
- В `update_docker.sh` бэкап ещё проще — только `irrigation.db` копируется в `./backups/`, ни кода, ни конфига.
- Restore-процедура в `DEPLOY-INSTRUCTIONS-POST-CONSOLIDATION.md:190-214` — ручная, на 25 строк, ни разу не тестировалась автоматизированно.
- `jobs.db` (APScheduler state) **не бэкапится** ни в `update_server.sh`, ни в `update_docker.sh` — потеряются все pending one-shot jobs (`zone_stop:N`, `master_close`).
**Risk for commercial deployment:** Один неудачный апдейт + умершая SD-карта = потеря данных клиента (программ полива, настроек MQTT, истории). Restore "одной командой" не существует.
**Recommendation:**
1. Скрипт `pull-backup.sh` на VPS-EU/Mailcow (cron daily): `rsync ... 10.2.5.244:/opt/wb-irrigation/backups/latest/ ./backups/<customer>/`. По SSH-ключу через Cloudflare Tunnel или ZeroTier.
2. Включить `jobs.db` и `.env`/`.secret_key`/`.irrig_secret_key` в backup tarball.
3. Скрипт `restore-controller.sh <backup.tar.gz>` — одна команда, идемпотентно поднимает полный stack.

### [P1] Нет автоматизации множественной установки (multi-controller)
**File:** Нет Ansible playbook / Salt / centralized config-management.
**Описание:** На сегодня "установить новому клиенту" = SSH на железку, `sudo bash install_wb.sh`, потом руками настроить MQTT-сервер, Telegram bot token, Cloudflare tunnel, watchdog cron. По примерным прикидкам — 30-60 минут на контроллер, plus DNS/Cloudflare per-customer.
**Risk for commercial deployment:** Сейчас 1 клиент. На 5 — техдолг управляем, на 20 — поддержка превратится в полную занятость одного человека.
**Recommendation:** Ansible playbook `roles/wb-irrigation/` с переменными `customer_id`, `controller_id`, `mqtt_topic_prefix`, `cf_tunnel_token`, `telegram_bot_token`. CI-build .deb-пакета с зависимостями (без `pip install` на проде).

### [P1] Watchdog cron не управляется самим пакетом
**File:** `scripts/watchdog.sh` + cron entry. В `install_wb.sh` cron не устанавливается.
**Описание:** `install_wb.sh` создаёт systemd unit, но **не** добавляет crontab `* * * * * watchdog.sh`. Это надо делать руками. И в `uninstall_wb.sh` он, соответственно, не убирается. У systemd-unit уже есть `WatchdogSec=60` + sd_notify — это перекрывает половину функционала watchdog.sh (ловит зависший event loop). Дублирование + ручной шаг = риск, что на новом контроллере watchdog забыли поставить.
**Risk for commercial deployment:** На одном клиенте watchdog есть, на другом нет — диагностика расходится. Когда unit упадёт по `WatchdogSec`, будут разные траектории: на одном просто рестарт, на другом ещё и cron-watchdog добивает.
**Recommendation:** Решить: `WatchdogSec` (через sd_notify) **или** cron+`scripts/watchdog.sh`, но не оба. Я бы оставил `WatchdogSec` (он уже работает, ловит wedged event loop), а `scripts/watchdog.sh` удалил. Тогда `install_wb.sh`/`uninstall_wb.sh` не должны знать о cron вообще.

### [P1] Нет ретеншена для `logs` и `water_usage` таблиц
**File:** `db/logs.py:20-60` — есть `add_log()`, нет `cleanup_old_logs()`. `audit_log` cleanup есть (`db/audit.py:176`, scheduled job daily 03:30), а `logs` и `water_usage` — нет.
**Описание:** Каждое включение зоны → запись в `logs`. Каждый цикл полива → запись в `water_usage`. Год работы = десятки тысяч строк. SQLite справится, но БД будет расти, бэкапы — раздуваться, восстановление — медленнее.
**Risk for commercial deployment:** Через 2-3 года эксплуатации БД может перевалить за сотни мегабайт на eMMC, где места и так мало (на текущем 5.6MB БД + 17MB старых логов в `backups/`).
**Recommendation:** Добавить такой же scheduled job, что и `job_audit_cleanup`, для `logs` (retention 90 дней) и `water_usage` (retention 1 год — нужно для статистики). Конфигурируемо через settings.

### [P1] Метрики наружу — есть, но без auth и без удалённой агрегации
**File:** `routes/health_api.py:385` — `/metrics` без auth (в коде комментарий: "Wave 3 (MASTER-M5): nginx IP allow-list").
**Описание:**
- Эндпоинт `/metrics` (Prometheus exposition) есть и считает `wb_zones_state`, `wb_scheduler_jobs`, `wb_readyz_check_status`, `wb_build_info` и т.д. — это плюс.
- Но: (а) без auth — кто угодно из LAN читает метрики, (б) **некуда** их собирать. Zabbix-сервер `10.10.61.96` для контроллеров на 10.2.5.0/24 недоступен напрямую без VPN.
- На `wb-cloud-agent-telegraf@wirenboard.cloud` шлёт метрики в Wirenboard Cloud — это про железо, не про irrigation app.
**Risk for commercial deployment:** Невозможно мониторить парк контроллеров централизованно. Узнаём о падении сервиса от клиента, не от Prometheus.
**Recommendation:** Prometheus push gateway на VPS-EU + cloudflared tunnel/ZeroTier reverse-туннель из контроллера. Или telegraf scrape `/metrics` → InfluxDB Cloud free tier. И auth на `/metrics` (Bearer token из `.env`, разный per-customer).

### [P1] `mosquitto.conf` (для Docker деплоя) и реальный `wb-mqtt-serial` config — две разных вселенных
**File:** `mosquitto.conf` (для docker-compose) vs `/etc/wb-mqtt-serial.conf` (реальный wirenboard).
**Описание:** Docker-deploy запускает СВОЙ mosquitto на :1884 с `passwd`/`acl` файлами. На реальном Wirenboard (`install_wb.sh`) mosquitto не настраивается вообще — приложение коннектится к встроенному `mosquitto` (anonymous, port 1883). Файлы `mosquitto/passwd`, `mosquitto/acl`, `setup_auth.sh` — для Docker-сценария.
**Risk for commercial deployment:** Два деплой-пути (Docker и native) расходятся в безопасности MQTT. Native — anonymous broker, любой в LAN может публиковать на `/devices/#`. Это и сейчас "проблема", но безопасность WB в LAN — данность.
**Recommendation:** Зафиксировать решение: Docker-путь (`install_docker.sh`, `update_docker.sh`, `docker-compose.yml`) или native-путь (`install_wb.sh`, `update_server.sh`, systemd). Если native — удалить Docker-файлы или переместить в `legacy/`. Это снимет половину противоречий в документации (см. P2 ниже).

### [P1] GitHub Actions deploy.yml использует password-SSH через jump host
**File:** `.github/workflows/deploy.yml:14-32` — `password: ${{ secrets.WB_PASSWORD }}` + `proxy_key: ${{ secrets.JUMP_SSH_KEY }}`.
**Описание:** SSH к контроллеру по паролю (а не ключу) — bad practice. На проде root-пароль в GitHub Secrets, на контроллере открытый SSH с `PasswordAuthentication yes`. Секреты в repo `Raul-1996/irrigation` — публичность не проверял.
**Risk for commercial deployment:** Утёкший GitHub PAT/access к repo = root на контроллере клиента. Чтобы добавить второго клиента, нужны его WB_PASSWORD/WB_HOST в secrets — не масштабируется.
**Recommendation:** SSH-key only (запретить password-auth на контроллерах). Деплой через self-hosted runner на VPS-EU с per-customer SSH-ключом, или вообще через "pull"-модель — контроллер сам ходит на GitHub раз в N минут и ставит новый тег если есть.

### [P1] Cloudflare tunnel и LAN-доступ — нет ясной разграниченной аутентификации
**File:** `wb-irrigation-auth.js`, `app.py:341` (`_ALLOWED_PUBLIC_POSTS`).
**Описание:** Cloudflare Worker блокирует анонимный `POST /api/zones/X/start` (до фикса VULN-001 в коде). Но **по LAN** (10.2.5.244:8080) Worker не работает — приложение пускает гостя по своим правилам. Пометки в README прямо говорят "LAN-исключение: автоматическое".
**Risk for commercial deployment:** Wi-Fi у клиента → доступ к управлению поливом без авторизации. У домашнего клиента — не катастрофа. У коммерческого (теплицы, агрохолдинг) с гостевыми Wi-Fi — реальный риск.
**Recommendation:** Фикс VULN-001 в коде (упомянут в коде). До фикса — приложение должно слушать только 127.0.0.1, и nginx/cloudflared единственный путь. Сейчас в `run.py:69` — `bind = ["0.0.0.0:8080"]`, что плохо.

### [P2] Документация — три разных deploy doc, частично устаревшие
**Files:** `DEPLOY-DOCKER.md`, `DEPLOY-INSTRUCTIONS-POST-CONSOLIDATION.md`, `NEXT-AGENT-PROMPT.md`, `README.md`, `README-LONG.md`, `README-SHORT.md`, `MEMORY.md`.
**Описание:**
- `DEPLOY-DOCKER.md` (40 строк) — описывает Docker-путь.
- `DEPLOY-INSTRUCTIONS-POST-CONSOLIDATION.md` — runbook для одноразовой консолидации `refactor/v2 → main` (2026-04-19), уже legacy.
- `NEXT-AGENT-PROMPT.md` — промпт для ИИ, не для человека-эксплуатанта.
- Три README. `README-SHORT.md` 1.4KB, `README.md` 22KB, `README-LONG.md` 7.6KB — порядок чтения непонятен.
- Нет единого `OPERATIONS.md` / `RUNBOOK.md` для саппорта: "что делать если сервис не отвечает", "как сменить MQTT broker", "как вернуть забытый пароль".
**Risk for commercial deployment:** Новый саппортер не знает откуда читать. Каждое обращение в поддержку = индивидуальный quest по репозиторию.
**Recommendation:** Один `RUNBOOK.md` для саппорта, один `INSTALL.md` для нового клиента, один `UPDATE.md` для апдейтов. Остальное — переместить в `docs/legacy/`.

### [P2] Default fallback `SECRET_KEY=wb-irrigation-secret` в install_docker.sh
**File:** `install_docker.sh:20`
**Описание:** Если `SECRET_KEY` не задан в env — используется литерал `wb-irrigation-secret`. Это значит сессии на двух контроллерах с дефолтом — взаимозаменяемы. Native-деплой генерит `.secret_key`/`.irrig_secret_key` файл (видно в `ls -la`), Docker-деплой — нет.
**Risk for commercial deployment:** На контроллере с дефолтным SECRET_KEY валидная сессия одного клиента работает у другого. Совместно с одинаковыми Cloudflare-Worker creds = плавающая граница trust между инстансами.
**Recommendation:** В `install_docker.sh` генерить `SECRET_KEY` через `openssl rand -hex 32` и писать в `.env`. Удалить fallback-литерал.

### [P2] `start_mqtt.sh` — dev-скрипт для macOS, лежит в корне рядом с прод-скриптами
**File:** `start_mqtt.sh:1` — `#!/bin/zsh` + Homebrew install логика.
**Описание:** Это локальный девелоперский скрипт автора (Homebrew, /opt/homebrew, zsh). На Wirenboard (Debian, bash) он бесполезен и сбивает читателей. Лежит в корне репо, в `.gitignore` не вынесен.
**Recommendation:** Перенести в `tools/dev/` или `scripts/dev/`, либо удалить.

### [P2] `install.bat`, `start.bat`, `RUN_TESTS_V2.sh`, `start_tests.sh` — мусор из истории
**Files:** Корень репо.
**Описание:** Windows-скрипты для контроллера на ARM/Debian — никогда не запустятся. Тестовые скрипты (`RUN_TESTS_V2.sh`, `start_tests.sh`) — для CI / разработки, не для прод-деплоя.
**Recommendation:** Перенести в `tools/dev/` (для test-скриптов) или удалить (для .bat).

### [P2] `test_results_final.txt`, `test_results_batched/` закоммичены
**File:** Корень репо + `test_results_batched/`.
**Описание:** Результаты прогонов тестов в репе. Не CI-артефакты, не reproducible — просто снимок.
**Recommendation:** В `.gitignore`, удалить из репо.

### [P2] `irrigation.db`, `jobs.db` лежат в репозитории
**File:** Корень репо — `irrigation.db` (5.6MB), `jobs.db` (16KB).
**Описание:** Это **prod-БД на dev-машине** агента (где идёт аудит). Возможно случайный коммит истории dev-данных. В `.gitignore` нужно добавить `*.db`, `*.db-wal`, `*.db-shm`.
**Recommendation:** Проверить git log — если БД были в коммитах, выпилить через `git filter-repo` (но это write-операция, требует подтверждения Рауля).

### [P2] `migrations/versions/.gitkeep` — пустая директория
**File:** `migrations/versions/.gitkeep` (0 байт), `migrations/reencrypt_secrets.py`.
**Описание:** Похоже, кто-то планировал использовать Alembic (`migrations/versions/`), но реальный механизм миграций — самописный `db/migrations.py:MigrationRunner` (1267 строк, named migrations через таблицу `migrations`). Пустая директория сбивает с толку.
**Recommendation:** Решить: либо переезжать на Alembic (правильно для коммерции — есть нормальный CLI, autogen, downgrade), либо удалить `migrations/versions/`. `MigrationRunner` рабочий, но downgrade покрывает только 15 из 20+ миграций.

### [P2] Watchdog systemd внутри Docker не пробрасывается
**File:** `docker-compose.yml`, `Dockerfile`.
**Описание:** В native-деплое systemd `WatchdogSec=60` + sd_notify работает (через `services/systemd_notify.py`). В Docker — никакого watchdog нет (systemd внутри контейнера обычно не запускают, sd_notify в сторону хоста не идёт). Healthcheck в Dockerfile есть (`curl /` каждые 30с) — это меньше чем event-loop-watchdog.
**Risk for commercial deployment:** Если выбираем Docker-путь — теряем event-loop-watchdog, который сейчас единственный страхует от зависшего hypercorn.
**Recommendation:** Если оставляем Docker-путь — добавить sidecar healthcheck-контейнер или внешний cron, который рестартует контейнер по failed-counter. Если выбираем native — этого пункта нет.

### [P2] Restore процедуры не тестируются
**File:** `DEPLOY-INSTRUCTIONS-POST-CONSOLIDATION.md:190-214`, `db/logs.py:_cleanup_old_backups`.
**Описание:** Backup создаётся, restore документирован — но я не нашёл ни одного теста, что restore из backup действительно поднимает рабочую систему. Это слепая зона.
**Recommendation:** В CI добавить шаг `restore-test.sh`: взять backup, развернуть в контейнер, прогнать smoke-тесты на /readyz. Если зелёный — backup валиден.

### [P2] SSH ключи саппорта — нет процесса ротации, не покрыто документацией
**File:** Не нашёл политики.
**Описание:** Сейчас саппорт ходит на 10.2.5.244 через jump host (rauls-ubunte) с ключом `botops_key`. Если саппортёр уходит — нет процесса "выкатить новый authorized_keys на все контроллеры". Если у клиента 5 контроллеров и 3 саппортёра — это уже система, требующая процесса.
**Recommendation:** SSH CA (короткоживущие сертификаты) или Cloudflare Access for Infrastructure (SSH через CF Tunnel с per-user auth). Сейчас можно отложить, но в чеклист коммерции — добавить.

---

## Сводка
- **P0:** 5 (default password '1234'; CF Worker hardcoded creds; нет controller_id; non-atomic update + missing downgrades; backups только локально без offsite)
- **P1:** 7 (multi-controller automation; watchdog двойной механизм; нет retention для logs/water_usage; /metrics без auth и без агрегации; разногласия Docker vs native MQTT; password-SSH в GitHub Actions; LAN bypass auth)
- **P2:** 11 (документация-винегрет; default SECRET_KEY; dev-скрипты в корне; .bat файлы; test-результаты в репе; БД-файлы в репе; migrations/versions/ пустой; Docker без watchdog; restore не тестируется; SSH ротация; и т.д.)

**Топ-5 одной строкой:**
1. **Дефолтный пароль `1234` без принудительной смены** — каждый новый клиент уязвим из коробки.
2. **Cloudflare Worker `Poliv:Poliv` хардкод** — одни креды на всех клиентов, ротация невозможна без редеплоя везде.
3. **Нет offsite-бэкапа и `controller_id`** — один умерший eMMC = потеря данных клиента; нельзя отличить инциденты разных клиентов в логах/метриках.
4. **`update_server.sh` не атомарен** — сломанная миграция оставляет систему в полуапдейтнутом состоянии без авто-rollback.
5. **Нет инструмента массовой установки/апдейта (Ansible/.deb/pull-mode)** — на 10+ клиентах поддержка превращается в пожар.

---

## Что нужно для commercial release (high-level checklist)

### Безопасность (release-blocker)
- [ ] First-boot: рандомный admin-пароль + форсированная смена при первом логине; убрать `'1234'` из миграций
- [ ] Per-customer креды на Cloudflare периметре (Workers Secrets/KV или CF Access)
- [ ] Фикс `VULN-001` (LAN bypass) или bind на 127.0.0.1 (cloudflared/nginx — единственный путь)
- [ ] `/metrics` под bearer-token auth, токен per-customer в `.env`
- [ ] Запретить password-SSH на контроллерах, только key-based; deploy.yml перевести на ключи

### Установка/апдейт/откат (release-blocker)
- [ ] `controller_id` + `customer_id` в `.env` на установке; в логах/метриках/Telegram-алертах
- [ ] Atomic update_server.sh: pre-migration backup → migrate → smoke → авто-rollback при провале
- [ ] CI-проверка: каждая новая миграция имеет `_down_*` или явный `@no_downgrade`
- [ ] Один из путей: `.deb`-пакет (рекомендую) или Docker — не оба
- [ ] Ansible playbook (или эквивалент) для установки на N контроллеров одной командой
- [ ] Отдельный путь для bulk-update (с rolling deploy: 5%/25%/100% в неделю)

### Бэкапы (release-blocker)
- [ ] Offsite-бэкап (cron на VPS-EU pulls daily через CF Tunnel); включает `irrigation.db`, `jobs.db`, `.env`, `.secret_key`, `.irrig_secret_key`
- [ ] `restore-controller.sh <backup>` — одна команда поднимает stack
- [ ] CI-тест: `restore-test.sh` берёт сегодняшний backup → smoke-тест /readyz

### Мониторинг/саппорт
- [ ] Централизованная агрегация метрик/логов (Prometheus push gw на VPS-EU, или telegraf → InfluxDB Cloud)
- [ ] Алерт "контроллер X молчит >5 минут" → Telegram канал саппорта
- [ ] Retention для `logs` (90д) и `water_usage` (1г) через scheduler
- [ ] Один `RUNBOOK.md` для саппорта (топ-10 ситуаций: "сервис не отвечает", "сменить MQTT", "забыли пароль", и т.д.)
- [ ] Решить судьбу `scripts/watchdog.sh` (оставить только sd_notify, либо только cron)

### Гигиена репо/документации
- [ ] Удалить из репо: `*.db`, `test_results_final.txt`, `test_results_batched/`, `*.bat`, `start_mqtt.sh` (или перенести в `tools/dev/`)
- [ ] Удалить или переместить в `docs/legacy/`: `DEPLOY-INSTRUCTIONS-POST-CONSOLIDATION.md`, `NEXT-AGENT-PROMPT.md`, `EXPERT-ANALYSIS.html`, всё `*-REPORT.md` старше N месяцев
- [ ] Один README + один INSTALL.md + один UPDATE.md + один RUNBOOK.md
- [ ] `migrations/versions/.gitkeep` + `migrations/reencrypt_secrets.py` — решить: Alembic или удалить директорию

### Опционально, но желательно
- [ ] SSH CA или Cloudflare Access for Infrastructure (process для саппортёров)
- [ ] `.deb`-пакет с pre/post-install скриптами вместо `install_wb.sh` + bash
- [ ] Versioning по semver (сейчас `2.0.186` — counter с `v2-base`, не semver)
- [ ] Pull-режим обновлений (контроллер сам ходит на GitHub за новым тегом) вместо push через GH Actions
