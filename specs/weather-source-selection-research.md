# Погодозависимый полив: исследование и целевая архитектура выбора источника

**Дата:** 2026-06-26
**Объект:** poliv-kg (WB-Techpom 10.2.5.244), Киргизия (42.6531/77.0822)
**Статус:** research / design — предшествует реализации фичи «выбор источника входных данных»
**Автор:** агент Рауля (по задаче weather-irrigation-task)

---

## 0. TL;DR / решение

- **Не делать прогноз точнее — сместить центр тяжести с прогноза на факт.** Вход для коэффициента брать с локальных датчиков; Open-Meteo оставить для упреждения (мороз/ливень впереди) и для **ET₀** (его нельзя померить локально без пиранометра).
- **Текущий метод (Zimmerman + ET₀-множитель) — нижний уровень зрелости.** Серьёзные системы используют **soil water balance** на базе ET₀. Но прыгать туда сразу нельзя: нет двух входов — **дождемера в мм** и **датчика влажности почвы**.
- **Принятое направление (одобрено Раулем 2026-06-26):**
  - **Горизонт 1 (сейчас):** temp/hum → локальный датчик при валидности, иначе Open-Meteo; авария `sensor_mismatch` при расхождении + fallback; freeze-защита по минимуму из (датчик, прогноз); осадки/ET₀ остаются на Open-Meteo; прогнозный дождь заранее в минус не засчитывать.
  - **Горизонт 2 (дорожная карта):** датчик влажности почвы → demand-based; опц. дождемер в мм → честный водный баланс.
  - **Порог расхождения — двухуровневый:** ~5°C soft-warn (UI), ~10°C hard mismatch + fallback. Пороги в `settings.weather.*`.

---

## 1. Как считается коэффициент СЕЙЧАС

`WeatherAdjustment.get_coefficient()` (`services/weather/adjustment.py`) берёт данные через `self._get_weather()` → `get_weather_service().get_weather()`. Цепочка (`service.py`): свежий кэш `weather_cache` (TTL 30 мин) → живой Open-Meteo → устаревший кэш → None.

**Критично:** в расчёт коэффициента идёт **только Open-Meteo**. Локальный датчик WB-MSW (EnvMonitor temp/hum) в формуле **не участвует**. Слияние «датчик > API» (`merge.py`, `SENSOR_STALE_TIMEOUT=600с`) используется **только для UI-виджета** (`/api/weather-merged`), на полив не влияет.

→ Задача — не «подправить выбор источника», а **впервые внедрить** приоритет датчика в движок коэффициента.

**Формула (если `weather.enabled=1`):** сначала hard-safety skip (дождь/мороз/ветер > порога → coef=0, игнорит тумблеры). Иначе `base=100 × temp × humidity × rain × wind × et0`, обрезка [0,200]. Метод — гибрид Zimmerman (OpenSprinkler) + ET₀ (FAO-56). Множители — см. `adjustment.py:288-368`.

**Железо объекта:** локально есть temp + hum (WB-MSW по MQTT) и дискретный датчик дождя (bool). **Нет** датчика влажности почвы, **нет** локального solar/wind. Open-Meteo отдаёт готовый `et0_fao_evapotranspiration` (hourly + daily).

---

## 2. Точность прогнозов — почему скепсис к Open-Meteo обоснован

- **Осадки (QPF) прогнозируются плохо, не лечится.** False Alarm Rate **до 84%**; Fractions Skill Score для дождя 5 мм ≈ **0.29** (порог полезности 0.5). Летние конвективные дожди — события 1–10 км при сетке модели 9–13 км, непредсказуемы даже на 3–6 ч. Для горной Киргизии это особенно актуально.
- **Температура — терпимо, кроме заморозков.** MAE прогноза 24–72ч ≈ **2–4°C**. Для коэффициента норм; для freeze-порога 2°C ошибка = пропущенный заморозок. Локальный датчик ночью в ложбине точнее.
- **Open-Meteo:** микс ECMWF IFS (9км) / ICON / GFS, «best match» по геолокации; признаёт «temperature jumps» при переключении моделей; для точечного прогноза интерполяция по сетке = неизбежная ошибка.

Вывод: Open-Meteo слаб **по осадкам и заморозкам**; по temp/hum днём приемлем, но датчик лучше.

---

## 3. Как делают другие (бенчмарк)

### 3.1 OpenSprinkler — метод Zimmerman (то, что у нас сейчас)
Формула (из исходника `ZimmermanAdjustmentMethod.ts`):
```
humidityFactor = (30 − humidity) × (h/100)         // baseline 30%
tempFactor     = (temp_F − 70) × 4 × (t/100)       // baseline 70°F, ±4%/°F
precipFactor   = (0 − precip_in) × 200 × (r/100)
scale = clamp(100 + humidityFactor + tempFactor + precipFactor, 0, 200)
```
Входы: **вчерашние** усреднённые данные (не прогноз). Ограничения: не видит solar/ветер; веса эмпирические; сам автор OpenSprinkler признал дефолтные веса «too aggressive»; ложные срабатывания на выбросах провайдеров (кейс: 366 мм вместо 20 → газон погиб); не реагирует на факт текущего дождя (issue #116). Поверх рекомендуют: rain sensor, rain delay, weather restrictions, ET-метод (FAO-56).

### 3.2 Коммерческие умные контроллеры
- **Rachio Flex Daily** — полноценный soil water balance (Penman-Monteith, ETc = ET₀×Kc, MAD 50%). Сеть >300k PWS + спутник + радар. **Документированный баг:** заранее засчитывает прогнозный дождь в баланс → недополив, если прогноз не сбылся.
- **RainMachine** — ET-based (ASCE Penman-Monteith, код open-source). **Killer feature — Forecast Correction:** после полива пересчитывает разницу между ET-прогнозом и фактическим ET, корректирует следующий полив. NOAA forecast + PWS correction.
- **Hunter Hydrawise** — Smart ET (reverse-engineered Penman-Monteith на 10 годах истории) + физические rain/soil/flow датчики как hardware override.
- **Netro** — soil water balance + ночное обновление по **фактическим** осадкам; экосистема с сенсором Whisperer (влажность почвы/temp/освещённость).

### 3.3 Консенсус индустрии — иерархия источников
1. **Физический датчик** (влажность почвы, дождь) — реальность
2. **Локальная станция** (факт рядом)
3. **Прогноз** — только упреждение
4. Климатические нормы — fallback

Урок: **в баланс идут ФАКТИЧЕСКИЕ осадки, не прогноз** (Rachio-баг vs RainMachine Forecast Correction).

### 3.4 Агрономия (FAO-56)
- **ET₀ Penman-Monteith** — золотой стандарт, требует temp+hum+wind+**solar radiation** (Rn даёт 70–80% ET₀ в ясный день). Без solar полный PM невозможен → у нас спасает готовый ET₀ от Open-Meteo.
- **Hargreaves-Samani** (ET₀ только по temp) — fallback; в гумидном климате завышает на 20–40%; не для периодов <10 дней.
- **Soil water balance:** `Dr,i = Dr,i-1 + ETc,i − P_eff,i − I_i`; полив когда `Dr ≥ RAW = p×TAW` (p=0.5). Effective rainfall: дожди <12.5 мм ≈ 0% эффективны (USDA SCS).
- **Kc:** cool-season turf 0.90–0.95; warm-season 0.80–0.85; деревья/кусты 0.20–0.90.
- **EPA WaterSense:** «smart» = ET-расчёт + датчик (дождя/влажности почвы); один rain sensor «умным» не считается. Soil moisture даёт **+28%** экономии vs weather-only.

---

## 4. Детект сбойного датчика (для `sensor_mismatch`)

- **Hard limits (физпределы):** temp −51…+54°C (NOAA MADIS); hum 0–100%; осадки ≥0.
- **Rate-of-change:** temp макс ±19°C/ч; hum ±50%/ч (превышение → подозрение).
- **Freshness:** уже есть `SENSOR_STALE_TIMEOUT=600с`. «Stuck sensor»: temp не меняется >0.1°C за 3ч при ветре/солнце — невероятно.
- **Cross-validation двух источников:** практич. порог расхождения temp — **5–8°C** hard-флаг, **3–5°C** soft-warn; NWS cross-check соседних станций — 5.5°C. Ночные расхождения (инверсии) допустимо больше.

→ Спека просила порог 10°C. По индустрии это грубо (поймает только полный отказ). Решение: **двухуровневый порог** (~5°C soft / ~10°C hard+fallback), в настройках.

---

## 5. Целевая архитектура (точка вклинивания)

`WeatherAdjustment._get_weather()` — сейчас отдаёт сырой Open-Meteo. Должен отдавать **выбранный по приоритету источник**:

1. **temp/hum:** локальный датчик, если `value≠None` + в физпределах + свежесть ≤600с; иначе Open-Meteo (`api_fallback`).
2. **Расхождение |local−api|:** ~5°C → soft-warn в UI; ~10°C → fault `sensor_mismatch` (через существующий `system_health`/баннер, `routes/system_status_api.py`) + fallback на Open-Meteo.
3. **freeze-защита:** считать по **минимуму** из (локальный датчик, прогноз min 6ч) — чтобы датчик не «спрятал» заморозок.
4. **осадки/ET₀:** остаются на Open-Meteo; прогнозный дождь заранее в минус не засчитывать.

Логику слияния (`merge.py`) можно переиспользовать — там уже есть приоритет датчика и freshness, но она для UI; нужно поднять её в движок коэффициента.

---

## 6. Источники

**OpenSprinkler/Zimmerman:** `ZimmermanAdjustmentMethod.ts` (OpenSprinkler-Weather); github.com/rszimm/sprinklers_pi/wiki/Weather-adjustments; openthings.freshdesk.com/.../using-weather-adjustments; форумы opensprinkler.com (Zimmerman useless / ET algorithm).
**Коммерция:** support.rachio.com (schedule-types, weather-intelligence); community.rachio.com (Flex Daily forecast-rain credit); support.rainmachine.com (Weather Engine, Forecast Correction); support.hydrawise.com (Smart ET); netrohome.com/faq.
**Агрономия:** fao.org/4/x0490e (FAO-56, Ch.6 Kc, Ch.8 stress); fao.org/4/X5560E (effective rainfall); epa.gov/watersense (WBIC spec v1.1 ANSI/ASABE S627); wucols.ucdavis.edu; ucanr.edu (turfgrass Kc).
**Точность/датчики:** ECMWF Newsletter 174/182; Frontiers in Agronomy 2025 (forecast accuracy irrigation); open-meteo.com/en/features; madis.ncep.noaa.gov (QC checks); PMC 2025 (soil moisture vs weather — 28.8% экономия).
