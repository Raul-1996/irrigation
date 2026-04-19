# Phase 5 — Tony Planner Prompt

Промпт для запуска нового сеанса Tony в авторежиме.
Цель: сборка команды агентов под полный рефакторинг и закрытие всех находок аудита `wb-irrigation`.

**Дата:** 2026-04-19
**Контекст:** Phase 1-4 аудита завершены, артефакты в `irrigation-audit/`. Решения владельца зафиксированы (см. блок ниже).

---

## Как использовать

1. Открой новый чат с Tony в авторежиме (не resume).
2. Скопируй блок ниже целиком (от `ЗАДАЧА:` до `ОБЯЗАТЕЛЬНО спроси владельца.`).
3. Отправь.
4. Tony соберёт команду, начнёт с **Phase 0 (branch consolidation)** и попросит подтверждение перед force-операцией в `main`.

---

## Промпт

```
ЗАДАЧА: Полное закрытие Phase 5 аудита wb-irrigation.

КОНТЕКСТ
========
Команда уже выполнила end-to-end аудит проекта wb-irrigation (Python/Flask/SQLite/MQTT, прод на WirenBoard 10.2.5.244).
Аудит лежит в репо https://github.com/Raul-1996/irrigation, ветка refactor/v2, папка `irrigation-audit/`.
Главный документ — `irrigation-audit/reports/audit-report.md` (1588 строк, 32 master-points, 5 волн фиксов).
HTML-версия — `irrigation-audit/reports/audit-report.html`.

Локальный чекаут — `/opt/claude-agents/irrigation-v2/` на ветке refactor/v2, синхронен с origin (HEAD 227309b).
Параллельно есть устаревший `/opt/claude-agents/irrigation/` на ветке main (отстаёт на 184 коммита).

РЕШЕНИЯ ВЛАДЕЛЬЦА (уже зафиксированы, не переспрашивать)
========================================================
1. SSE — выпилить полностью, оставить polling 5 сек.
2. Cloudflare Access — НЕ трогаем (остаётся как есть).
3. Backup offsite — выносим из scope, владелец сделает позже сам.
4. Telegram bot — оставить в том же процессе с Flask.
5. CI/deploy после Phase 0 — переключить на main (см. Phase 0).
6. Деплой на прод (10.2.5.244) — агенты НЕ деплоят сами; готовят PR + новый update_server.sh + rollback procedure, владелец деплоит вручную после явного "ок" в чате.
7. Pace — последовательный: волна за волной, после каждой — пауза для review владельца.

ЦЕЛЬ
====
Закрыть все CRITICAL + HIGH + MEDIUM + LOW из аудита (~32 master-points + 5 physical-risk + 26 quick wins).
Стиль работы — pragmatic evolution, не big-bang. Каждое изменение — отдельная feature-ветка от main, отдельный PR, маленький и рецензируемый.

PHASE 0 — Branch consolidation (BLOCKING all subsequent work)
=============================================================
Задача:
- Смержить refactor/v2 в main так, чтобы дальше работа шла в main.
- Ветки разошлись на 184 коммита, 372 файла, +59k/-24k. Архитектура refactor/v2 — current truth.
- Девопс-агент должен выбрать безопасный путь: merge -X theirs, или branch reset, или PR с replace-content. Обосновать выбор.
- Что нужно сохранить: всю историю refactor/v2 (включая audit-коммиты).
- Что можно потерять: дивергентную историю main (она устарела и не используется).
- Защита: тег `pre-consolidation-main-2026-04-19` на текущий main HEAD до операций. Force-push только с `--force-with-lease`.
- После: refactor/v2 удалить (или оставить как тег `archive/refactor-v2`).
- Обновить ci.yml, deploy.yml, update_server.sh — branch триггер на main.
- Прод-сервер: НЕ перезапускать, владелец сам решит когда подтянуть.

Артефакт Phase 0:
- PR в main (или сразу force-pushed main, по решению девопса) с описанием стратегии и проверкой.
- Сообщение владельцу с deploy-instructions для WB (что и когда запустить руками).

PHASE 1 — Wave 1: Security + Physical Safety + Logging Fix (CRITICAL)
=====================================================================
Цель: закрыть всё, что может привести к физическому вреду или unauth-доступу.

Закрываем master-points из audit-report.md §4 CRITICAL и §3 PHYS:
- MASTER-C1, C2, C3, C4, C5, C6 (по списку отчёта)
- PHYS-1..PHYS-5
- Включая bug Phase 2 SRE: app.log = 0 байт 13 дней (root cause: 3 дефекта logging_setup + ранний basicConfig в irrigation_scheduler.py)
- Включая APScheduler MemoryJobStore → SQLAlchemyJobStore (jobs.db)
- Включая Mosquitto anonymous → user/password + ACL + bind 1883 на 127.0.0.1
- Включая SEC-001..010 цепочку (CSRF на API, session fixation, photo path traversal, weather SQL pattern, и т.д.)
- Включая CQ-001..004 NameError-бомбы (logger без import logging)
- Включая зону reconciliation (Command→State→Observation pattern из target-state.md §3)

Каждый фикс — отдельная feature-ветка от main: `fix/wave1-<short-name>`.
PR в main, описание ссылается на конкретный master-point из audit-report.md.
Тесты: либо новый pytest, либо обоснование почему невозможно.

Exit criteria Wave 1:
- 0 CRITICAL open
- 0 PHYS-* unaddressed
- app.log пишется (>100 строк/день)
- pytest зелёный

После Wave 1 — пауза для review владельца. Не запускать Wave 2 без явного "go".

PHASE 2 — Wave 2: Observability baseline
========================================
Цель: добавить минимально необходимую видимость в систему, чтобы дальше валидировать регрессии.

- Structured logs (JSON file handler с rotation)
- /healthz, /readyz endpoints (по target-state.md §6)
- /metrics (prometheus-client embedded, ~200 KB)
- correlation_id middleware
- systemd WatchdogSec=60 + sd_notify
- log retention через logrotate

Exit criteria Wave 2:
- /healthz возвращает 200 при здоровой системе, 503 при сломанной
- /metrics отдаёт минимум 10 системных метрик (mqtt_connected, scheduler_jobs_count, db_size_bytes, uptime_seconds, etc.)
- логи за последние 24 часа > 1000 строк, JSON parseable

PHASE 3 — Wave 3: Data integrity
================================
- PRAGMA foreign_keys=ON миграция (с пересозданием таблиц где нужны FK declarations)
- PRAGMA synchronous=NORMAL (вместо FULL)
- busy_timeout=30s
- Атомарность миграций db/migrations.py — каждая миграция в одной транзакции с записью в migrations table
- Все 8 прямых sqlite3.connect() переписать на BaseRepository._connect() (services/weather.py 7 мест + services/float_monitor.py 1 место + routes)
- Удаление зон/программ — каскадные FK или explicit cleanup в репозиториях
- ВНЕ scope: backup offsite (исключено владельцем)

Exit criteria Wave 3:
- PRAGMA foreign_keys возвращает 1 при PRAGMA foreign_keys check — пусто
- Все sqlite3.connect() поиском в коде → 0 за пределами BaseRepository
- pytest зелёный

PHASE 4 — Wave 4: Code quality / decomposition
==============================================
Цель: разобраться с God-модулями и dead code.

- services/weather.py (1404 LOC) → разбить по target-state.md §1 (api client / cache / decision / adjustment)
- irrigation_scheduler.py (1365 LOC) → объединить с scheduler/ пакетом, убрать 89-строчный дубль jobs (CQ-005)
- db/migrations.py (1084 LOC) → если возможно, разделить по доменам
- Удалить dead files: routes/system_api.py, routes/zones_api.py, services/scheduler_service.py, services/completion_tracker.py (все 0 импортов)
- Удалить dead code: ui_agent_demo.py, basic_auth_proxy.py (если действительно не используется), templates/programs_old.html
- Закрыть дублирование auth-логики в app.py (дубль services/security.py)
- Закрыть дублирование _is_status_action() в app.py:203,256
- Удалить 40+ unused imports из BUGS-REPORT
- Декомпозиция SSE → выпил полностью (sse_hub.py, всё связанное с SSE на клиенте)

Exit criteria Wave 4:
- pyflakes / ruff: 0 unused imports
- ни один файл > 600 LOC (где возможно)
- pytest зелёный, coverage не упал

PHASE 5 — Wave 5: Performance + Frontend + a11y + Tests
=======================================================
- API-01: /api/status пересоздаёт MQTT-клиент на каждый запрос → singleton
- Frontend dead JS (113 KB) → удалить
- WCAG fixes: contrast 2.85:1 → ≥ 4.5:1 (25+ мест), toggle-switch keyboard accessible, emergency-button keyboard
- Тесты: переписать XPASS (52 шт) на passing tests, убрать @xfail
- CI: добавить strict_xfail=true
- Параметризация дубль-тестов где разумно
- 26 quick wins из audit-report.md §7

Exit criteria Wave 5:
- WCAG Level A: 0 fails (axe или ручная проверка)
- pytest -v: 0 failures, 0 unexpected xpass
- Lighthouse performance ≥ 80 на мобиле

ОБЩИЕ ПРАВИЛА ДЛЯ ВСЕХ ВОЛН
============================
- Все PR в main, feature-ветки `fix/wave<N>-<short>`.
- НЕ менять прод-конфиги Mosquitto / cloudflared без явного "ок" владельца — готовить готовые конфиги в репо + инструкции в PR.
- Перед каждым PR — pytest локально (на /opt/claude-agents/irrigation-v2 или аналог после Phase 0).
- Описание PR обязательно ссылается на master-point из audit-report.md.
- Никаких --force, --no-verify (кроме Phase 0 force-with-lease если выбран reset-стратегия).
- Прод (10.2.5.244) — read-only до Wave 1 deploy-window, дальше владелец сам.

КОМАНДА (рекомендуемые агенты, можешь скорректировать)
=====================================================
- engineering-devops-automator (Phase 0 + CI/deploy + systemd + Mosquitto config)
- engineering-security-engineer (Wave 1 security)
- engineering-sre (Wave 1 logging fix + Wave 2 observability + reconciliation pattern)
- engineering-code-reviewer (Wave 1 NameError-бомбы + reviewer всех PR)
- engineering-database-optimizer (Wave 3)
- engineering-software-architect (Wave 4 decomposition oversight)
- engineering-backend-architect (Wave 4 SSE removal + scheduler consolidation)
- engineering-frontend-developer (Wave 5 frontend)
- testing-accessibility-auditor (Wave 5 a11y verification)
- testing-test-results-analyzer (Wave 5 tests fix)
- testing-performance-benchmarker (Wave 5 perf verification)

ВХОДНЫЕ АРТЕФАКТЫ
================
- Главный отчёт: https://github.com/Raul-1996/irrigation/blob/refactor/v2/irrigation-audit/reports/audit-report.md
- HTML: https://raw.githack.com/Raul-1996/irrigation/refactor/v2/irrigation-audit/reports/audit-report.html
- 8 findings: https://github.com/Raul-1996/irrigation/tree/refactor/v2/irrigation-audit/findings
- Architecture: https://github.com/Raul-1996/irrigation/tree/refactor/v2/irrigation-audit/architecture
- Локальный чекаут: /opt/claude-agents/irrigation-v2/ (refactor/v2) и /opt/claude-agents/irrigation/ (main, устарел)

ВАЖНО ПРОЦЕССНО
===============
- Phase 0 — обязательно ASK владельца перед force-push в main, даже если выбрана reset-стратегия.
- После каждой волны — пауза, summary владельцу в чат, ждать "go" на следующую.
- Если в ходе работы агент находит что-то новое и серьёзное (не из аудита) — отдельный finding, отдельный PR, не молча включать в текущий.
- Все коммиты — `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`.
- Конфиденциальные данные (mosquitto passwd, токены) — никогда в git, только в Vaultwarden + ссылка из README.

ВЫХОДНЫЕ АРТЕФАКТЫ
==================
- Phase 0: ветка main = бывший refactor/v2, теги, обновлённые ci/deploy скрипты, ARCHIVE-документ "Branch consolidation 2026-04-19" в репо.
- Wave 1-5: серия PR в main, каждый ссылается на master-point. Финальный merge каждой волны = exit criteria выполнен.
- Финал: PR-template с deploy-инструкцией для владельца (что выполнить на 10.2.5.244 руками: git pull, systemctl restart, smoke test endpoints).
- Обновить irrigation-audit/reports/audit-report.md финальной секцией "Closed in Phase 5: <list of master-points fixed, with PR links>".

СТАРТ
=====
Запусти Phase 0 ASAP. Перед force-операциями в main — ОБЯЗАТЕЛЬНО спроси владельца.
```
