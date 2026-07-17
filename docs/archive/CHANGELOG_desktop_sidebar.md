# Desktop Sidebar Layout — Changelog

## 2026-03-31: Реализован desktop sidebar layout для status.html

### Изменения

#### 1. HTML структура (templates/status.html)
- Добавлен wrapper `.desktop-layout` вокруг всего контента
- Создан `<aside class="weather-sidebar">` с:
  - Weather widget (перенесён из основного контента)
  - Active zone indicator (`#sidebar-active-zone`)
  - Water meter widget (`#sidebar-water-meter`)
- Добавлена кнопка `#sidebar-toggle` для сворачивания/разворачивания sidebar
- Основной контент обёрнут в `<main class="main-content">`

#### 2. CSS стили (templates/status.html — блок extra_css)
**Desktop layout:**
- `.desktop-layout` — flex-контейнер, min-height: 100vh
- `.weather-sidebar` — sticky sidebar шириной 300px, position: sticky, top: 0, height: 100vh
- `.main-content` — flex: 1, основной контент
- `.sidebar-toggle` — кнопка на границе sidebar (fixed position)
- `.sidebar-collapsed` — состояние свёрнутого sidebar (width: 0, overflow: hidden)

**Прогноз 24ч:**
- `.weather-24h-grid` — grid с 3 колонками для 6 карточек прогноза
- `.hour-cell`, `.hour-time`, `.hour-icon`, `.hour-temp`, `.hour-detail` — новые классы для карточек

**Active zone indicator:**
- `#sidebar-active-zone` — синяя карточка с градиентом
- `.active-zone-header`, `.active-zone-name`, `.active-zone-timer`, `.active-zone-progress`, `.progress-bar`, `.active-zone-next`

**Water meter:**
- `#sidebar-water-meter` — белая карточка с рамкой
- `.water-meter-header`, `.water-meter-value`, `.water-meter-detail`

**Мобильная адаптация:**
- `@media (max-width: 1023px)` — sidebar наверху (flex-direction: column), полная ширина, кнопка toggle скрыта

**Dark theme:**
- Поддержка тёмной темы через `@media (prefers-color-scheme: dark)`

#### 3. JavaScript (static/js/status.js)
**Изменённые функции:**
- `renderForecast24h()` — рендерит 6 карточек (каждый 4-й час) в grid вместо горизонтального скролла

**Новые функции:**
- `updateActiveZoneIndicator(zones)` — отображает активную зону с таймером и progress bar
- `updateWaterMeter(zones)` — показывает общий расход воды за сегодня + топ-3 зоны
- Sidebar toggle IIFE — обработчик клика на кнопку сворачивания/разворачивания, сохраняет состояние в localStorage

**Интеграция:**
- Вызовы `updateActiveZoneIndicator()` и `updateWaterMeter()` добавлены в `loadZonesData()`

#### 4. Тесты (tests/ui/test_desktop_sidebar.py)
Создано 7 тестов:
1. `test_status_html_has_desktop_layout` — проверка структуры layout
2. `test_status_html_has_active_zone_indicator` — проверка наличия active zone элементов
3. `test_status_html_has_water_meter` — проверка наличия water meter элементов
4. `test_weather_widget_in_sidebar` — проверка что weather-widget внутри sidebar
5. `test_24h_grid_exists` — проверка CSS для grid прогноза 24ч
6. `test_sidebar_collapsed_css` — проверка CSS для collapsed состояния
7. `test_mobile_media_query` — проверка mobile responsive media query

**Результат:** все 7 тестов прошли ✓

### Обратная совместимость
- ✅ Все существующие ID элементов сохранены
- ✅ Мобильная версия не изменилась (sidebar наверху)
- ✅ Weather API не изменился
- ✅ Existing JS функции (renderWeatherSummary, renderForecast3d, renderWeatherDetails, renderWeatherFactors, renderWeatherHistory) работают без изменений
- ✅ Тёмная тема поддерживается

### Файлы изменены
1. `templates/status.html` — HTML + CSS
2. `static/js/status.js` — JS функции
3. `tests/ui/test_desktop_sidebar.py` — новые UI тесты
4. `tests/ui/__init__.py` — создана директория ui tests

### Что НЕ изменилось
- API routes (`/api/*`)
- Сервисы погоды (`services/weather*.py`)
- База данных
- Мобильная версия (только desktop ≥1024px получил sidebar)
- Существующие тесты (все проходят)
