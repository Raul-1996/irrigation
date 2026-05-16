# CLAUDE.md — wb-irrigation

Заметки для AI-агентов (Claude, Cursor и т.п.), работающих с этим репо.

## Python version

**Минимум: Python 3.11.**

Причины:
- В коде используется PEP 604 union-синтаксис (`X | None` вместо `Optional[X]`)
  везде, где есть тайп-хинты — это требует 3.10+.
- Используется `datetime.UTC` (импорт `from datetime import UTC`) —
  это требует 3.11+.

Что **нельзя** использовать (фичи 3.12+):
- `asyncio.TaskGroup`
- `tomllib` из стандартной библиотеки
- `except*` (exception groups)
- Generic type parameter syntax (`def f[T](x: T)`)

### Системный Python на Wirenboard

На целевой платформе (Wirenboard, Debian 11 aarch64) системный
интерпретатор — `python3.9.2`. Его НЕ обновляем — на нём держатся OS-сервисы.

Наше приложение запускается из отдельного venv, в котором лежит
изолированный Python 3.11, поставленный через **uv**
(`https://astral.sh/uv` — Astral, prebuilt `python-build-standalone`).

Путь venv на устройстве: `/opt/wb-irrigation/irrigation/venv`.
Bootstrap-скрипт для свежего контроллера: `install_wb.sh`.
Обновление существующей установки: `update_server.sh`.

## Ruff / линтеры

`[tool.ruff] target-version = "py311"` в `pyproject.toml`. Не понижать —
иначе ruff начнёт ругаться на PEP 604 и предлагать `Optional[X]`.

## Деплой

- WB (нативно, systemd) — единственный поддерживаемый продакшен-таргет.
- Docker — **не используется**. Все `*_docker.sh`, `Dockerfile`,
  `docker-compose.yml` оставлены для локальной разработки на десктопе,
  но на WB не применяются.

Service name: `wb-irrigation.service`.
Health check: `curl http://127.0.0.1:8080/readyz`.
