# SPEC: Circular Import Analysis & Fix Plan

**Project:** wb-irrigation (branch `refactor/v2`)  
**Date:** 2026-04-03  
**Author:** auto-generated spec  

---

## 1. Dependency Map (проектные модули)

Ниже — только внутренние зависимости проекта (stdlib/vendor исключены).

```
app.py
  ├── database
  ├── utils
  ├── config
  ├── irrigation_scheduler
  ├── services.mqtt_pub
  ├── services.logging_setup
  ├── services.api_rate_limiter
  ├── services.sse_hub          (injected via init())
  ├── services.events           (try/except, optional)
  ├── services.app_init         ← TOP-LEVEL import
  ├── services.shutdown
  ├── routes.*                  (12 blueprints)
  └── services.telegram_bot     (try/except)

services/app_init.py
  ├── services.shutdown          (top-level)
  ├── irrigation_scheduler       (lazy, inside initialize_app)
  ├── app._start_single_zone_watchdog  ← LAZY import from app  (line 49)
  ├── services.watchdog          (lazy)
  ├── services.zone_control      (lazy)
  ├── utils                      (lazy, inside _boot_sync)
  ├── services.mqtt_pub          (lazy, inside _boot_sync, _warm_mqtt_clients)
  └── services.monitors          (lazy, inside _start_monitors)
```

---

## 2. Найденные циклы

### Цикл 1: `app` ↔ `services.app_init` (ОСНОВНОЙ)

| Параметр | Значение |
|---|---|
| **Severity** | **Medium** |
| **Путь цикла** | `app.py` →(top-level)→ `services.app_init` →(lazy, line 49)→ `app._start_single_zone_watchdog` |
| **Что импортируется** | `app.py`: `from services.app_init import initialize_app` (top-level, line 74). `services/app_init.py`: `from app import _start_single_zone_watchdog` (runtime/lazy, line 49 внутри `initialize_app()`) |
| **Уровень** | app→app_init: **top-level**. app_init→app: **runtime** (внутри функции, в try/except) |
| **Текущий workaround** | Runtime import в `app_init.py` внутри функции `initialize_app()`, обёрнут в `try/except ImportError` |
| **Риск сейчас** | **Низкий для запуска.** К моменту вызова `initialize_app()` из `app.py` (строка ~185), модуль `app` уже полностью загружен, включая `_start_single_zone_watchdog`. Lazy import сработает корректно. Но: это хрупкая конструкция — если кто-то переместит вызов `_initialize_app(app, db)` выше по файлу (до определения `_start_single_zone_watchdog`), получим `ImportError` без понятной причины. |

### Цикл 2: `db.__init__` ↔ `db.zones/programs/groups/...` (ПАКЕТНЫЙ)

| Параметр | Значение |
|---|---|
| **Severity** | **Low** |
| **Путь цикла** | `db/__init__.py` →(top-level)→ `db.zones` →(top-level)→ `db.base`. Обратная ссылка: `db.zones` импортирует `db.base`, а не `db.__init__`, поэтому реального цикла нет. |
| **Текущий workaround** | Нет необходимости — `db.zones` не импортирует `db.__init__`, только `db.base`. Скрипт обнаружения дал false positive из-за `mod.split('.')[0]` |
| **Риск** | **Нулевой.** Это стандартный паттерн Python-пакета: `__init__` импортирует submodules, submodules импортируют sibling (`db.base`). Цикла нет. |

### Не-цикл (но зависимость): `services.sse_hub` и `app`

`sse_hub.py` явно спроектирован без импорта `app` — использует dependency injection через `init()`. Это **правильный** подход, не требует фикса.

### Не-цикл: `services.shutdown` и `app`

`shutdown.py` не импортирует `app` — использует lazy import `database` внутри функции. Безопасно.

---

## 3. Единственный реальный цикл: детальный анализ

### Текущий код

**app.py (строка 74, top-level):**
```python
from services.app_init import initialize_app as _initialize_app
```

**app.py (строка ~185, module-level execution):**
```python
_initialize_app(app, db)
```

**services/app_init.py (строка 49, внутри функции):**
```python
def initialize_app(app, db):
    ...
    try:
        from app import _start_single_zone_watchdog   # ← lazy import
        _start_single_zone_watchdog()
    except ImportError:
        logger.exception('single-zone watchdog start failed')
```

### Почему сейчас работает

1. Python начинает загрузку `app.py`
2. На строке 74 встречает `from services.app_init import initialize_app`
3. Python загружает `services/app_init.py` — тот **не** импортирует `app` на top-level, только `services.shutdown`
4. `services/app_init.py` загружен, `initialize_app` получен
5. Python продолжает загрузку `app.py` до конца, включая определение `_start_single_zone_watchdog`
6. На строке ~185 вызывается `_initialize_app(app, db)`
7. Внутри `initialize_app()` lazy import `from app import _start_single_zone_watchdog` — `app` уже полностью загружен → работает

### Почему это всё равно проблема

- **Хрупкость**: перестановка строк в `app.py` может сломать startup
- **Неочевидность**: разработчик не видит зависимость `app_init → app` без чтения тела функции
- **Тестируемость**: в тестах нужно мокать через `import app; app._start_single_zone_watchdog = ...`
- **Архитектурный запах**: init-модуль не должен зависеть от модуля, который его вызывает

---

## 4. План фикса

### Решение: Dependency Injection (передать функцию как параметр)

Самый чистый подход — `_start_single_zone_watchdog` уже вызывается из `initialize_app`, который вызывается из `app.py`. Значит `app.py` может просто передать функцию.

### Шаг 1: Изменить сигнатуру `initialize_app`

**Файл:** `services/app_init.py`

**Было (строки 23–49):**
```python
def initialize_app(app, db):
    """Run once at boot: scheduler, watchdogs, boot-sync, monitors, MQTT warm-up."""
    global _INIT_DONE
    if _INIT_DONE:
        return
    _INIT_DONE = True

    if app.config.get('TESTING'):
        return

    # ── 1. Scheduler ────────────────────────────────────────────────
    try:
        from irrigation_scheduler import init_scheduler
        init_scheduler(db)
        logger.info('Scheduler initialised')
    except ImportError as e:
        logger.error(f'Scheduler init failed: {e}')

    # ── 2. Single-zone exclusivity watchdog ─────────────────────────
    try:
        from app import _start_single_zone_watchdog
        _start_single_zone_watchdog()
    except ImportError:
        logger.exception('single-zone watchdog start failed')
```

**Стало:**
```python
def initialize_app(app, db, *, start_watchdog_fn=None):
    """Run once at boot: scheduler, watchdogs, boot-sync, monitors, MQTT warm-up.

    Args:
        app: Flask application instance.
        db: database handle.
        start_watchdog_fn: callable to start the single-zone exclusivity
            watchdog.  Injected from app.py to avoid circular import.
    """
    global _INIT_DONE
    if _INIT_DONE:
        return
    _INIT_DONE = True

    if app.config.get('TESTING'):
        return

    # ── 1. Scheduler ────────────────────────────────────────────────
    try:
        from irrigation_scheduler import init_scheduler
        init_scheduler(db)
        logger.info('Scheduler initialised')
    except ImportError as e:
        logger.error(f'Scheduler init failed: {e}')

    # ── 2. Single-zone exclusivity watchdog ─────────────────────────
    if start_watchdog_fn is not None:
        try:
            start_watchdog_fn()
        except Exception:
            logger.exception('single-zone watchdog start failed')
    else:
        logger.warning('start_watchdog_fn not provided, skipping watchdog')
```

### Шаг 2: Обновить вызов в `app.py`

**Файл:** `app.py`

**Было (строка ~185):**
```python
_initialize_app(app, db)
```

**Стало:**
```python
_initialize_app(app, db, start_watchdog_fn=_start_single_zone_watchdog)
```

### Шаг 3: Обновить тесты (если есть mock `_start_single_zone_watchdog`)

Проверить тесты на `initialize_app` — теперь можно передать `start_watchdog_fn=lambda: None` вместо мока модуля.

---

## 5. Порядок применения

| # | Файл | Изменение | Риск |
|---|---|---|---|
| 1 | `services/app_init.py` | Добавить `start_watchdog_fn` kwarg, убрать `from app import` | Нулевой — kwarg с default=None, обратно совместим |
| 2 | `app.py` | Передать `start_watchdog_fn=_start_single_zone_watchdog` | Нулевой — функция уже определена выше по файлу |
| 3 | Тесты | Обновить моки если есть | Зависит от покрытия |

**Общее время на реализацию:** ~15 минут.

---

## 6. Ложные циклы (db package)

Скрипт обнаружения показал циклы в `db` пакете:
```
db.__init__ -> db.zones -> back to db
db.__init__ -> db.programs -> back to db
...
```

Это **не настоящие циклы**. `db.zones` импортирует `db.base` (sibling), а не `db.__init__`. Стандартный паттерн Python-пакета. **Действий не требуется.**

---

## 7. Дополнительные рекомендации (не блокирующие)

1. **`_start_single_zone_watchdog` можно вынести в отдельный модуль** (например `services/watchdog_exclusive.py`), чтобы `app.py` стал тоньше. Это следующий этап рефакторинга, не связан с circular imports.

2. **`services/sse_hub.py` — образцовый паттерн** dependency injection. Использовать как reference для других модулей.

3. **`services/shutdown.py`** использует lazy import `database` внутри функции — это нормально, но если shutdown будет расширяться, стоит перейти на DI (передавать `db` при регистрации). Текущий подход уже частично это делает через `shutdown_all_zones_off(db=db)`.

---

## Итог

| Цикл | Severity | Статус | Фикс |
|---|---|---|---|
| `app` ↔ `services.app_init` | Medium | Работает через lazy import, но хрупко | DI: передать `start_watchdog_fn` как kwarg |
| `db.__init__` ↔ `db.*` | Low | False positive — цикла нет | Нет действий |

**Единственное реальное изменение:** 2 файла, ~5 строк кода. Публичный API не меняется.
