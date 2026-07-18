# Прогресс аудита и исправлений wb-irrigation

Дата checkpoint: 2026-07-18. Ветка: `refactor/audit-2026-07`.

## Текущее состояние

- Фаза 1 (read-only verification) завершена.
- Исходные 138 находок: `VERIFIED_REAL` — 133, `VERIFIED_FALSE` — 5, `UNVERIFIED` — 0.
- Пять независимых diff-review линз завершены; добавлено 14 новых `VERIFIED_REAL` находок.
- Финальный реестр: 152 записи, из них 147 real и 5 false.
- 12 записей помечены `duplicate_of`; они сохранены для трассируемости и не являются отдельными корневыми дефектами.
- Производственный код и тесты в Фазе 1 не менялись.
- Полный pytest в Фазе 1 намеренно не запускался; переданный baseline остаётся `1872 passed, 2 skipped, 17 deselected`.
- Рабочее дерево до документационного checkpoint содержало только пользовательский untracked `AGENTS.md`; файл не менялся и не индексировался.

## Итоговые ложные находки

- #16 и #31: UTC/local mismatch находится в `water_usage`, но у таблицы нет production writer, а у endpoint нет bundled consumer.
- #77: удалённый SSR-путь был мёртв уже на `main`; заявленной регрессии ветки нет.
- #105: server timestamps нормализуются в ISO 8601 с offset до попадания в `status.js`.
- #126: реальный boot/recovery flow не даёт двойного запуска; он возникал только с искусственным микробарьером.

## Новые находки diff-review

- #139: `/api/status` создаёт до `Z×P` SQLite cancellation-запросов на один публичный GET.
- #140–#142: заглушки program log/stats, отсутствующее различие smart/time-based и ложное CronTrigger-покрытие.
- #143: невалидный `interval_days` способен дать секундный interval либо зациклить APScheduler.
- #144: удалён явно стабильный weather compatibility API.
- #145–#149: пять lifecycle/concurrency дефектов — orphan jobs, потерянный Stop, Telegram runner, ownerless cancel-event и поздний ON после shutdown OFF.
- #150–#152: stale weather divergence и два edge-case дефекта общего next-watering калькулятора.

## Очерёдность Фазы 2

1. В первую очередь: branch regressions, все high safety/security defects и исходные W1–W7.
2. Затем: medium defects, для которых исправление локально и не меняет продуктовую семантику.
3. После этого: low/test-quality fixes с минимальным риском.
4. Каждая группа правок — отдельный worktree, один владелец на файл, regression test до исправления, локальный commit без push.
5. Интеграция — строго по одной ветке с полным серийным pytest и `uvx ruff check .` после каждого merge.

Планируемые владельцы файлов:

- `db/programs.py` — weekday normalization, duplicate/conflict semantics.
- `routes/programs_api.py` + `templates/programs.html` — единый validation contract и UI success/XSS.
- `irrigation_scheduler.py` — lifecycle, group sequencing, recovery, emergency and persistent-job safety.
- `services/zone_control.py` + `services/observed_state.py` — command serialization, fault state, publish-result and verifier generation.
- `services/sse_hub.py` — lock/connect lifecycle, echo handling, TLS/master subscriptions and startup.
- `services/next_watering.py` — scheduler parity, bounded weather cache and query amplification.
- `services/mqtt_pub.py` + `routes/mqtt_api.py` — callbacks, client lifecycle, validation and secret response.
- отдельные frontend/web owners — `status.js`, `zones.js`, `mqtt.html`, auth/security and image paths.

## Осознанно отложенные категории

Финальное решение об отложении принимается после fix-веток, но заранее не планируется рискованный широкий рефакторинг:

- распил `irrigation_scheduler.py` и `static/js/status.js`;
- миграция уже сдвинутых weekday-значений (их происхождение неразличимо; нужен post-deploy UI review);
- изменение special semantics группы 999;
- включение shadow water-balance H2 в реальный полив;
- Docker как production target;
- e2e и `mqtt_real` без физического WB/брокера.

Низкоприоритетные условные сценарии (legacy Safari, ручной downgrade migration, crafted admin-only malformed payloads) могут остаться отложенными только с индивидуальной причиной в финальном inventory; один общий blanket-defer недопустим.

## Следующая безопасная точка

После этого документационного checkpoint можно начинать Фазу 2 локально. Перед первым `git push` требуется явное подтверждение пользователя: `PHASE1-RAW-REPORTS-2026-07-18.md` и inventory содержат подробные данные внутреннего аудита и будут экспортированы во внешний настроенный `origin`.
