# Phase 2 — Frontend Audit `wb-irrigation` (ветка `refactor/v2`)

> Read-only анализ. file:line факты. Scope: Jinja templates, JS, CSS, UX, mobile.
> Out-of-scope: XSS (security-engineer), a11y-аудит (отдельный агент), perf core (отдельный).
> Локальный путь: `/opt/claude-agents/irrigation-v2/`. HEAD: `2fe9521`.

---

## 0. Executive Summary

**Оценка фронтенда: 4.5/10.** Кодовая база — типичный «органически выросший» Flask+jQuery-style фронт без bundler/модулей/линтера. Mobile-first декларируется, но реализуется частично. Главные проблемы:

1. **Полностью мёртвый SSE-клиент в `zones.html`** (`zones.js:1472`) подписан на endpoint, который явно отдаёт `204 No Content` (`routes/zones_watering_api.py:197-206`). Браузер впадает в бесконечный reconnect-цикл (EventSource поведение по умолчанию ~3 сек) и шлёт мусорные запросы. На мобильнике — тратит батарею и трафик.
2. **80 KB мёртвого JS в `static/js/`** — ни один из 9 модулей `static/js/zones-*.js` (53 KB) и `static/js/status/*.js` (27 KB) не подключён в шаблонах. Старая попытка модулярной разбивки `status.js`/`zones.js` была брошена — обе версии содержат одинаковые функции (`loadStatusData`, `loadZonesData`, `updateStatusDisplay`).
3. **Дубликат программы:** `static/js/programs.js` (514 LOC, 22 KB) НЕ подключён в `programs.html` — вместо этого ~545 LOC inline-JS прямо в шаблоне. Реализации поведения логики разные (старый wizard на 2 шага vs новый wizard на N шагов).
4. **Отсутствует bundler/minification**, ноль ESM/import-ов. `status.js` = **2187 LOC, 118 KB**, грузится синхронно на главной странице. На мобильнике 3G — это +0.5–1 сек к LCP.
5. **Mobile-first декларация без реализации:** hamburger-кнопка `36×36px` (ниже WCAG-минимума 44×44 для tap targets), полный набор decorative emoji в кнопках, нет `loading="lazy"` для фото зон, polling каждые 5 сек на главной странице висит даже когда вкладка неактивна (`document.hidden` не проверяется).
6. **CSRF не работает на API:** `app.js:17-49` шлёт `X-CSRFToken` во все non-GET запросы, но в `app.py:96-109` все API-blueprint'ы CSRF-exempt'нуты. Header игнорируется. Это ОК для read-only API, но `app.js:23` ещё и НЕ шлёт X-CSRFToken на GET (как и положено), однако SSE и settings формы зачем-то подтягивают meta — мёртвый код в `app.js`.
7. **Polling+SSE дублирование:** на странице `/` (status) идёт polling `/api/status` + `/api/zones` каждые 5 сек **И** есть отключённый SSE-клиент (комментарий `status.js:1079`). На странице `/mqtt` — реальный SSE на `/api/mqtt/<id>/scan-sse` без backoff'а и с error→stopSSE() (по сути disconnect-and-give-up).

**Топ-3 проблем — см. финальный summary.**

---

## 1. Inventory: Templates (Jinja2)

| Файл | LOC | Размер | Назначение | Замечание |
|---|---:|---:|---|---|
| `base.html` | 72 | 3.3 KB | Layout (header+nav+footer) — `extends` для всех остальных | viewport OK; **inline `<script>` для hamburger** (`base.html:34`) |
| `404.html` | — | 1.1 KB | NotFound | Изолированная (нет extends) — дубликат header |
| `login.html` | 126 | 6.3 KB | Форма входа | **НЕ extends base.html** — изолированная страница (отдельный CSS, JS) |
| `status.html` | 268 | 11.9 KB | Главный экран `/` | SSR-данные через `tojson` (`status.html:263-265`) |
| `zones.html` | 354 | 18.9 KB | Управление зонами | Огромная portatа inline-модалок + 9 onclick-`div`-ов |
| `programs.html` | **620** | **21.7 KB** | Программы | **545 LOC inline `<script>`** — параллельная реализация vs `static/js/programs.js` (514 LOC) |
| `mqtt.html` | 454 | 19.0 KB | Настройки MQTT серверов + topic browser | **~250 LOC inline JS** — единственное реальное использование SSE на frontend'е |
| `settings.html` | 391 | 21.6 KB | Пароль, telegram, system_name | ~230 LOC inline JS — лучшая UX (disable submit + spinner) |
| `logs.html` | 307 | 11.5 KB | Просмотр логов | inline JS |
| `map.html` | 160 | 6.1 KB | Карта зон | inline JS |

### Дублирование UI-логики

- `status.js:355-602` (`updateStatusDisplay`) **vs** `status.js:550-602` (`refreshSingleGroup`) — 70% копипаста (подтверждается `BUGS-REPORT.md:88`).
- `status.html:153/201` — две `.sheet-overlay` с одинаковой логикой close-on-click (могла быть макросом).
- Иконки зон захардкожены в шести местах: `zones.html:73-92`, `zones.html:235-253`, `status.html:163-170`, `zones.js` (несколько).
- Дублирование `_is_status_action()` (`app.py:203,256`) — упомянуто в BUGS-REPORT, к фронту косвенно.

### `templates/programs_old.html` (369 LOC из BUGS-REPORT)

**ОТСУТСТВУЕТ.** В refactor/v2 уже удалён (`ls /opt/claude-agents/irrigation-v2/templates/programs_old.html` → not found).

### Макросы / extends

- Все шаблоны кроме `404.html` и `login.html` делают `extends "base.html"` и переопределяют `extra_css`, `page_title`, `content`, `extra_js`.
- **`{% include %}` и `{% macro %}` — ноль использования.** Это значит: повторяющиеся UI-блоки (модалки, sheet-overlay, форм-группы, иконки зон) копипастятся.

---

## 2. Inventory: Client JS

| Файл | LOC | Размер | Подключён? | Замечание |
|---|---:|---:|---|---|
| `static/js/app.js` | 391 | 21 KB | да (через `base.html:68`) | Глобальный entry: CSRF interceptor, footer time, notification system, SW registration, health/stopwatch dev panels |
| `static/js/status.js` | **2187** | **118 KB** | да (`status.html:267`) | Главный SPA-controller статуса. **Самый большой файл фронта.** |
| `static/js/zones.js` | 1964 | 104 KB | да (`zones.html:349`) | Управление зонами (admin) |
| `static/js/programs.js` | 514 | 22 KB | **НЕТ** | **Мёртвый.** `programs.html` использует свой inline JS (см. §8) |
| `static/js/zones-core.js` | 64 | 2.3 KB | **НЕТ** | Мёртвый (старый split-attempt) |
| `static/js/zones-groups.js` | 303 | 18 KB | **НЕТ** | Мёртвый |
| `static/js/zones-sensors.js` | 371 | 20 KB | **НЕТ** | Мёртвый |
| `static/js/zones-table.js` | 285 | 13 KB | **НЕТ** | Мёртвый |
| `static/js/status/status-data.js` | 195 | 10 KB | **НЕТ** | Мёртвый split-attempt |
| `static/js/status/status-state.js` | 54 | 1.4 KB | **НЕТ** | Мёртвый |
| `static/js/status/status-groups.js` | 363 | 21.7 KB | **НЕТ** | Мёртвый (содержит дубликаты `status.js`) |
| `static/js/status/status-utils.js` | 94 | 4.9 KB | **НЕТ** | Мёртвый |
| `static/sw.js` | 82 | 2.9 KB | да (через `app.js:204-224`) | Network-first SW; HTML+API → сеть, статика → cache |

**Итого мёртвого JS: 9 файлов, 2143 LOC, ~94 KB** (см. §8).

### Модульная система / bundler

- **Нет.** Ни ESM, ни CommonJS, ни bundler'а. Всё через `<script src="...">` в глобальный namespace.
- Cache-busting на минимуме: `app.py:134` определяет `asset = lambda path: f"{path}?v={APP_VERSION}"`. Использовано **только** в `base.html:68`, `status.html:267`, `zones.html:349`. Остальные шаблоны (`programs.html`, `mqtt.html`, `settings.html`, `logs.html`) подключают inline JS, где cache-busting не нужен, но также подключают `*.css` без `asset()` — `<link href="/static/css/programs.css">` (`programs.html:7`). При обновлении CSS пользователь увидит старый стиль до hard-reload.
- **Нет minification.** `status.js` 118 KB сырого ES5/ES6 mix.

### Inline `<script>` в шаблонах

- `base.html:34` — onclick для hamburger (`this.classList.toggle('active');document.querySelector('header nav').classList.toggle('nav-open')`). Минор: лучше через `app.js`.
- `status.html:75` — onclick для mobile-weather-toggle.
- `status.html:175,177` — inline арифметика для +/- duration.
- `status.html:262-266` — SSR data injection через `JSON.parse({{ ...|tojson }})`.
- `programs.html:73-619` — **545 LOC inline JS** (полная реализация модуля).
- `mqtt.html:200-454` — ~250 LOC inline JS (единственный реальный SSE-клиент).
- `settings.html:171-389` — ~220 LOC inline JS.
- `logs.html` — inline JS (~150 LOC).
- `map.html` — inline JS.

### Onclick-handlers в шаблонах

102 inline `onclick=` атрибута в шаблонах. Для CSP это блокер при `unsafe-inline` ban; пока нет CSP — работает.

---

## 3. SSE-клиент

В системе **3 разных SSE-сценария**, и все три — с проблемами:

### 3.1. `zones.js:1472` — `/api/mqtt/zones-sse` — **полностью мёртв, но активен** (CRIT)

**Frontend (`static/js/zones.js:1468-1488`):**
```js
document.addEventListener('DOMContentLoaded', () => {
    loadData();
    try {
        const es = new EventSource('/api/mqtt/zones-sse');
        es.onmessage = (ev)=>{ ... обновление кнопок ▶️/⏹️ ... };
    } catch (e) {}
});
```

**Backend (`routes/zones_watering_api.py:197-206`):**
```python
@zones_watering_api_bp.route('/api/mqtt/zones-sse')
def api_mqtt_zones_sse():
    """SSE endpoint — DISABLED to prevent event loop death on ARM/Hypercorn.
    Frontend uses 5s polling instead. Returns 204 No Content."""
    try:
        _sse_hub.ensure_hub_started()
    except (OSError, RuntimeError) as e:
        logger.debug("SSE hub start (background): %s", e)
    return ('', 204)
```

**Что происходит:**
- Браузер открывает SSE-connection → сервер отдаёт `204 No Content` (без `Content-Type: text/event-stream`) → EventSource считает это ошибкой → `readyState=2 (CLOSED)`. По спецификации EventSource **автоматически переподключается** каждые ~3 сек (или по `retry:` field; здесь нет).
- Реально: в DevTools видно бесконечный поток запросов `/api/mqtt/zones-sse` каждые 3 сек → шум в логах (rate-limiter в `services/api_rate_limiter.py` или nginx может начать блокировать).
- На мобильнике это **батарея + трафик**. Один пользователь = 28800 запросов/сутки (реально меньше, но порядок такой).
- `onerror` handler не определён → браузер не понимает, что прекратить попытки.

**Severity: CRIT.** File: `static/js/zones.js:1472`. Соседний bug: блок `try{}catch(e){}` поглощает ошибку вместо корректной отписки.

### 3.2. `mqtt.html:355` — `/api/mqtt/<id>/scan-sse` — рабочий, но без backoff (HIGH)

```js
sseSource = new EventSource(url);
sseSource.onmessage = (ev)=>{ ... };
sseSource.addEventListener('ping', ()=>{});
sseSource.onerror = ()=>{
    appendLog('% SSE error — поток будет остановлен');
    stopSSE();
    scanning = false;
    document.getElementById('scanBtn').textContent = 'Сканировать топики';
};
```

**Анализ:**
- ✅ JSON parsing обёрнут в try/catch (`mqtt.html:357-383`).
- ✅ Memory: `if (entries.length > 20000)` cap на размер `lastTopicMap` (`mqtt.html:367-373`).
- ✅ Throttling: `setTimeout(...300)` для batched-rerender (`mqtt.html:364-381`).
- ❌ **Нет reconnect**: `onerror` → `stopSSE()` (закрывает и сбрасывает UI). Если сеть мигнула — пользователь должен вручную нажать «Сканировать» снова. Backoff отсутствует.
- ❌ Нет `beforeunload`-listener'а: при навигации на другую страницу EventSource НЕ закрывается явно. Браузер закроет при unload, но у Hypercorn могут оставаться зависшие генераторы (см. backend backwall в `services/sse_hub.py:23` — `MAX_SSE_CLIENTS=5`, и забытые connection'ы могут блокировать новые).
- ❌ Нет UI-индикатора connection state кроме текста кнопки.

**Severity: HIGH.** File: `templates/mqtt.html:354-391`.

### 3.3. `status.html` — SSE отключён, замена polling'ом (MED)

```js
// status.js:1071-1080
setInterval(() => {
    Promise.all([loadStatusData(), loadZonesData()]).catch(function(){});
}, 5000);
setInterval(tickCountdowns, 1000);
// SSE disabled — polling every 5s provides updates; SSE caused event loop death on ARM
// MQTT→DB sync still works via sse_hub backend (no browser SSE connections)
```

- Polling каждые 5 сек **постоянно**, даже при `document.hidden===true` (вкладка свёрнута). На телефоне — лишний расход батареи.
- Параллельно: `setInterval(updateDateTime, 1000)`, `setInterval(syncServerTime, 5*60*1000)`, `setInterval(refreshWeatherWidget, 5*60*1000)`, `setInterval(tickCountdowns, 1000)`. Это **5 параллельных таймеров** — не критично, но лучше bus один.
- При `loadStatusData` ошибке — `showConnectionError()` показывает баннер `#connection-status` (`status.html:12`). Это **единственный UI-индикатор** disconnect (но только для HTTP-ошибок, не для timeout/offline).

### Memory leaks (общее)

- `app.js:189-201` — `window.addEventListener('error', ...)` без снятия. OK для глобального, но при реальном SPA нужно.
- `app.js:323-352` — два module-level `document.addEventListener('keydown', ...)` (для health и stopwatch panels). Не снимаются. Это global, OK.
- `status.js`: `setInterval`'ы — не снимаются никогда. На SPA это leak, но т.к. навигация — full page reload, реальной утечки нет.
- `mqtt.html:355` — `EventSource` не закрывается при unload (см. §3.2).

---

## 4. Состояние форм

### 4.1. CSRF token

**`app.js:17-49` — global fetch/XHR interceptor:**
- Перехватывает все non-GET fetch и XHR, добавляет `X-CSRFToken: <meta>`.
- **На сервере (`app.py:96-109`)** все API-blueprint'ы помечены CSRF-exempt. Header игнорируется.
- Это **ОК** для guest-пользователей (см. комментарий в `app.py:96-99`), но пересекается с замечанием security-engineer.

### 4.2. Validation

| Форма | Файл:Линия | Client-side validation | Disable submit | Spinner | Error feedback |
|---|---|---|---|---|---|
| Login | `login.html:63-90` | `password.length < 4` | ✅ `setLoading(true)` | ✅ «Вхожу…» | ✅ `errorEl.textContent` |
| Password change | `settings.html:179-206` | `new_password.length < 4` | ✅ `btn.disabled = true` | ✅ ⏳ icon | ✅ `msg.textContent` |
| System name | `settings.html:208-228` | нет | ❌ | ❌ | ✅ msg |
| Telegram settings | `settings.html:248-280` | нет | ❌ | ❌ | ✅ msg |
| Zone create | `zones.js:1381-...` (+ `zones.html:227`) | `required` HTML5 | ❌ | ❌ | через `showNotification` |
| Group create | `zones.js:1432-...` | `required` HTML5 | ❌ | ❌ | через `showNotification` |
| Program save (`programs.html:570-603` inline) | inline | `name && time && days && zones && validations` | ❌ | ❌ | toast |
| Program save (`programs.js:439-502`, мёртвый) | мёртвый | `name && time && days && zones` | ❌ | ❌ | notification |
| Zone edit (sheet) `status.js:saveZoneEdit` | inline | минимум | ❌ | ❌ | toast |

**Главное:** только 2 из 9 форм имеют **double-submit protection** (login + password). Остальные — клик-кликнул → два запроса. Особенно опасно для:
- Создание программы (можно создать дубль)
- Запуск зоны (`emergencyStop` — `status.js:953`: `if (!confirm(...))` затем fetch без disable; реальный risk: оператор клик-клик → два POST `/api/emergency-stop` → не critical, но лишний MQTT)
- Запуск группы (`runGroupWithDefaults` — `status.js:1864`)

### 4.3. Обработка ошибок сервера

**`app.js:79-111` (api utility):** для 4xx/5xx **НЕ бросает** exception — возвращает body. Комментарий: *"Не бросаем исключение для 4xx/5xx, чтобы экран мог обработать soft-ошибки (например, конфликты программ)"*. Это разумно.

**Но:** многие места пишут `await api.post(...)` без проверки `res.success === false`:
- `programs.js:483-491` — проверка `res.has_conflicts` есть.
- `status.js:1797` — `if (data && data.success)` есть.
- `status.js:1830` — `api.put(...).catch(function() {})` — **silent fail**.
- `zones.js` (множество мест) — `try{...}catch(e){}` (`zones.js:1464`, `:1487`, `:1576` ...). 8+ silent-fail-блоков.

### 4.4. Network / Offline

- `navigator.onLine` — **не проверяется** нигде.
- `window.addEventListener('online'/'offline')` — **не используется**.
- При offline: fetch падает → `catch` → `showNotification('Ошибка сети', 'error')` (если catch есть). На странице status — `showConnectionError()` показывает баннер. Других offline-UI нет.

### 4.5. AbortController / отмена запросов

- **AbortController не используется ни разу.** Ctrl+R / переключение между группами / повторный submit → прошлые запросы продолжают резолвиться, иногда перезаписывая UI устаревшими данными (race condition).

---

## 5. Mobile UX

### 5.1. Viewport meta

- `base.html:7` — `<meta name="viewport" content="width=device-width, initial-scale=1.0">` ✅
- `login.html:5` и `404.html:5` — то же. ✅
- `user-scalable=no` НЕ установлен — пользователь может зумить. ✅ (важно для оператора с +1 в зрении).

### 5.2. Tap targets (минимум 44×44 px по Apple HIG / 48×48 по Material)

| Элемент | Размер | Файл:Линия |
|---|---|---|
| Hamburger | **36×36** ❌ | `base.css:96-110` |
| Nav-link | padding 6×12 → высота ~30px ❌ | `base.css:132-141` |
| Nav-link active mobile (open) | при `nav-open` — отдельные правила (см. `base.css:281+`) — нужна верификация |
| Hamburger span (3 полоски) | 22×2px (визуал) | OK |
| Sheet button (`.sheet-btn-save`) | padding 14px (`status.html:175-177`) → ~46px ✅ |
| Run-popup buttons (preset 5/10/15/20/30/60м) | inline padding в CSS — нужна верификация | — |
| `.zc-dur-btn` (+/− duration) | inline в CSS | — |
| `.notification-close` (×) | минимальный (~24px) ❌ | `app.js:154` |
| Кнопка `?` или mode hamburger | различно | — |

### 5.3. Sidebar / nav на узком экране

- `status.html:16-94` — `<aside class="weather-sidebar">` с `<button class="sidebar-toggle" id="sidebar-toggle">◀</button>` (`status.html:93`).
- `desktop-layout` + media-queries в `status.css:268+`, `:566+`, `:1214+`, `:1454+`. Скрытие/показ боковой панели делается через классы `mobile-expanded` (`status.html:75`).
- **Проблема UX:** mobile-weather-toggle меняет inner text inline (`onclick="this.textContent = ..."`) — это работает, но при динамическом перерендере виджета (refresh raз в 5 минут) — текст может слететь. Редкое, но баг возможен.

### 5.4. Скролл и фиксированные элементы

- `position: fixed` panels: notification-container (`base.css:195-201`), health/stopwatch dev-panels (`app.js:243+`, `:333+`), `.zone-toast`, `.loading-overlay`, `.bottom-sheet`, `.run-popup`, `.photo-modal`. Эти элементы могут перекрывать контент при scroll — **нет проверки safe-area-inset** (iPhone notch / home indicator).
- **iOS Safari fix** найден `zones.js:535`: *"Move modal to body to avoid transformed ancestors affecting fixed positioning (iOS Safari bug)"* — это HACK и работает, но повторяется только в одном месте. В status.js (фото-modal, run-popup, sheet) такой защиты нет → возможны баги позиционирования при `<main transform>` и т.п.

### 5.5. Touch events

- **Touch handlers — НЕ найдены** (`touchstart`, `touchmove`, `touchend`). Всё через `click`. На iOS это работает, но добавляет ~300ms delay (mitigated через viewport meta).
- Dial drag (`status.js:initDialDrag`, `:1861`) — нужна верификация, использует ли pointer events / touch events. Из контекста — это SVG-circle drag для выбора длительности; на iOS критично для UX.

### 5.6. iOS Safari quirks

- `git log --grep "iOS"` — `zones.js:535` — единственная фикса (transformed ancestors).
- **Date parsing на iOS:** Safari НЕ парсит `'YYYY-MM-DD HH:MM:SS'` (без `T`) — только ISO. Грепом видно:
  - `app.js:60` — `_serverNow = new Date(j.now_iso.replace(' ','T'))` ✅ — fix есть.
  - `status.js:1792-1793, 2059-2060, 2088-2089` — `new Date().toISOString().slice(0,19).replace('T',' ')` — здесь ОБРАТНОЕ преобразование (для DB-сравнения), безопасно.
  - `status.js:212, 704, 1108`, `status.js:1663` — все `.replace('T',' ')` для **отображения**, не парсинга → OK.
  - `status/status-data.js:134`, `status/status-groups.js:269,321` — мёртвый код.

- **Subtle bug:** `app.js:60` создаёт `new Date('2026-04-19T10:30:00')` — без timezone suffix → парсится как **локальное** время. Если сервер в UTC, а клиент в +3 — разъезд на 3 часа. На контроллере таймзона WB (`Asia/Novosibirsk`?) — нужно проверить, что и сервер, и клиент в одном TZ. Если расходятся — таймер обратного отсчёта будет врать.

### 5.7. PWA: manifest.json + service worker

- **`manifest.json` — ОТСУТСТВУЕТ.** `find` не находит. В `base.html:7-12` нет `<link rel="manifest">`. Это значит: «Add to Home Screen» работает, но без иконки и splash screen.
- **Service Worker** есть (`static/sw.js`, 82 LOC). Регистрируется в `app.js:204-224` через HEAD-проверку.
- SW behavior:
  - `text/event-stream` → bypass (правильно).
  - `/api/*` → network-first (правильно — иначе старые данные).
  - HTML navigation → network-first с cache fallback (правильно).
  - Static (CSS/JS) → cache-first → network. **Это значит: при обновлении CSS пользователь увидит старый стиль до полного reload+SW update** (cache-buster `?v=APP_VERSION` помогает только при изменении версии; если версия не меняется при изменении CSS — cache навсегда).
- **Без `<link rel="manifest">` SW не делает install prompt.** PWA deficient.
- Favicon — inline SVG `data:` URI (`base.html:12`) — emoji 💧. ОК для desktop, на iOS не отображается на home screen.

---

## 6. Performance (frontend layer; deep perf — у perf-агента)

### 6.1. Размер критичных JS/CSS на странице status (`/`)

| Asset | Сырой | gzip (est.) |
|---|---:|---:|
| `static/js/app.js` | 21 KB | ~7 KB |
| `static/js/status.js` | **118 KB** | ~30 KB |
| `static/css/base.css` | 10 KB | ~3 KB |
| `static/css/status.css` | **47 KB** | ~9 KB |
| **Total критичного** | **196 KB** | **~49 KB** |

На 3G (~50 KB/s) это ~4 сек только для JS+CSS. Минимум: bundle + minify → 30–40 KB gzip.

### 6.2. Lazy-load images (фото зон через `routes/zones_photo_api.py`)

- `status.js:730` — `<img src="/api/zones/${zone.id}/photo" ...>` — **без `loading="lazy"`** ❌.
- `zones.js:109` — то же ❌.
- На странице с 20+ зонами — все фото грузятся сразу. Каждое фото 50–200 KB (хранится в БД, отдаётся через Pillow).
- Также НЕТ `width`/`height` атрибутов → CLS shift.
- Нет `srcset` для retina/mobile.

### 6.3. Polling vs SSE дублирование

- См. §3. Главная страница: 5-сек polling + мёртвый SSE подёргивает на zones.html.
- Не суммируется — это разные страницы, но если оператор открыл /zones и /status в двух вкладках — sustained 4 fetch/sec на одного клиента.

### 6.4. Re-render frequency

- `status.js:1071-1073` — каждые 5 сек **полный** `loadStatusData()` + `loadZonesData()` → `updateStatusDisplay()` — перестраивает innerHTML всего `#groups-container` (`status.html:112`).
- Innerhtml-replace — не diff'ит DOM → каждый раз сбрасывается фокус, hover, in-progress анимации, стейт раскрытых deteils.
- Точечный `refreshSingleGroup` есть (`status.js:550`), но используется только в специфических местах.

### 6.5. SSR-инжекция данных

- `status.html:262-266` — SSR-инжекция `window._ssrZones`/`Groups`/`Status` через `tojson` → instant render без первого fetch. ✅ **Хороший паттерн.**
- В `zones.html` — отсутствует. Первый рендер идёт после `loadData()` → пустая таблица 200–500 мс.

---

## 7. Качество кода

### 7.1. `var` vs `let/const`

| Файл | `var` count |
|---|---:|
| `status.js` | **255** ❌ legacy |
| `status/status-data.js` | 8 (мёртвый) |
| `status/status-utils.js` | 6 (мёртвый) |
| `zones-core.js` | 8 (мёртвый) |
| `app.js` | 6 |
| `zones-sensors.js` | 3 (мёртвый) |
| `zones-table.js` | 1 (мёртвый) |
| `zones-groups.js` | 1 (мёртвый) |
| `programs.js` | 0 (мёртвый) |
| `zones.js` | **0** ✅ |

**Вывод:** `status.js` — legacy с `var` повсеместно. `zones.js` уже refactor'ен на `let/const`.

### 7.2. `==` vs `===`

`grep -cE "[^!=<>]==[^=]"`:
- `app.js`: 1 (внутри `csrfMethod.toUpperCase() !== 'GET'` — false positive для regex)
- `programs.js` (мёртвый): 1
- `zones.js`: 3 ❌
- `zones-table.js` (мёртвый): 2

3 случая в живом коде (`zones.js`). Нужен ручной обзор; обычно `==` для null-checks (`x == null` — вполне OK), но грепом не отличить.

### 7.3. async/await vs callback-hell

Большинство — async/await. Но в `status.js:1797-1813` (toggleZoneRun), `status.js:1864-1881` (runGroupWithDefaults), `status.js:1869-1872` — `.then(...).catch(...)` цепочки. Mix-style, нет единого подхода.

### 7.4. Дублирование между скриптами

- `static/js/status.js` ↔ `static/js/status/*.js` — функции `loadStatusData`, `loadZonesData`, `updateStatusDisplay`, `refreshSingleGroup`, `tickCountdowns`, `escapeHtml` определены в обоих местах. Но `status/*.js` — мёртвый.
- `static/js/zones.js` ↔ `static/js/zones-*.js` — то же.
- `static/js/programs.js` ↔ `templates/programs.html` inline — то же.
- `escapeHtml` определён в `app.js:6-14` (global) — корректно. Проверки повторного определения в других файлах нет, но grep подтверждает: только в `app.js`.
- `getCsrfToken` — `programs.html:87-90` (inline). Дубликат `app.js:18-19` логики. Не критично.

### 7.5. Magic strings / numbers

- `MAX_SSE_CLIENTS = 5` — backend, ок.
- `setInterval(..., 5000)`, `setInterval(..., 60000)`, `setInterval(..., 5 * 60 * 1000)` — magic numbers.
- `setTimeout(..., 1500)` / `2000` для «дать backend'у время на MQTT» — неявные предположения о сети.
- Магические URL: `/api/status`, `/api/zones`, `/api/groups`, `/api/programs`, `/api/health-details`, `/api/server-time`, `/api/emergency-stop`, `/api/emergency-resume`, `/api/groups/${gid}/start-from-first` — встречаются 30+ раз без константы.
- HTTP cache-buster `?ts=${Date.now()}` повторяется 8+ раз.

### 7.6. Error handling

- 105 закомментированных строк в `zones.js`, 129 в `status.js` (включая doc-комментарии).
- `try{...}catch(e){}` (silent fail) — 30+ раз в `status.js`, 25+ раз в `zones.js`. Из них **критичные silent fails** в:
  - `zones.js:1464` (`}catch(e){}` после inline DOM manip)
  - `zones.js:1487` (после SSE message)
  - `app.js:316` (`document.getElementById('health-content').textContent = 'error'` — есть feedback, OK)
  - Много `.catch(function() {})` для fetch.

### 7.7. console.log в production

| Файл | console count |
|---|---:|
| `app.js` | 5 (`console.log` для SW registration, perf, error) |
| `status.js` | 1 (`[Perf] Status page:` — оставлен debug) |
| `zones.js` | 14 (preponderance `console.error`, но и debug-`console.log`) |
| `programs.js` | 5 (мёртвый) |
| `sw.js` | 3 (`console.log('Opened cache')`, `'Deleting old cache'`, `'SW file not found'`) |

В production console замусорен, но это в основном `console.error` (легитимно). `console.log` есть в:
- `app.js:212` (`'SW file not found, skipping registration'`)
- `app.js:217` (`'SW registered: '`)
- `app.js:230` (`Page load time: ...ms`)
- `app.js:384` (`'[Perf] Status page: ...'`)

---

## 8. Мёртвый код (Dead Code Inventory)

### Файлы целиком

| Файл | LOC | Размер | Подтверждение |
|---|---:|---:|---|
| `static/js/programs.js` | 514 | 22 KB | Не подключён ни в одном template (`grep "programs.js" templates/*.html` → только base.html подключает app.js, programs.html использует inline JS) |
| `static/js/zones-core.js` | 64 | 2.3 KB | Не подключён |
| `static/js/zones-groups.js` | 303 | 18 KB | Не подключён |
| `static/js/zones-sensors.js` | 371 | 20 KB | Не подключён |
| `static/js/zones-table.js` | 285 | 13 KB | Не подключён |
| `static/js/status/status-data.js` | 195 | 10 KB | Не подключён |
| `static/js/status/status-state.js` | 54 | 1.4 KB | Не подключён |
| `static/js/status/status-groups.js` | 363 | 22 KB | Не подключён |
| `static/js/status/status-utils.js` | 94 | 5 KB | Не подключён |
| **TOTAL DEAD JS** | **2243** | **~113 KB** | — |

Verification: `grep -rn "zones-core\|zones-groups\|zones-sensors\|zones-table\|status-data\|status-state\|status-groups\|status-utils\|programs.js" /opt/claude-agents/irrigation-v2/templates/ /opt/claude-agents/irrigation-v2/static/` — найден только template usage `zones-table` как CSS class и id `#zones-table-body`, что НЕ есть JS-import. Прямых script-тегов нет.

### `templates/programs_old.html` (369 LOC по BUGS-REPORT)

✅ Уже **удалён** в refactor/v2 (`ls .../programs_old.html` → No such file).

### Неиспользуемые JS-функции внутри живых файлов

Не строго проверял (требует AST), но видно невооружённым глазом:
- `app.js:226-236` — performance-monitoring блок: `if (loadTime > 3000) showNotification('Страница загружается медленно')` — функция работает только при первом load, после SW-cache hit `loadTime` будет около 0. Полезность сомнительна.
- `app.js:238-353` — два dev-panel (Health 'h', Stopwatch 's'). **Production-shipped debug panels.** Не уязвимость, но 2 KB кода + два keydown listener'а активны для всех пользователей. Можно убрать в `if (DEBUG)`.
- `app.js:175-187` — `notificationHistory` массив на 50 элементов в памяти — только для debug.

### Закомментированные блоки

- `status.js:116, 1116-1126, ...` — много `//` коммент-блоков с TODO/legacy-логикой.
- `status.js:1079-1080` — комментарий «SSE disabled» — лучше превратить в Removed-блок.

---

## 9. UX edge cases

### 9.1. Zone starting но MQTT не дошёл

- `status.js:1782-1814` (`toggleZoneRun`) — **optimistic UI**: `z.state = wantOn ? 'on' : 'off'` ВНЕ try, до fetch. Затем `renderZoneCards()` сразу.
- При ошибке (`!data.success`): откат `z.state = wantOn ? 'off' : 'on'` (`status.js:1804`).
- При network ошибке (`.catch`): откат + showZoneToast (`status.js:1810-1812`).
- **НО:** что если HTTP 200 success=true, MQTT-message ушёл, реле не получило? backend `routes/zones_watering_api.py:200+` возвращает success без подтверждения от observed_state. UI считает зону running, реально — выкл. Это **обнаружится только через 2 сек** (`setTimeout(..., 2000) loadStatusData()`, `status.js:1802`), и то — если backend в `services/observed_state.py` обновил state. Если не обновил — UI висит в «running».
- `services/observed_state.py` (549 LOC) — verifier есть, но лаг 2+ сек на первый poll → пользователь видит «running» когда его нет. **MED.**

### 9.2. Network offline — feedback пользователю

- См. §4.4: только показывается баннер `connection-status` через `showConnectionError` после неудачного fetch. Если оператор стоит на участке вне Wi-Fi и 4G отвалился — UI продолжит показывать «running», polling будет тихо падать.
- `navigator.onLine` не используется. **MED.**

### 9.3. Долгий запрос — спиннер, абортируется ли при отмене

- `showLoading('Запуск...')` (`status.js:1784`) → `loading-overlay` (`status.html:248-252`).
- **Нет timeout для fetch.** Если backend завис на 30 сек — overlay висит весь этот время.
- **Нет cancel-кнопки** на overlay.
- **Нет AbortController.**

### 9.4. Zone group start — прогресс по зонам

- `runGroupWithDefaults` (`status.js:1864-1881`) — отправляет POST `/api/groups/${gid}/start-from-first` и сразу показывает toast «Группа запущена». Прогресса по отдельным зонам нет — оператор не видит, какая зона сейчас идёт, пока не обновится polling через 5 сек.
- В sidebar есть `#sidebar-active-zone` (`status.html:79-85`) — отображает текущую зону, но обновляется тоже из polling.

### 9.5. Postpone (rain delay) индикатор

- `status.js:312-318` — отрисовка статуса:
  ```
  if (r === 'rain') return mob ? '⏸ Дождь' : 'Отложено - полив отложен из-за дождя';
  if (r === 'manual') return mob ? '⏸ Отложено' : 'Отложено - полив отложен пользователем';
  if (r === 'emergency') return mob ? '⏸ Авария' : 'Отложено - полив отложен из-за аварии';
  ```
- ✅ Понятно: отдельные тексты на mobile/desktop.
- Кнопка «Продолжить»/«Продолжить по расписанию» (`status.js:451`) — отображается только не при emergency. ✅
- **Минор:** `_mob` (`status.js:451`) — variable name неинформативное, и эта переменная определяется в двух местах (с `_mob`/`_mob2`) — копипаста между updateStatusDisplay/refreshSingleGroup.

---

## 10. Internationalization

- **Hardcoded русский** повсеместно: 214 строк с кириллицей в `status.js`, 279 в `zones.js`, 62 в `programs.js`.
- **Нет i18n-системы** (нет `gettext`, нет `i18next`, нет даже dictionary-объекта).
- Это **business decision** (один пользователь — русскоязычный оператор), но если когда-нибудь понадобится английская версия — переписывать каждый файл.
- Дни недели: `['Пн','Вт','Ср','Чт','Пт','Сб','Вс']` дублируется в 3+ местах (`programs.js:95`, `programs.html:163`, `status.js:` …).

### Даты / время

- Сервер шлёт `'YYYY-MM-DD HH:MM:SS'` без TZ (`/api/server-time`).
- `app.js:60` — `new Date(j.now_iso.replace(' ','T'))` — парсит как локальное.
- `status.js:1792` — `new Date().toISOString().slice(0,19).replace('T',' ')` — клиент создаёт UTC-строку, но без TZ-маркера. Если сервер в Asia/Novosibirsk, а клиент тоже там — совпадает; если клиент в Москве — DB получит время на 4 часа раньше.
- **Нет timezone-awareness** на клиенте.

---

## 11. Accessibility coordination (только pointers, не deep audit)

Помечаю по пути для a11y-агента:

- `<div onclick>` антипаттерн используется 7 раз (`templates/*.html`):
  - `programs.html:53` — modal overlay
  - `programs.html:168` — program-header (раскрытие detail)
  - `programs.html:174` — toggle-switch (вкл/выкл программы)
  - `status.html:153,201` — sheet/popup overlay
  - `zones.html:150,189,221,279,299` — modal overlays (5 шт)
  - Должно быть `<button>`.
- `aria-label`: hamburger ✅ (`base.html:34`), toggle-visibility ✅ (`login.html:115`). Остальные — выборочно.
- `role="dialog" aria-modal="true"` — есть в zones.html модалках. ✅
- `role="status" aria-live="polite"` — login error ✅ (`login.html:120`).
- Lang `<html lang="ru">` ✅
- Form labels: большинство `<label for="...">` есть.
- Иконки emoji в кнопках без визуального текста: `▶`, `🗑`, `✏` (`programs.js:86-87`, `programs.html:206-209`) — для скринридеров неинформативно.
- Контраст: декларируется через CSS-vars (`--text-color`), не проверял по WCAG.

---

## 12. Cross-references (для остальных агентов)

### Security (security-engineer)
- CSRF interceptor в `app.js:17-49` — добавляет header, но `app.py:96-109` exempt'ит API. Header игнорируется. Это **не security issue** само по себе, но lying-code: интерцептор делает вид, что защищает.
- `escapeHtml` в `app.js:6-14` корректный для innerHTML; используется широко (зоны, имена групп). Но есть и места без него: `status.js:1652-1660` (детали зон), `programs.html:198` (zone.name через template literal без escape — есть `escapeHtml(zone.name)` — ✅ есть).
- SSE endpoint `/api/mqtt/zones-sse` — DDOS-вектор: бесконечные reconnect от каждого клиента (`zones.js:1472`).
- `localStorage`/`sessionStorage` — не нашёл широкого использования (хорошо — нет токенов в LS).

### Performance (perf-агент)
- 196 KB критичного JS+CSS на главной (см. §6.1).
- Polling 5s + 1s + dial timer'ы — 5 параллельных интервалов (см. §3.3).
- 113 KB мёртвого JS даже не отдаётся (он не подключён), НО `static/` целиком mount'ится → SW при навигации может поймать и закешировать `404` для них, если случайно запросятся → проблема минорная.
- Lazy-load для фото отсутствует (см. §6.2).
- DOM-rerender через innerHTML каждые 5 сек — возможен CLS / forced reflow.

### Accessibility (a11y-агент)
- См. §11. Главные: tap targets <44px, `<div onclick>` антипаттерн, emoji-only buttons.

### Architect / cleanup (architecture-агент)
- 113 KB мёртвого JS, 545 LOC inline JS в programs.html vs 514 LOC мёртвого `programs.js` — нужна консолидация.
- Нет bundler'а — добавить vite/esbuild можно за полдня и получить -50% размера.

---

## 13. Findings (приоритизировано)

### CRITICAL

**[FE-CRIT-1] Мёртвый SSE endpoint, бесконечный reconnect от клиента**
- File: `static/js/zones.js:1472`, backend `routes/zones_watering_api.py:197-206`
- Effect: на странице `/zones` каждый клиент шлёт `/api/mqtt/zones-sse` каждые ~3 сек, получает 204, EventSource переподключается. Логи замусорены, батарея/трафик мобильника, потенциальный rate-limit DDoS-вектор.

### HIGH

**[FE-HIGH-1] Дубликаты модулей: 113 KB мёртвого JS в `static/js/`**
- Files: `static/js/programs.js` (514 LOC), `static/js/zones-core.js`, `zones-groups.js`, `zones-sensors.js`, `zones-table.js`, `static/js/status/status-data.js`, `status-state.js`, `status-groups.js`, `status-utils.js` (всего 9 файлов, 2243 LOC, 113 KB).
- Effect: путаница для разработчика; правки могут случайно делаться в мёртвой версии.

**[FE-HIGH-2] Inline JS 545 LOC в `programs.html` вместо `programs.js`**
- File: `templates/programs.html:73-619` vs `static/js/programs.js`
- Effect: невозможно линтить, нет cache-busting, дубль логики, разные wizard-flow.

**[FE-HIGH-3] SSE-клиент в mqtt.html без reconnect/backoff и beforeunload**
- File: `templates/mqtt.html:354-391`
- Effect: при дрожащем Wi-Fi на участке оператор должен вручную reconnect; при навигации висят backend-генераторы, может занять MAX_SSE_CLIENTS=5 слотов.

**[FE-HIGH-4] Polling /api/status + /api/zones каждые 5с не учитывает `document.hidden`**
- File: `static/js/status.js:1071-1073`
- Effect: 17 280 запросов/сутки/клиент, даже когда вкладка свёрнута. На мобильнике — батарея.

**[FE-HIGH-5] Нет double-submit protection на 7 из 9 форм**
- Files: `programs.html:570-603`, `status.js:953` (emergencyStop), `status.js:1864` (runGroupWithDefaults), `zones.js` (zoneForm/groupForm/addGroupForm:1381,1407,1433), settings system_name, telegram.
- Effect: оператор клик-клик → дубль программы / два emergency-stop / двойной POST.

**[FE-HIGH-6] `static/js/status.js` (2187 LOC, 118 KB) грузится синхронно на /**
- File: `templates/status.html:267`, `static/js/status.js`
- Effect: на 3G LCP +1–2 сек. Нет splitting'а на critical/non-critical.

### MEDIUM

**[FE-MED-1] Hamburger 36×36px ниже WCAG 44×44**
- File: `static/css/base.css:96-110`
- Effect: попасть пальцем сложно, особенно на маленьких телефонах. Оператор перчатка/мокрый палец.

**[FE-MED-2] Нет `loading="lazy"` для фото зон**
- Files: `static/js/status.js:730`, `static/js/zones.js:109`
- Effect: на странице с 20+ зонами все фото грузятся сразу (10–40 запросов /api/zones/N/photo одновременно). Без width/height → CLS.

**[FE-MED-3] Нет AbortController, нет fetch timeout**
- Files: `static/js/app.js:79-111` (api util), везде
- Effect: race conditions при быстрых переключениях между группами; зависший backend = вечный спиннер.

**[FE-MED-4] Optimistic UI без verification от observed_state**
- File: `status.js:1782-1814` (toggleZoneRun)
- Effect: UI показывает «running» 2+ сек после неудачи MQTT. Пользователь думает зона запущена.

**[FE-MED-5] Нет `manifest.json` → PWA не устанавливается**
- File: отсутствует
- Effect: «Add to Home Screen» работает без иконки/splash. Не критично, но обещанная PWA.

**[FE-MED-6] Cache-busting только для status.js/zones.js/app.js, остальное — без `?v=`**
- Files: `programs.html:7`, `mqtt.html`, `settings.html`, `logs.html`, `zones.html:7` подключают CSS без `asset()`
- Effect: после релиза CSS пользователь видит старый стиль до hard-reload.

**[FE-MED-7] Нет `navigator.onLine` / offline-feedback**
- Effect: оператор на участке вне 4G не понимает, что UI устарел.

**[FE-MED-8] `static/js/status.js` использует `var` 255 раз (legacy ES5)**
- Effect: scope-bugs, hoisting сюрпризы. zones.js уже refactor'ed.

**[FE-MED-9] Дублирование `updateStatusDisplay` + `refreshSingleGroup` (70% копипаста)**
- File: `static/js/status.js:355-602` ↔ `:550-602`
- Effect: правки логики приходится делать в двух местах.

### LOW

**[FE-LOW-1] iOS Safari date parsing fix только в одном месте (`app.js:60`)**
- Другие места используют `replace('T',' ')` для display, не для парсинга — пока ОК. Но при добавлении новых дат-парсингов есть риск повторить старый bug.

**[FE-LOW-2] `app.js` содержит production-shipped debug panels (Health 'h', Stopwatch 's')**
- File: `app.js:238-353` (115 LOC)
- Effect: 2 KB лишнего кода + 2 keydown listener'а активны для всех пользователей.

**[FE-LOW-3] Decorative emoji в кнопках без альтернативного текста для screen readers**
- Files: `programs.js:86-87`, `programs.html:206-209` (✏, 🗑, ▶)
- Effect: VoiceOver читает «emoji-name», непонятно. (Coordinate с a11y-агентом.)

**[FE-LOW-4] 5 параллельных `setInterval` на status page**
- File: `status.js:1065-1074`, `:1417`
- Effect: можно объединить в один master tick.

**[FE-LOW-5] Magic strings: URL'ы, timeouts разбросаны без констант**
- Effect: код-смелл, не баг.

**[FE-LOW-6] `navigator.serviceWorker` registration + `fetch('/sw.js', {method:'HEAD'})` дублирует логику**
- File: `app.js:204-224`
- Effect: на каждой странице лишний HEAD-запрос (нужен только при первом visit).

**[FE-LOW-7] `console.log` в production (4 шт в app.js, 1 в status.js, 3 в sw.js)**
- Effect: незначительно засоряет devtools.

**[FE-LOW-8] Нет `<link rel="manifest">` в base.html, нет theme-color, нет apple-touch-icon**
- Effect: Mobile look-and-feel недоделан.

**[FE-LOW-9] Inline `onclick` в шаблонах (102 шт) — блокер для CSP без `unsafe-inline`**
- Effect: при попытке внедрить CSP — придётся переписать всё.

---

## 14. Summary

### Что в целом ОК
- SSR для главной страницы (`status.html:262-266`) — instant first render.
- Service Worker network-first для HTML/API — данные не залипают.
- Optimistic UI для запуска зон с откатом (`status.js:1782-1814`).
- Login + password change форм имеют disable submit + spinner + error feedback.
- Postpone-индикатор различает rain/manual/emergency.
- Адекватный escapeHtml в `app.js:6-14`, используется в opasных местах.
- iOS Safari modal-bug fix задокументирован (`zones.js:535`).

### Топ-3 проблем
1. **Мёртвый SSE endpoint c бесконечным reconnect** (`zones.js:1472` ↔ `routes/zones_watering_api.py:197-206`) — каждый клиент `/zones` шлёт ~28k запросов/сутки в пустоту. **Fix: убрать `new EventSource('/api/mqtt/zones-sse')` из zones.js, либо вернуть рабочий SSE.**
2. **113 KB мёртвого JS + 545 LOC inline JS дубликат** — 9 файлов в `static/js/` не подключены, плюс `programs.html` содержит inline-копию `programs.js`. Разработчик не понимает, какой код «настоящий». **Fix: удалить мёртвые файлы, выбрать одну версию programs.js, перенести inline в файл.**
3. **Mobile UX недоделан:** hamburger 36px (<44px WCAG), нет lazy-load для 20+ фото зон, polling 5с не учитывает `document.hidden`, отсутствует manifest.json, нет offline-detection. **Оператор на солнце с мокрым пальцем — не целевая аудитория этого UI.** Fix: серия точечных правок (см. §13 MED).

### Путь
**Файл артефакта:** `/opt/claude-agents/irrigation-v2/irrigation-audit/findings/frontend.md`

---

*Frontend Developer Agent. Phase 2. Read-only analysis. file:line references throughout.*
