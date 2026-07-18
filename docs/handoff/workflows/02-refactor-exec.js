export const meta = {
  name: 'irrigation-refactor-exec',
  description: 'Последовательное исполнение рефакторинга wb-irrigation батчами с тестовыми воротами',
  phases: [
    { title: 'Fix', detail: '9 последовательных батчей с прогоном тестов и коммитом' },
  ],
}

const ROOT = '/Users/raulkhalikov/Projects/irrigation'
const AUDIT = '/private/tmp/claude-501/-Users-raulkhalikov-Projects-irrigation/217a01dc-d063-428f-b956-332f611f5c25/scratchpad/audit.json'

const BASELINE_FAILURES = `tests/unit/test_version.py (5 тестов формата версии)
tests/unit/test_systemd_notify.py::test_notify_ready_sends_proper_payload
tests/integration/test_full_watering_cycle.py::TestFullWateringCycle::test_start_water_stop_cycle`

const COMMON = (baselineNote) => `Ты выполняешь батч рефакторинга Python/Flask-проекта wb-irrigation (полив на контроллере Wirenboard; Flask + MQTT + SQLite + APScheduler). Корень: ${ROOT}. Рабочая ветка: refactor/audit-2026-07 (уже checkout'нута).

ЖЁСТКИЕ ПРАВИЛА:
- Python минимум 3.11; ЗАПРЕЩЕНЫ фичи 3.12+ (tomllib, asyncio.TaskGroup, except*, generic syntax). PEP 604 (X | None) — можно.
- Проект работает в проде. Поведение менять только там, где это явно предписано находкой.
- Детали находок читай в JSON-файле ${AUDIT} (ключ confirmed, ищи по id). Перед УДАЛЕНИЕМ любого кода САМ перепроверь grep'ом по всему репо (включая tests/, templates/, static/, shell-скрипты, .service, docs), что на него нет живых ссылок. Если находка не подтверждается — пропусти её и отрази в отчёте.
- Стиль: как в окружающем коде. Никаких комментариев вида «удалено/отрефакторено», никаких упоминаний аудита в коде.
- Линт: .venv нет ruff, запускай «uvx ruff check <изменённые файлы>» (конфиг подхватится из pyproject.toml). Новых ошибок быть не должно.

ВЕРИФИКАЦИЯ (обязательно, в конце батча):
1. Целевые тесты изменённых областей.
2. Полный прогон: .venv/bin/python -m pytest -q -m "not mqtt_real and not e2e and not slow" -n 4 --timeout=60
3. Упавшие тесты перезапусти БЕЗ -n (серийно) — под xdist флейкают tests/api/test_coverage_boost.py::TestLoginLogout. Судить по серийному результату.
${baselineNote}

КОММИТ (только при зелёном прогоне):
- git add ТОЛЬКО явные пути (никогда -A; не трогай .claude/, .venv/, *.db, __pycache__).
- Сообщение: "refactor(audit): <краткая суть батча>" + маркированный список изменений, в конце строка:
Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
- Если какую-то находку не удаётся довести до зелёного — откати именно её (git checkout -- <files>), остальное закоммить, отрази в отчёте.

Финальный ответ — данные для оркестратора: JSON-подобный отчёт {done: [...], skipped: [{id, reason}], test_result: "...", commit: "sha или none"}.`

const BASE_NOTE_PRE = `Известные БАЗОВЫЕ падения (существуют до тебя, их чинит батч B — НЕ считай их своими регрессиями):
${BASELINE_FAILURES}`
const BASE_NOTE_POST = `После батча B базовых падений НЕТ: полный прогон обязан быть полностью зелёным (0 failed серийно).`

const BATCHES = [
  {
    key: 'A-tooling',
    note: BASE_NOTE_PRE,
    prompt: `БАТЧ A — зависимости и тулинг. Находки: dev-req-sqlalchemy-conflict, dev-req-selenium-dead, dev-req-missing-deps, pytest-config-duplicated, hyg-pytest-config-duplication, core-setup-bc-1, core-tests-script-1, hyg-broken-test-runner-refs, e2e-prod-creds-default-run.
Дополнительно: добавь .claude/ в .gitignore.
Указания: requirements-dev.txt переписать (убрать SQLAlchemy-пин и мёртвый selenium-стек/PyJWT, добавить реально используемые dev-зависимости — сверь с тем, что стоит в .venv и что требуют pytest.ini/CI). Конфиг pytest консолидируй в ОДНОМ месте (выбери pytest.ini как источник истины, убери разъехавшийся дубль из pyproject.toml, перенеся оттуда всё ценное). e2e/mqtt_real исключи из дефолтного прогона через addopts, прод-креды вынеси в env-переменные с понятной ошибкой при отсутствии. setup.sh: почини сравнение версий Python (bc не годится). start_tests.sh и README: убери/почини ссылки на несуществующие раннеры.`,
  },
  {
    key: 'B-tests',
    note: BASE_NOTE_PRE + '\nИменно ТВОЙ батч обязан устранить все базовые падения.',
    prompt: `БАТЧ B — лечение тестового слоя. Находки: tests-v2-xfail-stale (~80 вечных xfail «Not yet implemented» на реализованные фичи Programs v2 — сними маркеры, тесты должны проходить по-настоящему; 55 из них уже xpassed), desktop-sidebar-xfail-stale (6 xfail в tests/unit/test_desktop_sidebar.py — обнови ассерты под вынесенный CSS и сними маркеры), xfail-hides-water-usage-bug (xfail прячет РЕАЛЬНЫЙ баг: /api/water ломается при данных в water_usage — почини сам баг, потом сними xfail).
ПЛЮС три базовых падения не из аудита:
1) tests/unit/test_version.py — 5 тестов ждут старый формат "2.N (sha)", но коммит 3a1dc3d сменил формат на URL-safe "2.N+sha". Обнови тесты под НОВЫЙ формат (код прав, тесты устарели) и заодно устаревший докстринг services/version.py.
2) tests/unit/test_systemd_notify.py::test_notify_ready_sends_proper_payload — на macOS "AF_UNIX path too long" (tmp_path длиннее лимита 104). Бинди сокет в короткой директории (tempfile.mkdtemp(dir='/tmp')) с очисткой.
3) tests/integration/test_full_watering_cycle.py::test_start_water_stop_cycle — тест мокает services.zone_control.water_monitor голым MagicMock; в stop_zone (services/zone_control.py:753) t_l, a_lpm = water_monitor.summarize_run(...) падает ValueError (MagicMock.__iter__ пуст), из-за чего zone_runs не финализируется и get_last_watering_time даёт None. Почини ТЕСТ: настрой мок (summarize_run.return_value=(None,None), get_pulses_at_or_after.return_value=None и т.п.) так, чтобы тест проверял то, что задуман проверять.
После батча полный прогон обязан быть полностью зелёным серийно.`,
  },
  {
    key: 'C-dead-python',
    note: BASE_NOTE_POST,
    prompt: `БАТЧ C — мёртвый Python-код. Находки: sched-dead-mixins (5 mixin-модулей scheduler/ ~1350 строк никем не наследуются — scheduler/jobs.py ЖИВ, его не трогать сверх находки sched-manual-run-stale-job; поправь тест, ссылающийся на mixins), queue-float-dead-1 (program_queue.py + float_monitor.py не подключены в прод; ВНИМАНИЕ: перед удалением проверь, используются ли методы FloatRepository/фасада database.py живыми routes — если да, репозиторий оставь, удаляй только мёртвых потребителей; находка queue-float-contract-2 при удалении становится неактуальной — так и отметь), decision-engine-dead-3 (et_calculator.py + irrigation_decision.py), weather-merge-dead-5 (services/weather/merge.py и мёртвый шим weather_merged.py если он тоже без живых потребителей), routes-ssr-status-broken (мёртвый SSR-префетч в routes/status.py), routes-dead-telegram-bp (мёртвый import telegram_bp в app.py), sched-manual-run-stale-job (переведи ручной запуск программы на живой callable из irrigation_scheduler, не ломая импортируемость имён для персистентных func-ref джобов в jobs.db).
Удаляй вместе с кодом и его мёртвые тесты (тесты, тестировавшие только удалённый код). Не удаляй ничего, что тянут живые импорты.`,
  },
  {
    key: 'D-dead-frontend',
    note: BASE_NOTE_POST,
    prompt: `БАТЧ D — мёртвый и дублированный frontend. Находки: fe-dead-split-modules (8 неподключённых JS-модулей ~1766 строк — static/js/status/* и zones-*.js; перед удалением проверь grep'ом по templates/ и sw.js, что их никто не грузит), fe-dead-programs-js (static/js/programs.js мёртв — страница живёт на inline-скрипте; обнови/перенацель XSS-тесты, которые на него смотрят), fe-dead-v1-table (мёртвые ветки V1-таблицы в status.js — целевые DOM-элементы отсутствуют в шаблоне; вырезай консервативно, только доказуемо мёртвые ветки), fe-dup-helpers (КОНСЕРВАТИВНО: только дословно идентичные утилиты — CSRF-хелпер, escapeHtml-fallback — вынеси в один маленький общий файл, подключаемый в base.html ДО остальных скриптов; toast-системы и синхронизацию времени НЕ объединяй, только зафиксируй в отчёте).
Также реши судьбу prototypes/*.html (2 файла) — если ни один тест/шаблон/скрипт их не использует, перенеси в docs/archive/prototypes/.
Помни: билд-системы нет, это embedded; сервис-воркер sw.js кэширует ассеты — проверь его список precache и поправь при удалении файлов.`,
  },
  {
    key: 'E-bugfixes',
    note: BASE_NOTE_POST,
    prompt: `БАТЧ E — точечные баг-фиксы (все safe). Находки: mqtt-pwd-wipe (update_mqtt_server затирает пароль при сохранении из UI — пустой/отсутствующий password должен означать «не менять»), routes-emergency-bypass (групповые старты — start-from-first, run-selected, запуск программы, telegram — обязаны проверять EMERGENCY_STOP как одиночные), events-dedup-6 (дедупликация событий без TTL — добавь TTL, чтобы повторный emergency-stop доходил до Telegram), core-pii-mask-1 (PII-фильтры в services/logging_setup.py не маскируют значения секретов — почини regex'ы, докажи юнит-тестом), fe-water-route-500 (GET /water рендерит несуществующий water.html — почини: либо восстанови шаблон из живых кусков, либо убери маршрут и ссылки на него — выбери по факту наличия ссылок в UI), fe-keydown-debug-panels (хоткеи 'h'/'s' в app.js не должны срабатывать при фокусе в input/textarea/contenteditable), routes-photo-rotate-format (поворот WebP молча сохраняет JPEG под .webp — сохраняй в исходном формате).
На каждый Python-фикс — регрессионный тест рядом с существующими тестами области.`,
  },
  {
    key: 'F-dedup-core-db',
    note: BASE_NOTE_POST,
    prompt: `БАТЧ F — дедупликация ядра и БД. Находки: zc-dup-peerstop-7 (sequential-fallback в exclusive_start_zone дословно копирует _stop_peer — унифицируй), zc-dup-waterstats-8 (финализация водной статистики продублирована в двух ветках stop_zone — вынеси хелпер), sched-jobs-dup (два реестра APScheduler job-callables — унифицируй через re-export, КРИТИЧНО: сохрани importable-имена функций, на которые ссылаются персистентные джобы в jobs.db через func-ref строки), db/zones-update-copypaste (тройной copy-paste if-цепочек UPDATE в db/zones.py ~250 строк — единый построитель с явным списком разрешённых полей; дрейф полей устрани в пользу самого полного варианта), db/mig-addcol-boilerplate (~20 блоков add-column в db/migrations.py — общий хелпер _add_column_if_missing), db/mig-swallow-mark-applied (миграция, упавшая с sqlite3.Error, НЕ должна помечаться применённой — аккуратно: поведение раннера при сбое должно быть «лог + не помечать + не валить приложение», сверься с тем, как раннер вызывается при старте), core-auth-dup-1 (два before_request-хука с расходящимися whitelist — единый источник правды), core-bootsync-dup-1 (закрытие master-клапанов трижды — общий хелпер).
Это чистые рефакторинги: поведение (кроме исправления mig-swallow и дрейфа полей) меняться не должно. Прогоняй целевые тесты после каждой находки, не копи.`,
  },
  {
    key: 'G1-dedup-routes',
    note: BASE_NOTE_POST,
    prompt: `БАТЧ G1 — дедупликация routes. Находки: routes-dup-next-watering (три реализации расчёта «следующего полива» с разошедшейся семантикой — единый сервис; семантику выбери по самому корректному варианту и зафиксируй тестом), routes-dup-zone-start (три эндпоинта старта зоны с разной ручной оркестрацией вокруг exclusive_start_zone — вынеси общую оркестрацию в services), routes-dup-duration-conflicts (~90 строк дубля single vs bulk, у single ещё и N+1 запросы — унифицируй на bulk-версии), routes-dup-postpone (postpone продублирован между HTTP API и Telegram — общий сервис).
Бизнес-логику выноси в services/, роуты оставляй тонкими. Не меняй внешние контракты API (пути, формы ответов) — на них смотрит frontend и тесты.`,
  },
  {
    key: 'G2-behavior',
    note: BASE_NOTE_POST,
    prompt: `БАТЧ G2 — поведенческие фиксы (moderate, каждый с тестом). Находки: sse-stale-subs-10 (sse_hub строит MQTT-подписки и карты топиков один раз — сделай перестройку при изменении зон/групп; выбери минимально инвазивный механизм, например пересборка по событию изменения конфигурации зон), db/next-run-ignores-enabled (compute_next_run_for_zone игнорирует programs.enabled, schedule_type interval/even-odd и extra_times — почини с учётом семантики, закреплённой в G1-сервисе «следующего полива»), sched-recovery-gap (recover_missed_runs после рестарта видит только weekdays и основное время — расширь на interval/even-odd и extra_times, сверь с логикой расчёта расписания в живом irrigation_scheduler.py).
Каждый фикс — отдельный коммит-кандидат внутри батча; если какой-то не доводится до зелёного — откати его одного.`,
  },
  {
    key: 'H-docs-ci',
    note: BASE_NOTE_POST,
    prompt: `БАТЧ H — документация и CI. Находки: docs-readme-triplication (README-LONG.md и README-SHORT.md устарели на ~11 месяцев — в docs/archive/; README.md актуализируй: убери ссылки на несуществующее, поправь под текущую структуру), hyg-deploy-yml-wrong-python (deploy.yml ставит зависимости системным pip3 python3.9 вместо venv c 3.11 — почини по образцу install_wb.sh/update_server.sh), плюс генеральная уборка корня: устаревшие рабочие документы (NEXT-AGENT-PROMPT.md, PROMPT-*.md, SPEC-CIRCULAR-IMPORTS.md — устарел, цикл устранён; SPEC-RATE-LIMITING.md, SPEC-REVERT-AUTH.md — проверь актуальность, ARCHITECTURE-REPORT.md, BUGS-REPORT.md, REFACTOR-REPORT.md, EXPERT-ANALYSIS.html, TESTS_V2_SUMMARY.md, CHANGELOG_desktop_sidebar.md, MOBILE_RESPONSIVE_CHANGES.md, test_results_final.txt, reports/ целиком) — перемести в docs/archive/ с сохранением структуры (git mv). НИЧЕГО не удаляй безвозвратно, только архивируй. CLAUDE.md, README.md, VERSION оставь в корне. Проверь, что ни один тест/скрипт не читает перемещённые файлы (grep). deploy.yml: если это GitHub Actions — найди его в .github/workflows/.`,
  },
]

phase('Fix')
const results = []
for (const b of BATCHES) {
  log(`Батч ${b.key}`)
  const r = await agent(`${COMMON(b.note)}\n\n${b.prompt}`, { label: `fix:${b.key}`, phase: 'Fix' })
  results.push({ batch: b.key, report: r })
  if (r === null) log(`Батч ${b.key}: агент не вернул результат`)
}
return results