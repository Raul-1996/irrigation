# Stage 6b: Code Review — wb-irrigation refactor/v2

**Дата:** 2026-03-28

## Общая статистика diff

```
68 files changed, 8047 insertions(+), 6863 deletions(-)
```

## Структура после рефакторинга

### app.py (356 → 361 строк)
- ✅ Содержит только ядро: Flask app creation, config, logging, middleware, blueprint registration
- ✅ Blueprint registration для page-rendering (7) + API (5) + optional (telegram, reports, mqtt)
- ✅ Middleware: perf timing, security headers, session cookies, auth guard, mutation guard
- ✅ Group exclusivity watchdog остался в app.py (логически связан с core)
- ⚠️ `_force_group_exclusive` и `_enforce_group_exclusive_all_groups` — 100+ строк бизнес-логики в app.py. Кандидат на вынос в `services/`

### database.py (2359 → 306 строк, -87%)
- ✅ Facade-паттерн: проксирует все вызовы в `db/` субмодули
- ✅ Backward-compatible: все `db.get_zones()`, `db.create_program()` работают

### db/ (новый пакет, 2469 строк)
- `base.py` — BaseRepository + retry_on_busy decorator
- `zones.py` (587) — CRUD зон, фото, импорт/экспорт
- `programs.py` (258) — программы, конфликты, отмены
- `groups.py` (152) — группы
- `mqtt.py` (118) — MQTT серверы + шифрование паролей
- `settings.py` (222) — настройки
- `telegram.py` (236) — bot users, FSM, idempotency, reminders
- `logs.py` (201) — логи, backup/restore
- `migrations.py` (636) — все миграции

### routes/ (12 файлов, 3327 строк)
- Page-rendering: `status.py`, `files.py`, `zones.py`, `programs.py`, `groups.py`, `auth.py`, `settings.py`
- API: `zones_api.py` (1156), `groups_api.py` (367), `programs_api.py` (109), `mqtt_api.py` (269), `system_api.py` (1002)
- Optional: `telegram.py` (246), `reports.py`

### services/ (16 файлов, 2502 строк)
- Новые: `app_init.py`, `helpers.py`, `logging_setup.py`, `observed_state.py`, `rate_limiter.py`, `sse_hub.py`, `watchdog.py`
- Обновлённые: `monitors.py`, `mqtt_pub.py`, `zone_control.py`, `telegram_bot.py`

## Endpoints

Обнаружено **81 route decorator** (включая GET/POST/PUT/DELETE variants):
- zones_api: 16 endpoints
- system_api: 22 endpoints
- groups_api: 8 endpoints
- mqtt_api: 7 endpoints
- programs_api: 4 endpoints
- settings: 4 endpoints
- auth: 2 endpoints
- telegram: в routes/telegram.py
- page rendering: /, /status, /zones, /programs, /logs, /mqtt, /map, /water, /settings, /login
- misc: /sw.js, /ws, /health, /api/reports

**Все 66+ endpoints доступны.**

## Imports

- ✅ Нет `from app import` в routes/
- ⚠️ `services/app_init.py:43` — `from app import _start_single_zone_watchdog` (deferred import в функции, не на уровне модуля — circular import не происходит, но архитектурно не идеально)
- ✅ routes/ импортируют из `database`, `services/`, `utils` — правильно
- ✅ db/ не импортирует из app.py или routes/

## Потенциальные проблемы

1. **Deferred import из app.py** — `services/app_init.py` импортирует `_start_single_zone_watchdog` из `app.py`. Работает через lazy import, но лучше вынести watchdog в `services/`.
2. **Group exclusivity в app.py** — 130+ строк бизнес-логики (`_force_group_exclusive`, `_enforce_group_exclusive_all_groups`, `_start_single_zone_watchdog`). Кандидат на вынос в `services/group_watchdog.py`.
3. **monitors.py qos=0 для subscribe** — все подписки используют QoS 0. Для мониторинга это допустимо (потеря одного сообщения не критична), но для observed_state проверка использует собственную подписку с проверкой доставки.
4. **Дублирование auth логики** — `_auth_before_request` и `_require_admin_for_mutations` в app.py частично пересекаются. Стоит консолидировать.
