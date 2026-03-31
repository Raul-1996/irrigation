# Programs v2 Tests — TDD Implementation Guide

## Обзор

Три новых файла тестов для функционала Programs v2, написанные в подходе **TDD (Test-Driven Development)**:

1. **`tests/db/test_programs_db_v2.py`** — тесты БД для новых полей
2. **`tests/api/test_programs_api_v2.py`** — тесты API эндпоинтов
3. **`tests/unit/test_scheduler_v2.py`** — тесты scheduler для новых типов расписаний

## Ключевые принципы

### TDD подход
- **Тесты написаны ПЕРЕД кодом** — функционал ещё не реализован
- **Все тесты помечены `@pytest.mark.xfail`** — тест будет падать до реализации функционала
- **Тесты должны быть запускаемыми** — синтаксически корректны, импорты работают

### Когда убирать xfail
После реализации каждого функционала убирайте `@pytest.mark.xfail` для соответствующих тестов:

```python
# До реализации
@pytest.mark.xfail(reason="Not yet implemented: type field")
def test_create_program_with_type_time_based(self, test_db):
    # ...

# После реализации
def test_create_program_with_type_time_based(self, test_db):
    # ...
```

## Структура тестов

### 1. test_programs_db_v2.py (13 классов, 33 теста)

#### Новые поля БД
- **`TestProgramType`** — поле `type` (time-based / smart)
  - Создание с type
  - Дефолт type=time-based
  - Обновление type

- **`TestScheduleTypeWeekdays`** — schedule_type='weekdays'
  - Стандартное расписание по дням недели
  - Дефолт schedule_type=weekdays

- **`TestScheduleTypeEvenOdd`** — schedule_type='even-odd'
  - Чётные дни месяца (even_odd='even')
  - Нечётные дни месяца (even_odd='odd')
  - Обновление на even-odd

- **`TestScheduleTypeInterval`** — schedule_type='interval'
  - Каждые N дней (interval_days=3)
  - interval_days=1 (каждый день)
  - Обновление на interval

- **`TestProgramColor`** — поле `color`
  - Создание с color
  - Дефолт color=#42a5f5
  - Обновление color

- **`TestProgramEnabled`** — поле `enabled`
  - Создание enabled=1 / enabled=0
  - Дефолт enabled=1
  - Toggle enabled

- **`TestExtraTimes`** — поле `extra_times`
  - Создание с extra_times=['12:00', '18:00']
  - Дефолт extra_times=[]
  - Обновление и очистка extra_times

#### Интеграция
- **`TestGetProgramsReturnsNewFields`** — get_program(s) возвращает все новые поля
- **`TestDefaultValuesForExistingPrograms`** — миграция существующих программ
- **`TestDuplicateProgram`** — метод duplicate_program (если добавлен)
- **`TestBackwardCompatibility`** — старый формат продолжает работать

### 2. test_programs_api_v2.py (10 классов, 31 тест)

#### Создание с новыми полями
- **`TestCreateProgramWithNewFields`**
  - POST с type, schedule_type, color, enabled
  - POST с interval + interval_days
  - POST с even-odd + even_odd
  - POST с extra_times

#### Валидация
- **`TestValidation`**
  - Невалидный schedule_type → 400
  - Interval без interval_days → 400
  - Even-odd без even_odd → 400
  - Невалидный type → 400

#### Обновление
- **`TestUpdateProgramWithNewFields`**
  - PUT с новыми полями
  - Обновление schedule на interval

#### Чтение
- **`TestGetProgramsReturnsNewFields`**
  - GET /api/programs возвращает новые поля
  - GET /api/programs/<id> возвращает новые поля

#### Новые эндпоинты
- **`TestToggleProgramEnabled`**
  - PATCH /api/programs/<id>/enabled

- **`TestDuplicateProgram`**
  - POST /api/programs/<id>/duplicate
  - Дублирование несуществующей → 404

- **`TestProgramLog`**
  - GET /api/programs/<id>/log
  - Фильтры: period, limit

- **`TestProgramStats`**
  - GET /api/programs/<id>/stats
  - Статистика: total_runs, total_water, avg_duration

#### Совместимость
- **`TestBackwardCompatibility`**
  - POST/PUT со старым форматом работает

- **`TestCheckConflictsWithExtraTimes`**
  - Проверка конфликтов учитывает extra_times

### 3. test_scheduler_v2.py (9 классов, 27 тестов)

#### Типы расписаний
- **`TestScheduleWeekdaysProgram`**
  - Стандартное weekdays расписание
  - CronTrigger с правильными днями

- **`TestScheduleIntervalProgram`**
  - Interval расписание (каждые N дней)
  - IntervalTrigger
  - Первый запуск СЕГОДНЯ

- **`TestScheduleEvenOddProgram`**
  - Even-odd расписание
  - CronTrigger с чёт/нечет днями месяца

#### Несколько стартов
- **`TestExtraTimes`**
  - extra_times создаёт несколько jobs (main, extra0, extra1)
  - Job naming (program_X_main, program_X_extra0)
  - extra_times с interval schedule
  - Пустой extra_times → один job

#### Включение/выключение
- **`TestEnabledField`**
  - enabled=0 → программа не планируется
  - enabled=1 (дефолт) → программа планируется
  - Toggle enabled → пересоздание jobs

#### Погодокоррекция
- **`TestSmartTypeWeatherAdjustment`**
  - type='smart' → расширенная погодокоррекция
  - type='time-based' → стандартная погодокоррекция

#### Интеграция
- **`TestSchedulerIntegration`**
  - Полная программа v2 планируется
  - Reschedule при обновлении программы
  - Cancel удаляет все jobs (включая extra_times)

- **`TestBackwardCompatibilityScheduler`**
  - Старая программа планируется как weekdays

## Запуск тестов

### Все v2 тесты
```bash
cd /opt/wb-irrigation  # или путь к проекту

# Все три файла
pytest tests/db/test_programs_db_v2.py tests/api/test_programs_api_v2.py tests/unit/test_scheduler_v2.py -v

# Или по отдельности
pytest tests/db/test_programs_db_v2.py -v
pytest tests/api/test_programs_api_v2.py -v
pytest tests/unit/test_scheduler_v2.py -v
```

### Запуск конкретного класса
```bash
pytest tests/db/test_programs_db_v2.py::TestProgramType -v
pytest tests/api/test_programs_api_v2.py::TestDuplicateProgram -v
pytest tests/unit/test_scheduler_v2.py::TestExtraTimes -v
```

### Запуск конкретного теста
```bash
pytest tests/db/test_programs_db_v2.py::TestProgramType::test_create_program_with_type_time_based -v
```

### Показать xfail тесты
```bash
pytest tests/db/test_programs_db_v2.py -v --runxfail
```

## Порядок реализации (рекомендуемый)

### Week 1: БД + миграция
1. Написать миграцию `_migrate_programs_v2_fields` в `db/migrations.py`
2. Обновить `db/programs.py`:
   - `create_program()` — запись новых полей
   - `update_program()` — обновление новых полей
   - `get_program()` / `get_programs()` — чтение новых полей
3. Убрать xfail в `test_programs_db_v2.py` по мере реализации
4. Запустить тесты: `pytest tests/db/test_programs_db_v2.py -v`

### Week 2: API
1. Обновить `routes/programs_api.py`:
   - POST/PUT — принимать новые поля + валидация
   - GET — возвращать новые поля
2. Добавить новые эндпоинты:
   - `POST /api/programs/<id>/duplicate`
   - `PATCH /api/programs/<id>/enabled`
   - `GET /api/programs/<id>/log` (stub)
   - `GET /api/programs/<id>/stats` (stub)
3. Убрать xfail в `test_programs_api_v2.py`
4. Запустить тесты: `pytest tests/api/test_programs_api_v2.py -v`

### Week 3: Scheduler
1. Обновить `irrigation_scheduler.py`:
   - Метод `_schedule_single_time()`
   - Поддержка `schedule_type='interval'` → IntervalTrigger
   - Поддержка `schedule_type='even-odd'` → CronTrigger с днями
   - Поддержка `extra_times` → несколько jobs
   - Проверка `enabled` → skip если 0
2. Убрать xfail в `test_scheduler_v2.py`
3. Запустить тесты: `pytest tests/unit/test_scheduler_v2.py -v`

### Week 4: UI
1. Обновить фронтенд (не покрыто тестами)
2. Интеграционное тестирование

## Fixtures и зависимости

### Используемые fixtures (из conftest.py)
- `test_db` — изолированная БД для каждого теста
- `app` — Flask app с test config
- `admin_client` — клиент с admin правами
- `test_scheduler` — scheduler instance (в test_scheduler_v2.py)

### Импорты
Все тесты корректно импортируют существующие модули:
- `database.IrrigationDB`
- `irrigation_scheduler.IrrigationScheduler`
- `apscheduler.triggers.cron.CronTrigger`
- `apscheduler.triggers.interval.IntervalTrigger`

## Покрытие

### Что покрыто
✅ Все новые поля БД (type, schedule_type, interval_days, even_odd, color, enabled, extra_times)
✅ Дефолтные значения
✅ CRUD операции с новыми полями
✅ Валидация новых полей в API
✅ Новые эндпоинты (duplicate, toggle enabled, log, stats)
✅ Новые типы расписаний (interval, even-odd)
✅ Несколько времён старта (extra_times)
✅ Включение/выключение программы (enabled)
✅ Обратная совместимость (старые программы работают)

### Что НЕ покрыто
❌ UI (фронтенд) — требует e2e тесты
❌ Реальное выполнение программы (integration с MQTT)
❌ Реальная погодокоррекция (требует мок внешних API)
❌ Детали журнала (`/api/programs/<id>/log`) — stub endpoint
❌ Детали статистики (`/api/programs/<id>/stats`) — stub endpoint

## Метрики успеха

### Критерии завершения реализации
1. **БД**: все тесты `test_programs_db_v2.py` проходят (xfail убран)
2. **API**: все тесты `test_programs_api_v2.py` проходят (xfail убран)
3. **Scheduler**: все тесты `test_scheduler_v2.py` проходят (xfail убран)
4. **Coverage**: 100% новых полей и методов покрыты
5. **Integration**: хотя бы одна программа каждого типа создана и запущена
6. **Backward compatibility**: все существующие тесты продолжают проходить

## Troubleshooting

### Тесты не запускаются
```bash
# Проверить pytest установлен
source venv/bin/activate
pip install pytest pytest-timeout

# Проверить fixtures доступны
pytest --fixtures | grep test_db
```

### Тесты падают с ошибками импорта
```bash
# Убедиться что TESTING=1 установлен
export TESTING=1

# Проверить что модули на месте
python3 -c "from database import IrrigationDB; print('OK')"
python3 -c "from irrigation_scheduler import IrrigationScheduler; print('OK')"
```

### xfail тесты проходят (ложноположительный)
Это означает функционал уже реализован! Уберите `@pytest.mark.xfail`.

### Тесты scheduler требуют cleanup
```python
@pytest.fixture
def test_scheduler(test_db):
    scheduler = IrrigationScheduler(test_db, mock_mqtt)
    scheduler.start()
    yield scheduler
    scheduler.stop()  # Важно остановить scheduler
```

## Ссылки

- **Спецификация**: `/specs/programs-v2-spec.md`
- **Прототип UI**: `ui-prototypes-hunter.html`
- **Существующие тесты**: `tests/db/test_programs_db.py`, `tests/api/test_programs_api.py`
- **Fixtures**: `tests/fixtures/app.py`, `tests/fixtures/database.py`

---

**Автор**: OpenClaw AI  
**Дата**: 2026-03-30  
**Версия**: 1.0
