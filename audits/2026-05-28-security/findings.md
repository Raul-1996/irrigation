# Security & QA Audit Findings — wb-irrigation v2 (2026-05-28)

Дата: 2026-05-28
Источник: 2 параллельных аудит-агента (security audit + QA review of PR #53, #54, #55, #56).
Контекст: приложение опубликовано в интернет через Cloudflare Tunnel (poliv-gub.ops-lab.dev) + Cloudflare Worker basic auth (Poliv/Poliv). Внутри LAN — прямой доступ по IP.

## Решения Рауля по фиксам (2026-05-28)

1. Лимит фото — **30 MB** на файл, только для авторизированных пользователей
2. Дефолтные креды Poliv/Poliv — **оставляем как есть** (LAN-сценарий)
3. `_PENDING_CLOSE_TIMERS.pop()` фикс — да
4. **LAN bypass**: если запрос пришёл из 10.0.0.0/8 (прямой заход по IP), auth НЕ нужен. Если запрос пришёл через CF Tunnel (CF-Connecting-IP присутствует) — auth обязателен, даже если в CF-Connecting-IP лежит LAN-адрес (это spoof).
5. Rate limit по подбору пароля — **per-IP**, не per-username, с прогрессивной задержкой + алёрт в Telegram при превышении порога.

---

## Pipeline A — Hardware safety (PR #57 кандидат)

### A1. DoS через decompression bomb — `img.load()` до проверки размера (CRITICAL)
**Файл:** `services/image_pipeline.py:46-52`
**Симптом:** PNG/TIFF с фейковым IHDR 50000×50000 декодируется в RAM до проверки `w*h`. На armv7 с 1 GB RAM это OOM-kill (~7.5 GB на декод RGBA).
**Уже в main, уже доступно из интернета через CF.**

**Fix:**
1. После `Image.open(io.BytesIO(file_data))` — сразу проверить `img.size` (заголовок уже распарсен) **до** `img.load()`. Если `w*h > MAX_INPUT_PIXELS` → raise `ImageTooLargeError`.
2. На уровне модуля выставить `Image.MAX_IMAGE_PIXELS = MAX_INPUT_PIXELS` глобально, чтобы Pillow сам подстраховал.
3. Перевести `DecompressionBombWarning` в error: `warnings.simplefilter("error", Image.DecompressionBombWarning)` или ловить и реврейзить.

### A2. `DecompressionBombError` уходит как 500 (CRITICAL)
**Файл:** `routes/system_config_api.py:155-170` (`api_map`)
**Симптом:** `except (OSError, ValueError)` НЕ ловит `Image.DecompressionBombError` (это подкласс `Exception`) → 500 + traceback в логах вместо 413/415.

**Fix:** добавить `Image.DecompressionBombError` (и `ImageTooLargeError`, если не наследник `ValueError`) в `except`. Аналогично в `routes/zones_photo_api.py` для upload-эндпоинтов.

### A3. Лимит 30 MB на multipart upload (по требованию Рауля)
**Файлы:** `config.py`, эндпоинты загрузки изображений
**Fix:** `app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024`. Flask автоматически отрежет запрос >30 MB c 413 ДО того, как мы прочитаем его в память.
Если уже есть `MAX_CONTENT_LENGTH` — синхронизировать значение.

### A4. Эндпоинты загрузки фото — только auth
**Файлы:** `routes/system_config_api.py` (api_map), `routes/zones_photo_api.py` (upload)
**Fix:** убедиться что эти эндпоинты под `_auth_before_request` и НЕ попадают в `_PUBLIC_PATHS` / `_ALLOWED_PUBLIC_PATTERNS`. Сейчас в `app.py:462-464` `if request.method == "GET": return None` — это надо снять в Pipeline B, но загрузка — POST, так что не покрыта. Проверить.

### A5. Map upload: TOCTOU + неатомарная запись (HIGH)
**Файл:** `routes/system_config_api.py:140-160`
**Симптом:** имя `zones_map_{int(time.time())}.webp` — две загрузки в одну секунду перетирают друг друга. `with open(...): f.write(...)` — частично записанный файл при kill/full disk остаётся валидным URL.

**Fix:** `tempfile.NamedTemporaryFile(dir=upload_dir, delete=False)` → `os.fsync(f.fileno())` → `os.replace(tmp.name, final_path)`. Имя — `uuid4().hex` или `secrets.token_hex(16)`.

### A6. `_PENDING_CLOSE_TIMERS` никогда не очищается → watchdog supervisor заблокирован (CRITICAL)
**Файл:** `services/zone_control.py:129-211` (`_do_close`)
**Симптом:** таймер кладётся в dict (`_PENDING_CLOSE_TIMERS[topic] = t`), но НИ ОДИН return-путь `_do_close` не делает `pop()`. Watchdog в `services/watchdog.py:239-241` видит stale-pending → НИКОГДА не вмешается в сценарии "MQTT флапнул, master valve залип открытым".

**Fix:**
```python
# в finally _do_close (или в конце try-блока):
try:
    cached = _PENDING_CLOSE_TIMERS.get(topic)
    if cached is t:    # не вытирай чужой свежий таймер
        _PENDING_CLOSE_TIMERS.pop(topic, None)
except Exception:
    pass
```

### A7. После рестарта процесса (SIGKILL/oom) supervisor бездействует (CRITICAL)
**Файл:** `services/watchdog.py:_check_master_valves` (≈220-260)
**Симптом:** `any_active` определяется через `zones.state='on'` в БД. zone_control пишет `state='on'` ДО физического подтверждения; при SIGKILL/oom значение остаётся. После рестарта supervisor видит "есть активные зоны" → решает что master valve должен быть открыт → ничего не закрывает. Master valve лежит открытым до cap_minutes (4 часа).

**Fix:** на старте watchdog'а сделать stale-zone cleanup: если `state='on'` AND `started_at < now() - max_duration` → пометить как off. Либо считать `any_active` через "есть thread-планировщик который реально гонит зону".

### A8. `_run_program_threaded` finally может закрыть пустоту (HIGH)
**Файл:** `irrigation_scheduler.py:1105-1148`
**Симптом:** `program_gids = set()` собирается ВНУТРИ try; если `_collect_program_groups(program)` падает после первого `_smc(g, open=True)`, finally думает "программа ничего не открывала" → master valve остаётся открытым.

**Fix:** собирать `gids_to_close` инкрементально по мере открытия групп. Либо вынести `_smc(g, open=True)` ВНУТРЬ того же try-блока (тут уже не подходит — open уже делается там).

### A9. Watchdog MQTT publish блокирует тик (HIGH)
**Файл:** `services/watchdog.py`
**Симптом:** на отвалившемся брокере `paho-mqtt.publish` блокирует до ~45s. Watchdog single-threaded → следующая итерация не происходит в самый критичный момент.

**Fix:** либо явный `timeout=2` на publish, либо проверка состояния клиента до publish, либо отдельный thread на сам MQTT.

### A10. Plaintext temp password в логах (CRITICAL — попутно)
**Файл:** `db/settings.py:59`
```python
logger.warning("Initial random password generated: %s", temp_password)
```
**Fix:** убрать `%s`, логировать только факт генерации.

### A11. Pre-existing bug rotate в zones_photo (MEDIUM — попутно)
**Файл:** `routes/zones_photo_api.py:388-397`
```python
img = img.rotate(-angle, expand=True)
fmt = (img.format or "JPEG").upper()  # rotate возвращает новый объект без .format
```
**Fix:** запомнить `original_format = img.format` ДО rotate, использовать его.

---

## Pipeline B — Auth & rate limit (PR #58 кандидат)

### B1. LAN bypass authentication (по требованию Рауля)
**Файл:** `app.py:_auth_before_request` (≈446-470)
**Логика:**
1. Если есть `request.headers.get("CF-Connecting-IP")` (или `X-Forwarded-For`) — запрос пришёл через CF Tunnel. **Auth обязателен**, не доверять заголовку для LAN-проверки.
2. Если CF-Connecting-IP **отсутствует** AND `request.remote_addr` в `10.0.0.0/8` → LAN-сценарий, **пропустить auth**.
3. Иначе (CF присутствует ИЛИ remote_addr не LAN) → auth обязателен.

**Реализация:**
```python
def _is_lan_request() -> bool:
    if request.headers.get("CF-Connecting-IP"):
        return False
    if request.headers.get("X-Forwarded-For"):
        return False
    addr = request.remote_addr
    if not addr:
        return False
    try:
        return ipaddress.ip_address(addr) in ipaddress.ip_network("10.0.0.0/8")
    except ValueError:
        return False
```

### B2. ProxyFix с trusted_proxy env (CRITICAL)
**Файлы:** `app.py` (init), `routes/auth.py:55, 116, 153`, `services/audit.py:_resolve_ip`
**Симптом:** сейчас `request.remote_addr` за nginx/CF читает IP туннеля → rate limit и audit log сломаны. Без ProxyFix любой может подделать `X-Forwarded-For`.

**Fix:**
```python
# app.py при инициализации:
import os
from werkzeug.middleware.proxy_fix import ProxyFix
if os.getenv("TRUSTED_PROXY") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
```
В docker-compose / env проставить `TRUSTED_PROXY=1` для prod (с CF Tunnel). Без env — НЕ читать X-Forwarded-*.

### B3. GET /api/* публично читаем (CRITICAL)
**Файл:** `app.py:462-464`
```python
if request.method == "GET":
    return None
```
**Симптом:** `/api/zones`, `/api/programs`, `/api/system/config`, фото зон — отдаются без auth любому в LAN. Зачем это нужно — для guest viewer. После B1 не нужно (LAN и так без auth).

**Fix:** убрать early return на GET. Если есть legacy GUI-endpoints, которые должны быть public — добавить в `_PUBLIC_PATHS` explicit set (health-check, login). Не открывать весь GET.

### B4. Rate limit per-IP с прогрессивной задержкой (по требованию Рауля)
**Файл:** `services/rate_limiter.py`, `routes/auth.py`
**Текущая проблема:**
- Per-username lockout (`LOGIN_USERNAME_MAX` ≈ 10/час) → DoS-вектор для `admin`.
- `ip = request.remote_addr or "0.0.0.0"` сломан за CF (см. B2).

**Fix:**
1. Убрать per-username bucket.
2. Per-IP rate limit (sliding window): 5/мин, 20/час.
3. После 5 fail с IP — `time.sleep(2)` перед ответом; после 10 fail — `time.sleep(5)`.
4. После 20 fail/час с IP — алёрт в Telegram (через существующий audit-hook + Telegram bot endpoint).
5. IP брать из `request.remote_addr` (после ProxyFix).

**Алёрт в Telegram:** новый модуль `services/security_alerts.py` или хук в audit. Endpoint бота для алертов уже есть (см. `/opt/claude-agents/main` инфраструктуру) — взять ENV `TELEGRAM_ALERT_URL`. Если env пуст — только лог.

### B5. SESSION_COOKIE_SECURE=True по дефолту (CRITICAL)
**Файл:** `config.py:~50`
**Симптом:** дефолт False → cookie уедет открытым текстом если кто-то зайдёт по http://.
**Fix:** `SESSION_COOKIE_SECURE = True` по дефолту, отключение только через явный env `SESSION_COOKIE_SECURE=0`. Плюс `SESSION_COOKIE_SAMESITE = "Lax"`.

### B6. CSRF time_limit (HIGH)
**Файл:** `config.py:54`
**Симптом:** `WTF_CSRF_TIME_LIMIT = None` + 365-дневная сессия → украл token раз = стреляешь год.
**Fix:** `WTF_CSRF_TIME_LIMIT = 86400` (24 часа).

### B7. PERMANENT_SESSION_LIFETIME — сократить (HIGH)
**Файл:** `config.py`
**Симптом:** 365 дней — это не безопасность, это удобство. Cookie уехал — год доступа.
**Fix:** `PERMANENT_SESSION_LIFETIME = timedelta(days=30)`. Если Раул хочет дольше — обсудим.

### B8. User enumeration через timing (HIGH)
**Файл:** `services/users_service.py:71-85` (`authenticate`)
**Симптом:** `if user is None: return None` — ~5ms vs `check_password_hash` ~25ms на armv7.
**Fix:** при `user is None` всё равно вызвать `check_password_hash` с фиксированным dummy-хешем (см. werkzeug docs).

### B9. Stored XSS в admin_users.html (HIGH)
**Файл:** `templates/admin_users.html:61`
**Симптом:** `${u.username}` инжектится через `innerHTML` без escape. Имя `<img src=x onerror=alert(1)>` → XSS у админа.
**Fix:** использовать `document.createElement` + `.textContent`, либо явный escape.

### B10. Legacy /api/password — удалить (HIGH)
**Файл:** `routes/system_config_api.py:81`
**Симптом:** пишет в `settings.password_hash`, который после PR #56 никем не проверяется — dead endpoint но активный.
**Fix:** удалить функцию + route. (Если боишься — 410 Gone.)

### B11. Audit log принимает spoofed X-Forwarded-For (HIGH)
**Файл:** `services/audit.py:214-222` (`_resolve_ip`)
**Симптом:** читает X-Forwarded-For всегда.
**Fix:** после B2 (ProxyFix) `request.remote_addr` уже содержит правильный IP. Убрать ручное чтение X-Forwarded-For.

### B12. `_resolve_username_from_payload` fallback на "admin" (MEDIUM)
**Файл:** `routes/auth.py:95-101`
**Симптом:** пустой username → подставляется "admin" → rate-limit на `admin` срабатывает от каждого сканера.
**Fix:** после B4 (убран per-username bucket) проблема исчезает. Но всё равно — пустой username вернуть 400 "username required".

### B13. Миграция seed_default_users не в транзакции (MEDIUM)
**Файл:** `db/migrations.py:_migrate_seed_default_users`
**Fix:** обернуть в `with conn:`.

### B14. CSV injection в экспорте zones_history (HIGH — из security audit)
**Файл:** `routes/zones_history_api.py:380`
**Симптом:** zone.name экспортируется в CSV без escape `=+-@\t\r` prefixes → формула в Excel.
**Fix:** утилита `_csv_safe(value)` — если начинается с опасного символа, префикс `'`.

---

## Что НЕ делаем в этой итерации (отдельно, потом)

- Дефолтные креды Poliv/Poliv (решение Рауля)
- Унификация first-start init между PR #55 и PR #56
- Переименование `_smc` → `_set_master_close`
- Audit log dedup при флапе watchdog
- Watchdog jitter ±0.3s для master_valve_interval
- pbkdf2 → bcrypt/argon2 для armv7 (это огромная работа)

---

## Файлы — справка для Senior'ов

Репо локально: `/opt/claude-agents/irrigation` (origin: github.com/Raul-1996/irrigation, branch main = 5adf1fb).

Существующие открытые PR:
- PR #54 (`fix/issue-51-combined`): master valve safety net — текущая работа НЕ дотягивает до фиксов A6/A7/A8/A9. Pipeline A должен extend эту ветку (cherry-pick поверх ИЛИ branch off fix/issue-51-combined).
- PR #56 (`fix/issue-52-senior-2`): in-app auth. Pipeline B должен extend эту ветку.

Стратегия для каждого Senior:
1. Создать новую ветку от целевого base (main для A1-A5/A10/A11, fix/issue-51-combined для A6-A9, fix/issue-52-senior-2 для B).
2. Реализовать ВСЕ пункты своего pipeline в одной серии коммитов (по одному коммиту на логический фикс).
3. Тесты pytest для каждого критичного пункта.
4. Не трогать ничего сверх scope (Karpathy: Surgical Changes).
5. Финальный коммит: обновить CHANGELOG.md (если есть) или добавить раздел в README.

Тесты обязательны для:
- A1: фейковый PNG-bomb (~50KB заголовок 50000×50000) → `ImageTooLargeError` без аллокации (через `tracemalloc.get_traced_memory()`).
- A3: запрос 31 MB → 413; 29 MB ОК.
- A6: после `_do_close` → `_PENDING_CLOSE_TIMERS[topic]` отсутствует.
- A7: симуляция stale zones.state='on' с старым started_at → cleanup при старте watchdog.
- B1: запрос с CF-Connecting-IP → требует auth даже от 10.0.0.0/8. Запрос без заголовков с remote_addr 10.x → skip auth.
- B3: GET /api/zones без auth → 401.
- B4: 11 fail-логинов с одного IP → последний с задержкой >5s.
- B5: SESSION_COOKIE_SECURE=True по дефолту в test_config.
