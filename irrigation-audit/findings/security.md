# Security Audit — wb-irrigation refactor/v2

**Дата:** 2026-04-19
**Аудитор:** security-engineer
**Scope:** ветка `refactor/v2`, прод WB-Techpom (10.2.5.244)
**Метод:** статический анализ кода + чтение prod-snapshot. Никаких exploit-попыток на проде.

---

## Executive Summary

- **Критических находок:** 4
- **High:** 6
- **Medium:** 7
- **Low:** 4
- **Informational:** 3

### Топ-3 риска для немедленной эскалации

1. **SEC-001 (CRITICAL)** — Mosquitto на проде анонимный + нет TLS → любой в LAN управляет физическими клапанами полива через `mosquitto_pub`.
2. **SEC-002 (CRITICAL)** — Cloudflare Tunnel `https://poliv-kg.ops-lab.dev` идёт прямо в `localhost:8080`, минуя `basic_auth_proxy.py`. Все «public POST patterns» (`/api/zones/<id>/start`, `/api/emergency-stop`, `/api/groups/<id>/master-valve/*`, …) доступны **в открытый интернет без auth**.
3. **SEC-003 (CRITICAL)** — CSRF полностью снят с 10 API-blueprints (`app.py:100-109`); guest-ы могут стать жертвой CSRF на физические действия (открытие воды) при заходе на любой подконтрольный сайт.

---

## Findings

### CRITICAL

#### SEC-001: Mosquitto без auth/ACL/TLS — прямой контроль клапанов из LAN
- **Файл:** prod `/etc/mosquitto/mosquitto.conf` (см. `landscape/prod-snapshot.md`)
- **CVSS 3.1:** 9.6 (AV:A/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H) — Adjacent Network, Critical (физический ущерб)
- **Описание:** Из prod-snapshot — `allow_anonymous true` на listener'ах 1883 (TCP) и 18883 (WS), unix-socket. Файлы `passwd_file`/`acl_file` существуют, но не подключены директивами (`password_file`/`acl_file` закомментированы). Broker uptime 13 дней, 18 живых коннектов.
- **PoC (концептуально, не выполнял на проде):**
  ```
  # Из любой машины в подсети 10.2.0.0/16:
  mosquitto_sub -h 10.2.5.244 -p 1883 -t '#' -v          # читает все топики
  mosquitto_pub -h 10.2.5.244 -p 1883 -t '/devices/wb-mr6c_<ID>/controls/K1/on' -m '1'   # включает реле
  # Через WebSocket (port 18883) — то же самое из браузера в LAN.
  ```
- **Impact:**
  - Открытие любых клапанов в произвольное время → перерасход воды, затопление, повреждение растений.
  - Подмена retained-payload на топиках датчиков (env, rain, water meter) → ложные/нулевые показания → решения weather-engine на бракованных данных.
  - DoS через спам в топик мониторов (StateVerifier зацикливается на ретраях).
  - Кража MQTT-паролей других клиентов через subscribe (если бы они были — здесь все anonymous).
- **Что должно быть:** auth (passwd file подключён), ACL по client_id/topic-prefix, TLS на 8883, listener 1883 — bind 127.0.0.1 либо firewall на eth.

---

#### SEC-002: Cloudflare Tunnel идёт прямо в Flask:8080 в обход basic_auth_proxy
- **Файлы:** `app.py:96-109`, `app.py:202-214`, prod `/etc/cloudflared/config.yml`
- **CVSS 3.1:** 9.8 (AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H)
- **Описание:** Из prod-snapshot — cloudflared проксирует `https://poliv-kg.ops-lab.dev` → `http://localhost:8080`. Это Flask **напрямую**, не порт 8081 (`basic_auth_proxy.py`) и не nginx. Комментарий в `app.py:96-99` декларирует «service is behind nginx basic auth» — на edge через CF этого нет.
- **Список endpoint'ов, доступных через CF из открытого интернета без auth** (`app.py:202-214` whitelist `_ALLOWED_PUBLIC_POSTS` + `_ALLOWED_PUBLIC_PATTERNS`):
  - `POST /api/login`
  - `POST /api/password` *(но требует session — на guest'е вернёт 401)*
  - `POST /api/status`, `GET /health`
  - `POST /api/env`
  - `POST /api/emergency-stop` *(rate_limit 5/60s — но физическое действие)*
  - `POST /api/emergency-resume`
  - `POST /api/postpone`
  - `POST /api/zones/next-watering-bulk`
  - `POST /api/zones/<int>/mqtt/start`
  - `POST /api/zones/<int>/mqtt/stop`
  - `POST /api/zones/<int>/start`  ← **открывает клапан на N минут**
  - `POST /api/zones/<int>/stop`
  - `POST /api/groups/<int>/start-from-first` ← **запускает программу группы**
  - `POST /api/groups/<int>/stop`
  - `POST /api/groups/<int>/master-valve/<word>` ← **управление мастер-клапаном**
  - `POST /api/groups/<int>/start-zone/<int>`
- **PoC (концептуально):**
  ```
  curl -X POST https://poliv-kg.ops-lab.dev/api/zones/5/start
  curl -X POST https://poliv-kg.ops-lab.dev/api/groups/1/start-from-first
  curl -X POST https://poliv-kg.ops-lab.dev/api/emergency-stop
  ```
  Если Cloudflare Access не настроен (open=public), это сработает с любого устройства мира.
- **Impact:** Полный неавторизованный контроль над поливом из публичного интернета. Тривиальный DoS «лей воду пока счёт за воду не лопнет», или наоборот — emergency-stop в момент критического полива.
- **Зависит от:** наличия Cloudflare Access на тоннеле. Этот вопрос — `open-questions.md` Q12. Если CF Access **не** настроен — risk = Critical realised; если настроен (Service Token / OIDC) — реальный риск понижается до Medium (доверие к CF Access policy).
- **Что должно быть:** Либо CF Tunnel → port 8081 (basic_auth_proxy), либо CF Access Application с required identity, либо отдельный Flask before_request, проверяющий заголовок `Cf-Access-Jwt-Assertion`.

---

#### SEC-003: CSRF protection полностью снят с 10 API-blueprints
- **Файл:** `app.py:100-109`
- **CVSS 3.1:** 8.8 (AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:H/A:H)
- **Описание:** `csrf.exempt(<bp>)` вызвано для:
  ```
  zones_watering_api_bp, groups_api_bp, system_emergency_api_bp,
  system_status_api_bp, system_config_api_bp, weather_api_bp,
  zones_crud_api_bp, zones_photo_api_bp, mqtt_api_bp, programs_api_bp
  ```
  Это покрывает **все** mutating endpoints приложения. CSRFProtect остаётся только на page-render blueprints, которые не делают POST.
- **PoC (концептуально):** админ авторизован в `/login` → заходит на любой сайт с `<img src="https://poliv-kg.ops-lab.dev/api/zones/1/start">` или с `<form action=… method=POST>` + auto-submit. Браузер шлёт cookie, Flask видит валидную сессию, действие выполняется. Same-Origin не помогает: SameSite=`Lax` (`app.py:174`) разрешает top-level POST navigation в некоторых случаях, а `<img>`/`<link>` GET всегда уходит, и многие POST-only роуты тоже срабатывают через form submission.
- **Impact:** Любой вошедший admin/user может открыть произвольный клапан кликом на ссылку в email/мессенджере. С учётом SEC-002 (доступ из интернета) — exploit chains тривиально.
- **Что должно быть:** не exempt'ить blueprint'ы целиком; для legitimate guest-flow — CSRF token в `<meta>` страницы + автоматически в fetch headers; либо переход на double-submit cookie.

---

#### SEC-004: SQL injection через f-string в db/zones.py:341 (bulk import зон)
- **Файл:** `db/zones.py:341`
- **CVSS 3.1:** 6.5 (AV:N/AC:L/PR:H/UI:N/S:U/C:L/I:H/A:L) — требует admin (high), но позволяет порчу схемы/данных.
- **Описание:**
  ```python
  conn.execute(f"UPDATE zones SET {', '.join(fields)} WHERE id = ?", params)
  ```
  `fields` собирается из контролируемого whitelist (`name/icon/duration/group_id/topic/state/mqtt_server_id/updated_at`), но `add(field, value)` принимает имена полей **изнутри функции**, не извне. Сейчас инъекция невозможна, потому что field-имена hardcoded. **Однако** паттерн `cursor.execute(f"…{var}…", params)` опасен и повторён в `db/telegram.py:180` (`UPDATE bot_users SET {col}=?`) — там `col` тоже из whitelist (`allowed = {…}`).
- **Реальный риск сейчас:** низкий. Ставлю **High по pattern hygiene** — любая будущая правка, добавляющая поле из request body в `add()`, превращает это в полноценный SQLi.
- **Impact (потенциальный):** Если в `bulk_upsert_zones` попадёт field-имя из request → SQLi с правами admin. Может уничтожить таблицу `zones`, выгрузить хеш пароля через UNION (хотя SQLite execute не поддерживает multi-statement, но DROP/UPDATE возможны).
- **Что должно быть:** строгий whitelist через map `{api_key: db_column}`, без сборки SQL из строк.

---

### HIGH

#### SEC-005: Endpoint'ы управления клапанами без декоратора auth — полностью полагаются на before_request whitelist
- **Файлы:** `routes/zones_watering_api.py:28` (`start_zone`), `:94` (`stop_zone`); `routes/system_emergency_api.py:23,62`; `routes/system_config_api.py:231` (`api_postpone`)
- **CVSS:** 8.1 (AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:H)
- **Описание:** На функциях нет `@admin_required`/`@user_required`. Защита целиком зависит от двух before_request'ов в `app.py:225-249` и `:271-290` + `_is_status_action()` whitelist (`app.py:202-214`). Любая ошибка/refactor этого middleware (pop'нуть @app.before_request, поменять path matching) — мгновенный bypass.
- **Дополнительно:** `_is_status_action()` определена дважды-почти-одинаково (раньше было `app.py:203/256`, сейчас на одной helper'ной строке `:216`, но whitelist строится на `_re.compile` с потенциалом ошибки regex-anchoring). Regex `r'^/api/zones/\d+/start$'` не учитывает trailing slash, `?query`, encoded paths (`%73tart`) — Flask нормализует, но это лишний trust.
- **Impact:** При ошибочном изменении middleware (а refactor v2 уже дублирует логику auth) — открытие клапанов из guest-сессии.
- **Что должно быть:** explicit decorator на каждом mutating endpoint, before_request — backup-слой а не единственный.

---

#### SEC-006: Session не регенерится после login → session fixation
- **Файл:** `routes/auth.py:34-38`
- **CVSS:** 7.1 (AV:N/AC:H/PR:L/UI:R/S:U/C:H/I:H/A:N)
- **Описание:**
  ```python
  if success:
      login_limiter.reset(ip)
      session['logged_in'] = True
      session['role'] = role
      return jsonify({...})
  ```
  Нет `session.clear()` / `session.regenerate_id()` (Flask-Session) перед установкой role=admin. Атакующий, который заранее подсунул жертве свою session-cookie (через XSS/MITM/предзаход на guest-link `?guest=1`), после ввода жертвой пароля админа — получает admin-сессию.
- **Impact:** Переход guest → admin без нового sid. Эскалация привилегий.
- **Что должно быть:** новая сессия (новый sid) при смене role; logout (`/logout`) вместо `session['logged_in']=False` должен делать `session.clear()`.

---

#### SEC-007: Logout не уничтожает сессию, переводит role в 'user' (повышение для guest!)
- **Файл:** `routes/system_config_api.py:44-48`
- **CVSS:** 6.5 (AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:H/A:N)
- **Описание:**
  ```python
  @system_config_api_bp.route('/logout', methods=['GET'])
  def api_logout():
      session['logged_in'] = False
      session['role'] = 'user'      # ← НЕ 'guest', НЕ session.clear()
      return redirect(...)
  ```
  Cookie остаётся живой, sid тот же. После logout role становится `'user'`, что в `_auth_before_request` (`app.py:242`) обрабатывается так: `'user' != 'admin'` → проверяется `_is_status_action(pth)`. То есть logout-нутый юзер по-прежнему может вызывать `start_zone`/`stop_zone`/`emergency-stop` через whitelist.
- **Impact:** Logout не отзывает права на mutating actions. Сессия живёт до истечения cookie.
- **Что должно быть:** `session.clear()` + явное удаление session cookie (`session.permanent=False`, очистка sid), регенерация sid.

---

#### SEC-008: GET-only logout уязвим к CSRF
- **Файл:** `routes/system_config_api.py:44`
- **CVSS:** 4.3 → бонус к SEC-007.
- **Описание:** `/logout` доступен по GET. `<img src="https://poliv-kg.ops-lab.dev/logout">` логаутит админа. Не критично само по себе, но enables phishing → re-login на fake-page при next visit.
- **Impact:** Принудительный logout админа, возможно для timing-атак / навязывания re-auth на phishing-странице.

---

#### SEC-009: Endpoint'ы загрузки фото и фото-rotate не имеют auth-декоратора + path traversal через `static/<photo_path>`
- **Файлы:** `routes/zones_photo_api.py:72,133,154,192`
- **CVSS:** 6.5 (AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:H/A:L)
- **Описание:**
  - Декоратора нет; защита через before_request mutation guard (`app.py:271-290`). DELETE/POST методы здесь не в `_is_status_action()` whitelist — то есть для роли `'guest'` they correctly return 403. Для роли `'user'`/`'viewer'` — viewer заблочен (`viewer role: read-only`); user — допускается? Проверка `role != 'admin' and not _is_status_action(p)` → 403. **Но** для логаутнутого юзера с role='user' (см. SEC-007) — также 403. Ок: сейчас закрыто middleware-ом.
  - Однако `delete_zone_photo`/`get_zone_photo` строит путь `os.path.join('static', zone['photo_path'])`. `photo_path` берётся из БД, кладётся `f"media/{ZONE_MEDIA_SUBDIR}/{filename}"` через `update_zone_photo`. Если эту строку можно записать через bulk_upsert_zones из admin-API с `photo_path='../../etc/passwd'`, то `delete_zone_photo` удалит произвольный файл, а `get_zone_photo` отдаст его (path traversal с правами процесса). Проверки `photo_path` на `..` нет.
- **Impact:** Admin → произвольное чтение/удаление файлов в FS на правах service-юзера.
- **Что должно быть:** валидация `photo_path` — должен начинаться с `media/<known_subdir>/`, не содержать `..`/`/` за пределами базы; либо `safe_join`.

---

#### SEC-010: Telegram bot token хранится в SQLite encrypted ключом из файла на той же машине
- **Файлы:** `utils.py:49-81`, `services/telegram_bot.py:104`, `db/migrations.py:704`
- **CVSS:** 5.5 (AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N)
- **Описание:** `IRRIG_SECRET_KEY` загружается из env или файла `.irrig_secret_key` (`utils.py:57-81`), затем используется AES-GCM для шифрования telegram-token и MQTT-паролей. Если атакующий получает FS-доступ (через path traversal SEC-009, SSH-leak, бэкап) — он одновременно получает **и** шифротекст из `irrigation.db`, **и** ключ из `.irrig_secret_key`. Encryption-at-rest здесь даёт защиту только от случайной утечки одного файла.
- **Доп.:** XOR-fallback в `utils.py:97-100` (если pycryptodome нет) — это не encryption, это obfuscation; но pycryptodome в requirements.txt:10 присутствует.
- **Impact:** Эксфильтрация telegram-токена → атакующий получает контроль над ботом, может слать сообщения в admin chat (но `_is_authorized_chat()` проверяет direction — сообщения от не-admin chat игнорируются), читать историю бота через `getUpdates`.
- **Что должно быть:** ключ — в KMS / outside FS; либо ENC и DB на разных файловых системах с разными permissions; backup procedures должны исключать `.irrig_secret_key` либо шифровать его отдельно.

---

### MEDIUM

#### SEC-011: CSP содержит `'unsafe-inline'` для script-src и style-src — XSS не митигируется CSP
- **Файл:** `app.py:164-170`
- **CVSS:** 5.4 (AV:N/AC:L/PR:N/UI:R/S:U/C:L/I:L/A:N)
- **Описание:** CSP заголовок:
  ```
  default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'
  ```
  `'unsafe-inline'` в script-src отключает большую часть CSP-защиты. Inline-скрипты в `templates/status.html:263-265` (`window._ssrZones = JSON.parse({{ inline_zones|default('[]')|tojson }});`) — корректно используют `|tojson`, что защищает от XSS, но CSP не нужно открывать на `'unsafe-inline'` ради этого: nonce-based CSP или externalize.
- **Impact:** Если где-то есть DOM-XSS / reflected XSS (см. SEC-012) — CSP не остановит.

---

#### SEC-012: 50+ использований `innerHTML` с интерполяцией данных в JS
- **Файлы:** `static/js/status.js` (минимум 30 мест), `static/js/programs.js`, `static/js/zones.js`, `static/js/zones-groups.js`, `static/js/zones-table.js`, `static/js/app.js`
- **CVSS:** 5.4 (AV:N/AC:L/PR:L/UI:N/S:U/C:L/I:L/A:N)
- **Описание:** Многие места используют `escapeHtml(...)` (`zones.js:319-330`, `programs.js:291,300`), но не все:
  - `static/js/status.js:1246` — `metricsEl.innerHTML = '<span>💧 ' + (humVal !== null ? Math.round(humVal) + '%' : '—') + '</span>'` — humVal числовой, ок.
  - `static/js/status.js:638` — `card.innerHTML += '<div…>${cells.join('')}${pad2}</div>'` — `cells` строится из БД-данных. Зависит от того, экранируется ли там содержимое. Беглый взгляд: cells содержит названия групп/зон, имена пользователей — данные из admin-CRUD. Admin может вписать `<img onerror=fetch('/api/emergency-stop',{method:'POST'})>` в имя зоны → stored XSS на странице status, исполняется в контексте всех просматривающих.
  - `static/js/status.js:487,597,715,1140,1141` — `card.innerHTML = \`…${...}\`` без явного escape.
- **PoC:** admin (или атакующий через SEC-002) PUT'ит `/api/zones/<id>` с `name='<img src=x onerror=fetch("/api/emergency-stop",{method:"POST",credentials:"include"})>'`. Каждый зашедший на dashboard выполняет этот код.
- **Impact:** Stored XSS → действия от имени просматривающего admin-а (открытие воды, изменение программ). Учитывая SEC-003 (no CSRF), exploit-цепочка тривиальна.
- **Что должно быть:** заменить все `.innerHTML = ` с user-data на `.textContent` либо проводить через `escapeHtml()` всё интерполируемое; уйти с `'unsafe-inline'` в CSP.

---

#### SEC-013: Login limiter уязвим к bypass через X-Forwarded-For (если Flask за CF)
- **Файлы:** `routes/auth.py:27`, `services/api_rate_limiter.py:94`, `services/rate_limiter.py:33`
- **CVSS:** 5.3 (AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L)
- **Описание:** `ip = request.remote_addr or '0.0.0.0'`. Flask не настроен с `ProxyFix` middleware (поиск по проекту: `ProxyFix` нигде не упомянут). Через CF-tunnel `request.remote_addr` = `127.0.0.1` (cloudflared соединение localhost), и **все запросы из мира приходят с одного IP** → rate limiter залочит весь мир после 5 неудачных попыток. Это DoS. Но также: если за CF добавить ProxyFix и trust X-Forwarded-For — атакующий шлёт `X-Forwarded-For: <random>` и обходит лимит полностью.
- **Impact:** В текущей конфигурации — DoS на login (5 неверных попыток с любого устройства → 15 мин lockout всех). После «фикса» через ProxyFix без валидации trusted proxies — bypass полностью.
- **Что должно быть:** ProxyFix с `x_for=1` + trusted CF IP ranges, либо использовать `Cf-Connecting-IP` заголовок с проверкой что он от CF.

---

#### SEC-014: Параметр `angle` в rotate_zone_photo не валидирован → integer overflow / отказ обслуживания
- **Файл:** `routes/zones_photo_api.py:154-189`
- **CVSS:** 4.3 (AV:N/AC:L/PR:L/UI:N/S:U/C:N/I:N/A:L)
- **Описание:** `angle = int(data.get('angle', 90))` — нет диапазона. `angle=999999999` — Pillow `img.rotate(-999999999, expand=True)` сожрёт CPU/RAM. Auth: admin only через middleware.
- **Impact:** Admin (или атакующий через SEC-002, если CF Access выключен) → процесс зависает / OOM.

---

#### SEC-015: `weather_api.py:69,89` — SQL построен через `%d days`-формат-строки
- **Файл:** `routes/weather_api.py:69,89`
- **CVSS:** 4.0 — formal pattern issue
- **Описание:**
  ```python
  ('-%d days' % days, limit)
  ```
  `days` — `int(request.args.get('days', 7))`, обёрнут в `min(90, max(1, …))`. Сейчас безопасно (строго int). Но pattern «строкой подмешиваем число в SQL-параметр» опасен — если кто-то поменяет на `int_or_default()` без clamp, получим `'-X; DROP TABLE…'` в SQL parameter (правда, sqlite parameter binding это блокирует).
- **Impact:** Низкий, ставлю Medium как pattern hygiene.

---

#### SEC-016: SECRET_KEY из `.secret_key` файла — генерируется при первом запуске, но restart изменяет
- **Файл:** `config.py:11-38`
- **CVSS:** 4.0 (AV:L/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:L)
- **Описание:** `_load_or_generate_secret()` корректно: читает env → файл → генерит. **Но**: если файл `.secret_key` удалён/потерян (бэкап-восстановление, fresh deploy без копирования файла) — генерится новый ключ → все живые сессии инвалидируются. Это не security-bug в строгом смысле, но: если Docker-контейнер пересоздаётся без volume-mount `.secret_key`, ключ генерится в каждом новом контейнере → сессии живут только в одном инстансе. Прод сейчас — один процесс, риск теоретический.
- **Также:** check `env_val != 'wb-irrigation-secret'` подтверждает, что в истории был hardcoded дефолт — нужно проверить git history (не делал, scope `refactor/v2`).

---

#### SEC-017: `/api/logging/debug` без admin-required — guest может включить DEBUG-логирование
- **Файл:** `routes/system_config_api.py:327-345`
- **CVSS:** 4.3 (AV:N/AC:L/PR:L/UI:N/S:U/C:L/I:N/A:L)
- **Описание:** Endpoint `/api/logging/debug` (POST) меняет log level на DEBUG на runtime. Декоратора нет. POST → попадает в before_request mutation guard, role не admin → 403. **Но** path не в `_is_status_action()`, и mutation guard ждёт role='admin'. Защита через middleware есть. Ставлю Medium на pattern: GET тоже выдаёт current state, без auth — обнаружение системы.
- **Impact:** Information disclosure (узнать включён ли debug). DEBUG логи могут содержать MQTT-payload, имена клиентов, токен (через `_redact_url` он редактится, но не везде).

---

### LOW

#### SEC-018: Stack traces / internal errors в JSON response
- **Файлы:** многократно: `routes/settings.py:34,76`, `routes/system_emergency_api.py:92`, `routes/system_config_api.py:110,113`
- **CVSS:** 3.1
- **Описание:** В нескольких местах `return jsonify({'error': str(e)}), 500` или `'message': str(e)`. `str(e)` для `sqlite3.Error` содержит SQL-фрагменты, для `OSError` — пути файловой системы.
- **Impact:** Information disclosure.

---

#### SEC-019: `/api/backup` без auth-декоратора, может быть вызван не-admin через SEC-002
- **Файл:** `routes/system_emergency_api.py:82-92`
- **CVSS:** 3.7 (AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:N/A:L)
- **Описание:** `api_backup` создаёт backup через `db.create_backup()`. POST, нет декоратора. `/api/backup` не в `_ALLOWED_PUBLIC_POSTS` → middleware `:286-287` для не-admin вернёт 403. Защита есть. Ставлю Low: если CF-tunnel exposes endpoint и в CF logs появляются `backup_path` ответы — раскрытие путей.

---

#### SEC-020: `/api/postpone` принимает `days` без валидации диапазона
- **Файл:** `routes/system_config_api.py:241,253`
- **CVSS:** 2.6
- **Описание:** `days = data.get('days', 1)`, потом `timedelta(days=days)`. `days=99999` → отложить полив на 273 года. Не критично, но бесконтрольный input.

---

#### SEC-021: `app.run(debug=True)` в `app.py:445`
- **Файл:** `app.py:443-445`
- **CVSS:** 5.0 (только если запущен через `python app.py`)
- **Описание:** `if __name__ == '__main__': app.run(debug=True, host='0.0.0.0', port=8080)`. Запуск Flask debug — Werkzeug debugger PIN, RCE через `/console` если DEBUG включён. На проде запускается через hypercorn (см. systemd unit в prod-snapshot), `__main__` не выполняется → не активно. Но если кто-то запустит `python app.py` для дебага — мгновенный RCE на `0.0.0.0:8080`.
- **Impact:** Условный RCE.

---

### INFORMATIONAL

#### SEC-022: Verify_password не имеет explicit constant-time wrapper, но `werkzeug.security.check_password_hash` уже использует `hmac.compare_digest`
- **Файл:** `services/auth_service.py:27`
- **Описание:** Werkzeug 3.x → `check_password_hash` использует `hmac.compare_digest` внутри, защищая от timing-атак на сравнение хешей. Ок.

#### SEC-023: Telegram bot — chat_id whitelist реализован (`_is_authorized_chat`)
- **Файл:** `services/telegram_bot.py:279-290`
- **Описание:** Помечено как «SECURITY FIX (VULN-005)». Защита от чужих chat'ов работает в обоих транспортах (aiogram + HTTP poller). Без `telegram_admin_chat_id` все запросы deny — fail-safe. Хорошо.

#### SEC-024: Dependencies — версии в основном свежие
- **Файл:** `requirements.txt`
- **Описание:** Flask>=3.1.0, Pillow>=10.3.0 (CVE-2024-28219 — fixed in 10.3.0), paho-mqtt==2.1.0, APScheduler==3.10.4, aiogram>=3.8.0, pycryptodome>=3.21.0. Нет известных critical CVE на момент 2026-04-19 для этих версий. Прод по prod-snapshot — Python 3.9.2 (Debian 11). Python 3.9 EOL октябрь 2025; нужна миграция на 3.11+.

---

## Verified author findings (из BUGS-REPORT.md)

| Bug | Status | Comment |
|-----|--------|---------|
| **3.1** `_locks_snapshot` импортирован-не-используется в `app.py` | **Verified** | Импорт `from services.locks import snapshot_all_locks as _locks_snapshot` нашёл в `routes/system_status_api.py:13`, не в `app.py`. В `app.py` нет. **Расхождение с BUGS-REPORT** — возможно уже cleanup'нуто. Не security-bug. |
| **3.2** Дубль auth-логики в `app.py:225-249` и `:271-290` | **Verified, расширил → SEC-005, SEC-007** | Два before_request делают почти одну проверку, с разной обработкой viewer/guest. Refactor risk. |
| **3.3** `_is_status_action()` дубль 203/256 | **Verified нет** | В текущем `app.py` функция определена один раз на :216. Whitelist `_ALLOWED_PUBLIC_POSTS`+`_ALLOWED_PUBLIC_PATTERNS` определён на :202-214. Возможно баг был зафиксирован до cleanup'а. |
| **2.x** Anonymous Mosquitto | **Verified → SEC-001** | См. выше, расширил attack-сценарии. |
| **2.x** CSRF снят | **Verified → SEC-003** | Перечислил все 10 blueprint'ов, добавил chain с SEC-002. |
| **2.x** CF Tunnel mode | **Verified → SEC-002** | Подтвердил список public POST patterns. |

---

## Cross-cutting attack chain (для эскалации в Phase 4)

**Самый дешёвый exploit-путь, доступный сегодня без подготовки:**

1. Атакующий находит публичный URL `https://poliv-kg.ops-lab.dev` (сабдомен в CT-логах Cloudflare).
2. Без auth выполняет `curl -X POST https://poliv-kg.ops-lab.dev/api/groups/<gid>/start-from-first` для каждой группы (id 1..N — узнаются через `GET /api/groups` если этот endpoint открыт).
3. Все клапаны открываются на полную программу. Вода льётся часами.
4. Параллельно `curl -X POST https://poliv-kg.ops-lab.dev/api/emergency-stop` для timing-атак (включил-выключил-включил).
5. Если admin замечает и логинится, чтобы `/api/emergency-stop` вручную — атакующий продолжает спамить `/api/emergency-resume`.

**Защита от этого пути:** одно из двух — (а) Cloudflare Access Application с Service Token / OIDC на `poliv-kg.ops-lab.dev`, (б) перенаправить CF Tunnel на port 8081 (basic_auth_proxy.py). Без этого — **полная компрометация физической системы из открытого интернета**.

---

## Out of scope / open

- **Не проверял live**: реальная конфигурация Cloudflare Access на тоннеле (требует доступа к CF dashboard) — критично для подтверждения SEC-002.
- **Не запускал**: `pip-audit`, `bandit`, `safety` — рекомендую devops-у запустить локально на копии venv.
- **Не аудитил подробно**: `services/zone_control.py`, `irrigation_scheduler.py` — это домен logic-engineer, но возможны race conditions, влияющие на security (concurrent emergency-stop / start).
- **Не аудитил**: `static/sw.js` (service worker) — может быть вектором persistent attack через cache poisoning, требует frontend-эксперта.
- **Открытый вопрос Q12 (open-questions.md)**: нужно подтвердить наличие/отсутствие CF Access поверх tunnel. Это меняет SEC-002 с Critical на Medium.
- **Открытый вопрос**: SSE endpoint `/api/mqtt/zones-sse` — авторизация на подключение не аудитилась глубоко (только наличие). Может оказаться unauth-stream MQTT-данных.
- **WebSocket port 18883 на Mosquitto**: достижим ли он из CF Tunnel? Если cloudflared проксирует port 18883 в websocket-mode — public mqtt-over-ws без auth = SEC-001 в публичный интернет.
