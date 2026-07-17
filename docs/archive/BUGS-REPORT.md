# BUGS-REPORT.md — Отчёт по багам и качеству кода

**Дата:** 2026-04-02
**Проект:** wb-irrigation (branch refactor/v2)
**Оценка качества кода: 5.5/10**

---

## 1. Баги (подтверждённые)

### CRITICAL

| # | Файл:Строка | Описание | Fix |
|---|-------------|----------|-----|
| 1 | `routes/settings.py:31,73` | `sqlite3.Error` в except но `import sqlite3` отсутствует → **NameError в production** при любой DB ошибке в telegram settings | Добавить `import sqlite3` в начало файла |

### HIGH

| # | Файл:Строка | Описание | Fix |
|---|-------------|----------|-----|
| 2 | `app.py:203,256` | `_is_status_action()` определена ДВАЖДЫ в разных местах app.py — разная логика, разные переменные | Вынести в одну функцию |
| 3 | `routes/weather_api.py:63,275` | Прямой `sqlite3.connect()` минуя database.py — обход connection management, возможны проблемы с конкурентностью | Использовать db.get_connection() |
| 4 | `routes/settings.py:96` | Прямой `sqlite3.connect()` внутри try/except который ловит `sqlite3.Error` без импорта sqlite3 (строки 31,73) | Использовать db, добавить import |

### MEDIUM

| # | Файл:Строка | Описание | Fix |
|---|-------------|----------|-----|
| 5 | `services/telegram_bot.py:7` | Импортирует `check_password_hash` но не использует | Удалить |
| 6 | `services/auth_service.py:5` | Импортирует `generate_password_hash` но не использует | Удалить |
| 7 | `services/events.py:2,4` | Импортирует `queue` и `json` — не используются | Удалить |
| 8 | `app.py:65` | 6 неиспользуемых импортов из monitors | Удалить |
| 9 | `app.py:73` | `_locks_snapshot` импортирован но не используется | Удалить |

---

## 2. Неиспользуемые импорты (40+)

**app.py** — 11 неиспользуемых импортов (наибольшее количество):
- `_get_or_create_mqtt_client`
- `ensure_console_handler`, `apply_runtime_log_level`
- `check_password_hash`
- `rain_monitor`, `env_monitor`, `start_rain_monitor`, `start_env_monitor`, `water_monitor`, `start_water_monitor`, `probe_env_values`
- `_locks_snapshot`
- `admin_required`

**Другие файлы:**
- `database.py:10` — `datetime`
- `routes/zones_watering_api.py:2` — `Response`, `stream_with_context`
- `routes/zones_watering_api.py:6` — `queue`
- `routes/groups_api.py:2` — `session`
- `services/program_queue.py:12,14` — `timedelta`, `Any`, `Callable`
- `services/app_init.py:11` — `threading`
- `services/weather.py:13` — `timedelta`
- `services/observed_state.py:13` — `Optional`
- `services/weather_codes.py:6` — `Tuple`
- `services/telegram_bot.py:8` — `datetime`, `timedelta`
- `services/auth_service.py:1,2,6` — `Optional`, `os`, `generate_password_hash`, `threading`
- `db/migrations.py:4` — `Optional`

---

## 3. Мёртвый код (44 функции)

### Потенциально мёртвые функции (могут вызываться через Flask decorators/routes):

Многие из этих функций — Flask route handlers, зарегистрированные через `@bp.route()`. Они "мёртвые" по grep-у но могут вызываться через HTTP. **Нужна ручная проверка** — если route зарегистрирован но URL не используется в UI, функция мёртвая.

**Подозрительные (скорее всего мёртвые):**
- `app.py:148` — `add_security_headers()` — дублирует middleware?
- `services/api_rate_limiter.py:77` — `api_foo()` — **тестовый/demo код в production**
- `ui_agent_demo.py` — весь файл (295 LOC) — demo/прототип

**Файлы целиком мёртвые (подтверждено architecture отчётом):**
- `ui_agent_demo.py` (295 LOC)
- `basic_auth_proxy.py` (размер ~100 LOC)
- `templates/programs_old.html` (369 LOC)
- `services/scheduler_service.py` (если существует)

---

## 4. Дублирование кода

| Где | Что | LOC |
|----|-----|-----|
| `app.py:203` + `app.py:256` | `_is_status_action()` — одна и та же функция определена дважды с разными переменными | ~30 |
| `services/weather.py` + `services/weather_adjustment.py` + `services/weather_merged.py` | 3 weather-модуля (1538 LOC суммарно) с пересекающейся логикой | ~500 дублирования |
| `status.js: updateStatusDisplay` + `refreshSingleGroup` | 70% копипасты (из refactor отчёта) | ~200 |
| `services/monitors.py: EnvMonitor._start_temp` + `_start_hum` | 90% одинакового кода | ~60 |

---

## 5. Результаты тестов

**Тесты запускались но таймаутили** на этой машине (sandbox, не ARM контроллер).

**Подтверждённый failing тест:**
- `tests/api/test_coverage_boost.py::TestSettingsOperations::test_telegram_settings`
  - **Причина:** `NameError: name 'sqlite3' is not defined` в `routes/settings.py:73`
  - **Fix:** добавить `import sqlite3` в routes/settings.py

**Другие failures видимые по маркерам (F):** ~7 штук при полном прогоне, точные имена не получены из-за таймаута sandbox-а.

**Skipped тесты (X/x маркеры):** ~30+ тестов помечены xfail — вероятно known issues.

---

## 6. Рекомендации по приоритету

1. **НЕМЕДЛЕННО:** Добавить `import sqlite3` в `routes/settings.py` — баг в production
2. **СРОЧНО:** Убрать гостевой доступ к управлению клапанами (из security отчёта)
3. **НЕДЕЛЯ:** Вычистить 40+ неиспользуемых импортов
4. **НЕДЕЛЯ:** Удалить мёртвые файлы (ui_agent_demo.py, basic_auth_proxy.py, programs_old.html)
5. **МЕСЯЦ:** Консолидировать 3 weather-модуля в один
6. **МЕСЯЦ:** Убрать дублирование _is_status_action

---

*Отчёт составлен автоматически на основе статического анализа (grep + AST). Для полного покрытия рекомендуется прогнать тесты на ARM контроллере.*
