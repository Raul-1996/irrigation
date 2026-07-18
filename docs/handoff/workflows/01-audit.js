export const meta = {
  name: 'irrigation-audit-refactor',
  description: 'Аудит кодовой базы irrigation по подсистемам с адверсариальной проверкой находок',
  phases: [
    { title: 'Audit', detail: 'параллельные ридеры по 8 подсистемам' },
    { title: 'Verify', detail: 'адверсариальная проверка ключевых находок' },
  ],
}

const ROOT = '/Users/raulkhalikov/Projects/irrigation'

const COMMON = `Ты аудитор Python/Flask-проекта wb-irrigation (система управления поливом на контроллере Wirenboard, Flask + MQTT + SQLite + APScheduler). Корень: ${ROOT}.
ВАЖНЫЕ ОГРАНИЧЕНИЯ ПРОЕКТА (из CLAUDE.md): минимум Python 3.11, НЕЛЬЗЯ фичи 3.12+ (tomllib, TaskGroup, except*), ruff target py311. Проект работает в проде — рефакторинг должен быть безопасным.
РЕЖИМ: только чтение. НЕ изменяй файлы. НЕ запускай pytest всего проекта (он уже бежит в фоне). Можно запускать grep, wc, python -c для импортов/AST-анализа, ruff check в режиме отчёта.
Твоя финальная текстовая выдача — это данные для оркестратора, не сообщение человеку.`

const FINDINGS_SCHEMA = {
  type: 'object',
  required: ['subsystem_summary', 'findings'],
  properties: {
    subsystem_summary: { type: 'string', description: '3-6 предложений: что делает подсистема, общее состояние' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'kind', 'severity', 'title', 'evidence', 'proposal', 'risk'],
        properties: {
          id: { type: 'string', description: 'короткий slug, напр. sched-dup-1' },
          kind: { type: 'string', enum: ['bug', 'dead-code', 'duplication', 'structure', 'hygiene', 'dependency', 'performance', 'security'] },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          title: { type: 'string' },
          evidence: { type: 'string', description: 'файлы:строки и конкретные цитаты/факты, доказывающие проблему' },
          proposal: { type: 'string', description: 'конкретное предложение рефакторинга' },
          risk: { type: 'string', enum: ['safe', 'moderate', 'risky'], description: 'риск сломать прод при выполнении proposal' },
        },
      },
    },
  },
}

const SUBSYSTEMS = [
  {
    key: 'scheduler',
    prompt: `Подсистема: ПЛАНИРОВЩИК. Файлы: irrigation_scheduler.py (2358 строк) и пакет scheduler/ (events.py, jobs.py, program_runner.py, schedule_calc.py, state.py, zone_runner.py, ~1500 строк суммарно).
Ключевой вопрос: irrigation_scheduler.py — это god-файл, рядом с которым лежит частично выделенный пакет scheduler/. Разберись в их отношениях: кто кого импортирует, есть ли дублирование логики, что из irrigation_scheduler.py реально используется, какие куски можно безопасно вынести/удалить. Составь карту публичного API irrigation_scheduler.py (что импортируют другие модули — проверь grep'ом по всему проекту, включая tests/). Найди мёртвые функции, дубли, смешение ответственностей.`,
  },
  {
    key: 'routes',
    prompt: `Подсистема: ROUTES (Flask blueprints). Каталог routes/ — 23 файла, ~6800 строк: zones_crud_api.py, zones_watering_api.py, zones_history_api.py, zones_photo_api.py, groups_api.py, programs_api.py, system_status_api.py, system_config_api.py, system_emergency_api.py, mqtt_api.py, weather_api.py, health_api.py, auth.py, audit_api.py, files.py, reports.py, settings.py, telegram.py, а также старые zones.py, groups.py, programs.py, mqtt.py, status.py.
Ключевые вопросы: (1) старые файлы zones.py/groups.py/programs.py/mqtt.py/status.py — живы или это остатки до-blueprint эпохи? Проверь регистрацию blueprint'ов в app.py. (2) дублирование логики между route-файлами (валидация, сериализация, обработка ошибок). (3) бизнес-логика, затащенная в routes вместо services. Проверь каждый файл.`,
  },
  {
    key: 'services',
    prompt: `Подсистема: SERVICES. Каталог services/ — 44 файла, ~11500 строк, плюс подкаталоги services/logs, services/monitors, services/weather.
Особое внимание: (1) weather-стек — services/weather/ (в т.ч. adjustment.py 833 строки), weather_adjustment.py, weather_merged.py, weather_codes.py, et_calculator.py, irrigation_decision.py — есть подозрение на два поколения погодной логики, разберись что живое, что legacy. (2) zone_control.py (1043 строки) — god-файл? (3) float_monitor.py, monitors/, watchdog.py, observed_state.py — пересечение ответственностей мониторинга. (4) helpers.py — свалка? Найди мёртвый код (grep по импортам), дубли, файлы-кандидаты на объединение/разбиение.`,
  },
  {
    key: 'db',
    prompt: `Подсистема: DATA LAYER. Файлы: database.py (388 строк, фасад), каталог db/ (base.py, zones.py 928 строк, programs.py, groups.py, mqtt.py, settings.py, telegram.py, logs.py, migrations.py 1653 строки), каталог migrations/.
Ключевые вопросы: (1) database.py как прокси-фасад — все ли его реэкспорты используются, можно ли сузить? Проверь grep'ом какие импорты идут через database.py vs напрямую db/. (2) db/migrations.py 1653 строки — как устроены миграции, есть ли мёртвые. (3) db/zones.py 928 строк — что там разрослось. (4) сырой SQL vs повторяющиеся паттерны — есть ли copy-paste CRUD. (5) потокобезопасность соединений SQLite (check_same_thread, WAL, локи).`,
  },
  {
    key: 'core',
    prompt: `Подсистема: ЯДРО ПРИЛОЖЕНИЯ. Файлы: app.py (792 строки), config.py, constants.py, run.py, utils.py, services/app_init.py, services/logging_setup.py, services/shutdown.py, services/systemd_notify.py, wb-irrigation.service, start.sh, setup.sh, install_wb.sh, update_server.sh, uninstall_wb.sh, install.bat, start.bat, start_mqtt.sh, start_tests.sh.
Ключевые вопросы: (1) app.py 792 строки — что в нём осталось после рефакторинга v2, что ещё можно вынести (middleware, error handlers, jinja filters?). (2) порядок инициализации и circular imports (есть SPEC-CIRCULAR-IMPORTS.md — прочти и проверь актуальность). (3) дубли между shell-скриптами, мёртвые скрипты (Windows .bat нужны?). (4) config.py vs constants.py vs хардкод по коду.`,
  },
  {
    key: 'frontend',
    prompt: `Подсистема: FRONTEND. Каталоги static/ и templates/.
Ключевые вопросы: (1) размеры и god-файлы JS — status.js, zones.js и др. (в старом REFACTOR-REPORT.md status.js был 2187 строк с 8+ ответственностями, проверь текущее состояние). (2) дублирование между JS-файлами (fetch-обёртки, toast, форматирование времени, SSE-подписки). (3) inline <script> в шаблонах vs static/js. (4) мёртвые шаблоны/JS/CSS (grep по render_template и url_for). (5) prototypes/*.html — используются?
НЕ предлагай переписывание на фреймворки — только vanilla JS реорганизацию. Помни: это embedded-устройство, билд-систем нет.`,
  },
  {
    key: 'tests',
    prompt: `Подсистема: ТЕСТЫ. Каталог tests/ — 186 файлов, ~31000 строк. pytest.ini: markers mqtt_real, e2e, slow, serial; testpaths=tests.
Ключевые вопросы: (1) структура tests/ по подкаталогам — какие группы есть, есть ли дубли-поколения тестов (v1 vs v2). (2) сколько тестов skip/xfail навсегда — найди decorators grep'ом. (3) conftest.py — сколько их, дублируются ли фикстуры. (4) тесты, тестирующие удалённый/несуществующий код (импорты падают). (5) requirements-dev.txt пинит SQLAlchemy==1.4.52, конфликтуя с requirements.txt (>=2.0) — проверь, зачем dev-пин, что сломается. (6) selenium-тесты — живы ли (pytest.ini отключает плагин selenium!). НЕ запускай полный pytest — он уже бежит; можно запускать точечно отдельные файлы с --co.`,
  },
  {
    key: 'hygiene',
    prompt: `Подсистема: ГИГИЕНА РЕПОЗИТОРИЯ. В корне ~20 .md/.html файлов (ARCHITECTURE-REPORT.md, BUGS-REPORT.md, EXPERT-ANALYSIS.html, NEXT-AGENT-PROMPT.md, PROMPT-*.md, SPEC-*.md, README-LONG.md, README-SHORT.md, README.md, CHANGELOG_desktop_sidebar.md, MOBILE_RESPONSIVE_CHANGES.md, TESTS_V2_SUMMARY.md, test_results_final.txt и др.), каталоги reports/ (24 файла), irrigation-audit/, specs/, prototypes/, tools/, docker/, mosquitto/, configs/.
Ключевые вопросы: (1) какие документы устарели/дублируют друг друга (сверь даты и содержание с текущим кодом) — предложи что удалить, что переместить в docs/, что оставить. (2) tools/MQTT_emulator (999 строк) — используется в тестах? (3) docker/ — CLAUDE.md говорит «Docker не используется на проде, оставлен для локальной разработки» — в каком он состоянии. (4) .gitignore полнота (venv, .db, __pycache__). (5) pyproject.toml vs requirements.txt согласованность. Прочти README.md и сверь с реальностью.`,
  },
]

phase('Audit')
log('Аудит 8 подсистем параллельно')

const auditResults = await parallel(SUBSYSTEMS.map(s => () =>
  agent(`${COMMON}\n\n${s.prompt}\n\nВерни структурированные находки. В evidence обязательно точные пути и номера строк. Ставь kind='bug' только если есть конкретный сценарий сбоя. Дай не менее полного покрытия: пройди по КАЖДОМУ файлу подсистемы хотя бы бегло.`,
    { label: `audit:${s.key}`, phase: 'Audit', schema: FINDINGS_SCHEMA })
    .then(r => r ? { key: s.key, ...r } : null)
))

const audits = auditResults.filter(Boolean)
const allFindings = audits.flatMap(a => a.findings.map(f => ({ ...f, subsystem: a.key })))
log(`Собрано находок: ${allFindings.length} из ${audits.length}/8 подсистем`)

// Верифицируем только значимые находки; low-severity hygiene принимаем как есть
const toVerify = allFindings.filter(f =>
  f.severity === 'critical' || f.severity === 'high' ||
  (f.severity === 'medium' && (f.kind === 'bug' || f.kind === 'dead-code' || f.kind === 'duplication')))
log(`На адверсариальную проверку: ${toVerify.length}`)

const VERDICT_SCHEMA = {
  type: 'object',
  required: ['confirmed', 'reason', 'corrected_evidence', 'safe_to_fix'],
  properties: {
    confirmed: { type: 'boolean' },
    reason: { type: 'string' },
    corrected_evidence: { type: 'string', description: 'уточнённые факты/строки, если оригинал неточен; иначе пустая строка' },
    safe_to_fix: { type: 'boolean', description: 'можно ли чинить без риска для прода' },
  },
}

phase('Verify')
const verified = await parallel(toVerify.map(f => () =>
  agent(`${COMMON}\n\nТы СКЕПТИК. Твоя задача — ОПРОВЕРГНУТЬ находку аудита. Проверь факты в коде сам (Read/Grep). Если находка неточна, преувеличена или предложение сломает работающее поведение — confirmed=false. Если сомневаешься — confirmed=false.\n\nНаходка [${f.subsystem}/${f.id}] (${f.kind}, ${f.severity}): ${f.title}\nEvidence: ${f.evidence}\nProposal: ${f.proposal}`,
    { label: `verify:${f.id}`, phase: 'Verify', schema: VERDICT_SCHEMA })
    .then(v => v ? { ...f, verdict: v } : { ...f, verdict: null })
))

const confirmed = verified.filter(f => f.verdict && f.verdict.confirmed)
const rejected = verified.filter(f => f.verdict && !f.verdict.confirmed)
const lowAccepted = allFindings.filter(f => !toVerify.includes(f))

log(`Подтверждено: ${confirmed.length}, отклонено скептиками: ${rejected.length}, принято без проверки (low): ${lowAccepted.length}`)

return {
  summaries: audits.map(a => ({ subsystem: a.key, summary: a.subsystem_summary })),
  confirmed,
  rejected: rejected.map(f => ({ id: f.id, subsystem: f.subsystem, title: f.title, reason: f.verdict.reason })),
  low_accepted: lowAccepted,
}