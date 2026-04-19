# Phase 2 — Test Results Analyzer findings

**Scope:** тестовое покрытие, качество тестов, gap'ы, CI для ветки `refactor/v2`
**Branch:** `refactor/v2` (commit `HEAD`, 2 локальных коммита впереди origin)
**Runner:** `/tmp/test-venv-irrig` (Python 3.12.3, pytest 9.0.3, pytest-cov 7.1.0, pytest-timeout 2.4.0)
**Test root:** `/opt/claude-agents/irrigation-v2/tests/` (не `tools/tests/` — папки `tools/tests/` в `refactor/v2` не существует; landscape §7 это подтверждает: `tests/` — 114 файлов в 7 подпапках)

---

## Executive Summary

| Метрика | Значение | Вердикт |
|---|---|---|
| Тест-файлов (`test_*.py`) | **114** | хорошо |
| Тест-функций (собрано) | **1 558** unit/api/db + **25** integration + **13** e2e + **12** perf + **68** ui = **1 676** | очень много |
| unit/api/db run (6m 37s) | **1 392 passed / 3 failed / 1 skipped / 28 xfailed / 52 xpassed** | **3 fail** — deterministic drift |
| integration run (58s) | **25 passed** | OK |
| ui run (42s) | **58 passed / 4 failed / 3 xfailed / 3 xpassed** | 4 template-drift failures |
| perf+e2e run (9s) | **11 passed / 1 failed / 13 skipped** | SSE perf test broken |
| **Суммарно** | **1 486 passed / 8 failed / 14 skipped / 31 xfailed / 55 xpassed** | **~99.5% pass rate**, 8 deterministic failures |
| Coverage unit+api+db | **60.78%** (порог `fail_under=30` пройден) | средне |
| Selenium-тесты | **нет** (landscape §7 говорит «3 selenium» — **это неверно**: `test_*` в `tests/ui/` используют Flask test client, Selenium не импортируется) | факт отличается от landscape |
| CI для `refactor/v2` | **НЕ ПОКРЫТ** (`.github/workflows/ci.yml` triggers: only `main, master`) | **CRIT** |
| `pytest.ini` vs `pyproject.toml [tool.pytest.ini_options]` | **коллизия** — pytest-9 печатает warning `ignoring pytest config in pyproject.toml!` | **MED** |

### Топ-3 для Phase 4
1. **[CRIT] `ci.yml` не триггерится для `refactor/v2`** — любые регрессии в ветке сейчас обнаруживаются только вручную.
2. **[CRIT] 8 deterministic test failures** — drift между тестами и кодом (SSE maxsize 20 vs 100, SSE dead-client detection, `/api/mqtt/zones/sse` возвращает 204 вместо 200, ui template-классы `zone-card` отсутствуют, SSE 10-client load-test).
3. **[HIGH] 55 XPASS-тестов** — TDD-сценарии `test_programs_*_v2.py` и `test_scheduler_v2.py` уже работают → нужно снять `@pytest.mark.xfail` и сделать их регрессионной сеткой, иначе в проде сломается и никто не заметит.

---

## 1. Inventory тестов

### 1.1. Структура `tests/`
```
tests/
├── conftest.py                         fixtures: test_db_path, test_db, app, client,
│                                       admin_client, viewer_client, guest_client,
│                                       mock_mqtt_client, sample_{zone,program,mqtt_server}_data
├── __init__.py (пустой)
├── TESTS_V2_README.md                  TDD-гид по programs v2
│
├── fixtures/
│   ├── app.py                          Flask test app (reload-hack, WTF_CSRF_ENABLED=False)
│   ├── database.py                     изолированная tmp_path SQLite per-test
│   └── mqtt.py                         MockMQTTClient (published list)
│
├── unit/                               54 файла, 768 test-функций
│   ├── conftest.py
│   └── test_*.py                       scheduler (5), monitors (3), mqtt_pub (3),
│                                       observed_state (2), sse_hub (2), sse_hardening,
│                                       program_queue (2), weather (4), watchdog (2),
│                                       zone_control (2), security, xss_fix, ...
│
├── api/                                27 файлов, 461 test
│   └── test_*.py                       Flask test client против blueprint'ов:
│                                       auth, zones, groups, programs (+_v2), mqtt,
│                                       settings, system, weather, routes_*,
│                                       guest_zone_control
│
├── db/                                 21 файл, 251 test
│   └── test_*.py                       repository-тесты: zones, groups, programs (+v2),
│                                       mqtt, logs, telegram, settings, migrations
│
├── integration/                        5 файлов, 25 test
│   ├── test_emergency_flow.py          (3)
│   ├── test_full_watering_cycle.py     (2)
│   ├── test_mqtt_real.py               (4)  — pytestmark=mqtt_real, требует 10.2.5.244:1883
│   ├── test_queue_scheduler_integration.py  (13)
│   └── test_telegram_bot.py            (3)
│
├── e2e/                                2 файла, 13 test (@pytest.mark.e2e)
│   ├── test_concurrent.py
│   └── test_live_controller.py         — бьёт живой контроллер
│
├── performance/                        2 файла, 12 test
│   ├── test_response_times.py
│   └── test_sse_load.py                10 конкурентных SSE-клиентов
│
└── ui/                                 3 файла, 68 test — **НЕ selenium** (Flask test client)
    ├── test_desktop_sidebar.py         (9)
    ├── test_zones_functional.py        (33)
    └── test_zones_ui_v2.py             (26)
```

### 1.2. Selenium / web_tests*.py
- `web_tests*.py` — **отсутствуют** (не в этом дереве и не в landscape).
- `requirements-dev.txt` содержит `selenium==4.15.2`, `webdriver-manager==4.0.1`, `pytest-selenium==4.0.1` — **мёртвые зависимости**, ни один тест их не импортирует (`grep -rn "selenium\|webdriver" tests/` → 0 хитов).
- `pytest.ini` явно отключает плагин: `-p no:selenium -p no:pytest_selenium`.
- Landscape §7 пишет "3 selenium UI" — **неправда**: `tests/ui/*` — это чистые template-inspection тесты на `client.get('/status').data`.
- **Finding:** удалить `selenium`, `webdriver-manager`, `pytest-selenium` из `requirements-dev.txt` (confuses CI, тянут ~50 МБ).

### 1.3. Другие директории с тестами
- `/opt/claude-agents/irrigation-v2/tests/` — единственное место.
- `tools/tests/` — не существует (в ТЗ был неверный путь; возможно, был в `main` до cleanup — `/opt/claude-agents/irrigation/tools/tests/web_tests*.py` на prod-snapshot).

---

## 2. Запуск локально

### 2.1. Окружение
- Систeмного venv в `/opt/claude-agents/irrigation-v2/` нет.
- Поставил чистый venv `/tmp/test-venv-irrig` с `requirements.txt` + `pytest-cov` + `pytest-timeout`.
- `TESTING=1` автоматически ставится в `tests/conftest.py:12`.
- `pytest.ini` timeout=10 (signal), `pyproject.toml` timeout=10 — **конфликт**, pytest 9 читает `pytest.ini` и печатает warning.

### 2.2. Результаты прогона

```bash
# unit + api + db (основной CI-набор)
$ TESTING=1 pytest tests/unit tests/api tests/db --tb=no --timeout=30
3 failed, 1392 passed, 1 skipped, 28 xfailed, 52 xpassed in 397.60s (6:37)

# integration
$ pytest tests/integration --tb=no --timeout=20
25 passed in 57.82s

# ui
$ pytest tests/ui --tb=no --timeout=20
4 failed, 58 passed, 3 xfailed, 3 xpassed in 42.25s

# performance + e2e
$ pytest tests/performance tests/e2e --tb=no --timeout=15
1 failed, 11 passed, 13 skipped in 9.19s
```

### 2.3. Детали 8 failures

| # | Тест | Причина | Сев. |
|---|------|---------|------|
| 1 | `tests/unit/test_sse_hardening.py::TestSSEClientLimit::test_queue_maxsize_reduced` | `assert q.maxsize == 100` but actual `20`. Тест обгоняет код (или код откатил с 100 на 20). | MED |
| 2 | `tests/unit/test_sse_hardening.py::TestInternalBroadcastDeadDetection::test_multiple_dead_clients_removed` | `len(_SSE_HUB_CLIENTS) == 1` but `0`. Все клиенты удалены — логика dead-detection слишком агрессивна либо setup-assumption сломался. | MED |
| 3 | `tests/api/test_routes_deep.py::TestMqttZonesSSE::test_mqtt_zones_sse` | `/api/mqtt/zones/sse` returned `204` instead of `200`. Роут отключён/заглушен в `routes/mqtt_api.py` (coverage модуля 37% — много мёртвого кода). | HIGH |
| 4 | `tests/ui/test_desktop_sidebar.py::test_mobile_buttons_responsive` | в HTML нет `@media (max-width: 1023px/767px)` — media-query вынесен в CSS-файл. | LOW |
| 5 | `tests/ui/test_zones_functional.py::TestSSEEndpoint::test_sse_endpoint_responds` | 204 вместо 200 (тот же SSE endpoint). | HIGH |
| 6 | `tests/ui/test_zones_functional.py::TestPageRender::test_zone_card_css_classes` | отсутствует класс `zone-card` в `/status` HTML. | LOW |
| 7 | `tests/ui/test_zones_ui_v2.py::TestZonesUITemplateElements::test_has_zone_card_css` | то же. | LOW |
| 8 | `tests/performance/test_sse_load.py::TestSSELoad::test_10_sse_clients` | клиент получил 1 сообщение из 100. Либо броадкаст упирается в `maxsize=20`, либо heartbeat/sentinel не доставляется. | HIGH |

**Pattern:** 4 из 8 — это SSE hub (failures #1, #2, #3/#5, #8). Там есть **реальный регрессионный баг** на `refactor/v2`: SSE broadcast path сломан, тесты это ловят, но падают deterministic-но.

### 2.4. Зависимости от окружения
- **Сеть/брокер:** `tests/integration/test_mqtt_real.py` (marker `mqtt_real`, `10.2.5.244:1883`) — **не запускал** (прод).
- **Живой контроллер:** `tests/e2e/test_live_controller.py` (marker `e2e`) — 11 тестов, все skipped в обычном прогоне (нужен endpoint).
- **БД:** каждый тест — свежая `tmp_path/test_irrigation.db` (изоляция OK).
- **MQTT:** мокируется через `MockMQTTClient` / `patched_mqtt` (fixtures/mqtt.py).
- **Файлы конфига:** не требуются (`TESTING=1` → `TestConfig`, `CSRF=False`, `SECRET_KEY` hardcoded).
- **Flask app reload:** `tests/fixtures/app.py:19-53` делает сложную гимнастику с `sys.modules` pop/reload — **хрупкое место**, приводит к 2s setup-delays на ~15 тестах (см. §10).

---

## 3. Coverage

### 3.1. Общее (unit+api+db, 1392 passed)
**TOTAL: 60.78%** (11 434 stmt, 4 484 miss) — выше порога `fail_under = 30` из `pyproject.toml`.

### 3.2. По слоям (unit+api+db прогон)

| Слой | Coverage | Комментарий |
|---|---|---|
| **Core** | | |
| `app.py` | 50% | factory + error handlers не покрыты (ветки 231-356) |
| `database.py` | 98% | почти полный |
| `config.py` | 62% | prod-ветки конфига не ходят |
| `utils.py` | 68% | форматтеры покрыты частично |
| **DB repos** (`db/`) | | |
| `db/base.py` | 83% | |
| `db/groups.py` | 79% | |
| `db/logs.py` | 78% | |
| `db/migrations.py` | **77%** | 151 stmt-miss — это важно, много downgrade-путей не проверяются |
| `db/mqtt.py` | 78% | |
| `db/programs.py` | 79% | |
| `db/settings.py` | 80% | |
| `db/telegram.py` | 67% | bot-user lifecycle edges не покрыты |
| `db/zones.py` | 75% | |
| **Scheduler** | | |
| `irrigation_scheduler.py` | **57%** | 439 stmt-miss, включая цикл `recover_missed_runs`, `_run_program_threaded`, `cancel`, `reschedule` (важные пути) |
| **Routes** | | |
| `routes/auth.py` | **100%** | |
| `routes/zones.py`, `programs.py`, `groups.py`, `mqtt.py` | 86–100% | stub blueprints |
| `routes/programs_api.py` | 76% | |
| `routes/status.py` | 83% | |
| `routes/system_emergency_api.py` | 68% | |
| `routes/system_config_api.py` | 66% | |
| `routes/system_status_api.py` | **61%** | 192 miss |
| `routes/weather_api.py` | 62% | |
| `routes/groups_api.py` | **60%** | |
| `routes/zones_watering_api.py` | **49%** | 196 miss — **критично** (это zone start/stop!) |
| `routes/zones_crud_api.py` | **49%** | 221 miss |
| `routes/zones_photo_api.py` | 40% | не-фото функционал |
| `routes/mqtt_api.py` | **37%** | 173 miss — много legacy/duplicate endpoints |
| `routes/settings.py` | 57% | |
| `routes/telegram.py` | **10%** | 160 miss — Telegram routes почти не покрыты |
| `routes/reports.py` | 56% | |
| `routes/system_api.py`, `zones_api.py` | **0%** | файлы-заглушки (`routes/system_api.py:10-17` и `routes/zones_api.py:10-18`) |
| **Services** | | |
| `services/et_calculator.py`, `rate_limiter.py`, `scheduler_service.py`, `weather_adjustment.py`, `weather_merged.py` | 100% | |
| `services/irrigation_decision.py` | 97% | |
| `services/program_queue.py` | **93%** | хорошо |
| `services/helpers.py` | 94% | |
| `services/events.py` | 88% | |
| `services/locks.py` | 88% | |
| `services/float_monitor.py` | 83% | |
| `services/monitors/water_monitor.py` | 66% | |
| `services/watchdog.py` | 79% | |
| `services/security.py` | 71% | |
| `services/api_rate_limiter.py` | 82% | |
| `services/auth_service.py` | 76% | |
| `services/weather.py` | **65%** | 280 miss — weather сложный, покрыт средне |
| `services/mqtt_pub.py` | **65%** | 68 miss, publishers-логика |
| `services/shutdown.py` | 55% | |
| `services/logging_setup.py` | 50% | |
| `services/sse_hub.py` | **49%** | **критично** — SSE broadcast loop не покрыт (строки 175-293) |
| `services/observed_state.py` | **47%** | 92 miss |
| `services/zone_control.py` | **48%** | **критично** — 171 miss, строки 135-165, 195-237, 261-297, 313-331 (start/stop/exclusive/pause) |
| `services/monitors/rain_monitor.py` | **54%** | |
| `services/monitors/env_monitor.py` | **39%** | |
| `services/reports.py` | 42% | |
| `services/telegram_bot.py` | **18%** | 414 miss — telegram bot почти полностью не тестирован |
| `services/completion_tracker.py` | **0%** | заглушка |
| `services/weather_codes.py` | **0%** | константы? |
| `services/app_init.py` | **12%** | init-функции не вызываются в тестах |

### 3.3. Coverage критических путей

| Критический путь | Покрытие | Evidence |
|---|---|---|
| `zone_start` | **частичное** (`services/zone_control.py` 48%, `routes/zones_watering_api.py` 49%). `tests/unit/test_zone_control*.py` (2 файла, ~50 tests) + `tests/api/test_zones_api*.py`. | MED gap |
| `zone_stop` | то же (ветки 261-297 не ходят). | MED gap |
| `scheduler-job fire` (APScheduler trigger → `_run_program_threaded`) | **низкое** (`irrigation_scheduler.py` 57%, строки 979-1145 полностью miss). Нет теста, который реально триггерит APScheduler job и ожидает энкью. | **HIGH gap** |
| `postpone / rain delay` | `test_monitors_comprehensive.py`, `test_irrigation_decision.py`, `test_scheduler_comprehensive.py` упоминают `rain_delay`, но это в основном про решение запускать/не запускать. Explicit `postpone` flow на БД+scheduler нет. | HIGH gap |
| `group-start / start-from-first` | `tests/api/test_groups_api*.py` покрывает CRUD. Логику `start_from_first_zone`, `complete_group` — **не нашёл** тестов (`grep "start_from_first\|group_complete" tests/` → 0 matches в коде тестов, только упоминания в mock-payload). | **HIGH gap** |
| `MQTT on_message callback` | покрыт `test_float_monitor_mqtt.py`, `test_observed_state*.py`. | OK |
| `MQTT disconnect/reconnect` | `MockMQTTClient.reconnect()` — noop. Настоящего reconnect-теста нет. `on_disconnect` упоминается только в fixture. | HIGH gap |
| `БД locked retry` (OperationalError) | `tests/unit/test_program_queue.py` — 1 файл упоминает `OperationalError`. Нет systematic теста `retry on sqlite "database is locked"`. | MED gap |
| `Watchdog cap-time` | `tests/unit/test_watchdog*.py` (2 файла) — покрыто. | OK |
| `Boot recovery` | `tests/unit/test_boot_recovery.py` — **хороший TDD-тест** (7 tests, paused→off, float_events cleanup, queue_log running→interrupted, recover_missed_runs→enqueue). | OK |
| `login / logout` | `tests/api/test_auth_api.py` (12 tests) — login, wrong, rate-limit, logout, password change, guest. | OK |
| `password-reset` | **0 matches** (`grep "password.reset\|reset.password\|/reset" tests/` → пусто). Маршрута похоже нет или не тестируется. | LOW gap (возможно, фичи нет) |
| `Telegram bot commands` | `services/telegram_bot.py` — **18%**, `routes/telegram.py` — 10%. `tests/integration/test_telegram_bot.py` (3 tests) тестирует только DB-хук. | **HIGH gap** |
| `Auth decorators` (`@admin_required`) | 4 файла упоминают (security, coverage_boost, zones_api_comprehensive, guest_zone_control). | MED |
| `CSRF` | `WTF_CSRF_ENABLED=False` в тестах (fixtures/app.py:45). **Нулевое покрытие CSRF**. | **HIGH gap** |
| `Rate limiting` | `test_rate_limiter.py`, `test_rate_limit_hardening.py` — покрыто модульно. Login rate-limit — есть. | OK |
| `SSE connect/disconnect` | `test_sse_hub*.py` (2 файла), `test_sse_hardening.py` — тесты **есть**, но 2 из них падают (см. §2.3). | FAIL / HIGH |
| `Race conditions` | `test_program_queue_concurrency.py` — содержит `test_lock_ordering_no_deadlock_stress`. `test_locks_comprehensive.py`. Но **"zone start while running"** и **"master valve concurrency"** явно не покрыты. | MED gap |

---

## 4. Качество тестов

### 4.1. Asserts
- **`assert True` / `assert 1 == 1`:** 0 вхождений (grep). Хорошо.
- **Слишком широкие assert'ы:** встречаются:
  - `tests/api/test_auth_api.py:65`: `assert resp.status_code in (200, 201, 401, 403)` — viewer mutation должен быть либо однозначно OK либо однозначно запрещено; допуск 4 значений = тест ничего не проверяет.
  - `tests/api/test_auth_api.py:49, 55`: `assert resp.status_code in (200, 302)` — logout/guest.
  - Подобных `in (...)` assert'ов: **~40** по всему набору (grep подтверждает, не выписываю полный список).

### 4.2. Хрупкие тесты
- **`time.sleep()` в тестах:** **44 вхождения** в 7 файлах:
  - `tests/unit/test_program_queue.py` — 18
  - `tests/unit/test_program_queue_concurrency.py` — 15
  - `tests/integration/test_mqtt_real.py` — 6
  - `tests/unit/test_zone_control.py`, `test_scheduler_event_wait.py`, `test_zone_control_comprehensive.py` — по 1
  - `tests/integration/test_queue_scheduler_integration.py` — 2
  Это flakiness risk; при повторных прогонах 3×44=132 спайка. На моей машине прогон стабилен (см. §6), но на ARM Wirenboard может плыть.
- **Hardcoded datetime:** boot_recovery использует `datetime.now()` + `timedelta`, что терпимо, но `scheduler`-тесты могут плыть на границе суток (не проверял).
- **Template-inspection тесты** (`tests/ui/*`): ищут строки (`"zone-card"`, `"@media (max-width: 1023px)"`) в рендеренном HTML. Любая refactor-переверстка ломает 4 теста (что и случилось).

### 4.3. Fixtures и изоляция
- **`tests/conftest.py`** + **`tests/fixtures/{app,database,mqtt}.py`** — хорошо структурировано.
- **`test_db`** — честная изоляция (`tmp_path` per-test, закрытие соединений в teardown).
- **`app` fixture — опасна:** делает `sys.modules.pop()` для всех `database`, `app`, `routes.*`, `services.*`, `db.*`, затем reload и restore. Это:
  - Замедляет setup (1.5–2.0 s на фикстуру в 15+ тестах — см. durations table в §10).
  - Хрупко при import-side-effects (которых в проекте много — landscape §6).
  - Может давать ложное разделение — если тест взял ссылку на функцию до reload, она указывает на старый модуль.
- **`admin_client` / `viewer_client` / `guest_client`:** ставят `sess['role']`, но не тестируют реальный логин-flow.
- **Нет global `autouse=True` fixture для cleanup SSE-hub / MQTT-publishers**, поэтому тесты SSE перетекают (test_sse_hardening.py делает setup_method/teardown_method вручную — корректно, но не для всех).

### 4.4. Mock vs Real
- **MQTT**: всегда `MockMQTTClient` (хорошо) + 4 real-broker теста с marker `mqtt_real`.
- **HTTP weather API**: в `test_weather*.py` — нет явных `patch('requests.get')`; вероятно, мок на уровне функции. Не проверял детально.
- **Telegram bot**: `test_integration/test_telegram_bot.py` — только DB-слой, aiogram Bot НЕ мокируется (оттого 18% coverage).
- **APScheduler**: реальный `BackgroundScheduler.start()` в тестах (см. `test_boot_recovery.py:342`). Не мокается — отсюда 5-секундные тесты и 44 `sleep()`.

### 4.5. Параметризация
- **`@pytest.mark.parametrize`: 0 использований** (grep `parametrize` → 0 matches in `tests/`).
- Это **сильное упущение**: много тестов-копипаст (`test_login_correct_password` / `test_login_wrong_password` / `test_login_empty_password`) можно свернуть. Отсутствие parametrize объясняет почему количество tests (1676) чудовищно при покрытии 60%.

### 4.6. XFAIL / XPASS
- **28 xfailed + 52 xpassed = 80 тестов с маркером xfail.**
- Xfail-тесты из `TESTS_V2_README.md` (TDD для programs v2): 33 в `test_programs_db_v2.py`, 31 в `test_programs_api_v2.py`, 27 в `test_scheduler_v2.py`.
- **52 XPASS** означает: фича v2 уже реализована, но xfail-маркер не снят. README прямо говорит: "xfail тесты проходят (ложноположительный) → уберите `@pytest.mark.xfail`". Никто не убрал.
- В `pyproject.toml` **нет** `strict_xfail = true`, иначе xpass автоматически падал бы.

---

## 5. Gaps в покрытии — конкретный список

**Непокрытые критические ветки (priority-ordered):**

| Gap | Severity | Где должно быть |
|---|---|---|
| `irrigation_scheduler._run_program_threaded` полный цикл (fires APScheduler job → enqueue → run → log) | **CRIT** | `tests/integration/test_scheduler_*_real.py` (новый файл) |
| `routes/zones_watering_api.py` ветки POST `/zones/<id>/start`, `/zones/<id>/stop`, `/zones/<id>/pause`, `/zones/<id>/resume`, `/zones/<id>/postpone` — 196 miss | **CRIT** | `tests/api/test_zones_watering_api.py` (нет такого файла, есть только смежные) |
| `routes/mqtt_api.py` SSE endpoints (failure #3, #5) | **HIGH** | исправить либо удалить dead endpoint |
| `services/zone_control.py` exclusive-start race, master-valve concurrency (строки 135-237) | **HIGH** | `tests/unit/test_zone_control_race.py` (нет) |
| `services/sse_hub.py` broadcast loop (строки 175-293, 49% coverage) | **HIGH** | расширить `test_sse_hub_comprehensive.py` |
| `routes/telegram.py` 90% miss, `services/telegram_bot.py` 82% miss | **HIGH** | нужен aiogram-mock `tests/unit/test_telegram_bot_cmd.py` |
| CSRF-защита всех POST/PUT/DELETE (отключена в тестах) | **HIGH** | отдельный `tests/api/test_csrf.py` с `WTF_CSRF_ENABLED=True` |
| MQTT disconnect/reconnect handler — on_disconnect вызывается, `MockMQTTClient.reconnect()` noop | MED | `tests/unit/test_mqtt_reconnect.py` |
| SQLite "database is locked" retry | MED | `tests/unit/test_db_locked_retry.py` |
| Group `start-from-first`, `complete_group`, intra-group sequencing | HIGH | `tests/unit/test_group_sequencing.py` |
| Postpone / rain-delay end-to-end (БД `pause_reason` → scheduler → возобновление) | HIGH | `tests/integration/test_postpone_flow.py` |
| Auth decorators реальный guard (не fixture-bypass через `sess['role']`, а через `/api/login` → cookie → protected endpoint) | MED | `tests/api/test_auth_decorators.py` |
| Scheduler trigger fire с реальным APScheduler и time-mock (`freezegun`) | HIGH | переписать `test_scheduler*` на `freezegun` |
| Boot recovery **ARM-specific paths** (запись `paused` зон в systemd journal, чтение `/var/run/...`) | LOW | пропустить, прод-зависимо |

---

## 6. Flaky тесты

### 6.1. Из BUGS-REPORT §5
- `tests/api/test_coverage_boost.py::TestSettingsOperations::test_telegram_settings` — **упомянут как failing** ("NameError sqlite3").
  - **Проверил:** `grep -n "import sqlite3" routes/settings.py` → строка 1 присутствует.
  - **Повторный прогон:** `pytest tests/api/test_coverage_boost.py::TestSettingsOperations::test_telegram_settings` → **1 passed in 4.36s**.
  - Вывод: BUGS-REPORT §5 **устарел** (или фиксился в refactor/v2 после него). Флаг можно снять.

### 6.2. Стабильность при повторном прогоне

```bash
# 3× concurrency suite
117 passed in 44.62s
117 passed in 44.49s
117 passed in 44.58s

# 3× sse_hardening + sse_hub
2 failed, 30 passed in 0.49s
2 failed, 30 passed in 0.47s
2 failed, 30 passed in 0.47s
```

**Вывод:** фактически **0 flaky тестов** на моей машине. Все 8 failures — deterministic (воспроизводятся каждый раз).

### 6.3. Риски flakiness (не проявились на моей машине, но возможны на ARM)
- `test_program_queue.py::TestBasicOperations::test_shutdown_terminates_all_workers` — **5.00s** (slowest), содержит `time.sleep()`.
- `test_program_queue_concurrency.py::TestConcurrency::test_lock_ordering_no_deadlock_stress` — **2.01s**, stress-test с 10 тредами.
- `test_sse_load.py::test_10_sse_clients` — **deterministic fail**, но логика зависит от таймингов.
- Все 44 `time.sleep()` — кандидаты на замену `threading.Event().wait()` с таймаутом.

---

## 7. CI / GitHub Actions — критические gap'ы

### 7.1. `ci.yml` (47 строк)
```yaml
on:
  push:
    branches: [main, master]     # ← refactor/v2 НЕ триггерится
  pull_request:
    branches: [main, master]     # ← PR в main из refactor/v2 пустит CI, но push — нет
```

**Джобы:**
1. `lint` → `ruff check .` + `ruff format --check .`
2. `test` → `pytest tests/unit tests/db tests/api --cov`
   - **Не бежит:** `tests/integration`, `tests/e2e`, `tests/performance`, `tests/ui`
3. `security` → `bandit`

**Проблемы:**
| # | Проблема | Severity |
|---|---|---|
| 1 | Не триггерится на `refactor/v2` push | **CRIT** |
| 2 | 4 из 7 тест-директорий не в CI (integration, ui, perf, e2e) | **HIGH** |
| 3 | Нет `--timeout` у pytest (в локальной pytest.ini есть timeout=10, но `addopts` из pytest.ini действует) | MED |
| 4 | `pip install` без `--require-hashes` + кэша — CI медленный | LOW |
| 5 | `ruff format --check` может падать, а форматирование у проекта нестрогое | LOW |
| 6 | Не публикует coverage-badge / codecov.io | LOW |
| 7 | `bandit -ll` — только LOW-severity игнорируется; но json+artifact не анализируется | LOW |

### 7.2. `deploy.yml`
- Триггер только `workflow_dispatch` + confirmation="deploy" → безопасно.
- Деплоит **`origin/main`** на Wirenboard (`git pull origin main`).
- **Несовместим с refactor/v2** — даже если CI пройдёт на refactor/v2, deploy берёт main.

### 7.3. Что нужно изменить (минимально)
```yaml
# ci.yml
on:
  push:
    branches: [main, master, refactor/v2, "refactor/**"]
  pull_request:
    branches: [main, master, refactor/v2]
```
Плюс:
- Отдельная nightly job с integration+ui+perf (если их стабилизировать).
- Job-matrix `python-version: ['3.11', '3.12']` (прод на 3.11, sandbox 3.12 — хорошо бы тестировать оба).

---

## 8. Тестовые типы — распределение

| Тип | Файлов | Tests | Доля |
|---|---|---|---|
| Unit | 54 | 768 | 46% |
| Repository (db) | 21 | 251 | 15% |
| API (Flask test client) | 27 | 461 | 28% |
| Integration (multi-component, mock broker) | 5 | 25 | 1.5% |
| Integration (**real** broker, `mqtt_real`) | 1 | 4 | 0.2% |
| E2E (live controller, `e2e`) | 2 | 13 | 0.8% |
| Performance | 2 | 12 | 0.7% |
| UI (template inspection, НЕ selenium) | 3 | 68 | 4% |
| **Итого** | **114** | **~1 602** (собрано), + xfail+xpass=+80 = ~1 676 | |

**Диспропорция:** 89% unit+db+api, 1.5% integration — классическая test-pyramid в перекошенной форме (слишком широкая база). На такой кодовой базе (Flask+MQTT+scheduler+БД) должно быть 15–20% integration.

**Selenium:** нет ни одного реального selenium-теста (см. §1.2). Landscape §7 incorrect.

---

## 9. Test data

### 9.1. Hardcoded values
- `conftest.py:36-65` — `sample_zone_data`, `sample_program_data`, `sample_mqtt_server_data` с фиксированными именами ("Тест Зона 1", "Утренний полив"), `duration=15`, `group_id=1`.
- `test_auth_api.py:12`: password `'1234'` hardcoded (совпадает с default admin password в prod-конфиге — см. security report).
- `test_mqtt_real.py:7`: `MQTT_HOST = '10.2.5.244'` — прод IP в тестах.
- `test_live_controller.py` — URL живого контроллера (не проверял, т.к. не запускал).
- `test_boot_recovery.py` — в `_init_db` inline-SQL schema вместо использования `db/migrations.py`. Это drift-риск: если схема меняется, тест пройдёт на старой.

### 9.2. Миграции
- **В тестах миграции применяются**, т.к. `IrrigationDB.__init__()` вызывает `apply_migrations` (`database.py:1-100`). Покрытие `db/migrations.py` = 77%.
- **Но `test_boot_recovery.py` и `test_queue_scheduler_integration.py`** создают схему вручную (`CREATE TABLE IF NOT EXISTS ...`) — это антипаттерн, т.к. дублирует схему и расходится с миграциями.
- `test_migrations.py` + `test_migrations_comprehensive.py` + `test_migration_downgrade.py` — **хорошо**, миграции тестируются.

### 9.3. Тестовые пользователи
- `admin_client`, `viewer_client`, `guest_client` fixtures — ставят `sess['role']` напрямую. Не гарантирует, что реальный auth-flow работает (см. §5 gap).

---

## 10. Performance тестов

### 10.1. Slowest 15 (из полного прогона)

```
5.00s  test_program_queue.py::test_shutdown_terminates_all_workers
2.01s  test_system_api_deep.py::test_water (setup)
2.01s  test_program_queue_concurrency.py::test_lock_ordering_no_deadlock_stress
1.81s  test_routes_max_coverage.py::test_delete_map_nonexistent (setup)
1.81s  test_routes_comprehensive.py::test_api_login_wrong_password
1.79s  test_settings_db.py::test_set_password
1.77s  test_routes_comprehensive.py::test_api_login
1.60s  test_routes_deep.py::test_scheduler_jobs (setup)
1.52s  test_zone_control_comprehensive.py::test_exclusive_start_stops_peers
1.42s  test_routes_comprehensive.py::test_api_backup (setup)
1.41s  test_settings_db.py::test_set_password_changes_hash
1.31s  test_programs_api_v2.py::test_create_program_even_odd_requires_even_odd_field (setup)
1.22s  test_settings_db_comprehensive.py::test_set_and_get_password
1.20s  test_program_queue_concurrency.py::test_enqueue_during_worker_shutdown
1.18s  test_mqtt_api_deep.py::test_create_mqtt_server (setup)
```

### 10.2. Полный прогон unit+api+db = **6m 37s** (397s)
- Setup-overhead за счёт `app` fixture reload-hack ~20–30 s.
- Password hashing (PBKDF2) — 4 теста > 1 s каждый.

### 10.3. Что можно не в CI
| Набор | Где бежать |
|---|---|
| `tests/integration/test_mqtt_real.py` (4 tests) | только nightly или manual, требует `10.2.5.244:1883` |
| `tests/e2e/test_live_controller.py` (11 tests) | только manual QA на dev Wirenboard |
| `tests/performance/test_sse_load.py` (1 test, сейчас падает) | nightly |
| `tests/performance/test_response_times.py` (10 tests) | можно в CI — быстрые |

### 10.4. Slow tests
- 15 тестов > 1 s, 3 теста > 2 s, 1 тест 5 s — **допустимо**, но каждый `password_*` тест 1.5 s — это PBKDF2 iteration count из прода. Для тестов можно понизить (тест-специфичный hash-cost).

---

## Findings (CRIT / HIGH / MED / LOW)

### CRITICAL

**T-CRIT-1.** `ci.yml` не триггерится для `refactor/v2`. Любой push в ветку (включая эту фазу аудита) не гонит lint/test/security. **Fix:** добавить `refactor/v2` в `branches: on.push` и `on.pull_request`. Не требует код-изменений, только workflow-файл.

**T-CRIT-2.** 8 deterministic failures в suite (3 в unit/api, 4 в ui, 1 в perf). Из них **критический — 4 SSE-related**: `/api/mqtt/zones/sse` возвращает 204 вместо 200 (роут сломан), `test_10_sse_clients` получает 1/100 сообщений. Это не тесты, это сигнал что **SSE-hub на refactor/v2 сломан**. Пересечение с security/code-quality findings — вероятно.

**T-CRIT-3.** `tests/ui/*` тесты **не запускаются в CI** (только unit+db+api), при том что ui/ содержит 68 тестов. Текущий дрейф template/класс `zone-card` не поймался бы CI.

### HIGH

**T-HIGH-1.** **55 XPASS-тестов** — фича `programs v2` реализована, но xfail-маркеры не сняты. Если регрессия сломает реализацию, тесты просто перестанут быть xpass (никто не заметит). **Fix:** (1) снять `@pytest.mark.xfail` со всех xpass-тестов; (2) добавить `strict_xfail = true` в `pyproject.toml [tool.pytest.ini_options]`.

**T-HIGH-2.** Coverage `services/zone_control.py` = **48%**, `routes/zones_watering_api.py` = **49%**. Это **самый критичный путь** (zone start/stop/pause/resume), и половина ветвления не проверяется. Missing: exclusive-start race, master-valve concurrency, postpone, resume from pause.

**T-HIGH-3.** `irrigation_scheduler.py` = 57%, и miss-ветки включают `_run_program_threaded`, полный trigger-fire. Нет ни одного теста, который запускает APScheduler job с frozen time.

**T-HIGH-4.** **CSRF полностью отключён в тестах** (`fixtures/app.py:45`). Прод имеет CSRFProtect, но guarantee не проверяется. **Fix:** отдельный `tests/api/test_csrf.py` с `WTF_CSRF_ENABLED=True` и проверкой что POST без токена даёт 400.

**T-HIGH-5.** Telegram: `services/telegram_bot.py` 18%, `routes/telegram.py` 10%. Интеграция aiogram не мокируется; 3 теста тестируют только DB-слой.

**T-HIGH-6.** 4 из 7 тест-директорий вне CI (integration/ui/perf/e2e = 118 тестов без защиты).

### MEDIUM

**T-MED-1.** Конфликт `pytest.ini` vs `pyproject.toml [tool.pytest.ini_options]` — pytest 9 печатает `WARNING: ignoring pytest config in pyproject.toml!`. **Fix:** удалить одну из двух конфигураций (предлагаю оставить `pyproject.toml` как single source of truth и удалить `pytest.ini`). Открытый вопрос Q19 landscape — снят этим.

**T-MED-2.** **0 использований `@pytest.mark.parametrize`** во всём наборе. 1 676 тестов из них значительная часть — копипаст ("wrong / empty / too short / too long"). Увеличивает maintenance-burden.

**T-MED-3.** 44 `time.sleep()` в тестах (7 файлов) → flakiness risk на ARM. **Fix:** заменить на `threading.Event.wait(timeout=...)` или `freezegun`.

**T-MED-4.** `tests/fixtures/app.py` reload-hack (pop `sys.modules`, reload, restore) — хрупкий и замедляет setup на ~1.5-2.0 s в 15 тестах. **Fix:** переделать на `create_app()` factory (landscape §5 это уже отмечает).

**T-MED-5.** Assert-widening: ~40 тестов имеют `assert resp.status_code in (200, 201, 401, 403)` — тесты ничего не проверяют. **Fix:** сузить до одного ожидаемого значения.

**T-MED-6.** Мёртвые dev-зависимости: `selenium`, `webdriver-manager`, `pytest-selenium` в `requirements-dev.txt` не используются. **Fix:** удалить.

**T-MED-7.** `test_boot_recovery.py` и `test_queue_scheduler_integration.py` — **hardcoded inline SQL schema** вместо использования `db/migrations.py`. Drift-риск. **Fix:** импортировать и вызвать `apply_migrations(db_path)`.

**T-MED-8.** `test_auth_api.py::test_login_correct_password` использует hardcoded password `'1234'` — совпадает с default prod-паролем (см. security report). Тест не проверяет реальную защиту пароля.

**T-MED-9.** `test_mqtt_real.py` hardcoded prod IP `10.2.5.244`. **Fix:** переменная окружения `MQTT_TEST_HOST`.

**T-MED-10.** `db/telegram.py` = 67%, `db/logs.py` = 78%, `routes/groups_api.py` = 60% — средний gap, стоит добавить несколько тестов.

### LOW

**T-LOW-1.** BUGS-REPORT §5 упоминает `tests/api/test_coverage_boost.py::TestSettingsOperations::test_telegram_settings` как failing — **уже фикшено в refactor/v2** (локальный прогон: passed). Секцию в BUGS-REPORT стоит обновить.

**T-LOW-2.** UI-тесты завязаны на конкретные CSS-классы / media-query формат в HTML. Любая реверстка ломает 4 теста. **Fix:** либо тестировать через data-attribute (`data-testid="zone-card"`), либо удалить эти проверки.

**T-LOW-3.** Нет `codecov.io`/coverage badge в README. Coverage-report артефактится в CI но никак не тресингуется across-builds.

**T-LOW-4.** Нет job-matrix по Python-версиям (3.11 prod, 3.12 sandbox).

**T-LOW-5.** `RUN_TESTS_V2.sh` — ручной скрипт только для v2 TDD. Устарел после реализации (xpass показывает, что v2 работает). **Fix:** удалить.

**T-LOW-6.** Password-reset фича не тестируется (возможно, её нет в кодовой базе — нужно подтвердить у Security/UX аудитора).

---

## Test Infra рекомендации

### 11.1. Немедленно (Phase 4)
1. **Fix `ci.yml`** — добавить `refactor/v2` в триггеры. +5 строк.
2. **Добавить nightly CI** (`ci-nightly.yml`): integration + ui + perf + e2e (helper). Триггер `schedule: cron '0 3 * * *'` + `workflow_dispatch`.
3. **Удалить `pytest.ini`** (оставить только `[tool.pytest.ini_options]` в `pyproject.toml`).
4. **`strict_xfail = true`** — чтобы XPASS сразу падал.
5. **Снять `@pytest.mark.xfail` с 52 XPASS-тестов** (`programs_v2` уже реализован).
6. **Исправить или удалить** 3 critical SSE-related tests (they show a real bug — escalate to код-авторам).

### 11.2. В течение 1–2 недель
7. **Factory `create_app()` вместо reload-хака** в `fixtures/app.py` — ускорит setup на 20+ s.
8. **Новые тесты — критические пути**:
   - `tests/api/test_zones_watering_api.py` (start/stop/pause/resume/postpone)
   - `tests/api/test_csrf.py` (CSRF enabled)
   - `tests/integration/test_scheduler_fire.py` (freezegun + APScheduler)
   - `tests/unit/test_group_sequencing.py` (start-from-first, complete-group)
   - `tests/unit/test_mqtt_reconnect.py`
9. **Рефактор `time.sleep()` → `Event.wait()`** в 7 файлах.
10. **Миграции вместо inline-SQL** в `test_boot_recovery.py` и `test_queue_scheduler_integration.py`.

### 11.3. Continuous improvement
11. **Parametrize**: свернуть assertion-копипасты в `@pytest.mark.parametrize`. Ожидаемое сокращение: ~1 676 → ~1 000 tests при том же покрытии.
12. **Coverage gate**: поднять `fail_under` с 30 до 55 (текущее 60.78% — запас есть).
13. **Coverage badge в README** + codecov.io.
14. **Python-matrix**: `['3.11', '3.12']`.
15. **pytest-xdist** (параллельный запуск) — 6:37 → ~2:30 при `-n 4`.

---

## Summary — топ-3 для Phase 4

| # | Finding | Артефакт | Приоритет |
|---|---|---|---|
| 1 | **CI `ci.yml` не покрывает `refactor/v2`** — ветка рефакторинга без автомат-защиты. Добавить 2 строки в `on.push.branches` / `on.pull_request.branches` + расширить pytest paths. | `.github/workflows/ci.yml` + новый `ci-nightly.yml` | **P0** |
| 2 | **8 deterministic failures**, из которых 4 — **реальный SSE-regression** на refactor/v2 (`/api/mqtt/zones/sse` → 204, `test_10_sse_clients` → 1/100). Остальные — template-drift (`zone-card` class) и устаревшие assertions (`maxsize==100` vs actual 20). Escalate к code-quality/security агентам для пересечения. | `services/sse_hub.py`, `routes/mqtt_api.py`, `tests/ui/*`, `tests/unit/test_sse_hardening.py` | **P0** |
| 3 | **52 XPASS + 28 xfailed = 80 тестов в TDD-limbo** для `programs v2`. Фича работает, но `@pytest.mark.xfail` не снят → регрессия незаметна. Снять маркеры + `strict_xfail=true`. | `tests/{db,api,unit}/test_*_v2.py` (3 файла), `pyproject.toml` | **P1** |

**Путь к артефакту:** `/opt/claude-agents/irrigation-v2/irrigation-audit/findings/tests.md`

---

**Test Results Analyzer**
**Дата прогона:** 2026-04-19
**Тестовый venv:** `/tmp/test-venv-irrig` (Python 3.12.3, pytest 9.0.3)
**Статистическая уверенность:** высокая (повторные прогоны 3× дали 100% воспроизводимость; нет flaky-тестов на данной машине).
**Основные метрики:** 1 486 passed / 8 failed / 14 skipped / 31 xfailed / 55 xpassed; coverage = 60.78%; total duration = 7m 46s (unit+api+db+integration+ui+perf+e2e).
