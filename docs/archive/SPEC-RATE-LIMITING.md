# SPEC: Rate Limiting & Brute-Force Protection

**Status:** Draft  
**Date:** 2026-04-03  
**Context:** Сервис wb-irrigation доступен из интернета через nginx reverse proxy (`poliv-kg.ops-lab.dev`). Nginx имеет basic auth, но brute-force на basic auth — отдельный вектор (решается на стороне nginx). Этот документ — про защиту Flask-приложения.

---

## 1. Текущее состояние

### ✅ Login rate limiting — РЕАЛИЗОВАНО, РАБОТАЕТ

**Файл:** `services/rate_limiter.py`  
**Где используется:** `routes/auth.py` → `POST /api/login`

| Параметр | Значение | Источник |
|---|---|---|
| `LOGIN_MAX_ATTEMPTS` | 5 | `constants.py` |
| `LOGIN_WINDOW_SEC` | 300 (5 мин) | `constants.py` |
| `LOGIN_LOCKOUT_SEC` | 900 (15 мин) | `constants.py` |

**Механизм:** Sliding window по IP. После 5 неудачных попыток за 5 минут — lockout на 15 минут. Успешный логин сбрасывает счётчик. Thread-safe (threading.Lock). In-memory singleton `login_limiter`.

**Оценка:** ✅ Хорошая реализация. Достаточные лимиты для brute-force protection на уровне Flask. 5 попыток / 5 мин с 15-минутным lockout — разумный баланс.

### ✅ API rate limiting (per-endpoint) — РЕАЛИЗОВАНО, РАБОТАЕТ

**Файл:** `services/api_rate_limiter.py`  
**Декоратор:** `@rate_limit(group, max_requests, window_sec)`

| Endpoint | Group | Лимит | Окно |
|---|---|---|---|
| `POST /api/emergency-stop` | `emergency` | 5 req | 60s |
| `POST /api/emergency-resume` | `emergency` | 5 req | 60s |
| `POST /api/zones/<id>/mqtt/start` | `mqtt_control` | 10 req | 60s |
| `POST /api/zones/<id>/mqtt/stop` | `mqtt_control` | 10 req | 60s |
| `POST /api/programs` | `programs` | 20 req | 60s |
| `PUT /api/programs/<id>` | `programs` | 20 req | 60s |
| `DELETE /api/programs/<id>` | `programs` | 10 req | 60s |
| `PUT /api/programs/<id>/enabled` | `programs` | 20 req | 60s |

**Механизм:** Per-IP, per-group sliding window. Periodic pruning каждые 500 вызовов. Возвращает HTTP 429 + `Retry-After` header. Пропускает в TESTING режиме.

**Оценка:** ✅ Покрытие критических мутирующих endpoints. Лимиты разумные.

### ✅ General mutation rate limiting — РЕАЛИЗОВАНО, РАБОТАЕТ

**Файл:** `app.py` → `_general_api_rate_limit()` (before_request)

Все `POST/PUT/DELETE /api/*` endpoints, НЕ покрытые отдельными декораторами, ограничены **30 req/min per IP**.

**Исключения (skip):** `/api/login`, `/api/password`, `/api/status`, `/health`, `/api/env`, плюс endpoints с собственными декораторами.

**Оценка:** ✅ Хороший catch-all. Но `/api/login` исключён из general limiter — это правильно, у login свой limiter.

### ⚠️ SSE connections — ЧАСТИЧНО

**Файл:** `services/sse_hub.py`

- `MAX_SSE_CLIENTS = 5` — жёсткий лимит на количество одновременных SSE-клиентов (zones-sse hub).
- `zones-sse` endpoint **ОТКЛЮЧЁН** — возвращает 204 No Content (фронтенд использует polling).
- `scan-sse` (`/api/mqtt/<id>/scan-sse`) — **АКТИВЕН**, защищён `@admin_required`, автоматический disconnect через 300 секунд.

**Оценка:** ⚠️ Scan-SSE не имеет rate limiting и connection limit per IP. Но требует admin session, так что вектор атаки минимален.

### ❌ Nginx basic auth brute-force — НЕ ЗАЩИЩЁН на уровне приложения

Nginx basic auth — это первый барьер перед Flask. Brute-force на него — отдельный вектор, который решается на стороне nginx (fail2ban / `limit_req`), а не в Flask-приложении.

---

## 2. Матрица угроз и покрытия

| Вектор атаки | Текущая защита | Статус | Приоритет |
|---|---|---|---|
| Brute-force Flask login | `LoginRateLimiter` (5/5min, lockout 15min) | ✅ | — |
| DoS на мутирующие API | Per-endpoint + general limiter | ✅ | — |
| Brute-force nginx basic auth | Нет (нужен nginx `limit_req` / fail2ban) | ❌ | **P1** |
| SSE connection flood (scan) | `@admin_required` + 300s timeout | ⚠️ | P3 |
| Slowloris / connection exhaustion | Нет (Hypercorn limits) | ⚠️ | P3 |

---

## 3. Что нужно добавить / исправить

### 3.1 [P1] Nginx: `limit_req` для basic auth brute-force

**Проблема:** Nginx basic auth не имеет rate limiting. Атакующий может бесконечно перебирать credentials на уровне nginx, до Flask не дойдёт даже.

**Решение:** Добавить `limit_req_zone` в nginx конфигурацию на сервере `poliv-kg.ops-lab.dev`.

```nginx
# В http {} блоке:
limit_req_zone $binary_remote_addr zone=auth:1m rate=5r/m;

# В location / (перед proxy_pass):
limit_req zone=auth burst=3 nodelay;
limit_req_status 429;
```

**Лимиты:** 5 requests/minute per IP с burst=3. Это значит: первые 3 запроса пройдут мгновенно, потом — не чаще 1 каждые 12 секунд. Достаточно для легитимного использования (1-2 логина), но brute-force невозможен.

> ⚠️ Это изменение на стороне nginx, не в репозитории Flask-приложения. Документируем здесь для полноты.

### 3.2 [P2] Scan-SSE: добавить connection limit per IP

**Проблема:** `/api/mqtt/<id>/scan-sse` не ограничивает количество одновременных SSE-соединений с одного IP. Хотя endpoint требует admin session, один авторизованный пользователь (или украденная сессия) может открыть множество соединений и исчерпать ресурсы ARM-контроллера.

**Решение:** Добавить per-IP connection counter в `routes/mqtt_api.py`.

**Было:**
```python
@mqtt_api_bp.route('/api/mqtt/<int:server_id>/scan-sse')
@admin_required
def api_mqtt_scan_sse(server_id: int):
    """Stream MQTT messages as SSE for continuous scanning."""
    try:
        from flask import current_app
        server = db.get_mqtt_server(server_id)
        if not server:
            return api_error('MQTT_SERVER_NOT_FOUND', 'server not found', 404)
```

**Стало:**
```python
import threading

_scan_sse_connections: dict[str, int] = {}  # {ip: active_count}
_scan_sse_lock = threading.Lock()
MAX_SCAN_SSE_PER_IP = 2


@mqtt_api_bp.route('/api/mqtt/<int:server_id>/scan-sse')
@admin_required
def api_mqtt_scan_sse(server_id: int):
    """Stream MQTT messages as SSE for continuous scanning."""
    ip = request.remote_addr or '0.0.0.0'

    # Connection limit per IP
    with _scan_sse_lock:
        current = _scan_sse_connections.get(ip, 0)
        if current >= MAX_SCAN_SSE_PER_IP:
            return api_error('SSE_LIMIT', 'Too many SSE connections', 429)
        _scan_sse_connections[ip] = current + 1

    try:
        from flask import current_app
        server = db.get_mqtt_server(server_id)
        if not server:
            with _scan_sse_lock:
                _scan_sse_connections[ip] = max(0, _scan_sse_connections.get(ip, 1) - 1)
            return api_error('MQTT_SERVER_NOT_FOUND', 'server not found', 404)
```

И в `_gen()` generator, в `finally`:
```python
        @stream_with_context
        def _gen():
            try:
                # ... existing code ...
            finally:
                stop_event.set()
                with _scan_sse_lock:
                    _scan_sse_connections[ip] = max(0, _scan_sse_connections.get(ip, 1) - 1)
```

### 3.3 [P3] Password change endpoint rate limiting

**Проблема:** `/api/password` исключён из general rate limiter (`skip_paths`). Если у этого endpoint нет собственного rate limiting, атакующий (авторизованный) может спамить сменой пароля.

**Проверить:** есть ли `@rate_limit` декоратор на `/api/password`. Если нет — добавить:

```python
@rate_limit('password_change', max_requests=3, window_sec=300)
```

---

## 4. Сводная таблица лимитов (целевое состояние)

| Layer | Endpoint / Zone | Лимит | Механизм |
|---|---|---|---|
| **Nginx** | Все запросы к basic auth | 5 req/min, burst=3 | `limit_req_zone` |
| **Flask login** | `POST /api/login` | 5 failures / 5 min → lockout 15 min | `LoginRateLimiter` |
| **Flask API** | Emergency endpoints | 5 req/min per IP | `@rate_limit` decorator |
| **Flask API** | MQTT zone control | 10 req/min per IP | `@rate_limit` decorator |
| **Flask API** | Programs CRUD | 10-20 req/min per IP | `@rate_limit` decorator |
| **Flask API** | All other mutations | 30 req/min per IP | `before_request` hook |
| **Flask API** | Password change | 3 req/5 min per IP | `@rate_limit` decorator (**NEW**) |
| **Flask SSE** | Scan SSE | 2 concurrent per IP | Connection counter (**NEW**) |

---

## 5. Что НЕ нужно делать

1. **Redis** — ARM-контроллер, in-memory достаточно. Процесс один, state не шарится.
2. **Distributed rate limiting** — одна нода, один процесс.
3. **CAPTCHA** — API-сервис для IoT, нет UI для капчи.
4. **WAF** — overkill для данного масштаба.
5. **Менять существующие лимиты** — текущие значения разумны и протестированы.

---

## 6. Рекомендации по мониторингу

1. **Логирование 429 ответов** — уже есть (api_rate_limiter логирует через стандартный logger). Убедиться что логи ротируются на ARM.
2. **Nginx access.log** — анализировать на предмет массовых 401 (basic auth failures). Можно через простой cron + grep.
3. **fail2ban** (опционально) — если nginx `limit_req` недостаточно, fail2ban может банить IP на firewall уровне после N неудачных попыток basic auth.

---

## 7. Порядок внедрения

1. **[Nginx]** Добавить `limit_req` на `poliv-kg.ops-lab.dev` — P1, самый важный вектор
2. **[Flask]** Добавить connection limit на scan-sse — P2, минимальные изменения
3. **[Flask]** Проверить и добавить rate limit на `/api/password` — P3
4. **[Ops]** Настроить мониторинг 429/401 в логах — P3

Все изменения в Flask — in-memory, без внешних зависимостей, без breaking changes.
