# Финальный инвентарь аудита wb-irrigation

Фаза 1 завершена 2026-07-18: каждая исходная находка получила требуемые независимые роли, все расхождения разрешены оркестратором, пять линз diff-review завершены.

- Всего записей: **152** (138 исходных + 14 новых).
- `VERIFIED_REAL`: **147**.
- `VERIFIED_FALSE`: **5**.
- `UNVERIFIED`: **0**.
- `duplicate_of` означает подтверждённый дубль корневой причины; запись сохранена для трассируемости.
- Phase 2 disposition: **76 fixed · 2 deferred · 69 pending · 5 not_applicable**.

Подробные отчёты и оговорки: `PHASE1-RAW-REPORTS-2026-07-18.md`. Машиночитаемый источник: `findings-inventory.json`. Интеграционные пакеты: `PHASE2-PACKAGES.json`.

## Подтверждённые — 147

### #1 [medium] `services/sse_hub.py:422` — reload_hub: окно гонки со встречным ensure_hub_started даёт двойную обработку MQTT-сообщений

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ меняет topic зоны (PUT /api/zones/<id> → reload_hub), в этот же момент EventSource браузера переподключается (обычное авто-переподключение SSE) → ensure_hub_started стартует новые клиенты при ещё не остановленных старых. Пришедшее в окне ON/OFF-эхо реле обрабатывается дважды: по OFF оба _on_message берут один и тот же open zone_run (get_open_zone_run без zone_lock) и дважды вызывают finish_zone_run; по ON дважды выполняются cancel_zone_jobs + schedule_zone_stop; SSE-клиенты получают дубли событий.
- **Решение оркестратора:** Previous REFUTED status overturned. The race is conditional on a concurrent SSE start during hub shutdown and an MQTT message in the overlap, but both are reachable through the normal zones page.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Rebuild сериализован поколениями, registry и topic maps публикуются атомарно, а callback вытесненного клиента отклоняется как stale; перекрывающихся активных обработчиков одного server generation больше нет.
- **Пакет:** `phase2-sse-safety`.
- **Fix commit:** `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`.
- **Regression tests:** `tests/unit/test_phase2_sse_hub.py::test_stale_client_callback_is_terminal_after_reload`, `tests/unit/test_phase2_sse_hub.py::test_new_client_is_published_as_current_before_network_loop_starts`.

### #2 [medium] `services/sse_hub.py:430` — reload_hub из HTTP-хендлеров выполняет блокирующий client.connect() под глобальным _SSE_HUB_LOCK — стопорит broadcast/register_client и сами запросы

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** MQTT-брокер выключен/недоступен (типовая ситуация при отладке): сохранение зоны с изменённым topic висит ~5 с на server; одновременно нажатый «Стоп»/«Старт» другой зоны зависает внутри broadcast() (он ждёт _SSE_HUB_LOCK, у exclusive_start_zone — под group_lock, т.е. каскадно блокируется и группа), новые SSE-коннекты не устанавливаются. При нескольких MQTT-серверах задержка суммируется.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Connect/setup и terminal loop_stop выполняются асинхронным rebuild worker вне hub lock и HTTP request thread, поэтому broadcast/register и route save не ждут сетевой таймаут.
- **Пакет:** `phase2-sse-safety`.
- **Fix commit:** `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`.
- **Regression tests:** `tests/unit/test_phase2_sse_hub.py::test_connect_does_not_hold_hub_lock`, `tests/unit/test_phase2_sse_hub.py::test_reload_returns_while_terminal_client_shutdown_is_blocked`, `tests/unit/test_phase2_sse_hub.py::test_startup_prefers_nonblocking_paho_connect_async`.

### #3 [medium] `services/zone_control.py:568` — Снятие stop-джобов по подстроке без разделителя: рестарт зоны N удаляет zone_hard_stop чужих зон с id, начинающимся на N

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Зона 1 (группа A) включена, зона 12 (группа B) поливается с взведённым zone_hard_stop:12. Пользователь повторно жмёт «Старт» на зоне 1 → ветка reschedule удаляет job zone_hard_stop:12. Если штатный zone_stop зоны 12 не сработает (рестарт планировщика, misfire), watchdog-страховки больше нет — зона 12 льёт до zone_cap (240 мин) или бесконечно, если cap не взводился.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Already-on reschedule удаляет zone_stop по точному prefix с разделителем и zone_hard_stop только по полному ID, не затрагивая зоны с общим числовым префиксом.
- **Пакет:** `phase2-zone-state-safety`.
- **Fix commit:** `42142ddea539625fe948bdaeb358b338aff58cef`.
- **Regression tests:** `tests/unit/test_phase2_zone_state_safety.py::test_finding_3_restart_removes_only_exact_hard_stop`.

### #4 [medium] `services/sse_hub.py:268` — Гонка paho ON-эха с ручным override-стартом: _on_message затирает override-длительность базовой

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Зона с базовой длительностью 10 мин запускается с duration_percent=200 (override 20 мин). ON-эхо реле приходит через ~100 мс, до post-delegate планирования → в планировщике остаётся ранний 10-мин стоп — полив завершается вдвое раньше запрошенного. Обратный порядок (эхо позже, override 5 мин на базе 30) — стоп на 5 мин снят, зона льёт 30 мин. Существует и на main (тот же _on_message + тот же post-delegate порядок в api_zone_mqtt_start); ветка поведение не меняла, но добавила override-путь также в /start-зависимые эндпоинты.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** ON для уже активной зоны обновляет только observation и больше не отменяет/перевзводит override stop либо hard-stop; retained replay также не продлевает полив.
- **Пакет:** `phase2-sse-safety`.
- **Fix commit:** `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`.
- **Regression tests:** `tests/unit/test_phase2_sse_hub.py::test_active_retained_on_does_not_reset_override_or_hard_stop`.

### #5 [medium] `services/next_watering.py:113` — next-watering показывает полив от выключенной (enabled=0) программы — compute_next_watering не фильтрует enabled

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь выключает программу тумблером enabled → карточка зоны и /api/status продолжают показывать «следующий полив завтра 06:00», который планировщик никогда не выполнит; scheduled_start_time той же зоны (через db.compute_next_run_for_zone) при этом пуст — два эндпоинта противоречат друг другу. Новый тест-файл tests/api/test_next_watering_semantics.py кейс enabled=False не покрывает.
- **Disposition Фаз 2–3:** `pending`.

### #6 [low] `tests/unit/test_scheduler_comprehensive.py:410` — test_recover_even_odd_null_means_odd ассертит разное в зависимости от чётности дня месяца — по чётным дням не ловит потерю recovery NULL-программ

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Регрессия, при которой recover_missed_runs перестаёт восстанавливать even-odd программы с NULL (частичный revert d7161a5), уходит в main при прогоне CI в чётный день месяца; падение всплывает лишь при следующем прогоне в нечётный день — «мигающий» тест, который выглядит флейком.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Recovery-тест явно проверяет NULL even_odd как odd на нечётной дате и отсутствие запуска на чётной; реализация, всегда отбрасывающая NULL, больше не проходит.
- **Пакет:** `phase2-test-quality`.
- **Fix commit:** `9d65cb6f41894ca16712c86e4d54c2a197be8472`.
- **Regression tests:** `tests/unit/test_scheduler_comprehensive.py::TestRecoverMissedRuns::test_recover_even_odd_null_means_odd[odd-date]`, `tests/unit/test_scheduler_comprehensive.py::TestRecoverMissedRuns::test_recover_even_odd_null_means_odd[even-date]`.

### #7 [low] `tests/unit/test_scheduler_v2.py:163` — test_interval_program_first_run_today: xfail снят, но ассерт тавтологичен — заявленное «первый запуск СЕГОДНЯ» не проверяется

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Регрессия якорения IntervalTrigger (start_date смещается на interval_days вместо ближайшего 14:30) проходит тест — снятие xfail в 3f9444e создало видимость покрытия фичи «interval first run today», которого фактически нет.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** IntervalTrigger проверяется на точный пятидневный interval и start_date 14:30 сегодня либо завтра под фиксированным временем.
- **Пакет:** `phase2-test-quality`.
- **Fix commit:** `9d65cb6f41894ca16712c86e4d54c2a197be8472`.
- **Regression tests:** `tests/unit/test_scheduler_v2.py::TestScheduleIntervalProgram::test_interval_program_first_run_today[time-still-ahead]`, `tests/unit/test_scheduler_v2.py::TestScheduleIntervalProgram::test_interval_program_first_run_today[time-already-passed]`.

### #8 [low] `tests/unit/test_scheduler_comprehensive.py:332` — Новые recover-тесты флейкуют при пересечении полуночи: несогласованные выборки datetime.now() между setup и recover_missed_runs

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Ночной CI-прогон, стартующий за секунды до полуночи (или зависший на fixture setup через полночь), даёт ложное падение TestRecoverMissedRuns без какой-либо регрессии кода. Окно узкое (секунды), поэтому severity low, но паттерн «несколько несинхронизированных now() в одном тесте» присутствует во всех шести новых recover-тестах.
- **Решение оркестратора:** Previous REFUTED status overturned. The flake window is only the actual midnight crossing, not a ten-minute interval; impact is test reliability only.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Весь TestRecoverMissedRuns использует frozen dates; matching/wrong/NULL parity проверяются симметрично на чётной и нечётной датах без wall-clock drift.
- **Пакет:** `phase2-test-quality`.
- **Fix commit:** `9d65cb6f41894ca16712c86e4d54c2a197be8472`.
- **Regression tests:** `tests/unit/test_scheduler_comprehensive.py::TestRecoverMissedRuns::test_recover_even_odd_program_matching_parity[odd-date]`, `tests/unit/test_scheduler_comprehensive.py::TestRecoverMissedRuns::test_recover_even_odd_program_matching_parity[even-date]`, `tests/unit/test_scheduler_comprehensive.py::TestRecoverMissedRuns::test_recover_even_odd_program_wrong_parity[odd-date]`, `tests/unit/test_scheduler_comprehensive.py::TestRecoverMissedRuns::test_recover_even_odd_program_wrong_parity[even-date]`.

### #9 [low] `tests/integration/test_next_watering_weather_skip.py:52` — test_next_watering_returns_today_when_no_weather_skip ежедневно флейкует в окне 23:55–00:00

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Любой прогон полного сьюта, попадающий интеграционными тестами в интервал 23:55–00:00 локального времени, даёт ложное падение теста без регрессии кода.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Weather next-watering tests замораживают service time в полдень и требуют точный same-day slot 23:55 либо перенос за сегодня, исключая полуночную flaky границу.
- **Пакет:** `phase2-test-quality`.
- **Fix commit:** `9d65cb6f41894ca16712c86e4d54c2a197be8472`.
- **Regression tests:** `tests/integration/test_next_watering_weather_skip.py::test_next_watering_returns_today_when_no_weather_skip`, `tests/integration/test_next_watering_weather_skip.py::test_next_watering_pushes_past_today_when_weather_skip`, `tests/integration/test_next_watering_weather_skip.py::test_next_watering_bulk_pushes_past_today_when_weather_skip`.

### #10 [high] `db/programs.py:64` — Сдвиг дней недели на -1 при сохранении программы без понедельника (create_program/update_program)

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь в мастере программ выбирает Вт+Ср (payload days=[1,2]) → в БД сохраняется [0,1] → CronTrigger(day_of_week=0/1) поливает в Пн+Вт, на день раньше выбранного. Любая программа без понедельника систематически сдвигается на день назад.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Удалена неоднозначная legacy-эвристика 1-based дней из create/update; новые записи сохраняют переданные UI zero-based weekdays. Уже сохранённые строки намеренно не мигрируются.
- **Пакет:** `phase2-db-programs`.
- **Fix commit:** `ee453c30039997f7bf5ff74b671a96eea030b100`.
- **Regression tests:** `tests/unit/test_phase2_db_programs.py::test_create_program_preserves_zero_based_weekdays_without_monday`, `tests/unit/test_phase2_db_programs.py::test_update_program_preserves_zero_based_weekdays_without_monday`.

### #11 [medium] `services/next_watering.py:113` — compute_next_watering игнорирует program.enabled — выключенные программы показываются как «следующий полив»

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь выключает программу (enabled=0). Карточка зоны (/api/zones/<id>/next-watering), bulk-эндпоинт и next_start группы в /api/status продолжают показывать время старта этой программы, хотя полив не произойдёт; scheduled_start_time зоны (через compute_next_run_for_zone) при этом показывает другое значение или «Никогда».
- **Дубль:** `#5`.
- **Disposition Фаз 2–3:** `pending`.

### #12 [medium] `services/next_watering.py:141` — compute_next_watering игнорирует extra_times — расходится с планировщиком и compute_next_run_for_zone

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Программа time='06:00', extra_times=['18:00'], сейчас 12:00: планировщик польёт сегодня в 18:00 (джоб extra:0 существует), но /api/zones/<id>/next-watering и bulk показывают «завтра 06:00»; scheduled_start_time (через compute_next_run_for_zone) при этом показывает сегодня 18:00 — UI противоречит сам себе и реальному поведению.
- **Disposition Фаз 2–3:** `pending`.

### #13 [low] `services/next_watering.py:52` — Interval-программы в compute_next_watering аппроксимируются как «каждый день» — показ даты на 1..N-1 дней раньше фактического запуска

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Сервис перезапущен в Пн 05:00, программа interval_days=3, time='06:00' → реальные запуски Пн/Чт/Вс. Во Вт /api/zones/next-watering-bulk и next_start в /api/status показывают «Ср 06:00», хотя фактический следующий запуск — Чт 06:00: пользователь ждёт полив на день раньше реального.
- **Disposition Фаз 2–3:** `pending`.

### #14 [low] `db/zones.py:68` — Общий builder UPDATE zones молча переназначает group_id 0/NULL в группу 1 при любом обновлении зоны

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Зона создана с group_id=0 (импорт/API «без группы»). Любое последующее обновление — переименование, запись planned_end_time планировщиком, PUT /api/zones/<id> — молча переносит её в группу 1: зона начинает участвовать в последовательностях группы 1, peer-off и master-valve логике группы 1.
- **Решение оркестратора:** Previous REFUTED status overturned. Standard CSV import can create group_id=0 and a later ordinary zone update silently rewrites it to group 1.
- **Disposition Фаз 2–3:** `pending`.

### #15 [medium] `services/next_watering.py:113` — Единый калькулятор next-watering игнорирует флаг enabled программ — выключенные программы показывают время следующего полива

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь выключает программу (PUT /api/programs/<id> enabled=0 → планировщик отменяет её jobs). После этого GET /api/zones/<id>/next-watering и POST /api/zones/next-watering-bulk продолжают возвращать конкретное next_datetime по этой программе, а /api/status показывает next_start группы по ней же — UI обещает полив, который никогда не запустится (и наоборот скрывает реальное «Никогда»).
- **Дубль:** `#5`.
- **Disposition Фаз 2–3:** `pending`.

### #17 [medium] `routes/mqtt_api.py:98` — PUT /api/mqtt/servers/<id> возвращает расшифрованный сохранённый пароль брокера, который клиент не присылал

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ в UI редактирует у MQTT-сервера только имя/хост (форма шлёт объект из GET, где password='***') → PUT-ответ содержит реальный пароль брокера в открытом виде; он оседает в devtools/прокси-логах/любом клиенте, который сам секрет не знал. Фикс: маскировать password в ответе PUT так же, как в GET.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** POST и PUT всегда маскируют password в ответе; сохранённый расшифрованный секрет не покидает сервер даже при password-less partial update.
- **Пакет:** `phase2-mqtt-safety`.
- **Fix commit:** `7d750b49fe5612f52d6121bdb4c923f5e42df3ad`.
- **Regression tests:** `tests/api/test_phase2_mqtt_api.py::test_findings_17_69_create_and_partial_update_never_echo_saved_secret`.

### #18 [medium] `services/zone_control.py:544` — Повторный «Старт» уже работающей зоны больше не отменяет программу группы — ручное продление молча стирается планировщиком

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Программа поливает зону A (осталось 2 мин из 10). Садовник жмёт «Старт» по зоне A, чтобы вручную полить полные 10 мин → API отвечает 200 «Зона A запущена, остальные остановлены», planned_end_time = now+10мин. Через 2 мин тред программы выключает зону A и включает зону B — ручное продление потеряно без какого-либо сигнала пользователю (на main программа в этот момент отменялась и зона лилась полные 10 мин). Ответ роута start-zone «остальные остановлены» при этом ложный: peer-off в rescheduled-ветке тоже не выполняется.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Ручное продление отменяет активный group runner, сохраняет manual ownership и не позволяет старому auto-stop стереть новое расписание зоны.
- **Пакет:** `phase2-zone-state-safety`.
- **Fix commit:** `42142ddea539625fe948bdaeb358b338aff58cef`.
- **Regression tests:** `tests/unit/test_phase2_zone_state_safety.py::test_finding_18_restart_cancels_active_group_program`.

### #19 [low] `routes/programs_api.py:60` — PUT /api/programs/<id> не валидирует v2-поля — некорректный schedule_type молча превращает программу в неработающую

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** PUT /api/programs/5 {"schedule_type": "daily"} (опечатка клиента/скрипта) → 200 OK, программа сохранена,但 при перепланировании не получает jobs — полив по программе тихо прекращается без ошибок в API и логах. Существовало и на main (там валидации не было нигде); ветка закрыла дыру только для POST, декларировав это как цель (комментарий «другие значения молча приводят к программе без jobs»).
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** PUT валидирует эффективные v2-поля тем же helper, что POST, и отклоняет неизвестный schedule_type до сохранения.
- **Пакет:** `phase2-programs-web`.
- **Fix commit:** `d0a30b346c1f520b9368ca39d8b6f97b0b3c0e1c`.
- **Regression tests:** `tests/api/test_phase2_programs_web.py::TestPutValidationParity::test_put_rejects_invalid_schedule_type_without_persisting_it`.

### #20 [medium] `static/js/status.js:1818` — toggleZoneRun: при success:false от сервера не вызывается hideLoading — полноэкранный оверлей блокирует UI на 15 секунд

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь нажимает ⏹/▶ на карточке зоны, сервер отвечает success:false — это штатно: /api/zones/<id>/mqtt/start возвращает 400 при активной аварийной остановке, 404 для отсутствующей зоны, а rate_limit('mqtt_control', 10 req/60s) срабатывает при быстром переключении нескольких зон. Результат: тост с ошибкой показан, но полноэкранный спиннер остаётся и блокирует все элементы главной страницы управления поливом на 15 секунд.
- **Решение оркестратора:** The overlay remains for the 15-second safety timeout after a server-declared failure.
- **Disposition Фаз 2–3:** `pending`.

### #21 [medium] `static/js/status.js:1807` — Оптимистичные watering_start_time/planned_end_time пишутся как UTC-naive строка — на не-UTC клиентах таймер сразу показывает 00:00 и прогресс 100%

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Клиент в любой TZ восточнее UTC (прод — Wirenboard, RU: UTC+3..+12) запускает зону на 10 минут через карточку или run-popup. Оптимистичный planned_end_time получается в прошлом → _remain = max(0, ...) = 0 → таймер мгновенно рисует «00:00», прогресс-бар 100% вместо обратного отсчёта; в сайдбаре «Сейчас поливается» — «осталось 0:00». Исправляется только приходом серверных данных на следующем 5-секундном поллинге. В TZ западнее UTC — обратный эффект: таймер показывает лишние часы.
- **Решение оркестратора:** The effect is transient until the next 5-second poll, but is visible on non-controller time zones.
- **Disposition Фаз 2–3:** `pending`.

### #22 [medium] `static/js/status.js:90` — loadZonesData глушит ошибки fetch в пустой массив: при недоступном сервере страница показывает «Зоны не найдены» и прячет баннер потери связи

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пропадает связь с контроллером (Wi-Fi/рестарт сервиса) или истекает сессия (401 → HTML → json() падает). Вместо баннера «Нет связи с сервером» и последних известных данных пользователь видит пустой список «Зоны не найдены» с нулевой статистикой — выглядит как потеря конфигурации; баннер связи скрыт или мигает.
- **Disposition Фаз 2–3:** `pending`.

### #23 [low] `static/js/status.js:1975` — saveZoneEdit игнорирует success:false — при отклонённом сервером сохранении показывается «✅ Зона сохранена»

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь в шторке редактирования стирает название зоны и жмёт «Сохранить». Сервер отвечает 400 «name must be non-empty», но UI закрывает шторку и показывает «✅ Зона сохранена»; после loadZonesData старое имя молча возвращается — изменение потеряно без какого-либо сообщения об ошибке.
- **Disposition Фаз 2–3:** `pending`.

### #24 [low] `static/js/zones.js:187` — zones.js: имя MQTT-сервера вставляется в innerHTML без escapeHtml в трёх модалках настроек группы — stored XSS

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Администратор сохраняет MQTT-сервер с именем `</option></select><img src=x onerror=alert(document.cookie)>` на странице /mqtt. При каждом открытии /zones разметка вырывается из <option> и скрипт исполняется в сессии любого администратора, открывшего страницу (HTML внутри <option> не отображается, но парсером обрабатывается при закрывающих тегах).
- **Disposition Фаз 2–3:** `pending`.

### #25 [low] `static/js/status.js:1626` — zone.icon и производный label вставляются в innerHTML без экранирования (status.js карточки зон, zones.js таблица) — stored XSS через API/CSV-импорт

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Через CSV-импорт зон (колонка icon = `<img src=x onerror=...>`) или прямой PUT /api/zones значение сохраняется в БД. Карточки зон на главной странице рендерятся для ВСЕХ пользователей, включая роль «наблюдатель», — внедрённый скрипт исполняется в их сессиях при каждом открытии и каждые 5 секунд при перерисовке.
- **Disposition Фаз 2–3:** `pending`.

### #26 [low] `static/js/status.js:2137` — initDialDrag навешивает новые document-обработчики (в т.ч. non-passive touchmove) при каждом открытии run-popup — утечка слушателей на долгоживущей странице

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Страница статуса — долгоживущая (настенный планшет). После N запусков полива через попап каждое движение пальца/мыши по странице проходит через N touchmove-обработчиков с passive:false, что блокирует композитный скролл на мобильных и постепенно деградирует отзывчивость; после десятков открытий скролл списка зон становится заметно дёрганым.
- **Disposition Фаз 2–3:** `pending`.

### #27 [low] `static/js/status.js:265` — env-пробник перезапускается каждым 5-секундным обновлением статуса — вечный опрос /api/env раз в секунду вместо заявленных «до 10 попыток»

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Датчик температуры/влажности включён в настройках, но физически офлайн (обрыв, MQTT-топик молчит). Каждая открытая вкладка страницы статуса бессрочно шлёт запрос /api/env каждую секунду (плюс штатный поллинг) — лишняя нагрузка на слабый ARM-контроллер Wirenboard, особенно с несколькими открытыми клиентами.
- **Disposition Фаз 2–3:** `pending`.

### #28 [medium] `services/next_watering.py:141` — compute_next_watering игнорирует extra_times — карточки показывают более поздний слот, чем реально польёт планировщик

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Программа 06:00 с extra_times ['19:00']. В 12:00 карточка зоны и /api/status показывают «следующий полив завтра 06:00», планировщик включает полив сегодня в 19:00 — для пользователя выглядит как самопроизвольный запуск.
- **Дубль:** `#12`.
- **Disposition Фаз 2–3:** `pending`.

### #29 [low] `services/next_watering.py:229` — Регрессия: отменённый на сегодня запуск снова показывается для зоны с postpone, истекающим между стартом программы и слотом зоны

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь отменил сегодняшний запуск программы для группы, у одной зоны стоит отложка, истекающая после старта программы, но до слота зоны → карточка этой зоны показывает сегодняшнее время полива, который не состоится; остальные зоны группы показывают завтра.
- **Решение оркестратора:** Real but exact same-day postpone requires API/integration input; normal +1/+2/+3 UI postpones to end of day.
- **Disposition Фаз 2–3:** `pending`.

### #30 [low] `irrigation_scheduler.py:2293` — recover_missed_runs не восстанавливает запуск, прерванный переходом через полночь

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Программа пн 23:30, 3 зоны по 20 мин. Отключение питания в 23:50, восстановление во вт 00:10 — окно запуска ещё активно (до 00:30), но recovery молча пропускает программу: зоны 2–3 не политы, в логах нет 'Recovery'.
- **Решение оркестратора:** Previous REFUTED status overturned. A graceful restart after midnight loses a still-running program whose scheduled start belongs to the previous local date.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Recovery рассматривает текущую и предыдущую локальную дату, поэтому окно программы, пересёкшее полночь, восстанавливает правильный хвост.
- **Пакет:** `phase2-scheduler-safety`.
- **Fix commit:** `a91116a14fea7e6df07bb2869b0ae726fe562edd`.
- **Regression tests:** `tests/unit/test_phase2_scheduler_safety.py::test_30_recovery_considers_previous_local_date_when_window_crosses_midnight`.

### #32 [low] `services/next_watering.py:147` — Программа, идущая через полночь, невидима для in-progress-логики next-watering — зоны показывают следующую неделю вместо ближайшего слота

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Программа только по понедельникам в 23:50, зоны 1(30м) и 2(30м). Во вторник 00:10, пока зона 1 ещё поливается, карточка зоны 2 показывает «следующий полив: следующий понедельник 00:20», хотя её полив начнётся через 10 минут.
- **Disposition Фаз 2–3:** `pending`.

### #33 [high] `services/sse_hub.py:268` — SSE-hub по эху 'on' снимает все стоп-джобы зоны и перепланирует автостоп на базовую длительность (теряется override и hard-stop)

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** 1) Пользователь стартует зону с override 2 мин (start_zone_orchestrated планирует stop@2мин + hard_stop@2мин ПОСЛЕ возврата exclusive_start_zone, т.е. после параллельного peer-off с QoS2-ожиданиями). Если MQTT-эхо '1' обрабатывается тредом paho ПОЗЖЕ этого планирования (эхо-латентность против длительности peer-off — гонка 50/50), cancel_zone_jobs удаляет и override-stop, и hard-stop, а взамен ставится stop на базовые 30 мин → зона льёт 30 мин вместо 2. 2) Детерминированный вариант: реконнект хаба к брокеру посреди полива (рестарт mosquitto, wifi-blip) — on_connect пересабскрайбит топики, брокер доставляет retained '1' → таймер стопа сбрасывается на ПОЛНУЮ базовую длительность от текущего момента, hard-stop удаляется навсегда → полив продлевается вплоть до удвоения, страховка остаётся только у cap-watchdog (240 мин).
- **Дубль:** `#4`.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** ON для уже активной зоны обновляет только observation и больше не отменяет/перевзводит override stop либо hard-stop; retained replay также не продлевает полив.
- **Пакет:** `phase2-sse-safety`.
- **Fix commit:** `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`.
- **Regression tests:** `tests/unit/test_phase2_sse_hub.py::test_active_retained_on_does_not_reset_override_or_hard_stop`.

### #34 [medium] `services/zone_control.py:298` — Гонка двойного старта: ветка 'on/starting → pass' в exclusive_start_zone всё равно создаёт второй открытый zone_run

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Двойной клик «Старт» → два POST /api/zones/<id>/start. Оба читают state='off' (до того как первый записал 'starting') → оба делегируют в exclusive_start_zone. Первый под group_lock стартует зону и открывает zone_run №1; второй, дождавшись лока, видит cur_state='on' → pass → создаёт ВТОРОЙ открытый zone_run №2 и повторно публикует '1'. При стопе get_open_zone_run (ORDER BY id DESC) закрывает только №2; №1 остаётся открытым, и следующий же observed-off в sse_hub (или idempotent-ветка stop_zone) закрывает его как status='ok' с фиктивной длительностью — фантомный полив в истории; иначе строка висит до ребута.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Повторный start под group serialization возвращает существующий результат и не создаёт второй open zone_run.
- **Пакет:** `phase2-zone-state-safety`.
- **Fix commit:** `42142ddea539625fe948bdaeb358b338aff58cef`.
- **Regression tests:** `tests/unit/test_phase2_zone_state_safety.py::test_finding_34_repeated_exclusive_start_has_one_open_run`.

### #35 [medium] `services/zone_control.py:793` — stop_zone не сериализован с exclusive_start_zone: проверка state по снапшоту до лока, group_lock не берётся, publish вне локов

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Зависший zone_hard_stop-джоб предыдущего запуска уже выполняет stop_zone(force=True), когда пользователь стартует ту же зону: cancel_group_jobs не может снять уже исполняющийся джоб. Интерливинг: старт (под group_lock) создаёт zone_run, публикует '1', пишет state='on'; параллельно stop публикует '0' (порядок '0' vs '1' на брокере недетерминирован), затем его _finalize_water_stats находит СВЕЖЕсозданный open run нового старта и закрывает его status='ok' с ~нулевой длительностью, после чего пишет state='off'. Итог: реле может остаться в состоянии, противоположном БД (relay ON / DB off — вода течёт при «выключенной» зоне), run нового полива уже закрыт, а самовосстановление через SSE-hub не работает, если ни один SSE-клиент ни разу не подключался (хаб стартует лениво из ensure_hub_started в роуте SSE).
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Вся OFF-транзакция stop_zone, включая publish и финальный state, выполняется под тем же group lock, что start, исключая последний физический ON при DB off.
- **Пакет:** `phase2-zone-state-safety`.
- **Fix commit:** `42142ddea539625fe948bdaeb358b338aff58cef`.
- **Regression tests:** `tests/unit/test_phase2_zone_state_safety.py::test_finding_35_stop_holds_group_serialization_until_off_publish_returns`.

### #36 [medium] `services/zone_control.py:134` — TOCTOU в _do_close мастер-клапана + неатомарный pop/insert таймеров: закрытие мастера возможно сразу после открытия при старте зоны

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Зона A остановлена → таймер close на 60с. На ~60-й секунде пользователь стартует зону B той же группы: _do_close успевает прочитать состояния (все off) ДО записи state='starting', а его publish close ложится на брокер ПОСЛЕ publish open из exclusive_start_zone. Мастер закрыт, зона B «поливает» при перекрытой магистрали — воды нет, но zone_run завершится как 'ok' и history зафиксирует полив. Окно = длительность DB-сканирования+publish (десятки-сотни мс на WB-железе). Осиротевший таймер расширяет окно: он не отменяется ни новым stop_zone, ни emergency_stop_all (Phase C чистит только запись в dict).
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Master close scan+publish сериализованы по master topic, open отменяет pending close, а замена timer регистрируется и запускается одной атомарной операцией.
- **Пакет:** `phase2-zone-state-safety`.
- **Fix commit:** `42142ddea539625fe948bdaeb358b338aff58cef`.
- **Regression tests:** `tests/unit/test_phase2_zone_state_safety.py::test_finding_36_master_open_cancels_pending_close`, `tests/unit/test_phase2_zone_state_safety.py::test_finding_36_concurrent_master_schedules_leave_one_live_timer`, `tests/unit/test_phase2_zone_state_safety.py::test_finding_36_firing_close_finishes_before_concurrent_master_open`.

### #37 [medium] `db/zones.py:326` — update_zone_versioned не реализует optimistic lock: version читается в той же транзакции, WHERE version=? не может не совпасть; fallback в zones_state дожимает запись при любой ошибке

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Любая пара конкурирующих писателей state (watchdog_cap stop vs ручной рестарт, peer_off vs mqtt_observed_change из sse_hub) завершается победой хронологически последней записи независимо от того, чьё представление о версии/состоянии устарело — «versioned» механизм не отсекает ни одну из гонок из находок выше, хотя модульные комментарии (zones_state.py: «eliminates the TOCTOU race», «optimistic-lock version match») обещают обратное. Конкретно: stop_zone завис между publish '0' и финальной записью 'off'; за это время sse_hub по эху записал 'off' и обнулил watering_start_time, затем пришёл рестарт и записал 'on' + start_time; поздняя запись stop_zone 'off'+start_time=None перетирает активный старт — зона физически включена ('1' переиздан рестартом), в БД 'off'.
- **Disposition Фаз 2–3:** `pending`.

### #38 [medium] `services/app_init.py:137` — _boot_sync: stop_all_in_group(force=True) закрывает «крашнутые» открытые zone_runs как status='ok' ДО mop-up'а в 'aborted'

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Отключение питания посреди полива (зона state='on', zone_run открыт). После ребута _boot_sync «останавливает» зону и записывает прерванный run как успешный полив, завершившийся во время загрузки — даже если краш был за 10 часов до этого. get_last_watering_time (MAX(end_utc) WHERE status='ok') и карточка зоны показывают ложное «последний полив: <время ребута>»; в истории прерванный полив неотличим от полноценного; данные о литрах/расходе фиктивны.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** _boot_sync сначала атомарно закрывает crash-open zone_runs как aborted с end_utc, и лишь затем вызывает обычные force-stop paths; stale run больше не может стать ok.
- **Пакет:** `phase2-app-lifecycle`.
- **Fix commit:** `dcd0e30ecea830f3e09968c40e50ca79da9c0875`.
- **Regression tests:** `tests/unit/test_phase2_app_lifecycle.py::test_boot_aborts_crash_open_run_before_forced_zone_stop`.

### #39 [medium] `services/mqtt_pub.py:105` — Блокирующий cl.connect() под глобальным _MQTT_CLIENTS_LOCK: мёртвый брокер задерживает OFF-команды на здоровый брокер

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Конфигурация с двумя MQTT-серверами (например, локальный WB + удалённый брокер), удалённый недоступен. StateVerifier/мастер-клапан ретраит publish на мёртвый сервер → тред висит в connect() под _MQTT_CLIENTS_LOCK. В этот момент срабатывает автостоп/watchdog_cap зоны на ЖИВОМ брокере: publish_mqtt_value → get_or_create_mqtt_client → ожидание глобального лока → команда OFF реле задерживается на ~5-10с на каждый цикл ретраев чужого сервера; при систематических ретраях (verify_async каждые старт/стоп) задержки постоянные.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Создание клиента сериализуется отдельным lock на server_id, а connect/TLS/loop_start выполняются без глобального registry lock; недоступный брокер не задерживает другой сервер.
- **Пакет:** `phase2-mqtt-safety`.
- **Fix commit:** `7d750b49fe5612f52d6121bdb4c923f5e42df3ad`.
- **Regression tests:** `tests/unit/test_phase2_mqtt_safety.py::test_finding_39_dead_broker_connect_does_not_hold_global_client_lock`.

### #40 [low] `services/sse_hub.py:381` — reload_hub выполняет пересборку хаба (blocking connect всех клиентов под _SSE_HUB_LOCK) в треде HTTP-запроса; на время пересборки теряются эхо реле

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ сохраняет зону, у которой mqtt-сервер недоступен (или брокер временно лежит): ensure_hub_started висит в connect() до сокет-таймаута на каждый сервер, всё это время под _SSE_HUB_LOCK → параллельные stop_zone/emergency-треды блокируются в broadcast() на секунды, SSE-запросы браузеров зависают на register_client. Если в момент reload_hub шёл полив и реле прислало эхо '0'/'1' в окно «клиенты остановлены → подписки ещё не восстановлены», хаб его не увидит: observed_state не обновится, mark_zone_run_confirmed не вызовется, открытый run не закроется по observed-off.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Reload возвращает сразу, переиспользует живое соединение при неизменных credentials и меняет subscriptions без teardown-gap либо повторной подписки на retained topic.
- **Пакет:** `phase2-sse-safety`.
- **Fix commit:** `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`.
- **Regression tests:** `tests/unit/test_phase2_sse_hub.py::test_reload_reuses_connection_and_unsubscribes_removed_topic`, `tests/unit/test_phase2_sse_hub.py::test_unchanged_reload_does_not_resubscribe_retained_topics`.

### #41 [high] `db/programs.py:122` — JSON-null в programs.zones/extra_times принимается без валидации и валит старт всего сервиса

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** PUT /api/programs/<id> с {"zones": null, "time": "06:00", "days": [0]} → 200 OK (route не валидирует, check_program_conflicts при zones=None возвращает []), яд сохранён в БД → при следующем рестарте контроллера импорт app.py падает с TypeError → hypercorn не стартует, весь полив и веб-интерфейс мертвы до ручной правки БД. То же с {"extra_times": null} (enumerate(None) в schedule_program:1200). Также ломается compute_next_run_for_zone (`zone_id not in None` → TypeError, подтверждено скриптом).
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Repository отклоняет zones/extra_times со значением JSON null до INSERT/UPDATE, поэтому отравленная программа не сохраняется и не ломает последующий startup.
- **Пакет:** `phase2-db-programs`.
- **Fix commit:** `ee453c30039997f7bf5ff74b671a96eea030b100`.
- **Regression tests:** `tests/unit/test_phase2_db_programs.py::test_create_program_rejects_json_null_list_fields[zones]`, `tests/unit/test_phase2_db_programs.py::test_create_program_rejects_json_null_list_fields[extra_times]`, `tests/unit/test_phase2_db_programs.py::test_update_program_rejects_json_null_list_fields_atomically[zones]`, `tests/unit/test_phase2_db_programs.py::test_update_program_rejects_json_null_list_fields_atomically[extra_times]`.

### #42 [medium] `db/zones.py:68` — update_zone: int()-касты в новом _build_zone_update_fields бросают непойманный ValueError/TypeError — 500 вместо контрактного None

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** PUT /api/zones/5 {"group_id": "abc"} (или group_id-объект/список от кривого клиента) → необработанный ValueError → HTTP 500 вместо 4xx/контрактного None; docstring/контракт update_zone «None при ошибке» нарушен для всех ~15 внутренних вызовов (scheduler, sse_hub, zone_control ловят только sqlite3.Error/OSError). На main тот же вход молча писал текст в INTEGER-колонку — ветка сменила режим отказа на исключение, не добавив обработку ни в одном вызывающем.
- **Disposition Фаз 2–3:** `pending`.

### #43 [medium] `db/zones.py:414` — bulk_upsert_zones: одна строка с нечисловым/null duration откатывает ВЕСЬ импорт с 500 вместо инкремента failed

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** CSV-импорт зон: фронтенд (static/js/zones.js:1901) делает parseInt(duration) → NaN для «10 мин»/пустых ячеек → JSON null → int(None) TypeError → весь импорт из N зон откачен, пользователь получает 500 «Ошибка импорта» без указания проблемной строки, счётчики created/updated/failed не возвращаются.
- **Disposition Фаз 2–3:** `pending`.

### #44 [medium] `db/zones.py:323` — update_zone_versioned: `except sqlite3.Error: pass` вокруг BEGIN IMMEDIATE глотает lock-ошибку — TOCTOU-защита молча отключается, retry_on_busy не срабатывает

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Конкурентная запись (WAL checkpoint, bulk-импорт) держит лок дольше busy_timeout → services.zones_state.update_zone_state получает (False, None): смена состояния зоны молча не применяется и не ретраится; при коротком контеншене — prev_zone читается вне транзакции и audit-переход может дублироваться/врать.
- **Disposition Фаз 2–3:** `pending`.

### #45 [medium] `db/base.py:21` — retry_on_busy — мёртвый код во всех репозиториях: внутренние except sqlite3.Error глотают BUSY до декоратора

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Лок держится дольше busy_timeout=30s (checkpoint + бэкап на медленной SD Wirenboard): update_zone возвращает None → routes/zones_crud_api.py:144 отвечает «Zone not found» 404 по существующей зоне; finish_zone_run возвращает False → zone_run навсегда остаётся открытым (end_utc NULL), история и last_watering_time теряют полив. Ретраев, обещанных декоратором, не происходит ни разу.
- **Disposition Фаз 2–3:** `pending`.

### #46 [low] `db/zones.py:272` — update_zone: исключение зоны из программ при переводе в группу 999 не срабатывает для строкового group_id

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Клиент/интеграция передаёт group_id строкой (Telegram-бот, curl, сторонний скрипт): зона попадает в служебную группу 999 «БЕЗ ПОЛИВА», но остаётся во всех программах → планировщик продолжает открывать её клапан по расписанию, хотя UI показывает зону как исключённую из полива.
- **Disposition Фаз 2–3:** `pending`.

### #47 [low] `db/zones.py:836` — compute_next_run_for_zone: для interval-программ с interval_days>1 пишется заведомо неверный scheduled_start_time

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Программа «каждые 3 дня в 06:00», сервис работает 2-е сутки: reschedule_group_to_next_program записывает в zones.scheduled_start_time «завтра 06:00», UI обещает полив завтра, фактический запуск — через 2 дня. Пользователь принимает решения (отложка/ручной запуск) по ложному времени.
- **Disposition Фаз 2–3:** `pending`.

### #48 [low] `db/audit.py:100` — get_audit_logs/get_logs: фильтры дат сравниваются с UTC-временем, а отображение — localtime (сдвиг на TZ-офсет)

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Контроллер в UTC+3, фильтр журнала аудита «с 2026-07-18 по 2026-07-18»: события 00:00–02:59 локального 18-го (UTC 17-е 21:00–23:59) не попадают в выборку, а события 00:00–02:59 локального 19-го попадают — страница аудита и /api/logs показывают неполные/чужие сутки.
- **Disposition Фаз 2–3:** `pending`.

### #49 [high] `services/mqtt_pub.py:120` — Публикующий MQTT-клиент: on_disconnect с сигнатурой VERSION1 при CallbackAPIVersion.VERSION2 — TypeError убивает сетевой тред paho, авто-reconnect мёртв

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Рестарт mosquitto / обрыв Wi-Fi → брокер рвёт TCP → paho зовёт on_disconnect → TypeError → сетевой тред умирает, авто-reconnect (reconnect_delay_set 1..5с) не срабатывает. Если обрыв случился во время _publish_with_retries: cl.reconnect() (стр. 163) восстанавливает сокет, но обрабатывать входящие PUBACK/PUBCOMP некому → publish rc=0, wait_for_publish молча таймаутится → ~22с задержки (backoff 1+2+4 + 3×5с wait) на каждый топик, затем инвалидация клиента и пересоздание; команда зоны ON/OFF может вернуть False и попасть в audit как mqtt_publish_failure, хотя брокер жив. Каждый дисконнект = спам «Caught exception in on_disconnect» + деградация доставки до следующего publish.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** on_disconnect принимает полный пятиаргументный Paho CallbackAPIVersion.VERSION2 контракт.
- **Пакет:** `phase2-mqtt-safety`.
- **Fix commit:** `7d750b49fe5612f52d6121bdb4c923f5e42df3ad`.
- **Regression tests:** `tests/unit/test_phase2_mqtt_safety.py::test_finding_49_disconnect_callback_accepts_paho_v2_signature`.

### #50 [high] `services/observed_state.py:199` — Observed-state верификация — тавтология: приложение само пишет retained-значение в report-топик, который затем читает верификатор («ложное подтверждение» реле)

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** RS-485 модуль реле физически отключён (или wb-mqtt-serial упал). Пользователь стартует зону: publish '1' уходит на брокер, реле молчит. verify('on') подписывается на базовый топик, получает retained '1', записанный самим приложением → «confirmed on attempt 1», mark_zone_run_confirmed фиксирует «физически подтверждённый» полив. _record_fault недостижим: fault_count не растёт, state='fault' не ставится, Telegram-алерт не уходит — весь контракт PHYS-1 (стр. 235-254) мёртв. История показывает успешные поливы, грядки сухие.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** P05 сделал command-scoped verifier единственным владельцем physical confirmation, а P06 удалил hub-side подтверждение по неразличимому live base-topic ON; retained/self-publish больше не создают тавтологическое подтверждение.
- **Пакет:** `phase2-sse-safety`.
- **Fix commit:** `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`.
- **Regression tests:** `tests/unit/test_phase2_sse_hub.py::test_live_base_topic_on_does_not_self_confirm_physical_watering`, `tests/unit/test_phase2_zone_state_safety.py::test_finding_64_stale_verifier_does_not_republish_or_fault`.

### #51 [high] `services/sse_hub.py:268` — sse_hub._on_message на каждом наблюдаемом 'on' безусловно отменяет и перевзводит автостоп на БАЗОВУЮ длительность — override-длительность затирается, retained '1' при реконнекте продлевает полив

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** (a) Зона с базовой длительностью 10 мин стартуется через POST /api/zones/N/mqtt/start с duration=30. start_zone_orchestrated взводит стоп на +30 мин, но эхо '1' (loopback собственной публикации) заставляет хаб сделать cancel_zone_jobs + schedule_zone_stop(10): либо 30-мин job отменён и остался 10-мин, либо оба живы и первый (10 мин) срабатывает раньше, либо новый job молча выпал по ConflictingIdError — во всех раскладах полив останавливается через 10 мин вместо 30, hard-stop при этом снят и не перевзведён. (b) Обрыв/реконнект MQTT хаба (или reload_hub при правке зоны) на 55-й минуте 60-минутного полива: retained '1' передоставляется при resubscribe → стоп перевзводится на +60 мин от текущего момента → зона льёт до 115 мин (ограничено только zone_cap_stop 240 мин, который для программных запусков может отсутствовать).
- **Дубль:** `#4`.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** ON для уже активной зоны обновляет только observation и больше не отменяет/перевзводит override stop либо hard-stop; retained replay также не продлевает полив.
- **Пакет:** `phase2-sse-safety`.
- **Fix commit:** `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`.
- **Regression tests:** `tests/unit/test_phase2_sse_hub.py::test_active_retained_on_does_not_reset_override_or_hard_stop`.

### #52 [medium] `utils.py:154` — decrypt_secret: при смене/утере ключа AES-ветка бросает ValueError('MAC check failed') — рушатся get_mqtt_server, все publish-пути и список серверов в API

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Сервис запущен с другим WorkingDirectory (правка systemd-юнита, запуск из другого каталога, перенос БД на новый контроллер без файла ключа) → тихо создаётся НОВЫЙ ключ → все пароли ENC: в mqtt_servers перестают расшифровываться. Итог: GET /api/mqtt/servers и /api/mqtt/servers/<id> отдают 500 (except в routes/mqtt_api.py:45 не ловит ValueError) — страница настроек MQTT недоступна; exclusive_start_zone/stop_zone падают по ValueError (старт возвращает failed/500); SSE-хаб не может стартовать. Управление поливом полностью теряется без единого понятного сообщения о причине.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** P07 ввёл typed secret failure и безопасные API/publisher boundaries; P06 изолирует нечитаемый server record, продолжает запуск остальных серверов и сохраняет уже живой client при неуспешном reload.
- **Пакет:** `phase2-sse-safety`.
- **Fix commit:** `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`.
- **Regression tests:** `tests/unit/test_phase2_sse_hub.py::test_unreadable_server_credential_does_not_abort_other_server_startup`, `tests/unit/test_phase2_sse_hub.py::test_unreadable_credential_during_reload_preserves_live_client`.

### #53 [medium] `services/mqtt_pub.py:50` — Правка MQTT-сервера (host/port/пароль) не инвалидирует ни кэш клиентов публикатора, ни подписки SSE-хаба — команды продолжают уходить на старый брокер до рестарта

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Брокер переезжает на новый хост (или админ исправляет опечатку в host, указывавшую на другой живой брокер): админ обновляет сервер через UI → API отвечает success, но кэшированный клиент остаётся подключён к СТАРОМУ брокеру (is_connected()==True) и все команды зон/мастер-клапана уходят туда с подтверждением доставки qos=2; SSE-хаб продолжает слушать эхо на старом брокере. Реальные реле на новом брокере команд не получают, при этом ни одной ошибки в логах — до полного рестарта сервиса.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Публичная invalidation boundary очищает publisher client/server/debounce caches, а MQTT PUT/DELETE инвалидирует publisher и инициирует безопасный hub reload.
- **Пакет:** `phase2-mqtt-safety`.
- **Fix commit:** `7d750b49fe5612f52d6121bdb4c923f5e42df3ad`.
- **Regression tests:** `tests/unit/test_phase2_mqtt_safety.py::test_finding_53_public_invalidation_clears_all_publisher_caches`, `tests/api/test_phase2_mqtt_api.py::test_finding_53_update_invalidates_publisher_and_reloads_hub`.

### #54 [medium] `services/sse_hub.py:168` — SSE-хаб не создаёт MQTT-клиента для сервера, на котором есть только топик мастер-клапана (без зон) — master_valve_observed никогда не обновляется

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Группа настроена так, что реле зон висят на сервере A, а мастер-клапан — на отдельном сервере B (master_mqtt_server_id=B). Хаб подключается только к A. Эхо мастер-клапана с B никогда не принимается: master_valve_observed в БД и mv_state в SSE навсегда отражают лишь оптимистичные записи из zone_control (update_group_fields сразу после publish), реальное залипание/ручное переключение мастер-клапана в UI не видно.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Набор MQTT clients строится по объединению server IDs зон и master valves, поэтому master-only сервер получает отдельную подписку.
- **Пакет:** `phase2-sse-safety`.
- **Fix commit:** `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`.
- **Regression tests:** `tests/unit/test_phase2_sse_hub.py::test_master_only_tls_server_subscribes_with_safe_keepalive_and_no_mode`.

### #55 [low] `services/mqtt_pub.py:216` — _publish_with_retries: последняя републикация в backoff-цикле отправляется, но никогда не ожидается — функция возвращает False после фактически ушедшей команды и провоцирует дубль через новый клиент

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Кратковременный затык брокера ~7-15с: первые три wait_for_publish таймаутятся, 4-я публикация уходит и успешно доставляется после восстановления — но функция уже вернула False. Для master close это значит ложный «masters_failed_publish» в emergency_stop_all и запись mqtt_publish_failure в аудит при фактически закрытом клапане; плюс гарантированный дубликат команды через пересозданный клиент (для идемпотентных '0'/'1' безвредно, но аудит и счётчики врут, оператор видит несуществующий отказ).
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Каждая QoS republish-попытка, включая последнюю, получает wait_for_publish; успешная последняя доставка больше не возвращает False.
- **Пакет:** `phase2-mqtt-safety`.
- **Fix commit:** `7d750b49fe5612f52d6121bdb4c923f5e42df3ad`.
- **Regression tests:** `tests/unit/test_phase2_mqtt_safety.py::test_finding_55_last_qos_retry_is_waited_for_and_can_succeed`.

### #56 [low] `routes/auth.py:55` — Все Flask-рейт-лимитеры используют request.remote_addr без ProxyFix — за nginx все клиенты схлопываются в одно ведро, блокировка логина затрагивает всех

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** За nginx любой пользователь (или атакующий за basic-auth) делает 5 неудачных попыток логина → login_limiter заносит lockout для ключа '127.0.0.1' → следующие 900 с (15 мин) ВСЕ пользователи, включая админа с верным паролем, получают 429 на /api/login. Аналогично per-IP изоляция брутфорса на уровне Flask не работает — все запросы считаются как один IP. Первичную защиту даёт nginx limit_req (per real IP), поэтому серьёзность низкая, но Flask-слой деградирует до общего ведра и создаёт self-DoS логина.
- **Решение оркестратора:** Conditional on an external reverse proxy; the supported native WB deployment exposes Hypercorn directly.
- **Disposition Фаз 2–3:** `pending`.

### #57 [low] `routes/auth.py:39` — GET /login?guest=1 меняет сессию (сброс + role=viewer) без CSRF — можно принудительно понизить/разлогинить авторизованного админа

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Авторизованный админ (role=admin) открывает страницу/письмо с `<img src="https://controller/login?guest=1">`. Браузер шлёт GET с cookie админа; Flask очищает сессию и в ответе ставит cookie viewer. Админ молча понижается до read-only viewer и теряет доступ к мутациям до повторного входа. CSRF-даунгрейд/логаут (тот же класс, что задокументированный SEC-008 GET-logout), эскалации привилегий нет.
- **Disposition Фаз 2–3:** `pending`.

### #58 [high] `templates/programs.html:630` — Мастер программ трактует конфликтный ответ 200 {success:false} как успешное сохранение

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь редактирует программу так, что она пересекается с другой (в т.ч. отключённой) программой → PUT /api/programs/<id> возвращает 200 {success:false, has_conflicts:true} → resp.ok=true → мастер закрывается с тостом «✅ Программа обновлена», хотя изменения отброшены; loadPrograms() показывает старые значения. Аналогично при создании: тост «✅ Программа создана», программы в списке нет.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Wizard требует одновременно успешный HTTP-статус и отсутствие success:false; конфликтный 200 больше не закрывает форму и не показывает успех.
- **Пакет:** `phase2-programs-web`.
- **Fix commit:** `d0a30b346c1f520b9368ca39d8b6f97b0b3c0e1c`.
- **Regression tests:** `tests/api/test_phase2_programs_web.py::TestProgramsWizardResponseHandling::test_mutations_report_http_and_success_false_errors`.

### #59 [high] `db/programs.py:64` — Эвристика «1-базных» дней в БД портит выбор дней недели из UI, если не выбран понедельник

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь в мастере выбирает дни без понедельника, напр. «Вт, Чт» ([1,3]) → в БД сохраняется [0,2] → полив реально запускается в Пн и Ср, а после перезагрузки страницы в UI подсвечены Пн/Ср вместо выбранных. Выбор «только Вс» ([6]) превращается в субботу. Конфликт-чек при этом сравнивает несдвинутый ввод со сдвинутыми хранимыми днями.
- **Дубль:** `#10`.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Удалена неоднозначная legacy-эвристика 1-based дней из create/update; новые записи сохраняют переданные UI zero-based weekdays. Уже сохранённые строки намеренно не мигрируются.
- **Пакет:** `phase2-db-programs`.
- **Fix commit:** `ee453c30039997f7bf5ff74b671a96eea030b100`.
- **Regression tests:** `tests/unit/test_phase2_db_programs.py::test_create_program_preserves_zero_based_weekdays_without_monday`, `tests/unit/test_phase2_db_programs.py::test_update_program_preserves_zero_based_weekdays_without_monday`.

### #60 [medium] `static/js/zones.js:1052` — Удаление зоны: ответ 204 с пустым телом трактуется api.delete как falsy — UI молча не обновляется

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ на /zones жмёт «🗑» → confirm → зона реально удалена на сервере (204), но `if (success)` не срабатывает: строка остаётся в таблице, счётчик зон не меняется, тоста нет. Повторный клик по «🗑» той же строки возвращает 404 текст "Zone not found" → truthy → ложный тост «Зона удалена». Реальное состояние видно только после перезагрузки страницы.
- **Disposition Фаз 2–3:** `pending`.

### #61 [medium] `static/js/status.js:1975` — saveZoneEdit игнорирует success:false — валидационная ошибка 400 показывается как «Зона сохранена»

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** На странице «Статус» пользователь открывает лист редактирования зоны, стирает имя, жмёт сохранить → PUT возвращает 400 {success:false} → JS всё равно закрывает лист и показывает «✅ Зона сохранена»; loadZonesData() возвращает старое имя — изменение потеряно без единого сообщения об ошибке.
- **Дубль:** `#23`.
- **Disposition Фаз 2–3:** `pending`.

### #62 [low] `db/mqtt.py:100` — Сохранение MQTT-сервера из UI молча сбрасывает TLS-настройки (частичный PUT перезаписывает отсутствующие поля дефолтами)

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** MQTT-сервер настроен на TLS через прямой вызов API/скрипт (tls_enabled=1, пути к сертификатам заданы). Оператор на странице /mqtt правит, например, имя сервера и жмёт «Сохранить» → PUT /api/mqtt/servers/<id> без tls_* → tls_enabled=0, пути=NULL: TLS отключён молча, соединение с брокером далее идёт открытым текстом или ломается.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** MQTT repository строит настоящий partial UPDATE только из переданных полей; отсутствие TLS-полей больше не перезаписывает их дефолтами.
- **Пакет:** `phase2-mqtt-safety`.
- **Fix commit:** `7d750b49fe5612f52d6121bdb4c923f5e42df3ad`.
- **Regression tests:** `tests/api/test_phase2_mqtt_api.py::test_finding_62_partial_put_preserves_tls_configuration`.

### #63 [high] `irrigation_scheduler.py:1552` — start_group_sequence выключает бегущие зоны только в БД: без MQTT OFF и со снятием их стоп-джобов реле остаётся физически открытым и неуправляемым

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Зона 5 физически поливает (реле ON, retained '1', в APScheduler висит её zone_stop). Пользователь жмёт «Запустить группу с первой зоны»: bulk-off ставит зоне 5 state='off' в БД без публикации OFF, её автостоп-джобы удаляются, секвенсор включает зону 1. Реле зоны 5 остаётся запитанным: открыты два клапана одновременно (падение давления, нарушение эксклюзивности группы), причём ни group-exclusivity watchdog, ни ZoneWatchdog этого не видят — в БД только одна зона 'on'. Физический OFF зона 5 получит лишь когда до неё дойдёт очередь секвенса (возможно через десятки минут/часы); в варианте run-selected с подмножеством без зоны 5 состояние двух открытых клапанов чинит только ежесекундный watchdog по чужой ветке, а при отсутствии мастер-клапана вода течёт всё это время.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Group start физически force-останавливает все зоны до снятия jobs; после best-effort stop повторно читает группу и не создаёт sequence, пока каждая зона не подтверждена state=off.
- **Пакет:** `phase2-scheduler-safety`.
- **Fix commit:** `a91116a14fea7e6df07bb2869b0ae726fe562edd`.
- **Regression tests:** `tests/unit/test_phase2_scheduler_safety.py::test_63_group_start_physically_stops_every_peer_before_planting_sequence`, `tests/unit/test_phase2_scheduler_safety.py::test_63_group_start_aborts_when_peer_remains_fault_after_physical_stop`.

### #64 [high] `services/observed_state.py:147` — StateVerifier ретраит устаревшую команду: фоновая верификация перекрывает более свежую противоположную команду и может закрыть/открыть клапан вопреки текущему состоянию

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** stop_zone публикует '0', запускает verify_async(zone,'off'). Эхо реле задержалось/потерялось (ровно тот сбой, ради которого существуют ретраи) — верификатор ждёт 10 с. На 3-й секунде пользователь снова запускает зону: publish '1', полив пошёл, БД 'on'. На 10-й секунде отставший верификатор ре-публикует retained '0' (+ '/on') — реле ФИЗИЧЕСКИ закрывается посреди полива, верификатор подтверждает 'off' и завершается. SSE-хаб по эху '0' переводит зону в off и закрывает zone_run как 'ok' — полив молча потерян. Обратный вариант: verify('off') исчерпывает 3 попытки за ~30 с, пока зона уже успешно перезапущена, и _record_fault переводит здоровую поливающую зону в state='fault' (исключение из расписания до вмешательства оператора).
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Per-zone generation token и persisted commanded_state прекращают stale retry/confirmation/fault после более новой противоположной команды.
- **Пакет:** `phase2-zone-state-safety`.
- **Fix commit:** `42142ddea539625fe948bdaeb358b338aff58cef`.
- **Regression tests:** `tests/unit/test_phase2_zone_state_safety.py::test_finding_64_stale_verifier_does_not_republish_or_fault`.

### #65 [high] `irrigation_scheduler.py:1599` — start_group_sequence не сериализует сессии группы: повторный запуск (или запуск во время идущей программы) порождает два параллельных секвенсора, интерливящих ON/OFF по одним зонам

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь дважды жмёт «Запустить группу» с интервалом 2 с (или жмёт её, пока по расписанию идёт программа этой группы). Оба вызова проходят: два треда _run_group_sequence работают параллельно по одному списку зон со сдвигом. Тред A стартует зону 1; тред B делает bulk-off (state='off' в БД у только что включённой зоны), снимает её джобы и снова публикует ON зоны 1, создавая ВТОРОЙ открытый zone_run. Дальше A останавливает зону 1 по своему таймеру и включает зону 2, пока B ещё «поливает» зону 1 → B позже повторно включит зону 2, A остановит её посреди слота B и т.д. Итог: рваные циклы открытия/закрытия клапанов, задвоенные и преждевременно закрытые zone_runs, попеременная борьба с ежесекундным group-exclusivity watchdog.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Group session захватывается до DB/MQTT initialization и второй запуск отклоняется, не переиспользуя и не заменяя Event владельца.
- **Пакет:** `phase2-scheduler-safety`.
- **Fix commit:** `a91116a14fea7e6df07bb2869b0ae726fe562edd`.
- **Regression tests:** `tests/unit/test_phase2_scheduler_safety.py::test_65_second_group_session_is_rejected_instead_of_reusing_event`.

### #66 [medium] `irrigation_scheduler.py:2391` — Гонка recover_missed_runs со стартовой зачисткой: восстановленный запуск глушится stop_on_boot_active_zones/_boot_sync, первая зона не поливается, но получает фантомный zone_run 'ok'

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Сервис перезапускается посреди программы. recover_missed_runs запускает остаток: runner пишет зоне state='on', exclusive_start_zone создаёт zone_run, открывает мастер-клапан, публикует ON. Через долю секунды в главном треде stop_on_boot_active_zones видит state='on' и force-стопает зону (finish_zone_run status='ok', retained '0'), а _boot_sync дополнительно публикует OFF по всем топикам и закрывает мастер-клапан, который runner только что открыл. Runner спит duration минут, полагая, что зона поливается: физически клапан закрыт, «восстановленная» зона не получает воду, в истории остаётся фантомная запись 'ok' с нулевым фактическим поливом. Последующие зоны программы включаются уже после boot_sync и поливаются — сбой затрагивает ровно прерванную зону и мастер-клапан, недетерминированно по таймингу тредов.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** APScheduler стартует paused, boot cleanup/OFF выполняются до recovery, а resume разрешён только через complete_boot_recovery после внешней reconciliation.
- **Пакет:** `phase2-scheduler-safety`.
- **Fix commit:** `a91116a14fea7e6df07bb2869b0ae726fe562edd`.
- **Regression tests:** `tests/unit/test_phase2_scheduler_safety.py::test_66_87_init_stays_paused_until_explicit_boot_completion`.

### #67 [medium] `services/sse_hub.py:236` — Анти-рестарт-окно SSE-хаба мертво: mark_zone_stopped не вызывается ни одним стоп-путём, recently_stopped всегда False — защита от эхо-включения после ручного стопа недостижима

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь останавливает зону; в этот момент MQTT-линк SSE-хаба рвётся и paho переподключается. На переподписке (_on_connect, qos=1) брокер доставляет retained/недоставленный 'on' старее команды стопа. Задуманная защита recently_stopped(zid, 5s) должна была проигнорировать эхо и опубликовать '0', но окно никогда не взводится: хаб принимает 'on', снова помечает зону поливающей (state='on', watering_start_time=now), взводит автостоп на полную базовую длительность и в конце пишет фантомный zone_run 'ok' — остановленная пользователем зона до конца «виртуального» полива отображается как работающая, а история фиксирует полив, которого не было.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Observed OFF записывает anti-restart timestamp, а свежий persisted commanded_state=off/state=off|stopping закрывает остановки, случившиеся во время disconnect hub; поздний ON получает контркоманду.
- **Пакет:** `phase2-sse-safety`.
- **Fix commit:** `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`.
- **Regression tests:** `tests/unit/test_phase2_sse_hub.py::test_recent_commanded_stop_blocks_late_on_without_external_marker_call`.

### #68 [medium] `db/zones.py:260` — update_zone: полная перезапись строки из несвежего снапшота — read-modify-write без транзакции затирает конкурентные изменения state/postpone/watering_start_time

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь сохраняет правку имени/длительности зоны в момент, когда планировщик стартует полив (zones_state пишет state='on', watering_start_time) или rain-monitor ставит postpone_until: update_zone уже прочитал снапшот со state='off'/postpone_until=NULL, его UPDATE ждёт освобождения лока и коммитит поверх — состояние в БД откатывается на 'off' при физически открытом клапане (UI показывает «выключено», exclusive_start считает зону свободной → возможен второй старт), либо теряется свежепоставленный postpone и зона польёт в дождь.
- **Disposition Фаз 2–3:** `pending`.

### #69 [medium] `routes/mqtt_api.py:98` — PUT /api/mqtt/servers/<id> возвращает расшифрованный сохранённый пароль брокера — маскировка VULN-004 есть только в GET, а новая keep-семантика ветки делает утечку регулярной

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ правит имя или хост MQTT-сервера в настройках → в JSON-ответе PUT приходит {"server": {..., "password": "<реальный пароль брокера>"}} — секрет попадает в devtools/память страницы/прокси-логи; в сочетании с уже найденными stored-XSS в zones.js любой внедрённый скрипт может снять пароль одним fetch'ем сохранения.
- **Дубль:** `#17`.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** POST и PUT всегда маскируют password в ответе; сохранённый расшифрованный секрет не покидает сервер даже при password-less partial update.
- **Пакет:** `phase2-mqtt-safety`.
- **Fix commit:** `7d750b49fe5612f52d6121bdb4c923f5e42df3ad`.
- **Regression tests:** `tests/api/test_phase2_mqtt_api.py::test_findings_17_69_create_and_partial_update_never_echo_saved_secret`.

### #70 [medium] `db/programs.py:216` — check_program_conflicts игнорирует enabled: выключенная программа навсегда блокирует создание/сохранение пересекающейся программы

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь выключает старую программу «Вечер 23:00» и создаёт вместо неё новую на то же время с теми же зонами → API отказывает («Обнаружены конфликты программ»), хотя выключенная программа никогда не запустится; единственный обход — удалить старую программу, потеряв её настройки.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Conflict query исключает programs.enabled=0, поэтому выключенная программа больше не блокирует пересекающееся расписание.
- **Пакет:** `phase2-db-programs`.
- **Fix commit:** `ee453c30039997f7bf5ff74b671a96eea030b100`.
- **Regression tests:** `tests/unit/test_phase2_db_programs.py::test_conflict_check_ignores_disabled_programs`.

### #71 [medium] `db/programs.py:186` — duplicate_program молча сдвигает дни недели копии на день назад, если в днях оригинала нет понедельника

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь дублирует программу «Вт/Чт/Сб 06:00», меняет в копии только время — копия сразу планируется и поливает по Пн/Ср/Пт: полив идёт не в те дни без каких-либо ошибок в UI. Тот же корень, что у ранее найденной эвристики 1-базных дней, но отдельная точка входа: чистый server-side round-trip без участия UI-формата.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** duplicate_program использует тот же zero-based writer без повторного сдвига дней копии.
- **Пакет:** `phase2-db-programs`.
- **Fix commit:** `ee453c30039997f7bf5ff74b671a96eea030b100`.
- **Regression tests:** `tests/unit/test_phase2_db_programs.py::test_duplicate_program_preserves_stored_zero_based_weekdays`.

### #72 [low] `db/zones.py:738` — finish_zone_run: чтение confirmed и UPDATE статуса не атомарны — поздний echo реле помечает реальный полив как 'failed'

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Echo реле 'on' приходит с задержкой (переподключение SSE-хаба к брокеру, retained-сообщение) практически одновременно с автостопом: finish_zone_run читает confirmed=0, SSE-тред коммитит confirmed=1, finish_zone_run записывает status='failed' — история показывает «полив не состоялся» для физически прошедшего полива, статистика воды и last_watering_time (фильтр status='ok') его теряют.
- **Disposition Фаз 2–3:** `pending`.

### #73 [low] `db/migrations.py:344` — _recreate_table_without_columns теряет UNIQUE/FK-констрейнты при downgrade: после rollback telegram_add_fsm_and_notif ломается upsert_bot_user (ON CONFLICT без UNIQUE)

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Оператор откатывает миграцию telegram_add_fsm_and_notif штатным rollback_migration() (задокументированный DOWNGRADE_REGISTRY) → бот продолжает работать, но upsert_bot_user падает на каждом сообщении (логируется и возвращает False), авторизация/учёт пользователей Telegram полностью ломаются до ручного восстановления схемы.
- **Disposition Фаз 2–3:** `pending`.

### #74 [high] `templates/programs.html:489` — Stored XSS: имя зоны вставляется в innerHTML без escapeHtml в мастере программ (шаг 4 «Выбор зон»)

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Зона переименована в `<img src=x onerror=alert(document.cookie)>` (через PUT /api/zones/<id>, CSV-импорт или bulk-import). Админ открывает /programs и нажимает «Создать программу» (или «Редактировать») — payload исполняется в админской сессии при первом же renderWizard(), ещё на шаге 1. CSRF-токен доступен из meta-тега.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Имя зоны в шаге выбора мастера проходит escapeHtml перед вставкой в innerHTML.
- **Пакет:** `phase2-programs-web`.
- **Fix commit:** `d0a30b346c1f520b9368ca39d8b6f97b0b3c0e1c`.
- **Regression tests:** `tests/api/test_phase2_programs_web.py::TestProgramsWizardResponseHandling::test_wizard_escapes_zone_names_before_assigning_inner_html`.

### #75 [medium] `static/js/zones.js:1862` — CSV-экспорт зон без квотирования: запятая в имени зоны ломает раскладку колонок и портит данные при реимпорте

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Зона с именем «Газон, север» экспортируется как `5,Газон, север,🌿,10,...`. При импорте этого же файла: name='Газон', icon=' север', duration=parseInt('🌿')=NaN → JSON.stringify превращает NaN в null → бэкенд получает duration:null (что по смежному багу bulk_upsert_zones откатывает весь импорт с 500; а после его фикса — молча запишет искажённые icon/name). Заявленный round-trip «экспорт → импорт» портит данные всех зон после первой с запятой.
- **Disposition Фаз 2–3:** `pending`.

### #76 [medium] `static/js/status.js:1804` — Гонка оптимистичного состояния зоны с 5-секундным поллингом: только что запущенная зона визуально откатывается в «off» и приглашает повторный старт

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь жмёт «Запустить» в момент, когда очередной опрос /api/zones уже в полёте (ответ ещё со state='off', т.к. POST start ещё не обработан). Ответ опроса приходит после оптимистичного рендера → карточка возвращается в «▶ Запустить», таймер исчезает, хотя зона реально запускается. До следующего опроса (до 5с) UI показывает ложное «выключено»; повторный клик пользователя отправляет второй POST /mqtt/start, попадая в подтверждённую ранее backend-гонку двойного старта (второй открытый zone_run).
- **Disposition Фаз 2–3:** `pending`.

### #78 [low] `static/js/status.js:2381` — confirmRunWithDefaults / confirmRun: цепочки fetch без .catch — при сетевой ошибке или не-JSON ответе полноэкранный оверлей блокирует UI на 15с без сообщения

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Wi-Fi обрыв или nginx 502 (HTML-тело → r.json() бросает) в момент «Запустить с настройками зон»: оверлей с крутилкой висит 15 секунд, блокируя все элементы страницы, никакого сообщения об ошибке; в пути одиночной группы все зоны группы дополнительно остаются в оптимистичном состоянии «on» с таймерами до следующего поллинга.
- **Disposition Фаз 2–3:** `pending`.

### #79 [low] `templates/programs.html:244` — programs.html: toggleEnabled/duplicateProgram/deleteProgram молча игнорируют не-OK ответы сервера — ни тоста, ни отката UI

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Сервер отвечает 500 (или 409) на PATCH /api/programs/<id>/enabled — пользователь кликает тумблер программы, визуально ничего не происходит и не появляется ни одного сообщения; пользователь не понимает, включена программа или нет (аналогично для «Дублировать» и «Удалить»).
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** toggle/duplicate/delete читают безопасный JSON-ответ и показывают ошибку при non-OK или success:false без ложной локальной мутации.
- **Пакет:** `phase2-programs-web`.
- **Fix commit:** `d0a30b346c1f520b9368ca39d8b6f97b0b3c0e1c`.
- **Regression tests:** `tests/api/test_phase2_programs_web.py::TestProgramsWizardResponseHandling::test_mutations_report_http_and_success_false_errors`.

### #80 [high] `routes/mqtt_api.py:156` — Совпадающий client_id: probe/status/scan-sse из настроек MQTT выбивают live-подписку SSE-хаба (MQTT session takeover)

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** У MQTT-сервера в настройках заполнен client_id → хаб подключён с ним. Админ открывает страницу MQTT и жмёт «проверить» (/status) или запускает скан (/scan-sse, до 300 с): временный клиент коннектится с тем же client_id → брокер выкидывает клиента хаба → paho-реконнект (1–30 с) снова выбивает сканер — ping-pong на всё время скана. Подписки clean-session: все эхо реле за окно разрыва теряются → mark_zone_run_confirmed не вызывается, реальный полив записывается в историю как 'failed', ant-restart/emergency-контроль эха и master_valve_observed молча не работают.
- **Disposition Фаз 2–3:** `pending`.

### #81 [medium] `services/sse_hub.py:173` — SSE-хаб игнорирует TLS-настройки MQTT-сервера — для tls_enabled=1 брокера эхо реле не приходит вообще (как и probe/status/scan)

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Сервер настроен с tls_enabled=1 (порт 8883). Команды зонам уходят (publisher с TLS), но клиент хаба делает plaintext CONNECT на TLS-порт → рукопожатие падает → warning в лог и continue. Итог: SSE-стрим состояний зон мёртв, master_valve_observed никогда не обновляется, анти-рестарт и подавление ON при EMERGENCY_STOP не работают — молча, без какой-либо индикации в UI. /api/mqtt/<id>/probe|status|scan-sse на том же сервере также всегда «connect failed/не подключён», хотя публикация работает.
- **Решение оркестратора:** TLS is supported by the publisher/verifier but omitted by the hub and diagnostic MQTT clients.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** P06 добавил TLS permanent SSE hub, а P07 follow-up централизованно применяет CA/client cert/key, version и insecure mode ко всем probe/status/scan clients до connect; ошибка TLS setup исключает plaintext fallback.
- **Пакет:** `phase2-mqtt-diagnostic-tls`.
- **Fix commit:** `e71db24703adb6bf70d382bb36cd009f3b978684`.
- **Regression tests:** `tests/api/test_phase2_mqtt_api.py::test_finding_81_diagnostic_client_applies_tls_before_route_connect[probe]`, `tests/api/test_phase2_mqtt_api.py::test_finding_81_diagnostic_client_applies_tls_before_route_connect[status]`, `tests/api/test_phase2_mqtt_api.py::test_finding_81_diagnostic_client_applies_tls_before_route_connect[scan]`, `tests/api/test_phase2_mqtt_api.py::test_finding_81_tls_setup_failure_aborts_before_plaintext_connect`, `tests/unit/test_phase2_sse_hub.py::test_master_only_tls_server_subscribes_with_safe_keepalive_and_no_mode`.

### #82 [medium] `services/sse_hub.py:381` — keepalive=5 c у клиента SSE-хаба + блокирующие DB/publish-вызовы внутри _on_message → брокер сам выкидывает хаб по таймауту пинга

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Приходит эхо 'on' при активном EMERGENCY_STOP (или в анти-рестарт-окне): _on_message синхронно публикует контр-команду '0'; если кэшированный publish-клиент заклинен, ветка ретраев занимает ~20-30 с. Всё это время сетевой тред paho не шлёт PINGREQ; при keepalive=5 брокер рвёт соединение через ~7.5 с. Подписка clean-session → сообщения за время разрыва потеряны: пропущенные эхо 'on' → полив записан 'failed', пропущенные 'off' → зона висит «on» в UI до следующего события.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Keepalive увеличен до 60 секунд, а DB/scheduler/publish обработка перенесена из paho callback в упорядоченный background worker, переживающий ошибочный event.
- **Пакет:** `phase2-sse-safety`.
- **Fix commit:** `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`.
- **Regression tests:** `tests/unit/test_phase2_sse_hub.py::test_paho_callback_returns_before_blocking_database_work_finishes`, `tests/unit/test_phase2_sse_hub.py::test_event_worker_survives_unexpected_handler_exception`.

### #83 [medium] `services/sse_hub.py:229` — Контр-команда OFF при EMERGENCY_STOP/анти-рестарте — fire-and-forget QoS0 без retain, а observed_state='off' пишется независимо от успеха публикации

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** EMERGENCY_STOP активен, реле зоны включено извне (wb-панель/retained '1' при реконнекте). Хаб видит 'on', переписывает new_state='off', пишет в БД state='off' и observed_state='off', и отправляет '0' с QoS0. При транзиентном сбое publish (клиент-паблишер не подключён — get_or_create вернул None, «client unavailable, dropping») команда молча теряется, ретраев нет, нового эха не будет (реле состояние не меняло). Итог: во время аварийного стопа клапан физически открыт и льёт воду, а БД/UI показывают off с «подтверждённым» observed_state.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Emergency/anti-bounce OFF публикуется QoS 2 retained без debounce и с проверкой результата; broker ack не выдаётся за физический OFF, observed state остаётся ON до настоящего OFF-эхо.
- **Пакет:** `phase2-sse-safety`.
- **Fix commit:** `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`.
- **Regression tests:** `tests/unit/test_phase2_sse_hub.py::test_emergency_counter_off_is_reliable_and_observation_stays_truthful_on_failure`.

### #84 [medium] `services/mqtt_pub.py:291` — _publish_one/_invalidate_client разрушает разделяемый MQTT-клиент, которым в этот момент публикуют другие треды — каскад ложных сбоев OFF-команд

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Старт зоны в группе из 5+ зон: воркеры параллельно шлют '0' соседям через один кэшированный клиент. У одного воркера доставка QoS2 подвисает → спустя ~20 с он вызывает _invalidate_client и дисконнектит клиента, которым остальные воркеры прямо сейчас ждут PUBACK/PUBCOMP → их wait_for_publish истекает, каждый по цепочке тоже инвалидирует (в т.ч. только что созданный здоровый клиент соседнего воркера) и пере-публикует. Итог: лавина пересозданий клиентов, ложные false от publish_mqtt_value для реально доставленных OFF (плюс дубли команд через новые клиенты), state-машина зон получает «mqtt off failed» на ровном месте.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Invalidation привязана к generation клиента и откладывает teardown, пока активные publisher leases не освобождены; stale worker не разрушает replacement/shared client.
- **Пакет:** `phase2-mqtt-safety`.
- **Fix commit:** `7d750b49fe5612f52d6121bdb4c923f5e42df3ad`.
- **Regression tests:** `tests/unit/test_phase2_mqtt_safety.py::test_finding_84_stale_worker_cannot_invalidate_replacement_client`, `tests/unit/test_phase2_mqtt_safety.py::test_finding_84_in_use_client_teardown_is_deferred_until_last_release`.

### #85 [low] `routes/mqtt_api.py:150` — /api/mqtt/<id>/probe: duration из тела запроса не валидируется — гигантское значение/『1e999』 навсегда блокирует HTTP-воркер, нечисловое даёт 500

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** POST /api/mqtt/1/probe {"duration": "1e999"} → float = inf → цикл не завершается, пока не накопится 5000 сообщений (на тихом брокере — никогда): тред hypercorn занят навсегда, MQTT-клиент (с общим client_id — см. отдельную находку) держит сессию и перманентно выбивает SSE-хаб. {"duration": "abc"} → непойманный ValueError → 500 c трейсбеком вместо контрактного api_soft.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Probe принимает только конечную duration в диапазоне 0 < duration <= 30 секунд и отклоняет NaN/Infinity/строки/неположительные/чрезмерные значения до запуска клиента.
- **Пакет:** `phase2-mqtt-safety`.
- **Fix commit:** `7d750b49fe5612f52d6121bdb4c923f5e42df3ad`.
- **Regression tests:** `tests/api/test_phase2_mqtt_api.py::test_finding_85_probe_duration_is_finite_and_bounded`.

### #86 [low] `routes/mqtt_api.py:333` — scan-sse: через 300 с MQTT-тред тихо завершается, а SSE-генератор продолжает слать ping вечно — скан «замерзает», слот per-IP-лимита остаётся занят

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ запускает непрерывный скан топиков и оставляет вкладку: спустя 5 минут сообщения перестают приходить, но UI выглядит «живым» (ping-и идут) — пользователь думает, что в сети тишина, хотя подписка давно снята. Вкладка при этом продолжает занимать один из MAX_SCAN_SSE_PER_IP=2 слотов; две «замёрзшие» вкладки → 429 для нового скана до закрытия старых.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** По истечении scan timeout worker отправляет terminal end event, завершает generator и освобождает per-IP слот.
- **Пакет:** `phase2-mqtt-safety`.
- **Fix commit:** `7d750b49fe5612f52d6121bdb4c923f5e42df3ad`.
- **Regression tests:** `tests/api/test_phase2_mqtt_api.py::test_finding_86_scan_timeout_ends_stream_and_releases_slot`.

### #87 [high] `irrigation_scheduler.py:2329` — Recovery-запуск гонится с boot-ремедиацией: первая восстановленная зона молча «поливается» с выключенным реле/закрытым мастер-клапаном

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Сервис рестартует в 06:20 посреди программы 06:00. recovery-job стартует первую незавершённую зону (~1-3 c: guard → weather-check → state='on' → MV open → MQTT '1'); параллельно основной поток доходит до stop_on_boot_active_zones/_boot_sync, который через секунды публикует retained '0' в топик этой зоны и закрывает все мастер-клапаны. Реле выключено/МВ закрыт, а секвенсер recovery считает, что зона поливается, и ждёт весь её слот (до полной длительности зоны) — первая восстановленная зона остаётся сухой без единой ошибки в логах; в zone_runs при этом остаётся «успешный» ран.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Recovery Date jobs и persistent misfires не могут исполняться до завершения boot OFF: scheduler остаётся paused до явной lifecycle boundary.
- **Пакет:** `phase2-scheduler-safety`.
- **Fix commit:** `a91116a14fea7e6df07bb2869b0ae726fe562edd`.
- **Regression tests:** `tests/unit/test_phase2_scheduler_safety.py::test_66_87_init_stays_paused_until_explicit_boot_completion`.

### #88 [medium] `irrigation_scheduler.py:2294` — recover_missed_runs повторно поливает уже завершившийся (укороченный погодой или остановленный вручную) запуск — окно строится по номинальным длительностям без проверки фактического завершения

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Погодный коэффициент 50%: программа 06:00, 8 зон × 20 мин номинала (окно до 08:40), фактически полив закончился в 07:20. Плановый деплой/рестарт сервиса в 08:00 → окно ещё «активно», зоны выключены → recovery вычисляет start_idx=6 и ПОВТОРНО поливает зоны 7-8, которые уже политы час назад. Аналогично: пользователь остановил идущую программу кнопкой Стоп в 06:30, watchdog/деплой рестартует сервис в 06:40 → recovery «догоняет» остаток программы, которую пользователь сознательно отменил.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Boot recovery требует persisted evidence реально прерванной активной зоны и не воскресит только номинально совпавшее уже завершённое окно.
- **Пакет:** `phase2-scheduler-safety`.
- **Fix commit:** `a91116a14fea7e6df07bb2869b0ae726fe562edd`.
- **Regression tests:** `tests/unit/test_phase2_scheduler_safety.py::test_88_boot_recovery_requires_evidence_of_an_interrupted_active_zone`.

### #89 [medium] `services/zone_control.py:609` — Ручной старт одной зоны отменяет на сегодня ВСЕ программы группы, чьё основное время уже прошло — вечерние extra_times-слоты молча пропадают

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Программа: основное время 06:00 + extra_times ['20:00'], зоны группы 1. Утренний запуск отработал штатно. В 12:00 пользователь вручную включает одну зону группы тумблером на статус-странице (/api/zones/<id>/mqtt/start). Условие 06:00 <= 12:00 истинно → в program_run_cancellations пишется отмена программы на сегодня для группы 1. В 20:00 планировщик срабатывает по extra-слоту, видит отмену и пропускает все зоны группы — вечерний полив молча не происходит (в UI и логах это выглядит как «отменено», хотя пользователь ничего не отменял).
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Ручной старт отменяет только активную runtime session и больше не записывает day-wide program cancellation, поэтому вечерний extra_times slot сохраняется.
- **Пакет:** `phase2-zone-state-safety`.
- **Fix commit:** `42142ddea539625fe948bdaeb358b338aff58cef`.
- **Regression tests:** `tests/unit/test_phase2_zone_state_safety.py::test_finding_89_manual_start_does_not_cancel_future_extra_slot`.

### #90 [low] `irrigation_scheduler.py:1177` — schedule_program затирает zones.scheduled_start_time «сегодняшним» временем программы независимо от дней недели и прошедшего времени — карточка группы показывает ложную «Следующая: HH:MM»

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Программа только по понедельникам в 06:00; рестарт сервиса в четверг в 10:00. Всем зонам программы записывается scheduled_start_time = четверг 06:00 (уже прошедшее сегодня). Карточка группы показывает «Следующая: <зона> → 06:00», хотя реальный ближайший запуск — в понедельник; значение остаётся до первого события, вызывающего reschedule_group_to_next_program. То же после любого сохранения программы из UI.
- **Решение оркестратора:** Arbitrated REAL with corrected consumer: the false time appears in the global active-zone sidebar, not the group card, and requires another active zone to make the sidebar visible.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** scheduled_start_time строится от минимального настоящего next fire живых trigger jobs и zone offsets, а не от сегодняшнего HH:MM.
- **Пакет:** `phase2-scheduler-safety`.
- **Fix commit:** `a91116a14fea7e6df07bb2869b0ae726fe562edd`.
- **Regression tests:** `tests/unit/test_phase2_scheduler_safety.py::test_90_scheduled_start_uses_next_real_weekday_not_today`.

### #91 [high] `app.py:407` — POST /api/env (запись конфигурации env-датчиков) доступен анонимно и освобождён от CSRF — межсайтовая форма стирает конфиг датчиков

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** 1) Аноним в LAN (или гость по /login?guest=1) шлёт POST /api/env с JSON {"temp":{"enabled":true,"topic":"/evil","server_id":1}} — без сессии и без X-CSRFToken конфиг датчиков перезаписывается и env_monitor переподписывается на чужой топик; температура/влажность на дашборде начинают браться из подложных данных. 2) Drive-by: страница злоумышленника с <form method=POST action="http://контроллер/api/env"> — CSRF-exempt, request.get_json() по form-urlencoded вернёт None → data={} → set_env_config({}) отключает оба датчика и стирает топики; работает даже из браузера админа за nginx basic-auth.
- **Решение оркестратора:** Anonymous JSON mutation is real on the native port. The original drive-by HTML-form scenario is not: form encoding gets 415 and cross-origin JSON normally needs CORS preflight.
- **Disposition Фаз 2–3:** `pending`.

### #92 [medium] `static/js/status.js:60` — status.js syncServerTime парсит now_iso ('YYYY-MM-DD HH:MM:SS') через new Date() без нормализации — на Safari/iOS шапка «Время:» навсегда показывает NaN

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь открывает страницу статуса на iPhone/Safari: syncServerTime() при загрузке получает now_iso, new Date() даёт Invalid Date, _serverTimeOffset=NaN; setInterval(updateDateTime,1000) (status.js:1020) каждую секунду пишет в <span id="datetime"> строку вида «NaN-NaN-NaN NaN:NaN:NaN» — часы в шапке навсегда сломаны, при этом футер (app.js) показывает время корректно.
- **Решение оркестратора:** Conditional browser support defect. Reproduced for Safari 15-era WebKit; current Safari accepts the non-standard string. No minimum supported browser version is documented, so the legacy path remains real.
- **Disposition Фаз 2–3:** `pending`.

### #93 [medium] `static/js/history.js:444` — История поливов: zone_runs.start_utc/end_utc хранят ЛОКАЛЬНОЕ naive-время, а history.js трактует их как UTC-ISO — на Safari/iOS список запусков без дат и времени

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Открыть модалку «История полива» на iPhone (Safari/iOS-Chrome): в renderRuns (history.js:382-384) utcIsoToLocalDate('2026-07-17 06:00:00')='' — все запуски сваливаются в одну группу с пустым заголовком даты, время каждого запуска — пустая строка ('' вместо '06:00 → 06:30'). Дополнительно: на устройстве с TZ, отличной от контроллера (телефон в поездке), Chromium показывает время контроллера, помечая его как местное — пользователь видит сдвинутые часы запусков.
- **Решение оркестратора:** Modern Safari parses the string, but production writers store controller-local naive values while the frontend contract interprets them as UTC, producing a real timezone shift.
- **Disposition Фаз 2–3:** `pending`.

### #94 [low] `routes/zones_history_api.py:84` — /api/zones/*/history: SQL-окно сравнивает локальные строки 'YYYY-MM-DD HH:MM:SS' с UTC-ISO границами 'YYYY-MM-DDTHH:MM:SSZ' — первый день окна выпадает целиком при TZ контроллера UTC+0 и западнее

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Контроллер в TZ UTC+0 (или любой западнее, напр. после сброса TZ в UTC при переустановке): пользователь открывает историю за 7 дней — запуски первого (самого старого) дня отсутствуют в графике, в списке запусков, в summary (total_minutes/total_runs занижены) и в CSV-экспорте (/api/zones/<id>/history.csv), хотя строки лежат в zone_runs. В Москве (UTC+3) дефект замаскирован: граница получает дату на день раньше окна.
- **Disposition Фаз 2–3:** `pending`.

### #95 [low] `static/js/zones.js:446` — zones.js настройки группы: ответы 4xx с {success:false} — истинные объекты для truthiness-проверок, UI показывает «Сохранено» при отклонённом сервером сохранении

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Вкладка A: админ удаляет MQTT-сервер на /mqtt. Вкладка B (открыта ранее, /zones): админ включает тумблер мастер-клапана группы — клиентская валидация проходит (select ещё содержит старый сервер), сервер отвечает 400 {success:false, message:'Нужен корректный MQTT-сервер для мастер-клапана'}, но UI показывает «Настройки мастер-клапана сохранены» и помечает use_master_valve=true в локальном кэше. Пользователь уверен, что мастер-клапан настроен; фактически в БД ничего не записано, клапан не будет управляться.
- **Disposition Фаз 2–3:** `pending`.

### #96 [medium] `services/security.py:49` — user_required отвергает роль "viewer": гостевой просмотр (/login?guest=1) полностью нерабочий — редирект на логин вместо статус-страницы

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Оператор публикует ссылку `https://controller/login?guest=1` для садовника, чтобы тот в режиме «только чтение» видел статус зон без админ-пароля. Садовник открывает ссылку → сессия становится role=viewer → GET / (index) под @user_required видит role не из [guest,user,admin] → 302 redirect на /login. Read-only просмотр, ради которого существует роль viewer, недоступен вообще: пользователь бесконечно возвращается на форму логина. (Дополнительно: механизм усиливает известную ?guest=1-атаку — принудительный downgrade авторизованного админа в viewer лишает его доступа не только к мутациям, но и ко всем страницам приложения.)
- **Решение оркестратора:** Only /login?guest=1 viewer mode is broken; the current /?guest=1 button uses role=guest and works.
- **Disposition Фаз 2–3:** `pending`.

### #97 [low] `services/image_pipeline.py:48` — Decompression-bomb cap (50 MP) проверяется ПОСЛЕ полного декодирования img.load() — не предотвращает выделение памяти при загрузке изображений

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ (загрузка фото зоны /api/zones/<id>/photo и карты /api/map требует роль admin) отправляет крафтовый JPEG 12000×12500 (~150 MP, размер файла в пределах 20 MB MAX_FILE_SIZE). Pillow декодирует его в ~450 MB RGB-битмап в памяти на контроллере Wirenboard (обычно 512 MB–1 GB ОЗУ) до того, как проверка 50 MP отбросит файл → риск OOM/деградации сервиса. Заявленная защита от decompression-bomb фактически не срабатывает для 50–178 MP. Требует прав admin (отсюда низкая severity); от изображений >178 MP частично защищает встроенный guard Pillow.
- **Disposition Фаз 2–3:** `pending`.

### #98 [high] `db/migrations.py:267` — Рестарт сервиса с пустой таблицей zones молча сбрасывает пароль администратора на дефолтный "1234"

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ ставит свой пароль, затем при реконфигурации удаляет все зоны (штатный DELETE /api/zones/<id> по одной) и перезапускает контроллер (deploy/systemd restart). Логин-пароль молча становится '1234': владелец теряет доступ со своим паролем, а любой, кто знает дефолт, получает полный админ-доступ к системе полива без принудительной смены пароля.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Bootstrap пароля теперь использует INSERT OR IGNORE: свежая БД по-прежнему получает начальный пароль, а существующий password_hash сохраняется независимо от количества зон.
- **Пакет:** `phase2-migration-password`.
- **Fix commit:** `29708009a2a14ea59d82160b1aa69bddbe05e389`.
- **Regression tests:** `tests/unit/test_phase2_migration_password.py::test_restart_with_empty_zones_preserves_existing_admin_password`.

### #99 [medium] `db/migrations.py:1250` — Миграция zone_runs_backfill_source трактует локальное naive-время start_utc как UTC — на не-UTC контроллере вся история программных запусков классифицируется как 'manual'

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Контроллер в любой не-UTC зоне (Wirenboard в РФ = UTC+3): при первом апгрейде на версию с колонкой source одноразовая миграция необратимо помечает все исторические программные поливы как 'ручные'. Фильтр 'программа/вручную' в истории поливов и любая статистика по source навсегда врут для всей предшествующей истории.
- **Disposition Фаз 2–3:** `pending`.

### #100 [medium] `db/programs.py:216` — check_program_conflicts полностью игнорирует extra_times: пересечение по дополнительным слотам не детектируется, программы реально накладываются в рантайме

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь создаёт программу на 20:00 для группы, у которой уже есть программа с extra-слотом 20:00 — валидация пропускает без предупреждения. В 20:00 стартуют две программы по одним зонам/группе одновременно; с учётом несериализованных секвенсоров группы это даёт интерливинг ON/OFF по одним реле и непредсказуемый полив.
- **Решение оркестратора:** Arbitrated REAL with corrected runtime scope. Ordinary groups normally cause the #129 full skip; group 999 is excluded from the session guard yet is selectable in the wizard, so both same-time jobs run.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** P01 сравнивает primary candidate со stored extra slots, P02 проверяет каждый candidate extra slot; группа 999 остаётся участником validation без изменения её runtime-семантики.
- **Пакет:** `phase2-programs-web`.
- **Fix commit:** `684d0cd4d1ea8fb445c9035dabedadc2a2c70b44`.
- **Regression tests:** `tests/unit/test_phase2_db_programs.py::test_conflict_check_includes_stored_extra_times_for_group_999`, `tests/api/test_phase2_programs_web.py::TestCandidateExtraTimesConflictChecks`.

### #101 [medium] `db/settings.py:167` — set_env_config: неатомарная запись из шести отдельных коммитов + непойманный ValueError на нечисловом server_id — 500 и полуприменённая конфигурация датчиков

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Сохранение настроек env-датчиков с нечисловым/пустым server_id (а /api/env по уже найденной проблеме доступен анонимно и без CSRF): клиент получает 500, при этом датчик температуры уже выключен и его топик заменён наполовину — env_monitor перезапускается с противоречивой конфигурацией, показания температуры пропадают до ручной починки.
- **Disposition Фаз 2–3:** `pending`.

### #102 [low] `routes/system_status_api.py:676` — /api/logs: фильтры from/to/type применяются в Python ПОСЛЕ 'LIMIT 1000 новейших' — запросы за старые периоды возвращают пусто при живых данных в БД

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** На проде logs пополняется каждым стартом/стопом зоны, импортом и т.п. — 1000 записей накапливаются за считанные дни. Запрос журнала событий за прошлый месяц (?from=2026-06-01&to=2026-06-15) возвращает пустой список, хотя записи есть в таблице; пользователь делает вывод, что событий не было — журнал бесполезен для расследований старше пары дней.
- **Disposition Фаз 2–3:** `pending`.

### #103 [medium] `templates/mqtt.html:219` — mqtt.html: updateStatuses запускает новую вечную цепочку опроса при каждом loadServers — опрос /api/mqtt/*/status умножается после каждого сохранения сервера

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ на странице MQTT сохраняет 3 строки серверов и создаёт один новый → 5 параллельных вечных цепочек. Каждая цепочка каждые 5 с последовательно дергает GET /api/mqtt/<id>/status по всем серверам, а бэкенд на каждый такой запрос делает реальный blocking client.connect() к брокеру с НАСТОЯЩИМ client_id сервера (routes/mqtt_api.py:228-232). Итог: постоянный шторм подключений к брокеру (усиливает уже известный takeover live-подписки SSE-хаба по совпадающему client_id в N раз), лишняя нагрузка на медленный ARM-контроллер и мигающие/ложные индикаторы offline из-за гонок параллельных проверок. Лечится только перезагрузкой страницы.
- **Disposition Фаз 2–3:** `pending`.

### #104 [low] `templates/mqtt.html:241` — mqtt.html: buildDisplayRegex ломает MQTT-wildcard '+' — сканер топиков показывает пустое дерево при фильтре с '+'

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь в браузере топиков вводит фильтр с MQTT-wildcard '+', например '/devices/+/controls/K1', и нажимает «Сканировать топики». Подписка на брокер уходит корректно (subFilter = uiFilter), события по SSE приходят, но клиентский displayRe не матчит ни один реальный топик — дерево топиков остаётся пустым, как будто сообщений нет. То же в startScanLoop (строка 416). Фильтры с '#' и '*' работают, только '+' сломан.
- **Disposition Фаз 2–3:** `pending`.

### #106 [medium] `static/js/zones.js:1500` — zones.js: SSE zones-sse — только дельты без снапшота и без ресинка при реконнекте; пропущенные события оставляют кнопки старт/стоп в противоположном состоянии навсегда

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ держит открытой страницу «Зоны». SSE-соединение рвётся (пересборка хаба после правки MQTT-сервера / 21-й SSE-клиент / Wi-Fi-блип), в этот момент планировщик включает зону 5. После авто-реконнекта дельта потеряна: в таблице зона 5 показана выключенной (▶). Пользователь жмёт ▶ → startZone шлёт mqtt/start по уже работающей зоне (перезапуск автостопа/таймера), либо наоборот считает зону выключенной, хотя реле открыто. Состояние не самовосстанавливается до ручного F5.
- **Решение оркестратора:** Stale state lasts until reload, another loadData-triggering action, or a later event; not literally forever.
- **Disposition Фаз 2–3:** `pending`.

### #107 [low] `static/js/zones.js:540` — zones.js: openModalById переносит модалку группы в body, а renderGroupsGrid создаёт дубликат с теми же id — открытая модалка «замерзает»: кнопка «Закрыть» и +/- цифр работают по скрытой копии

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ меняет длительность зоны в таблице (onchange → scheduleZoneAutoSave, 500 мс) и сразу открывает «Настройки счётчика воды». Через полсекунды saveZone → renderGroupsGrid пересоздаёт карточки. У видимой модалки: кнопки «+»/«−» крутят цифры невидимой копии (визуально ничего не меняется), live-обновление показаний пишет в скрытую копию, кнопка «Закрыть» вызывает closeModalById по скрытой копии — модалка не закрывается (спасает только клик по фону через window.onclick). Введённое показание счётчика при закрытии сохраняется из скрытой копии (нули), а не из того, что видел пользователь. Дубликаты id накапливаются при каждом цикле открытие→ререндер.
- **Disposition Фаз 2–3:** `pending`.

### #108 [low] `static/js/app.js:296` — app.js: Health-панель (клавиша 'h') вставляет сырые строки meta_tail (MQTT topic/payload) в innerHTML — XSS через сообщение брокера

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Любой, кто может публиковать в настроенный брокер (соседнее скомпрометированное WB-устройство, брокер без пароля в LAN), шлёт в отслеживаемый meta-топик payload вида `<img src=x onerror=fetch('/api/zones/1/mqtt/start',{method:'POST'})>`. Админ открывает Health-панель клавишей 'h' (панель авто-рефрешится каждые 2.5 с) — скрипт исполняется в его авторизованной сессии со всеми admin-правами (CSRF-токен доступен через мета-тег).
- **Решение оркестратора:** XSS requires a configured subscribed topic ending in /meta (or matching wildcard); normal WB relay topics do not.
- **Disposition Фаз 2–3:** `pending`.

### #109 [low] `templates/mqtt.html:177` — mqtt.html: createServer/updateRow молча игнорируют ответ {success:false} — валидационная ошибка сервера не показывается вообще

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ вводит некорректный порт/пустой host и жмёт «Сохранить»: бэкенд отвечает 400 {success:false, message}. UI не показывает ничего — ни тоста, ни отката; строка таблицы продолжает отображать невалидные значения, админ уверен, что сохранил, а публикация продолжает идти по старым параметрам.
- **Disposition Фаз 2–3:** `pending`.

### #110 [high] `services/zone_control.py:298` — Контракт state='fault' не реализован: fault-зона свободно стартует по расписанию, а любой stop_zone молча сбрасывает fault в 'off'

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Реле зоны 3 не подтверждает ON (обрыв провода) → StateVerifier ставит state='fault', оператор получает алерт «зона исключена из расписания». В тот же вечер плановая программа вызывает exclusive_start_zone(3): гард видит 'fault' (не on/starting) → перезаписывает в 'starting'→'on', полив «числится идущим», создаётся новый phantom zone_run, fault повторяется — и так каждый день. Либо: пользователь вручную стартует соседнюю зону 2 → cancel_group_jobs → stop_all_in_group(force=True) → stop_zone(3) переводит fault→off, защитный статус исчезает без вмешательства оператора. Заявленная в алерте и в докстринге _record_fault защита не существует.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** P05 сохраняет fault и блокирует direct start; P03 удалил scheduler prewrite state=on, вызывает exclusive_start_zone(source=program), учитывает False/fault и не планирует success jobs/logs после отказа.
- **Пакет:** `phase2-scheduler-safety`.
- **Fix commit:** `a91116a14fea7e6df07bb2869b0ae726fe562edd`.
- **Regression tests:** `tests/unit/test_phase2_scheduler_safety.py::test_110_scheduled_program_never_admits_fault_zone`, `tests/unit/test_phase2_scheduler_safety.py::test_110_program_honors_failed_central_start`, `tests/unit/test_phase2_scheduler_safety.py::test_110_group_sequence_honors_failed_central_start`.

### #111 [medium] `services/zone_control.py:399` — exclusive_start_zone игнорирует результат publish_mqtt_value: при подтверждённом сбое доставки зона всё равно переводится в 'on' с audit_reason='mqtt_ack_on', API отвечает «started»

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Брокер недоступен (рестарт Mosquitto): пользователь жмёт «Старт» → get_or_create_mqtt_client возвращает None, _publish_one×2 = False → код всё равно пишет state='on' с аудитом «mqtt_ack_on» (ack не было), start_zone_orchestrated возвращает "started", UI показывает идущий полив, планируется auto-stop. Реле не получало команду; только через ~30-40с фоновый StateVerifier (3×10с) переведёт зону в fault — всё это время статус, аудит и ответ API лгут. В ветке peer-off: OFF пиру не доставлен → его zone_run закрыт 'ok' (confirmed=1 с реального старта), state='off', а реле физически открыто.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Failed zone ON или peer OFF переводит затронутую зону в fault, закрывает неуспешный run и возвращает failure вместо mqtt_ack_on/HTTP success.
- **Пакет:** `phase2-zone-state-safety`.
- **Fix commit:** `42142ddea539625fe948bdaeb358b338aff58cef`.
- **Regression tests:** `tests/unit/test_phase2_zone_state_safety.py::test_finding_111_failed_on_publish_is_not_reported_as_started`, `tests/unit/test_phase2_zone_state_safety.py::test_finding_111_failed_peer_off_aborts_exclusive_start`.

### #112 [medium] `services/zone_control.py:847` — Зона без MQTT-топика/сервера в группе с мастер-клапаном: старт открывает мастер, но stop_zone никогда не планирует его закрытие — клапан остаётся открытым

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ создал зону, забыл заполнить topic; группа с use_master_valve=1. Ручной старт зоны: мастер-клапан открывается (publish '1' на master-топик), зона застревает в 'starting' (переход в 'on' тоже под `if sid and topic:`). По истечении длительности auto-stop вызывает stop_zone: state 'starting'→'stopping'→'off', но блок `if sid and topic:` пропущен целиком — _maybe_schedule_master_close не вызван. Мастер-клапан остаётся физически открытым на неопределённый срок (магистраль под давлением), пока кто-то не запустит/остановит другую зону группы или не нажмёт стоп повторно (идемпотентная ветка).
- **Решение оркестратора:** A later hard-stop normally schedules master close, so indefinite opening requires an early manual stop plus removed/failed safety jobs; the immediate stop path is still wrong.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Master не открывается, если у зоны нет собственного control channel; каждый завершённый stop-path рассматривает master close независимо от zone topic.
- **Пакет:** `phase2-zone-state-safety`.
- **Fix commit:** `42142ddea539625fe948bdaeb358b338aff58cef`.
- **Regression tests:** `tests/unit/test_phase2_zone_state_safety.py::test_finding_112_missing_zone_topic_never_opens_master`, `tests/unit/test_phase2_zone_state_safety.py::test_finding_112_stop_without_zone_topic_still_schedules_master_close`.

### #113 [medium] `services/sse_hub.py:206` — SSE-хаб инвертирует master_valve_observed для мастер-клапанов в режиме NO: эхо '1' всегда трактуется как 'open', игнорируя master_mode

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Группа с master_mode='NO'. _do_close публикует '1' (закрыть), пишет master_valve_observed='closed' и рассылает mv_state='closed'; через мгновение реле эхо-репортит '1' → sse_hub перезаписывает observed='closed'→'open' и шлёт клиентам mv_state='open'. Статус-страница постоянно показывает мастер-клапан «открыт», когда он закрыт, и «закрыт», когда открыт — оператор видит ложную тревогу/ложное спокойствие по всем NO-группам, а последнее слово всегда за инвертированным эхом.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Master echo интерпретируется с учётом NC/NO mode; для NO relay-high означает closed, а relay-low — open.
- **Пакет:** `phase2-sse-safety`.
- **Fix commit:** `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`.
- **Regression tests:** `tests/unit/test_phase2_sse_hub.py::test_master_only_tls_server_subscribes_with_safe_keepalive_and_no_mode`.

### #114 [medium] `routes/mqtt_api.py:213` — GET /api/mqtt/<id>/status — единственный MQTT-эндпоинт без @admin_required: аноним инициирует блокирующие подключения к брокеру с сохранёнными креденшалами

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Неаутентифицированный клиент в LAN циклически дёргает GET /api/mqtt/1/status: каждый запрос — блокирующий TCP+MQTT connect (до ~5с paho connect-timeout при чёрной дыре/фильтрации). На Wirenboard с малым числом hypercorn-воркеров несколько параллельных запросов исчерпывают воркеры — статус-страница и управление перестают отвечать. Заодно аноним получает оракул доступности внутреннего брокера (connected true/false) перебором server_id.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** MQTT status endpoint требует admin до чтения сохранённых credentials или попытки подключения.
- **Пакет:** `phase2-mqtt-safety`.
- **Fix commit:** `7d750b49fe5612f52d6121bdb4c923f5e42df3ad`.
- **Regression tests:** `tests/api/test_phase2_mqtt_api.py::test_finding_114_status_requires_admin_before_loading_credentials`.

### #115 [low] `services/zone_control.py:568` — Перезапуск уже включённой зоны снимает zone_hard_stop чужой зоны: подстрочное сравнение id джоба без разделителя (зона 5 матчит zone_hard_stop:51)

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Зоны 5 и 51 (разные группы) поливаются одновременно, у обеих взведён zone_hard_stop. Пользователь повторно жмёт «Старт» на уже включённой зоне 5 (restart_if_on/override) → ветка already-ON удаляет и "zone_hard_stop:5", и "zone_hard_stop:51". Зона 51 теряет страховочный hard-stop: если её штатный zone_stop не сработает (снят/упал), полив продолжается до 4-часового cap вместо hard-stop.
- **Дубль:** `#3`.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Already-on reschedule удаляет zone_stop по точному prefix с разделителем и zone_hard_stop только по полному ID, не затрагивая зоны с общим числовым префиксом.
- **Пакет:** `phase2-zone-state-safety`.
- **Fix commit:** `42142ddea539625fe948bdaeb358b338aff58cef`.
- **Regression tests:** `tests/unit/test_phase2_zone_state_safety.py::test_finding_3_restart_removes_only_exact_hard_stop`.

### #116 [low] `routes/mqtt_api.py:340` — scan-sse: тред MQTT-клиента обёрнут в `except queue.Full` — сбой connect убивает тред без сообщения, SSE вечно шлёт ping по мёртвому скану

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ открывает «Сканирование топиков» для брокера, который сейчас перезапускается/недоступен: клиенту уходит event:open (успех), тред падает на connect мгновенно и молча. UI показывает идущий скан, данные не приходят, ошибки нет; соединение висит до ручного закрытия вкладки, занимая слот per-IP лимита (2) и HTTP-воркер. Отличается от ранее найденного 300-секундного замерзания: здесь скан мёртв с нулевой секунды из-за слишком узкого except.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Ошибки connect в scan worker превращаются в безопасный terminal SSE error, после которого stream заканчивается и слот освобождается.
- **Пакет:** `phase2-mqtt-safety`.
- **Fix commit:** `7d750b49fe5612f52d6121bdb4c923f5e42df3ad`.
- **Regression tests:** `tests/api/test_phase2_mqtt_api.py::test_finding_116_scan_connect_error_is_reported_and_stream_ends`.

### #117 [low] `services/sse_hub.py:462` — Эвикция SSE-клиента при полной очереди теряет sentinel: вытесненный клиент навсегда остаётся «живым» зомби-соединением без обновлений

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** 20 вкладок/устройств, подключается 21-я: вытесняется самый старый клиент, чья очередь как раз забита бурстом событий. put_nowait(None) → queue.Full → pass. Вкладка-жертва держит SSE-соединение, получает только пинги: состояние зон в этом UI молча перестаёт обновляться навсегда (EventSource не реконнектится — соединение формально здорово), плюс HTTP-воркер занят зомби-стримом. Лечится только ручной перезагрузкой страницы.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Полная очередь вытесненного SSE-клиента дренируется и немедленно получает terminal sentinel, поэтому generator завершается вместо вечного zombie connection.
- **Пакет:** `phase2-sse-safety`.
- **Fix commit:** `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`.
- **Regression tests:** `tests/unit/test_phase2_sse_hub.py::test_evicted_full_client_queue_gets_immediate_terminal_sentinel`.

### #118 [medium] `routes/zones_watering_api.py:293` — Общий rate-limit-бакет «mqtt_control» (10 req/мин на IP) объединяет старт и СТОП зоны — ручная остановка получает 429, зона продолжает поливать

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь с 5+ зонами вручную включает 5 зон (5 запросов) и начинает их выключать: 5 стопов исчерпывают лимит ровно на 10; при 6 зонах или одном рестарте (stop+start) 11-й запрос — команда ВЫКЛЮЧЕНИЯ — получает 429 «Too many requests». UI показывает ошибку и откатывает оптимистичное состояние, реле остаётся открытым до срабатывания auto-stop таймера (до 120 мин), выключить зону из UI нельзя до минуты. За nginx (все клиенты в одном ведре — известная проблема ProxyFix) порог достигается ещё быстрее.
- **Disposition Фаз 2–3:** `pending`.

### #119 [medium] `static/js/zones.js:102` — Таблица зон: select MQTT-сервера без пустой опции — зона с mqtt_server_id=NULL показывается привязанной к первому серверу, и любое автосохранение молча записывает эту привязку

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** В системе 2 MQTT-сервера. Зона создана без сервера (бэкенд явно это допускает и предупреждает). На странице /zones колонка MQTT показывает сервер №1, как будто он назначен. Пользователь исправляет только имя или длительность зоны → debounce-автосохранение отправляет PUT /api/zones/<id> с mqtt_server_id=<первый сервер> — зона молча привязывается к серверу, который никто не выбирал; команды реле пойдут на чужой брокер.
- **Disposition Фаз 2–3:** `pending`.

### #120 [low] `templates/mqtt.html:219` — mqtt.html: каждый вызов loadServers() порождает новую вечную цепочку updateStatuses — поллинг статусов брокеров множится с каждым сохранением сервера

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ на странице MQTT сохраняет настройки сервера 5 раз → 6 параллельных вечных циклов; при 2 серверах это 12 реальных TCP-коннектов к брокерам каждые 5 секунд с одного вкладки, каждый занимает воркер hypercorn (до 5 с на мёртвом брокере) и каждый коннект с client_id боевого сервера дополнительно выбивает live-подписку SSE-хаба (session takeover). Деградация растёт с каждым сохранением до перезагрузки страницы.
- **Дубль:** `#103`.
- **Disposition Фаз 2–3:** `pending`.

### #121 [low] `templates/mqtt.html:154` — mqtt.html: создание/сохранение MQTT-сервера молча проглатывает ошибку сервера; нечисловой порт даёт непойманный TypeError → 500

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь вводит в поле Port значение «abc» (или вставляет строку с пробелом) и жмёт «Создать»: запрос падает 500, в UI нет ни тоста, ни ошибки — кнопка просто «не работает», сервер не создан, причина не видна. То же при любом отклонённом сохранении строки таблицы.
- **Disposition Фаз 2–3:** `pending`.

### #122 [low] `templates/settings.html:209` — Контракт минимальной длины пароля разъехался: UI обещает и валидирует «≥ 4 символа», бэкенд требует MIN_PASSWORD_LENGTH = 8

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Пользователь по подсказке страницы придумывает 6-символьный пароль, проходит клиентскую валидацию и все три поля, получает от сервера отказ «Пароль должен быть не менее 8 символов» — противоречащий тексту прямо над формой. Любой пароль 4–7 символов гарантированно отбивается вопреки UI-контракту.
- **Disposition Фаз 2–3:** `pending`.

### #123 [low] `templates/settings.html:421` — Сохранение координат погоды: parseFloat обрезает запятую-десятичную, а результат PUT /api/settings/location игнорируется — «Настройки погоды сохранены» при потерянных/искажённых координатах

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Русскоязычный пользователь вводит широту «55,7558» (запятая — стандартный десятичный разделитель), жмёт «Сохранить» → сохраняется широта 55.0 (ошибка ~80 км), сообщение — «Настройки погоды сохранены». При нечисловом вводе координата вовсе не записывается, но UI также рапортует успех; погодная коррекция работает по чужой точке или не работает.
- **Disposition Фаз 2–3:** `pending`.

### #124 [low] `static/js/status.js:2262` — Запуск «всех групп» показывает «▶ Все группы запущены» безусловно — отказы отдельных групп (400/аварийный режим/занятый секвенсор) проглатываются

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** В одной из трёх групп уже идёт сессия полива (или планировщик вернул False, или включён EMERGENCY_STOP между рендером и кликом) → часть/все запросы отвечают 400, но пользователь видит зелёный тост «Все группы запущены: 20 мин» и уходит; фактически группы не политы, никакого признака сбоя нет (в отличие от одиночного и run-selected путей, которые проверяют data.success).
- **Disposition Фаз 2–3:** `pending`.

### #125 [low] `routes/programs_api.py:62` — PUT /api/programs/<id> не прогоняет _validate_program_v2_fields (в отличие от POST) и обращается к data["time"]/["zones"]/["days"] без .get — асимметричный контракт: апдейтом сохраняется программа, которую планировщик молча не планирует; частичный PUT падает 500

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** API-клиент (или будущий фронтенд-код) шлёт PUT {schedule_type:'interval'} без interval_days или с опечаткой 'даily': POST такой payload отбил бы 400, PUT возвращает 200 и обновлённую программу — планировщик молча не создаёт для неё jobs, полив по программе прекращается без какого-либо сигнала. Частичный PUT без поля time (легальный для «обновить только имя») вместо ответа падает 500 KeyError.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Частичный PUT сливается с сохранённой программой перед validation/conflict check и больше не обращается к отсутствующим ключам напрямую.
- **Пакет:** `phase2-programs-web`.
- **Fix commit:** `d0a30b346c1f520b9368ca39d8b6f97b0b3c0e1c`.
- **Regression tests:** `tests/api/test_phase2_programs_web.py::TestPutValidationParity::test_partial_put_uses_stored_schedule_fields_instead_of_raising_keyerror`, `tests/api/test_phase2_programs_web.py::TestPutValidationParity::test_put_switch_to_interval_requires_interval_days`.

### #127 [high] `irrigation_scheduler.py:1153` — PUT программы с пустыми zones/days оставляет старые персистентные cron-джобы — «фантомная» программа поливает прежний список зон и не удаляется

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Программа 5 (Пн–Пт 06:00, зоны [1,2,3], джобы в jobs.db). PUT {"zones": []} → 200, БД обновлена, schedule_program выходит до cancel_program → старые cron-джобы с args=[5,[1,2,3],…] продолжают каждый будний день поливать зоны 1–3, хотя в UI программа без зон. После рестарта load_programs снова выходит рано → program_jobs[5] не заполняется → DELETE программы вызывает cancel_program, который не снимает ни одного джоба: осиротевшие персистентные джобы поливают неограниченно долго, убрать их можно только удалением jobs.db вручную.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Invalid/empty/disabled program update отменяет jobs, а cancel сканирует jobstore по program prefix даже при потере in-memory registry.
- **Пакет:** `phase2-scheduler-safety`.
- **Fix commit:** `a91116a14fea7e6df07bb2869b0ae726fe562edd`.
- **Regression tests:** `tests/unit/test_phase2_scheduler_safety.py::test_127_invalid_update_removes_existing_persistent_jobs`, `tests/unit/test_phase2_scheduler_safety.py::test_127_cancel_scans_jobstore_when_in_memory_registry_was_lost`.

### #128 [medium] `irrigation_scheduler.py:2298` — recover_missed_runs: при перекрытии окон main и extra_times берётся max(candidates) — восстанавливается не тот запуск, уже политые зоны поливаются повторно

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Программа: main 06:00, extra_times ["06:30"], зоны A/B/C по 20 мин (total 60). Краш в 06:20 (зона A полита полностью), рестарт в 06:40. Оба окна (06:00–07:00 и 06:30–07:30) содержат 06:40 → выбран старт 06:30, elapsed=10 → start_idx=0 → recovery заново поливает A, затем B и C со сдвигом на 40 минут. Корректное возобновление прерванного 06:00-запуска (elapsed=40) — с зоны C. До ветки (main учитывал только основное время) такая мисатрибуция была невозможна.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** При overlap main/extra recovery выбирает самое раннее допустимое окно — то, которое владело single session, — и продолжает только его хвост.
- **Пакет:** `phase2-scheduler-safety`.
- **Fix commit:** `a91116a14fea7e6df07bb2869b0ae726fe562edd`.
- **Regression tests:** `tests/unit/test_phase2_scheduler_safety.py::test_128_overlapping_extra_window_does_not_recover_from_later_skipped_fire`.

### #129 [medium] `db/programs.py:216` — check_program_conflicts не видит extra_times — пересечение по extra-слоту проходит валидацию, а на fire-time вся программа молча пропускается guard'ом

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Программа A: 06:00, зоны группы 1, суммарно 60 мин. Программа B: main 21:00, extra_times ["06:30"], зоны той же группы. Конфликт-чекер сравнивает 06:00 с 21:00 → «нет конфликтов», сохранение проходит и в мастере, и на сервере. Каждое утро fire B в 06:30 попадает в guard (сессия A активна) и пропускается ЦЕЛИКОМ (включая зоны B из других групп): утренний полив зон B не происходит, без ретрая и без сигнала в UI — только запись prog_skipped_manual_running в логе.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Все primary/extra candidate starts проверяются против всех primary/extra stored starts, поэтому пересечение extra slot блокируется до scheduler fire-time.
- **Пакет:** `phase2-programs-web`.
- **Fix commit:** `684d0cd4d1ea8fb445c9035dabedadc2a2c70b44`.
- **Regression tests:** `tests/unit/test_phase2_db_programs.py::test_conflict_check_accepts_candidate_extra_times`, `tests/api/test_phase2_programs_web.py::TestCandidateExtraTimesConflictChecks`.

### #130 [low] `db/zones.py:867` — compute_next_run_for_zone: postpone-якорь не сдвигается на offset зоны — scheduled_start_time уводит на следующий день, хотя планировщик польёт сегодня

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Ежедневная программа 06:00, зоны A(30м), B(30м), C с offset 60 (слот 07:00). Зоне C поставлена отложка до 06:30. Планировщик реально польёт C сегодня в 07:00 (отложка истечёт к её слоту). reschedule_group_to_next_program → compute_next_run_for_zone ищет старт после 06:30 → завтра 06:00 → пишет zones.scheduled_start_time = завтра 07:00. Карточка зоны (/api/zones/<id>/next-watering, через исправленный next_watering) показывает сегодня 07:00 — расписание группы противоречит и карточке, и фактическому поливу.
- **Disposition Фаз 2–3:** `pending`.

### #131 [low] `services/next_watering.py:157` — next_watering: in-progress-окно программы строится по номинальным длительностям — при погодном коэффициенте <100% зонам показываются слоты, которых не будет

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Погодный коэффициент 50%, программа 06:00, зоны A/B/C по 20 мин (номинал 60, факт 30). В 06:35 полив уже завершён, но in_progress=True до 07:00: для зоны C (off_min 40 ≥ elapsed 35) карточка показывает «следующий полив сегодня 06:40» — слот, который никогда не наступит; и во время самого полива слоты B/C показываются на 10–20 минут позже реальных стартов. Самокоррекция — только после окончания номинального окна.
- **Disposition Фаз 2–3:** `pending`.

### #132 [high] `services/zone_control.py:831` — stop_zone/exclusive_start_zone игнорируют результат publish_mqtt_value — при сбое доставки OFF зона помечается 'off', реле остаётся физически открытым, и все страховочные стоп-слои слепнут

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Зона поливается; в момент автостопа брокер MQTT недоступен (роутер перезагружается). publish_mqtt_value возвращает False, но stop_zone пишет state='off', watering_start_time=None. Реле физически включено. Далее hard-stop и cap-джобы вызывают stop_zone(force=False) → идемпотентный ранний выход БЕЗ повторной публикации OFF; ZoneWatchdog зону со state='off' не видит; сторожок эксклюзивности считает только 'on'. Единственное окно восстановления — 3 ретрая StateVerifier (~30 с); если брокер лежит дольше, OFF больше никто никогда не переиздаёт — клапан открыт до ручного вмешательства или ребута (перелив/затопление). Зеркальный кейс на старте: ON не доставлен, но state='on' — UI показывает полив, zone_run открывается, грядка сухая.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Failed OFF остаётся state=fault/physically unknown, не закрывает активный run как успех и позволяет последующим safety-stop снова публиковать OFF.
- **Пакет:** `phase2-zone-state-safety`.
- **Fix commit:** `42142ddea539625fe948bdaeb358b338aff58cef`.
- **Regression tests:** `tests/unit/test_phase2_zone_state_safety.py::test_finding_132_failed_off_publish_stays_fault_and_retries`.

### #133 [high] `irrigation_scheduler.py:835` — EMERGENCY_STOP не блокирует запуски по расписанию: планировщик не проверяет флаг, а cancel_group_jobs гасит только уже идущие сессии — программа стартует полив во время активного аварийного режима

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** 13:00 — прорвало трубу, пользователь жмёт «Аварийная остановка» (EMERGENCY_STOP=True, всё закрыто), закрывает ноутбук и уезжает за фитингами. 20:00 — cron-джоб программы срабатывает штатно: _run_program_threaded не находит активных сессий (guard пуст), открывает мастер-клапан, публикует ON и последовательно поливает все зоны группы полную длительность. Вода хлещет из разрыва при «активном» аварийном режиме; UI при этом продолжает показывать emergency-баннер и отклонять только РУЧНЫЕ старты.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Cron program/group runners проверяют process-local EMERGENCY_STOP до DB/hardware и повторно перед каждым ON.
- **Пакет:** `phase2-scheduler-safety`.
- **Fix commit:** `a91116a14fea7e6df07bb2869b0ae726fe562edd`.
- **Regression tests:** `tests/unit/test_phase2_scheduler_safety.py::test_133_emergency_flag_blocks_cron_runner_before_db_or_hardware`.

### #134 [medium] `services/sse_hub.py:151` — SSE-хаб — единственный подписчик на эхо реле — запускается только при первом открытии страницы: после рестарта сервиса без веб-клиента внешние включения реле невидимы и никогда не останавливаются

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Контроллер перезагрузился ночью (обновление/сбой питания); веб-интерфейс никто не открывал. Утром садовник включает реле зоны кнопкой на Wirenboard (или приходит retained '1' со старого брокера). Ни один компонент не подписан на топик: в БД зона остаётся 'off', «удалённый» автостоп, который обязан взвести хаб, не планируется, ZoneWatchdog зону не видит. Реле остаётся включённым неограниченно — до ручного выключения или следующего ребута. Дополнительно до первого открытия UI не работают emergency-override и обновление observed_state/master_valve_observed.
- **Решение оркестратора:** The hub is the only permanent semantics-aware subscriber; temporary verifier/scan clients do not close this gap.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Hub запускает постоянного safety subscriber при init приложения без ожидания первого browser SSE client; connect остаётся асинхронным и не задерживает boot.
- **Пакет:** `phase2-sse-safety`.
- **Fix commit:** `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`.
- **Regression tests:** `tests/unit/test_phase2_sse_hub.py::test_init_starts_hub_without_waiting_for_first_sse_client`.

### #135 [medium] `routes/groups_api.py:527` — Ручное открытие мастер-клапана не отменяет взведённый таймер отложенного закрытия — клапан молча закрывается через delay после открытия

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Полив зоны закончился в 10:00:00 — stop_zone взводит закрытие мастера на 10:01:00. В 10:00:30 пользователь через UI открывает мастер-клапан вручную (набрать воды из крана за мастером / проверка линии) — API отвечает success, observed='open'. В 10:01:00 отложенный _do_close видит «ни одной зоны on/starting» и закрывает клапан: вода у пользователя пропадает через полминуты без каких-либо действий с его стороны, в UI клапан «сам» становится closed.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** OPEN route входит в P05 master_topic_lock, снимает зарегистрированный pending close и только затем публикует OPEN; firing timer и ручная команда больше не могут пересечься в обратном порядке.
- **Пакет:** `phase2-master-route`.
- **Fix commit:** `b519a57e25a92873305d66f26cd09382a8880bc3`.
- **Regression tests:** `tests/api/test_groups_api.py::TestMasterValveAPI::test_manual_open_cancels_pending_close_inside_master_topic_lock`.

### #136 [medium] `services/zone_control.py:568` — Перезапуск уже включённой зоны снимает hard-stop джобы ЧУЖИХ зон: substring-матч 'zone_hard_stop:1' совпадает с 'zone_hard_stop:10..19'

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Зоны с id 1 и 12 в разных группах; зона 12 поливается (взведены zone_stop:12:* и zone_hard_stop:12), зона 1 тоже включена. Пользователь повторно жмёт «Старт» на зоне 1 (restart_if_on → ветка reschedule). Цикл удаления снимает zone_hard_stop:12 вместе с джобами зоны 1 и пересоздаёт их только для зоны 1. Зона 12 остаётся без hard-stop страховки: если её обычный zone_stop сорвётся (исключение в джобе, сбой БД), реле будет работать до cap-лимита 240 минут вместо запланированного времени.
- **Дубль:** `#3`.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Already-on reschedule удаляет zone_stop по точному prefix с разделителем и zone_hard_stop только по полному ID, не затрагивая зоны с общим числовым префиксом.
- **Пакет:** `phase2-zone-state-safety`.
- **Fix commit:** `42142ddea539625fe948bdaeb358b338aff58cef`.
- **Regression tests:** `tests/unit/test_phase2_zone_state_safety.py::test_finding_3_restart_removes_only_exact_hard_stop`.

### #137 [low] `routes/zones_photo_api.py:223` — Загрузка изображений: PIL DecompressionBombError не перехватывается — 500 вместо 400 (заявленный 50 MP декомпрешн-кап обходится)

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Админ (эндпоинты admin-only) загружает сплошного цвета PNG 20000x10000 = 200M пикс. (сжимается в <1 МБ, проходит 20 МБ лимит) на /api/zones/<id>/photo или /api/map. Ожидается аккуратный 400 IMAGE_TOO_LARGE, но `Image.open()` бросает DecompressionBombError, которое не перехвачено → 500 и шумный трейс в логах; контракт 'enforce a 50 MP decompression-bomb cap' из docstring image_pipeline не выполняется.
- **Решение оркестратора:** Pillow rejects the image, so this is a 500-vs-400 contract bug, not a successful cap bypass.
- **Disposition Фаз 2–3:** `pending`.

### #138 [low] `services/postpone.py:34` — /api/postpone: параметр days не валидируется — ValueError/OverflowError → 500 на гость-доступном CSRF-exempt эндпоинте

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent Phase 1 roles confirmed the current code path, realistic reachability, and the concrete user, hardware, availability, or test-quality effect. Detailed evidence and qualification are preserved in PHASE1-RAW-REPORTS-2026-07-18.md.
- **Репродукция/сценарий:** Клиент (за nginx basic-auth, роль guest) шлёт POST /api/postpone {"action":"postpone","group_id":1,"days":"abc"} или days:99999999999999 → 500 Internal Server Error вместо контрактного 400. Значение postpone_date вычисляется до записей в БД, поэтому частичного состояния нет, но эндпоинт тривиально роняется в 500 некорректным вводом.
- **Disposition Фаз 2–3:** `pending`.

### #139 [medium] `routes/system_status_api.py:318-385; services/next_watering.py:177-200; db/programs.py:460-471` — Публичный /api/status создаёт Z×P отдельных SQLite-запросов отмен программ

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Independent instrumentation reproduced the exact query count through the real blueprint.
- **Репродукция/сценарий:** Create 32 zones and 16 all-zone interval programs, GET /api/status, count is_program_run_cancelled_for_group calls: 512.
- **Решение оркестратора:** New diff finding; not a duplicate of the anonymous MQTT status endpoint.
- **Disposition Фаз 2–3:** `pending`.

### #140 [low] `routes/programs_api.py:294-327; tests/api/test_programs_api_v2.py:396-459` — Program log/stats API остаются заглушками, но ветка сняла xfail ложными shape-only тестами

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug_with_branch_false_green_tests`.
- **Верификация:** Both reviewers reproduced false empty/zero results after completed runs.
- **Репродукция/сценарий:** Record program_start/program_finish twice; GET log and stats still reports no runs.
- **Disposition Фаз 2–3:** `deferred`.
- **Причина disposition:** Текущие zone_runs не хранят program_id/run identity, а program_start/program_finish logs не связывают overlapping/restarted executions и не содержат требуемых water/duration metrics. Сначала нужно определить semantics, добавить program_runs либо эквивалентный execution identity и scheduler writers; shape-only заглушки не заменяются вымышленными значениями.
- **Пакет:** `phase2-programs-web`.

### #141 [medium] `irrigation_scheduler.py:801-880,1140-1294; tests/unit/test_scheduler_v2.py:401-444` — Типы программ smart и time-based фактически не различаются, а тесты проверяют только наличие job

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug_with_branch_false_green_tests`.
- **Верификация:** Code introspection and targeted tests confirmed type is never consumed.
- **Репродукция/сценарий:** Schedule otherwise identical smart and time-based programs; both execute the same weather path.
- **Disposition Фаз 2–3:** `deferred`.
- **Причина disposition:** В продукте не определено, чем runtime smart должен отличаться от time-based: нет smart decision inputs, scheduling policy или UI promise. Реализация без отдельной спецификации выдумала бы поведение; H2 включать запрещено.
- **Пакет:** `phase2-scheduler-safety`.

### #142 [low] `tests/unit/test_scheduler_v2.py:51-78,215-314` — CronTrigger-тесты не проверяют weekday/even-odd/extra-time поля и пропускают полностью неверное расписание

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_test_regression`.
- **Верификация:** Independent mutation-style checks demonstrated the tests do not distinguish correct triggers.
- **Репродукция/сценарий:** Replace every constructed trigger with Tuesday 23:59 in a monkeypatch; the three semantic tests still pass.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Weekday/even/odd/main/extra jobs проверяют точные day/day_of_week/hour/minute CronTrigger fields и полный ожидаемый набор job IDs вместо shape-only наличия.
- **Пакет:** `phase2-test-quality`.
- **Fix commit:** `9d65cb6f41894ca16712c86e4d54c2a197be8472`.
- **Regression tests:** `tests/unit/test_scheduler_v2.py::TestScheduleWeekdaysProgram::test_weekdays_program_has_correct_trigger`, `tests/unit/test_scheduler_v2.py::TestScheduleEvenOddProgram::test_even_odd_program_has_correct_cron_days[even]`, `tests/unit/test_scheduler_v2.py::TestScheduleEvenOddProgram::test_even_odd_program_has_correct_cron_days[odd]`, `tests/unit/test_scheduler_v2.py::TestExtraTimes::test_schedule_extra_times`.

### #143 [medium] `routes/programs_api.py:25-43,113-150; irrigation_scheduler.py:1242-1268` — POST programs принимает невалидный interval_days: отрицательное значение зацикливает APScheduler, ноль даёт интервал 1 секунду

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Safe API probes and APScheduler source/runtime inspection confirmed all invalid conversions and effects.
- **Репродукция/сценарий:** POST interval_days=-1 or '0' through an admin test client; observe 201 and unsafe stored trigger value.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** POST и PUT принимают только non-boolean integer interval_days в безопасном диапазоне 1..30, исключая секундный trigger и отрицательный APScheduler loop.
- **Пакет:** `phase2-programs-web`.
- **Fix commit:** `d0a30b346c1f520b9368ca39d8b6f97b0b3c0e1c`.
- **Regression tests:** `tests/api/test_phase2_programs_web.py::TestIntervalDaysValidation`.

### #144 [low] `services/weather/__init__.py:3-39; deleted services/weather_merged.py; specs/weather-integration-spec.md` — Ветка удалила объявленный stable weather API get_merged_weather и compatibility-модуль services.weather_merged

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Both public import paths were smoke-tested and compared with the explicit main contract.
- **Репродукция/сценарий:** Import get_merged_weather from services.weather and import services.weather_merged; both fail on the branch.
- **Disposition Фаз 2–3:** `pending`.

### #145 [high] `routes/programs_api.py:84-102; irrigation_scheduler.py:1165-1203,1299-1313` — Конкурентные PUT/DELETE программы оставляют persistent orphan APScheduler jobs

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** A deterministic barrier reproduced a job present while its DB program no longer existed.
- **Репродукция/сценарий:** Pause PUT after DB commit, complete DELETE/cancel, resume PUT scheduling; observe an orphan program job.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Schedule/cancel сериализованы per scheduler lock, schedule повторно читает DB revision, а job callable валидирует текущую enabled program/zones; DELETE выигрывает у stale PUT continuation.
- **Пакет:** `phase2-scheduler-safety`.
- **Fix commit:** `a91116a14fea7e6df07bb2869b0ae726fe562edd`.
- **Regression tests:** `tests/unit/test_phase2_scheduler_safety.py::test_145_delete_wins_over_stale_put_scheduler_continuation`.

### #146 [high] `irrigation_scheduler.py:1552-1663,2130-2180; routes/groups_api.py:194-196` — Stop во время инициализации group start теряется, после успешной остановки создаётся новый sequence job

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** A barrier before event registration reproduced a post-stop unset event and newly created group_seq job.
- **Репродукция/сценарий:** Pause group start before setdefault, call cancel_group_jobs, then resume; group_seq job appears after stop.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Group Event регистрируется до initialization, cancel видит его немедленно, а все pre-job границы повторно проверяют Event/shutdown/emergency и identity-safe освобождают владельца.
- **Пакет:** `phase2-scheduler-safety`.
- **Fix commit:** `a91116a14fea7e6df07bb2869b0ae726fe562edd`.
- **Regression tests:** `tests/unit/test_phase2_scheduler_safety.py::test_146_stop_during_group_initialization_prevents_late_sequence_job`, `tests/unit/test_phase2_scheduler_safety.py::test_group_runner_early_exit_releases_owned_session_event[emergency]`.

### #147 [high] `routes/telegram.py:167-175; irrigation_scheduler.py:1741-2035` — Telegram «Остановить группу» выключает текущую зону, но не отменяет runner — следующая зона запускается снова

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** A two-zone runner probe produced starts for both zones after Telegram reported success.
- **Репродукция/сценарий:** Run a two-zone sequence, invoke Telegram group stop during zone one, observe zone two still starts.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** Telegram Stop вызывает cancel_group_jobs, который ставит Event активного runner, останавливает текущие zones и снимает jobs; sequencer больше не продолжает следующей зоной.
- **Пакет:** `phase2-telegram-stop`.
- **Fix commit:** `40b961f7d3761c8752c25886570b6568c8ec8531`.
- **Regression tests:** `tests/unit/test_phase2_telegram_stop.py::test_group_stop_cancels_scheduler_session`, `tests/unit/test_phase2_telegram_stop.py::test_group_stop_falls_back_to_zone_control_without_scheduler`.

### #148 [medium] `routes/system_status_api.py:139-157; irrigation_scheduler.py:733-744,2083,2130-2188` — Health cancel создаёт ownerless group_cancel_event и блокирует будущие cron-программы группы

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Two independent probes reproduced the persistent set event and prog_skipped_manual_running.
- **Репродукция/сценарий:** Health-cancel a standalone manual zone, then run a scheduled program for its group; it skips and the event remains.
- **Disposition Фаз 2–3:** `pending`.

### #149 [high] `services/app_init.py:284-305; services/shutdown.py:83-167; irrigation_scheduler.py:436` — Shutdown выполняет финальный OFF без quiesce планировщика — worker может отправить retained ON после OFF

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_bug`.
- **Верификация:** Independent deterministic traces reproduced OFF, companion OFF, then a late retained ON.
- **Репродукция/сценарий:** Pause shutdown ACK wait, allow an existing runner to start the next zone, observe final base payload 1.
- **Disposition Фаз 2–3:** `fixed`.
- **Причина disposition:** P03 quiesce закрывает admission, отменяет sessions и ждёт admitted runners; P15 вызывает эту boundary до первой финальной QoS2 retained OFF, исключая поздний scheduler ON после OFF.
- **Пакет:** `phase2-app-lifecycle`.
- **Fix commit:** `dcd0e30ecea830f3e09968c40e50ca79da9c0875`.
- **Regression tests:** `tests/unit/test_phase2_scheduler_safety.py::test_149_quiesce_is_a_strict_fence_for_inflight_program_runner`, `tests/unit/test_phase2_app_lifecycle.py::test_shutdown_quiesces_scheduler_before_final_retained_off`, `tests/unit/test_phase2_app_lifecycle.py::test_initialize_completes_scheduler_boot_only_after_off_reconciliation`.

### #150 [low] `services/next_watering.py:23-36; services/weather/service.py:113-150; services/weather/cache.py:91-121` — Cache-only next-watering доверяет кэшу погоды любого возраста и расходится с live-решением планировщика

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `branch_regression`.
- **Верификация:** A stale-rain/live-dry probe produced opposite display and scheduler decisions.
- **Репродукция/сценарий:** Seed a 10-day rainy cache row, mock live dry weather, compare cache-only display skip with scheduler live skip.
- **Disposition Фаз 2–3:** `pending`.

### #151 [low] `services/next_watering.py:125-168,209-254` — Weather skip + программа 00:00 показывает первой зоне «Никогда»

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_root_branch_expanded`.
- **Верификация:** Frozen-time reproduction returned None with has_programs=True for a valid daily midnight program.
- **Репродукция/сценарий:** Freeze noon, set skip_today=True and a one-zone daily 00:00 program; compute next_dt returns None.
- **Решение оркестратора:** The root existed in main bulk; the branch expanded it to single and /api/status consumers.
- **Disposition Фаз 2–3:** `pending`.

### #152 [low] `services/next_watering.py:140-145; irrigation_scheduler.py:1210-1297` — Malformed program.time молча превращается в отображаемый midnight-run, которого scheduler не создаёт

- **Статус:** `VERIFIED_REAL`.
- **Происхождение:** `main_root_branch_expanded`.
- **Верификация:** The same malformed program produced a midnight display and an empty scheduler job list.
- **Репродукция/сценарий:** Store time='not-a-time'; calculator returns next midnight while _schedule_single_time creates no jobs.
- **Решение оркестратора:** The root existed in main bulk; the branch expanded it to single and /api/status consumers.
- **Disposition Фаз 2–3:** `pending`.

## Отклонённые — 5

### #16 [low] `routes/system_status_api.py:743` — /api/water: суточные корзины строятся по UTC-дате timestamp, а ось дат — по локальному времени контроллера

- **Статус:** `VERIFIED_FALSE`.
- **Происхождение:** `branch_regression`.
- **Верификация:** UTC/local mismatch exists in the dormant water_usage path, but production code has no writer or caller for add_water_usage and the bundled frontend does not consume /api/water. The claimed user-visible daily-bucket failure therefore cannot occur in the current product.
- **Репродукция/сценарий:** Контроллер Wirenboard в TZ UTC+6: полив, завершившийся в 05:30 локального 18-го числа, записан с timestamp 23:30 UTC 17-го → литры попадают в корзину «2026-07-17», хотя локально полив был 18-го. Все поливы первых 6 часов локальных суток систематически приписываются предыдущему дню в daily_usage (total_liters группы при этом корректен); zone_usage.last_used также отдаёт сырую UTC-строку без TZ-конверсии, в отличие от остальных таймстампов API (_zone_ts_to_iso).
- **Решение оркестратора:** Final FALSE because the affected data path has no production writer or consumer.
- **Disposition Фаз 2–3:** `not_applicable`.
- **Причина disposition:** Фаза 1 признала находку ложной; production-fix не применяется.
- **Пакет:** `phase1-verification`.

### #31 [low] `routes/system_status_api.py:743` — /api/water: суточные корзины смешивают UTC-даты строк water_usage с локальными датами меток

- **Статус:** `VERIFIED_FALSE`.
- **Происхождение:** `branch_regression`.
- **Верификация:** Duplicate of #16. water_usage is never populated by production code and the endpoint has no bundled frontend consumer, so the stated user-visible bucket mixing is not reachable.
- **Репродукция/сценарий:** Ночной полив в 00:30 местного времени (21:30 UTC предыдущего дня) записывает литры во вчерашнюю корзину графика расхода; «сегодня» показывает 0 л, вчерашний столбик завышен; last_used у зоны отображает время на 3 часа меньше фактического.
- **Дубль:** `#16`.
- **Disposition Фаз 2–3:** `not_applicable`.
- **Причина disposition:** Фаза 1 признала находку ложной; production-fix не применяется.
- **Пакет:** `phase1-verification`.

### #77 [low] `static/js/status.js:1000` — Регрессия ветки: удалён SSR statusData (_ssrStatus) — карточки групп пусты до ~1-2с, admin-кнопки и emergency-guard отсутствуют на первом рендере

- **Статус:** `VERIFIED_FALSE`.
- **Происхождение:** `branch_regression`.
- **Верификация:** The removed SSR path was already dead on main: routes/status.py imported a nonexistent build_status_response, caught ImportError, and always emitted an empty object. The short first-render gap exists on both revisions, but the claimed branch regression is false.
- **Репродукция/сценарий:** Открытие главной страницы: #groups-container (основной блок управления группами) пуст минимум 1с + RTT (на ARM-контроллере /api/status небыстрый) — на main рендерился мгновенно из SSR. renderZoneCards() на первом рендере видит isAdmin=false (statusData null) → у админа пропадают кнопки «✏️ Редактировать» и появляются с мерцанием через 1.5-3с; при активной аварийной остановке кнопки запуска зон на первом рендере не имеют emergency-guard (startAction ведёт на toggleZoneRun вместо предупреждения).
- **Решение оркестратора:** The branch removed dead SSR code; it did not introduce the already-existing first-render delay.
- **Disposition Фаз 2–3:** `not_applicable`.
- **Причина disposition:** Фаза 1 признала находку ложной; production-fix не применяется.
- **Пакет:** `phase1-verification`.

### #105 [medium] `static/js/status.js:393` — status.js: серверные planned_end_time/watering_start_time ('YYYY-MM-DD HH:MM:SS') парсятся new Date() без нормализации — на Safari/iOS таймер бегущей зоны показывает «NaN:NaN» и NaN%

- **Статус:** `VERIFIED_FALSE`.
- **Происхождение:** `main_bug`.
- **Верификация:** All production server timestamps reaching status.js are normalized by to_iso_with_tz to ISO 8601 with an explicit offset. The claimed raw-space datetime never reaches this consumer; the separate optimistic-client timestamp issue is #21.
- **Репродукция/сценарий:** Пользователь открывает статус-страницу с iPhone/Safari, любая зона запущена планировщиком (времена пришли с сервера, не оптимистичные): карточка зоны показывает «Осталось NaN:NaN NaN%», прогресс-бар пуст, и так до конца полива. Отличается от уже найденных Safari-багов (syncServerTime now_iso, history.js, оптимистичные UTC-записи) — здесь ломается основной таймер обратного отсчёта из серверных данных.
- **Решение оркестратора:** False for server-provided data; the separate optimistic-client path remains #21.
- **Disposition Фаз 2–3:** `not_applicable`.
- **Причина disposition:** Фаза 1 признала находку ложной; production-fix не применяется.
- **Пакет:** `phase1-verification`.

### #126 [high] `irrigation_scheduler.py:2380` — Boot-replay персистентного program-джоба гонится с recover_missed_runs — двойной параллельный запуск программы после рестарта

- **Статус:** `VERIFIED_FALSE`.
- **Происхождение:** `main_bug`.
- **Верификация:** Boot/recovery guards prevent duplicate execution in the real startup flow. Ten cold-boot probes produced one program_start each; duplication required an artificial barrier inside a tiny nonblocking window and is not a supported production scenario.
- **Репродукция/сценарий:** Сервис лежал 05:50–06:20, программа стартует в 06:00 (зоны 30+30 мин). При рестарте: (а) персистентный cron-джоб «program:N:main:dX» реплеится (misfire < 3600 c) и запускает ПОЛНУЮ программу с зоны 0; (б) recover_missed_runs видит now внутри окна [06:00, 07:00) и планирует «(recovered)»-запуск с индекса по elapsed=20 мин. Оба джоба уходят в один thread-pool, оба проходят неатомарный guard → две параллельные последовательности по одним зонам: каждая перед стартом своей зоны глушит peer-зоны группы (OFF в MQTT/БД), интерливинг ON/OFF, двойной полив и рассинхрон физического реле с БД.
- **Решение оркестратора:** Final FALSE; do not add a fix for the artificial micro-barrier scenario.
- **Disposition Фаз 2–3:** `not_applicable`.
- **Причина disposition:** Фаза 1 признала находку ложной; production-fix не применяется.
- **Пакет:** `phase1-verification`.
