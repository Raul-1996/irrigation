# Irrigation Audit — wb-irrigation (refactor/v2)

End-to-end аудит проекта `wb-irrigation`, выполненный командой Claude-агентов под руководством Tony (team-lead).

**Целевая ветка:** `refactor/v2` (то, что развёрнуто на проде WB-Techpom 10.2.5.244).
**Старт:** 2026-04-19.
**Статус:** в работе.

## Структура

```
irrigation-audit/
├── README.md                  ← навигация и статус (этот файл)
├── landscape/                 ← Phase 1 — карта кода + прод-снапшот
├── findings/                  ← Phase 2 — отчёты по 8 направлениям
├── architecture/              ← Phase 3 — текущее и целевое состояние
├── reports/                   ← Phase 4 — единый сводный отчёт
└── roadmap/                   ← Phase 5 — план фиксов и PR
```

## Фазы

| # | Фаза | Агенты | Параллельно | Статус |
|---|------|--------|-------------|--------|
| 1 | Landscape — разведка кода + прод-снапшот | codebase-onboarding-engineer | нет | в работе |
| 2 | Глубокий аудит по слоям | security, code-reviewer, database, sre, perf, tests, frontend, a11y | да (8) | pending |
| 3 | Архитектурный синтез | software-architect, backend-architect | да (2) | pending |
| 4 | Сводный отчёт + приоритизация | incident-response-commander | нет | pending |
| 5 | Фиксы через feature-branches | security, code-reviewer, db, sre, devops | да (5) | pending |

## Принципы

1. **Прод (WB-Techpom 10.2.5.244) — read-only** на фазах 1-4. Никаких write-операций без явного approve.
2. **Все PR в Phase 5** — в feature-branches от `refactor/v2`. Прямого push в `refactor/v2` нет.
3. **Существующие отчёты** (`ARCHITECTURE-REPORT.md`, `BUGS-REPORT.md`, `EXPERT-ANALYSIS.html` в корне репо) — используются как baseline, верифицируются и расширяются, не дублируются.
4. **`main` ветка игнорируется** как устаревшая (отстаёт на 184 коммита).

## Контакты

Tony (team-lead) → Telegram-бот @Tony_teamlead_bot.
