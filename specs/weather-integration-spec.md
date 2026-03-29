# Спека интеграции погодного виджета в wb-irrigation

**Версия:** 1.0  
**Дата:** 2026-03-29  
**Ветка:** refactor/v2  
**Автор:** AI-agent (Фаза 1)  
**Статус:** Готово к реализации

---

## 1. Анализ текущего состояния

### 1.1 Что уже реализовано

#### `services/weather.py` (298 строк)
- **WeatherData** — парсит hourly/daily из Open-Meteo
- **WeatherService** — singleton, кеш в SQLite (`weather_cache`), TTL 30 мин
- Запрашиваемые hourly: `temperature_2m`, `relative_humidity_2m`, `precipitation`, `wind_speed_10m`, `et0_fao_evapotranspiration`
- Запрашиваемые daily: `precipitation_sum`, `et0_fao_evapotranspiration`
- `forecast_days: 2`
- `get_weather_summary()` — плоский JSON для дашборда
- Fallback на stale cache при недоступности API

#### `services/weather_adjustment.py` (265 строк)
- **WeatherAdjustment** — гибрид Zimmerman + ET₀
- Skip: rain 24ч > порог, rain forecast 6ч > порог, freeze < порог, wind > порог
- Коэффициент 0–200%: temp_factor × humidity_factor × rain_factor × wind_factor × et0_factor
- Настройки из DB: `weather.enabled`, `weather.rain_threshold_mm`, `weather.freeze_threshold_c`, `weather.wind_threshold_kmh`

#### `routes/weather_api.py` (120 строк)
- `GET /api/weather` — плоский JSON (temperature, humidity, precipitation, wind_speed, coefficient, skip)
- `GET/PUT /api/settings/weather` — enabled, пороги
- `GET/PUT /api/settings/location` — lat/lon
- `POST /api/weather/refresh` — принудительное обновление
- `GET /api/weather/log` — лог корректировок

#### `services/monitors.py` (~500 строк)
- **RainMonitor** — MQTT, `is_rain: bool`, NO/NC логика, автоматический rain postpone
- **EnvMonitor** — MQTT, `temp_value`, `hum_value`, `last_temp_rx_ts`, `last_hum_rx_ts`
- **WaterMonitor** — MQTT, импульсы, расход
- Синглтоны: `rain_monitor`, `env_monitor`, `water_monitor`

#### `irrigation_scheduler.py` (интеграция)
- `_check_weather_skip()` — вызывает `WeatherAdjustment.should_skip()` перед поливом
- `_get_weather_adjusted_duration()` — умножает длительность на coefficient/100

#### UI виджет (текущий)
- `templates/status.html:551` — info-box: `⛅ Погода: X°C, Y%, 🌧Z mm / Коэфф: N% [SKIP]`
- `static/js/status.js:1275` — `refreshWeatherWidget()`, fetch `/api/weather` каждые 5 мин

#### БД (миграции в `db/migrations.py`)
- `weather_cache` — кеш API ответов
- `weather_log` — лог корректировок по зонам
- Настройки в `settings`: `weather.enabled`, `weather.latitude/longitude`, `weather.rain_threshold_mm`, `weather.freeze_threshold_c`, `weather.wind_threshold_kmh`

### 1.2 Чего НЕ хватает

| Компонент | Текущее | Нужно |
|---|---|---|
| Open-Meteo параметры | forecast_days=2, нет weather_code/sunrise/sunset/temp_min_max | forecast_days=3, +weather_code, +sunrise/sunset, +temp_min/max |
| Приоритет датчиков | Локальные и API данные НЕ объединяются | Merged-данные с приоритетом локальных |
| Прогноз 24ч | Не отдаётся в API | Массив {time, temp, precip, wind, weather_code} каждые 4ч |
| Прогноз 3 дня | Не отдаётся в API | Массив {date, temp_min, temp_max, precip_sum, weather_code} |
| Sunrise/sunset | Не запрашиваются | Нужны для виджета |
| Источник данных | Не указан в ответе API | source: "local"/"api"/"api_fallback" per-field |
| Виджет | Одна строка info-box | Полноценный виджет с прогнозом, details, историей |
| Weather decisions | Нет таблицы/API | Таблица + GET /api/weather/decisions |
| Настройки UI | Только пороги rain/freeze/wind | + humidity порог, + toggle per-factor |
| Ветер в UI | км/ч | м/с (требование ТЗ) |
| Заморозок | Только текущая T | + проверка прогноза на 6ч вперёд |

### 1.3 Какие файлы затрагиваются

| Файл | Действие |
|---|---|
| `services/weather.py` | МОДИФИЦИРОВАТЬ — расширить API-запрос, добавить парсинг daily forecast, weather_code, sunrise/sunset |
| `services/weather_adjustment.py` | МОДИФИЦИРОВАТЬ — приоритет датчиков, humidity factor, freeze forecast 6ч |
| `services/weather_merged.py` | СОЗДАТЬ — объединение локальных + API данных |
| `services/et_calculator.py` | СОЗДАТЬ — ET Hargreaves-Samani, Kt, K_precip, cycle-soak |
| `services/irrigation_decision.py` | СОЗДАТЬ — decision table из IRRIGATION-ALGORITHM.md |
| `routes/weather_api.py` | МОДИФИЦИРОВАТЬ — расширить `/api/weather`, добавить `/api/weather/decisions` |
| `db/migrations.py` | МОДИФИЦИРОВАТЬ — добавить миграцию `weather_decisions`, расширить settings |
| `templates/status.html` | МОДИФИЦИРОВАТЬ — заменить info-box на виджет |
| `static/js/status.js` | МОДИФИЦИРОВАТЬ — переписать `refreshWeatherWidget()` |
| `templates/settings.html` | МОДИФИЦИРОВАТЬ — расширить секцию погоды (humidity порог, per-factor toggles) |
| `tests/unit/test_et_calculator.py` | СОЗДАТЬ |
| `tests/unit/test_irrigation_decision.py` | СОЗДАТЬ |
| `tests/unit/test_weather_merged.py` | СОЗДАТЬ |

---

## 2. Бэкенд: ET-расчёт (`services/et_calculator.py`)

### 2.1 Модуль

Новый файл. Чистые функции, без побочных эффектов, легко тестировать. Python 3.9 совместимость.

### 2.2 Функции

```python
# Все из IRRIGATION-ALGORITHM.md, секции 1–2

def lookup_et_base(t_avg: float) -> float:
    """ET_base по таблице 1.1. Возвращает мм/сут."""

def calc_kt(t_avg: float) -> float:
    """Температурный коэффициент Kt по таблице 1.2."""

def calc_k_precip(et_need_mm: float, precip_48h_mm: float) -> float:
    """Вычитает эффективные осадки. Эффективность: 90%/75%/50% по слоям 0-5/5-15/15+ мм."""

def calc_et_corrected(t_avg: float, site_id: str) -> float:
    """ET_corrected = ET_base × Kt × K_altitude × K_lake.
    site_id: 'orsk' | 'cholpon_ata'
    """

def calc_irrigation_need(t_avg: float, precip_48h_mm: float, site_id: str) -> float:
    """Скорректированная норма полива (мм) = calc_k_precip(ET_corrected, precip_48h)."""

def calc_zone_runtime(irrigation_need_mm: float, pr_mm_h: float) -> float:
    """Время_мин = (irrigation_need_mm / pr_mm_h) × 60. Clamp [2, 60]."""

def calc_cycle_soak(runtime_min: float, pr_mm_h: float,
                    max_infiltration_mm_h: float = 15.0) -> list:
    """Разбивает полив на циклы если Pr > скорости инфильтрации.
    Возвращает list[dict] с ключами run_min, soak_min.
    """
```

### 2.3 Константы

```python
ALTITUDE_CORRECTION = {"orsk": 1.0, "cholpon_ata": 1.12}
LAKE_HUMIDITY_FACTOR = {"orsk": 1.0, "cholpon_ata": 0.92}

NOZZLE_PR = {
    "mp_rotator": 10.0,
    "pgp_ultra": 13.0,
    "pro_fixed": 40.0,
    "i20": 15.0,
}

MIN_IRRIGATION_MM = 2.0
MIN_ZONE_RUNTIME_MIN = 2.0
MAX_ZONE_RUNTIME_MIN = 60.0
```

### 2.4 Примечания

- ET-калькулятор — **утилитарный модуль**. Не вызывает DB, не использует MQTT.
- `irrigation_scheduler.py` и `irrigation_decision.py` импортируют его.
- Все формулы дословно из IRRIGATION-ALGORITHM.md секции 1–2.

---

## 3. Бэкенд: Decision Engine (`services/irrigation_decision.py`)

### 3.1 Назначение

Реализация Decision Table из IRRIGATION-ALGORITHM.md (секция 3.2) как набора функций. 12 правил с приоритетами.

### 3.2 Структура

```python
from typing import Optional, Dict, Any, List

# Decision results
DECISION_STOP = 'stop'           # Полив запрещён
DECISION_SKIP = 'skip'           # Пропустить
DECISION_POSTPONE = 'postpone'   # Отложить (ветер)
DECISION_EMERGENCY = 'emergency' # Срочный полив
DECISION_IRRIGATE = 'irrigate'   # Стандартный полив
DECISION_SYRINGE = 'syringe'    # Сиринг

class IrrigationDecision:
    """Результат решения."""
    def __init__(self, decision, reason, rule_id, coefficient=100,
                 syringe=False, syringe_time=None, extra=None):
        # type: (str, str, int, int, bool, Optional[str], Optional[Dict]) -> None
        self.decision = decision
        self.reason = reason
        self.rule_id = rule_id
        self.coefficient = coefficient
        self.syringe = syringe
        self.syringe_time = syringe_time
        self.extra = extra or {}

    def to_dict(self):
        # type: () -> Dict[str, Any]
        ...


def evaluate_decision(
    site_id,         # type: str   # "orsk" | "cholpon_ata"
    month,           # type: int
    day,             # type: int
    t_avg,           # type: float
    t_current,       # type: float
    precip_24h,      # type: float
    precip_48h,      # type: float
    precip_forecast_12h,  # type: float
    wind_speed_kmh,  # type: float
    soil_moisture_pct=None,  # type: Optional[float]
):
    # type: (...) -> IrrigationDecision
    """
    Применяет decision table из IRRIGATION-ALGORITHM.md.
    Правила проверяются по приоритету (1→12), первое сработавшее — финальное.
    """
```

### 3.3 Правила (последовательно)

| # | Условие | Результат |
|---|---|---|
| 1 | Вне активного сезона site_id | STOP "off_season" |
| 2 | t_current < 5°C | STOP "frost" |
| 3 | wind_speed_kmh > 25 | POSTPONE "wind" |
| 4 | precip_24h > 5mm | SKIP "rain_24h" |
| 5 | precip_forecast_12h > 5mm | SKIP "rain_forecast" |
| 6 | soil_moisture_pct ≥ 50 | SKIP "soil_moist" |
| 7 | soil_moisture_pct < 30 | EMERGENCY (+20% норма) |
| 8 | irrigation_need < 2mm | SKIP "below_min" |
| 9 | t_current > 35°C (Орск) | IRRIGATE + syringe 13:00 |
| 10 | t_current > 28°C (Чолпон-Ата) | IRRIGATE + syringe 12:00 |
| 11 | Pr зоны > инфильтрации | IRRIGATE + cycle-soak |
| 12 | Прочее | IRRIGATE стандартный |

### 3.4 Сезонные границы (из IRRIGATION-ALGORITHM.md)

```python
SEASON_BOUNDS = {
    "orsk": {"start_month": 4, "start_day": 15, "end_month": 10, "end_day": 31},
    "cholpon_ata": {"start_month": 4, "start_day": 1, "end_month": 10, "end_day": 31},
}
```

### 3.5 Интеграция

Decision engine — **автономный модуль**. НЕ заменяет `irrigation_scheduler.py`, а дополняет:
- `irrigation_scheduler._check_weather_skip()` может вызывать `evaluate_decision()` вместо прямого `should_skip()`
- Но это **опциональная** интеграция. На первом этапе decision engine работает параллельно для записи решений в `weather_decisions`.

---

## 4. Бэкенд: Merged Weather Data (`services/weather_merged.py`)

### 4.1 Назначение

Объединяет данные из Open-Meteo API и локальных MQTT-датчиков с приоритетом локальных.

### 4.2 Матрица приоритетов

| Параметр | Локальный | API | Fallback на API если |
|---|---|---|---|
| Температура | `env_monitor.temp_value` | Open-Meteo `temperature_2m` | `env.temp.enabled == false` ИЛИ `time() - last_temp_rx_ts > 600` |
| Влажность | `env_monitor.hum_value` | Open-Meteo `relative_humidity_2m` | `env.hum.enabled == false` ИЛИ `time() - last_hum_rx_ts > 600` |
| Дождь (факт) | `rain_monitor.is_rain` | Open-Meteo `precipitation > 0` | `rain.enabled == false` ИЛИ `rain_monitor.is_rain is None` |
| Ветер | нет датчика | Open-Meteo `wind_speed_10m` | Всегда API |
| Осадки (mm) | нет датчика | Open-Meteo `precipitation` | Всегда API |
| ET₀ | нет датчика | Open-Meteo `et0_fao_evapotranspiration` | Всегда API |

### 4.3 Таймаут offline

```python
SENSOR_STALE_TIMEOUT = 600  # 10 минут
```

### 4.4 Функция

```python
def get_merged_weather(db_path):
    # type: (str) -> Dict[str, Any]
    """
    Объединяет локальные датчики + Open-Meteo API.
    
    Возвращает dict с ключами:
    - available: bool
    - temperature: {value, source, unit}
    - humidity: {value, source, unit}
    - rain: {value, source}
    - wind_speed: {value, source, unit}
    - precipitation_mm: {value, source, unit}
    - precipitation_24h: float
    - precipitation_forecast_6h: float
    - daily_et0: float
    - weather_code: int | None
    - forecast_24h: list[dict]
    - forecast_3d: list[dict]
    - astronomy: {sunrise, sunset}
    - sensors: {temperature: {enabled, online, last_rx}, ...}
    - timestamp: float
    - cache_age_sec: float
    
    source значения: "local" | "api" | "api_fallback"
    """
```

### 4.5 Логика

```python
import time
from services.monitors import env_monitor, rain_monitor
from services.weather import get_weather_service

def get_merged_weather(db_path):
    STALE_TIMEOUT = 600
    now = time.time()
    
    svc = get_weather_service(db_path)
    weather = svc.get_weather()
    
    if weather is None:
        return {'available': False}
    
    # --- Температура ---
    # 1. Проверить env_monitor.temp_value
    # 2. Если enabled + online (now - last_temp_rx_ts < STALE_TIMEOUT) → source="local"
    # 3. Иначе API value, source="api" или "api_fallback" (если датчик enabled но stale)
    
    # --- Влажность --- (аналогично)
    
    # --- Дождь ---
    # 1. rain_monitor.is_rain если enabled + not None → source="local"
    # 2. Иначе precipitation > 0 → source="api"
    
    # --- Ветер, осадки мм, ET₀ --- всегда API
    
    # --- Прогноз 24ч --- из weather.raw hourly (каждые 4ч, 6 точек)
    # --- Прогноз 3 дня --- из weather.raw daily
    # --- Sunrise/sunset --- из weather.raw daily
    
    return result
```

### 4.6 Интеграция с WeatherAdjustment

`WeatherAdjustment.get_coefficient()` и `should_skip()` должны использовать merged-данные:
- **Температура** для коэффициента — из merged (приоритет локального)
- **Влажность** для коэффициента — из merged (приоритет локального)
- **Дождь** для skip — `rain_monitor.is_rain` имеет приоритет (мгновенная реакция vs API 30-мин кеш)
- **Осадки mm, ET₀, ветер** — всегда из API (нет локальных датчиков)

**Способ реализации:** Добавить опциональный параметр `merged_data` в `get_coefficient()` и `should_skip()`. Если передан — использовать его вместо прямого вызова `self._get_weather()`. Обратная совместимость сохранена.

---

## 5. Бэкенд: Расширение Open-Meteo запроса

### 5.1 Изменения в `services/weather.py`

#### `_fetch_api()` — расширить параметры

```python
params = {
    'latitude': lat,
    'longitude': lon,
    'hourly': ','.join([
        'temperature_2m',
        'relative_humidity_2m',
        'precipitation',
        'wind_speed_10m',
        'et0_fao_evapotranspiration',
        'weather_code',          # NEW
    ]),
    'daily': ','.join([
        'precipitation_sum',
        'et0_fao_evapotranspiration',
        'temperature_2m_max',     # NEW
        'temperature_2m_min',     # NEW
        'weather_code',           # NEW
        'sunrise',                # NEW
        'sunset',                 # NEW
    ]),
    'timezone': 'auto',
    'forecast_days': 3,          # CHANGED: was 2
    'wind_speed_unit': 'ms',     # NEW: м/с вместо км/ч
}
```

**ВАЖНО:** `wind_speed_unit: 'ms'` — Open-Meteo по умолчанию возвращает км/ч. С `ms` будет м/с. Это влияет на пороги ветра! Текущий порог `25 км/ч` → новый порог `7 м/с` (25/3.6 ≈ 6.94).

#### `WeatherData._parse()` — парсить новые поля

```python
# Новые атрибуты WeatherData:
self.weather_code = _safe_get_int(hourly, 'weather_code', idx)  # текущий WMO code

# Daily forecast (3 дня)
self.daily_forecast = []  # list[dict] с temp_min, temp_max, precip_sum, weather_code, sunrise, sunset

# Hourly forecast 24h (каждые 4ч, 6 точек)
self.hourly_forecast_24h = []  # list[dict] с time, temp, precip, wind, weather_code
```

#### `get_weather_summary()` — НЕ ТРОГАТЬ

Оставить как есть для обратной совместимости. Новый расширенный формат — через `get_merged_weather()`.

### 5.2 Обратная совместимость

`/api/weather` будет возвращать **оба** формата — старые плоские поля + новые вложенные:

```json
{
  "available": true,
  
  // === Старые поля (обратная совместимость) ===
  "temperature": 23.5,
  "humidity": 65,
  "precipitation": 0.0,
  "wind_speed": 12.3,
  "precipitation_24h": 2.1,
  "precipitation_forecast_6h": 0.5,
  "daily_et0": 4.2,
  "coefficient": 95,
  "skip": false,
  "skip_reason": "",
  "timestamp": 1711720800.0,
  
  // === Новые поля ===
  "current": { ... },
  "forecast_24h": [ ... ],
  "forecast_3d": [ ... ],
  "astronomy": { ... },
  "sensors": { ... },
  "factors": { ... }
}
```

---

## 6. Бэкенд: Расширение API

### 6.1 `GET /api/weather` — расширенный формат

Новые поля (добавляются к существующим):

```json
{
  "current": {
    "temperature": {"value": 7.0, "source": "local", "unit": "°C"},
    "humidity": {"value": 78, "source": "api", "unit": "%"},
    "rain": {"value": false, "source": "local"},
    "precipitation_mm": {"value": 0.0, "source": "api", "unit": "мм"},
    "wind_speed": {"value": 3.9, "source": "api", "unit": "м/с"},
    "weather_code": 2,
    "weather_icon": "⛅",
    "weather_desc": "Переменная облачность"
  },
  
  "stats": {
    "precipitation_24h": 1.2,
    "precipitation_forecast_6h": 0.3,
    "daily_et0": 2.8
  },
  
  "adjustment": {
    "coefficient": 85,
    "skip": false,
    "skip_reason": "",
    "skip_type": null,
    "factors": {
      "temperature": {"status": "ok", "detail": "7°C — норма"},
      "humidity": {"status": "ok", "detail": "78% < 80%"},
      "rain": {"status": "ok", "detail": "1.2 мм < 5 мм"},
      "freeze": {"status": "ok", "detail": "мин +2°C за 6ч"},
      "wind": {"status": "ok", "detail": "3.9 м/с < 7 м/с"}
    }
  },
  
  "forecast_24h": [
    {"time": "16:00", "temp": 7, "precip": 0.0, "wind": 3.9, "weather_code": 2, "icon": "⛅"},
    {"time": "20:00", "temp": 5, "precip": 0.1, "wind": 2.8, "weather_code": 3, "icon": "☁️"},
    {"time": "00:00", "temp": 3, "precip": 0.8, "wind": 2.2, "weather_code": 61, "icon": "🌧️"},
    {"time": "04:00", "temp": 2, "precip": 1.5, "wind": 3.3, "weather_code": 61, "icon": "🌧️"},
    {"time": "08:00", "temp": 4, "precip": 0.3, "wind": 3.1, "weather_code": 80, "icon": "🌦️"},
    {"time": "12:00", "temp": 9, "precip": 0.0, "wind": 4.2, "weather_code": 2, "icon": "⛅"}
  ],
  
  "forecast_3d": [
    {"date": "2026-03-30", "day_name": "Пн", "temp_min": 2, "temp_max": 8, "precip_sum": 1.2, "weather_code": 2, "icon": "⛅"},
    {"date": "2026-03-31", "day_name": "Вт", "temp_min": 1, "temp_max": 6, "precip_sum": 4.8, "weather_code": 63, "icon": "🌧️"},
    {"date": "2026-04-01", "day_name": "Ср", "temp_min": 3, "temp_max": 11, "precip_sum": 0.0, "weather_code": 0, "icon": "☀️"}
  ],
  
  "astronomy": {
    "sunrise": "06:28",
    "sunset": "19:15"
  },
  
  "sensors": {
    "temperature": {"enabled": true, "online": true, "last_rx": 1711720500.0},
    "humidity": {"enabled": false, "online": false, "last_rx": 0},
    "rain": {"enabled": true, "online": true, "value": false}
  },
  
  "cache_age_sec": 120
}
```

### 6.2 `GET /api/weather/decisions` — история решений (НОВЫЙ)

Не требует admin (read-only).

**Параметры:**
- `days` (int, default 7) — за сколько дней
- `limit` (int, default 50) — макс. записей

**Ответ:**

```json
{
  "decisions": [
    {
      "id": 142,
      "date": "2026-03-29",
      "time": "18:00:00",
      "temperature": 7.0,
      "humidity": 78,
      "precipitation_24h": 1.2,
      "wind_speed": 3.9,
      "coefficient": 85,
      "decision": "adjust",
      "reason": "Облачно, +7°C, коэффициент снижен",
      "mode": "auto",
      "data_sources": {"temperature": "local", "humidity": "api"},
      "user_override": false,
      "created_at": "2026-03-29T18:00:00"
    }
  ],
  "total": 1,
  "stats": {
    "skips_7d": 2,
    "avg_coefficient_7d": 72,
    "water_saved_pct": 28
  }
}
```

### 6.3 `PUT /api/settings/weather` — расширить

Новые поля (добавляются к существующим):

```json
{
  "enabled": true,
  "rain_threshold_mm": 5.0,
  "freeze_threshold_c": 2.0,
  "wind_threshold_ms": 7.0,
  "humidity_threshold_pct": 80.0,
  "humidity_reduction_pct": 30,
  "factors": {
    "rain": true,
    "freeze": true,
    "wind": true,
    "humidity": true,
    "heat": true
  }
}
```

**ВАЖНО:** Ветер теперь в м/с. Миграция: если в БД есть старое значение `weather.wind_threshold_kmh`, конвертировать в м/с → `weather.wind_threshold_ms`. Хранить оба ключа на переходный период.

---

## 7. Бэкенд: Расширение WeatherAdjustment

### 7.1 Изменения в `should_skip()`

#### Заморозок — проверка прогноза 6ч

Текущее: проверяется только `weather.temperature < freeze_threshold`.

Новое: проверять **минимальную** температуру из hourly прогноза на ближайшие 6ч:

```python
# В should_skip():
# Freeze skip: текущая T < порог ИЛИ мин T за ближайшие 6ч < порог
if temp is not None and temp < freeze_threshold:
    # текущая T ниже порога
    ...
    
# NEW: проверяем прогноз
min_temp_6h = _get_min_temp_forecast_6h(weather)
if min_temp_6h is not None and min_temp_6h < freeze_threshold:
    result['skip'] = True
    result['reason'] = 'freeze_forecast_skip: прогноз мин %.1f°C за 6ч (порог %.0f°C)' % (min_temp_6h, freeze_threshold)
    result['details'] = {'type': 'freeze_forecast', 'value': min_temp_6h, 'threshold': freeze_threshold}
    return result
```

#### Ветер — переход на м/с

Текущее: порог в км/ч (`weather.wind_threshold_kmh`), API возвращает км/ч.

Новое: порог в м/с (`weather.wind_threshold_ms`), API запрашивает `wind_speed_unit=ms`.

#### Humidity skip (новый)

Текущее: humidity учитывается только в коэффициенте.

Новое: если humidity > `weather.humidity_threshold_pct` (default 80%) → уменьшить коэффициент на `humidity_reduction_pct` (default 30%).

Это НЕ skip, а дополнительный фактор коэффициента.

### 7.2 Изменения в `get_coefficient()`

- Использовать merged-данные (если переданы)
- Учесть per-factor toggles из настроек

### 7.3 Факторы коррекции — детализация для API

Новый метод `get_factors_detail()`:

```python
def get_factors_detail(self, weather=None):
    # type: (Optional[Any]) -> Dict[str, Dict[str, str]]
    """
    Возвращает детализацию по каждому фактору для виджета.
    
    Формат:
    {
      "rain": {"status": "ok"|"warn"|"danger", "detail": "1.2 мм < 5 мм"},
      "freeze": {"status": "ok"|"warn"|"danger", "detail": "мин +2°C за 6ч"},
      "wind": {"status": "ok"|"warn"|"danger", "detail": "3.9 м/с < 7 м/с"},
      "humidity": {"status": "ok"|"warn"|"danger", "detail": "78% < 80%"},
      "heat": {"status": "ok"|"warn"|"danger", "detail": "7°C — норма"}
    }
    """
```

---

## 8. БД: Миграции

### 8.1 Новая таблица `weather_decisions`

```sql
CREATE TABLE IF NOT EXISTS weather_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    temperature REAL,
    humidity REAL,
    precipitation_24h REAL,
    wind_speed REAL,
    coefficient INTEGER NOT NULL,
    decision TEXT NOT NULL,       -- stop/skip/postpone/irrigate/emergency/adjust/normal
    reason TEXT,
    mode TEXT NOT NULL DEFAULT 'auto',
    data_sources TEXT DEFAULT '{}',
    user_override INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_weather_decisions_date ON weather_decisions(date);
CREATE INDEX IF NOT EXISTS idx_weather_decisions_created ON weather_decisions(created_at);
```

### 8.2 Новые настройки в `settings`

```python
weather_new_keys = {
    'weather.wind_threshold_ms': '7.0',       # NEW (м/с)
    'weather.humidity_threshold_pct': '80.0',  # NEW
    'weather.humidity_reduction_pct': '30',    # NEW
    'weather.factor.rain': '1',               # per-factor toggle
    'weather.factor.freeze': '1',
    'weather.factor.wind': '1',
    'weather.factor.humidity': '1',
    'weather.factor.heat': '1',
}
```

### 8.3 Миграция ветра км/ч → м/с

```python
def _migrate_wind_kmh_to_ms(self, conn):
    """Конвертирует порог ветра из км/ч в м/с."""
    cur = conn.execute("SELECT value FROM settings WHERE key = 'weather.wind_threshold_kmh'")
    row = cur.fetchone()
    if row and row[0]:
        kmh = float(row[0])
        ms = round(kmh / 3.6, 1)
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('weather.wind_threshold_ms', ?)", (str(ms),))
    conn.commit()
```

### 8.4 Порядок миграций

Добавить в `db/migrations.py` → `init_db()`:

```python
self._apply_named_migration(conn, 'weather_create_decisions', self._migrate_create_weather_decisions)
self._apply_named_migration(conn, 'weather_add_extended_settings', self._migrate_add_extended_weather_settings)
self._apply_named_migration(conn, 'weather_wind_kmh_to_ms', self._migrate_wind_kmh_to_ms)
```

---

## 9. Фронтенд: Виджет на status.html

### 9.1 Выбор варианта

Используем **Вариант B компактный** из `weather-widget-final.html` (утверждённый прототип). Это:
- Сводка (иконка + t° + коэфф) → всегда видна
- Прогноз 24ч (горизонтальный скролл)
- Прогноз 3 дня
- `<details>` для подробностей, погодокоррекции, истории решений

### 9.2 Размещение

Заменить текущий `<div class="info-box" id="weather-box">` (строка 551–556 `status.html`) на виджет.

Виджет вставляется **после** блока `top-bar` info-box'ов, **перед** `status-grid`. Обёртка:

```html
<div id="weather-widget" style="display:none;">
  <!-- Виджет из weather-widget-final.html, адаптированный к CSS переменным base.html -->
</div>
```

### 9.3 CSS адаптация

Прототип использует собственные CSS-переменные (`--bg`, `--card`, `--text`, `--muted`, `--border`, `--primary`, `--green`, `--red`, `--warn`, `--chip`, `--radius`). Нужно маппить на переменные `base.html`:

| Прототип | base.html |
|---|---|
| `--bg` | `--background-color` |
| `--card` | `--card-background` |
| `--text` | `--text-color` |
| `--muted` | `#999` (оставить) |
| `--border` | `--border-color` |
| `--primary` | `--primary-color` |
| `--green` | `--success-color` |
| `--red` | `--danger-color` |
| `--warn` | `--warning-color` |

CSS виджета добавляется в блок `{% block extra_css %}` status.html. НЕ в отдельный файл (уменьшить HTTP-запросы на ARM).

### 9.4 HTML структура виджета

```html
<div id="weather-widget" style="display:none;">
  <!-- СВОДКА -->
  <div class="weather-card">
    <div class="weather-summary">
      <div class="weather-summary-left">
        <span class="weather-icon" id="w-icon">⛅</span>
        <div>
          <div class="weather-temp"><span id="w-temp">+7</span><span class="weather-unit">°C</span></div>
          <div class="weather-desc" id="w-desc">Переменная облачность</div>
        </div>
      </div>
      <div class="weather-coeff">
        <div class="weather-coeff-val" id="w-coeff">85%</div>
        <div class="weather-coeff-label">полив</div>
      </div>
    </div>
    <div class="weather-metrics" id="w-metrics">
      <span>💧 78%</span>
      <span>💨 3.9 м/с</span>
      <span>🌧 1.2 мм</span>
    </div>
    <div class="weather-source" id="w-source">📡 WB-MSW (t°) · 🌐 Open-Meteo · 2 мин назад</div>
  </div>

  <!-- ПРОГНОЗ 24Ч -->
  <div class="weather-card">
    <div class="weather-section-title">Прогноз на 24 часа</div>
    <div class="weather-hours" id="w-hours">
      <!-- JS генерирует -->
    </div>
    <div class="weather-hours-legend">осадки мм · ветер м/с</div>
  </div>

  <!-- ПРОГНОЗ 3 ДНЯ -->
  <div class="weather-card">
    <div class="weather-section-title">Прогноз на 3 дня</div>
    <div class="weather-days" id="w-days">
      <!-- JS генерирует -->
    </div>
  </div>

  <!-- ДЕТАЛИ (details/summary — работают на iOS без JS) -->
  <div class="weather-card">
    <details>
      <summary>📊 Подробнее</summary>
      <div class="weather-details-content" id="w-details">
        <!-- JS заполняет -->
      </div>
    </details>

    <details>
      <summary>⚙️ Погодокоррекция</summary>
      <div class="weather-details-content" id="w-factors">
        <!-- JS заполняет факторы -->
      </div>
    </details>

    <details>
      <summary>📋 История решений</summary>
      <div class="weather-details-content" id="w-history">
        <!-- JS заполняет -->
      </div>
    </details>
  </div>
</div>
```

### 9.5 Viewer vs Admin

Виджет — **read-only** для всех ролей. Настройки погодокоррекции (toggles, пороги) доступны только на settings.html для admin.

### 9.6 Удаление старых info-box'ов

Удалить info-box'ы `temp-box`, `hum-box`, `rain-box` (строки 542–550 status.html) — их данные интегрированы в виджет. Удалить `weather-box` (строки 551–556) — заменён виджетом.

---

## 10. Фронтенд: JavaScript (`static/js/status.js`)

### 10.1 Переписать `refreshWeatherWidget()`

Заменить текущую функцию (строки 1275–1313 status.js) на:

```javascript
// --- Weather Widget ---
var WEATHER_ICONS = {
    0: '☀️', 1: '🌤️', 2: '⛅', 3: '☁️',
    45: '🌫️', 48: '🌫️',
    51: '🌦️', 53: '🌦️', 55: '🌧️',
    61: '🌧️', 63: '🌧️', 65: '🌧️',
    71: '❄️', 73: '❄️', 75: '❄️', 77: '❄️',
    80: '🌦️', 81: '🌧️', 82: '🌧️',
    85: '🌨️', 86: '🌨️',
    95: '⛈️', 96: '⛈️', 99: '⛈️'
};

var WEATHER_DESCS = {
    0: 'Ясно', 1: 'Малооблачно', 2: 'Переменная облачность', 3: 'Пасмурно',
    45: 'Туман', 48: 'Туман',
    51: 'Морось', 53: 'Морось', 55: 'Сильная морось',
    61: 'Небольшой дождь', 63: 'Дождь', 65: 'Сильный дождь',
    71: 'Небольшой снег', 73: 'Снег', 75: 'Сильный снег', 77: 'Снежная крупа',
    80: 'Кратковр. дождь', 81: 'Ливень', 82: 'Сильный ливень',
    85: 'Снегопад', 86: 'Сильный снегопад',
    95: 'Гроза', 96: 'Гроза с градом', 99: 'Сильная гроза'
};

function getWeatherIcon(code) {
    return WEATHER_ICONS[code] || '🌡️';
}

function getWeatherDesc(code) {
    return WEATHER_DESCS[code] || '';
}

function formatTemp(v) {
    if (v === null || v === undefined) return '—';
    var sign = v > 0 ? '+' : '';
    return sign + Math.round(v);
}

function formatSource(sensors) {
    var parts = [];
    if (!sensors) return '';
    if (sensors.temperature && sensors.temperature.online) parts.push('📡 WB-MSW (t°)');
    if (sensors.humidity && sensors.humidity.online) parts.push('📡 (💧)');
    if (sensors.rain && sensors.rain.enabled) parts.push('📡 (🌧)');
    parts.push('🌐 Open-Meteo');
    return parts.join(' · ');
}

async function refreshWeatherWidget() {
    try {
        var r = await fetch('/api/weather');
        var j = await r.json();
        var widget = document.getElementById('weather-widget');
        if (!widget) return;
        if (!j || !j.available) { widget.style.display = 'none'; return; }
        widget.style.display = '';
        
        // Сводка
        renderWeatherSummary(j);
        // Прогноз 24ч
        renderForecast24h(j.forecast_24h || []);
        // Прогноз 3 дня
        renderForecast3d(j.forecast_3d || []);
        // Подробности
        renderWeatherDetails(j);
        // Факторы
        renderWeatherFactors(j.adjustment || {});
        // История
        renderWeatherHistory();
    } catch (e) { /* ignore weather fetch errors */ }
}

function renderWeatherSummary(j) { /* ... */ }
function renderForecast24h(items) { /* ... */ }
function renderForecast3d(items) { /* ... */ }
function renderWeatherDetails(j) { /* ... */ }
function renderWeatherFactors(adj) { /* ... */ }

async function renderWeatherHistory() {
    try {
        var r = await fetch('/api/weather/decisions?days=7&limit=10');
        var j = await r.json();
        // Заполнить #w-history
    } catch(e) {}
}

refreshWeatherWidget();
setInterval(refreshWeatherWidget, 5 * 60 * 1000);
```

### 10.2 Индикация источника данных

В сводке виджета: `📡 WB-MSW (t°) · 🌐 Open-Meteo · X мин назад`

Формат строки source:
- `📡` — данные от локального MQTT-датчика
- `🌐` — данные от Open-Meteo API
- `⚠️🌐` — fallback на API (локальный датчик offline)

### 10.3 Цвет коэффициента

```javascript
function coeffColor(c) {
    if (c === 0) return 'var(--danger-color)';
    if (c < 80) return 'var(--warning-color)';
    if (c > 120) return 'var(--primary-color)';
    return 'var(--success-color)';
}
```

---

## 11. Фронтенд: Настройки погоды (`templates/settings.html`)

### 11.1 Расширение секции

Добавить к существующей форме `weather-form`:

```html
<!-- После существующих полей rain/freeze/wind -->

<div class="settings-row">
  <label for="humidity_threshold">Порог влажности (%) → снижение коэффициента</label>
  <div class="settings-input">
    <input type="number" id="humidity_threshold" step="5" min="50" max="100" value="80">
  </div>
</div>

<div class="settings-row">
  <label for="humidity_reduction">Снижение при высокой влажности (%)</label>
  <div class="settings-input">
    <input type="number" id="humidity_reduction" step="5" min="10" max="50" value="30">
  </div>
</div>

<details style="margin-top:8px;">
  <summary style="font-size:0.85rem; cursor:pointer;">⚙️ Факторы коррекции (вкл/выкл)</summary>
  <div style="padding:8px 0;">
    <label style="display:flex; align-items:center; gap:6px; margin:4px 0;">
      <input type="checkbox" id="factor_rain" checked> 🌧️ Дождь
    </label>
    <label style="display:flex; align-items:center; gap:6px; margin:4px 0;">
      <input type="checkbox" id="factor_freeze" checked> ❄️ Заморозок
    </label>
    <label style="display:flex; align-items:center; gap:6px; margin:4px 0;">
      <input type="checkbox" id="factor_wind" checked> 💨 Ветер
    </label>
    <label style="display:flex; align-items:center; gap:6px; margin:4px 0;">
      <input type="checkbox" id="factor_humidity" checked> 💧 Влажность
    </label>
    <label style="display:flex; align-items:center; gap:6px; margin:4px 0;">
      <input type="checkbox" id="factor_heat" checked> 🌡️ Жара
    </label>
  </div>
</details>
```

### 11.2 Изменение label ветра

Текущее: `Порог ветра (км/ч) → отложить`
Новое: `Порог ветра (м/с) → отложить`

Значение по умолчанию: `7` (было `25`).

### 11.3 JavaScript сохранения

Расширить обработчик `weather-save`:

```javascript
// Добавить к существующему PUT /api/settings/weather:
var body = {
    enabled: document.getElementById('weather_enabled').checked,
    rain_threshold_mm: parseFloat(document.getElementById('rain_threshold').value),
    freeze_threshold_c: parseFloat(document.getElementById('freeze_threshold').value),
    wind_threshold_ms: parseFloat(document.getElementById('wind_threshold').value),  // CHANGED
    humidity_threshold_pct: parseFloat(document.getElementById('humidity_threshold').value),
    humidity_reduction_pct: parseInt(document.getElementById('humidity_reduction').value),
    factors: {
        rain: document.getElementById('factor_rain').checked,
        freeze: document.getElementById('factor_freeze').checked,
        wind: document.getElementById('factor_wind').checked,
        humidity: document.getElementById('factor_humidity').checked,
        heat: document.getElementById('factor_heat').checked
    }
};
```

---

## 12. WMO Weather Code → Иконка + Описание

### 12.1 Маппинг (полный)

Используется и на бэкенде (для API), и на фронтенде (для рендера).

```python
# services/weather_codes.py — вынести в отдельный модуль
WEATHER_CODES = {
    0:  ("☀️", "Ясно"),
    1:  ("🌤️", "Малооблачно"),
    2:  ("⛅", "Переменная облачность"),
    3:  ("☁️", "Пасмурно"),
    45: ("🌫️", "Туман"),
    48: ("🌫️", "Изморозь"),
    51: ("🌦️", "Слабая морось"),
    53: ("🌦️", "Морось"),
    55: ("🌧️", "Сильная морось"),
    56: ("🌧️", "Морось с заморозком"),
    57: ("🌧️", "Сильная морось с заморозком"),
    61: ("🌧️", "Небольшой дождь"),
    63: ("🌧️", "Дождь"),
    65: ("🌧️", "Сильный дождь"),
    66: ("🌧️", "Дождь с заморозком"),
    67: ("🌧️", "Сильный дождь с заморозком"),
    71: ("❄️", "Небольшой снег"),
    73: ("❄️", "Снег"),
    75: ("❄️", "Сильный снег"),
    77: ("❄️", "Снежная крупа"),
    80: ("🌦️", "Кратковр. дождь"),
    81: ("🌧️", "Ливень"),
    82: ("🌧️", "Сильный ливень"),
    85: ("🌨️", "Снегопад"),
    86: ("🌨️", "Сильный снегопад"),
    95: ("⛈️", "Гроза"),
    96: ("⛈️", "Гроза с градом"),
    99: ("⛈️", "Сильная гроза с градом"),
}

def get_weather_icon(code):
    # type: (int) -> str
    entry = WEATHER_CODES.get(code)
    return entry[0] if entry else "🌡️"

def get_weather_desc(code):
    # type: (int) -> str
    entry = WEATHER_CODES.get(code)
    return entry[1] if entry else ""
```

---

## 13. Тесты

### 13.1 `tests/unit/test_et_calculator.py`

```python
# Тестируемый модуль: services/et_calculator.py

def test_lookup_et_base_below_5():
    """t_avg < 5 → ET = 0"""

def test_lookup_et_base_ranges():
    """Проверить все 8 диапазонов из таблицы 1.1"""

def test_calc_kt_ranges():
    """Проверить все 9 диапазонов Kt"""

def test_calc_k_precip_no_rain():
    """Нет осадков → норма не меняется"""

def test_calc_k_precip_layers():
    """5мм → 90% эффект., 10мм → 90%×5 + 75%×5, 20мм → полный расчёт"""

def test_calc_k_precip_excess():
    """Осадки > нормы → результат 0 (не отрицательный)"""

def test_calc_et_corrected_orsk():
    """Орск: correction = 1.0 × 1.0 = 1.0"""

def test_calc_et_corrected_cholpon():
    """Чолпон-Ата: correction = 1.12 × 0.92 ≈ 1.03"""

def test_calc_zone_runtime():
    """5mm / 10mm_h → 30 мин"""

def test_calc_zone_runtime_clamp():
    """Ниже 2 → 2, выше 60 → 60"""

def test_calc_cycle_soak_not_needed():
    """Pr <= инфильтрации → один цикл"""

def test_calc_cycle_soak_pro_fixed():
    """Pr=40, 7.5мин → разбивка на циклы по 8 мин + soak 12 мин"""
```

### 13.2 `tests/unit/test_irrigation_decision.py`

```python
# Тестируемый модуль: services/irrigation_decision.py

def test_rule1_off_season():
    """Орск, март → STOP"""

def test_rule2_frost():
    """t_current=3°C → STOP"""

def test_rule3_wind():
    """wind=30 км/ч → POSTPONE"""

def test_rule4_rain_24h():
    """precip_24h=8mm → SKIP"""

def test_rule5_rain_forecast():
    """precip_forecast_12h=7mm → SKIP"""

def test_rule6_soil_moist():
    """soil=55% → SKIP"""

def test_rule7_soil_critical():
    """soil=25% → EMERGENCY (+20%)"""

def test_rule8_below_min():
    """irrigation_need=1.5mm → SKIP"""

def test_rule9_syringe_orsk():
    """Орск, t_current=37 → IRRIGATE + syringe 13:00"""

def test_rule10_syringe_cholpon():
    """Чолпон-Ата, t_current=30 → IRRIGATE + syringe 12:00"""

def test_rule12_normal():
    """Нормальные условия → IRRIGATE"""

def test_priority_frost_over_rain():
    """t=3, rain=10mm → STOP (frost), не SKIP (rain)"""
```

### 13.3 `tests/unit/test_weather_merged.py`

```python
# Тестируемый модуль: services/weather_merged.py

def test_local_temp_priority():
    """Локальный датчик online → source='local'"""

def test_local_temp_stale_fallback():
    """Датчик stale (>10мин) → source='api_fallback'"""

def test_local_temp_disabled():
    """Датчик disabled → source='api'"""

def test_rain_local_priority():
    """RainMonitor.is_rain=True → source='local'"""

def test_wind_always_api():
    """Ветер → всегда source='api'"""

def test_forecast_24h_format():
    """6 точек каждые 4ч"""

def test_forecast_3d_format():
    """3 дня с temp_min/max, precip_sum"""

def test_api_unavailable():
    """API недоступен → available=False"""
```

### 13.4 Существующие тесты

Все 46 существующих тестов (`test_weather.py`, `test_weather_adjustment.py`, `test_weather_deep.py`) ДОЛЖНЫ проходить после изменений. Проверяется на каждом коммите.

---

## 14. Порядок реализации и коммиты

### Этап 1: Новые модули (без изменения существующего)

| # | Задача | Файлы | Коммит |
|---|---|---|---|
| 1 | ET Calculator | `services/et_calculator.py`, `tests/unit/test_et_calculator.py` | `feat: add ET calculator (Hargreaves-Samani)` |
| 2 | Decision Engine | `services/irrigation_decision.py`, `tests/unit/test_irrigation_decision.py` | `feat: add irrigation decision engine` |
| 3 | Weather Codes | `services/weather_codes.py` | (вместе с п.4) |

### Этап 2: Расширение бэкенда

| # | Задача | Файлы | Коммит |
|---|---|---|---|
| 4 | Расширение weather.py | `services/weather.py`, `services/weather_codes.py` | `feat: extend Open-Meteo request (forecast 3d, weather codes, sunrise/sunset)` |
| 5 | Merged weather | `services/weather_merged.py`, `tests/unit/test_weather_merged.py` | `feat: add merged weather with sensor priority` |
| 6 | Weather adjustment update | `services/weather_adjustment.py` | `feat: extend weather adjustment (freeze forecast, humidity, wind m/s)` |
| 7 | API + миграции | `routes/weather_api.py`, `db/migrations.py` | `feat: extend weather API and add decisions table` |

### Этап 3: Фронтенд

| # | Задача | Файлы | Коммит |
|---|---|---|---|
| 8 | Виджет | `templates/status.html`, `static/js/status.js` | `feat: add weather widget to status page` |
| 9 | Настройки | `templates/settings.html` | `feat: add weather correction settings UI` |

### Этап 4: Деплой

| # | Задача | Коммит |
|---|---|---|
| 10 | git push, SSH deploy на .244, тестирование | — |

---

## 15. Риски и mitigation

| Риск | Вероятность | Mitigation |
|---|---|---|
| Сломать существующие тесты | Средняя | Запускать `pytest` после каждого коммита |
| Python 3.9 несовместимость | Высокая | Линтить `python3 -c "import ast; ast.parse(open('file').read())"` с 3.9 syntax |
| Open-Meteo изменит формат | Низкая | Защитное программирование, `_safe_get()` уже есть |
| EnvMonitor `round()` теряет точность | Низкая (для виджета) | Показывать целое, как сейчас. Внутренне использовать float для расчётов |
| Ветер м/с конфликт со старыми настройками | Высокая | Миграция конвертирует km/h → m/s. Читать оба ключа, приоритет нового |
| Большой JSON `/api/weather` на ARM | Низкая | JSON ~2-3 KB, минимальный overhead |

---

## 16. Ограничения текущей спеки

1. **Режимы погодокоррекции** (Semi-auto, Manual override, Rain delay, Seasonal adjust, Disable) — описаны в `weather-widget-spec.md` (секция 9), но **НЕ входят в скоуп текущей интеграции**. Реализовать в следующем спринте. Текущий скоуп: только режим Auto + отображение данных.

2. **Telegram-интеграция** (inline-кнопки при skip, semi-auto) — **НЕ в скоупе**. Текущие Telegram-уведомления о skip продолжают работать как есть.

3. **Фертигация** — отдельный модуль, **НЕ в скоупе**.

4. **Зоны с Pr/area** — в спеке IRRIGATION-ALGORITHM описаны параметры зон (nozzle_type, pr_mm_h, area_m2). Миграция БД для зон **НЕ в скоупе** — ET-калькулятор принимает Pr как параметр, хранение привязки к зонам — позже.

5. **Cycle-soak** — реализуется как чистая функция в ET-калькуляторе, но **интеграция с scheduler НЕ в скоупе**.

6. **Soil moisture** — нет физического датчика. Decision engine принимает `soil_moisture_pct=None` и пропускает правила 6/7.

---

## 17. Чек-лист для разработчика (Фаза 2)

- [ ] Все новые файлы Python совместимы с 3.9: нет `:=`, нет `match/case`, нет `X | Y`, используй `Optional[X]`
- [ ] Все существующие тесты (46 шт) проходят после каждого коммита
- [ ] Новые тесты покрывают ET, Decision, Merged — минимум 25 тестов
- [ ] `/api/weather` возвращает старые поля + новые (обратная совместимость)
- [ ] Ветер в UI — м/с, не км/ч
- [ ] `<details>/<summary>` для выпадающих секций (работает на iOS)
- [ ] CSS адаптирован под переменные `base.html` (light + dark theme)
- [ ] `forecast_days=3` в Open-Meteo запросе
- [ ] `wind_speed_unit=ms` в Open-Meteo запросе
- [ ] Миграция wind threshold км/ч → м/с
- [ ] Freeze skip проверяет прогноз 6ч, не только текущую T
- [ ] Weather codes → emoji маппинг (бэкенд + фронтенд)
- [ ] Прогноз 24ч: 6 точек каждые 4ч
- [ ] Прогноз 3 дня: temp_min/max, precip_sum, weather_code
- [ ] История решений: таблица `weather_decisions`, API `/api/weather/decisions`
- [ ] git branch: `refactor/v2`
- [ ] git push после каждого коммита