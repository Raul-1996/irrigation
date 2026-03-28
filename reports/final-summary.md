# Финальный отчёт: wb-irrigation v2.0 рефакторинг

**Дата:** 2026-03-28

## Что было → Что стало

| Метрика | До | После |
|---------|-----|-------|
| app.py | 4411 строк | 361 строк (-92%) |
| database.py | 2359 строк | 306 строк (-87%) + db/ 2469 строк |
| Модули Python | ~5 | 52 файла (app, db/10, routes/12, services/16, utils, config, etc.) |
| Security rating | CRITICAL | B+ (GOOD) |
| catch-all except без лога | 356+ (pass без логирования) | 0 (все 742 except с logger или raise) |
| QoS MQTT | 0 | 2 + retain (для управления зонами) |
| SECRET_KEY | hardcoded `wb-irrigation-secret` | auto-generated `secrets.token_hex(32)` + file persist |
| MQTT auth | anonymous | username+password+ACL (allow_anonymous false) |
| CSRF | disabled | enabled (CSRFProtect + WTF_CSRF_CHECK_DEFAULT=True) |
| Guest access | full control | viewer (read-only, 403 на мутации) |
| Password | 1234 | random 16-char + force change on first login |
| Docker user | root | appuser |
| MQTT passwords | plaintext в БД | encrypted (ENC: prefix, Fernet) |

## Закрытые уязвимости

- **SEC-001: Anonymous MQTT** → ✅ закрыто (allow_anonymous false + password_file + acl_file)
- **SEC-002: Hardcoded SECRET_KEY** → ✅ закрыто (auto-gen + env variable + file persist)
- **SEC-003: Plaintext MQTT passwords** → ✅ закрыто (ENC: encryption в db/mqtt.py + миграция)
- **SEC-004: CSRF disabled** → ✅ закрыто (CSRFProtect enabled)
- **SEC-005: Guest full access** → ✅ закрыто (viewer role: read-only)
- **SEC-006: Session rate-limit** → ✅ закрыто (LoginRateLimiter IP-based)
- **SEC-007: Hostname key** → ✅ закрыто (криптографический random key)
- **SEC-008: QoS 0** → ✅ закрыто (qos=2 + retain для zone control commands)

## Архитектура

```
wb-irrigation/
├── app.py              # Flask core: config, middleware, blueprint registration, watchdog
├── config.py           # Config class with auto-generated SECRET_KEY
├── database.py         # Facade (backward-compatible proxy → db/)
├── db/                 # Repository pattern
│   ├── base.py         # BaseRepository + retry_on_busy
│   ├── zones.py        # Zone CRUD, photo, import/export
│   ├── programs.py     # Programs, conflicts, cancellations
│   ├── groups.py       # Groups
│   ├── mqtt.py         # MQTT servers + password encryption
│   ├── settings.py     # Settings key-value store
│   ├── telegram.py     # Bot users, FSM, reminders
│   ├── logs.py         # Logs, backup/restore
│   └── migrations.py   # All DB migrations
├── routes/             # Blueprints
│   ├── *_api.py        # API endpoints (zones, groups, programs, mqtt, system)
│   └── *.py            # Page rendering (status, zones, programs, etc.)
├── services/           # Business logic
│   ├── zone_control.py # Zone start/stop with QoS 2
│   ├── mqtt_pub.py     # MQTT publish with QoS support + retry
│   ├── observed_state.py # State verification via MQTT feedback
│   ├── monitors.py     # Rain, env, water monitors
│   ├── sse_hub.py      # Server-Sent Events hub
│   ├── rate_limiter.py # Login brute-force protection
│   ├── watchdog.py     # Cap-time watchdog
│   ├── telegram_bot.py # Telegram bot integration
│   └── ...
├── utils.py            # Shared utilities
├── mosquitto.conf      # Secure MQTT config
├── Dockerfile          # Non-root (appuser)
└── docker-compose.yml  # Production compose
```

**Endpoints:** 81 route decorators = 66+ unique API endpoints + page routes

## Except Audit

| Категория | Количество |
|-----------|-----------|
| Всего except блоков | 742 |
| С логированием (logger.*) | 740 |
| С re-raise | 1 |
| Без лога (queue.Empty keepalive) | 1 (с комментарием) |
| Bare `except:` | 0 |
| `except Exception` (broad) | 590 |
| Specific exceptions | 152 |

**Исправлено в этом этапе:** 26 except блоков без логирования → добавлен `logger.debug()` с описанием.

## Оставшиеся замечания

1. **Group exclusivity логика в app.py** — ~130 строк бизнес-логики (`_force_group_exclusive`, `_enforce_group_exclusive_all_groups`, `_start_single_zone_watchdog`). Рекомендуется вынести в `services/group_watchdog.py`.

2. **Deferred import в app_init.py** — `from app import _start_single_zone_watchdog` внутри функции. Работает, но архитектурно не идеально. Решается пунктом 1.

3. **Дублирование auth логики** — `_auth_before_request` и `_require_admin_for_mutations` в app.py частично пересекаются. Стоит консолидировать в один middleware.

4. **Высокое количество broad `except Exception`** — 590 из 742. Многие оправданы (daemon threads, top-level route handlers), но часть можно сузить до конкретных исключений.

5. **QoS 0 для subscribe в monitors.py** — 7 подписок. Для мониторинга допустимо, но QoS 1 был бы надёжнее.

## Рекомендации

1. **Вынести group watchdog** из app.py в `services/group_watchdog.py` — уменьшит app.py до ~230 строк
2. **Консолидировать auth middleware** — объединить `_auth_before_request` и `_require_admin_for_mutations`
3. **Добавить HSTS + CSP headers** для повышения security rating до A
4. **Rate limiting на все API endpoints** — не только login
5. **Сузить except Exception** — в некоторых местах можно использовать `(sqlite3.Error, ValueError, KeyError)`
6. **Интеграционные тесты** — добавить pytest suite для проверки всех endpoints
7. **CI/CD** — автоматический `py_compile` + security scan в pipeline
