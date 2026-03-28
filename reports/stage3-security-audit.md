# Этап 3: Security Audit wb-irrigation

**Дата:** 2026-03-28
**Аудитор:** AI Security Auditor (Claude Opus 4.6)

## Executive Summary

Проект wb-irrigation управляет реальными клапанами полива через MQTT → реле Wirenboard в локальной сети. Общий уровень безопасности — **ниже среднего**: присутствуют захардкоженные секреты, отключённая CSRF-защита, анонимный MQTT без TLS, хранение MQTT-паролей в plaintext, и отсутствие гарантии доставки критичных команд (QoS 0). При этом SQL-инъекции практически исключены, пароли пользователей хешируются грамотно, и есть механизм аварийной остановки. Ключевой риск — возможность неавторизованного управления реле из локальной сети через MQTT, что может привести к реальному затоплению.

## Risk Rating: CRITICAL

## Статистика

- Critical: 3
- High: 5
- Medium: 6
- Low: 4
- Info: 4

---

## Critical Vulnerabilities

### SEC-001: Анонимный MQTT брокер — прямое управление реле без аутентификации

- **Severity:** CRITICAL
- **Файл:** `mosquitto.conf:2`, `docker-compose.yml:18-19`
- **Вектор атаки:**
  1. Атакующий подключается к сети (Wi-Fi/Ethernet) Wirenboard контроллера
  2. Подключается к MQTT брокеру на порту 1883 (или 1884 с хоста) без пароля
  3. Публикует `1` в топик `/devices/wb-mr6cv3_85/controls/K1/on` — реле открывается
  4. Может открыть все 24 зоны одновременно
- **Impact:** Неконтролируемый полив, затопление территории, повреждение оборудования и растений, расход воды. Приложение wb-irrigation даже не узнает о такой команде, если она отправлена напрямую (хотя SSE-хаб подпишется на изменения и обновит UI, но не заблокирует).
- **Текущий конфиг:**
```
listener 1883
allow_anonymous true
persistence true
```
- **Fix:**
```
# mosquitto.conf
listener 1883
allow_anonymous false
password_file /mosquitto/config/passwd
persistence true
persistence_location /mosquitto/data/
log_dest file /mosquitto/log/mosquitto.log

# ACL (опционально — ограничить топики)
acl_file /mosquitto/config/acl

# Генерация файла паролей:
# mosquitto_passwd -c /mosquitto/config/passwd irrigation_app
# mosquitto_passwd -b /mosquitto/config/passwd irrigation_app <strong_password>
```

### SEC-002: Захардкоженный SECRET_KEY для Flask-сессий

- **Severity:** CRITICAL
- **Файл:** `config.py:9`, `docker-compose.yml:8`
- **Вектор атаки:**
  1. SECRET_KEY по умолчанию `wb-irrigation-secret` — известен всем, кто видел код
  2. Атакующий подделывает Flask session cookie, устанавливая `role: admin` и `logged_in: True`
  3. Получает полный доступ ко всем API включая управление зонами, изменение MQTT-серверов, бэкапы
  4. Инструмент: `flask-unsign --sign --cookie '{"logged_in":true,"role":"admin"}' --secret 'wb-irrigation-secret'`
- **Impact:** Полный обход аутентификации, управление всеми зонами, доступ к credentials
- **Текущий код:**
```python
# config.py:9
SECRET_KEY = os.environ.get('SECRET_KEY', 'wb-irrigation-secret')

# docker-compose.yml:8
- SECRET_KEY=${SECRET_KEY:-wb-irrigation-secret}
```
- **Fix:**
```python
# config.py
import secrets
import os

def _load_or_generate_secret():
    key = os.environ.get('SECRET_KEY')
    if key and key != 'wb-irrigation-secret':
        return key
    key_file = os.path.join(os.path.dirname(__file__), '.secret_key')
    if os.path.exists(key_file):
        with open(key_file, 'r') as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(key_file, 'w') as f:
        f.write(key)
    os.chmod(key_file, 0o600)
    return key

class Config:
    SECRET_KEY = _load_or_generate_secret()
```

### SEC-003: MQTT пароли хранятся в plaintext в SQLite

- **Severity:** CRITICAL
- **Файл:** `database.py:1648-1675` (create_mqtt_server), `database.py:1688-1727` (update_mqtt_server)
- **Вектор атаки:**
  1. Атакующий получает доступ к файлу `irrigation.db` (через бэкап API, физический доступ, или path traversal)
  2. `SELECT password FROM mqtt_servers` — пароли MQTT-серверов в открытом виде
  3. При этом Telegram bot token шифруется через `encrypt_secret()` — явная inconsistency
- **Impact:** Утечка MQTT credentials, возможность подключения к MQTT-брокерам от имени приложения
- **Текущий код:**
```python
# database.py:1651-1665
cur = conn.execute('''
    INSERT INTO mqtt_servers (name, host, port, username, password, ...)
    VALUES (?, ?, ?, ?, ?, ...)
''', (
    data.get('name', 'MQTT'),
    data.get('host', 'localhost'),
    int(data.get('port', 1883)),
    data.get('username'),
    data.get('password'),  # ← PLAINTEXT!
    ...
))
```
- **Fix:**
```python
# database.py - create_mqtt_server / update_mqtt_server
from utils import encrypt_secret, decrypt_secret

# При записи:
password_enc = encrypt_secret(data.get('password')) if data.get('password') else None

# При чтении (get_mqtt_server):
if row:
    d = dict(row)
    if d.get('password'):
        d['password'] = decrypt_secret(d['password'])
    return d
```

---

## High Vulnerabilities

### SEC-004: CSRF-защита фактически отключена

- **Severity:** HIGH
- **Файл:** `config.py:11`, `app.py` (27 строк с `@csrf.exempt`)
- **Вектор атаки:**
  1. `WTF_CSRF_CHECK_DEFAULT = False` — CSRF не проверяется ни для одного endpoint по умолчанию
  2. Дополнительно 27 явных `@csrf.exempt` — двойное отключение
  3. Пользователь залогинен в wb-irrigation (session cookie), открывает вредоносный сайт
  4. Сайт отправляет POST на `http://10.2.5.244:8080/api/emergency-stop` или `api/zones/1/mqtt/start` — запрос проходит
- **Impact:** В локальной сети — атакующий может управлять поливом через CSRF, если пользователь посетит вредоносную страницу. Severity HIGH (не CRITICAL), т.к. требуется что пользователь одновременно залогинен и посетит вредоносный сайт в той же сети.
- **Эндпоинты с наибольшим риском (из 27 @csrf.exempt):**

  | Endpoint | Риск |
  |----------|------|
  | `/api/emergency-stop` (POST) | HIGH — может остановить полив |
  | `/api/emergency-resume` (POST) | HIGH — может снять аварийный стоп |
  | `/api/mqtt/servers` (POST) | HIGH — создание MQTT-сервера |
  | `/api/mqtt/servers/<id>` (PUT/DELETE) | HIGH — изменение/удаление MQTT |
  | `/api/zones/<id>/photo` (POST/DELETE) | MEDIUM — загрузка файлов |
  | `/api/backup` (POST) | MEDIUM — создание бэкапа |
  | `/api/settings/*` (POST) | MEDIUM — изменение настроек |

- **Fix:**
```python
# config.py
WTF_CSRF_CHECK_DEFAULT = True  # Включить CSRF глобально

# Для JSON API — использовать CSRF-токен в заголовке X-CSRFToken
# В base.html добавить:
# <meta name="csrf-token" content="{{ csrf_token() }}">
# В JavaScript:
# fetch(url, { headers: {'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content} })
```

### SEC-005: Гостевой доступ позволяет управлять зонами и аварийным стопом

- **Severity:** HIGH
- **Файл:** `routes/auth.py:12-14`, `app.py:860`, `app.py:1086-1098`
- **Вектор атаки:**
  1. Открыть `http://10.2.5.244:8080/login?guest=1` — мгновенный вход как guest, без пароля
  2. Guest получает role='guest', но `_is_status_action()` разрешает ему:
     - `/api/emergency-stop` — аварийная остановка
     - `/api/emergency-resume` — снятие аварийного стопа
     - `/api/zones/*/mqtt/start` и `/mqtt/stop` — включение/выключение зон
     - `/api/groups/*/start-from-first` и `/stop` — управление группами
     - `/api/postpone` — отложить полив
  3. Фактически гость может полностью управлять поливом без знания пароля
- **Impact:** Любой, кто знает URL системы, может управлять всеми зонами без аутентификации
- **Fix:**
```python
# routes/auth.py — убрать гостевой доступ или ограничить его read-only:
@auth_bp.route('/login', methods=['GET'])
def login_page():
    # Гостевой доступ — только для просмотра, без возможности управления
    if request.args.get('guest') == '1':
        session['logged_in'] = True
        session['role'] = 'viewer'  # Новая роль: только просмотр
        return redirect(url_for('status_bp.index'))
    return render_template('login.html')

# app.py — _is_status_action: убрать start/stop для guest
def _is_status_action(path: str) -> bool:
    if session.get('role') == 'viewer':
        return False  # viewer не может ничего менять
    # ... остальная логика для admin/user
```

### SEC-006: Rate-limiting на логин привязан к session, не к IP

- **Severity:** HIGH
- **Файл:** `routes/auth.py:24-29`
- **Вектор атаки:**
  1. Rate-limit проверяет `session.get('_last_login_try')` — привязан к cookie
  2. Атакующий не отправляет cookies → rate-limit не срабатывает вообще
  3. Окно 0.5 сек — даже с cookies это 120 попыток/мин
  4. Дефолтный пароль '1234' → bruteforce за секунды
  5. Нет lockout после N неудачных попыток
- **Impact:** Brute-force пароля тривиален, особенно с дефолтным паролем '1234'
- **Текущий код:**
```python
# routes/auth.py:24-29
now = time.time()
last = float(session.get('_last_login_try', 0))
session['_last_login_try'] = now
if (now - last) < 0.5:
    return jsonify({'success': False, 'message': 'Слишком часто.'}), 429
```
- **Fix:**
```python
# services/rate_limiter.py
import time
import threading
from collections import defaultdict

class LoginRateLimiter:
    def __init__(self, max_attempts=5, window_sec=300, lockout_sec=900):
        self._attempts = defaultdict(list)  # ip -> [timestamps]
        self._lock = threading.Lock()
        self.max_attempts = max_attempts
        self.window_sec = window_sec
        self.lockout_sec = lockout_sec

    def check(self, ip: str) -> tuple[bool, int]:
        """Returns (allowed, retry_after_sec)"""
        now = time.time()
        with self._lock:
            attempts = self._attempts[ip]
            # Удаляем старые попытки
            attempts[:] = [t for t in attempts if now - t < self.window_sec]
            if len(attempts) >= self.max_attempts:
                retry = int(self.lockout_sec - (now - attempts[0]))
                return False, max(1, retry)
            return True, 0

    def record_failure(self, ip: str):
        with self._lock:
            self._attempts[ip].append(time.time())

    def reset(self, ip: str):
        with self._lock:
            self._attempts.pop(ip, None)

_limiter = LoginRateLimiter()

# routes/auth.py — использовать:
from services.rate_limiter import _limiter

@auth_bp.route('/api/login', methods=['POST'])
def api_login():
    ip = request.remote_addr or '0.0.0.0'
    allowed, retry_after = _limiter.check(ip)
    if not allowed:
        return jsonify({'success': False, 'message': f'Заблокировано. Повторите через {retry_after}с'}), 429
    # ...
    if not success:
        _limiter.record_failure(ip)
    else:
        _limiter.reset(ip)
```

### SEC-007: Ключ шифрования генерируется из hostname — предсказуем

- **Severity:** HIGH
- **Файл:** `utils.py:25-33`
- **Вектор атаки:**
  1. Если `IRRIG_SECRET_KEY` не задан (а он не задан в docker-compose.yml), ключ берётся из `os.uname().nodename`
  2. Для Wirenboard hostname предсказуем: `wirenboard-XXXX` (серийный номер)
  3. Серийный номер видно на корпусе / в веб-интерфейсе WB
  4. Зная hostname → вычисляем ключ → дешифруем Telegram bot token из БД
  5. XOR fallback (при отсутствии pycryptodome) тривиально реверсируется
- **Impact:** Компрометация шифрованных секретов (Telegram bot token), если атакующий получит доступ к БД
- **Текущий код:**
```python
def _get_secret_key() -> bytes:
    key = os.getenv('IRRIG_SECRET_KEY')
    if key:
        try:
            return base64.urlsafe_b64decode(key + '===')
        except Exception:
            pass
    # fallback from hostname (weak, but better than plain)
    try:
        host = os.uname().nodename
    except Exception:
        host = 'irrigation'
    b = (host or 'irrigation').encode('utf-8')
    return (b * 4)[:32]
```
- **Fix:**
```python
def _get_secret_key() -> bytes:
    key = os.getenv('IRRIG_SECRET_KEY')
    if key:
        try:
            return base64.urlsafe_b64decode(key + '===')
        except Exception:
            pass
    # Генерируем и сохраняем случайный ключ
    key_file = os.path.join(os.path.dirname(__file__), '.irrig_secret_key')
    if os.path.exists(key_file):
        with open(key_file, 'rb') as f:
            return f.read()[:32]
    import secrets
    new_key = secrets.token_bytes(32)
    with open(key_file, 'wb') as f:
        f.write(new_key)
    os.chmod(key_file, 0o600)
    return new_key
```

### SEC-008: QoS 0 для критичных MQTT-команд (включение/выключение реле)

- **Severity:** HIGH
- **Файл:** `services/mqtt_pub.py:91` (дефолт `qos: int = 0`), `services/zone_control.py` (все вызовы без qos)
- **Вектор атаки:** Не вектор атаки, а reliability failure:
  1. Команда ON/OFF реле отправляется с QoS 0 (fire-and-forget)
  2. При потере пакета (перегрузка сети, перезапуск брокера) команда не доставляется
  3. БД обновляет state на 'on'/'off', хотя реле реально не переключилось
  4. Зона показывает "выключена" в UI, но реально клапан открыт
- **Impact:** Несоответствие состояния в БД и реального положения реле. Зона может остаться открытой при неполучении команды OFF.
- **Fix:**
```python
# services/zone_control.py — все вызовы publish_mqtt_value для ON/OFF:
publish_mqtt_value(server, normalize_topic(topic), '1',
    min_interval_sec=0.0, qos=1,  # ← QoS 1 для гарантии доставки
    meta={'cmd': 'zone_on', 'ver': str(ver)})

# services/mqtt_pub.py — изменить дефолт:
def publish_mqtt_value(..., qos: int = 1) -> bool:  # ← QoS 1 по умолчанию
```

---

## Medium Vulnerabilities

### SEC-009: Дефолтный пароль '1234' при инициализации

- **Severity:** MEDIUM
- **Файл:** `database.py:181,197-199`
- **Описание:** При первом запуске создаётся пароль '1234'. Есть механизм `ensure_password_change_required()` (database.py:1315), который выставляет флаг `password_must_change=1`, и `_init_scheduler_before_request` (app.py:967) блокирует мутационные POST-запросы пока пароль не сменён. Однако:
  1. Блокируются только POST/PUT/DELETE к `/api/*`, не к страницам
  2. Guest-вход обходит проверку полностью (role != admin)
  3. Эвристика detect слабая — любой хеш считается "нужно сменить" при первом запуске
- **Fix:** Минимальная длина нового пароля ≥ 8 символов (сейчас ≥ 4, app.py:1307). Запрет '1234', '0000', 'password' и т.п.

### SEC-010: Subprocess для git rev-list (command injection — низкий риск)

- **Severity:** MEDIUM
- **Файл:** `app.py:217-218`
- **Текущий код:**
```python
cnt = subprocess.check_output(['git', 'rev-list', '--count', 'HEAD'], cwd=os.getcwd())
```
- **Описание:** Используется список аргументов (не shell=True), данные пользователя не участвуют. Риск минимален. Но `subprocess` в before_request или инициализации Flask — нетипично. Ошибка git (нет репозитория) молча ловится.
- **Fix:** Вычислять версию один раз при старте, кешировать. Уже сделано (`APP_VERSION = _compute_app_version()`), но стоит убрать try/except и обработать ошибку явно.

### SEC-011: Бэкапы содержат все данные БД включая plaintext MQTT-пароли

- **Severity:** MEDIUM
- **Файл:** `database.py:1793-1836` (create_backup), `app.py:3312-3326` (api_backup)
- **Описание:**
  1. Бэкап — полная копия `irrigation.db` через SQLite backup API
  2. Содержит: plaintext MQTT-пароли, хеши паролей, зашифрованный Telegram token
  3. Бэкап-файлы хранятся в `./backups/` без шифрования
  4. Endpoint `/api/backup` (POST) доступен для admin (проверка в `_require_admin_for_mutations`)
  5. Но файлы бэкапов доступны на файловой системе без дополнительной защиты
- **Fix:** Шифровать бэкап файлы, или хотя бы зашифровать MQTT-пароли в БД (SEC-003 fix).

### SEC-012: f-string в динамическом SQL для bot_users

- **Severity:** MEDIUM
- **Файл:** `database.py:747`
- **Текущий код:**
```python
conn.execute(f'UPDATE bot_users SET {col}=? WHERE chat_id=?', (1 if enabled else 0, int(chat_id)))
```
- **Описание:** `col` формируется из whitelist `allowed` dict (6 фиксированных значений), поэтому SQL-инъекция невозможна в текущей реализации. Однако паттерн опасен — если кто-то добавит пользовательский ввод в `col` при рефакторинге, получит SQL-инъекцию.
- **Fix:**
```python
# Добавить assert для явности:
col = allowed.get(key)
if not col:
    return False
assert col in ('notif_critical', 'notif_emergency', 'notif_postpone',
               'notif_zone_events', 'notif_rain', 'notif_zone_start', 'notif_zone_stop'), f"invalid col: {col}"
```

### SEC-013: Session cookie без Secure флага на HTTP

- **Severity:** MEDIUM
- **Файл:** `app.py:438-440`
- **Описание:** `SESSION_COOKIE_SECURE` по умолчанию `False` (через env). На HTTP-only IoT-устройстве в локальной сети это допустимо, но если доступ будет через reverse proxy с HTTPS — cookie будет передаваться по незащищённому каналу. Зато `SameSite=Lax` и `HttpOnly=True` — корректно настроены.
- **Fix:** Для HTTPS-сценариев: `SESSION_COOKIE_SECURE=1` в docker-compose.yml.

### SEC-014: Нет валидации MQTT payload в SSE хабе

- **Severity:** MEDIUM
- **Файл:** `app.py:3812-3820` (SSE hub _on_message callback)
- **Описание:** MQTT payload декодируется и пересылается в SSE поток как JSON-значение. Если атакующий отправит вредоносный MQTT payload (при анонимном брокере!), данные попадут в SSE → JavaScript клиента. Jinja2 auto-escaping не защищает от XSS в SSE/JavaScript контексте.
- **Текущий код:**
```python
payload = msg.payload.decode('utf-8', errors='ignore').strip()
# ... далее payload включается в JSON:
data_mv = json.dumps({'mv_group_id': int(gid), 'mv_state': mv_state})
```
- **Fix:** Payload валидируется до допустимых значений ('0', '1', 'on', 'off', 'true', 'false'). Уже частично сделано через `new_state = 'on' if payload in ('1','true','ON','on') else 'off'`. JSON-сериализация (`json.dumps`) также обеспечивает экранирование. **Реальный XSS-риск низкий**, но стоит валидировать payload строже.

---

## Low / Info

### SEC-015: Dockerfile запускает приложение от root (LOW)

- **Файл:** `Dockerfile`
- **Описание:** Нет `USER` директивы — приложение запускается как root внутри контейнера. Если произойдёт RCE — атакующий получит root в контейнере.
- **Fix:**
```dockerfile
RUN adduser --disabled-password --gecos '' appuser
RUN chown -R appuser:appuser /app
USER appuser
```

### SEC-016: Hypercorn binds на 0.0.0.0 (LOW)

- **Файл:** `run.py:13`
- **Описание:** `cfg.bind = [f"0.0.0.0:{port}"]` — приложение доступно на всех интерфейсах. На Wirenboard это означает доступность из всей локальной сети. Если WB подключён к нескольким сетям — доступ из любой из них.
- **Fix:** Для изоляции — привязать к 127.0.0.1 и использовать reverse proxy (nginx). Но на IoT-устройстве в локальной сети это обычная конфигурация.

### SEC-017: Нет ограничения на размер JSON body (LOW)

- **Файл:** `app.py` (все endpoints принимающие `request.get_json()`)
- **Описание:** Flask по умолчанию ограничивает body 16MB (`MAX_CONTENT_LENGTH`). Но явное ограничение не установлено. Атакующий может отправить большой JSON и вызвать DoS (memory exhaustion на слабом WB контроллере).
- **Fix:**
```python
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2MB
```

### SEC-018: Логирование маскирует PII но не MQTT-пароли в контексте создания (LOW)

- **Файл:** `app.py` (PIIMaskingFilter)
- **Описание:** `PIIMaskingFilter` маскирует слова password/token/secret в логах. Но при создании MQTT-сервера пароль логируется в контексте всей data dict. Фильтр использует regex и может не покрыть все случаи.

### SEC-019: Нет CORS-настройки — по умолчанию same-origin (INFO)

- **Описание:** Flask не добавляет CORS-заголовки по умолчанию. Это безопасно — cross-origin запросы не пройдут (кроме CSRF через формы, которые не требуют CORS). Но SSE endpoint также защищён same-origin.

### SEC-020: Python 3.9-slim — EOL и отсутствие security patches (INFO)

- **Файл:** `Dockerfile:2`
- **Описание:** Python 3.9 вышел из активной поддержки (EOL октябрь 2025). Security-патчи больше не выпускаются.
- **Fix:** Обновить до `python:3.11-slim` или `python:3.12-slim`.

### SEC-021: HTML-шаблоны дублированы в корне проекта (INFO)

- **Файл:** `zones.html`, `status.html`, `programs.html`, `logs.html`, `water.html` в корне
- **Описание:** Дубли или legacy-версии `templates/*.html`. Могут содержать устаревший код без security-фиксов. Не используются Flask (только `templates/`), но могут быть доступны через static serving.

### SEC-022: Нет HSTS / Security Headers (INFO)

- **Описание:** Нет заголовков `Strict-Transport-Security`, `X-Content-Type-Options`, `X-Frame-Options`, `Content-Security-Policy`. На HTTP-only IoT-устройстве в ЛС — некритично.
- **Fix:**
```python
@app.after_request
def add_security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return resp
```

---

## Физическая безопасность

### PHYS-001: Нет аппаратного ограничения максимального времени полива

- **Severity:** HIGH (физический риск)
- **Описание:**
  - Есть software watchdog (`_start_single_zone_watchdog` в app.py:1233-1249) — проверяет exclusive constraint каждую 1 секунду
  - Есть `schedule_zone_cap()` (irrigation_scheduler.py:618) — абсолютный лимит работы зоны (по умолчанию 240 мин)
  - Есть `schedule_zone_hard_stop()` — доп. страховка на точное время
  - **НО**: все эти механизмы — software-only. Если процесс Python крашнется, или контейнер остановится, или OOM-killer убьёт процесс — клапаны останутся в последнем состоянии
  - Wirenboard реле WB-MR6C **не имеют встроенного таймера автоотключения**
  - MQTT-реле при потере связи с брокером **сохраняют последнее состояние** (retain behavior)
- **Impact:** При крахе приложения во время полива — зоны останутся открытыми до ручного вмешательства
- **Рекомендации:**
  1. Использовать Mosquitto `persistent_client_expiration` + retained OFF messages
  2. Настроить systemd WatchdogSec + WatchdogSignal на Wirenboard для перезапуска при зависании
  3. Рассмотреть добавление cron-задачи на WB, которая проверяет состояние реле и отключает все зоны, если wb-irrigation не отвечает >5 минут
  4. Или аппаратный watchdog таймер на реле (если WB-MR6C поддерживает)

### PHYS-002: Можно включить все 24 зоны одновременно

- **Severity:** HIGH (физический риск)
- **Описание:**
  - `exclusive_start_zone()` в `zone_control.py` обеспечивает эксклюзивность **внутри одной группы** — при старте зоны останавливаются другие зоны в той же группе
  - Но если зоны распределены по разным группам — ограничения нет
  - Через MQTT напрямую (SEC-001) — можно открыть все 24 зоны одновременно
  - Через API — если у каждой группы по 1-6 зон, можно запустить по одной зоне в каждой группе одновременно (до 4 зон с 4 контроллерами)
- **Impact:** Перегрузка системы водоснабжения, падение давления, неэффективный полив. При подключении к городскому водопроводу — потенциальное снижение давления у соседей.
- **Рекомендации:**
  1. Добавить глобальный лимит одновременно включённых зон (max_concurrent_zones в settings)
  2. Watchdog: если >N зон ON одновременно → emergency stop

### PHYS-003: Emergency stop — есть, но работает только через software

- **Severity:** MEDIUM
- **Описание:**
  - Emergency stop реализован: `api_emergency_stop()` (app.py:3253) останавливает все зоны во всех группах, ставит флаг `EMERGENCY_STOP=True`
  - SSE hub проверяет флаг и блокирует ON-команды (app.py:~3847)
  - **Но**: emergency stop — только через web UI или API. Нет физической кнопки
  - При полном крахе приложения — emergency stop недоступен
- **Рекомендации:** Добавить физическую кнопку аварийной остановки на Wirenboard (через GPIO), или аппаратный kill-switch на магистральном клапане.

---

## Зависимости (CVE)

Результат `pip-audit -r requirements.txt`:

| Пакет | Версия | CVE | Фикс-версия | Severity |
|-------|--------|-----|-------------|----------|
| Flask | 2.3.3 | CVE-2026-27205 | 3.1.3 | Medium |
| Pillow | 10.0.1 | CVE-2023-50447 | 10.2.0 | High |
| Pillow | 10.0.1 | CVE-2024-28219 | 10.3.0 | Medium |
| requests | 2.32.3 | CVE-2024-47081 | 2.32.4 | Medium |
| requests | 2.32.3 | CVE-2026-25645 | 2.33.0 | Medium |
| aiohttp | 3.9.5 | 10 CVEs | 3.13.3 | High |

**Рекомендация:** Обновить `requirements.txt`:
```
Flask>=3.1.3
Pillow>=10.3.0
requests>=2.33.0
# aiohttp — транзитивная зависимость aiogram; обновить aiogram
aiogram>=3.8.0
```

---

## Рекомендации (приоритизированные)

### P0 — Немедленно (до следующего запуска)

1. **Отключить анонимный MQTT** — `allow_anonymous false` + пароль для mosquitto (SEC-001)
2. **Сгенерировать случайный SECRET_KEY** — сохранить в файл, убрать дефолт (SEC-002)
3. **Зашифровать MQTT-пароли в БД** через `encrypt_secret()` (SEC-003)
4. **Убрать или ограничить гостевой доступ** — guest не должен управлять зонами (SEC-005)

### P1 — В ближайшем релизе

5. **Включить CSRF-защиту** для mutation endpoints (SEC-004)
6. **Rate-limiting по IP** с lockout после 5 попыток (SEC-006)
7. **Сгенерировать случайный IRRIG_SECRET_KEY** вместо hostname-based (SEC-007)
8. **Перевести critical MQTT на QoS 1** — ON/OFF зон и мастер-клапана (SEC-008)
9. **Обновить зависимости** — Flask, Pillow, requests, aiohttp (CVEs)
10. **Аварийный watchdog на уровне ОС** — systemd WatchdogSec, cron-check реле (PHYS-001)

### P2 — При рефакторинге

11. **Non-root в Docker** (SEC-015)
12. **Установить MAX_CONTENT_LENGTH** = 2MB (SEC-017)
13. **Security headers** — X-Content-Type-Options, X-Frame-Options (SEC-022)
14. **Обновить Python** до 3.11+ (SEC-020)
15. **Глобальный лимит одновременных зон** (PHYS-002)
16. **Удалить дублирующие HTML-файлы** из корня проекта (SEC-021)
17. **Минимальная длина пароля** ≥ 8 символов (SEC-009)
