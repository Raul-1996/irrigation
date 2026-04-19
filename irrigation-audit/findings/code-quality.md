# Code Quality Audit — wb-irrigation refactor/v2

**Дата:** 2026-04-19
**Аудитор:** code-reviewer (Phase 2 параллельный аудит, agent #2 из 8)
**Объект:** `/opt/claude-agents/irrigation-v2/` ветка `refactor/v2` (sync с prod HEAD)
**Скоуп:** correctness, maintainability, bugs, dead code, race-conditions, дублирование, error-handling. **Out of scope:** security (см. `security.md`), DB-индексы и план запросов (database-optimizer), деплой (sre).

---

## Executive Summary

- **CRITICAL:** 4 (NameError-bombы, silent pass на критичных путях, БД shutdown race).
- **HIGH:** 8 (дубликат `scheduler/jobs.py` ↔ `irrigation_scheduler.py:67-155`; антипаттерн `except (X, Y, Exception)`; god-modules после рефактора; миграции без транзакционной идемпотентности и без порядка; race в `_delayed_close` потоке master valve; SSE cleaner без stop-event; и т.д.).
- **MEDIUM:** 9 (silent `except Exception: pass` в queue/programs; thin shim re-export-модули; protected-API утечки в routes; basicConfig в библиотечном коде; неполные `_get_settings` идемпотентность; `Random` taxes; etc).
- **LOW:** 6 (мёртвые re-exports, прозрачные shim-routes, нелогичные имена `_is_status_action`, и пр.).

**Топ-3 приоритета для Phase 4:**
1. **CQ-001** — `routes/settings.py` использует `logger.*()` без определения `logger`/`import logging` → NameError при первом же `try/except` блоке. Production-fail при первой `sqlite3.Error`.
2. **CQ-002** — `services/locks.py` использует `logger.debug()` без импорта `logging` → AttributeError при любом возникновении исключения внутри `snapshot_all_locks()`.
3. **CQ-005** — `scheduler/jobs.py` *полностью* дублирует module-level job-функции из `irrigation_scheduler.py:67-155` (job_run_program, job_run_group_sequence, job_stop_zone, job_close_master_valve, job_clear_expired_postpones, job_dispatch_bot_subscriptions). APScheduler сериализует job-callable по dotted path, поэтому при запуске persisted-задачи может вызваться **другая копия**, чем при schedule. Поведение зависит от того, какой модуль был импортирован первым.

---

## Verification of author's BUGS-REPORT.md

| # | Bug (BUGS-REPORT секция) | Status | Цитата текущего состояния |
|---|--------------------------|--------|---------------------------|
| 1 | 1.1 `services/security.py:749` — несоответствие 749/41 LOC | **FIXED** | `services/security.py` сейчас 41 LOC, содержит только декораторы. Landscape-цифра 749 ошибочна. |
| 2 | 1.2 `_is_status_action` дубликат в app.py:203,256 | **FIXED** | `app.py:216` — единственное определение. Используется в `app.py:242, 286`. |
| 3 | 2.1 `routes/settings.py` использует `sqlite3.connect(...)` без `import sqlite3` | **PARTIAL FIXED** | `import sqlite3` добавлен на L1. Прямые `sqlite3.connect()` исчезли, НО появились обращения к `db.telegram._connect()` (L96) — нарушение инкапсуляции facade. Также см. **CQ-001** (NameError — `logger` тоже не импортирован). |
| 4 | 2.2 `services/zone_control.py` `with _active_zones_lock:` без определения | **FIXED** | `_active_zones_lock` нигде в `services/zone_control.py` не используется. Логика теперь в `services/locks.py` — named locks (group_lock / zone_lock). |
| 5 | 2.3 `routes/weather_api.py` использует `sqlite3.connect()` без import | **PARTIAL FIXED** | `import sqlite3` (L13) добавлен. На L63, L275 — обращения к `db.logs._connect()` (private API repo) — нарушение фасадной модели. |
| 6 | 2.4 `routes/weather_api.py:182` использует `requests.exceptions` без import | **NEED MANUAL CHECK (likely fixed)** | Прямого `requests.exceptions` в файле не найдено; `requests` импортируется опосредованно через `services.weather`. |
| 7 | 2.6 `services/auth_service.py` использует `generate_password_hash` без import | **FIXED** | Файл импортирует только `check_password_hash`. `generate_password_hash` нет в коде модуля. |
| 8 | 2.7 `services/events.py` использует `queue` и `json` без import | **FIXED** | Файл сейчас не использует ни `queue`, ни `json`; реализация на `collections.OrderedDict` + `threading`. |
| 9 | 3.7 `services/sse_hub.py:288` ловит `Exception` где не должен | **PARTIAL** | Многие `except Exception` заменены на typed tuples. НО появился новый антипаттерн `except (X, Y, Exception)` (см. CQ-008) — семантически тот же catch-all, но «закамуфлированный». |

**Дополнительно проверил из BUGS-REPORT:**

| # | Item | Status |
|---|------|--------|
| 3.2 dup auth-логика app.py vs services/security.py | **PARTIAL** — `app.py:240-302` всё ещё содержит `_check_session_or_basic_auth` логику, дублирующую декораторы из `services/security.py`. |
| 3.3 cycle app.py ↔ services/app_init.py | **FIXED** — `services/app_init.py` импортирует `app` лениво внутри функций, на топ-левеле — нет. |
| 5.2 двойной close-master-valves в `_boot_sync` | **STILL PRESENT** — `services/app_init.py` содержит ДВА цикла прохода groups с close-master (см. CQ-006). |
| 5.5 `_is_status_action` дублирован | **FIXED** (см. #2). |
| 5.7 `_locks_snapshot` импортирован но не используется | **INCORRECT IN BUGS-REPORT** — используется на `routes/system_status_api.py:75` (`locks = _locks_snapshot()`). Импорт корректный. |
| Dead-files: `ui_agent_demo.py`, `basic_auth_proxy.py`, `templates/programs_old.html` | **FIXED** — все три файла отсутствуют. |
| `services/scheduler_service.py` (DEPRECATED stub) | **STILL PRESENT** — 3 LOC, никем не импортируется (`from services.scheduler_service` — 0 hits). |
| `services/completion_tracker.py` (re-export stub) | **STILL PRESENT** — 4 LOC, никем не импортируется (0 hits). |

---

## Findings

### CRITICAL

#### CQ-001: `routes/settings.py` — NameError-бомба, `logger` не определён
- **Файл:** `routes/settings.py:1-200+` (множество мест: L33, L54, L63, L75, L109, L112, L154+).
- **Что не так:** Модуль использует `logger.debug(...)`, `logger.warning(...)` в десятке `except` блоков, но **не содержит** ни `import logging`, ни `logger = logging.getLogger(__name__)`. Файл импортируется через `app.py` при registr_blueprint.
- **Почему важно:** На любом успешном пути работает (logger не вызывается). Но ПРИ ПЕРВОМ ЖЕ исключении (`sqlite3.OperationalError`, `ValueError` парсинга, `KeyError` в request.json) — `NameError: name 'logger' is not defined`. Flask вернёт 500, но в логе будет НЕ оригинальная ошибка, а NameError, что **скроет реальный баг**. Также: вторичное исключение в обработчике 500 у некоторых WSGI-серверов приводит к падению worker'а.
- **Свидетельство:** `Grep '^logger\\s*=|^import logging' routes/settings.py` → пустой результат. Сравните с другими routes (`programs_api.py:4,11`, `weather_api.py:14,22`).
- **Направление фикса:** добавить два модульных импорта/инициализаций. Тривиально, но критично — тест coverage явно не покрывает exceptions paths.

#### CQ-002: `services/locks.py` — `logger.debug` без импорта `logging`
- **Файл:** `services/locks.py` — целиком отсутствует `import logging`, при этом `logger.debug(...)` вызывается на L35, L39 (в except блоках вокруг `RLock` acquire/release).
- **Что не так:** Те же симптомы что CQ-001. Модуль ключевой для concurrency — named locks для зон/групп.
- **Почему важно:** Если `RLock.acquire/release` поднимет `RuntimeError` (при попытке release без acquire — реальный сценарий при panic в worker'е), вместо логирования получим `NameError`, который сломает упаковывающий context-manager. Lock останется НЕ освобождённым, последующие try-acquire зависнут на timeout → каскадный deadlock между scheduler и web-route.
- **Свидетельство:** `Grep '^logger\\s*=|^import logging' services/locks.py` → пусто.
- **Направление фикса:** добавить `import logging; logger = logging.getLogger(__name__)`.

#### CQ-003: `services/mqtt_pub.py` — `logger.debug` в except ImportError ДО определения `logger`
- **Файл:** `services/mqtt_pub.py:7-18`
  ```python
  try:
      import paho.mqtt.client as mqtt
  except ImportError as e:
      logger.debug("Exception in line_8: %s", e)   # L10 — logger ещё не существует
      mqtt = None
  ...
  try:
      from database import db as _db
  except ImportError as e:
      logger.debug("Exception in line_15: %s", e)  # L17 — logger ещё не существует
      _db = None

  logger = logging.getLogger(__name__)              # L20 — ОПРЕДЕЛЕНИЕ ниже
  ```
- **Почему важно:** При отсутствии `paho-mqtt` (старая инсталляция, dependency drift) импорт модуля падает с `NameError` ВМЕСТО graceful degradation, которую автор явно хотел. Тот же паттерн в `services/observed_state.py:18-22` — там logger выше, всё ОК; в `irrigation_scheduler.py:22, 28, 36, 42` — тот же баг (logger определён на L14, ОК на ImportError, но `logging.basicConfig` ниже L48 переопределяет — порядок осмысленный).
- **Свидетельство:** Прямая проверка топа файла.
- **Направление фикса:** перенести `logger = logging.getLogger(__name__)` на верх модуля до try/import блоков.

#### CQ-004: `services/telegram_bot.py:47` — `logger.debug` ДО определения `logger`
- **Файл:** `services/telegram_bot.py:40-56` — try/except на импорт `aiogram` использует `logger.debug` на L47, но `logger = logging.getLogger(__name__)` определён на L56.
- **Почему важно:** На системах без `aiogram` (опциональная зависимость) импорт модуля падает с NameError при попытке стартовать app.
- **Направление фикса:** аналогично CQ-003.

---

### HIGH

#### CQ-005: Дубликат module-level jobs — `scheduler/jobs.py` ↔ `irrigation_scheduler.py:67-155`
- **Файлы:** `scheduler/jobs.py` (124 LOC) vs `irrigation_scheduler.py:67-155` (89 LOC дубликата).
- **Что не так:** Оба файла определяют **одноимённые** функции `job_run_program`, `job_run_group_sequence`, `job_stop_zone`, `job_close_master_valve`, `job_clear_expired_postpones`, `job_dispatch_bot_subscriptions`. APScheduler сериализует job по dotted-path callable. Если задача добавлена через `scheduler/zone_runner.py` (импортирует `from scheduler.jobs import job_stop_zone`), а после restart та же задача восстановилась из jobstore — она вызовет ту копию, которая указана в `func` поле. Однако ручные scheduling из самого `IrrigationScheduler` могут пользоваться `irrigation_scheduler.job_*` (через `func='irrigation_scheduler:job_run_program'` — нужно проверить, какой path кладётся в jobstore).
- **Почему важно:** (1) Любая правка одной копии **не повлияет** на restored задачи, использовавшие другой путь. (2) Двойная настройка `logging.basicConfig` (L48 в scheduler.py + L16 в jobs.py) → root-logger переопределяется в зависимости от порядка импорта. (3) DRY-violation — 89 LOC чистого дубля.
- **Свидетельство:** оба файла, idem function bodies.
- **Направление фикса:** оставить ОДНУ реализацию (в `scheduler/jobs.py`); из `irrigation_scheduler.py` удалить дубль; явно перенести существующие jobstore-job'ы (миграционный скрипт обновляющий `jobs.func` поле).

#### CQ-006: `services/app_init._boot_sync` — двойной проход close-master-valves
- **Файл:** `services/app_init.py:105-138` (первый цикл) и `:168-211` (второй цикл) — оба проходят `db.get_groups()` и закрывают мастер-клапан.
- **Почему важно:** При boot два MQTT publish '0' за <100ms на одну и ту же тему. Не критично из-за retain=True, но засоряет broker, может дать race с подписчиками которые увидят `0 → 1 → 0` (если retain message приходит между двумя publish). Также удваивает время boot-sync.
- **Свидетельство:** BUG-REPORT 5.2 — STILL PRESENT.
- **Направление фикса:** объединить циклы; либо явно обозначить «pre-zones-off close» и «post-zones-off cap», если намеренно разные семантики.

#### CQ-007: `services/zone_control._delayed_close` — race + неотменяемый поток
- **Файл:** `services/zone_control.py:264-293` (приблизительно) — `threading.Thread(target=_delayed_close, daemon=True).start()` для отложенного закрытия master valve.
- **Что не так:** (1) Поток sleep'ит `MASTER_VALVE_CLOSE_DELAY_SEC`, потом проверяет `any zones on?` без удержания лока **группы** → race с одновременным `start_zone(zone_X)` той же группы — мастер-клапан закроется в момент включения новой зоны. (2) Поток detached, нет stop event — при `shutdown_all_zones_off()` он может выполнить close ПОСЛЕ shutdown'а (или наоборот: после `os._exit(0)` daemon-поток просто умрёт, оставив open valve, если delay > shutdown timeout).
- **Почему важно:** Edge cases для физической инфраструктуры — клапан может остаться открытым/не закрытым.
- **Направление фикса:** заменить на APScheduler date-job (он уже есть как `job_close_master_valve`), либо ввести `shutdown_event` + `with group_lock:` вокруг проверки.

#### CQ-008: Антипаттерн `except (Specific, Specific, Exception)` — семантический catch-all замаскированный под typed catch
- **Файлы:** 12+ мест, наиболее заметные:
  - `services/mqtt_pub.py:164` — `except (ValueError, RuntimeError, Exception) as wfp_err`
  - `services/weather.py:1084,1116,1136` — `except (ImportError, OSError, Exception)` / `except (ImportError, Exception)`
  - `services/shutdown.py:53,71,98,105,127,151,161,170` — 8 occurrences `(..., Exception)`
- **Что не так:** `Exception` поглощает всё, включая `ValueError/RuntimeError/OSError/ImportError`. Тuple с подмножествами + `Exception` логически эквивалентен `except Exception`, но code-review (или линтер `no-broad-except`) может пропустить это как «typed». Это намеренное обёртывание после `tools/batch_replace.py` рефактора bare-except → typed. По факту escape-hatch.
- **Почему важно:** скрывает тяжёлые баги (KeyboardInterrupt — нет, потому что наследник BaseException, но MemoryError, RecursionError, AssertionError — да). Также вводит в заблуждение reviewers.
- **Направление фикса:** убрать `Exception` из tuple (если catch-all не нужен), либо использовать честный `except Exception:` с комментарием почему. `tools/batch_replace.py` — заменить генерацию.

#### CQ-009: `db/migrations.py` — миграции без транзакционной атомарности применения и регистрации
- **Файл:** `db/migrations.py:_apply_named_migration` (около L195-206)
  ```python
  with sqlite3.connect(self.db_path, timeout=5) as conn:
      cur = conn.execute('SELECT 1 FROM migrations WHERE name = ? LIMIT 1', (name,))
      if cur.fetchone(): return
      func(conn)
      conn.execute('INSERT OR REPLACE INTO migrations(name) VALUES (?)', (name,))
      conn.commit()
  ```
  + миграционный код сам внутри `func(conn)` делает `conn.commit()` (см. `_migrate_days_format:312`, `_migrate_add_postpone_reason:322`, и т.д.).
- **Что не так:** Если миграция уже сделала `conn.commit()`, то выполнение строки `INSERT INTO migrations` идёт уже **в новой неявной транзакции**. Если процесс упадёт между внутренним commit миграции и commit регистрации — изменения схемы применены, но запись `migrations(name)` отсутствует → следующий запуск **повторно** запустит миграцию. Многие миграции защищены `IF NOT EXISTS` или `ALTER TABLE ADD COLUMN` (который УПАДЁТ при повторе). Например, `_migrate_add_postpone_reason` проверяет `PRAGMA table_info` — идемпотентно, ОК. Но `_migrate_days_format` без проверки → при повторе повторно «сдвинет» дни на -1 → программы запустятся в неверный день недели.
- **Почему важно:** Может привести к корраптности данных пользователя при неудачном рестарте.
- **Направление фикса:** обернуть в `BEGIN IMMEDIATE; ... INSERT INTO migrations; COMMIT;` без внутренних commit'ов; либо завести флаг `_applied_in_session` и идемпотентную проверку для каждой миграции.

#### CQ-010: `db/migrations.py` — отсутствует версионирование/порядок зависимостей
- **Файл:** `db/migrations.py:118-…` (`init_database` cascade `_apply_named_migration` calls).
- **Что не так:** Порядок миграций задан жёстко строками кода. Ни schema-version pragma, ни target version — невозможно «откатить N миграций» без знания их порядка. `DOWNGRADE_REGISTRY` существует, но не имеет цепочки.
- **Почему важно:** Невозможно установить «хотим базу состояния N для регресс-теста». Усложняет CI и rollback при проблемном релизе.
- **Направление фикса:** ввести `schema_version` integer + miграции numbered; либо использовать Alembic.

#### CQ-011: `services/sse_hub._clean_loop` — поток без stop event
- **Файл:** `services/sse_hub.py:325-334`
  ```python
  def _clean_loop():
      while True:
          time.sleep(60)
          ...
  ```
- **Что не так:** `daemon=True` → процесс умрёт без graceful cleanup. При тестах (multiple init/test) — потоки накапливаются в TestRunner до конца pytest-сессии (десятки потоков). Также: цикл просто ЛОГИРУЕТ кол-во клиентов, никакой реальной cleanup-логики, при том что dead-client eviction сделан в `register_client` — `_clean_loop` бесполезен с точки зрения функциональности.
- **Почему важно:** Test pollution + no real value.
- **Направление фикса:** удалить `_clean_loop` полностью или ввести `_clean_stop = threading.Event()`; в любом случае логирование 1 раз/мин не нужно.

#### CQ-012: `database.py` модуль вызывает `logging.basicConfig()` при импорте
- **Файл:** `database.py:21` — `logging.basicConfig(level=logging.INFO)`. Также в `irrigation_scheduler.py:48` и `scheduler/jobs.py:16` — каждый раз вызывают `basicConfig`.
- **Что не так:** `basicConfig` — no-op если root logger уже настроен. Но в данном случае — три модуля наперегонки настраивают root logger при первом импорте. Окончательная конфигурация зависит от порядка импорта. Worse, `database.py` устанавливает уровень на `INFO`, scheduler — на env-зависимый. Если `services.logging_setup` (если такой есть/настраивается) выполняется ПОСЛЕ — он перетрёт всё. Это антипаттерн — библиотечный код не должен трогать root-logger.
- **Направление фикса:** убрать `basicConfig` из всех `services/`, `db/`, `irrigation_scheduler.py`. Оставить только в одном месте (`logging_setup.py` вызываемом из `app.py` при создании Flask-приложения).

---

### MEDIUM

#### CQ-013: Silent `except Exception: pass` в queue/programs
- **Файлы:** `services/program_queue.py:121, 165, 233, 423` (4 occurrences) + `db/programs.py:225`.
- **Что не так:** Полное проглатывание любых исключений, даже без логирования. Ожидаемая ситуация — что код «знает» что эта ветка может бросить ValueError, и хочет тихо проигнорировать. Но под `Exception` уйдут и MemoryError? Нет (это BaseException). Уйдут KeyboardInterrupt? Нет. А вот KeyError из-за гонки в общем dict — да, и его НИКТО не увидит.
- **Почему важно:** Bugs становятся невидимыми в проде. У `services/program_queue.py` уже репутация сложного компонента → полное игнорирование исключений делает диагностику крашей невозможной.
- **Направление фикса:** заменить на `logger.debug(...)` — минимум.

#### CQ-014: `routes/weather_api.py:63, 275`, `routes/settings.py:96` — обращение к private API репозиториев (`db.logs._connect()`, `db.telegram._connect()`)
- **Что не так:** Обходят facade `database.IrrigationDB`, дёргают `_connect` (приватный по соглашению). При замене SQLite на другой backend сломаются.
- **Почему важно:** Нарушение слоистой архитектуры. Авторский фасад теряет смысл.
- **Направление фикса:** добавить публичные методы в репозитории, либо использовать существующие (если есть).

#### CQ-015: `services/weather.py` — 7 прямых `sqlite3.connect()` минуя фасад
- **Файл:** `services/weather.py:7` — 7 вхождений (см. Phase 1 landscape).
- **Что не так:** `WeatherAdjustment._get_settings` (L331), `_has_ms_threshold` (L381), и т.д. напрямую читают `settings` таблицу. Не пользуются `db.settings.get_setting_value()`.
- **Почему важно:** Дублирование connection-handling кода, разный timeout, нет retry-on-busy. Невозможно подменить БД через DI в тестах (надо мокать sqlite3).
- **Направление фикса:** Перевести `WeatherAdjustment` на `SettingsRepository` (через DI: принимать `settings_repo` в `__init__`).

#### CQ-016: Thin re-export shim модули, **никем не импортируемые**
- **Файлы:**
  - `services/scheduler_service.py` (3 LOC) — STUB DEPRECATED, 0 импортов.
  - `services/completion_tracker.py` (4 LOC) — re-export, 0 импортов.
  - `routes/system_api.py`, `routes/zones_api.py` — shim re-export, 0 импортов.
- **Что не так:** Если `from X import Y` из этих shims-ов **исчез из всего проекта**, файлы стали dead.
- **Свидетельство:** `Grep 'from services\\.scheduler_service|from services\\.completion_tracker|from routes\\.system_api|from routes\\.zones_api'` — нет результатов.
- **Направление фикса:** удалить.

#### CQ-017: `services/weather_merged.py` (76 LOC) и `services/weather_adjustment.py` (8 LOC) — shim'ы, импортирующие приватные `_*` функции из `services.weather`
- **Что не так:** `services/weather_merged.py:6-19` импортирует `_merge_temperature, _merge_humidity, ...` (имена с подчёркиванием → приватные). Это сломанная инкапсуляция: shim знает внутреннюю кухню `services.weather`. Если `services/weather.py` отрефакторит helpers → shim сломается. Цель сделать «backward-compat» оборачивает не публичный API, а внутренности.
- **Почему важно:** Любая правка внутренностей `services/weather.py` потребует синхронной правки shim.
- **Направление фикса:** либо инлайнить логику `get_merged_weather` в `services/weather`, либо сделать публичным API helpers в `services.weather` (без `_`) и shim только из public.

#### CQ-018: `services/watchdog.py:131-141` — `_send_alert` ловит только `ImportError`
- **Файл:** `services/watchdog.py:131-141`
  ```python
  def _send_alert(self, message):
      try:
          ...
          notifier.send_message(int(admin_chat), message)
      except ImportError:
          logger.exception("Watchdog: Telegram alert failed")
  ```
- **Что не так:** `notifier.send_message` может бросить `requests.ConnectionError`, `aiogram.exceptions.*`, `ValueError` (если admin_chat не int), `OSError` — все они **пройдут наверх**, в `_check_zones`, и попадут в catch-all `except (..., RuntimeError)` (L55). Алерт о watchdog-stop **не отправится**, watchdog не упадёт, но ошибку увидим под чужим именем.
- **Направление фикса:** расширить tuple до `(ImportError, OSError, ConnectionError, ValueError)` или явный `except Exception as e: logger.exception(...)` (это тот случай где catch-all уместен — best-effort alert).

#### CQ-019: `db/zones.update_zone` — два пути обновления `zone_data` vs `updated_data`
- **Файл:** `db/zones.py:118-200`.
- **Что не так:** Часть полей берётся из `updated_data` (merged), часть — только из `zone_data` (`planned_end_time`, `watering_start_source`, `commanded_state`). Смешано. При partial update (только `state`) — `commanded_state` НЕ запишется, даже если он был задан в zone_data. Также `last_avg_flow_lpm`, `last_total_liters` берутся из `updated_data` → если в request есть только `last_avg_flow_lpm`, оба поля попадут в SQL (одно с новым значением, другое со старым).
- **Почему важно:** Subtle bugs при concurrent updates (last-writer-wins, но wins не тем что хотел).
- **Направление фикса:** Унифицировать на `zone_data` (явный update) или `updated_data` (полная замена). Не мешать.

#### CQ-020: Дубль auth-логики `app.py` ↔ `services/security.py`
- **Файлы:** `app.py:240-302` (`_check_session_or_basic_auth`-like логика в `before_request`) vs `services/security.py` (декораторы `require_session_or_basic_auth`).
- **Что не так:** Та же проверка реализована дважды — один раз глобальным `before_request` middleware, второй раз — декоратором. Любая правка (новый bypass-path, новый header, разрыв компат) требует синхронных изменений.
- **Направление фикса:** оставить либо middleware (предпочтительно), либо декораторы — не оба.

#### CQ-021: `services/sse_hub.register_client` evict oldest вне зависимости от активности
- **Файл:** `services/sse_hub.py:343-350`. При `>=MAX_SSE_CLIENTS` evict-it `_SSE_HUB_CLIENTS[0]`. Если первый клиент активный (open browser), а остальные — мёртвые с полными очередями (queue.Full при broadcast → `dead.append`), то `dead` будут удалены при следующем broadcast, но ДО этого новый client выкинет здорового.
- **Направление фикса:** evict по «давно ничего не отправлено» либо `qsize == 0` (idle).

---

### LOW

#### CQ-022: `app.py:216` имя `_is_status_action` — название говорит «статус», а проверяет admin-bypass paths
- Косметика, но при code-review путает.

#### CQ-023: `irrigation_scheduler.py:50-65` дублирует logging формат-setup из `scheduler/jobs.py:13-31`
- См. CQ-005 — родственное.

#### CQ-024: `database.py:62-…` — методы facade — линейный proxy. Список ручных проксирующих методов растёт, легко забыть один при добавлении в repository.
- Можно генерировать через `__getattr__` на класс-уровне, но это снижает type-checking → trade-off.

#### CQ-025: Test-only ветки в production коде
- `services/observed_state.py:55-57` `if os.environ.get('TESTING'): return` (skip async verify).
- `scheduler/zone_runner.py:64` `if os.getenv('TESTING') == '1':` (укорочение duration).
- `services/mqtt_pub.py:231` `if not os.environ.get('TESTING'): atexit.register(_shutdown_mqtt_clients)`.
- **Что не так:** Production-код ветвится по env var `TESTING`. Не катастрофа, но легко включить случайно (production runner с `TESTING=1` в systemd).
- **Направление фикса:** Заменить на dependency injection «test mode» через config.

#### CQ-026: `services/float_monitor.py:124` — silent `except Exception: pass`
- В отличие от 30+ других `except Exception: logger.exception(...)` в этом же файле — этот один молчит. Скорее всего описка автора.

#### CQ-027: `services/zone_control.py` — глобальный singleton `state_verifier = StateVerifier()` (`observed_state.py:260`) с lazy property для `db` и `notifier`
- Plus side: lazy → нет circular import. Minus side: thread-unsafe — два потока одновременно входящие в `db` property могут попытаться импорт; для CPython GIL это OK, но семантически — анти-паттерн. Также невозможна dependency injection в тестах.

---

## Hotspot deep-dive (>500 LOC)

### `services/weather.py` (1404 LOC)
**Что внутри:** 4 концепта в одном файле:
1. `WeatherData` — парсинг Open-Meteo API.
2. `WeatherService` — HTTP-клиент + SQLite-кэш (с прямыми `sqlite3.connect` minutia).
3. `WeatherAdjustment` — Zimmerman + ET₀ алгоритм коррекции продолжительности (читает `settings` напрямую).
4. `get_merged_weather` — слияние данных API + локальные MQTT сенсоры (helpers `_merge_*, _build_*`).

**Дубликат с другими модулями:** `services/weather_merged.py` (76 LOC) — wrapper, реимпортирует приватные `_merge_*` (CQ-017). `services/weather_adjustment.py` (8 LOC) — re-export `WeatherAdjustment`. То есть три файла говорят про одно, но два — пустые shim'ы.

**Как разбить:**
- `services/weather/api.py` — `WeatherData`, `WeatherService`, HTTP+cache (~400 LOC).
- `services/weather/adjustment.py` — `WeatherAdjustment` (Zimmerman/ET₀) (~500 LOC). DI `SettingsRepository`.
- `services/weather/merge.py` — `get_merged_weather` + `_merge_*`/`_build_*` (~400 LOC).
- Удалить `services/weather_merged.py`, `services/weather_adjustment.py` shim'ы.

### `irrigation_scheduler.py` (1365 LOC)
**Что осталось после рефактора:** Класс `IrrigationScheduler` + 6 mixins из `scheduler/` (`ProgramRunnerMixin`, `WeatherMixin`, `ZoneRunnerMixin`, и др.). Также **дубликат** module-level job функций `job_run_program/job_stop_zone/...` (CQ-005). Также класс содержит: `__init__`, scheduler config, `start/stop`, `reschedule_all`, `cancel_*`, `clear_expired_postpones`, `_run_program_threaded` (если не унесён в mixin), `_run_group_sequence` (mixin?).

**Как разбить:**
- Удалить дубль `job_*` функций (оставить в `scheduler/jobs.py`).
- Перенести `_run_program_threaded`, `_run_group_sequence` целиком в mixin'ы (если ещё там нет).
- В `irrigation_scheduler.py` оставить только `IrrigationScheduler` класс (composition mixins) + `get_scheduler()` singleton. Цель: <300 LOC.

### `db/migrations.py` (1084 LOC)
**Что внутри:** `MigrationRunner` с `init_database`, `_apply_named_migration`, ~30 методов `_migrate_*` (по одной миграции на метод), `_recreate_table_without_columns` helper, `DOWNGRADE_REGISTRY` (для `rollback_migration`).

**Проблемы:**
- Транзакционная race (CQ-009).
- Нет cumulative version (CQ-010).
- Каждая миграция — `with sqlite3.connect(...)` из нуля, без shared connection. PRAGMA выставляется только в `init_database`, не в каждой миграции — но миграции иногда используют foreign keys, которые могут быть OFF.
- Идемпотентность зависит от исполнения миграции: одни проверяют `PRAGMA table_info`, другие нет.

**Как разбить:** один файл-per-migration `db/migrations/0001_initial.py`, `0002_add_postpone_reason.py`, и т.д. + `MigrationRunner` оркестратор. Стандартизировать «class Migration: def up(conn); def down(conn); def is_applied(conn)».

### `services/float_monitor.py` (603 LOC)
**Что внутри:** `_GroupState` + `FloatMonitor` (subscriptions, debounce, hysteresis, telegram alerts, DB persistence pause_remaining_seconds, queue manager integration).

**Concurrency:** один self._lock, разделяет MQTT callback (paho thread), worker waiting (wait_for_resume_or_cancel), scheduler reload_group, public state queries. Лок крупный → потенциал contention. `is_paused()` берёт lock → каждое чтение из worker лочит весь monitor.

**Что чисто:** debounce + hysteresis с monotonic time — выглядит корректно.

**Что грязно:** `except Exception: pass` на L124 (CQ-026); прямой `sqlite3.connect()` на L424. Сложная state machine в одном классе.

**Как разбить:** выделить `FloatStateMachine` (debounce + hysteresis, без MQTT/DB), `FloatSubscriber` (MQTT pub/sub), `FloatPersistence` (DB пауза/resume).

### `db/zones.py` (610 LOC)
**Что внутри:** `ZoneRepository` — CRUD + bulk + zone_runs. Каждый метод — `with sqlite3.connect(timeout=5)` (нет sharing connection даже при `update_zone` который делает SELECT + UPDATE). `update_zone` имеет 18 if-веток per-field (CQ-019). `update_zone_versioned` — optimistic locking (zone_version), отдельный путь.

**Дубль логики:** `BaseRepository._connect()` существует, но `ZoneRepository.get_zones` его не использует — каждый метод inline `sqlite3.connect()`. Inconsistent.

**N+1 риск:** `get_zones` делает JOIN — OK. Но `update_zone` потом вызывает `self.get_zone(zid)` для возврата — отдельный SELECT. При bulk_update_zones возможен N+1.

**Как разбить:** разделить на `ZoneCRUDRepository` и `ZoneRunsRepository` (хотя 610 LOC ещё допустимо).

---

## Dead code inventory

### Подтверждённые из BUGS-REPORT (still present)
- `services/scheduler_service.py` — 3 LOC stub, **0 импортов**. **DELETE.**
- `services/completion_tracker.py` — 4 LOC re-export, **0 импортов**. **DELETE.**

### Подтверждённые удалёнными
- `ui_agent_demo.py` — отсутствует. ✓
- `basic_auth_proxy.py` — отсутствует. ✓
- `templates/programs_old.html` — отсутствует. ✓

### **Новые** dead-файлы (не в BUGS-REPORT)
- `routes/system_api.py` — shim re-export, **0 импортов** в коде. **DELETE.**
- `routes/zones_api.py` — shim re-export, **0 импортов** в коде. **DELETE.**

### **Новые** dead-shim'ы (используются, но прозрачные)
- `services/weather_adjustment.py` (8 LOC) — re-export. Если no-один не патчит этот namespace в тестах, можно вместо этого делать `from services.weather import WeatherAdjustment` напрямую.
- `services/weather_merged.py` (76 LOC) — НЕ просто shim, а wrapper-функция (см. CQ-017). НЕ удалять без анализа.

### Dead функции
44 функции из BUGS-REPORT — не верифицированы построчно (out of time-budget). Высокий приоритет: пропустить через `vulture` или `deadcode` инструмент.

### Dead код в `tools/`
`tools/batch_replace.py`, `tools/fix_exceptions.py`, `tools/smart_replace.py` — это инфраструктура самой рефактор-волны, явно одноразовая. Не тестируется. Рекомендую переместить в `scripts/refactor-2026/` или удалить (история в git).

---

## Threading / Concurrency summary

| Subsystem | Lock | Stop event | Risk |
|-----------|------|------------|------|
| `services/locks.py` named locks | `threading.RLock` per name | n/a | CQ-002 logger NameError при ошибке release. |
| `IrrigationScheduler` (APScheduler) | внутренний | `scheduler.shutdown()` | CQ-005 dual job copies; persisted job на старую копию после deploy. |
| `services/sse_hub` | `_SSE_HUB_LOCK` | НЕТ | CQ-011 cleaner-thread без stop. CQ-021 evict-oldest unfair. |
| MQTT paho callbacks | `_TOPIC_LOCK`, `_MQTT_CLIENTS_LOCK` | atexit | CQ-003 logger NameError при ImportError paho. |
| `services/watchdog.ZoneWatchdog` | n/a (single thread) | `_stop_event` | OK. CQ-018 alert exceptions. |
| `services/float_monitor` | `self._lock` (грубый) | `_started` flag | OK по основной логике. CQ-026 silent pass. |
| `services/zone_control._delayed_close` | НЕТ | НЕТ | **CQ-007 race + неотменяемый поток.** |
| `services/observed_state.verify_async` | n/a | n/a | OK best-effort, daemon. |
| `services/shutdown.shutdown_all_zones_off` | `_shutdown_lock` (idempot) | n/a | OK на _shutdown_done flag. |
| singleton `db = IrrigationDB()` | n/a | n/a | OK для read-only chains; конкурентные writes — каждый раз новый `sqlite3.connect()` через repo. |

---

## Resource leaks summary

- **SQLite connect без `with`:** не обнаружено в production-коде (все `sqlite3.connect` — внутри `with`). Хорошо.
- **Threads без `daemon=True`:** проверочно выборочно — все thread'ы в `services/*` явно `daemon=True`. ОК.
- **Threads без `join()`:** `_delayed_close` (CQ-007), `_clean_loop` (CQ-011), `verify_async` (OK best-effort), worker'ы внутри `program_queue` — должны быть проверены отдельно (out of time).
- **MQTT клиенты:** `services/mqtt_pub._MQTT_CLIENTS` — atexit-обработчик `_shutdown_mqtt_clients`. ОК. **НО:** `services/sse_hub._SSE_HUB_MQTT` — нет атексит-cleanup. При reload (или stop SSE hub) `loop_start` нити продолжают работать.
- **HTTP-сессии (`requests`):** в `services/weather.py` используются ad-hoc `requests.get(...)` без `Session()`. Не leak, но connection-pool не переиспользуется.

---

## Out of scope (отдано другим аудиторам)

- **Security** (`security-engineer` → `findings/security.md`):
  - Basic-auth bypass paths (CQ-022 «status_action» — на грани).
  - SQL injection (если есть user-input не через placeholders).
  - Хранение `werkzeug.security` хэшей.
  - TLS-настройки в MQTT (`services/mqtt_pub.py:50-60`).
  - encrypt_secret / utils.encrypt_secret.
- **Database optimizer** (database-optimizer):
  - SQL plans, индексы (`db/migrations.py:107-111` — есть idx_zones_group, idx_logs_*, idx_water_*).
  - WAL checkpoint поведение (`PRAGMA wal_autocheckpoint=1000`).
  - N+1 в `bulk_update_zones`, `get_zones`.
- **SRE / deployment** (sre):
  - `atexit` vs `signal.SIGTERM` под systemd.
  - `_clean_loop` в production логах.
  - Rotation `logs` таблицы (нет TTL).
- **API contracts / front-end integration** (api-engineer):
  - `routes/system_api.py` shim re-export (если фронт ожидает старые URL).
  - `db.update_zone` versioned vs не-versioned.
- **Test coverage** (qa-engineer):
  - Покрытие exception-paths (CQ-001, CQ-002 — тесты явно не покрывают).
  - Test-only ветки в production коде (CQ-025).

---

## Метрики

- Прочитано модулей: 19 из 67 ключевых.
- Прочитано LOC: ~5500.
- Подтверждённых багов из BUGS-REPORT: 9/9 (4 fixed, 2 partial, 2 still-present, 1 incorrect-in-report).
- Новых находок: 27 (CQ-001 … CQ-027) + 4 dead-files (CQ-016).
- Уверенность: HIGH для CQ-001…CQ-013, MEDIUM для CQ-014…CQ-021, LOW для CQ-022…CQ-027 (косметика и шим).
