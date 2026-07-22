# Audit Logging Expansion — Architectural Plan

**Author:** Backend Architect
**Date:** 2026-05-07
**Status:** Plan only (no code changes yet)
**Scope:** Расширение существующего audit-логирования на полное покрытие mutations, state-машины зон/групп, MQTT и scheduler. Без переписывания того, что уже работает.

---

## 0. TL;DR

Бóльшая часть инфраструктуры **уже реализована**:

- Таблица `audit_log` со схемой и индексами (миграция `_migrate_create_audit_log`, файл `db/migrations.py:969`).
- Декоратор `@audit_log(...)` и helper `record_audit(...)` в `services/audit.py` с redact / actor / ip / duration_ms.
- Endpoints `/api/audit`, `/api/audit/types`, `/api/audit/ui` (`routes/audit_api.py`).
- Frontend recorder `static/js/audit.js` с `[data-audit-action]`-делегированием.
- Debug toggle: ключ `logging.debug` в таблице `settings`, endpoint `POST /api/logging/debug`, runtime-toggle уровня root logger, **auto-off через APScheduler**, UI чекбокс уже есть в `templates/logs.html` (стр. 340-364).
- Ротация audit_log: APScheduler-job `audit_cleanup` ежедневно в **03:30**, 7 дней (`irrigation_scheduler.py:166`, `:388`). Самозапись факта чистки — есть.

Что **реально нужно добавить**:

1. Декоратор на ~6-7 mutation endpoints без покрытия (utility/bulk-проверки).
2. `record_audit` на переходы state-машины зон в `services/zone_control.py` (через единую обёртку над `_versioned_update`).
3. `debug_audit(...)` helper и условные emit'ы для MQTT publish / scheduled timers (только при `logging.debug=true`) — чтобы не засорять audit_log в обычном режиме.
4. Замена ~30 ключевых `logger.debug("Exception in ...")` на `logger.exception(...)` в горячих местах (zone_control, mqtt_pub, sse_hub, scheduler).
5. Опционально вынести debug-чекбокс на `templates/settings.html` (сейчас он в /logs — допустимо, но Рауль просил на /settings).
6. Допокрытие UI кнопок `data-audit-action`: settings.html, mqtt.html, map.html, login.html — там их сейчас нет.

---

## 1. Текущее состояние — что уже есть

### 1.1 База данных

**Файл:** `db/migrations.py:969-998` — миграция `create_audit_log`.

```sql
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actor TEXT,
    source TEXT NOT NULL,         -- 'api'|'ui'|'scheduler'|'mqtt'|...
    action_type TEXT NOT NULL,    -- snake_case
    target TEXT,                  -- 'zone:5', 'group:3', ...
    payload_json TEXT,
    result TEXT,                  -- 'success:200'|'failure:400'|'error'|'click'
    error_msg TEXT,
    ip TEXT,
    duration_ms INTEGER
);
CREATE INDEX idx_audit_log_ts;
CREATE INDEX idx_audit_log_action;
CREATE INDEX idx_audit_log_target;
```

Repository: `db/audit.py` (`AuditRepository`) — `add_audit`, `get_audit_logs`, `count_audit_logs`, `cleanup_audit_logs`, `get_distinct_action_types`. Доступ через `database.db.audit.*` (proxy в `database.py:324`).

### 1.2 Сервисный слой

**Файл:** `services/audit.py`

| Объект | Назначение | Где живёт |
|---|---|---|
| `audit_log(action_type, target_extractor, payload_filter, source, skip_methods)` | Декоратор для роутов | строка 154 |
| `record_audit(action_type, source, target, payload, result, error, actor, duration_ms)` | Прямой emit | строка 251 |
| `_redact(value)` | Маскирует `password/token/secret/...`, обрезает строки до 1024 символов, словари до 64 ключей | строка 52 |
| `_resolve_actor(req)` | Берёт `session['user']` или роль, иначе `'guest'` | строка 112 |
| `_resolve_ip(req)` | `X-Forwarded-For` → `remote_addr` | строка 127 |
| `_extract_payload(req)` | Собирает body+form+query, redact'ит | строка 81 |

### 1.3 API

**Файл:** `routes/audit_api.py`

- `POST /api/audit/ui` — приёмник UI-событий (НЕ декорирован сам — был бы рекурсивен). Source = `'ui'`. CSRF-exempt (см. `_ALLOWED_PUBLIC_POSTS`).
- `GET /api/audit` (admin) — пагинация, фильтры `from/to/action_type/actor/source/q`.
- `GET /api/audit/types` (admin) — для UI dropdown.

### 1.4 Frontend recorder

**Файл:** `static/js/audit.js`

- Делегированный `click`-listener в capture-фазе.
- Контракт разметки: `data-audit-action="..." data-audit-target="..." data-audit-context='{"src":"..."}'`.
- Транспорт: `fetch(..., {keepalive: true})` с CSRF-токеном, fallback на `navigator.sendBeacon`.
- Hard cap 4 KiB — большие contexts заменяются на `{__truncated__: true}`.
- Глобал: `window.WBAudit.record(action, target, ctx)`.

### 1.5 UI

- `templates/logs.html` — таб "Аудит (audit_log)" с фильтрами (дата/тип/источник/текст). **+ Debug toggle** (чекбокс на самой странице /logs, строки 340-364).
- В трёх шаблонах уже расставлены `data-audit-action`: `status.html` (5), `zones.html` (7), `programs.html` (5) — итого **17 элементов**.

### 1.6 Debug toggle (Level 2)

- Хранение: ключ `logging.debug` в таблице `settings`. Геттер/сеттер: `db/settings.py:64-70` (`get_logging_debug`, `set_logging_debug`). Через proxy в `database.db`.
- Endpoint: `POST /api/logging/debug` (`routes/system_config_api.py:392`) — body `{"enabled": bool, "auto_off_minutes": 1..720}`. На POST: persist + `logging.getLogger().setLevel(...)` + (re)schedule one-shot job `debug_auto_off` через APScheduler `DateTrigger`.
- Уже декорирован `@audit_log('debug_log_toggle', target='logging:debug')`.
- Auto-off job (`_disable_debug_logging_job`) выключает debug и пишет `record_audit('debug_log_auto_off')`.

### 1.7 Ротация

- `db/audit.py:176` — `cleanup_audit_logs(older_than_days=7)` → `DELETE FROM audit_log WHERE ts < datetime('now', '-7 days')`.
- Проксируется в `database.py:324`.
- Job `audit_cleanup`: APScheduler CronTrigger 03:30 daily (`irrigation_scheduler.py:388`). Самозапись о факте чистки + кол-ве удалённых строк (`irrigation_scheduler.py:166-187`).

> **ОТКРЫТЫЙ ВОПРОС 1:** В промпте требование "04:00 локально", сейчас стоит 03:30. Стоит ли менять? Рекомендация: **оставить 03:30** (cleanup, weather refresh, и пр. ночные джобы не должны сходиться в одну минуту). Если строго нужно 04:00 — тривиальный edit.

---

## 2. Список mutation endpoints

Полная инвентаризация POST/PUT/PATCH/DELETE по `routes/*.py`. Знак ✅ — декоратор есть, ⛔ — нет.

| Файл | Метод | Path | Decorator | Action type | Комментарий |
|---|---|---|---|---|---|
| `auth.py` | POST | `/api/login` | ✅ | `login` | |
| `system_config_api.py` | POST | `/logout` | ✅ | `logout` | |
| `system_config_api.py` | POST | `/api/password` | ✅ | `password_change` | |
| `system_config_api.py` | POST | `/api/map` | ✅ | `map_upload` | |
| `system_config_api.py` | DELETE | `/api/map/<filename>` | ✅ | `map_delete` | |
| `system_config_api.py` | POST | `/api/rain` | ✅ | `rain_config_save` | |
| `system_config_api.py` | POST | `/api/env` | ✅ | `env_config_save` | |
| `system_config_api.py` | POST | `/api/postpone` | ✅ | `postpone_action` | |
| `system_config_api.py` | POST | `/api/settings/early-off` | ✅ | `setting_early_off` | |
| `system_config_api.py` | POST | `/api/settings/system-name` | ✅ | `setting_system_name` | |
| `system_config_api.py` | POST | `/api/logging/debug` | ✅ | `debug_log_toggle` | |
| `system_emergency_api.py` | POST | `/api/emergency-stop` | ✅ | `emergency_stop` | |
| `system_emergency_api.py` | POST | `/api/emergency-resume` | ✅ | `emergency_resume` | |
| `system_emergency_api.py` | POST | `/api/backup` | ✅ | `backup_create` | |
| `system_status_api.py` | POST | `/api/health/job/<id>/cancel` | ✅ | `scheduler_job_cancel` | |
| `system_status_api.py` | POST | `/api/health/group/<id>/cancel` | ✅ | `scheduler_group_cancel` | |
| `system_status_api.py` | POST | `/api/scheduler/init` | ✅ | `scheduler_init` | |
| `weather_api.py` | PUT | `/api/settings/weather` | ✅ | `weather_settings_save` | |
| `weather_api.py` | PUT | `/api/settings/location` | ✅ | `weather_location_save` | |
| `weather_api.py` | POST | `/api/weather/refresh` | ✅ | `weather_refresh` | |
| `mqtt_api.py` | POST | `/api/mqtt/servers` | ✅ | `mqtt_server_create` | |
| `mqtt_api.py` | PUT | `/api/mqtt/servers/<id>` | ✅ | `mqtt_server_update` | |
| `mqtt_api.py` | DELETE | `/api/mqtt/servers/<id>` | ✅ | `mqtt_server_delete` | |
| `mqtt_api.py` | POST | `/api/mqtt/<id>/probe` | ⛔ | **(ADD)** `mqtt_server_probe` | Тестовая публикация — должна логироваться. |
| `groups_api.py` | POST | `/api/groups` | ✅ | `group_create` | |
| `groups_api.py` | PUT | `/api/groups/<id>` | ✅ | `group_save` | |
| `groups_api.py` | DELETE | `/api/groups/<id>` | ✅ | `group_delete` | |
| `groups_api.py` | POST | `/api/groups/<id>/stop` | ✅ | `group_stop` | |
| `groups_api.py` | POST | `/api/groups/<id>/start-from-first` | ✅ | `group_start_from_first` | |
| `groups_api.py` | POST | `/api/groups/<id>/start-zone/<zid>` | ✅ | `zone_start_exclusive` | |
| `groups_api.py` | POST | `/api/groups/<id>/master-valve/<action>` | ✅ | `master_valve_toggle` | |
| `programs_api.py` | POST | `/api/programs` | ✅ | `program_create` | |
| `programs_api.py` | PUT | `/api/programs/<id>` | ✅ | `program_modify` | |
| `programs_api.py` | DELETE | `/api/programs/<id>` | ✅ | `program_modify` | через тот же `@audit_log` |
| `programs_api.py` | POST | `/api/programs/check-conflicts` | ⛔ | **(ADD)** `program_check_conflicts` | Read-only по сути, но POST с body — для трассировки UI намерений полезно. |
| `programs_api.py` | POST | `/api/programs/<id>/duplicate` | ✅ | `program_duplicate` | |
| `programs_api.py` | PATCH | `/api/programs/<id>/enabled` | ✅ | `program_toggle` | |
| `zones_crud_api.py` | POST | `/api/zones` | ✅ | `zone_create` | |
| `zones_crud_api.py` | PUT | `/api/zones/<id>` | ✅ | `zone_modify` | |
| `zones_crud_api.py` | DELETE | `/api/zones/<id>` | ✅ | `zone_modify` | |
| `zones_crud_api.py` | POST | `/api/zones/import` | ✅ | `zones_import_bulk` | |
| `zones_crud_api.py` | POST | `/api/zones/next-watering-bulk` | ⛔ | **(ADD)** `zones_next_watering_bulk` | Bulk update планируемого следующего полива. |
| `zones_crud_api.py` | POST | `/api/zones/check-duration-conflicts` | ⛔ | **(ADD)** `zones_check_conflicts` | Скорее read-only, но приходит POST — логируем как UI intent. |
| `zones_crud_api.py` | POST | `/api/zones/check-duration-conflicts-bulk` | ⛔ | **(ADD)** `zones_check_conflicts_bulk` | То же. |
| `zones_watering_api.py` | POST | `/api/zones/<id>/start` | ✅ | `zone_start` | |
| `zones_watering_api.py` | POST | `/api/zones/<id>/stop` | ✅ | `zone_stop` | |
| `zones_watering_api.py` | POST | `/api/zones/<id>/mqtt/start` | ✅ | `zone_mqtt_start` | |
| `zones_watering_api.py` | POST | `/api/zones/<id>/mqtt/stop` | ✅ | `zone_mqtt_stop` | |
| `zones_photo_api.py` | POST | `/api/zones/<id>/photo` | ✅ | `photo_upload` | |
| `zones_photo_api.py` | DELETE | `/api/zones/<id>/photo` | ✅ | `photo_delete` | |
| `zones_photo_api.py` | POST | `/api/zones/<id>/photo/rotate` | ✅ | `photo_rotate` | |
| `settings.py` | PUT | `/api/settings/telegram` | ⛔ | **(ADD)** `telegram_settings_save` | Содержит bot_token — `_redact` уже маскирует по ключу `token`. |
| `settings.py` | POST | `/api/settings/telegram/test` | ⛔ | **(ADD)** `telegram_test_send` | Тестовая отправка. |
| `audit_api.py` | POST | `/api/audit/ui` | (намеренно нет) | varied | Recorder, был бы рекурсивен. |

**Итого:** 47 mutation endpoints; 40 уже декорированы (85%); **7 надо добавить декоратор**.

> **ОТКРЫТЫЙ ВОПРОС 2:** `*/check-conflicts*` и `next-watering-bulk` — спорные. Это POST, но не меняют состояние БД (только пересчёт). Вариант: записывать в audit с `result='check'`, source='api', чтобы можно было фильтровать. Альтернатива — пометить debug-уровнем (см. секцию 4) и в обычном режиме не писать. Рекомендация: **debug-уровень** — это именно UI intent, без физического действия.

---

## 3. State-машина зон / групп / мастер-вентиля

### 3.1 Где живёт

**Главный файл:** `services/zone_control.py` — централизованная логика `start/stop/exclusive_start/emergency_stop_all` для зон и мастер-вентилей.

Состояния зоны (в БД, поле `zones.state`):
```
off ──start──► starting ──MQTT ack──► on ──stop──► stopping ──MQTT ack──► off
```

Все переходы идут через утилиту `_versioned_update(zone_id, updates)` (zone_control.py:157), которая дёргает `db.update_zone_versioned` с optimistic concurrency.

Точки изменения state, которые **должны порождать audit-запись `zone_state_change`**:

| # | Файл:строка | Переход | Контекст |
|---|---|---|---|
| 1 | `zone_control.py:197` | `→ starting` | exclusive_start_zone (manual/api) |
| 2 | `zone_control.py:258` | `→ on` | после успешного MQTT publish '1' |
| 3 | `zone_control.py:277, 301` | `→ stopping` | peer-stop при exclusive |
| 4 | `zone_control.py:284, 308` | `→ off` | peer завершён |
| 5 | `zone_control.py:422` | `→ stopping` | stop_zone (manual/auto/program) |
| 6 | `zone_control.py:449` | `→ off` | stop_zone финальный |

### 3.2 Группы / мастер-вентиль

**Файл:** `services/zone_control.py`
- `_schedule_master_close` (строка 27) — таймер закрытия мастер-вентиля.
- `emergency_stop_all` (строка 539) — Phase A/B/C, синхронное закрытие всех мастеров.

Точки эмиссии `master_valve_state_change` / `master_valve_close` уже частично логируются на API-уровне (`master_valve_toggle`), но автоматическое закрытие после задержки — нет.

**Файл:** `services/program_queue.py` — состояния очереди программ (`QueueEntryState`: WAITING/RUNNING/COMPLETED/CANCELLED/EXPIRED/FAILED). На переходы записей в очереди тоже стоит писать `program_queue_transition` (debug-уровень).

### 3.3 План эмиссии

Не разбрасывать `record_audit` по 8+ местам, а сделать **обёртку** в `services/zone_control.py`:

```python
def _versioned_update(zone_id: int, updates: dict, *, audit_reason: str = '') -> None:
    prev = db.get_zone(zone_id) or {}
    prev_state = prev.get('state')
    ok = False
    try:
        ok = db.update_zone_versioned(zone_id, updates)
    except (sqlite3.Error, OSError):
        logger.exception("_versioned_update failed zone=%s", zone_id)
        ok = False
    if not ok:
        db.update_zone(zone_id, updates)
    new_state = updates.get('state')
    if new_state and new_state != prev_state:
        # Always-on audit: state transitions are principal-critical.
        record_audit(
            action_type='zone_state_change',
            source='zone_control',
            target=f'zone:{zone_id}',
            payload={'from': prev_state, 'to': new_state, 'reason': audit_reason},
            actor='system',
        )
```

Каждый существующий вызов `_versioned_update(zone_id, {'state': ...})` добавляет именованный аргумент `audit_reason='manual_start'` / `'peer_stop'` / `'auto_stop'` / `'emergency'` / `'mqtt_ack_on'` / `'mqtt_ack_off'`.

> **Уровень — always-on audit** (не debug): переходы зон — это самый principal-critical signal в системе.

### 3.4 MQTT publish

**Файл:** `services/mqtt_pub.py:98` — `publish_mqtt_value`.

В каждом успешном `cl.publish(...)` (строки 138, 169, 190, 203) можно вызвать `debug_audit('mqtt_publish', ...)` — но только при `logging.debug=true`, иначе таблица распухнет в десятки тысяч записей в час (Wirenboard публикует часто).

> **Решение:** **debug-уровень** через новый helper `debug_audit(...)` (см. секцию 4).

### 3.5 Scheduler timers

**Файл:** `irrigation_scheduler.py`

Места эмиссии `scheduler_timer_*` (debug-уровень):

| Метод | Точка | action_type |
|---|---|---|
| `schedule_zone_stop` (`:841`) | `add_job(stop_zone)` | `scheduler_timer_plant` |
| `schedule_zone_hard_stop` (`:887`) | `add_job(hard_stop)` | `scheduler_timer_plant` |
| `schedule_zone_cap` (`:912`) | `add_job(cap)` | `scheduler_timer_plant` |
| `schedule_master_valve_cap` (`:943`) | `add_job(close_master)` | `scheduler_timer_plant` |
| `schedule_program` / `_schedule_single_time` | `add_job(program_run)` | `scheduler_timer_plant` |
| `remove_job(...)` (несколько мест: 833, 937, 969, 1023, 1307, 1332) | `remove_job` | `scheduler_timer_cancel` |
| Внутри `job_*` функций (когда триггер сработал) | callback entry | `scheduler_timer_fire` |

Все три — **debug-уровень**. Только фактический результат (зона включилась / отключилась) уже логируется через `zone_state_change`.

---

## 4. Debug toggle — точное место хранения и контракт

### 4.1 Хранение

- **Уже существует.** Таблица `settings`, ключ `logging.debug`, значения `'1'`/`'0'`.
- Геттер: `db.get_logging_debug() -> bool` (`db/settings.py:64`).
- Сеттер: `db.set_logging_debug(bool)` (`db/settings.py:68`).

> **Никакая миграция не нужна**, поле уже есть. (В отличие от того, что просил Рауль про "BOOL default false в новой колонке" — это не стиль этого проекта; project использует key-value `settings`. Это лучше: миграции не плодим.)

### 4.2 Endpoint

- **Уже существует.** `POST /api/logging/debug` (`routes/system_config_api.py:392`).
- Body: `{"enabled": true|false, "auto_off_minutes": 1..720}`.
- Защита: middleware mutation guard требует `role='admin'`; decorator-level admin guard можно усилить отдельно.

> **Рекомендация:** добавить `@admin_required` явно к `api_logging_debug_toggle` — сейчас защита через before_request, что менее очевидно. Один лишний декоратор — defence in depth.

### 4.3 Helper `debug_audit`

Добавить в `services/audit.py`:

```python
def debug_audit(
    action_type: str,
    source: str = 'system',
    target: Optional[str] = None,
    payload: Any = None,
    **kw,
) -> None:
    """Emit audit row only when DEBUG logging is enabled.

    Used for high-volume diagnostic events (MQTT publishes, timer plants/fires)
    that would overflow audit_log in normal operation.
    """
    try:
        from database import db as _db
        if not _db.get_logging_debug():
            return
        record_audit(action_type=action_type, source=source,
                     target=target, payload=payload, **kw)
    except Exception:
        logger.exception("debug_audit failed (action=%s)", action_type)
```

> **Кеширование:** `get_logging_debug()` дёргает SQLite на каждый publish — это сотни-тысячи вызовов в час. Сделать тонкий TTL-cache (5 секунд) внутри `services/audit.py` — либо локальный, либо через тот же `_disable_debug_logging_job`, который должен сбрасывать его при выключении.

### 4.4 Уровневое разделение action_type

См. секцию 5 (таблица).

### 4.5 UI

**Текущее состояние:** чекбокс "Показывать подробные (DEBUG) сообщения" уже есть в `templates/logs.html:340-364`, без auto-off control.

> **ОТКРЫТЫЙ ВОПРОС 3:** В промпте Рауля сказано "Debug toggle button на странице /settings". Сейчас он на /logs. Варианты:
> a. **Перенести** на /settings и убрать с /logs (один источник правды).
> b. **Дублировать** на /settings, оставив на /logs (удобство — переключатель рядом с фильтрами логов).
> c. Оставить как есть (только /logs).
>
> Рекомендация: **(a) перенести** — `/settings` это семантически правильное место для глобального флага. Также добавить выпадающий select "Auto-off через" со значениями 15/60/240/нет, чего сейчас в UI нет — приходится дёргать через curl.

---

## 5. Action types — таблица уровней

| # | action_type | Уровень | Где emit'ится | source |
|---|---|---|---|---|
| 1 | `login` | audit | `routes/auth.py` | api |
| 2 | `logout` | audit | `routes/system_config_api.py` | api |
| 3 | `password_change` | audit | `routes/system_config_api.py` | api |
| 4 | `zone_create` / `zone_modify` | audit | `routes/zones_crud_api.py` | api |
| 5 | `zones_import_bulk` | audit | `routes/zones_crud_api.py` | api |
| 6 | `zones_next_watering_bulk` (NEW) | audit | `routes/zones_crud_api.py` | api |
| 7 | `zones_check_conflicts(_bulk)` (NEW) | **debug** | `routes/zones_crud_api.py` | api |
| 8 | `zone_start` / `zone_stop` | audit | `routes/zones_watering_api.py` | api |
| 9 | `zone_mqtt_start` / `zone_mqtt_stop` | audit | `routes/zones_watering_api.py` | api |
| 10 | `zone_start_exclusive` | audit | `routes/groups_api.py` | api |
| 11 | `zone_state_change` (NEW) | audit | `services/zone_control.py` (через `_versioned_update`) | zone_control |
| 12 | `group_create` / `group_save` / `group_delete` / `group_stop` / `group_start_from_first` | audit | `routes/groups_api.py` | api |
| 13 | `master_valve_toggle` | audit | `routes/groups_api.py` | api |
| 14 | `master_valve_auto_close` (NEW) | audit | `services/zone_control.py:_schedule_master_close` | zone_control |
| 15 | `program_create` / `program_modify` / `program_duplicate` / `program_toggle` | audit | `routes/programs_api.py` | api |
| 16 | `program_check_conflicts` (NEW) | **debug** | `routes/programs_api.py` | api |
| 17 | `program_run_started` (NEW) | audit | `irrigation_scheduler.py` job_run_program | scheduler |
| 18 | `program_run_completed` (NEW) | audit | `services/program_queue.py` ProgramCompletionTracker | scheduler |
| 19 | `program_queue_transition` (NEW) | **debug** | `services/program_queue.py` (WAITING→RUNNING→COMPLETED/...) | scheduler |
| 20 | `emergency_stop` / `emergency_resume` | audit | `routes/system_emergency_api.py` | api |
| 21 | `backup_create` | audit | `routes/system_emergency_api.py` | api |
| 22 | `weather_settings_save` / `weather_location_save` / `weather_refresh` | audit | `routes/weather_api.py` | api |
| 23 | `mqtt_server_create` / `mqtt_server_update` / `mqtt_server_delete` | audit | `routes/mqtt_api.py` | api |
| 24 | `mqtt_server_probe` (NEW) | audit | `routes/mqtt_api.py` | api |
| 25 | `mqtt_publish` (NEW) | **debug** | `services/mqtt_pub.py` | mqtt |
| 26 | `mqtt_publish_failure` (NEW) | audit | `services/mqtt_pub.py` (после исчерпания retries, QoS≥1 not delivered) | mqtt |
| 27 | `scheduler_init` / `scheduler_job_cancel` / `scheduler_group_cancel` | audit | `routes/system_status_api.py` | api |
| 28 | `scheduler_timer_plant` (NEW) | **debug** | `irrigation_scheduler.py` | scheduler |
| 29 | `scheduler_timer_cancel` (NEW) | **debug** | `irrigation_scheduler.py` | scheduler |
| 30 | `scheduler_timer_fire` (NEW) | **debug** | `irrigation_scheduler.py` job_* entries | scheduler |
| 31 | `audit_cleanup` | audit | `irrigation_scheduler.py` | scheduler |
| 32 | `debug_log_toggle` / `debug_log_auto_off` | audit | `routes/system_config_api.py` | api/scheduler |
| 33 | `setting_early_off` / `setting_system_name` / `rain_config_save` / `env_config_save` / `map_upload` / `map_delete` | audit | `routes/system_config_api.py` | api |
| 34 | `postpone_action` | audit | `routes/system_config_api.py` | api |
| 35 | `photo_upload` / `photo_delete` / `photo_rotate` | audit | `routes/zones_photo_api.py` | api |
| 36 | `telegram_settings_save` (NEW) | audit | `routes/settings.py` | api |
| 37 | `telegram_test_send` (NEW) | audit | `routes/settings.py` | api |
| 38 | `*_click` (UI events) | audit | браузер → `/api/audit/ui` | ui |

**Always-on (audit):** ~30 типов — все значимые мутации, переходы зон, сбои MQTT QoS≥1.
**Debug (toggleable):** 8 типов — MQTT publish, scheduler timer plant/cancel/fire, program queue transitions, conflict-check intents.

---

## 6. Frontend — UI элементы которым нужен `data-audit-action`

### 6.1 Уже есть (17 элементов)

- `templates/status.html:120,123,148,192,245` — emergency stop/resume, group_run, zone_edit_save, zone_run_confirm.
- `templates/zones.html:17,32,33,34,106,138,293` — group_create_open, zones_export_csv, zones_import_csv, zone_create_open, zones_bulk_apply, setting_early_off_save, group_save_submit.
- `templates/programs.html:65,208,211,214,217` — program_wizard_next_or_save_click, program_edit_click, program_duplicate_click, program_run_click, program_delete_click.

### 6.2 Нужно добавить

| Файл | Селектор / id | data-audit-action |
|---|---|---|
| `templates/settings.html:20` | `#sys-save` | `settings_system_name_save` |
| `templates/settings.html:54` | `#tg-save` | `settings_telegram_save` |
| `templates/settings.html:55` | `#tg-test` | `settings_telegram_test` |
| `templates/settings.html:76` | `#geo-detect` | `settings_geo_detect` |
| `templates/settings.html:130` | `#weather-save` | `settings_weather_save` |
| `templates/settings.html:131` | `#weather-test` | `settings_weather_test` |
| `templates/settings.html:162` | `#submit-btn` (pwd-form) | `settings_password_submit` |
| `templates/settings.html` (новый блок debug toggle) | `#debug-mode-toggle` | `debug_log_toggle_click` |
| `templates/mqtt.html:45` | `onclick=createServer()` | `mqtt_server_create_click` |
| `templates/mqtt.html:54` | `#subBtn` | `mqtt_subscribe_toggle_click` |
| `templates/mqtt.html:61` | `#scanBtn` | `mqtt_scan_toggle_click` |
| `templates/mqtt.html:62` | `onclick=showConnectLogs()` | `mqtt_show_logs_click` |
| `templates/mqtt.html:63` | `onclick=clearBrowser()` | `mqtt_clear_browser_click` |
| `templates/mqtt.html:110` | `onclick=updateRow(...)` | `mqtt_server_update_click` |
| `templates/mqtt.html:111` | `onclick=deleteServer(...)` | `mqtt_server_delete_click` |
| `templates/login.html` | submit form | `login_submit` |
| `templates/map.html` | upload button | `map_upload_click` |
| `templates/map.html` | each delete-link | `map_delete_click` |
| `templates/zones.html:293+` | inline modal "Удалить зону" | `zone_delete_confirm_click` |

> **НЕ логируем (явный non-goal):**
> - открытие/закрытие модалок (`.modal-open`, `.modal-close`),
> - переключение табов (`.tabs button`, `.logs-tab`),
> - "глаз" показа пароля (`.settings-toggle`),
> - кнопки фильтров на /logs.
>
> Фильтр в `audit.js`: уже идеален — он реагирует только на элементы с явным `data-audit-action`. Никаких регэкспов / магии. Просто **не ставим атрибут** там, где не хотим логировать.

### 6.3 Submit-форм

`audit.js` сейчас слушает только `click`. Стоит добавить второй listener:

```js
document.addEventListener('submit', function (ev) {
  var f = ev.target;
  if (f && f.dataset && f.dataset.auditAction) {
    record(f.dataset.auditAction, f.dataset.auditTarget || null,
           f.dataset.auditContext ? safeJsonParse(f.dataset.auditContext) : null);
  }
}, true);
```

Это даст бесплатное покрытие `<form data-audit-action="...">` без необходимости вешать атрибут на submit-кнопку отдельно.

---

## 7. Ротация — точное место в APScheduler

**Уже реализовано.** Вот как:

- `irrigation_scheduler.py:166` — функция `job_audit_cleanup()`. Вызывает `db.cleanup_audit_logs(7)`, логирует кол-во удалённых строк, пишет `record_audit('audit_cleanup', source='scheduler', payload={'deleted': N, 'older_than_days': 7})`.
- `irrigation_scheduler.py:307` — регистрация в `start()` через `schedule_audit_cleanup()`.
- `irrigation_scheduler.py:388` — реализация `schedule_audit_cleanup`: CronTrigger `hour=3, minute=30`, `id='audit_cleanup'`, `replace_existing=True`, `coalesce=True`, `max_instances=1`.
- БД-функция: `db/audit.py:175` — `cleanup_audit_logs(older_than_days)` под `@retry_on_busy()`.

**Покрытие тестами:** `tests/unit/test_audit_cleanup_job.py` — 3 теста.

> **Не трогать**, кроме как сменить 03:30 → 04:00 если Рауль настаивает (см. ОТКРЫТЫЙ ВОПРОС 1).

---

## 8. Файлы, которые будут изменены

### 8.1 Backend

| Файл | Что меняется | Уровень риска |
|---|---|---|
| `services/audit.py` | + helper `debug_audit(...)` с TTL-cache на `logging.debug`. | низкий |
| `services/zone_control.py` | `_versioned_update` принимает `audit_reason='...'`, эмиттит `zone_state_change`. + `master_valve_auto_close` в `_schedule_master_close._do_close` после успешного publish. + замена ~10 `logger.debug("Exception ...")` на `logger.exception(...)` на критичных путях. | средний (фундаментальная функция) |
| `services/mqtt_pub.py` | `debug_audit('mqtt_publish', target=topic, payload={'value': value, 'qos': qos, 'retain': retain})` после успеха. `record_audit('mqtt_publish_failure', ...)` после исчерпания retries (для QoS≥1). + замена ~5 `logger.debug("Exception ...")` на `logger.exception` в путях `connect/publish` (но **не** на duplicate-skip). | средний |
| `services/program_queue.py` | `debug_audit('program_queue_transition', ...)` при смене `QueueEntryState`. `record_audit('program_run_started')` в worker entry. `record_audit('program_run_completed')` в ProgramCompletionTracker. | низкий |
| `irrigation_scheduler.py` | `debug_audit('scheduler_timer_plant'/'_cancel'/'_fire', ...)` в add_job/remove_job/job_*-callbacks. | низкий-средний (много точек) |
| `routes/settings.py` | `@audit_log('telegram_settings_save')` на PUT /api/settings/telegram, `@audit_log('telegram_test_send')` на POST /api/settings/telegram/test. | низкий |
| `routes/programs_api.py` | `@audit_log('program_check_conflicts')` (debug-уровень — оборачиваем в условие в самой функции, либо `audit_log('program_check_conflicts')` без специальной логики и принимаем что это всегда пишется). | низкий |
| `routes/zones_crud_api.py` | + декораторы на `next-watering-bulk`, `check-duration-conflicts`, `check-duration-conflicts-bulk`. | низкий |
| `routes/mqtt_api.py` | + `@audit_log('mqtt_server_probe')`. | низкий |
| `routes/system_config_api.py` | + явный `@admin_required` на `api_logging_debug_toggle` (defence in depth). | низкий |

### 8.2 Frontend

| Файл | Что меняется |
|---|---|
| `static/js/audit.js` | + `submit`-listener (см. 6.3). Опционально — debounce для повторных кликов. |
| `templates/settings.html` | + `data-audit-action` на 7 кнопок (см. 6.2). + новый блок "Debug logging" с чекбоксом и select auto-off. JS вызывает `POST /api/logging/debug` с body `{enabled, auto_off_minutes}`. |
| `templates/mqtt.html` | + `data-audit-action` на 7 кнопок. |
| `templates/login.html` | + `data-audit-action="login_submit"` на форму. |
| `templates/map.html` | + `data-audit-action` на upload/delete. |
| `templates/logs.html` | (опц.) удалить дублирующий debug-чекбокс или оставить «Live»-переключатель уровня без persist. |

### 8.3 Тесты

| Файл | Что добавить |
|---|---|
| `tests/unit/test_audit_log.py` | unit-тесты на `debug_audit` (сэкономит когда `logging.debug=false`, пишет когда `true`). |
| `tests/unit/test_zone_state_audit.py` (NEW) | проверить что start_zone/stop_zone порождают `zone_state_change` с правильными from/to. |
| `tests/api/test_audit_coverage.py` (NEW) | тест-петля: для каждого decorated endpoint в `routes/` сделать минимальный POST/PUT/DELETE и убедиться что строка появилась в audit_log. |
| `tests/integration/test_mqtt_publish_audit.py` (NEW) | mock paho, проверить debug_audit вызывается при `logging.debug=true`. |

### 8.4 Документация (без новых .md)

> Промпт Рауля запрещает создание лишних .md (за пределами этого файла). Вношу секцию "Audit logging" непосредственно в **этот** документ — никаких новых README.

---

## 9. ОТКРЫТЫЕ ВОПРОСЫ (резюме)

1. **Время ротации:** 03:30 (текущее) или 04:00 (промпт)?
   *Рекомендация: 03:30, тривиально менять.*
2. **`*/check-conflicts*` endpoints:** decorated audit или debug?
   *Рекомендация: debug — это intent, не мутация.*
3. **Где UI debug-toggle:** /logs (текущее), /settings (промпт), оба?
   *Рекомендация: перенести на /settings, добавить auto-off select. Дубль не делать.*
4. **`logger.debug("Exception ...")` — все 397 заменять на `logger.exception`?**
   *Рекомендация: НЕТ. Только в местах где исключение реально неожиданное (zone_control critical paths, mqtt_pub publish, scheduler job entries). Глоты в типа "duplicate-skip", "loop_start", "tls_set" — оставить как есть. Таргет: ~30 точек.*
5. **TTL-cache для `get_logging_debug()`:** 5 секунд достаточно?
   *Рекомендация: да; auto-off job уже invalidate'ит при flip-back, manual toggle через POST тоже надо invalidate'ить (одна строка после `set_logging_debug`).*
6. **`mqtt_publish` source:** `'mqtt'` или `'mqtt_pub'`?
   *Рекомендация: `'mqtt'` — короче, не повторяется в action_type.*
7. **`payload` для `mqtt_publish`:** включать топик в `target`, value в payload — обрезать большие payloads?
   *`_redact` уже делает это (cap 1024 chars). Оставить.*

---

## 10. Порядок внедрения (для разработчика)

1. **Phase 0 — без рисков:** добавить декораторы на 7 непокрытых endpoints (правка только `routes/*.py`). Мерж + наблюдение в проде сутки.
2. **Phase 1 — helper и cache:** `debug_audit` в `services/audit.py` + TTL-cache. Тест unit. Мерж.
3. **Phase 2 — state-машина:** обернуть `_versioned_update`, добавить `audit_reason` в каждый вызов. Тесты unit + integration. Мерж под debug-режимом сутки, потом включить always-on.
4. **Phase 3 — MQTT/scheduler debug emits:** только когда `debug_audit` стабилен.
5. **Phase 4 — UI:** добавить `data-audit-action` атрибуты + перенос debug-toggle на /settings. Тестировать через смок-сценарий "пройти все формы".
6. **Phase 5 — `logger.exception` rewrite:** точечно ~30 мест.

Все фазы независимы — можно мержить отдельными PR.

---

## Приложение A. Action types для UI dropdown в /logs

После внедрения, `GET /api/audit/types` будет возвращать ~38 action_types. Имеет смысл сгруппировать в UI:

- **Auth:** login, logout, password_change.
- **Zones:** zone_create, zone_modify, zone_start, zone_stop, zone_state_change, zone_mqtt_start, zone_mqtt_stop, zone_start_exclusive.
- **Groups & Master Valve:** group_*, master_valve_*.
- **Programs:** program_*, scheduler_*.
- **System:** emergency_*, backup_create, audit_cleanup, debug_log_*, weather_*, mqtt_server_*, setting_*.
- **Diagnostics (debug only):** mqtt_publish, scheduler_timer_*, program_queue_transition, *_check_conflicts.
- **UI:** все *_click.

Это улучшение — не блокер для основной работы.
