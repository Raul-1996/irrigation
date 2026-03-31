# Desktop Sidebar Layout — Спецификация

## Цель
Перевести страницу status.html на sidebar layout для десктопа (≥1024px).
Погода — в sticky sidebar слева, основной контент — справа.
На мобилке (<1024px) — без изменений (sidebar наверху стеком).

## Что меняется

### 1. Layout container
- `status.html`: добавить wrapper `.desktop-layout` вокруг weather-widget + основного контента
- На десктопе: `display: flex`, sidebar 300px sticky + main flex:1
- На мобилке: `flex-direction: column`, sidebar width: 100%

### 2. Weather sidebar (десктоп)
**Содержимое (сверху вниз):**
1. **Текущая погода** — иконка, температура, описание, коэффициент полива
2. **Метрики** — влажность, ветер, осадки, ET₀
3. **Прогноз 24ч** — 6 карточек с интервалом 4 часа (grid 3×2)
4. **Прогноз 3 дня** — строки: день, иконка, мин-макс°, осадки
5. **Погодокоррекция** — факторы (температура, влажность, ветер, осадки, ET₀, итого)
6. **История решений** — последние 3-5 записей
7. **Активная зона** — карточка "💧 Сейчас поливается: Зона N, осталось X:XX" + progress bar + следующая зона (показывается только при активном поливе)
8. **Расход воды** — общий за сегодня + breakdown по зонам (показывается если water_meter подключён)

**CSS:**
- `position: sticky; top: 0; height: 100vh; overflow-y: auto`
- `width: 300px; border-right: 1px solid var(--border-color)`
- Кнопка ◀ toggle для свёртки sidebar

### 3. JS изменения в status.js

**Прогноз 24ч → 6 карточек:**
```javascript
function renderForecast24h(hours) {
  // Группировка по 4-часовым интервалам: берём часы 06,10,14,18,22,02
  // Или: каждый 4-й элемент из массива
  const intervals = [6, 10, 14, 18, 22, 2];
  const filtered = hours.filter(h => intervals.includes(parseInt(h.time)));
  // Если нет точных попаданий — берём каждый 4-й
  if (filtered.length < 4) {
    filtered = hours.filter((_, i) => i % 4 === 0).slice(0, 6);
  }
  // Рендер в grid 3×2
}
```

**Активная зона (новый блок):**
```javascript
function updateActiveZoneIndicator(zones) {
  const active = zones.find(z => z.state === 'on');
  const el = document.getElementById('sidebar-active-zone');
  if (!active) { el.style.display = 'none'; return; }
  el.style.display = '';
  // Заполнить: имя зоны, оставшееся время (planned_end_time - now), progress bar
  // Следующая зона: найти зону с ближайшим scheduled_start_time
}
```

**Расход воды:**
```javascript
function updateWaterMeter() {
  // Fetch /api/zones, суммировать last_total_liters за сегодня
  // Показать общий расход + top-3 зоны
}
```

**Sidebar toggle:**
```javascript
document.querySelector('.sidebar-toggle').addEventListener('click', () => {
  document.querySelector('.desktop-layout').classList.toggle('sidebar-collapsed');
  localStorage.setItem('sidebar-collapsed', ...); // запомнить состояние
});
```

### 4. HTML изменения в status.html

**До (текущая структура):**
```html
<div id="weather-widget">...</div>
<div class="status-grid" id="groups-container">...</div>
<div class="legend">...</div>
<button class="emergency">...</button>
<div class="zones-section">...</div>
```

**После:**
```html
<div class="desktop-layout">
  <!-- Sidebar -->
  <aside class="weather-sidebar" id="weather-sidebar">
    <div id="weather-widget">
      <!-- Те же блоки, но layout адаптирован -->
    </div>
    <div id="sidebar-active-zone" style="display:none">...</div>
    <div id="sidebar-water-meter" style="display:none">...</div>
  </aside>
  <button class="sidebar-toggle" id="sidebar-toggle">◀</button>
  
  <!-- Main -->
  <main class="main-content">
    <div class="top-bar">...</div>
    <div class="status-grid" id="groups-container">...</div>
    <div class="legend">...</div>
    <button class="emergency">...</button>
    <div class="zones-section">...</div>
  </main>
</div>
```

### 5. CSS изменения

**Новые стили (desktop ≥1024px):**
```css
.desktop-layout {
  display: flex;
  min-height: 100vh;
}
.weather-sidebar {
  width: 300px;
  position: sticky;
  top: 0;
  height: 100vh;
  overflow-y: auto;
  border-right: 1px solid var(--border-color);
  padding: 16px;
  background: var(--card-background);
}
.main-content {
  flex: 1;
  padding: 16px;
  overflow-y: auto;
}
```

**Sidebar collapsed:**
```css
.desktop-layout.sidebar-collapsed .weather-sidebar {
  width: 0; padding: 0; overflow: hidden; border: none;
}
.desktop-layout.sidebar-collapsed .sidebar-toggle { left: 0; }
```

**Мобилка (<1024px):**
```css
@media (max-width: 1023px) {
  .desktop-layout { flex-direction: column; }
  .weather-sidebar { width: 100%; height: auto; position: static; border-right: none; border-bottom: 1px solid var(--border-color); }
  .sidebar-toggle { display: none; }
}
```

**Прогноз 24ч grid:**
```css
.weather-24h-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
}
```

**Активная зона:**
```css
.sidebar-active-zone {
  background: linear-gradient(135deg, #2196f3, #1976d2);
  color: white; border-radius: 8px; padding: 12px; margin-top: 12px;
}
.active-zone-progress { height: 4px; background: rgba(255,255,255,0.3); border-radius: 2px; }
.progress-bar { height: 100%; background: white; }
```

**Расход воды:**
```css
.sidebar-water-meter {
  background: var(--card-background); border: 1px solid var(--border-color);
  border-radius: 8px; padding: 12px; margin-top: 12px;
}
```

### 6. Обратная совместимость
- Все существующие ID элементов сохраняются (`w-icon`, `w-temp`, `w-coeff`, `w-hours`, `w-days`, `w-details`, `w-factors`, `w-history`)
- Все существующие функции JS остаются рабочими
- Мобильный layout не меняется
- Weather API (/api/weather) не меняется
- Тёмная тема работает через существующие CSS variables

### 7. Тестирование

**Unit тесты (новые):**
- `test_desktop_sidebar_layout` — проверка что HTML содержит `.desktop-layout`, `.weather-sidebar`, `.main-content`
- `test_sidebar_24h_forecast_6_cards` — JS функция фильтрует до 6 интервалов
- `test_sidebar_active_zone_indicator` — показывается при active зоне, скрывается без

**Existing тесты:**
- Все существующие тесты status.html должны пройти (обратная совместимость)
- API тесты weather не затрагиваются

### 8. Файлы для изменения
1. `templates/status.html` — HTML structure + CSS
2. `static/js/status.js` — JS rendering + new functions
3. `tests/` — новые тесты

### 9. Не трогаем
- `routes/weather_api.py` — API не меняется
- `services/weather*.py` — сервис не меняется  
- `templates/base.html` — base layout не меняется
- Мобильная версия — без изменений
