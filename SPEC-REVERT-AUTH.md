# SPEC: Восстановление guest-доступа к управлению зонами/группами/emergency

## Контекст

Сервис wb-irrigation доступен из интернета **только** через nginx с basic auth.
Внутри Flask-приложения НЕ нужна отдельная авторизация для **управления** зонами/группами.

Недавние security-изменения (VULN-001/002/003) закрыли guest-доступ к endpoints управления зонами, что сломало работу садовников — они заходят через nginx basic auth и получают Flask-роль `guest` (без пароля внутри Flask).

## Что НЕ трогаем (оставляем как есть)

| Что | Почему |
|-----|--------|
| MQTT CRUD API (mqtt_api.py) — list/create/update/delete серверов | admin-only, всё правильно |
| Маскировка MQTT паролей | Безопасность паролей |
| Авторизация Telegram бота (routes/telegram.py) | Безопасность |
| `services/security.py` — декораторы | Файл не меняем, просто убираем вызовы декораторов |

---

## Файл 1: `app.py`

### Проблема

`_ALLOWED_PUBLIC_POSTS` — whitelist endpoints доступных без admin. Security-фикс убрал из него endpoints управления зонами/группами. Два места проверяют этот whitelist:
1. `_auth_before_request` (строка ~162): `if session.get('role') != 'admin' and not _is_status_action(pth)`
2. `_require_admin_for_mutations` (строка ~184): `if role != 'admin' and not _is_status_action(p)`

### Текущее значение

```python
_ALLOWED_PUBLIC_POSTS = {'/api/login', '/api/password', '/api/status', '/health', '/api/env', '/api/emergency-stop', '/api/postpone', '/api/zones/next-watering-bulk'}
```

### Нужное значение

```python
_ALLOWED_PUBLIC_POSTS = {'/api/login', '/api/password', '/api/status', '/health', '/api/env', '/api/emergency-stop', '/api/emergency-resume', '/api/postpone', '/api/zones/next-watering-bulk'}
```

**Но этого НЕ достаточно** — пути вида `/api/zones/<id>/mqtt/start`, `/api/groups/<id>/stop` и т.д. содержат dynamic segments и НЕ могут быть в статическом set'е.

### Решение: расширить `_is_status_action()` паттернами

Заменить текущую функцию `_is_status_action`:

```python
# ТЕКУЩИЙ КОД:
_ALLOWED_PUBLIC_POSTS = {'/api/login', '/api/password', '/api/status', '/health', '/api/env', '/api/emergency-stop', '/api/postpone', '/api/zones/next-watering-bulk'}

def _is_status_action(path):
    if path in _ALLOWED_PUBLIC_POSTS:
        return True
    return False
```

**Заменить на:**

```python
import re as _re

_ALLOWED_PUBLIC_POSTS = {'/api/login', '/api/password', '/api/status', '/health', '/api/env', '/api/emergency-stop', '/api/emergency-resume', '/api/postpone', '/api/zones/next-watering-bulk'}

# Patterns for zone/group control endpoints that guests (nginx basic auth users) can access
_ALLOWED_PUBLIC_PATTERNS = [
    _re.compile(r'^/api/zones/\d+/mqtt/start$'),
    _re.compile(r'^/api/zones/\d+/mqtt/stop$'),
    _re.compile(r'^/api/zones/\d+/start$'),
    _re.compile(r'^/api/zones/\d+/stop$'),
    _re.compile(r'^/api/groups/\d+/start-from-first$'),
    _re.compile(r'^/api/groups/\d+/stop$'),
    _re.compile(r'^/api/groups/\d+/master-valve/\w+$'),
    _re.compile(r'^/api/groups/\d+/start-zone/\d+$'),
]

def _is_status_action(path):
    if path in _ALLOWED_PUBLIC_POSTS:
        return True
    for pat in _ALLOWED_PUBLIC_PATTERNS:
        if pat.match(path):
            return True
    return False
```

---

## Файл 2: `routes/zones_watering_api.py`

### Проблема

На 4 endpoints добавлен декоратор `@admin_required`, который делает redirect на login page для non-admin. Это блокирует guest.

### Изменения

**Убрать `@admin_required`** с 4 endpoints:

| Endpoint | Строка | Действие |
|----------|--------|----------|
| `POST /api/zones/<id>/start` — `start_zone()` | строка ~25-26 | Удалить `@admin_required` |
| `POST /api/zones/<id>/stop` — `stop_zone()` | строка ~73-74 | Удалить `@admin_required` |
| `POST /api/zones/<id>/mqtt/start` — `api_zone_mqtt_start()` | строка ~131-133 | Удалить `@admin_required` |
| `POST /api/zones/<id>/mqtt/stop` — `api_zone_mqtt_stop()` | строка ~229-231 | Удалить `@admin_required` |

**Также** удалить неиспользуемый импорт:

```python
# Удалить строку:
from services.security import admin_required
```

**Точные замены:**

1. `start_zone`:
```python
# БЫЛО:
@zones_watering_api_bp.route('/api/zones/<int:zone_id>/start', methods=['POST'])
@admin_required
def start_zone(zone_id):

# СТАЛО:
@zones_watering_api_bp.route('/api/zones/<int:zone_id>/start', methods=['POST'])
def start_zone(zone_id):
```

2. `stop_zone`:
```python
# БЫЛО:
@zones_watering_api_bp.route('/api/zones/<int:zone_id>/stop', methods=['POST'])
@admin_required
def stop_zone(zone_id):

# СТАЛО:
@zones_watering_api_bp.route('/api/zones/<int:zone_id>/stop', methods=['POST'])
def stop_zone(zone_id):
```

3. `api_zone_mqtt_start`:
```python
# БЫЛО:
@zones_watering_api_bp.route('/api/zones/<int:zone_id>/mqtt/start', methods=['POST'])
@rate_limit('mqtt_control', max_requests=10, window_sec=60)
@admin_required
def api_zone_mqtt_start(zone_id: int):

# СТАЛО:
@zones_watering_api_bp.route('/api/zones/<int:zone_id>/mqtt/start', methods=['POST'])
@rate_limit('mqtt_control', max_requests=10, window_sec=60)
def api_zone_mqtt_start(zone_id: int):
```

4. `api_zone_mqtt_stop`:
```python
# БЫЛО:
@zones_watering_api_bp.route('/api/zones/<int:zone_id>/mqtt/stop', methods=['POST'])
@rate_limit('mqtt_control', max_requests=10, window_sec=60)
@admin_required
def api_zone_mqtt_stop(zone_id: int):

# СТАЛО:
@zones_watering_api_bp.route('/api/zones/<int:zone_id>/mqtt/stop', methods=['POST'])
@rate_limit('mqtt_control', max_requests=10, window_sec=60)
def api_zone_mqtt_stop(zone_id: int):
```

---

## Файл 3: `routes/system_emergency_api.py`

### Проблема

`@admin_required` на `api_emergency_resume` блокирует guest-доступ к возобновлению после emergency stop.

### Изменения

**Убрать `@admin_required`** с `api_emergency_resume`:

```python
# БЫЛО:
@system_emergency_api_bp.route('/api/emergency-resume', methods=['POST'])
@rate_limit('emergency', max_requests=5, window_sec=60)
@admin_required
def api_emergency_resume():

# СТАЛО:
@system_emergency_api_bp.route('/api/emergency-resume', methods=['POST'])
@rate_limit('emergency', max_requests=5, window_sec=60)
def api_emergency_resume():
```

**Также** удалить неиспользуемый импорт:

```python
# Удалить строку:
from services.security import admin_required
```

> **Примечание:** `api_emergency_stop` уже БЕЗ `@admin_required` — это правильно (fail-safe: любой может остановить).

---

## Файл 4: `routes/groups_api.py`

### Проблема

`@admin_required` на 3 endpoints:
- `api_stop_group` — остановка всех зон группы
- `api_start_group_from_first` — запуск последовательного полива
- `api_master_valve_toggle` — управление мастер-клапаном

### Изменения

**Убрать `@admin_required`** с 3 endpoints:

1. `api_stop_group`:
```python
# БЫЛО:
@groups_api_bp.route('/api/groups/<int:group_id>/stop', methods=['POST'])
@admin_required
def api_stop_group(group_id):

# СТАЛО:
@groups_api_bp.route('/api/groups/<int:group_id>/stop', methods=['POST'])
def api_stop_group(group_id):
```

2. `api_start_group_from_first`:
```python
# БЫЛО:
@groups_api_bp.route('/api/groups/<int:group_id>/start-from-first', methods=['POST'])
@admin_required
def api_start_group_from_first(group_id):

# СТАЛО:
@groups_api_bp.route('/api/groups/<int:group_id>/start-from-first', methods=['POST'])
def api_start_group_from_first(group_id):
```

3. `api_master_valve_toggle`:
```python
# БЫЛО:
@groups_api_bp.route('/api/groups/<int:group_id>/master-valve/<action>', methods=['POST'])
@admin_required
def api_master_valve_toggle(group_id, action):

# СТАЛО:
@groups_api_bp.route('/api/groups/<int:group_id>/master-valve/<action>', methods=['POST'])
def api_master_valve_toggle(group_id, action):
```

**НЕ трогаем** (оставляем БЕЗ `@admin_required`, как уже есть):
- `api_groups` (GET) — уже открыт
- `api_update_group` (PUT) — нет `@admin_required`, но защищён middleware в app.py (требует admin для PUT). **Оставляем как есть** — редактирование групп не является status action.
- `api_create_group` (POST) — нет `@admin_required`, защищён middleware. **Оставляем как есть** — создание групп не status action.
- `api_delete_group` (DELETE) — аналогично.
- `api_start_zone_exclusive` — уже БЕЗ `@admin_required`, но защищён middleware. Нужно **добавить паттерн** в `_ALLOWED_PUBLIC_PATTERNS` (уже добавлен выше).

**Также** удалить неиспользуемый импорт (после удаления всех `@admin_required`):

```python
# Удалить строку:
from services.security import admin_required
```

---

## Сводная таблица: Финальное состояние endpoints

### Открыты для guest (без admin):

| Endpoint | Метод | Файл | Механизм |
|----------|-------|------|----------|
| `/api/zones/<id>/mqtt/start` | POST | zones_watering_api.py | Убран `@admin_required` + паттерн в `_is_status_action` |
| `/api/zones/<id>/mqtt/stop` | POST | zones_watering_api.py | Убран `@admin_required` + паттерн |
| `/api/zones/<id>/start` | POST | zones_watering_api.py | Убран `@admin_required` + паттерн |
| `/api/zones/<id>/stop` | POST | zones_watering_api.py | Убран `@admin_required` + паттерн |
| `/api/groups/<id>/start-from-first` | POST | groups_api.py | Убран `@admin_required` + паттерн |
| `/api/groups/<id>/stop` | POST | groups_api.py | Убран `@admin_required` + паттерн |
| `/api/groups/<id>/master-valve/<action>` | POST | groups_api.py | Убран `@admin_required` + паттерн |
| `/api/groups/<id>/start-zone/<id>` | POST | groups_api.py | Уже без `@admin_required` + паттерн |
| `/api/emergency-stop` | POST | system_emergency_api.py | Уже в `_ALLOWED_PUBLIC_POSTS` |
| `/api/emergency-resume` | POST | system_emergency_api.py | Убран `@admin_required` + добавлен в `_ALLOWED_PUBLIC_POSTS` |
| `/api/postpone` | POST | system_config_api.py | Уже в `_ALLOWED_PUBLIC_POSTS` |
| `/api/status` | POST/GET | — | Уже в `_ALLOWED_PUBLIC_POSTS` |
| Все GET `/api/*` | GET | app.py | Middleware пропускает GET |

### Остаются admin-only:

| Endpoint | Метод | Файл | Почему |
|----------|-------|------|--------|
| `/api/mqtt/servers` | GET/POST | mqtt_api.py | MQTT CRUD |
| `/api/mqtt/servers/<id>` | PUT/DELETE | mqtt_api.py | MQTT CRUD |
| `/api/mqtt/servers/<id>/test` | POST | mqtt_api.py | MQTT CRUD |
| `/api/mqtt/all-topics` | POST | mqtt_api.py | MQTT диагностика |
| `/api/zones` | POST | zones_crud_api.py | Создание зон |
| `/api/zones/<id>` | PUT/DELETE | zones_crud_api.py | Редактирование/удаление зон |
| `/api/groups` | POST | groups_api.py | Создание групп |
| `/api/groups/<id>` | PUT/DELETE | groups_api.py | Редактирование/удаление групп |
| `/api/programs/*` | POST/PUT/DELETE | programs_api.py | Управление программами |
| `/api/settings/*` | POST/PUT | system_config_api.py | Настройки системы |
| `/api/backup` | POST | system_emergency_api.py | Бэкап |

---

## Порядок применения

1. **app.py** — расширить `_is_status_action()` с regex-паттернами
2. **routes/zones_watering_api.py** — убрать 4× `@admin_required`, убрать импорт
3. **routes/system_emergency_api.py** — убрать 1× `@admin_required`, убрать импорт
4. **routes/groups_api.py** — убрать 3× `@admin_required`, убрать импорт

## Важно: двойная защита

Убирание `@admin_required` **недостаточно** само по себе — middleware `_require_admin_for_mutations` в app.py **также** блокирует non-admin POST запросы. Поэтому **оба** изменения нужны:
1. Расширение `_is_status_action()` — чтобы middleware пропускал эти пути
2. Удаление `@admin_required` — чтобы blueprint-level декоратор не блокировал

Без обоих изменений guest всё равно получит 401/403 или redirect.
