# Zones UI v2 — Hunter-style Mobile-First

## Цель
Заменить текущий UI зон на странице "Статус" (status.html + status.js) на Hunter-style карточки с аккордеоном, табами групп, компактной погодой и bottom-sheet редактированием. Страница "Зоны и группы" (zones.html, zones.js) — НЕ трогаем, это админка.

## Scope: только frontend (templates + static/js)
Backend API уже полный: CRUD, watering, photo, next-watering, SSE, weather. Изменения **только** в:
- `templates/status.html` — HTML + CSS
- `static/js/status.js` — JS рендер зон

### НЕ в scope
- Backend (routes/, db/, services/) — без изменений
- templates/zones.html, static/js/zones.js — без изменений (админка)
- templates/base.html — уже обновлён (hamburger, compact header)

---

## Текущее состояние (что есть)

### API endpoints (все существуют, работают):
- `GET /api/zones` — список всех зон
- `GET /api/zones/<id>` — одна зона
- `PUT /api/zones/<id>` — обновить (name, icon, duration, group_id, topic)
- `POST /api/zones/<id>/mqtt/start` — запустить полив
- `POST /api/zones/<id>/mqtt/stop` — остановить полив
- `GET /api/zones/<id>/watering-time` — remaining time
- `POST /api/zones/next-watering-bulk` — следующий полив для списка зон
- `GET /api/zones/<id>/photo` — фото зоны
- `GET /api/groups` — список групп
- `GET /api/status` — статус групп (watering/waiting/postponed/error)
- `GET /api/weather` — погода + коэффициент + прогноз
- `GET /api/mqtt/zones-sse` — SSE обновления состояний зон

### DB schema zones (ключевые поля):
- id, name, icon, duration, group_id, state (on/off)
- topic, mqtt_server_id
- photo_path
- watering_start_time, planned_end_time, scheduled_start_time
- last_watering_time, last_avg_flow_lpm, last_total_liters

### Текущий UI зон на status.html:
- Десктоп: HTML-таблица (zones-table)
- Мобилка: карточки (zones-cards) с аккордеоном — **сломано** (не показывались из-за breakpoint 767px → исправлено на 1024px)
- Рендерится в JS через `loadZonesData()`

---

## Целевой UI (из прототипа zones-hunter-v2.html)

### 1. Погодный виджет (компактный)
**Расположение:** между stats-bar и group-tabs
**Свёрнут (по умолчанию):** иконка + температура + влажность/ветер/осадки + коэффициент полива
**Развёрнут (тап):** почасовой прогноз (8 часов, горизонтальный скролл) + факторы погодокоррекции (пилюли) + источник данных

### 2. Табы групп
**Горизонтальный скролл:** "Все (24)" | "Насос-1 (8)" | "Насос-2 (6)" | ...
- Каждый таб: индикатор статуса (цветная точка) + имя + счётчик
- При выборе: фильтрация зон + кнопка "Запустить <группу>"
- Таб "Все": зоны разбиты секциями "НАСОС-1 ─── 8 зон"

### 3. Quick Actions
- "▶ Запустить группу" (или "все") — зелёная
- "⏹ Стоп всё" — красная (аварийная)

### 4. Карточки зон (аккордеон)
**Свёрнуто (по умолчанию):**
| Иконка типа | #номер Имя | Тип · Длительность | Время след. полива | ▼ |

**Статусы (цвет левого бордера):**
- Зелёный: enabled/idle
- Синий + пульсация: running (полив идёт)
- Серый: disabled
- Красный: error

**Развёрнуто (тап):**
- Детали: длительность, группа, след. полив, послед. полив (сетка 2×2)
- Контроль длительности: кнопки +/− (изменяет через PUT /api/zones/<id>)
- Кнопки: "▶ Запустить" / "⏹ Стоп" + "✏️" (edit)

**Running-состояние:**
- Синяя строка между main и expanded: dot + "Осталось" + таймер + процент
- Progress bar под строкой

### 5. Bottom Sheet (редактирование)
Выезжает снизу при нажатии ✏️:
- Поля: Название, Тип (select), Длительность (number), Группа (select)
- Кнопки: Отмена / Сохранить
- Сохранение: PUT /api/zones/<id>

### 6. Поиск
Иконка 🔍 в header → показывает/скрывает поле поиска, фильтрует по имени/номеру

### 7. Stats Bar
Всего зон | Активных | Групп | Расход (л) сегодня

---

## Что удаляется из текущего status.html

1. **Легенда статусов** — уже удалена
2. **zones-table** (десктоп таблица) — заменяется на карточки и на десктопе
3. **zones-cards** (текущие мобильные карточки) — заменяются на Hunter-style
4. **zones-section** wrapper — заменяется на новую структуру

## Что остаётся без изменений

1. **Карточки групп** (status-grid, groups-container) — верх страницы
2. **Погодный sidebar** (weather-sidebar) — на десктопе; на мобилке компактная версия
3. **Аварийная кнопка** (emergency-btn, resume-btn)
4. **SSE подписка** (zones-sse) — остаётся, адаптируем handlers
5. **Все backend API** — без изменений

---

## Технические решения

### Рендеринг
- Зоны рендерятся в JS (как сейчас) — `renderZoneCards()`
- Группы загружаются из `/api/groups`
- Следующий полив из `/api/zones/next-watering-bulk`
- Фильтрация по группе — клиентская (зоны уже загружены)

### SSE обновления
- При получении SSE zone update → обновить карточку зоны (статус, таймер)
- При получении SSE group update → обновить таб группы

### Responsive
- Единый UI для мобилки и десктопа (карточки)
- На десктопе: max-width 1400px, карточки в grid 2 колонки
- Погодный sidebar остаётся на десктопе, на мобилке — компактный strip

### Duration edit
- Кнопки +/− сразу вызывают `PUT /api/zones/<id>` с debounce 500ms
- Показывают новое значение мгновенно (optimistic update)

### Тесты (frontend-only)
- Selenium/Playwright не используем (нет на ARM)
- Тесты через Python: проверяем что API endpoints отвечают корректно
- Тесты шаблона: рендер status.html через Flask test client, проверка наличия ключевых элементов
- JS логика не тестируется unit-тестами (прототип)

---

## Файлы для изменения

| Файл | Действие | Описание |
|------|----------|----------|
| templates/status.html | MODIFY | Новая секция зон (hunter-style), погода strip, tabs, search, bottom sheet |
| static/js/status.js | MODIFY | renderZoneCards(), group tabs, search, duration edit, sheet, SSE adapters |
| tests/ui/test_zones_ui_v2.py | CREATE | Тесты рендера шаблона + API smoke |

---

## Этапы реализации

### Этап 1: HTML/CSS (status.html)
- Удалить старую секцию зон (zones-section, zones-table, zones-cards)
- Добавить: stats-bar-zones, group-tabs, search, zone-list container, bottom-sheet
- CSS для всех компонентов (из прототипа, адаптировано)

### Этап 2: JS рендер (status.js)
- `renderGroupTabs()` — табы с фильтрацией
- `renderZoneCards()` — карточки с аккордеоном
- `toggleZoneCard()`, `changeDuration()`, `openEditSheet()`, `saveZoneEdit()`
- `filterZonesBySearch()`, `selectGroup()`
- Адаптировать SSE handlers для новых карточек
- Quick actions: запуск группы, стоп всё

### Этап 3: Тесты
- test_zones_ui_v2.py: рендер шаблона, наличие элементов, API smoke

### Этап 4: Деплой + проверка
