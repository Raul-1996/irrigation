# Frontend / UX Audit — wb-irrigation (2026-05-09)

Single-tenant commercial-grade полит-PWA для садовников. Деплой на даче, мобильные и планшеты, сеть нестабильная. Оценка готовности: **не блокирует продажу, но 4-5 P0 надо закрыть до коммерческого релиза**.

---

## Находки

### [P0] Math.random() в production CSS-classifier для группы

**File:** `static/js/status.js:425, 578` (+ дубликаты в `static/js/status/status-groups.js:184, 208`)

```js
const flowActive = group.status === 'watering' && Math.random() > 0.3;
card.className = `card ${group.status} ${flowActive ? 'flow-active' : ''}`;
```

**Описание:** Класс `flow-active` (видимая CSS-анимация течения воды) включается случайно в 70% случаев при каждом рендере. В одном пятисекундном тике карточка "льёт", в следующем — нет, потом снова. Это поведение работает каждые 5 сек и при каждом действии пользователя.

**Impact:** Визуальный мусор и дезориентация пользователя (если у CSS `.flow-active` есть видимый эффект). Прямой репутационный риск для продажи: садовник видит "глючит, льёт-не льёт" и решает что система ненадёжна. Также — лишний reflow.

**Recommendation:** Убрать `Math.random()`. `flow-active` должен следовать из реальных данных (например `group.flow_value > 0` или `group.status === 'watering' && !group.master_valve_state==='closed'`). Если эффект вообще не используется — убрать.

---

### [P0] Утечка памяти и performance death по wrapped `window.fetch`

**File:** `static/js/app.js:21,365`, `static/js/status.js:11,1175`, `static/js/status/status-state.js:11`

**Описание:** Глобальный `window.fetch` оборачивается **минимум 4 раза** на странице Status:
1. `app.js:21` — CSRF interceptor
2. `app.js:365` — Real-User-Timing performance monitor
3. `status.js:11` — UI timing для control endpoints
4. `status.js:1175` (внутри DOMContentLoaded) — Stopwatch instrumentation

Каждый wrapper держит замыкание на предыдущий `window.fetch`. Каждый запрос проходит через все 4 функции (с регексп-проверками URL, console.log'ами и т.д.).

**Impact:**
- Каждый fetch — 4 лишних async hop'а + несколько regex.test() + добавление меток в массивы.
- На WB ARM-боксе с 2 ядрами и 5-секундным polling это уже ощутимо.
- Если PWA остаётся открытой долго, history-массивы (`marks`, `notificationHistory`) только растут.
- Перерегистрация stopwatch fetch на каждом DOMContentLoaded после restart (если вдруг soft-reload) добавит ещё один слой.

**Recommendation:** Объединить всю инструментацию в **один** wrapper в `app.js`. Удалить отладочный stopwatch из production (или гейтить через `localStorage.debugTiming === 'true'`). RUM-метрики в `app.js:365` оставить.

---

### [P0] SW не уведомляет пользователя о новой версии — обновления "застревают"

**File:** `static/sw.js:24` + `static/js/app.js:204-224`

**Описание:** SW делает `self.skipWaiting()` сразу при install и `clients.claim()` при activate. Это значит:
- Новая версия SW активируется немедленно.
- Но **открытые страницы продолжают использовать старую** до полного reload (skipWaiting только активирует SW, не reload'ит страницы).
- Регистрация в `app.js` не слушает `controllerchange`, не показывает баннер "доступно обновление", не делает `location.reload()`.

Если пользователь держит приложение открытым на даче (типичный сценарий PWA на телефоне), он будет видеть **старый JS неделями**, пока сам не закроет таб. Бaги, фиксы (например тот самый таймер) до него не дойдут.

**Impact:** Производственный фикс не доезжает до пользователя. Для commercial single-tenant — критично (нет канала "позвоню скажу нажми F5").

**Recommendation:**
```js
navigator.serviceWorker.addEventListener('controllerchange', () => {
  if (this.refreshing) return;
  this.refreshing = true;
  showNotification('Обновление установлено, перезагружаю...', 'info');
  setTimeout(() => location.reload(), 1500);
});
// или на registration: reg.addEventListener('updatefound', ...) с UI prompt
```

И/или раз в 60 минут: `registration.update()` для проверки.

---

### [P0] Неподтверждённый запуск полива — только Stop требует confirm

**File:** `static/js/status.js:850, 971, 1099`, `zones.js:1047,1645,1797`

**Описание:** Сейчас `confirm()` показывается только при:
- Остановке группы
- Аварийной остановке
- Удалении фото / зоны / группы

**Запуск** зоны/группы — **молча стартует** при тапе `▶`. На мобиле случайный тап по карточке (особенно при кривом скролле) → вода льётся. Это для дачи (где может ничего не быть рядом, кран не закрыт, дренаж не подключён) — реальная проблема.

**Impact:** Случайный запуск может привести к подтоплению/перерасходу воды. Для commercial: представь жалобу клиента "полилось пока я в магазине".

**Recommendation:**
- Для группового запуска (и тем более "Запустить все") — обязательный confirm с явным текстом "Запустить полив группы X на Y минут?".
- Одиночная зона уже идёт через `showRunPopup` (диалог с диалом) — там OK.
- Но `startOrStopZone` (status.js:869, использован в индикаторах в строке таблицы) запускает зону без подтверждения — это дыра.

---

### [P0] Глобальный `error` handler показывает stack-trace пользователю

**File:** `static/js/app.js:190-201`

```js
window.addEventListener('error', (event) => {
    var msg = '';
    if (event.error && event.error.message) msg = event.error.message;
    ...
    showNotification('JS: ' + msg, 'error');
});
```

**Описание:** Любая необработанная ошибка JS (включая отвалившийся fetch на плохой сети, race condition, undefined property) показывает пользователю красный popup "JS: Cannot read property 'foo' of undefined". Это:
1. **Утечка деталей реализации** — потенциальный security issue (имена переменных, путей).
2. **Шум для пользователя** — садовник не знает что делать.
3. **Брендовый удар** — выглядит как "программа сломалась".

**Impact:** UX/brand-degradation. Для commercial-grade — недопустимо.

**Recommendation:** Логировать в console + слать на бэкенд (`/api/client-error`), пользователю показывать дружелюбное "Произошла ошибка, мы уже знаем. Попробуйте обновить страницу." Только в `localStorage.devMode === '1'` показывать сырое сообщение.

---

### [P1] Полный `innerHTML = ''` re-render groups-container каждые 5 секунд

**File:** `static/js/status.js:419-505` (`updateStatusDisplay`)

**Описание:** При каждом polling tick (5 сек):
```js
const container = document.getElementById('groups-container');
container.innerHTML = '';
for (const group of statusData.groups) { ... container.appendChild(card); }
```

Карточки полностью пересобираются. Это:
1. Сбрасывает любой in-progress UI state (focus на кнопке, hover-state).
2. Терёт обработчики (хорошо что они inline `onclick=`, но это ещё одна проблема).
3. Делает анимации фликерящими.
4. Мешает AT (screen reader теряет позицию).
5. Отрисовка ~5 карточек × 5 sec × N часов = постоянный layout thrashing.

**Impact:** Заметные подвисания на WB-ARM, дёрганый UI на телефоне.

**Recommendation:** Diff-based update:
- Если карточка для group.id уже есть — обновить только текстовые поля и состояние кнопок.
- Создавать DOM только для новых групп.
- Удалять только для исчезнувших.

Аналогичное в `renderZoneCards` (status.js:1718) — там есть preserve-open-state hack для accordion (строки 1722-1727), это симптом проблемы.

---

### [P1] Дублирование `updateStatusDisplay` ↔ `refreshSingleGroup`

**File:** `static/js/status.js:359-505 (updateStatusDisplay)` vs `566-662 (refreshSingleGroup)`

**Описание:** Логика рендера карточки группы дублируется ~150 строк один-в-один между двумя функциями. Все ветки (`status === 'watering'`, `postponed`, `error`, mvBlock, gridCells) скопированы дважды с переменными вида `extraText` / `extraText2`, `_m` / `_m3`, `mvState` / `mvState2`.

**Impact:** Любой фикс надо делать в двух местах. Я уже вижу что в одном месте есть, в другом нет (см. `mvBlock` vs `cells`/`pad2` — в `refreshSingleGroup` использован `card.innerHTML +=` что повторно парсит весь DOM). Будет drift, будут баги расхождения.

**Recommendation:** Вынести `renderGroupCard(group)` → возвращает HTML или DOM. Использовать в обоих местах.

---

### [P1] Setting `onclick` через `setAttribute` — race condition

**File:** `static/js/status.js:265, 901, 264`

```js
btn.setAttribute('onclick', action);
// где action = "startOrStopZone(1, 'on')"
```

**Описание:** Это работает, но:
1. Парсится как inline-скрипт каждый раз (нарушает CSP `script-src 'self'`).
2. **Если в имени зоны есть кавычки** — в `'${(isOn ? 'on' : 'off')}'` ничего не передаётся пользователем, так что XSS нет, но это хрупко.
3. На каждом polling-тике строки таблицы перепарсятся.

**Impact:** CSP-блокеры (если введёте hard CSP — приложение сломается). Performance.

**Recommendation:** `addEventListener('click', handler)` или event delegation на `tbody` через `data-zone-id`/`data-action`.

---

### [P1] Heavy `setInterval(loadStatusData + loadZonesData, 5000)` без backoff

**File:** `static/js/status.js:1217-1219`

```js
setInterval(() => {
    Promise.all([loadStatusData(), loadZonesData()]).catch(...);
}, 5000);
```

+ ещё:
- `setInterval(updateDateTime, 1000)` (1218)
- `setInterval(tickCountdowns, 1000)` (1220)
- `setInterval(syncServerTime, 5*60*1000)` (1211)
- `setInterval(refreshWeatherWidget, 5*60*1000)` (1568)

**Описание:** На плохой сети (типичная ситуация на даче — 3G/Edge):
- Если запрос висит 30 сек — за это время накопится 6 параллельных запросов.
- `loadZonesData` сам по себе делает 2-3 параллельных fetch (`/api/zones`, `/api/groups`, `/api/zones/next-watering-bulk`), плюс `/api/health-details` от health-panel.
- Нет AbortController — запросы не отменяются при unmount/перезагрузке.

**Impact:** На медленной сети UI тормозит, мобильный браузер "захлебывается", баттери drain. Пользователь видит spinning + "Нет связи" даже если запрос пройдёт через 30 сек.

**Recommendation:**
- Один `setInterval`-tick → проверка что предыдущий завершён (флаг `inFlight`).
- AbortController с timeout 5-7 сек.
- Exponential backoff при ошибках (5s → 10s → 20s).
- Page Visibility API: остановить опрос когда таб неактивен.

---

### [P1] Не закрывается `envProbeTimer` если страница navigates

**File:** `static/js/status.js:394-418`

```js
if (...) && !envProbeTimer) {
    envProbeAttempts = 0;
    envProbeTimer = setInterval(async () => { ... }, 1000);
}
```

**Описание:** `envProbeTimer` стартует если показывается "нет данных". Очищается только когда:
- Получили данные.
- 10 попыток исчерпаны.

При повторном открытии того же экрана (SPA-style hash-навигация — здесь её нет, но при wake-screen из bg) таймер может сосуществовать с новым. Также: если пользователь видит "нет данных" + пишет неправильно настроенный sensor — таймер на 10 сек × DOM-обновления.

**Impact:** Минорная утечка, но симптом — отсутствие centralized timer registry. Аналогично `_loadingTimer`, footer time intervals.

**Recommendation:** Page Visibility API: при `visibilitychange` → hidden → clearInterval всех periodic timers, на visible — restart.

---

### [P1] PWA cache strategy: иконки + manifest, всё остальное — fallback

**File:** `static/sw.js:3-10, 65-74`

**Описание:** Precache содержит только manifest + 3 иконки. CSS, JS, HTML — кэшируются **только если их уже однажды загрузили в режиме онлайн**. Поведение:
- Первая загрузка офлайн → пустая страница.
- На лошадиной 3G — пользователь видит белый экран пока CSS не пришёл.
- При фейле сети для `/api/zones` (cache-first для не-API) — стейл данные могут показываться, но через `network-first для API` — будет ошибка.

**Impact:** Для PWA "садовник на даче без сигнала" — приложение не запустится без интернета первый раз.

**Recommendation:**
- В precache добавить `/static/css/base.css`, `/static/js/app.js`, базовый offline-fallback `/offline.html`.
- Для `/static/css/*` и `/static/js/*` — stale-while-revalidate (быстрая загрузка, фоновое обновление).
- Push notifications для "Полив завершён" / "Нет связи с MQTT" — отсутствуют. Для commercial — было бы плюсом.

---

### [P1] Touch targets местами < 40px — Apple HIG требует 44pt, Material 48dp

**File:** `static/css/status.css:53-58, 297, 521, 548, 1064`

Examples:
- Карточка статуса group: `height: 32px; min-height: 32px;` (mobile mode, line 604-616)
- `.indicator { width: 16px; height: 16px; }` (status таблица — это нормально для индикатора, но если кликабельно — мало).
- Многие `min-height: 40px` — это меньше 44px Apple минимум.

**Impact:** Промахи по кнопкам на телефоне, особенно у пожилых пользователей (типичная аудитория садовода).

**Recommendation:** Все кликабельные элементы — `min-height: 44px; min-width: 44px;` на мобильных. Между соседними кликабельными — `gap: 8px;`.

---

### [P1] Версия приложения видна, но cache_buster не обновляется автоматически

**File:** `templates/base.html:1, 68`

```jinja
{% set version = 'v' %}
{% set cache_buster = version %}
...
<div>WB-Irrigation System v{{ app_version }}</div>
```

**Описание:**
- `version = 'v'` — захардкожено, не меняется. `cache_buster = 'v'` — тоже.
- `{{ app_version }}` — приходит из Flask-контекста, видна.
- `asset()` хелпер используется только для `app.js`/`audit.js`/`status.js`/`zones.js`, не для CSS и не для других JS-страниц.

**Impact:**
- При деплое CSS изменился → пользователь видит старый стиль (cache hit). Жалобы "у меня кнопки не как на скриншоте".
- Service Worker берёт `caches.match` для CSS → даже worse.

**Recommendation:**
- Использовать `{{ asset('static/css/base.css') }}` везде.
- В `asset()` хеш брать от mtime/content (а не от `app_version`).

---

### [P2] Inline event handlers `onclick="..."` повсюду

**File:** все templates: 106 onclick handlers + ~64 в JS через innerHTML

**Описание:** Множество inline `onclick=` — это:
1. Блокировка любой строгой CSP (`script-src 'self'` без `unsafe-inline`).
2. Дублирование вызова в HTML и JS-функции.
3. Сложно тестировать.

**Impact:** Не блокер, но мешает безопасностной зачистке (рекомендую sec-агенту).

**Recommendation:** Постепенный переход на event delegation. Для XSS-страниц с пользовательским вводом — приоритетно.

---

### [P2] `console.log/error` в production

**File:** `app.js`: 7, `programs.js`: 5, `status.js`: 5, `zones.js`: 14, etc — всего ~37+

Например:
```js
console.log(`[Perf] Status page: status=...`);
console.log('[UI Timing] ...');
console.log('SW registered: ', registration);
```

**Impact:** Замусоривают консоль (для саппорта), на ARM-WB небольшое замедление, утечка деталей реализации.

**Recommendation:** `if (window._debug) console.log(...)`. Или сборка с TerserPlugin, drop_console.

---

### [P2] Notification history — мёртвый код

**File:** `static/js/app.js:170-187`

```js
const notificationHistory = [];
function addToNotificationHistory(message, type) { ... }
```

Никем не читается, только пишется. Растёт до 50, потом shift. Дополнительная work на каждое уведомление.

**Recommendation:** Удалить (Karpathy: dead code → flag, не удаляй сам, но я отмечаю).

---

### [P2] Дублированный код status/* — мёртвая попытка модуляризации

**File:** `static/js/status/status-data.js`, `status-state.js`, `status-groups.js`, `status-utils.js` (всего ~700 строк)

**Описание:** Эти файлы **не подключаются ни в одном template** (`status.html` импортирует только `status.js`). Старая попытка разбить на модули, забытая. Контент частично дублируется с `status.js`. Уже зафиксировано в `irrigation-audit/findings/frontend.md`.

**Impact:** Confusion для разработчика, риск что кто-то поправит "не тот файл" → бага вернётся.

**Recommendation:** Удалить. Задача mv `status.js` → разбиение через ES modules или Vite/esbuild — отдельный refactor.

---

### [P2] Performance warning при load > 3s — пользователю показывается

**File:** `static/js/app.js:227-236`

```js
if (loadTime > 3000) {
    showNotification('Страница загружается медленно', 'warning');
}
```

**Описание:** На WB-ARM-боксе через VPN из дома по 3G — load > 3s **норма**. Каждый раз пользователь видит warning. Ничего не предлагает.

**Impact:** Шум, который пользователь учится игнорировать (boy who cried wolf). Brand: "приложение жалуется на себя".

**Recommendation:** Удалить или логировать только в console.

---

### [P2] Inline styles в шаблонах + CSS — двойной источник

**File:** `templates/base.html:42`, `status.html:75-76, 79, 88` (примеры)

```html
<button class="hamburger" onclick="this.classList.toggle('active');document.querySelector('header nav').classList.toggle('nav-open')">
```

Огромный inline-handler на гамбургере. Стилевые `style="display:none"` повсюду.

**Impact:** Maintenance, нельзя CSP без 'unsafe-inline'.

**Recommendation:** Перенести в `app.js`/`base.css` с использованием классов `.is-open`.

---

### [P2] Отсутствует "skeleton" / loading state на первый рендер

**File:** `templates/status.html:112-114`

```html
<div class="status-grid" id="groups-container">
    <!-- Группы будут загружены динамически -->
</div>
```

**Описание:** SSR-данные есть (`window._ssrZones`, `_ssrStatus` — base.html окружение это поддерживает), но если их нет (первая загрузка без warmup), пользователь видит **пустую страницу** на 1-3 сек пока fetch не вернётся. Спиннера нет.

**Recommendation:** Skeleton-screens (серые placeholders в форме карточек). Особенно важно на мобильном при медленной сети.

---

### [P2] Аccessibility — слабая поддержка screen reader

**File:** все templates

**Описание:**
- 15 aria-* атрибутов на 7 шаблонов — мало для приложения с динамической таблицей и live-данными.
- `groups-container` обновляется без `aria-live="polite"` → SR-пользователь не услышит "зона запустилась".
- `.zc-running-timer` тикает каждую секунду без `aria-live="off"` → SR будет читать таймер каждую секунду = неюзабельно.
- `connection-status` имеет роль warning, но без `aria-live` SR не узнает.
- Иконки (`📷`, `▶`, `⏹`, `🌿`) — без `role="img" aria-label="..."` или текстовой альтернативы рядом.
- Контраст: `nav a` на синем фоне с opacity — может не пройти 4.5:1.
- Touch targets (см. P1).

**Impact:** WCAG 2.1 AA не проходит. Для commercial single-tenant в РФ — формально не критично (нет 508), но любая проверка качества сделает замечание.

**Recommendation:**
- `<div id="groups-container" aria-live="polite" aria-atomic="false">` — но НЕ для таймеров.
- Добавить screen-reader-only текст к иконкам: `<span class="sr-only">Запустить</span>` рядом с `▶`.
- Тестирование с TalkBack/VoiceOver — отдельная сессия.

---

### [P2] Mobile breakpoint — magic number 1024px дублируется в JS

**File:** `static/js/status.js:307, 446, 450, 601, 605` (всего ~10 мест)

```js
const _m = window.innerWidth < 1024;
```

**Описание:** На каждом рендере читается `window.innerWidth` (force reflow!) и решается формат строки. Дублируется десятками раз.

**Impact:** На каждый rerender — N лишних layout-trigger reads. На медленной CSS-анимации (resize) — jank.

**Recommendation:** matchMedia('(max-width: 1023px)') один раз, listener на изменение, кеш.

---

### [P2] CSS `@media (prefers-reduced-motion)` есть, но `prefers-color-scheme: dark` ломает контраст

**File:** `static/css/base.css:18-26`

```css
@media (prefers-color-scheme: dark) {
    :root {
        --background-color: #1a1a1a;
        --card-background: #2d2d2d;
        --text-color: #ffffff;
        ...
    }
}
```

**Описание:** Только base.css имеет dark mode. `status.css`, `zones.css`, `programs.css` (всего 100+ KB CSS) — захардкоженные цвета. На устройстве с dark mode пользователь увидит белые карточки на чёрном фоне.

**Impact:** UX rough edge. Не блокер.

**Recommendation:** Либо отключить `prefers-color-scheme` пока не сделан полный dark theme. Либо реально пройти все стили.

---

### [P2] Пользовательский ввод имени зоны — нет защиты от длины

**File:** `templates/status.html:164` `<input type="text" class="sheet-input" id="editZoneName">`

**Описание:** Нет `maxlength`, нет валидации. Пользователь введёт 5000 символов → сохранит → весь UI развалится (карточки, таблица, breadcrumb). escapeHtml не помогает от layout-разрыва.

**Recommendation:** `maxlength="60"`. Backend validation тоже (другой агент).

---

### [P2] Login: `password.trim()` срезает trailing spaces — расхождение с backend

**File:** `templates/login.html:71`

```js
const password = document.getElementById('password').value.trim();
```

**Описание:** Если backend хеширует `password` как-есть (с пробелами), а frontend trim'ит — пароль с пробелами на конце сломается. Tiny edge case, но для commercial — лучше единообразно.

**Recommendation:** Не trim'ить пароль на фронте либо trim'ить и на сервере.

---

## Сводка

| Категория | P0 | P1 | P2 |
|-----------|----|----|----|
| JS quality | 2 | 4 | 3 |
| PWA / offline | 1 | 1 | 0 |
| Mobile UX | 1 | 1 | 1 |
| Accessibility | 0 | 0 | 2 |
| Performance | 0 | 1 | 2 |
| Brand / polish | 1 | 1 | 5 |
| **Всего** | **5** | **8** | **13** |

### P0 (must-fix перед коммерческим релизом):
1. **Math.random() в `flow-active`** — хаотическое мерцание карточек.
2. **Тройная-четверная обёртка `window.fetch`** — performance death + утечки.
3. **SW не уведомляет о новой версии** — обновления не доезжают до пользователя.
4. **Нет confirm на запуск зоны/группы** — случайный полив.
5. **Глобальный JS-error toast с stack-trace** — утечка деталей + UX trash.

### P1 (важно для качества, не блокирует):
6. Полный `innerHTML=''` re-render групп каждые 5 сек.
7. Дублирование `updateStatusDisplay` ↔ `refreshSingleGroup` (~150 строк).
8. `setAttribute('onclick',...)` на каждом тике (CSP, race).
9. Polling без AbortController/backoff/Page Visibility.
10. PWA кэширует только иконки — нет offline boot.
11. Touch targets местами < 44px.
12. Cache-buster `version='v'` захардкожен.
13. envProbeTimer и другие интервалы без cleanup.

### P2 (polish):
14-26. Inline handlers, console.log в проде, мёртвый код status/*, slow load warning, slabый screen-reader, отсутствие skeletons, magic number 1024 в JS, неполный dark theme, отсутствие maxlength, password.trim mismatch, dead notificationHistory.

---

## Рекомендуемый план действий

### Спринт 1 (1-2 дня, до бета-релиза):
- P0 #1, #4, #5 — простые фиксы (1-2 часа каждый).
- P0 #2 — консолидация fetch wrappers (3-4 часа).
- P0 #3 — controllerchange listener + reload UI (1-2 часа).

### Спринт 2 (3-5 дней, перед commercial):
- P1 #6, #7 — diff-based render + extraction `renderGroupCard` (1 день).
- P1 #9 — polling guard + AbortController (полдня).
- P1 #10 — расширить precache + offline.html (2 часа).
- P1 #11 — touch targets audit + CSS правки (полдня).

### Спринт 3 (опционально, perfecting):
- P2 cleanup, accessibility deep-dive, dark theme полный.
