export const meta = {
  name: 'irrigation-branch-review',
  description: 'Адверсариальное ревью диффа refactor/audit-2026-07 против main',
  phases: [
    { title: 'Review', detail: 'ревьюеры по областям диффа' },
    { title: 'Verify', detail: 'скептики проверяют каждую находку' },
  ],
}

const ROOT = '/Users/raulkhalikov/Projects/irrigation'
const COMMON = `Ты ревьюер ветки refactor/audit-2026-07 проекта wb-irrigation (Flask+MQTT+SQLite+APScheduler, прод на Wirenboard, Python 3.11, фичи 3.12+ запрещены). Корень: ${ROOT}. База сравнения: main.
Дифф смотри через: git diff main..HEAD -- <пути>  и  git log main..HEAD --oneline.
РЕЖИМ: только чтение кода; pytest можно запускать ТОЧЕЧНО (отдельные файлы), полный прогон уже идёт отдельно.
Ищи ТОЛЬКО реальные дефекты, привнесённые ЭТОЙ веткой: регрессии поведения, сломанные контракты (API-формы ответов, importable-имена персистентных APScheduler func-ref из jobs.db, patch-пути в тестах), удалённый код, на который остались живые ссылки (включая templates/, static/, shell-скрипты, systemd, sw.js precache, deploy.yml), ошибки в новых регексах/SQL, потерю поведения при дедупликации. НЕ отмечай стилистику и пред-существующие проблемы main.
Финальный текст — данные для оркестратора.`

const FINDINGS = {
  type: 'object', required: ['findings'],
  properties: { findings: { type: 'array', items: {
    type: 'object', required: ['title', 'file', 'evidence', 'scenario', 'severity'],
    properties: {
      title: { type: 'string' }, file: { type: 'string' },
      evidence: { type: 'string', description: 'точные строки/цитаты' },
      scenario: { type: 'string', description: 'конкретный сценарий сбоя: вход → неверный результат' },
      severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
    } } } },
}

const AREAS = [
  { key: 'scheduler-services', paths: 'irrigation_scheduler.py scheduler/ services/zone_control.py services/next_watering.py services/duration_conflicts.py services/events.py services/sse_hub.py services/rate_limiter.py' },
  { key: 'routes', paths: 'routes/ app.py services/logging_setup.py' },
  { key: 'db-tests-infra', paths: 'db/ database.py tests/conftest.py tests/fixtures/ requirements-dev.txt pytest.ini pyproject.toml setup.sh start_tests.sh .github/' },
  { key: 'frontend-docs', paths: 'static/ templates/ docs/ README.md prototypes' },
  { key: 'deleted-refs', paths: '', prompt: 'Специальная задача: составь список ВСЕХ удалённых веткой файлов и символов (git diff main..HEAD --diff-filter=D --name-only; плюс удалённые функции/классы из изменённых файлов) и проверь grep-ом по ТЕКУЩЕМУ дереву (код, тесты, шаблоны, скрипты, конфиги, sw.js, systemd, CI), что на них нет живых ссылок. Особо: static/js precache в sw.js, func-ref строки в коде для APScheduler jobs.db, patch()/import в оставшихся тестах.' },
]

phase('Review')
const reviews = await parallel(AREAS.map(a => () =>
  agent(`${COMMON}\n\nТвоя область: ${a.prompt ? a.prompt : `git diff main..HEAD -- ${a.paths}. Пройди дифф ПОЛНОСТЬЮ.`}`,
    { label: `review:${a.key}`, phase: 'Review', schema: FINDINGS })
))

const all = reviews.filter(Boolean).flatMap((r, i) => r.findings.map(f => ({ ...f, area: AREAS[i].key })))
log(`Находок до верификации: ${all.length}`)

const VERDICT = {
  type: 'object', required: ['real', 'reason'],
  properties: { real: { type: 'boolean' }, reason: { type: 'string' } },
}

phase('Verify')
const verified = await parallel(all.map(f => () =>
  agent(`${COMMON}\n\nТы СКЕПТИК. Опровергни находку ревью — проверь код сам. real=true только если дефект реально привнесён веткой (на main его нет) и сценарий воспроизводим. Пред-существующее на main => real=false (укажи это в reason).\n\nНаходка [${f.area}] (${f.severity}): ${f.title}\nФайл: ${f.file}\nEvidence: ${f.evidence}\nСценарий: ${f.scenario}`,
    { label: `verify:${f.title.slice(0, 40)}`, phase: 'Verify', schema: VERDICT })
    .then(v => ({ ...f, verdict: v }))
))

const real = verified.filter(f => f.verdict && f.verdict.real)
log(`Подтверждено реальных: ${real.length} из ${all.length}`)
return { real, dismissed: verified.filter(f => f.verdict && !f.verdict.real).map(f => ({ title: f.title, reason: f.verdict.reason })) }