# Security Audit — wb-irrigation (2026-05-09)

**Аудитор:** Security Engineer
**Скоуп:** Flask app `/opt/claude-agents/irrigation/` + Cloudflare Worker `/opt/claude-agents/devops/cloudflare/workers/wb-irrigation-auth.js`
**Контекст:** single-tenant (1 клиент = 1 инстанс), коммерциализация. Хост `poliv-kg.ops-lab.dev` за Cloudflare Worker (Basic Auth + HMAC cookie).
**Метод:** read-only static analysis. Базис сравнения — `SECURITY-REPORT.md` 2026-04-02.

---

## Статус прошлых VULN-001..005

### VULN-001 — Гостевой вход → управление клапанами
**Статус:** ⚠️ PARTIAL (архитектурно admit'нутo, не уязвимость в текущей threat-model)

`routes/auth.py:31-40` — `?guest=1` всё ещё устанавливает `role=viewer`. Перед логином идёт `_regenerate_session()` (auth.py:12-28), что закрывает session fixation (см. VULN-007).

`app.py:341-353` — `_ALLOWED_PUBLIC_POSTS` и `_ALLOWED_PUBLIC_PATTERNS` всё ещё разрешают всем (включая viewer/guest) дёргать start/stop/emergency без admin (`_is_status_action()`, app.py:355-361).

**Текущая модель:** периметр-контроль — Cloudflare Worker (Basic Auth `Poliv:Poliv`). Любой кто прошёл периметр считается доверенным «садовником». Это документированно в комментарии app.py:337-340 («Service runs behind nginx basic auth. Internal Flask auth … is unnecessary»).

**Что осталось:**
- Если перенести продукт к клиенту, у которого Cloudflare Worker не настроен — Flask голый. Нужно проверять deployment-precondition.
- Internal break-glass: emergency-resume по-прежнему доступен любому (см. VULN-012 в старом отчёте) — допустимо для fail-safe модели, но коммерческому клиенту вероятно надо разделить право включения после emergency.

---

### VULN-002 — Status Actions без admin
**Статус:** ⚠️ PARTIAL (то же, что VULN-001)

`app.py:355-361` `_is_status_action()` whitelist всё ещё содержит управление клапанами. Защищено только периметром Cloudflare Worker. Внутри Flask нет разделения «садовник vs админ» для физического действия.

**Файл:** `app.py:341-353` — список не сократился по сравнению с прошлым отчётом.

---

### VULN-003 — MQTT CRUD без auth
**Статус:** ✅ FIXED

`routes/mqtt_api.py:32,47,64,80,97,114,231` — все CRUD-функции теперь декорированы `@admin_required`. Также `app.py:425-427` — middleware `_require_admin_for_mutations` больше **не whitelist'ит** `/api/mqtt/` (явный комментарий «SECURITY FIX (VULN-003)»).

**Sanity-check:** `api_mqtt_status` (line 195) НЕ имеет `@admin_required` — но это GET и не возвращает credentials, только bool. Допустимо.

---

### VULN-004 — MQTT credentials в plaintext
**Статус:** ⚠️ PARTIAL

`routes/mqtt_api.py:38-40,72-73` — пароли маскируются как `'***'` в API-ответах. Хорошо.

**Что осталось:**
- В БД (`mqtt_servers.password`) пароли хранятся **в plaintext** (см. `db/mqtt.py:81-105` — никакого `encrypt_secret`/`decrypt_secret` при INSERT/UPDATE). См. P1-NEW-01 ниже.
- При компрометации `irrigation.db` (world-readable, см. P1-NEW-02) — все MQTT-пароли утекают в открытом виде.

---

### VULN-005 — basic_auth_proxy с admin/admin
**Статус:** ✅ FIXED

Файл `basic_auth_proxy.py` **удалён** из репозитория. Перенесено на Cloudflare Worker (`wb-irrigation-auth.js`). `.env.example:32-37` всё ещё упоминает `AUTH_USER=admin/AUTH_PASS=admin`, но эти переменные больше нигде не читаются — следы dead config (см. P2-NEW-09).

Worker имеет свои проблемы — см. P0-NEW-04, P1-NEW-05.

---

## Новые находки

### [P0] NEW-01 — MQTT credentials хранятся в БД в plaintext

**Severity:** Critical (для коммерческого single-tenant)
**File:line:** `db/mqtt.py:47-105` (create/update — нет шифрования) + `db/mqtt.py:25-39` (read возвращает plaintext password в memory)
**OWASP:** A02:2021 — Cryptographic Failures

**Описание:**
Таблица `mqtt_servers` хранит `username`, `password` в открытом виде. У вас уже есть `utils.encrypt_secret`/`decrypt_secret` (AES-256-GCM, ключ `.irrig_secret_key`) — он используется для `telegram_bot_token`, но не применён к MQTT credentials. API-маскировка `'***'` (VULN-004 fix) не помогает в случае компрометации `irrigation.db`.

**Impact:**
Для коммерческого продукта со SLA на конфиденциальность — backup БД, кража диска контроллера, доступ к bind-mount (`docker-compose.yml:14`) → утечка MQTT-кредов → прямой контроль над всеми реле минуя Flask.

**Remediation:**
```python
# db/mqtt.py — при create/update password
from utils import encrypt_secret, decrypt_secret
# INSERT: encrypt_secret(data['password'])
# SELECT: decrypt_secret(row['password']) перед использованием в paho.mqtt
```
Миграция: одноразовый скрипт `re-encrypt mqtt_servers.password` (можно через `db/migrations.py`).

---

### [P0] NEW-02 — `irrigation.db` world-readable + bind-mount наружу контейнера

**Severity:** Critical
**File:line:** `docker-compose.yml:14` (`./irrigation.db:/app/irrigation.db`) + `irrigation.db` permissions `0644` (host)
**OWASP:** A05:2021 — Security Misconfiguration

**Описание:**
БД содержит `password_hash` (admin), `telegram_bot_token_encrypted`, MQTT credentials (см. NEW-01), audit_log с PII (IP-адреса, действия). Файл доступен на чтение любому пользователю на хосте (`-rw-r--r--`). Bind-mount экспонирует его в обе стороны.

**Impact:**
- Любой непривилегированный процесс на WB-контроллере читает БД.
- Backup-инфраструктура (rsync, scp, мониторинг) может случайно скопировать `irrigation.db`.
- При коммерческом разворачивании — клиент не контролирует периметр Wirenboard, потенциальный leak через service-аккаунт.

**Remediation:**
1. `chmod 600 irrigation.db` на хосте (после остановки контейнера).
2. Перейти на named volume или явно установить umask в `Dockerfile` для `appuser`.
3. Документация для коммерческого деплоя — обязательная инструкция по permissions.
4. Опционально: SQLCipher для encryption-at-rest.

---

### [P0] NEW-03 — Гостевые модификаторы environment config (датчики)

**Severity:** High → P0 для коммерческого клиента
**File:line:** `app.py:341,427` — `/api/env` в whitelist + `routes/system_config_api.py:205-245`
**OWASP:** A01:2021 — Broken Access Control

**Описание:**
`/api/env` POST разрешён в `_ALLOWED_PUBLIC_POSTS` (app.py:341) **И** в whitelist `_require_admin_for_mutations` (app.py:427: `p.startswith('/api/env')`). Любой прошедший Cloudflare-периметр (т.е. с дефолтным `Poliv:Poliv`) может:
- Изменить MQTT-топик датчика температуры/влажности
- Перенаправить env_monitor на свой broker
- Подделать показания → обмануть weather_adjustment → отключить полив (DoS)

**Impact:**
Коммерческий клиент: садовник с дефолтным паролем может сломать конфигурацию датчиков без админа.

**Remediation:**
- Убрать `/api/env` из обоих whitelist'ов.
- Заменить на: GET `/api/env/values` (read-only, существует) + POST `/api/env` под `@admin_required`.

---

### [P0] NEW-04 — Cloudflare Worker: дефолтные creds `Poliv:Poliv` hardcoded

**Severity:** Critical (блокер для коммерциализации)
**File:line:** `devops/cloudflare/workers/wb-irrigation-auth.js:13-14`
**OWASP:** A07:2021 — Identification and Authentication Failures

**Описание:**
```js
const BASIC_USER = 'Poliv';
const BASIC_PASS = 'Poliv';
```
Креды захардкожены в Worker, одинаковы для всех клиентов, тривиально угадываются. Это единственная защита периметра — за ним голый Flask без пользовательских прав на физическое управление.

**Impact:**
- Любой кто знает домен (или сканирует CF sites) может зайти и управлять поливом.
- При коммерциализации **на каждого клиента** должен быть отдельный Worker / отдельные креды — текущая модель не поддерживает мультитенантность даже на уровне «1 worker per client».
- Логин лежит в коде — ротация = редеплой Worker → invalidates all cookies (приемлемо для emergency, неприемлемо для регулярной).

**Remediation:**
1. Перенести `BASIC_PASS` и `SECRET` в Cloudflare Worker Secrets (binding через `wrangler secret`).
2. Логин per-client (не одна строка `Poliv` для всех).
3. Минимум 16-символьный пароль, генерация per-client при онбординге.
4. Документировать процесс ротации.

---

### [P1] NEW-05 — Cloudflare Worker: HMAC secret hardcoded, `===` для Authorization

**Severity:** High
**File:line:** `wb-irrigation-auth.js:17, 32`
**OWASP:** A02:2021 — Cryptographic Failures + Timing Attack

**Описание:**
1. `SECRET = 'b30d34...'` (line 17) — HMAC ключ hardcoded в коде Worker. Любой с доступом к Worker source (CF dashboard, кто-то из команды, утечка `.git`) сможет генерировать валидные cookies без пароля → бессрочный bypass Basic Auth до ротации SECRET.
2. `authHeader === expected` (line 32) — обычное сравнение строк. Технически возможна timing-атака на пароль через CF edge (на практике маловероятна из-за edge jitter, но best practice — constant-time).

**Impact:**
- HMAC secret в коде — нарушение «secrets out of source». Особенно критично если worker репозиторий публичный или у broader-команды доступ к коду.
- Утечка SECRET = бесшумный бекдор: атакующий выписывает себе cookie на 90 дней без логов в Worker.

**Remediation:**
1. `SECRET` через `wrangler secret put HMAC_SECRET`, читать через `env.HMAC_SECRET`.
2. Замена `authHeader === expected` на constant-time compare через `crypto.subtle.timingSafeEqual` (или ручной XOR-loop как `constantTimeEqualHex`).
3. Cookie должна включать **user identifier** (не только expiry), чтобы можно было отозвать конкретный токен через blacklist.
4. Audit log в Worker — отправка failed attempts в logs/Sentry.

---

### [P1] NEW-06 — Cloudflare Worker: нет anti-bruteforce на Basic Auth

**Severity:** High
**File:line:** `wb-irrigation-auth.js` (нет rate limiting вообще)
**OWASP:** A07:2021

**Описание:**
Worker возвращает 401 на неверные креды без задержек, без rate limiting, без learning-mode. Атакующий может в неограниченном темпе перебирать пароли (Cloudflare Free Tier лимиты Worker — порядка 100k req/day, для 4-символьного пароля достаточно).

**Impact:**
Защищает только entropy пароля (`Poliv` — 5 знаков, мгновенный взлом). После исправления NEW-04 (16+ символов) — менее критично, но всё равно нужен brute-force defence.

**Remediation:**
1. Cloudflare WAF — rate limit `/login` или весь сайт (e.g., 30 req/min per IP).
2. Cloudflare Bot Fight Mode (Free tier).
3. После N failed attempts — увеличить delay (CF Workers KV для счётчиков).

---

### [P1] NEW-07 — Дефолтный пароль admin при первом запуске не форсит сложный

**Severity:** High
**File:line:** `db/settings.py:41-62` — `ensure_password_change_required()`
**OWASP:** A07:2021

**Описание:**
При первом запуске генерируется случайный 12-байтный URL-safe токен и **печатается в logs.warning**:
```python
logger.warning("Initial random password generated: %s (change it on first login!)", temp_password)
```

Пароль попадает в `backups/app.log` — файл который доступен в bind-mount, ротируется но не зашифрован, может уехать в backup. PIIMaskingFilter (`logging_setup.py:39-55`) маскирует ключи `password=`, `secret=` — но здесь используется именно format-string, и PII filter применяется к `record.getMessage()` уже после format → проверим: `msg.replace(f"{k}=", f"{k}=[REDACTED]")` — здесь `password ` (с пробелом перед двоеточием) НЕ matches `password=`. **Filter не сработает на этом сообщении.**

**Impact:**
Initial password утекает в logs, доступен на хосте, возможно копируется в backup-инфраструктуру.

**Remediation:**
1. Не логировать сам пароль. Записывать только событие: `"Initial password generated, must change on first login"`.
2. Альтернатива: показывать пароль один раз через console (stderr вне log handler), или через volume-маркерный файл `.first_run_password` (chmod 600), удалять при первом успешном логине.
3. Усилить PIIMaskingFilter — pattern на «password is X», «password: X», «generated: X».

---

### [P1] NEW-08 — Bulk update SQL: dynamic column names без whitelist

**Severity:** High (defense-in-depth)
**File:line:** `db/zones.py:300-307` (`update_zone_versioned`), `db/zones.py:338-365` (`bulk_update_zones`), `db/groups.py:100`, `db/programs.py:148`
**OWASP:** A03:2021 — Injection

**Описание:**
В `update_zone_versioned`:
```python
for k, v in updates.items():
    fields.append(f"{k} = ?")  # k — ключ из dict, попадает в SQL без валидации
    params.append(v)
sql = f"UPDATE zones SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND version = ?"
```

Сейчас все callers передают hardcoded keys (state, watering_start_time и т.д. — см. `irrigation_scheduler.py:620,657`). Реальной SQL-инъекции **нет** в текущем коде. Однако:
- Если новый разработчик добавит endpoint `/api/zones/<id>/raw_update` который принимает `request.json` и передаёт целиком в `update_zone_versioned(zone_id, request.get_json())` — мгновенный SQLi через ключ типа `"name=?, secret_field=(SELECT ...)--"`.
- В `bulk_upsert_zones` (`db/zones.py:414-417`) уже есть whitelist `_ALLOWED_UPDATE_COLUMNS` — это пример как должно быть везде.

**Impact:**
Скрытый footgun, который рано или поздно выстрелит. Для коммерческого продукта — это HIGH risk maintenance burden.

**Remediation:**
Добавить whitelist column names во все динамические UPDATE:
```python
_ALLOWED_KEYS = {'name', 'icon', 'duration', 'state', ...}
for k, v in updates.items():
    if k not in _ALLOWED_KEYS:
        raise ValueError(f"unknown column: {k}")
    fields.append(f"{k} = ?")
```

---

### [P1] NEW-09 — `SESSION_COOKIE_SECURE` по умолчанию False (та же VULN-013)

**Severity:** High
**File:line:** `app.py:316-317`, `.env.example:13`
**OWASP:** A02:2021

**Описание:**
По-прежнему `SESSION_COOKIE_SECURE = '0'` если не задано env. Cloudflare Worker подаёт TLS, но Flask cookie без `Secure` flag отправляется на любую схему. Отдельно — нет `HSTS` header (`Strict-Transport-Security`) в `add_security_headers` (app.py:299-310).

**Remediation:**
- Установить `SESSION_COOKIE_SECURE=1` в production deployment (не в `.env.example`, а в `docker-compose.production.yml` / systemd unit).
- Добавить HSTS header: `resp.headers['Strict-Transport-Security'] = 'max-age=15552000; includeSubDomains'`.
- Опционально автоопределение через `X-Forwarded-Proto`.

---

### [P1] NEW-10 — Telegram bot — chat_id-based auth, нет защиты от webhook spoof

**Severity:** Medium → High для коммерциализации
**File:line:** `services/telegram_bot.py:280-291, 541-551`
**OWASP:** A07:2021

**Описание:**
VULN-005 (старого отчёта) пофикшен через whitelist `telegram_admin_chat_id`. Но:
1. **Single chat_id** — если у клиента несколько админов, все должны делить один chat (например, через group). Группа может быть скомпрометирована (приглашение бота куда-то).
2. **HTTP poller fallback** (`SimpleHTTPPoller._run`, line 431) использует Telegram getUpdates polling — нет webhook secret check, нет TLS-pinning Telegram API. Полагается на корректность `requests` cert validation.
3. Bot token хранится зашифрованно (✅), но один раз decrypt'нутый кешируется в `self._token` (line 109) — в RAM долго, dump процесса откроет.

**Remediation:**
1. Для multi-admin — список `telegram_admin_chat_ids` (JSON array).
2. Документировать что Telegram bot — single-tenant (1 token = 1 instance).
3. Webhook режим вместо polling — с secret_token через Telegram setWebhook (если выходить на webhook).

---

### [P1] NEW-11 — CSP всё ещё с `'unsafe-inline'`

**Severity:** Medium → P1 для коммерциализации (prevent stored XSS escalation)
**File:line:** `app.py:303-309`
**OWASP:** A03:2021

**Описание:**
```python
"script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
```
Старая VULN-008. Не пофиксено. В сочетании с `innerHTML` использованием в шаблонах (logs.html, programs.html, settings.html — см. grep) — реальный путь stored-XSS → RCE через MQTT-топик зоны (если admin введёт `<script>` в имя зоны и viewer откроет страницу).

**Remediation:**
- Nonce-based CSP: генерировать nonce per-request, передавать в шаблон, инлайн-скрипты `<script nonce="...">`.
- Или вынести inline JS в отдельные файлы (большая работа).
- Минимум — убедиться что **все** места где user input рендерится в HTML, проходят через `escapeHtml()` или Jinja autoescape.

---

### [P1] NEW-12 — Нет HSTS, X-Permitted-Cross-Domain-Policies, Referrer-Policy

**Severity:** Medium
**File:line:** `app.py:299-310`
**OWASP:** A05:2021

**Описание:**
`add_security_headers` отдаёт только `X-Content-Type-Options`, `X-Frame-Options`, `CSP`. Не хватает:
- `Strict-Transport-Security` (см. NEW-09)
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy` (e.g. `geolocation=(), camera=(), microphone=()`)

**Remediation:**
```python
resp.headers['Strict-Transport-Security'] = 'max-age=15552000; includeSubDomains'
resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
resp.headers['Permissions-Policy'] = 'geolocation=(), camera=(), microphone=(), payment=()'
```

---

### [P2] NEW-13 — XOR fallback в `encrypt_secret` всё ещё в коде

**Severity:** Low
**File:line:** `utils.py:95-101`
**OWASP:** A02:2021

**Описание:**
Та же VULN-018. Если `pycryptodome` отсутствует — XOR с фиксированным ключом ≠ шифрование. В `requirements.txt` крипта зафиксирована (`pycryptodome>=3.21.0`), и Docker образ её ставит — реального риска нет. Но dead fallback увеличивает поверхность ошибки.

**Remediation:**
- Удалить XOR fallback. При отсутствии библиотеки — `raise ImportError` с понятным сообщением.
- Или хотя бы `logger.error` при использовании XOR.

---

### [P2] NEW-14 — `.env.example` экспонирует устаревшие basic_auth_proxy переменные

**Severity:** Informational
**File:line:** `.env.example:32-37`

**Описание:**
basic_auth_proxy.py удалён (см. VULN-005), но `.env.example` всё ещё показывает `AUTH_USER=admin/AUTH_PASS=admin`. Это путает следующего разработчика и может привести к попытке использовать эти переменные в новом коде.

**Remediation:**
Удалить блок `# ── Basic Auth Proxy` из `.env.example`.

---

### [P2] NEW-15 — Зависимости: paho-mqtt 2.1.0 и Flask 3.x — без SBOM/CVE-сканера

**Severity:** Low
**File:line:** `requirements.txt`

**Описание:**
- `paho-mqtt==2.1.0` — pinned exact version, но никаких CVE на 2.1.0 нет на 2026-05.
- Pillow `>=10.3.0` — open upper bound. Pillow часто получает CVE (CVE-2024-28219 — не fixed в 10.3.0; CVE-2025-* — нужно проверять).
- aiogram `>=3.8.0` — open bound, aiogram активно развивается.
- Нет CI-step с `pip-audit`/`safety check`.
- `python-dotenv >=1.0.1`, `requests >=2.28.0` — допустимо, но open bounds vs supply chain attack.

**Remediation:**
1. `pip-audit` или `pip-licenses` в CI.
2. Установить upper bounds для всех зависимостей (`Pillow>=10.3.0,<12.0`).
3. Renovate/Dependabot для контролируемых обновлений.
4. SBOM (`cyclonedx-bom`) генерируется в build → хранится с релизом.

---

### [P2] NEW-16 — Нет idle-session timeout

**Severity:** Low
**File:line:** `app.py:312-319` (no `PERMANENT_SESSION_LIFETIME`), `routes/auth.py:26` (`session.permanent = False`)

**Описание:**
Flask session по умолчанию — browser-session (закрытие браузера). При `permanent=False` — нет idle timeout. Открытая на компе админка садовника может оставаться залогиненной сутки.

**Remediation:**
- `app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=4)` + `session.permanent = True` после логина.
- Или server-side session storage (Flask-Session) с явным timeout.

---

### [P2] NEW-17 — `MAX_CONTENT_LENGTH=10MB` слишком много для IoT

**Severity:** Low (DoS)
**File:line:** `app.py:111`

**Описание:**
10MB body позволяет атакующему (через прошедший периметр) забить контейнерную память. Wirenboard — слабый CPU, гигабайты RAM не имеет. Photo upload и так ограничен 5MB (`MAX_FILE_SIZE` в helpers.py:119) — снизить до 6MB глобально.

**Remediation:**
```python
app.config['MAX_CONTENT_LENGTH'] = 6 * 1024 * 1024  # 6MB
```
Для не-photo endpoints — Pydantic-стиль валидация на размер JSON в audit-decorator (already truncates to 1024B per field в `_redact`).

---

### [P2] NEW-18 — Audit recursion depth = 8, но `_extract_payload` не обрезает массивы

**Severity:** Informational
**File:line:** `services/audit.py:99,143` + `_MAX_PAYLOAD_KEYS=64`

**Описание:**
Hard cap 8 на depth и 64 ключа — хорошо. Однако `_extract_payload` (line 158-186) собирает body + form + args без ограничения по общему размеру. Если атакующий отправит 10MB JSON (см. NEW-17) с 64×8 структурой — `_redact` сможет работать, но `_extract_payload` сначала прогонит `req.get_json()` — Flask загрузит 10MB в память.

**Remediation:**
- Связано с NEW-17 — снизить MAX_CONTENT_LENGTH.
- Опционально — early-reject body > 100KB на `/api/audit/ui` (где payload не должен быть большим).

---

### [P2] NEW-19 — Нет CSRF-защиты на `/logout` GET

**Severity:** Low
**File:line:** `routes/system_config_api.py:45` (`methods=['GET', 'POST']`)

**Описание:**
В коде комментарий «SEC-008: GET-based logout was CSRF-able» признаёт проблему, но всё равно поддерживает GET для backward-compat. `<img src=/logout>` в email/IM выкинет админа из сессии. Это не критично (только DoS), но всё ещё CSRF-vector.

**Remediation:**
Удалить `'GET'` из methods через 1-2 релиза, найдя и обновив все ссылки в шаблонах на форму с POST + CSRF token.

---

### [P2] NEW-20 — `mosquitto.conf` без TLS, port 1884 на хосте

**Severity:** Low
**File:line:** `docker-compose.yml:23`, `mosquitto.conf`

**Описание:**
Та же VULN-017. `1884:1883` пробрасывается на host (не localhost-bind). На WB-контроллере если включить firewall на `0.0.0.0` — broker в LAN. MQTT credentials в plaintext по сети.

**Remediation:**
- `ports: "127.0.0.1:1884:1883"` (bind на localhost).
- Или TLS на mosquitto (cafile/certfile/keyfile).
- Документировать deployment-precondition.

---

## Сводка

| Уровень | Кол-во | Описание |
|---------|--------|----------|
| **P0** (блокер коммерциализации) | 4 | NEW-01, NEW-02, NEW-03, NEW-04 |
| **P1** (важно до релиза) | 8 | NEW-05, NEW-06, NEW-07, NEW-08, NEW-09, NEW-10, NEW-11, NEW-12 |
| **P2** (желательно) | 8 | NEW-13, NEW-14, NEW-15, NEW-16, NEW-17, NEW-18, NEW-19, NEW-20 |
| **Старые VULN — пофикшено** | 3 | VULN-003, VULN-005, VULN-010 (path traversal — `safe_zone_photo_path`), VULN-007 (session fixation), VULN-008/CSP — оставлено |
| **Старые VULN — частично** | 2 | VULN-001, VULN-002 (приняты как threat-model без Cloudflare bypass) |

---

## Критические рекомендации (то что должно блокировать коммерциализацию)

1. **NEW-04 (CF Worker hardcoded `Poliv:Poliv`)** — главный блокер. Без per-client секретов продукт нельзя продавать.
2. **NEW-01 (MQTT plaintext в БД)** — обязательное шифрование before commercial release.
3. **NEW-02 (db permissions + bind-mount)** — deployment hardening документ + chmod в init-скрипте.
4. **NEW-03 (env config доступен гостю)** — 5-минутный fix, должен быть до релиза.
5. **NEW-05 (HMAC secret hardcoded)** — переместить в Worker Secrets.

---

## Положительное

- Авторизация бэкенда выстроена через roles (`admin`/`viewer`/`guest`), session regeneration на login пофикшено (отсутствие session fixation).
- Path-traversal защита (`safe_zone_photo_path`) — образцовая реализация.
- AES-256-GCM для telegram_bot_token.
- PII-фильтры в логах (хоть и не идеальные, см. NEW-07).
- Rate-limiter на login + general API + emergency.
- Audit log с redaction секретов (`_SECRET_KEY_FRAGMENTS`, `_redact` recursion cap).
- Whitelist column-mapping в `bulk_upsert_zones._ALLOWED_UPDATE_COLUMNS` (пример best practice).
- CSRF protection включён глобально, exemption список явный и узкий.
- Werkzeug `generate_password_hash`/`check_password_hash` (pbkdf2:sha256) + password blocklist + min-length.
- Non-root `appuser` в Docker.
- Constant-time HMAC compare в Cloudflare Worker (`constantTimeEqualHex`).

---

*Метод: статический анализ кода + cross-reference с прошлым отчётом 2026-04-02. Не выполнялись DAST, fuzzing, или динамические тесты. Рекомендую при подготовке к коммерческому релизу провести pentest от внешнего исполнителя (фокус: NEW-04, NEW-05).*
