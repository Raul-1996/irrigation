export const meta = {
  name: 'irrigation-full-review-bughunt',
  description: 'Полное ревью диффа ветки пятью линзами + охота за багами по всей кодовой базе со скептиками',
  phases: [
    { title: 'Review', detail: '5 линз по всему диффу main..HEAD' },
    { title: 'BugHunt', detail: '7 направлений по живому коду, до сухого раунда' },
    { title: 'Verify', detail: 'панель скептиков по каждой находке' },
  ],
}

const ROOT = '/Users/raulkhalikov/Projects/irrigation'
const COMMON = `Проект wb-irrigation: Flask + MQTT (paho 2.1.0) + SQLite + APScheduler, прод — контроллер Wirenboard (один процесс, hypercorn + фоновые треды). Python минимум 3.11, фичи 3.12+ запрещены. Корень: ${ROOT}. Ветка refactor/audit-2026-07 (16 коммитов поверх main): аудит-рефакторинг — удалён мёртвый код, дедупликация, баг-фиксы; полный дифф: git diff main..HEAD, список коммитов: git log main..HEAD --oneline.
РЕЖИМ: только чтение; pytest можно запускать точечно (отдельные файлы/тесты) для проверки гипотез.
Ищи ТОЛЬКО реальные дефекты с конкретным сценарием сбоя (вход → неверное поведение). НЕ стилистика, НЕ теоретические придирки, НЕ «стоило бы иначе». Финальный текст — данные для оркестратора.`

const FINDINGS = {
  type: 'object', required: ['findings'],
  properties: { findings: { type: 'array', items: {
    type: 'object', required: ['title', 'file', 'line', 'evidence', 'scenario', 'severity', 'introduced_by_branch'],
    properties: {
      title: { type: 'string' }, file: { type: 'string' }, line: { type: 'integer' },
      evidence: { type: 'string' }, scenario: { type: 'string' },
      severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
      introduced_by_branch: { type: 'boolean', description: 'true если дефект привнесён веткой, false если существует и на main' },
    } } } },
}

const REVIEW_LENSES = [
  { key: 'correctness', prompt: 'Линза КОРРЕКТНОСТЬ: пройди дифф main..HEAD полностью, коммит за коммитом (git log main..HEAD --oneline; git show <sha>). Ищи логические ошибки в новом/изменённом коде: перепутанные условия, off-by-one, неверные значения по умолчанию, потерянные ветки поведения при дедупликации, неверную обработку None/пустых значений.' },
  { key: 'concurrency', prompt: 'Линза КОНКУРЕНТНОСТЬ: по диффу main..HEAD проверь всё, что касается локов, тредов, паho-колбэков, APScheduler-джобов, SSE. Дедлоки, гонки, вызовы блокирующих операций под локами, доступ к разделяемому состоянию без лока, реэнтерабельность. Особо: services/sse_hub.py (reload_hub), services/zone_control.py (locks, _stop_peer, _finalize_water_stats), services/events.py (TTL-дедуп), services/rate_limiter.py (reset_all).' },
  { key: 'contracts', prompt: 'Линза КОНТРАКТЫ: по диффу main..HEAD проверь согласованность интерфейсов: формы ответов API vs ожидания frontend (static/js, inline-скрипты шаблонов); importable-имена персистентных APScheduler func-ref (jobs.db) vs фактические модули; сигнатуры функций vs все call-sites (grep); patch-пути в тестах vs реальные модули; схема БД/миграции vs SQL-запросы; sw.js precache vs фактические файлы; canonical even-odd vs все места сравнения schedule_type.' },
  { key: 'security', prompt: 'Линза БЕЗОПАСНОСТЬ: по диффу main..HEAD — новые эндпоинты/изменённые роуты (auth-декораторы, CSRF, guest-доступ), PII-маскировка (services/logging_setup.py — обойди регексы), SQL (f-string в db/mqtt.py password_sql — инъекция?), путь фото/файлов, e2e-креды, аудит-события.' },
  { key: 'test-quality', prompt: 'Линза КАЧЕСТВО ТЕСТОВ: по диффу main..HEAD проверь новые/изменённые тесты: ассертят ли они то, что заявляют; не замокано ли ровно то, что тестируется; не станут ли они флейками (время, чётность дня месяца, полночь, порядок); тесты со снятыми xfail — реально ли проверяют фичу. Особо: тесты чётности (even-odd) сломаются в определённые дни месяца? тесты next-watering у полуночи? test_recover_even_odd_null_means_odd?' },
]

const HUNT_LENSES = [
  { key: 'time-math', prompt: 'ОХОТА, направление ВРЕМЯ: irrigation_scheduler.py, services/next_watering.py, db/zones.py (_first_program_start_after, compute_next_run), scheduler/jobs.py. Полночь, переход суток во время полива, DST, misfire_grace, окна recovery, extra_times сортировка, сравнение naive/aware datetime, strftime/parse форматы.' },
  { key: 'concurrency-live', prompt: 'ОХОТА, направление КОНКУРЕНТНОСТЬ (весь живой код, не только дифф): services/zone_control.py, services/locks.py, services/mqtt_pub.py, services/observed_state.py, services/watchdog.py, services/sse_hub.py, app.py (_boot_sync, group-exclusivity watchdog). Гонки старта/стопа зон, двойной запуск джобов, TOCTOU на state, локи через процессы vs треды.' },
  { key: 'sqlite', prompt: 'ОХОТА, направление SQLITE: db/base.py (retry_on_busy, _connect), db/*.py, database.py. Транзакционность многошаговых операций, commit внутри циклов, чтение-модификация-запись без транзакции, JSON-колонки (days, extra_times) — парсинг мусора, NULL-семантика, миграции на живой БД.' },
  { key: 'mqtt', prompt: 'ОХОТА, направление MQTT: services/mqtt_pub.py, services/observed_state.py, services/sse_hub.py, services/zone_control.py (publish paths), routes/mqtt_api.py. Reconnect/resubscribe, retained-эхо как ложное подтверждение, QoS, потеря callback-исключений в paho-треде, шифрование пароля (ENC:), таймауты.' },
  { key: 'security-live', prompt: 'ОХОТА, направление БЕЗОПАСНОСТЬ (весь живой код): routes/auth.py, app.py (before_request, CSRF exempt), routes/files.py, routes/zones_photo_api.py (upload/rotate — path traversal, content-type), services/security.py, services/auth_service.py, guest/viewer права на мутирующие эндпоинты, rate limiter обходы.' },
  { key: 'frontend-logic', prompt: 'ОХОТА, направление FRONTEND: static/js/status.js, zones.js, app.js, common.js, inline-скрипты templates/. Таймеры/countdown после SSE-переподключения, гонки render vs fetch, escapeHtml пропуски (innerHTML с сырыми данными), обработка ошибок fetch, двойные подписки на события.' },
  { key: 'api-contracts-live', prompt: 'ОХОТА, направление API-КОНТРАКТЫ (весь живой код): сверь fetch-вызовы из static/js и inline-скриптов с реальными роутами (метод, путь, payload-поля, форма ответа). Ищи обращения к несуществующим эндпоинтам, поля, которые бэкенд игнорирует/переименовал, расхождения статус-кодов, которые JS трактует как успех.' },
]

phase('Review')
const reviews = await parallel(REVIEW_LENSES.map(l => () =>
  agent(`${COMMON}\n\n${l.prompt}\n\nПомечай introduced_by_branch честно: сравнивай с main (git show main:<file>).`,
    { label: `review:${l.key}`, phase: 'Review', schema: FINDINGS })
))

phase('BugHunt')
const seen = new Set()
const key = f => `${f.file}|${(f.title || '').toLowerCase().slice(0, 60)}`
let hunted = []
let dry = 0
let round = 0
while (dry < 2 && round < 3) {
  round++
  const prev = hunted.length ? `\nУЖЕ НАЙДЕНО в прошлых раундах (НЕ повторяй, ищи ДРУГОЕ):\n${hunted.map(f => `- ${f.file}: ${f.title}`).join('\n')}` : ''
  const batch = await parallel(HUNT_LENSES.map(l => () =>
    agent(`${COMMON}\n\n${l.prompt}${prev}\n\nРаунд ${round}. Ищи дефекты, которых ещё нет в списке выше.`,
      { label: `hunt:${l.key}:r${round}`, phase: 'BugHunt', schema: FINDINGS })
  ))
  const fresh = batch.filter(Boolean).flatMap(r => r.findings).filter(f => !seen.has(key(f)))
  if (!fresh.length) { dry++; continue }
  dry = 0
  fresh.forEach(f => seen.add(key(f)))
  hunted.push(...fresh)
  log(`Раунд ${round}: +${fresh.length} находок (всего ${hunted.length})`)
}

const reviewFindings = reviews.filter(Boolean).flatMap((r, i) => r.findings.map(f => ({ ...f, source: `review:${REVIEW_LENSES[i].key}` })))
const huntFindings = hunted.map(f => ({ ...f, source: 'hunt' }))
const all = [...reviewFindings, ...huntFindings]
log(`Всего до верификации: ${all.length} (ревью ${reviewFindings.length}, охота ${huntFindings.length})`)

const VERDICT = {
  type: 'object', required: ['real', 'reason'],
  properties: { real: { type: 'boolean' }, reason: { type: 'string' } },
}

phase('Verify')
const verified = await parallel(all.map(f => () =>
  parallel([
    () => agent(`${COMMON}\n\nТы СКЕПТИК-опровергатель. Проверь код сам и попытайся ОПРОВЕРГНУТЬ находку. real=true только если сценарий реально воспроизводим в проде как описано. Сомневаешься — real=false.\n\n[${f.source}/${f.severity}] ${f.title}\n${f.file}:${f.line}\nEvidence: ${f.evidence}\nСценарий: ${f.scenario}`,
      { label: `v1:${f.title.slice(0, 35)}`, phase: 'Verify', schema: VERDICT }),
    () => agent(`${COMMON}\n\nТы СКЕПТИК-практик. Вопрос не «формально ли верно», а «случится ли это на реальном контроллере и заметит ли пользователь». Проверь код сам. real=false если сценарий требует нереалистичных предусловий или эффект пренебрежим.\n\n[${f.source}/${f.severity}] ${f.title}\n${f.file}:${f.line}\nEvidence: ${f.evidence}\nСценарий: ${f.scenario}`,
      { label: `v2:${f.title.slice(0, 35)}`, phase: 'Verify', schema: VERDICT }),
  ]).then(vs => {
    const votes = vs.filter(Boolean)
    return { ...f, real: votes.length === 2 && votes.every(v => v.real), reasons: votes.map(v => v.reason) }
  })
))

const confirmed = verified.filter(f => f.real)
const order = { critical: 0, high: 1, medium: 2, low: 3 }
confirmed.sort((a, b) => (order[a.severity] ?? 9) - (order[b.severity] ?? 9))
log(`Подтверждено обоими скептиками: ${confirmed.length} из ${all.length}`)
return {
  confirmed,
  dismissed: verified.filter(f => !f.real).map(f => ({ title: f.title, file: f.file, severity: f.severity, reasons: f.reasons })),
}