# 🔒 Security Audit Report — WB-Irrigation

**Дата:** 2026-04-02  
**Аудитор:** Security Engineer (OWASP Expert)  
**Проект:** wb-irrigation (Flask + MQTT + WirenBoard)  
**Хост:** poliv-kg.ops-lab.dev (nginx reverse proxy)  
**Контекст:** IoT-система управления физическими клапанами полива. Компрометация = потенциальный потоп.

---

## 📊 Общая оценка безопасности: 6.5 / 10

Система демонстрирует **зрелый подход к безопасности** во многих аспектах: CSRF-защита, rate limiting, параметризованные SQL-запросы, CSP-заголовки, шифрование секретов AES-GCM. Однако есть **критичные архитектурные проблемы** в аутентификации и авторизации, которые могут привести к несанкционированному управлению физическими клапанами.

---

## 🔴 ТОП-5 САМЫХ КРИТИЧНЫХ ПРОБЛЕМ

| # | Severity | Проблема | Риск для IoT |
|---|----------|----------|-------------|
| 1 | **CRITICAL** | Гостевой вход без пароля → доступ к управлению клапанами | Потоп |
| 2 | **CRITICAL** | API эндпоинты управления клапанами доступны без admin-роли | Потоп |
| 3 | **HIGH** | MQTT-серверы API не защищены аутентификацией (CRUD) | Перехват управления |
| 4 | **HIGH** | Отсутствие аутентификации на Telegram-боте | Удалённое управление клапанами |
| 5 | **HIGH** | basic_auth_proxy с дефолтными admin/admin credentials | Обход авторизации |

---

## 📋 Детальный реестр уязвимостей

### VULN-001: Гостевой вход позволяет управлять клапанами
- **Severity:** 🔴 CRITICAL  
- **OWASP:** A01:2021 – Broken Access Control  
- **Файл:** `routes/auth.py:14-18`  
- **Описание:**  
  Переход на `/login?guest=1` устанавливает `session['role'] = 'viewer'` и `session['logged_in'] = True` без какой-либо аутентификации. Хотя viewer теоретически read-only, проблема в том что:
  1. Роль `viewer` не блокирована в middleware `_auth_before_request` для "status actions"
  2. Эндпоинты `/api/zones/<id>/start`, `/api/zones/<id>/stop`, `/api/emergency-stop`, `/api/groups/<id>/start-from-first` включены в `_is_status_action()` whitelist (`app.py:203-212, 256-263`)
  3. Результат: **любой без пароля** может запускать/останавливать зоны полива и делать emergency stop
- **Impact:** Удалённый неаутентифицированный злоумышленник может открыть все клапаны → физический ущерб (затопление)
- **Remediation:**  
  - Убрать гостевой вход или ограничить viewer строго GET-запросами без исключений
  - Убрать start/stop/emergency из `_is_status_action()` whitelist для не-admin ролей
  - Для операций с клапанами требовать `admin` роль

---

### VULN-002: Status Actions доступны без аутентификации для не-admin  
- **Severity:** 🔴 CRITICAL  
- **OWASP:** A01:2021 – Broken Access Control  
- **Файл:** `app.py:202-214` и `app.py:254-264`  
- **Описание:**  
  Две функции `_is_status_action()` определяют whitelist POST-эндпоинтов, доступных без admin роли:
  ```python
  # Эти эндпоинты доступны ЛЮБОМУ пользователю (в т.ч. guest/viewer):
  '/api/emergency-stop', '/api/emergency-resume'
  '/api/postpone'
  '/api/groups/<id>/start-from-first', '/api/groups/<id>/stop'
  '/api/groups/<id>/master-valve/<action>'
  '/api/zones/<id>/mqtt/start', '/api/zones/<id>/mqtt/stop'
  '/api/zones/<id>/start', '/api/zones/<id>/stop'
  ```
  Все эти эндпоинты управляют физическими клапанами через MQTT. Их выполнение не требует admin-аутентификации — достаточно гостевой сессии.
- **Impact:** Полный контроль над физическими клапанами без пароля
- **Remediation:**  
  - Все эндпоинты управления клапанами — только `admin` роль
  - Разделить "status read" (GET) от "status action" (POST с side-effects)
  - Emergency stop — возможно оставить без auth (fail-safe), но emergency resume — только admin

---

### VULN-003: MQTT Server CRUD API без аутентификации
- **Severity:** 🟠 HIGH  
- **OWASP:** A01:2021 – Broken Access Control  
- **Файл:** `routes/mqtt_api.py:26-81`  
- **Описание:**  
  Все CRUD-эндпоинты для MQTT серверов (`/api/mqtt/servers` GET/POST, `/api/mqtt/servers/<id>` GET/PUT/DELETE) не имеют декораторов `@admin_required`. Middleware в app.py пропускает POST/PUT/DELETE для путей начинающихся с `/api/mqtt/` (строка `app.py:257: if p == '/api/login' or p.startswith('/api/env') or p.startswith('/api/mqtt/') or p == '/api/password': return None`).
  
  Это позволяет:
  1. Получить credentials MQTT сервера (username/password в plaintext)
  2. Изменить MQTT сервер → перенаправить команды на свой broker
  3. Удалить MQTT сервера → отключить управление системой
- **Impact:** Утечка MQTT credentials, перехват управления, DoS
- **Remediation:**  
  - Добавить `@admin_required` на все CRUD эндпоинты MQTT серверов
  - Убрать `/api/mqtt/` из whitelist в `_require_admin_for_mutations`
  - Не возвращать MQTT пароли в GET-ответах (маскировать)

---

### VULN-004: MQTT credentials возвращаются в plaintext
- **Severity:** 🟠 HIGH  
- **OWASP:** A02:2021 – Cryptographic Failures  
- **Файл:** `routes/mqtt_api.py:27-30` и `db/mqtt.py` (через `db.get_mqtt_servers()`)  
- **Описание:**  
  `GET /api/mqtt/servers` возвращает полный объект MQTT сервера включая `username` и `password` в открытом виде. Эти credentials дают полный доступ к MQTT-брокеру и управлению всеми реле через MQTT.
- **Impact:** Получив MQTT credentials, атакующий может напрямую управлять реле обходя веб-приложение
- **Remediation:**  
  - Маскировать password в API-ответах (`****` + последние 2 символа)
  - Хранить MQTT passwords зашифрованными (как telegram_bot_token)

---

### VULN-005: Telegram-бот без аутентификации пользователей
- **Severity:** 🟠 HIGH  
- **OWASP:** A07:2021 – Identification and Authentication Failures  
- **Файл:** `services/telegram_bot.py:289-310` и `routes/telegram.py`  
- **Описание:**  
  Telegram-бот не проверяет, является ли отправитель сообщения авторизованным пользователем. Любой, кто найдёт бота, может:
  1. Отправить `/start` → получить главное меню
  2. Нажать "Группы" → увидеть все группы
  3. Нажать "Запустить" → включить полив
  4. Нажать "Отложить" → изменить расписание
  
  В `_on_message` просто вызывается `db.upsert_bot_user()` и отправляется меню без проверки прав.
- **Impact:** Любой пользователь Telegram может управлять физическими клапанами
- **Remediation:**  
  - Проверять `chat_id` по whitelist (telegram_admin_chat_id)
  - Или требовать авторизацию паролем при первом использовании (`/auth <password>`)
  - В настройках бота есть `telegram_access_password_hash` — но он нигде не используется

---

### VULN-006: basic_auth_proxy с дефолтными credentials
- **Severity:** 🟠 HIGH  
- **OWASP:** A07:2021 – Identification and Authentication Failures  
- **Файл:** `basic_auth_proxy.py:22-23` и `.env.example:38-39`  
- **Описание:**  
  ```python
  USER = os.environ.get("AUTH_USER", "admin")
  PASS = os.environ.get("AUTH_PASS", "admin")
  ```
  Дефолтные credentials `admin/admin`. `.env.example` также показывает `AUTH_USER=admin, AUTH_PASS=admin`. Если proxy используется в production без изменения переменных окружения, система полностью открыта.
- **Impact:** Обход аутентификации через прокси
- **Remediation:**  
  - Не задавать дефолтные пароли — требовать явную установку
  - Генерировать случайный пароль если переменная не задана
  - Добавить warning log при запуске с дефолтными credentials

---

### VULN-007: Session Fixation через гостевой вход
- **Severity:** 🟡 MEDIUM  
- **OWASP:** A07:2021 – Identification and Authentication Failures  
- **Файл:** `routes/auth.py:14-18`  
- **Описание:**  
  При гостевом входе сессия не регенерируется. Атакующий может зафиксировать session ID (если cookie перехвачен), а затем эскалировать до admin после настоящего логина (session fixation).
  
  Также при `api_login()` (строка 29) сессия не регенерируется после успешной аутентификации.
- **Remediation:**  
  - Вызывать `session.regenerate()` или пересоздавать сессию после успешного логина
  - `session.clear()` + заново установить role после аутентификации

---

### VULN-008: CSP разрешает `unsafe-inline` для script и style
- **Severity:** 🟡 MEDIUM  
- **OWASP:** A03:2021 – Injection  
- **Файл:** `app.py:140-145`  
- **Описание:**  
  ```python
  "script-src 'self' 'unsafe-inline'; "
  "style-src 'self' 'unsafe-inline'; "
  ```
  `unsafe-inline` полностью нивелирует защиту CSP от XSS. Если атакующий сможет внедрить HTML/JS (stored XSS), CSP не заблокирует выполнение.
- **Impact:** CSP не защищает от XSS
- **Remediation:**  
  - Использовать nonce-based CSP (`script-src 'nonce-<random>'`)
  - Вынести инлайн-скрипты в отдельные .js файлы
  - Если невозможно — хотя бы добавить `'strict-dynamic'`

---

### VULN-009: Потенциальный Stored XSS через innerHTML с данными из API
- **Severity:** 🟡 MEDIUM  
- **OWASP:** A03:2021 – Injection  
- **Файл:** `templates/logs.html:302,340`, `templates/programs.html:980,1060,1365`, `templates/settings.html:398`  
- **Описание:**  
  В нескольких шаблонах используется `innerHTML` для вставки данных из API. Хотя в `mqtt.html` и `zones.js` используется `escapeHtml()`, в `logs.html` и `programs.html` данные вставляются через template literals в innerHTML без экранирования. Если атакующий (или баг) сохранит `<script>` в имени зоны/программы/лога, это приведёт к Stored XSS.
  
  Пример в `logs.html:355-366`: `tdTime.textContent` — безопасно, но в других местах (`tbody.innerHTML`) может быть небезопасно.
  
  **Смягчение:** Большая часть данных — цифры и внутренние строки. `escapeHtml()` определён в `static/js/app.js` и используется в key templates. Риск реальный но ограниченный.
- **Remediation:**  
  - Использовать `textContent` вместо `innerHTML` везде где возможно
  - Пропускать все данные через `escapeHtml()` в template literals
  - Добавить серверную санитизацию имён зон/программ (alphanumeric + limited special chars)

---

### VULN-010: Path Traversal в photo upload (ограниченный)
- **Severity:** 🟡 MEDIUM  
- **OWASP:** A01:2021 – Broken Access Control  
- **Файл:** `routes/zones_photo_api.py:88-131`  
- **Описание:**  
  Имя файла формируется как `f"ZONE_{zone_id}{out_ext}"` — zone_id приходит из URL-параметра (int) и расширение из `normalize_image()`. Основное имя безопасно. Однако при получении фото (`get_zone_photo`, строка 167):
  ```python
  filepath = os.path.join('static', zone['photo_path'])
  ```
  Значение `photo_path` берётся из БД. Если через SQL-инъекцию или direct DB manipulation `photo_path` содержит `../../../etc/passwd`, `send_file()` отдаст произвольный файл.
  
  **Смягчение:** SQL-инъекция маловероятна (parameterized queries), но photo_path не валидируется при чтении.
- **Remediation:**  
  - Валидировать `photo_path` при чтении: `os.path.realpath()` должен быть внутри `static/media/`
  - Использовать `secure_filename()` при сохранении path в БД

---

### VULN-011: MQTT probe позволяет подписку на произвольные топики
- **Severity:** 🟡 MEDIUM  
- **OWASP:** A04:2021 – Insecure Design  
- **Файл:** `routes/mqtt_api.py:91-145`  
- **Описание:**  
  `POST /api/mqtt/<id>/probe` принимает `filter` из JSON body и подписывается на произвольные MQTT-топики. В сочетании с VULN-003 (нет auth на MQTT API), любой может:
  1. Прочитать все MQTT-сообщения (включая данные о состоянии клапанов, датчиков)
  2. Через scan-sse — подписаться на `#` и получить весь трафик брокера
- **Impact:** Information disclosure, monitoring state всех устройств
- **Remediation:**  
  - Добавить `@admin_required`
  - Ограничить допустимые topic filters (whitelist)

---

### VULN-012: Emergency stop/resume без admin-роли
- **Severity:** 🟡 MEDIUM  
- **OWASP:** A01:2021 – Broken Access Control  
- **Файл:** `routes/system_emergency_api.py:24-67` и `app.py:202,257`  
- **Описание:**  
  `/api/emergency-stop` и `/api/emergency-resume` включены в `allowed_public_posts` и `_is_status_action()`. Любой пользователь (даже guest) может:
  - Выполнить аварийную остановку (может быть допустимо как fail-safe)
  - **Снять аварийную остановку** (resume) — это критично, т.к. позволяет возобновить полив после emergency
  
  Rate limiter (5 req/min) незначительно ограничивает это.
- **Remediation:**  
  - Emergency **resume** — только admin
  - Emergency stop можно оставить без auth (fail-safe принцип)

---

### VULN-013: SESSION_COOKIE_SECURE по умолчанию отключен
- **Severity:** 🟡 MEDIUM  
- **OWASP:** A02:2021 – Cryptographic Failures  
- **Файл:** `app.py:151-154` и `.env.example:10`  
- **Описание:**  
  `SESSION_COOKIE_SECURE` по умолчанию `False`. Для системы доступной через интернет (poliv-kg.ops-lab.dev) за nginx+HTTPS, session cookie может быть перехвачен при downgrade-атаке или mixed content.
- **Remediation:**  
  - Установить `SESSION_COOKIE_SECURE=1` в production
  - Автоопределение: если `X-Forwarded-Proto: https` — включать Secure

---

### VULN-014: SQL string formatting в миграциях
- **Severity:** 🟢 LOW  
- **OWASP:** A03:2021 – Injection  
- **Файл:** `db/migrations.py:247,280-284`  
- **Описание:**  
  ```python
  cur = conn.execute("PRAGMA table_info(%s)" % table)
  conn.execute('DROP TABLE IF EXISTS %s' % tmp)
  conn.execute('CREATE TABLE %s (%s)' % (tmp, defs_csv))
  ```
  Использование `%` formatting для SQL. Однако значение `table` определяется внутри кода (не из user input), поэтому реальный риск SQL-инъекции минимален. Это migration-only код.
- **Remediation:**  
  - Использовать параметризованные запросы где возможно
  - Валидировать имена таблиц regex: `^[a-z_]+$`

---

### VULN-015: Отсутствие rate limiting на GET API-эндпоинтах
- **Severity:** 🟢 LOW  
- **OWASP:** A04:2021 – Insecure Design  
- **Файл:** `app.py:273-293`  
- **Описание:**  
  Rate limiter `_general_api_rate_limit` пропускает все GET-запросы. Это может привести к:
  - Scraping данных (зоны, программы, MQTT servers с credentials)
  - DoS через множественные GET-запросы (особенно `/api/status` который делает MQTT connection check)
- **Remediation:**  
  - Добавить мягкий rate limit на GET-эндпоинты (100 req/min)
  - Кэшировать `/api/status` MQTT check

---

### VULN-016: Docker container runs as non-root but DB file mounted writable
- **Severity:** 🟢 LOW  
- **OWASP:** A05:2021 – Security Misconfiguration  
- **Файл:** `docker-compose.yml:10-11` и `Dockerfile:25-27`  
- **Описание:**  
  ```yaml
  volumes:
    - ./irrigation.db:/app/irrigation.db
  ```
  DB файл монтируется напрямую. Хотя container runs as `appuser`, host-side permissions на `irrigation.db` могут быть world-readable, что позволяет извлечь password hashes и encrypted secrets при доступе к host filesystem.
- **Remediation:**  
  - Использовать named Docker volume вместо bind mount для DB
  - Или: `chmod 600 irrigation.db` на хосте

---

### VULN-017: MQTT Broker без TLS в docker-compose
- **Severity:** 🟢 LOW  
- **OWASP:** A02:2021 – Cryptographic Failures  
- **Файл:** `mosquitto.conf`, `docker-compose.yml:15-22`  
- **Описание:**  
  Mosquitto настроен с `allow_anonymous false` + password file + ACL — хорошо. Но listener на порту 1883 без TLS. В docker-compose порт 1884:1883 открыт на хосте. MQTT credentials передаются в plaintext.
  
  **Смягчение:** Если MQTT-брокер доступен только внутри Docker network или localhost — риск ограничен.
- **Remediation:**  
  - Включить TLS для MQTT (`listener 8883`, `cafile`, `certfile`, `keyfile`)
  - Не пробрасывать порт MQTT на хост если не нужно
  - Или ограничить `ports: "127.0.0.1:1884:1883"`

---

### VULN-018: XOR fallback для шифрования секретов
- **Severity:** 🟢 LOW  
- **OWASP:** A02:2021 – Cryptographic Failures  
- **Файл:** `utils.py:86-89`  
- **Описание:**  
  Если `pycryptodome` не установлен, используется XOR "шифрование" — это не шифрование, XOR с known-length key тривиально обратим.
  ```python
  x = bytes([b[i] ^ k[i % len(k)] for i in range(len(b))])
  return 'xor:' + base64.urlsafe_b64encode(x).decode('utf-8')
  ```
  **Смягчение:** В Docker образе `pycryptodome` в requirements.txt, так что AES-GCM используется.
- **Remediation:**  
  - Убрать XOR fallback — при отсутствии crypto library не сохранять секрет
  - Или хотя бы логировать WARNING при использовании XOR

---

## ✅ Что сделано хорошо

| Компонент | Оценка | Комментарий |
|-----------|--------|-------------|
| **CSRF Protection** | ✅ Отлично | Flask-WTF CSRFProtect включен глобально. Только `/api/login` exempt — корректно. |
| **SQL Injection** | ✅ Отлично | Все запросы параметризованы. Column names в UPDATE через whitelist. |
| **Password Hashing** | ✅ Хорошо | werkzeug `generate_password_hash` с PBKDF2. Минимальная длина 8 символов. Blocklist простых паролей. |
| **Rate Limiting** | ✅ Хорошо | Login rate limiter (5 attempts / 5min / 15min lockout). API rate limiter per-IP. Emergency rate limited. |
| **Security Headers** | ✅ Хорошо | X-Content-Type-Options, X-Frame-Options, CSP (хоть и с unsafe-inline). |
| **Session Config** | ✅ Хорошо | HttpOnly=True, SameSite=Lax по умолчанию. |
| **Secret Key Management** | ✅ Хорошо | Автогенерация, сохранение в файл с 0o600 permissions. Не hardcoded. |
| **File Upload** | ✅ Хорошо | MIME type validation, extension whitelist, size limit (5MB), image normalization. |
| **MQTT Broker Config** | ✅ Хорошо | `allow_anonymous false`, password file, ACL — baseline security. |
| **Encryption** | ✅ Хорошо | AES-256-GCM для telegram token и других секретов. Key persistence с proper permissions. |
| **Graceful Shutdown** | ✅ Хорошо | Все клапаны закрываются при shutdown (SIGTERM/atexit). |
| **Non-root Container** | ✅ Хорошо | Docker: `USER appuser`. |

---

## 🛡️ Рекомендации по Hardening

### Критичные (сделать немедленно)

1. **Убрать гостевой вход или ограничить строго GET-запросами**
   - `routes/auth.py:14-18` — закомментировать или добавить whitelist на read-only endpoints
   - `app.py:202-214` — убрать start/stop/emergency из `_is_status_action()` whitelist

2. **Добавить `@admin_required` на все MQTT API endpoints**
   - `routes/mqtt_api.py` — все функции
   - `app.py:257` — убрать `/api/mqtt/` из whitelist в `_require_admin_for_mutations`

3. **Защитить Telegram-бота аутентификацией**
   - Проверять `chat_id` по `telegram_admin_chat_id`
   - Или использовать уже хранимый `telegram_access_password_hash` для первичной авторизации

4. **Emergency resume — только admin**

### Важные (ближайший спринт)

5. **Маскировать MQTT passwords в API-ответах**
6. **Регенерировать session после логина** (session fixation protection)
7. **Включить `SESSION_COOKIE_SECURE=1` в production**
8. **Добавить TLS для MQTT** или ограничить прослушивание localhost

### Желательные (backlog)

9. **Заменить `unsafe-inline` в CSP на nonce-based**
10. **Добавить rate limiting на GET endpoints**
11. **Валидировать `photo_path` из DB перед `send_file()`** 
12. **Убрать XOR fallback в encrypt_secret**
13. **Добавить audit logging** для всех аутентификационных событий
14. **Ограничить MQTT topic filters в probe/scan**
15. **Добавить HSTS header** для production

### IoT-специфичные рекомендации

16. **Watchdog на длительность полива** — уже есть (zone cap, group exclusive), но добавить hard limit на уровне MQTT ACL (если WirenBoard поддерживает)
17. **Физический kill switch** — рекомендовать hardware-level fallback (таймер на электроклапанах)
18. **Мониторинг аномалий** — alert если зона работает >N минут сверх запланированного
19. **Network segmentation** — WirenBoard контроллер в отдельном VLAN, MQTT только через VPN/internal network

---

## 📊 Матрица рисков

| Severity | Count | Описание |
|----------|-------|----------|
| 🔴 CRITICAL | 2 | Неаутентифицированное управление клапанами |
| 🟠 HIGH | 4 | MQTT credential exposure, Telegram auth bypass, default creds |
| 🟡 MEDIUM | 6 | CSP, XSS, session fixation, path traversal, emergency resume |
| 🟢 LOW | 6 | Docker config, MQTT TLS, XOR fallback, rate limiting gaps |

**Общее количество уязвимостей:** 18

---

*Отчёт подготовлен на основе статического анализа кода. Рекомендуется дополнительно провести DAST-тестирование (Burp Suite / OWASP ZAP) и pentest.*
