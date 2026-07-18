# Phase 1 raw verifier reports — 2026-07-18

> Checkpoint only. These are refuter-role results, not final statuses. A finding becomes
> VERIFIED_REAL only after an independent practitioner also says it is realistic.

- Branch/HEAD: refactor/audit-2026-07 / ef830fa7d05c2f07590dfd87462327e68b8abf44
- Root Codex task: 019f741a-5cb1-7022-9727-ca1bc98f44e3
- Completed refuter verdicts: 87/113
- Refuter said real: 84; false: 3
- Refuter still pending: [20, 21, 23, 24, 25, 26, 27, 58, 60, 61, 74, 75, 77, 78, 79, 92, 93, 95, 105, 106, 107, 108, 119, 122, 123, 124]
- All 113 practitioner verdicts remain pending.
- All 19 CONFIRMED rechecks, 6 REFUTED rechecks, and 5 independent diff lenses remain pending.
- No production-code changes were made.

## Reports

### /root/a_sched_1_refuter

inventory_index: 133  
title: EMERGENCY_STOP не блокирует запуски по расписанию: планировщик не проверяет флаг, а cancel_group_jobs гасит только уже идущие сессии — программа стартует полив во время активного аварийного режима  
real: true  
reason: `EMERGENCY_STOP` выставляется в `routes/system_emergency_api.py:34-38`, после чего вызывается `cancel_group_jobs` (`:57-64`). Он ставит event только уже зарегистрированной сессии (`irrigation_scheduler.py:2138-2142`) и удаляет zone/group jobs, но не persistent `program:*` cron jobs (`:2162-2182`). Следующий cron tick через `job_run_program` без emergency-гейта входит в `_run_program_threaded` (`irrigation_scheduler.py:101-116,688-795`), создаёт новый незаданный event (`:786-792`) и открывает master/zone через `exclusive_start_zone` (`:992-996`; `services/zone_control.py:348-397`). Ручной API-гейт (`routes/programs_api.py:258-261`) к cron-пути неприменим. SSE-компенсатор (`services/sse_hub.py:223-230`) реактивен и необязателен: hub стартует только при открытии `/api/mqtt/zones-sse` (`routes/zones_watering_api.py:238-258`); status page, с которой доступен emergency stop, этот EventSource не открывает. Даже при активном hub сначала проходит физический ON, а `StateVerifier` способен позже повторить ON.  
introduced_by_branch: false — на `main` та же emergency/cancel/run цепочка; релевантные изменения ветки её не создавали.  
reproduction: В свежем процессе не открывать страницу зон; создать enabled-программу на ближайший cron slot, нажать emergency stop на status page при отсутствии активной групповой сессии и дождаться tick. `program:*` job остаётся, runner публикует master/zone ON при `EMERGENCY_STOP=True`.

inventory_index: 127  
title: PUT программы с пустыми zones/days оставляет старые персистентные cron-джобы — «фантомная» программа поливает прежний список зон и не удаляется  
real: true  
reason: PUT принимает `zones=[]`/`days=[]` и возвращает 200 (`routes/programs_api.py:60-94`); conflict-check для них возвращает пустой результат (`db/programs.py:253-256`). В `schedule_program` оба ранних выхода находятся до `cancel_program` (`irrigation_scheduler.py:1153-1166`). Старый persistent job хранит замороженные zones в args (`:1221-1238`) в `jobs.db` (`:312-345`). После рестарта `program_jobs` пуст (`:376`), invalid-программа снова выходит рано при `load_programs` (`:2218-2223`), boot cleanup не удаляет `program:*` (`:2339-2345`), а `cancel_program` знает только IDs из `program_jobs` (`:1299-1313`). Runner не перечитывает существование или зоны программы из БД (`:101-117,688-720,835-839`). Обычный wizard пустые значения блокирует (`templates/programs.html:545-570,616-619`), но server API/старый клиент — нет. Формулировка «убрать только удалением jobs.db» чрезмерна: отдельный job можно снять health API (`routes/system_status_api.py:120-133`).  
introduced_by_branch: false — порядок ранних return/cancel и PUT-поведение уже присутствуют на `main`.  
reproduction: Создать weekdays-программу с зонами `[1,2,3]`, затем выполнить PUT с `zones=[]` либо `days=[]`. Убедиться, что старый `program:<id>:...` job сохранил прежние args. Перезапустить сервис, затем удалить программу: `program_jobs` пуст, job остаётся и продолжает запускать прежние зоны.

inventory_index: 63  
title: start_group_sequence выключает бегущие зоны только в БД: без MQTT OFF и со снятием их стоп-джобов реле остаётся физически открытым и неуправляемым  
real: true  
reason: `start_group_sequence` выполняет только DB-off через `update_zone_state` (`irrigation_scheduler.py:1551-1567`); helper содержит DB update и аудит, но не MQTT/finish-run (`services/zones_state.py:108-168`). Затем снимаются `zone_stop`/`zone_hard_stop` и `active_zones` (`irrigation_scheduler.py:1607-1626`). Новый runner напрямую ставит следующую зону в DB-on и публикует ON (`:1807-1902`), без peer-off. Безопасный путь `cancel_group_jobs → stop_all_in_group(force=True)` существует (`:2138-2156`), но start call-sites его не вызывают (`routes/groups_api.py:408-410,689-697`; `routes/telegram.py:150-161`). Watchdogs доверяют `state == "on"` (`services/watchdog.py:82-85`; `app.py:537-539`), поэтому ложная DB-off зона невидима. `zone_cap_stop` остаётся, но также не спасает: он вызывает `stop_zone(force=False)`, чей DB-off branch возвращает без MQTT (`irrigation_scheduler.py:168-175`; `services/zone_control.py:793-815`). Для `run-selected`, не включающего текущую зону, bulk-off этой зоны не происходит; полный дефект воспроизводится через `start-from-first` либо subset, содержащий работающую зону.  
introduced_by_branch: false — bulk-off/cleanup блок идентичен `main`.  
reproduction: Штатно запустить позднюю в порядке группы зону Z5, затем вызвать `POST /api/groups/<gid>/start-from-first`. Z5 станет DB-off без MQTT OFF, её normal/hard stop jobs исчезнут. После ON первой зоны Z1 физически открыты Z1 и Z5, а оба watchdog видят только Z1.

inventory_index: 65  
title: start_group_sequence не сериализует сессии группы: повторный запуск (или запуск во время идущей программы) порождает два параллельных секвенсора, интерливящих ON/OFF по одним зонам  
real: true  
reason: Существующий cancel-event только переиспользуется; запуск не отклоняется, не отменяет и не ждёт владельца (`irrigation_scheduler.py:1591-1605`). Новый DateTrigger получает `group_seq:<gid>:<epoch-second>` (`:1643-1664`); для разных секунд это разные jobs, а `max_instances=1` действует на каждый job отдельно. Уже исполняющийся одноразовый DateTrigger отсутствует в `get_jobs`, поэтому cleanup (`:1607-1624`) его не снимает. Оба workers проходят независимый цикл (`:1741-2035`) и создают отдельные zone_runs (`:1905-1941`); уникального ограничения на открытый run нет (`db/zones.py:641-679`). Первый завершившийся worker также очищает общий event (`irrigation_scheduler.py:2063-2065`), пока второй может продолжать. `/start-from-first`, `run-selected` и Telegram не имеют session gate (`routes/groups_api.py:334-410,635-697`; `routes/telegram.py:150-160`). Guard `irrigation_scheduler.py:733-782` защищает только scheduled-after-manual, но не manual-after-scheduled. Нюанс: два вызова в одну epoch-секунду обычно сталкиваются по одинаковому job ID; нужны разные секунды либо ручной запуск во время программы.  
introduced_by_branch: false — весь релевантный `start_group_sequence` блок идентичен `main`.  
reproduction: В production-mode дважды вызвать `POST /api/groups/<gid>/start-from-first` с паузой 2 секунды либо вызвать его во время плановой программы. Появляются два одновременно работающих runner-а, повторные starts/zone_runs и преждевременные OFF первого runner-а внутри слота второго.

inventory_index: 87  
title: Recovery-запуск гонится с boot-ремедиацией: первая восстановленная зона молча «поливается» с выключенным реле/закрытым мастер-клапаном  
real: true  
reason: `init_scheduler` сначала запускает APScheduler и добавляет recovery, затем выполняет cleanup/stop (`irrigation_scheduler.py:2376-2400`). Recovery ставится на `DateTrigger(now)` уже работающему scheduler (`:2327-2330`), поэтому worker конкурирует с boot-потоком. После `init_scheduler` `_boot_sync` force-стопает группы, повторно публикует retained OFF и закрывает master (`services/app_init.py:52-80,129-188`). Runner при этом ждёт слот, проверяя только cancel/skip/shutdown (`irrigation_scheduler.py:1044-1060`); `stop_zone`/`stop_all_in_group` cancel-event не выставляют (`services/zone_control.py:764-914`). Поэтому после boot OFF runner продолжает считать зону активной. Уточнение: hard crash со stale `state='on'` обычно пропустит recovery из-за `irrigation_scheduler.py:2279-2281`; естественный сценарий — graceful systemd restart, поскольку shutdown предварительно пишет DB-off (`services/shutdown.py:149-167`). Также zone_run не обязательно будет `ok`: неподтверждённый ON понижается до `failed` (`db/zones.py:731-742`), а ON-verifier может позднее снова открыть реле (`services/observed_state.py:122-148`). Эти нюансы не устраняют гонку.  
introduced_by_branch: false — `main` содержит тот же `DateTrigger(now)` до boot cleanup/stop; ветка лишь расширила набор восстанавливаемых расписаний.  
reproduction: Выполнить graceful restart внутри окна выполнения программы, когда shutdown уже перевёл зону в DB-off. Замедлить boot MQTT sweep либо дождаться немедленного recovery start: recovery публикует ON, boot remediation затем публикует OFF/закрывает master, но runner остаётся в `active_zones`, его cancel-event не установлен и слот продолжает отсчитываться.

Рабочее дерево не изменялось; полный `pytest` не запускался.

### /root/a_sched_2_refuter

[
  {
    "inventory_index": 126,
    "title": "Boot-replay персистентного program-джоба гонится с recover_missed_runs — двойной параллельный запуск программы после рестарта",
    "real": false,
    "reason": "Replay действительно возможен: scheduler стартует до recovery (`irrigation_scheduler.py:2380-2391`), cron имеет grace 3600 секунд (`:1221-1231`). Но заявленный двойной запуск практически отсекается двумя защитами. Recovery не создаёт job, если replay уже выставил зоне `state='on'` (`:2268-2281`). Если оба job всё же submitted, runner проверяет group session на `:744` и почти сразу резервирует её атомарным `dict.setdefault` на `:788-792`; второй runner возвращается на `:745-782`. Утверждение inventory о десятках DB-чтений между check и reserve неверно: чтения находятся до guard (`:718-731`), между ним и `setdefault` нет I/O/lock wait. Natural stress из 50 000 синхронных попыток при стандартном CPython switch interval 5 ms дал `both_passed=0`; дефект воспроизводится только искусственным barrier внутри микроскопического окна. По правилу сомнение=>false это не практический дефект.",
    "introduced_by_branch": false
  },
  {
    "inventory_index": 88,
    "title": "recover_missed_runs повторно поливает уже завершившийся (укороченный погодой или остановленный вручную) запуск — окно строится по номинальным длительностям без проверки фактического завершения",
    "real": true,
    "reason": "Recovery проверяет только `state == 'on'` и суммирует номинальные duration (`irrigation_scheduler.py:2268-2283`), затем по ним вычисляет окно и start_idx (`:2293-2309`) и ставит остаток без проверки `zone_runs`/`program_finish` (`:2313-2331`). Реальный runner применяет погодный коэффициент (`:662-683`, вызов `:880`). Downstream-защита частичная: group-stop записывает persistent cancellation (`routes/groups_api.py:219-230`), которую runner проверяет (`irrigation_scheduler.py:845-852`), но погодное завершение не защищено; stop отдельной зоны сохраняет лишь in-memory cancel-event (`routes/zones_watering_api.py:401-421`).",
    "introduced_by_branch": false,
    "reproduction": "Зафиксировать время 2026-07-18 08:00; программа 06:00, восемь зон по 20 минут, коэффициент 50%, все зоны уже off и имеют завершённую историю до 07:20. Вызов recovery всё равно создаёт job с `zones=[7,8]`, повторно поливая последние две зоны."
  },
  {
    "inventory_index": 128,
    "title": "recover_missed_runs: при перекрытии окон main и extra_times берётся max(candidates) — восстанавливается не тот запуск, уже политые зоны поливаются повторно",
    "real": true,
    "reason": "Current собирает все перекрывающиеся `time + extra_times` и без факта реального запуска выбирает позднейший (`irrigation_scheduler.py:2284-2298`), после чего вычисляет хвост от неверной точки (`:2299-2309`). Main и extra — отдельные jobs (`:1190-1201`); пока main активен, extra закономерно пропускается guard’ом (`:744-782`), но recovery этого не знает. Перекрытие достижимо через API: conflict checker получает только main time (`routes/programs_api.py:70-72,127-129`), а DB-запрос даже не читает `extra_times` (`db/programs.py:216-223`). В `main` recovery использует только `p.time`; неоднозначности там нет.",
    "introduced_by_branch": true,
    "reproduction": "Main 06:00, extra 06:30, зоны A/B/C по 20 минут. Main работает в 06:30, поэтому extra пропускается guard’ом. Выполнить штатный stop/restart в 06:35–06:40: зоны становятся off. В 06:40 current recovery выбирает 06:30 и ставит `[A,B,C]`; логика main от 06:00 поставила бы только `[C]`. Исходный сценарий с crash в 06:20 следует заменить этим вариантом."
  },
  {
    "inventory_index": 66,
    "title": "Гонка recover_missed_runs со стартовой зачисткой: восстановленный запуск глушится stop_on_boot_active_zones/_boot_sync, первая зона не поливается, но получает фантомный zone_run 'ok'",
    "real": true,
    "reason": "Recovery ставит немедленный DateTrigger (`irrigation_scheduler.py:2312-2331`) до cleanup и forced stop (`:2376-2400`). Затем `_boot_sync` выполняет `stop_all_in_group`, retained OFF и закрытие master valves (`services/app_init.py:123-188`). Boot-пути не выставляют `group_cancel_events`, а runner во время слота проверяет только cancel/skip/shutdown (`irrigation_scheduler.py:1029-1060`), поэтому после принудительного OFF продолжает спать и затем переходит к следующей зоне. Ограничение: после abrupt crash сохранённый `state='on'` блокирует recovery (`:2279-2281`), но штатный SIGTERM переводит зоны в off (`services/shutdown.py:149-167`), поэтому `systemctl restart` реалистичен. `zone_run='ok'` условен: без подтверждённого ON он понижается до failed (`db/zones.py:731-743`), но при быстром MQTT echo перед boot-OFF остаётся ложный ok.",
    "introduced_by_branch": false,
    "reproduction": "На временной БД создать программу в текущем recovery-окне со всеми зонами off. Запустить recovery worker и остановить его barrier’ом сразу после подтверждённого `exclusive_start_zone`; в основном потоке выполнить `cleanup_jobs_on_boot`, `stop_on_boot_active_zones` и `_boot_sync`. Результат: зона off, закрытый confirmed `zone_run` со status ok, group Event существует, но не set, а runner остаётся жив до конца полного слота."
  },
  {
    "inventory_index": 90,
    "title": "schedule_program затирает zones.scheduled_start_time «сегодняшним» временем программы независимо от дней недели и прошедшего времени — карточка группы показывает ложную «Следующая: HH:MM»",
    "real": false,
    "reason": "Writer действительно сохраняет сегодняшнюю, возможно прошедшую дату (`irrigation_scheduler.py:1170-1183`), но заявленного потребителя и эффекта нет. Надпись «Следующая» находится не в карточке группы, а в sidebar активной зоны (`static/js/status.js:1385-1433`), который полностью скрывается при отсутствии активной зоны (`:1395-1397`). Карточки групп (`:294-332`) поле не читают. `queue_remaining` считается только при фактическом watering и использует значения как относительный порядок (`routes/system_status_api.py:458-475`). Корректный `next_start` группы рассчитывается независимо через `compute_next_watering` (`:375-385`). Следовательно, четверговый restart для программы только по понедельникам не показывает заявленную ложную карточку. Сырой DB-value остаётся латентной data-quality проблемой, но это отдельная находка.",
    "introduced_by_branch": false
  }
]

Точечная проверка `TestRecoverMissedRuns::test_recover_extra_time_window` прошла: `1 passed`. Репозиторий не изменялся; в status остался только исходный untracked `AGENTS.md`.

### /root/b_zone_1_refuter

[
  {
    "inventory_index": 64,
    "title": "StateVerifier ретраит устаревшую команду: фоновая верификация перекрывает более свежую противоположную команду и может закрыть/открыть клапан вопреки текущему состоянию",
    "real": true,
    "reason": "verify() читает зону только один раз и сохраняет expected; между попытками не проверяет commanded_state/version и не отменяется новым verify_async. На строках 146-147 повторно публикуется старое значение, а после последней попытки _record_fault безусловно пишет fault по устаревшему снимку. Обычная потеря только физического эха маскируется находкой 50, но сценарий достижим при отказе исходной base-публикации либо когда реальный report перезаписал retained противоположным состоянием. Проба с реальной временной IrrigationDB: после первой неудачной OFF-попытки зона переведена новой командой в commanded_state=on; старый verifier всё равно дважды опубликовал '0' и завершил state=fault, commanded_state=on.",
    "current_location": "services/observed_state.py:98-152; services/zone_control.py:402,842",
    "introduced_by_branch": false,
    "reproduction": "Запустить verify(zone, 'off', retries=3), заставить первую _subscribe_and_wait вернуть False, затем записать более свежие state/commanded_state='on'. Последующие вызовы publish_mqtt_value получают '0'; итоговый снимок в выполненной пробе: {'state':'fault','commanded_state':'on','fault_count':1}."
  },
  {
    "inventory_index": 50,
    "title": "Observed-state верификация — тавтология: приложение само пишет retained-значение в report-топик, который затем читает верификатор («ложное подтверждение» реле)",
    "real": true,
    "reason": "publish_mqtt_value сначала публикует retained-команду в базовый report-топик и ждёт QoS-ack, а затем отдельно публикует физическую команду в <topic>/on. StateVerifier подписывается на тот же базовый топик и принимает любой подходящий payload без проверки msg.retain, источника или correlation-id. Поэтому при живом брокере и отключённом реле retained-значение самого приложения подтверждает команду. Уже работающий sse_hub аналогично получает live loopback base-публикации и вызывает mark_zone_run_confirmed.",
    "current_location": "services/mqtt_pub.py:335-380; services/observed_state.py:120-135,165-233; services/sse_hub.py:201-257; utils.py:64-66",
    "introduced_by_branch": false,
    "reproduction": "При физически отключённом реле оставить брокер доступным, вызвать publish_mqtt_value(server, base_topic, '1', qos=2, retain=True), затем StateVerifier.verify(zone,'on'). Подписка получает retained '1' из base_topic и возвращает True, хотя /on никто физически не исполнил."
  },
  {
    "inventory_index": 110,
    "title": "Контракт state='fault' не реализован: fault-зона свободно стартует по расписанию, а любой stop_zone молча сбрасывает fault в 'off'",
    "real": true,
    "reason": "exclusive_start_zone запрещает только on/starting; fault попадает в ветку перехода starting→on. stop_zone рано выходит только для off/stopping, поэтому fault переводится через stopping в off. Более высокий ручной оркестратор не защищает статус: cancel_group_jobs вызывает stop_all_in_group(force=True) и сначала сам очищает fault. Оба scheduler-пути запуска также не фильтруют fault и пишут on. Единственный найденный потребитель fault вне StateVerifier — warning в system_status_api.",
    "current_location": "services/observed_state.py:235-300; services/zone_control.py:296-305,793-859; services/zone_control.py:592-600; irrigation_scheduler.py:887-996,1787-1818,2130-2154",
    "introduced_by_branch": false,
    "reproduction": "В выполненной пробе с временной IrrigationDB зона была установлена в fault: exclusive_start_zone вернул True и оставил state=on; после повторной установки fault вызов stop_zone(force=True) оставил state=off."
  },
  {
    "inventory_index": 132,
    "title": "stop_zone/exclusive_start_zone игнорируют результат publish_mqtt_value — при сбое доставки OFF зона помечается 'off', реле остаётся физически открытым, и все страховочные стоп-слои слепнут",
    "real": true,
    "reason": "Оба пути игнорируют boolean publish_mqtt_value: старт после False пишет state=on, стоп после False безусловно пишет state=off и очищает watering_start_time. auto-stop, hard-stop и APScheduler cap используют общий job_stop_zone→stop_zone без force; повторный вызов при off/stopping выходит без MQTT. ZoneWatchdog рассматривает только state=on. StateVerifier даёт временные ретраи, а реальный report при восстановлении брокера или мастер-клапан могут иногда исцелить/локализовать ситуацию, но гарантированного persistent-reconcile нет; для группы без мастера и без нового report клапан остаётся открыт до внешнего действия/ребута.",
    "current_location": "services/zone_control.py:386-402,793-864; irrigation_scheduler.py:168-175,569-578,1393-1439; services/watchdog.py:76-109",
    "introduced_by_branch": false,
    "reproduction": "В пробе publish_mqtt_value был принудительно равен False: exclusive_start_zone вернул True и записал on; stop_zone вернул True и записал off. Повторный stop_zone не сделал новой публикации: счётчик publish-вызовов остался равен 1."
  },
  {
    "inventory_index": 38,
    "title": "_boot_sync: stop_all_in_group(force=True) закрывает «крашнутые» открытые zone_runs как status='ok' ДО mop-up'а в 'aborted'",
    "real": true,
    "reason": "_boot_sync сначала вызывает stop_all_in_group(force=True), и лишь затем UPDATE открытых zone_runs в aborted. Force-путь stop_zone вызывает _finalize_water_stats, который находит открытый run, ставит end_utc и передаёт status='ok'. finish_zone_run понижает неподтверждённый run до failed, поэтому ok не безусловен; однако подтверждённый реальный полив становится ok и уже не подходит mop-up. Существующий integration-тест этого не ловит, поскольку явно мокает stop_all_in_group.",
    "current_location": "services/app_init.py:130-162; services/zone_control.py:678-721,793-864,881-904; db/zones.py:719-759",
    "introduced_by_branch": false,
    "reproduction": "Временная IrrigationDB: зона state=on, открытый confirmed=1 zone_run, затем настоящий _boot_sync с замоканным только MQTT. Получено: run=(id, 'ok', '<boot timestamp>'), zone_state='off'; aborted-sweep строку не изменил."
  },
  {
    "inventory_index": 36,
    "title": "TOCTOU в _do_close мастер-клапана + неатомарный pop/insert таймеров: закрытие мастера возможно сразу после открытия при старте зоны",
    "real": true,
    "reason": "_do_close читает состояния всех зон и затем публикует close без group_lock и повторной проверки. exclusive_start_zone использует group_lock, но таймер его не использует и при открытии мастера не отменяется. Поэтому переход off→starting между сканированием и publish не замечается. Кроме того, pop/cancel и последующий insert/start защищены разными секциями _PENDING_CLOSE_LOCK: два конкурентных планировщика могут оба увидеть пустой слот и запустить два таймера, один из которых будет потерян из словаря.",
    "current_location": "services/zone_control.py:78-239,292-373",
    "introduced_by_branch": false,
    "reproduction": "Зафиксировать таймер после чтения зон как off, параллельно запустить зону до state=starting и публикации master-open, затем отпустить таймер. Детерминированная проба дала последовательность ['master_opened_by_concurrent_start', ('delayed_close_publish','starting')]."
  },
  {
    "inventory_index": 34,
    "title": "Гонка двойного старта: ветка 'on/starting → pass' в exclusive_start_zone всё равно создаёт второй открытый zone_run",
    "real": true,
    "reason": "start_zone_orchestrated читает исходный state и выполняет already-on гейт до захвата group_lock. Два запроса могут оба увидеть off. exclusive_start_zone сериализует их, но второй после ожидания видит on/starting и выполняет pass вместо return; create_zone_run ниже вызывается безусловно. В zone_runs нет unique-ограничения на открытый run, а get_open_zone_run выбирает только последний. Rate-limit MQTT endpoint допускает 10 запросов/мин и не сериализует их; обычный /start не имеет debounce-гейта.",
    "current_location": "services/zone_control.py:292-339,513-545; db/zones.py:641-695; routes/zones_watering_api.py:26-54,292-310",
    "introduced_by_branch": false,
    "reproduction": "В выполненной пробе два потока start_zone_orchestrated были синхронизированы барьером после первых чтений off. Оба вернули 'started'; SQL показал open_runs=2. Отдельный последовательный вызов exclusive_start_zone дважды также дал две открытые строки при state=on."
  }
]

### /root/b_zone_2_refuter

{
  "compared_against": "main@4798625",
  "findings": [
    {
      "inventory_index": 111,
      "title": "exclusive_start_zone игнорирует результат publish_mqtt_value: при подтверждённом сбое доставки зона всё равно переводится в 'on' с audit_reason='mqtt_ack_on', API отвечает «started»",
      "real": true,
      "reason": "publish_mqtt_value явно возвращает false при недоступном клиенте или исчерпании подтверждений доставки. exclusive_start_zone отбрасывает результат, безусловно пишет state='on' с ложным audit_reason='mqtt_ack_on', запускает только асинхронный verifier и возвращает true. start_zone_orchestrated вслед за этим возвращает 'started', а HTTP-адаптеры — success. EMERGENCY_STOP, auth, CSRF и rate-limit проверяются до этого и сбой брокера не отсекают. StateVerifier через 3×10 секунд может перевести зону в fault, но не исправляет уже выданный успех и временно ложное состояние.",
      "current_location": "services/zone_control.py:383-404, 632-675; services/mqtt_pub.py:276-399; services/observed_state.py:55-66, 122-153",
      "introduced_by_branch": false,
      "reproduction": "Зона имеет корректные mqtt_server_id/topic, но брокер недоступен; либо publish_mqtt_value подменён на возврат false. POST /api/zones/<id>/mqtt/start даёт HTTP 200 success, в БД state='on' и остаётся открытый unconfirmed zone_run, хотя команда не доставлена. Позже verifier может поставить fault. На main тот же код в exclusive_start_zone:386-404 и тот же контракт mqtt_pub."
    },
    {
      "inventory_index": 136,
      "title": "Перезапуск уже включённой зоны снимает hard-stop джобы ЧУЖИХ зон: substring-матч 'zone_hard_stop:1' совпадает с 'zone_hard_stop:10..19'",
      "real": true,
      "reason": "Проверка использует `f\"zone_hard_stop:{zone_id}\" in jid`, тогда как ID имеет точный формат `zone_hard_stop:<id>`. Поэтому префикс одного числового ID совпадает с чужими ID. get_jobs() возвращает задания всех групп. Безопасный точный образец уже существует в cancel_zone_jobs. В текущей ветке /start и start-zone с restart_if_on=true достигают ветки даже без override; /mqtt/start достигает её с duration/duration_percent.",
      "current_location": "services/zone_control.py:544-574, особенно 568; irrigation_scheduler.py:1393-1421 и 2194-2202",
      "introduced_by_branch": false,
      "reproduction": "Запустить зоны 1 и 12 в разных группах, убедиться в наличии `zone_hard_stop:12`, затем повторно POST /api/zones/1/start. `zone_hard_stop:12` удаляется, обычный `zone_stop:12:*` остаётся. На main тот же дефект воспроизводится повторным POST /api/zones/1/mqtt/start с телом `{\"duration\": 5}` в routes/zones_watering_api.py:424-449.",
      "duplicate_root": "Полный дубль inventory_index 115; отличаются только числа примера."
    },
    {
      "inventory_index": 89,
      "title": "Ручной старт одной зоны отменяет на сегодня ВСЕ программы группы, чьё основное время уже прошло — вечерние extra_times-слоты молча пропадают",
      "real": true,
      "reason": "Housekeeping ручного старта сравнивает только p.time и записывает отмену по ключу program_id/date/group_id на весь день. extra_times создают отдельные APScheduler jobs, но с тем же program_id; каждый fire проверяет эту дневную отмену для каждой зоны. cancel_group_jobs cron-jobs программы не удаляет, а reschedule_group_to_next_program отмену не очищает. Единственный посторонний очиститель — событие окончания дождя — обычный сценарий не спасает.",
      "current_location": "services/zone_control.py:586-618, особенно 601-610; db/programs.py:444-471; irrigation_scheduler.py:841-852 и 1189-1201",
      "introduced_by_branch": false,
      "reproduction": "Создать enabled-программу текущего дня с time='06:00', extra_times=['20:00'] и зоной группы G. В 12:00 вызвать POST /api/zones/<id>/mqtt/start. В program_cancellations появляется запись program/date/G. В 20:00 extra-job запускает _run_program_threaded, но зона пропускается на проверке is_program_run_cancelled_for_group. На main эта логика уже находится в живом UI-пути routes/zones_watering_api.py:467-490; extra_times там также планируются.",
      "duplicate_root": null
    },
    {
      "inventory_index": 35,
      "title": "stop_zone не сериализован с exclusive_start_zone: проверка state по снапшоту до лока, group_lock не берётся, publish вне локов",
      "real": true,
      "reason": "stop_zone читает и валидирует state до zone_lock, group_lock не использует, а MQTT publish и поиск/закрытие open zone_run выполняет вне zone_lock. exclusive_start_zone держит group_lock, но zone_lock — только на коротких state-записях. update_zone_versioned сериализует отдельный SQL UPDATE, однако не проверяет допустимость перехода из ожидаемого caller-состояния: последний писатель побеждает. Уже исполняющийся APScheduler stop невозможно нейтрализовать удалением его job.",
      "current_location": "services/zone_control.py:285-305, 292-305, 383-404, 764-864; services/locks.py:9-29; services/zones_state.py:105-140; db/zones.py:293-349",
      "introduced_by_branch": false,
      "reproduction": "Детерминированный barrier-тест: зона изначально on. Поток A вызывает stop_zone, пишет stopping и физически публикует OFF, после чего блокируется внутри publish до возврата. Поток B в это время вызывает exclusive_start_zone: видит stopping, создаёт новый run, публикует ON и пишет on. Затем разблокировать A: он пишет off и _finalize_water_stats закрывает самый новый run. Итог — последняя физическая команда ON, БД off, новый run закрыт. На main структура локов и порядок операций те же.",
      "duplicate_root": null
    },
    {
      "inventory_index": 112,
      "title": "Зона без MQTT-топика/сервера в группе с мастер-клапаном: старт открывает мастер, но stop_zone никогда не планирует его закрытие — клапан остаётся открытым",
      "real": true,
      "reason": "Мастер открывается до проверки sid/topic зоны. При отсутствующей конфигурации зона остаётся starting, но exclusive_start_zone возвращает true. В первом неидемпотентном stop_zone вызов _maybe_schedule_master_close вложен в `if sid and topic` и `if server`, поэтому закрытие не взводится. Конфигурация достижима: создание зоны без MQTT разрешено и лишь возвращает warning. Уточнение: утверждение про гарантированно «неопределённый срок» преувеличено — start_zone_orchestrated обычно ставит обычный stop, hard-stop и cap; следующий stop уже на state=off попадёт в идемпотентную ветку и запланирует закрытие. Но ранний ручной stop гарантированно оставляет мастер открытым до следующего страховочного задания, то есть на оставшуюся длительность плюс close-delay; при отсутствии/гонке страховочного job утечка может стать бессрочной.",
      "current_location": "services/zone_control.py:348-399, 639-675, 789-847; routes/zones_crud_api.py:156-194; irrigation_scheduler.py:1332-1421",
      "introduced_by_branch": false,
      "reproduction": "Группа использует рабочий master MQTT, зона группы имеет topic='' и mqtt_server_id=NULL. POST /api/zones/<id>/mqtt/start открывает мастер и отвечает success, зона остаётся starting. Сразу POST /api/zones/<id>/mqtt/stop переводит её в off, но не создаёт master-close timer; мастер остаётся физически открыт до позднего zone_stop/hard-stop/cap. На main порядок открытия и вложенность закрытия идентичны.",
      "duplicate_root": null
    },
    {
      "inventory_index": 115,
      "title": "Перезапуск уже включённой зоны снимает zone_hard_stop чужой зоны: подстрочное сравнение id джоба без разделителя (зона 5 матчит zone_hard_stop:51)",
      "real": true,
      "reason": "Это тот же подстрочный матч, что в index 136: строка `zone_hard_stop:5` входит в `zone_hard_stop:51`, поэтому чужой hard-stop удаляется. Точный формат job ID и корректное сравнение `==` подтверждаются в irrigation_scheduler.",
      "current_location": "services/zone_control.py:544-574, особенно 568; irrigation_scheduler.py:1393-1421 и 2194-2202",
      "introduced_by_branch": false,
      "reproduction": "Запустить зоны 5 и 51 в разных группах, затем повторно стартовать уже on-зону 5 через /api/zones/5/start либо /mqtt/start с override. После reschedule отсутствуют и `zone_hard_stop:5`, и `zone_hard_stop:51`; заново создаётся только job зоны 5. На main тот же substring находился в routes/zones_watering_api.py:443.",
      "duplicate_root": "Полный дубль inventory_index 136; один корневой дефект и одна исправляемая строка."
    }
  ],
  "duplicate_roots": [
    {
      "inventory_indices": [
        136,
        115
      ],
      "root": "Подстрочное вместо точного сравнения APScheduler ID `zone_hard_stop:<zone_id>` в already-on reschedule."
    }
  ]
}

### /root/c_db_1_refuter

1. `inventory_index: 98`
   `title:` Рестарт сервиса с пустой таблицей zones сбрасывает пароль на `1234`
   `real: true`
   `reason:` `init_database()` при каждом запуске вызывает `_insert_initial_data()`. При `COUNT(zones)==0` выполняется безусловный `INSERT OR REPLACE password_hash`, независимо от существующего пользовательского хеша. Штатный DELETE позволяет удалить последнюю зону. После ранее выполненного `set_password()` флаг `password_must_change='0'` сохраняется, поэтому `ensure_password_change_required()` его не поднимает.
   `current_location:` `db/migrations.py:119,249-271`; `database.py:39-59,388`; `routes/zones_crud_api.py:146-153`; `db/settings.py:41-67,187-200`.
   `introduced_by_branch: false` — та же логика присутствует на `main` (`db/migrations.py:241-264`).
   `reproduction:` на временной БД: создать зону → `set_password("supersecret")` → удалить все зоны → повторно `MigrationRunner.init_database()`. Результат: `check_password_hash(hash,"1234")==True`, для `supersecret` — `False`, `password_must_change=="0"`.
   `duplicate:` нет.

2. `inventory_index: 59`
   `title:` Эвристика 1-базных дней сдвигает выбор UI
   `real: true`
   `reason:` UI однозначно формирует 0-базные индексы (`Пн=0`), а `create_program`/`update_program` считают любой непустой набор без нуля 1-базным и вычитают единицу. Планировщик напрямую передаёт сохранённые значения в `CronTrigger(day_of_week=...)`.
   `current_location:` `db/programs.py:60-65,111-117`; `templates/programs.html:425-433,590-596,622-628`; `irrigation_scheduler.py:1223-1224,2257`.
   `introduced_by_branch: false` — `db/programs.py` идентичен `main`; корень восходит к старому scheduler-коду.
   `reproduction:` временная БД: `create_program(days=[1,3])` возвращает/хранит `[0,2]`.
   `duplicate:` да, точный дубль уже `CONFIRMED`-находки inventory `#10`.

3. `inventory_index: 41`
   `title:` JSON-null в zones/extra_times валит старт сервиса
   `real: true`
   `reason:` наличие ключа проверяется, его тип/nonnull — нет. `json.dumps(None)` сохраняет строку `null`, а чтение возвращает Python `None`. API conflict-check для `zones=None` возвращает пустой список и PUT/POST успешно сохраняет яд. Немедленное перепланирование ловит `TypeError`, поэтому запрос остаётся успешным; на старте `schedule_program` не ловит `TypeError`, затем он проходит через `load_programs`, `init_scheduler` и `initialize_app`, где также не ловится, обрывая импорт `app` из `run.py`.
   `current_location:` `routes/programs_api.py:60-93,109-150`; `db/programs.py:78,85,120-145`; `irrigation_scheduler.py:1140-1208,2218-2225,2376-2387`; `services/app_init.py:52-59`; `app.py:752`; `run.py:11`.
   `introduced_by_branch: false` — опасные DB/scheduler/startup-пути есть на `main`; добавленная веткой v2-валидация не проверяет `zones`/`extra_times`.
   `reproduction:` на временной БД `update_program(..., {"zones": None})` сохраняет `zones is None`, после чего `list(program["zones"])` даёт `TypeError`; с `extra_times=None` аналогично падает `enumerate`.
   `duplicate:` нет; inventory `#127` связан с невалидными пустыми списками, но имеет другой эффект.

4. `inventory_index: 45`
   `title:` retry_on_busy недостижим для штатных repository DB-ошибок
   `real: true`
   `reason:` найдено 51 применение декоратора. У 46 методов есть непосредственный перехват `sqlite3.Error`; оставшиеся пять (`duplicate_program` и составные settings-setters) вызывают только helpers, которые сами глотают SQLite-ошибки. Следовательно, производственный `OperationalError("database is locked")` превращается в `None`/`False` до wrapper. Вызовы действительно трактуют это как бизнес-ошибку: например, PUT зоны отвечает 404. Для `finish_zone_run` строка остаётся с `end_utc=NULL`; boot-cleanup лишь меняет `status` на `aborted`, но не закрывает её.
   `current_location:` `db/base.py:12-26`; примеры перехватов — `db/zones.py:248-291,718-763`; ошибочный 404 — `routes/zones_crud_api.py:128-144`.
   `introduced_by_branch: false` — `db/base.py` побайтно совпадает с `main`, количество декораторов на `main` также 51.
   `reproduction:` `_connect=Mock(side_effect=OperationalError("database is locked"))`; вызов `update_program` вернул `None`, `_connect.call_count==1`, то есть повторов не было.
   `duplicate:` частично пересекается с `#44`, но не дубль: `#44` отдельно описывает потерю `BEGIN IMMEDIATE` и TOCTOU в `update_zone_versioned`.

5. `inventory_index: 99`
   `title:` backfill source принимает локальное naive-время за UTC
   `real: true`
   `reason:` исторические writers передают `datetime.now().strftime(...)`, то есть локальную naive-строку. Миграция навешивает на неё `UTC`, затем переводит в local timezone, искусственно сдвигая время и иногда календарный день до сравнения с программой. Одноразово записанный `manual` затем не пересчитывается.
   `current_location:` writers — `services/zone_control.py:294,337-339`, `irrigation_scheduler.py:1808,1932-1941`; ошибка — `db/migrations.py:1141-1297`, особенно `1249-1254`.
   `introduced_by_branch: false` — тот же алгоритм и те же local-naive writers есть на `main`.
   `reproduction:` при `TZ=Europe/Moscow`, программе `06:00`, `days=[0]` и run `2026-07-13 06:00:10` миграция записала `source='manual'`.
   `duplicate:` нет.
   `оговорка:` формулировка «вся история» чрезмерна — возможны случайные совпадения с другой программой после TZ-сдвига; текущий UI также не имеет заявленного source-фильтра, но показывает source-badge и экспортирует его в CSV. Само необратимое искажение данных подтверждено.

6. `inventory_index: 71`
   `title:` duplicate_program повторно сдвигает дни
   `real: true`
   `reason:` `duplicate_program` делает server-side round-trip прочитанных 0-базных `days` через `create_program`, где они снова проходят ошибочную эвристику. Состояние без понедельника достижимо штатно: исходный UI-ввод `[2,4,6]` сначала сохраняется как `[1,3,5]`, после чего duplicate сдвигает его ещё раз.
   `current_location:` `db/programs.py:64-65,173-186`; `routes/programs_api.py:201-216`; кнопка — `templates/programs.html:204-206`.
   `introduced_by_branch: false` — код идентичен `main`.
   `reproduction:` `create_program(days=[2,4,6]) → [1,3,5]`; затем `duplicate_program(id) → [0,2,4]`.
   `duplicate:` да, отдельный call-site того же дефекта, уже явно охваченный inventory `#10`; также производный от дубля `#59`.

Файлы репозитория не изменялись; проверки выполнялись на временных БД.

### /root/c_db_2_refuter

Итог: 5 реальных записей, 1 опровергнута. `[100]` — дубль корня `[129]` с неверно заявленным основным runtime-эффектом.

```yaml
- inventory_index: 70
  title: "check_program_conflicts игнорирует enabled: выключенная программа навсегда блокирует создание/сохранение пересекающейся программы"
  real: true
  reason: >
    SELECT не читает enabled, цикл не фильтрует выключенные программы.
    POST/PUT прекращают сохранение при найденном конфликте. Планировщик при
    enabled=0, напротив, снимает jobs. Это не выглядит намеренным поддержанием
    инварианта: PATCH повторного включения вообще не проверяет конфликты.
    Оговорки: блокировка не «навсегда» — старую программу можно удалить или
    перенести; ложный success-toast — отдельная запись inventory [58].
    Путь достижим из штатного admin UI, несмотря на admin+CSRF gate.
  current_location:
    - "db/programs.py:216-223,324-430"
    - "routes/programs_api.py:70-84,127-141"
    - "irrigation_scheduler.py:1142-1146"
  introduced_by_branch: false
  reproduction: >
    Создать зону и программу A на 23:00/[0], выключить A через PATCH enabled=false,
    затем POST программы B на то же время/день/зону. Ответ — HTTP 200 с
    success:false, has_conflicts:true; B в БД отсутствует.

- inventory_index: 100
  title: "check_program_conflicts полностью игнорирует extra_times: пересечение по дополнительным слотам не детектируется, программы реально накладываются в рантайме"
  real: false
  reason: >
    Первая половина верна: checker полностью игнорирует extra_times. Но заявленное
    обычное интерливинг-исполнение не следует из текущего runtime: активная
    программа регистрирует group_cancel_events, а второй fire на общей группе
    возвращается из manual-vs-scheduled guard до запуска зон. Это тот же корень,
    что у [129], где описан фактический основной результат — полный пропуск B.
    Между проверкой is_group_session_active и setdefault есть узкий TOCTOU для
    строго одновременных стартов, но inventory его не воспроизводит; это отдельная
    неподтверждённая race-находка и не спасает текущую формулировку.
    Кроме того, текущий wizard не позволяет добавить extra_times, только сохраняет
    уже имеющиеся; путь остаётся доступен через admin API/legacy rows.
  current_location:
    - "db/programs.py:216-223,342-350"
    - "irrigation_scheduler.py:744-782,788-792,1192-1201"
  introduced_by_branch: false
  reproduction: >
    A: main=06:00, extra_times=[20:00]; B: main=20:00. Checker действительно
    возвращает [], но если дождаться group_cancel_events от A и вызвать B,
    B пишет prog_skipped_manual_running и не включает зоны — вместо заявленного
    интерливинга.
  duplicate_of: 129

- inventory_index: 129
  title: "check_program_conflicts не видит extra_times — пересечение по extra-слоту проходит валидацию, а на fire-time вся программа молча пропускается guard'ом"
  real: true
  reason: >
    Checker сравнивает только основные time. Scheduler создаёт отдельные jobs для
    extra_times. Пока A выполняется, её Event делает is_group_session_active=true;
    fire B возвращается до цикла зон. Проверка устроена по принципу «если занята
    хотя бы одна группа», поэтому пропускаются также зоны B из свободных групп.
    Ретрая нет, лог ошибочно называется prog_skipped_manual_running.
    Существующий extra-times API-тест не защищает семантику: ожидаемый assert
    has_conflicts is True закомментирован.
  current_location:
    - "db/programs.py:216-223,324-350"
    - "routes/programs_api.py:70-72,127-129"
    - "irrigation_scheduler.py:716-782,788-792,1192-1201"
  introduced_by_branch: false
  reproduction: >
    A: 06:00, группа 1, длительность 60 минут. B: main=21:00,
    extra_times=[06:30], зоны групп 1 и 2. Сохранение B проходит. Запустить A,
    дождаться Event группы 1 и вызвать fire B: появляется
    prog_skipped_manual_running, ни одна зона B, включая группу 2, не запускается.
  duplicate_root_with: 100

- inventory_index: 101
  title: "set_env_config: неатомарная запись из шести отдельных коммитов + непойманный ValueError на нечисловом server_id — 500 и полуприменённая конфигурация датчиков"
  real: true
  reason: >
    Каждый set_setting_value открывает своё соединение и делает commit. К моменту
    int(temp.server_id) первые два значения уже зафиксированы; ValueError не
    ловится ни set_env_config, ни роутом. При обычной DB-ошибке отдельный setter
    возвращает false, но остальные RHS всё равно выполняются через &=, после чего
    роут запускает monitor с потенциально смешанной конфигурацией.
    Коррекция сценария: именно после ValueError env_monitor не перезапускается —
    исключение обрывает роут раньше start(); смесь остаётся в БД.
    Штатный UI отправляет число/null, однако прямой POST доступен Flask guest и
    CSRF-exempt; прод предполагает внешний nginx Basic Auth. Пустая строка падает,
    JSON null безопасен.
  current_location:
    - "db/settings.py:27-38,159-174"
    - "routes/system_config_api.py:236-276"
    - "app.py:150-173,404-413"
  introduced_by_branch: false
  reproduction: >
    Задать старые temp-настройки, затем POST /api/env с temp.enabled=false,
    topic='/new/t', server_id='abc'. Получается HTTP 500; env.temp.enabled и
    env.temp.topic уже новые, server_id и hum-настройки остаются старыми.

- inventory_index: 42
  title: "update_zone: int()-касты в новом _build_zone_update_fields бросают непойманный ValueError/TypeError — 500 вместо контрактного None"
  real: true
  reason: >
    int(duration/group_id) расположен вне sqlite-only except update_zone.
    HTTP-роут валидирует duration, поэтому malformed duration получает 400;
    реальный HTTP-пробел — group_id, который не валидируется и даёт 500.
    Штатный UI формирует числовой select, а endpoint требует admin+CSRF, поэтому
    поверхность ограничена кривым/tampered API-клиентом. Заявление о формальном
    контракте «None при любой ошибке» не подтверждается docstring; комментарий
    явно требует ValueError у вызывающего. Дефект находится на API-границе,
    которая его не ловит. Внутренние call-sites обычно передают валидные значения,
    но любая уже испорченная строка становится необновляемой, поскольку builder
    повторно кастит merged snapshot при каждом update_zone.
  current_location:
    - "db/zones.py:63-68,248-291"
    - "routes/zones_crud_api.py:105-128"
  introduced_by_branch: true
  reproduction: >
    Под admin-сессией с CSRF выполнить PUT /api/zones/<id>
    {"group_id":"abc"}. Валидация пропускает payload, int("abc") поднимает
    ValueError, Flask отвечает 500; запись не изменяется. На main тот же ввод
    записывался как SQLite TEXT. Builder добавлен d58c209, но сами int-касты
    внесены позднее коммитом 0407bdb.

- inventory_index: 68
  title: "update_zone: полная перезапись строки из несвежего снапшота — read-modify-write без транзакции затирает конкурентные изменения state/postpone/watering_start_time"
  real: true
  reason: >
    update_zone читает current_zone через отдельное соединение, затем без общей
    транзакции/version predicate пишет почти весь merged snapshot. Generic CRUD
    запрещает state в payload, но всё равно неявно пишет snapshot.state,
    postpone_until/reason, watering_start_time и scheduled_start_time.
    CRUD-роут не разделяет service group/zone locks. Если versioned writer
    успевает между read и UPDATE, plain UPDATE оставляет уже увеличенный version,
    но возвращает старые runtime-поля — конфликт не обнаруживается.
    Возможное последующее MQTT-эхо может самоисправить state, поэтому «второй
    старт» не гарантирован; потеря postpone/runtime-состояния сама по себе реальна.
  current_location:
    - "db/zones.py:252-268"
    - "db/zones.py:59-91"
    - "db/zones.py:313-346"
    - "db/base.py:48-51"
  introduced_by_branch: false
  reproduction: >
    T1 вызывает update_zone(name='renamed') и останавливается после get_zone.
    T2 коммитит update_zone_versioned(state='on', watering_start_time=...).
    После освобождения T1 его полный UPDATE возвращает state='off' и NULL-время
    из старого snapshot, сохраняя новое имя. Аналогично воспроизводится с
    update_zone_postpone.
```

Точечно выполнены 3 теста — все прошли:

```text
tests/api/test_run_selected_api.py::TestManualVsScheduledGuard::test_scheduled_blocked_by_active_manual
tests/api/test_programs_api_v2.py::TestCheckConflictsWithExtraTimes::test_check_conflicts_with_extra_times
tests/api/test_settings_api.py::TestEnvConfigAPI::test_set_env_config
3 passed
```

Extra-times тест проходит только потому, что ключевой assert закомментирован. Файлы не менялись; исходный `?? AGENTS.md` остался нетронут.

### /root/c_db_3_refuter

[
  {
    "inventory_index": 37,
    "title": "update_zone_versioned не реализует optimistic lock: version читается в той же транзакции, WHERE version=? не может не совпасть; fallback в zones_state дожимает запись при любой ошибке",
    "real": true,
    "reason": "BEGIN IMMEDIATE атомарно сериализует отдельную DB-транзакцию, но expected_version от вызывающего отсутствует: версия читается уже после получения write-lock, поэтому WHERE version=? почти гарантированно совпадает. Это не защищает устаревшее намерение многошаговой операции. stop_zone отпускает zone_lock между stopping и финальным off; параллельный restart может записать starting/on, после чего старый stop прочитает новую version и успешно перезапишет state в off. group_lock используется стартом, но не stop_zone; SSE и scheduler также пишут без этих lock. При ok=False zones_state безусловно пытается raw update_zone, превращая возможный CAS-конфликт в advisory; успешность fallback при этом не гарантирована.",
    "current_location": [
      "db/zones.py:293-349",
      "services/zones_state.py:108-140",
      "services/zone_control.py:789-859",
      "irrigation_scheduler.py:923-991"
    ],
    "introduced_by_branch": false,
    "reproduction": "Зона on. Остановить stop_zone после публикации MQTT OFF, но до финального off; выполнить restart, который пишет starting/on и публикует ON; затем продолжить старый stop. Все четыре versioned UPDATE возвращают ok=True, итоговая version последовательно растёт, но старый stop оставляет БД в off после более нового ON-командования."
  },
  {
    "inventory_index": 43,
    "title": "bulk_upsert_zones: одна строка с нечисловым/null duration откатывает ВЕСЬ импорт с 500 вместо инкремента failed",
    "real": true,
    "reason": "API проверяет только непустой список и не валидирует duration. Для существующей зоны builder выполняет int(z['duration']); TypeError/ValueError не ловятся per-row и внешними repository catch, которые принимают только sqlite3.Error. Исключение выходит из sqlite connection context, поэтому вся незакоммиченная транзакция откатывается; API ловит его и отвечает 500. Уточнение: null при INSERT превращается в default 10, а пустая CSV-ячейка не добавляет поле; дефект воспроизводится null для UPDATE либо truthy нечисловой строкой. В frontend действительно достижим через parseInt('abc') → NaN → JSON null.",
    "current_location": [
      "db/zones.py:63-66",
      "db/zones.py:390-464",
      "routes/zones_crud_api.py:213-250",
      "static/js/zones.js:1889-1906"
    ],
    "introduced_by_branch": false,
    "reproduction": "Создать зоны 1 и 2; вызвать bulk_upsert_zones([{'id':1,'name':'should-rollback'},{'id':2,'duration':None}]). Получен TypeError, а повторное чтение зоны 1 показывает прежнее имя. Через POST /api/zones/import тот же payload даёт HTTP 500."
  },
  {
    "inventory_index": 44,
    "title": "update_zone_versioned: `except sqlite3.Error: pass` вокруг BEGIN IMMEDIATE глотает lock-ошибку — TOCTOU-защита молча отключается, retry_on_busy не срабатывает",
    "real": true,
    "reason": "Штатный _connect создаёт новое соединение, поэтому комментарий про already in transaction неприменим. Ошибка занятого writer-lock на BEGIN IMMEDIATE проглатывается; последующий SELECT выполняется без write-транзакции, а ошибка UPDATE ловится внешним catch и преобразуется в (False,None). Декоратор retry_on_busy исключения не видит. zones_state пытается fallback, но update_zone также глотает lock-error, а результат fallback не проверяется. busy_timeout=30000 защищает от обычного короткого contention, поэтому сценарий inventory слишком широк, но при lock дольше timeout дефект подтверждается.",
    "current_location": [
      "db/zones.py:293-349",
      "db/base.py:12-30",
      "services/zones_state.py:108-140"
    ],
    "introduced_by_branch": false,
    "reproduction": "Удерживать BEGIN IMMEDIATE другим соединением дольше busy_timeout либо в точечном тесте уменьшить busy_timeout. Вызов update_zone_versioned возвращает (False,None), состояние не меняется, предупреждений SQLite BUSY retry/backoff-вызовов нет. При продолжающемся lock fallback update_zone также ничего не записывает."
  },
  {
    "inventory_index": 48,
    "title": "get_audit_logs/get_logs: фильтры дат сравниваются с UTC-временем, а отображение — localtime (сдвиг на TZ-офсет)",
    "real": true,
    "reason": "audit_log.ts хранится через CURRENT_TIMESTAMP в UTC. SELECT отображает strftime(...,'localtime'), но WHERE с тем же именем ts применяется к исходному UTC-столбцу. UI передаёт локальную календарную дату без timezone; count_audit_logs повторяет ошибку. Защита есть только у текущего /api/logs: он вызывает get_logs() без DB-фильтров и фильтрует уже локализованные строки в Python. Поэтому logs endpoint не затронут, но /api/audit затронут, а публичные параметры LogRepository.get_logs остаются дефектными.",
    "current_location": [
      "db/audit.py:91-104",
      "db/audit.py:143-150",
      "db/logs.py:27-42",
      "routes/audit_api.py:134-157",
      "routes/system_status_api.py:670-697"
    ],
    "introduced_by_branch": false,
    "reproduction": "При TZ=Asia/Bishkek вставить UTC-записи 2026-07-17 20:00 и 2026-07-18 20:00. Обе отображаются соответственно как 18 июля 02:00 и 19 июля 02:00. get_audit_logs(since='2026-07-18',until='2026-07-18') вернул только вторую запись — то есть чужие локальные сутки — и пропустил первую."
  },
  {
    "inventory_index": 73,
    "title": "_recreate_table_without_columns теряет UNIQUE/FK-констрейнты при downgrade: после rollback telegram_add_fsm_and_notif ломается upsert_bot_user (ON CONFLICT без UNIQUE)",
    "real": true,
    "reason": "Helper восстанавливает только сведения PRAGMA table_info: PK, NOT NULL и default. UNIQUE, внешние ключи и индексы в эту структуру не входят. Конкретно rollback telegram_add_fsm_and_notif удаляет UNIQUE(chat_id), после чего ON CONFLICT(chat_id) недействителен. Полный Telegram polling thread не падает: upsert ловит ошибку и возвращает False, а вызывающие игнорируют bool; однако регистрация/обновление bot_user действительно перестаёт работать. FK-часть — общий дефект helper; конкретному Telegram-воспроизведению достаточно потери UNIQUE.",
    "current_location": [
      "db/migrations.py:334-382",
      "db/migrations.py:1422-1433",
      "db/migrations.py:644-660",
      "db/telegram.py:27-43",
      "services/telegram_bot.py:317-331"
    ],
    "introduced_by_branch": false,
    "reproduction": "На полностью мигрированной временной БД до rollback PRAGMA index_list(bot_users) содержит sqlite_autoindex_bot_users_1 с unique=1. После rollback_migration('telegram_add_fsm_and_notif') список индексов пуст, CREATE TABLE не содержит UNIQUE, а TelegramRepository.upsert_bot_user(...) логирует 'ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint' и возвращает False."
  }
]

### /root/c_db_4_refuter

[
  {
    "inventory_index": 62,
    "title": "Сохранение MQTT-сервера из UI молча сбрасывает TLS-настройки (частичный PUT перезаписывает отсутствующие поля дефолтами)",
    "real": true,
    "reason": "UI отправляет только name/host/port/username/client_id/enabled. API передаёт payload без нормализации, а update_mqtt_server безусловно записывает отсутствующие tls_* как 0/NULL. Кешированный MQTT-клиент может временно продолжить старое TLS-соединение, но сохранённая конфигурация уже уничтожена; после пересоздания клиента или рестарта будет использована конфигурация без TLS. На main дефект идентичен.",
    "current_location": [
      "db/mqtt.py:79-120",
      "templates/mqtt.html:164-176",
      "routes/mqtt_api.py:85-98",
      "services/mqtt_pub.py:77-90",
      "services/observed_state.py:183-193"
    ],
    "introduced_by_branch": false,
    "reproduction": {
      "method": "Изолированная временная БД: создать сервер с tls_enabled=1 и всеми путями, затем вызвать update_mqtt_server с payload формы.",
      "observed": "До обновления: tls_enabled=1, пути /ca.pem,/cert.pem,/key.pem, tls_insecure=1, tls_version=TLS. После: tls_enabled=0, все пути NULL, tls_insecure=0, tls_version=NULL."
    }
  },
  {
    "inventory_index": 46,
    "title": "update_zone: исключение зоны из программ при переводе в группу 999 не срабатывает для строкового group_id",
    "real": true,
    "reason": "_build_zone_update_fields преобразует \"999\" в int и сохраняет group_id=999, но следующий gate сравнивает исходное updated_data[\"group_id\"] со значением 999 без нормализации. Штатный UI использует parseInt и эту ветку не воспроизводит, однако PUT API принимает строку без проверки типа. Runtime-защиты также нет: _run_program_threaded исключает 999 лишь из session-lock набора, но затем всё равно обрабатывает зону; exclusive_start_zone не открывает master valve и не пишет zone_run для 999, однако MQTT ON самой зоне публикует. На main сравнение такое же.",
    "current_location": [
      "db/zones.py:249-288",
      "routes/zones_crud_api.py:82-128",
      "irrigation_scheduler.py:716-727",
      "irrigation_scheduler.py:835-874",
      "services/zone_control.py:313-339",
      "services/zone_control.py:383-399"
    ],
    "introduced_by_branch": false,
    "reproduction": {
      "method": "Создать зону и программу с этой зоной; вызвать update_zone(zid, {\"group_id\": \"999\"}).",
      "observed": "zones.group_id стал 999, но programs.zones остался [zid]. Числовой payload 999 проходит существующий тест и удаляет членство, подтверждая именно type-sensitive дефект."
    }
  },
  {
    "inventory_index": 72,
    "title": "finish_zone_run: чтение confirmed и UPDATE статуса не атомарны — поздний echo реле помечает реальный полив как 'failed'",
    "real": true,
    "reason": "SELECT confirmed выполняется до первого DML и не защищён BEGIN IMMEDIATE. Между ним и финальным UPDATE другой поток может закоммитить confirmed=1; finish_zone_run уже вычислил status='failed' и затем сохраняет его, не затрагивая confirmed. Реальные конкурентные call-sites есть в MQTT/SSE callback и StateVerifier, тогда как stop_zone завершает run в другом потоке. Строка confirmed=1,status='failed' исключается из last_watering_time и фактических итогов. Окно узкое, но детерминированно воспроизводится. Код присутствует на main.",
    "current_location": [
      "db/zones.py:700-759",
      "services/sse_hub.py:247-259",
      "services/observed_state.py:122-134",
      "services/zone_control.py:678-721",
      "db/zones.py:765-782",
      "services/history_calc.py:214-224"
    ],
    "introduced_by_branch": false,
    "reproduction": {
      "method": "Принудительно выдержать interleaving: finish SELECT получает confirmed=0; параллельный mark_zone_run_confirmed коммитит 1; затем finish выполняет UPDATE со status='failed'.",
      "observed": "finish_zone_run вернул true; итоговая строка: status='failed', confirmed=1."
    }
  },
  {
    "inventory_index": 47,
    "title": "compute_next_run_for_zone: для interval-программ с interval_days>1 пишется заведомо неверный scheduled_start_time",
    "real": true,
    "reason": "_first_program_start_after для schedule_type='interval' игнорирует interval_days и рассматривает каждый календарный день. Живой планировщик использует IntervalTrigger(days=interval_days) с сохранённым якорем, поэтому после первого fire следующие даты расходятся. reschedule_group_to_next_program записывает ложную проекцию как точный scheduled_start_time. На main interval с пустыми days возвращал None; ошибочное ежедневное значение добавлено веткой в d7161a5.",
    "current_location": [
      "db/zones.py:820-841",
      "db/zones.py:844-891",
      "db/zones.py:897-908",
      "irrigation_scheduler.py:1242-1268"
    ],
    "introduced_by_branch": true,
    "reproduction": {
      "method": "interval_days=3, время 14:00, живой IntervalTrigger с якорем 2026-07-18 14:00; проверка после этого fire.",
      "observed": "При now=2026-07-18 15:19 compute_next_run_for_zone вернул 2026-07-19 14:00, тогда как IntervalTrigger.next_run_time — 2026-07-21 14:00."
    }
  },
  {
    "inventory_index": 130,
    "title": "compute_next_run_for_zone: postpone-якорь не сдвигается на offset зоны — scheduled_start_time уводит на следующий день, хотя планировщик польёт сегодня",
    "real": true,
    "reason": "Функция заменяет now на postpone_until и ищет старт программы строго после этой точки, хотя затем отдельно добавляет offset зоны. Планировщик проверяет postpone непосредственно перед обработкой каждой зоны в последовательности; поэтому старт программы может быть раньше postpone_until, если слот конкретной зоны позже него. Исправленный services.next_watering вычитает offset из нижней границы и подтверждает расхождение. На main та же ошибка существовала с cc66c83; ветка исправила только соседний сервис.",
    "current_location": [
      "db/zones.py:844-889",
      "db/zones.py:897-908",
      "services/next_watering.py:180-234",
      "irrigation_scheduler.py:835-874"
    ],
    "introduced_by_branch": false,
    "reproduction": {
      "method": "Ежедневная программа 14:00, три зоны по 30 минут, третья имеет offset 60 минут; postpone третьей до 14:30.",
      "observed": "Физический слот и services.next_watering: 2026-07-19 15:00. compute_next_run_for_zone и записываемый scheduled_start_time: 2026-07-20 15:00."
    }
  }
]

### /root/d_mqtt_1_refuter

[
  {
    "inventory_index": 80,
    "title": "Совпадающий client_id: probe/status/scan-sse из настроек MQTT выбивают live-подписку SSE-хаба (MQTT session takeover)",
    "real": true,
    "reason": "Хаб, probe, status и scan-sse передают один сохранённый client_id в отдельные MQTTv3.1.1-клиенты. Новый CONNECT с тем же ClientID обязан вытеснить существующее соединение. Особенно опасны scan-sse и probe: их сетевые циклы могут реконнектиться одновременно с хабом и устроить ping-pong. /status вызывается UI автоматически каждые 5 секунд; тот же дефект есть в регулярно опрашиваемом /api/status. Последствия в finding частично завышены: StateVerifier использует уникальный ID и может подтвердить zone_run, а retained-состояние часто будет повторно доставлено. Но разрывы подписки, потеря non-retained событий и деградация master-valve/echo-наблюдения реальны. Условие воспроизведения — непустой client_id и уже запущенный SSE-хаб.",
    "current_location": "routes/mqtt_api.py:156,228,297; services/sse_hub.py:173; routes/system_status_api.py:541-548; templates/mqtt.html:202-219,284,368,419,453",
    "introduced_by_branch": false,
    "reproduction": "Четыре настоящих paho-mqtt 2.1.0 Client(VERSION2, client_id='same-id') получили одинаковый _client_id и clean_session=True. Исходник paho connect() подтверждает синхронную отправку CONNECT; согласно MQTT 3.1.1 одинаковый ClientID детерминированно вызывает session takeover. Все основные строки finding без изменений присутствуют в main."
  },
  {
    "inventory_index": 49,
    "title": "Публикующий MQTT-клиент: on_disconnect с сигнатурой VERSION1 при CallbackAPIVersion.VERSION2 — TypeError убивает сетевой тред paho, авто-reconnect мёртв",
    "real": true,
    "reason": "Клиент создаётся с CallbackAPIVersion.VERSION2, но вложенный callback принимает максимум четыре аргумента. Paho 2.1.0 вызывает VERSION2 on_disconnect с пятью аргументами и при suppress_exceptions=False пробрасывает TypeError наружу. _thread_main не перехватывает исключение, поэтому текущий network loop завершается до auto-reconnect. На некоторых disconnect-путях обновление внутреннего state идёт после callback, из-за чего кэш может временно считать клиента connected. Self-healing следующей публикации способен позднее пересоздать клиента, поэтому деградация не обязательно длится до рестарта сервиса, но штатный auto-reconnect именно этого клиента сломан.",
    "current_location": "services/mqtt_pub.py:74,105-128; .venv/lib/python3.11/site-packages/paho/mqtt/client.py:4383-4396,4521-4525",
    "introduced_by_branch": false,
    "reproduction": "Из get_or_create_mqtt_client был получен фактический вложенный callback и вызван в форме VERSION2 с пятью аргументами. Результат: TypeError: get_or_create_mqtt_client.<locals>._on_disconnect() takes from 3 to 4 positional arguments but 5 were given. У реального paho-клиента suppress_exceptions=False.",
  },
  {
    "inventory_index": 51,
    "title": "sse_hub._on_message на каждом наблюдаемом 'on' безусловно отменяет и перевзводит автостоп на БАЗОВУЮ длительность — override-длительность затирается, retained '1' при реконнекте продлевает полив",
    "real": true,
    "reason": "Гейт watering_start_time защищает только запись времени старта; cancel_zone_jobs и schedule_zone_stop находятся вне него и выполняются на каждом ON. Используется z.duration, тогда как override хранится лишь локально в start_zone_orchestrated и planned_end_time. Поскольку publish '1' выполняется до планирования override-stop и хаб подписан на этот retained base-topic, при любом порядке callback оставляет ранний stop на базовую длительность: либо отменяет override, либо создаёт base-job раньше, а совпавший command_id конфликт проглатывается как KeyError. При повторной подписке retained ON также отсчитывает базовую длительность заново. Для ручного запуска zone_cap_stop не отменяется и ограничивает максимум 240 минут, но это не устраняет раннюю остановку override и продление при реконнекте.",
    "current_location": "services/sse_hub.py:247-271,353-394; services/zone_control.py:639-673; irrigation_scheduler.py:1332-1391,1393-1421,2194-2216",
    "introduced_by_branch": false,
    "reproduction": "Фактический _on_message был вызван для активной зоны с duration=10 и непустым watering_start_time. Mock scheduler зафиксировал cancel_zone_jobs(7) и schedule_zone_stop(7, 10). ConflictingIdError APScheduler подтверждён как подкласс KeyError, который schedule_zone_stop поглощает."
  },
  {
    "inventory_index": 69,
    "title": "PUT /api/mqtt/servers/<id> возвращает расшифрованный сохранённый пароль брокера — маскировка VULN-004 есть только в GET, а новая keep-семантика ветки делает утечку регулярной",
    "real": true,
    "reason": "PUT после обновления непосредственно сериализует db.get_mqtt_server(), который расшифровывает ENC:-пароль. Маскирование применяется только в GET list/detail. UI PUT не отправляет password, а новая веточная семантика сохраняет прежний секрет, поэтому любое обычное сохранение теперь возвращает его открытым текстом. admin_required ограничивает доступ админом, но не исключает утечку в браузер, DevTools, прокси и same-origin XSS. Небезопасный unmasked response существовал и раньше для POST/явной смены пароля, однако именно регулярная утечка сохранённого пароля при password-less UI PUT появилась в ветке.",
    "current_location": "routes/mqtt_api.py:35-44,69-79,85-99; db/mqtt.py:14-20,79-121; templates/mqtt.html:164-180",
    "introduced_by_branch": true,
    "reproduction": "Mocked PUT /api/mqtt/servers/9 без поля password вернул status=200 и response_password='broker-secret'. Сравнение с main: прежний update всегда записывал password из payload и при отсутствии поля затирал его NULL; ветка добавила условное сохранение секрета."
  },
  {
    "inventory_index": 114,
    "title": "GET /api/mqtt/<id>/status — единственный MQTT-эндпоинт без @admin_required: аноним инициирует блокирующие подключения к брокеру с сохранёнными креденшалами",
    "real": true,
    "reason": "У status отсутствует admin_required, а глобальный auth middleware безусловно пропускает GET /api/*. Общий rate limiter также исключает все GET. Обработчик извлекает расшифрованные credentials и синхронно вызывает paho connect(); третий аргумент 3 является keepalive, а socket connect timeout paho равен 5 секундам. Прод привязан к 0.0.0.0:8080, поэтому LAN может обойти nginx. Для исчерпания WSGI executor требуется параллельная нагрузка, но доступный без входа блокирующий ресурс и availability-oracle подтверждены.",
    "current_location": "routes/mqtt_api.py:212-244; app.py:440-466,671-680; run.py:69-78",
    "introduced_by_branch": false,
    "reproduction": "Запрос через Flask test client без логина при TESTING=False создал автоматическую роль guest, вернул 200 и вызвал fake MQTT connect('broker.internal', 1883, 3), предварительно передав username_pw_set('u', 'secret')."
  },
  {
    "inventory_index": 53,
    "title": "Правка MQTT-сервера (host/port/пароль) не инвалидирует ни кэш клиентов публикатора, ни подписки SSE-хаба — команды продолжают уходить на старый брокер до рестарта",
    "real": true,
    "reason": "Publisher-кэш индексируется только server id и возвращает любой is_connected()==True клиент, не сравнивая host, port, credentials или TLS. Истечение пяти минут _SERVER_CACHE не помогает: новый dict сервера всё равно сопоставляется со старым подключённым клиентом. PUT не вызывает _invalidate_client или reload_hub. SSE-хаб также хранит клиент и замыкания подписок, собранные из старой конфигурации. При живом старом брокере рассинхронизация бессрочна; при его разрыве publisher со временем пересоздастся, но SSE-хаб всё равно требует reload/restart.",
    "current_location": "routes/mqtt_api.py:85-101; services/mqtt_pub.py:50-72,249-295,310-323; services/sse_hub.py:167-176,351-395,402-430; templates/mqtt.html:164-180",
    "introduced_by_branch": false,
    "reproduction": "В _MQTT_CLIENTS был помещён connected-клиент id=4, затем get_or_create_mqtt_client вызван с тем же id, но новым host/port/password. Результат same_cached_client_after_host_change=True; фабрика нового клиента не вызывалась. rg подтверждает отсутствие invalidation/reload в MQTT CRUD."
  },
  {
    "inventory_index": 39,
    "title": "Блокирующий cl.connect() под глобальным _MQTT_CLIENTS_LOCK: мёртвый брокер задерживает OFF-команды на здоровый брокер",
    "real": true,
    "reason": "Второй критический блок удерживает общий lock во время создания клиента, TLS setup, connect и loop_start. Поэтому поток для уже закешированного клиента другого server id не может пройти даже первую проверку кэша, пока чужой connect не завершится. Paho 2.1.0 использует socket timeout 5 секунд; многосерверная конфигурация и конкурентные scheduler/HTTP/StateVerifier-публикации поддерживаются. Уточнение к finding: если первоначальный get_or_create вернул None, _publish_one не делает немедленный invalidate+повторное создание; повторные пятисекундные стойла возникают от последующих publish/StateVerifier retry, а не внутри одного такого вызова.",
    "current_location": "services/mqtt_pub.py:50-58,70-132,276-295; services/observed_state.py:55-66,122-148",
    "introduced_by_branch": false,
    "reproduction": "Двухпоточная репродукция удержала fake DeadClient.connect под lock и одновременно запросила уже закешированный healthy server. Через 250 мс healthy-поток всё ещё был заблокирован; после release вернулся тот же healthy-клиент. Вывод: blocked_while_connect=True, wait_seconds=0.255."
  }
]

### /root/d_mqtt_2_refuter

[
  {
    "inventory_index": 84,
    "title": "_publish_one/_invalidate_client разрушает разделяемый MQTT-клиент, которым в этот момент публикуют другие треды — каскад ложных сбоев OFF-команд",
    "real": true,
    "reason": "_MQTT_CLIENTS хранит один клиент на server id, а _publish_one не сериализован по клиенту. После неудачи _invalidate_client(sid) удаляет и останавливает текущий объект в кеше, не проверяя, что это тот же объект, на котором ошибся вызывающий тред. Поэтому запоздавший воркер со старым клиентом A может удалить уже созданный другим воркером клиент B, пока B используется. Реальный путь параллелизма — ThreadPoolExecutor в exclusive_start_zone. Нужна как минимум исходная MQTT-ошибка; утверждение «на ровном месте» преувеличено, а return publish_mqtt_value в peer-off вообще игнорируется. Код идентичен на main.",
    "current_location": "services/mqtt_pub.py:36-132,249-295; services/zone_control.py:407-471",
    "introduced_by_branch": false,
    "reproduction": "Запустить минимум три параллельных _publish_one для разных топиков одного sid. Заставить два вызова на клиенте A завершиться False со сдвигом: после того как первый воркер инвалидирует A и создаст B, разрешить второму вернуть False. Его _invalidate_client(sid) вызовет loop_stop/disconnect уже у B. На реальном пути это воспроизводится параллельным peer-off группы на одном брокере при нескольких QoS2 timeout."
  },
  {
    "inventory_index": 134,
    "title": "SSE-хаб — единственный подписчик на эхо реле — запускается только при первом открытии страницы: после рестарта сервиса без веб-клиента внешние включения реле невидимы и никогда не останавливаются",
    "real": true,
    "reason": "initialize_app запускает scheduler, watchdog, boot-sync, monitors и publisher warm-up, но не ensure_hub_started. Единственный первоначальный вызов хаба находится в GET /api/mqtt/zones-sse; reload_hub до первого старта явно no-op. Постоянного альтернативного подписчика зонных топиков нет, а StateVerifier создаёт лишь краткую подписку после команды приложения. ZoneWatchdog рассматривает только state='on' из БД. Оговорка: обычный старый retained '1' boot-sync пытается перезаписать retained OFF, поэтому надёжное воспроизведение — внешнее ON после boot либо неуспешный boot-sync. Та же архитектура на main.",
    "current_location": "services/sse_hub.py:151-400,402-430; routes/zones_watering_api.py:238-262; services/app_init.py:32-115; services/watchdog.py:76-109",
    "introduced_by_branch": false,
    "reproduction": "Перезапустить сервис и не открывать страницу зон/EventSource; после успешного boot-sync внешним MQTT-клиентом или физической кнопкой включить реле. _SSE_HUB_STARTED остаётся False, зона в БД остаётся off, schedule_zone_stop не появляется и watchdog её пропускает. После открытия /api/mqtt/zones-sse повторное ON уже обновляет БД и ставит stop-job."
  },
  {
    "inventory_index": 54,
    "title": "SSE-хаб не создаёт MQTT-клиента для сервера, на котором есть только топик мастер-клапана (без зон) — master_valve_observed никогда не обновляется",
    "real": true,
    "reason": "_rebuild_subscriptions корректно помещает мастер-топик в mv_topics, но ensure_hub_started итерирует только zone_topics.items(). Клиент и master-подписки создаются лишь внутри этой итерации. API и UI разрешают независимо выбрать master_mqtt_server_id, поэтому отдельный брокер без зон — достижимая конфигурация; publisher warm-up не подписывается на него. Идентичный цикл присутствует на main.",
    "current_location": "services/sse_hub.py:122-148,167-175,361-395; routes/groups_api.py:67-103; static/js/zones.js:185-203",
    "introduced_by_branch": false,
    "reproduction": "Создать сервер A с зоной и сервер B, выбранный только как master_mqtt_server_id группы. Запустить zones-sse. В _SSE_HUB_MQTT будет только A; публикация на master-топик B не изменит master_valve_observed и не породит mv_state."
  },
  {
    "inventory_index": 81,
    "title": "SSE-хаб игнорирует TLS-настройки MQTT-сервера — для tls_enabled=1 брокера эхо реле не приходит вообще (как и probe/status/scan)",
    "real": true,
    "reason": "SSE-хаб создаёт Client, задаёт credentials и вызывает connect без tls_set/tls_insecure_set. Те же TLS-пропуски есть в probe, status и scan-sse, тогда как publisher и StateVerifier TLS применяют. На TLS-only брокере хаб не получит CONNACK и эхо. Деталь сценария неточна: paho connect ждёт только TCP и отправляет CONNECT, поэтому /status может ложно вернуть connected=true, а probe/scan могут просто остаться без сообщений, а не обязательно сообщить connect failed. Отсутствие TLS и потеря подписки реальны и существуют на main.",
    "current_location": "services/sse_hub.py:168-175,377-398; routes/mqtt_api.py:156-203,228-241,295-340; services/mqtt_pub.py:77-90; services/observed_state.py:183-195",
    "introduced_by_branch": false,
    "reproduction": "Настроить TLS-only Mosquitto на 8883 и запись tls_enabled=1. Команда через publish_mqtt_value проходит с TLS, но после открытия zones-sse клиент хаба многократно посылает plaintext MQTT CONNECT и не получает эхо. В unit-spy Client.tls_set остаётся не вызванным; /status способен при этом ошибочно вернуть true после успешного TCP connect."
  },
  {
    "inventory_index": 113,
    "title": "SSE-хаб инвертирует master_valve_observed для мастер-клапанов в режиме NO: эхо '1' всегда трактуется как 'open', игнорируя master_mode",
    "real": true,
    "reason": "Обработчик master-топика переводит любой truthy payload в open, не загружая группу и master_mode. Командные пути однозначно используют для NO open_val='0' и close_val='1'; следовательно эхо закрытия перезаписывает observed в open и наоборот. /api/status напрямую отображает это поле. Для воспроизведения брокер мастера должен также входить в zone_topics, иначе раньше срабатывает дефект №54. Код идентичен на main.",
    "current_location": "services/sse_hub.py:201-219; services/zone_control.py:161-166,366-372; routes/groups_api.py:485-527; routes/system_status_api.py:417-423",
    "introduced_by_branch": false,
    "reproduction": "На брокере, где есть хотя бы одна зона, создать группу use_master_valve=1 с master_mode='NO'. После старта хаба опубликовать '1' на master-топик: БД и SSE покажут open, хотя это штатное значение закрытия NO. Публикация '0' даст closed вместо open."
  },
  {
    "inventory_index": 83,
    "title": "Контр-команда OFF при EMERGENCY_STOP/анти-рестарте — fire-and-forget QoS0 без retain, а observed_state='off' пишется независимо от успеха публикации",
    "real": true,
    "reason": "Обе контр-команды вызывают injected publish_mqtt_value только с тремя позиционными аргументами, то есть qos=0 и retain=False. Возвращаемый bool не проверяется. new_state заранее меняется на off и затем безусловно записывается как state/observed_state, даже если publisher вернул False. Emergency-ветка достижима независимо от мёртвого anti-restart. Реализация та же на main.",
    "current_location": "services/sse_hub.py:221-245,247-325; services/mqtt_pub.py:298-305",
    "introduced_by_branch": false,
    "reproduction": "При запущенном хабе выставить EMERGENCY_STOP=True, подменить publish_mqtt_value возвратом False и передать _on_message payload='1' зонного топика. Вызов публикации не содержит qos/retain, но update_zone_state получает state='off', observed_state='off'. Модель реле, не меняющая состояние при неуспешной команде, остаётся физически ON."
  },
  {
    "inventory_index": 67,
    "title": "Анти-рестарт-окно SSE-хаба мертво: mark_zone_stopped не вызывается ни одним стоп-путём, recently_stopped всегда False — защита от эхо-включения после ручного стопа недостижима",
    "real": true,
    "reason": "В production-коде mark_zone_stopped только определяется и присваивается алиасу app._mark_zone_stopped; вызовов нет. Оба HTTP stop-пути делегируют services.zone_control.stop_zone либо cancel_group_jobs, и ни центральный stop, ни fallback не взводят _LAST_MANUAL_STOP. Поэтому после обычного процесса boot карта пуста и anti-restart условие недостижимо. Unit-тесты проверяют функцию лишь прямым искусственным вызовом. На main набор production-call-site такой же.",
    "current_location": "services/sse_hub.py:97-113,233-245; app.py:709-710; routes/zones_watering_api.py:60-117,428-453; services/zone_control.py:764-875",
    "introduced_by_branch": false,
    "reproduction": "Очистить _LAST_MANUAL_STOP, остановить активную зону через /api/zones/<id>/stop или /mqtt/stop и сразу проверить recently_stopped(id): вернётся False. Переданный в течение пяти секунд ON-echo проходит ветку как обычное внешнее включение, обновляет БД и ставит schedule_zone_stop."
  }
]

### /root/d_mqtt_3_refuter

[
  {
    "inventory_index": 82,
    "title": "keepalive=5 c у клиента SSE-хаба + блокирующие DB/publish-вызовы внутри _on_message → брокер сам выкидывает хаб по таймауту пинга",
    "real": true,
    "reason": "Ядро подтверждено, но publish-сценарий преувеличен. Paho 2.1.0 выполняет on_message синхронно в единственном network-loop thread, поэтому блокировка callback задерживает PINGREQ/PUBACK. В callback есть синхронные SQLite-записи, а busy_timeout равен 30 секундам — больше брокерного лимита 1.5×5=7.5 секунды. Однако контр-публикация использует qos=0 по умолчанию, поэтому указанные wait_for_publish по 5 секунд и backoff 1/2/4 здесь не выполняются. Обычный relay-ON DB-путь и EMERGENCY_STOP достижимы. Релевантный код присутствует на main; изменения ветки касаются reload_hub и комментария.",
    "current_location": [
      "services/sse_hub.py:177-349",
      "services/sse_hub.py:223-245",
      "services/sse_hub.py:247-333",
      "services/sse_hub.py:381",
      "services/sse_hub.py:394",
      "db/base.py:39-53",
      "services/mqtt_pub.py:192-219",
      "services/mqtt_pub.py:298-305"
    ],
    "introduced_by_branch": false,
    "reproduction": "На временной IrrigationDB отдельное соединение удерживало BEGIN IMMEDIATE 8.2 секунды. Вызов захваченного текущего relay-ON callback занял 8.254 секунды: больше 7.5-секундного MQTT keepalive-предела. Исходники установленного paho-mqtt 2.1.0 подтверждают синхронный callback до QoS1 PUBACK.",
    "duplicates": []
  },
  {
    "inventory_index": 103,
    "title": "mqtt.html: updateStatuses запускает новую вечную цепочку опроса при каждом loadServers — опрос /api/mqtt/*/status умножается после каждого сохранения сервера",
    "real": true,
    "reason": "loadServers безусловно вызывает updateStatuses. Каждый вызов updateStatuses независимо планирует setTimeout(updateStatuses, 5000), без timer handle или guard. Успешные create/update/delete снова вызывают loadServers и навсегда добавляют ещё одну цепочку. Каждая цепочка опрашивает все строки, а status endpoint создаёт Paho-клиент с сохранённым client_id и синхронно вызывает connect. Файлы целиком идентичны main.",
    "current_location": [
      "templates/mqtt.html:99-141",
      "templates/mqtt.html:143-199",
      "templates/mqtt.html:201-220",
      "routes/mqtt_api.py:212-244"
    ],
    "introduced_by_branch": false,
    "reproduction": "Функции из фактического шаблона выполнены в минимальном Node DOM/API harness. Три вызова loadServers создали три независимых таймера с задержками [5000,5000,5000]. В браузере то же видно по росту количества /status-запросов после каждого сохранения.",
    "duplicates": [
      {
        "inventory_index": 120,
        "kind": "exact"
      }
    ]
  },
  {
    "inventory_index": 120,
    "title": "mqtt.html: каждый вызов loadServers() порождает новую вечную цепочку updateStatuses — поллинг статусов брокеров множится с каждым сохранением сервера",
    "real": true,
    "reason": "Дефект реален, но это точный дубль №103: совпадают файл, строки, timer-chain, CRUD call-sites, backend endpoint и последствия. Файлы идентичны main.",
    "current_location": [
      "templates/mqtt.html:99-141",
      "templates/mqtt.html:143-199",
      "templates/mqtt.html:201-220",
      "routes/mqtt_api.py:212-244"
    ],
    "introduced_by_branch": false,
    "reproduction": "Повтор воспроизведения №103: каждый дополнительный успешный CRUD вызывает loadServers и создаёт ещё один независимый вечный setTimeout-цикл.",
    "duplicates": [
      {
        "inventory_index": 103,
        "kind": "exact",
        "canonical": true
      }
    ]
  },
  {
    "inventory_index": 85,
    "title": "/api/mqtt/<id>/probe: duration из тела запроса не валидируется — гигантское значение/『1e999』 навсегда блокирует HTTP-воркер, нечисловое даёт 500",
    "real": true,
    "reason": "duration преобразуется через float без проверки типа, диапазона и конечности. abc вызывает непойманный ValueError и 500; 1e999 становится inf. Находка даже сильнее указанного сценария: on_message сохраняет максимум 1000 сообщений, а цикл ждёт len(received)<5000, поэтому трафик никогда не завершит inf-цикл. Уточнение: штатный UI — фиксированный select 2/3/5/10 секунд; нужен прямой аутентифицированный POST или изменение DOM/запроса. Трейсбек будет в серверном журнале, не обязательно в production-ответе. Код идентичен main.",
    "current_location": [
      "routes/mqtt_api.py:123-206",
      "routes/mqtt_api.py:150",
      "routes/mqtt_api.py:174-180",
      "routes/mqtt_api.py:194-195",
      "routes/mqtt_api.py:204",
      "templates/mqtt.html:59-64",
      "templates/mqtt.html:281-285",
      "templates/mqtt.html:417-420",
      "templates/mqtt.html:452-453"
    ],
    "introduced_by_branch": false,
    "reproduction": "Аутентифицированный POST с duration=\"abc\" проходит до непойманного ValueError и возвращает 500. Для duration=\"1e999\": float даёт inf, условие времени остаётся истинным, а len(received) физически не превышает 1000 при проверке <5000, поэтому обработчик не завершится.",
    "duplicates": [],
    "related_not_duplicate": [
      80
    ]
  },
  {
    "inventory_index": 86,
    "title": "scan-sse: через 300 с MQTT-тред тихо завершается, а SSE-генератор продолжает слать ping вечно — скан «замерзает», слот per-IP-лимита остаётся занят",
    "real": true,
    "reason": "Через 300 секунд MQTT-тред выходит из цикла, останавливает loop и disconnect, но не выставляет stop_event и не отправляет sentinel. SSE-генератор имеет независимый while True и продолжает ping. UI игнорирует ping и прекращает скан только в EventSource.onerror, которого у живого SSE-соединения не будет. Per-IP счётчик уменьшается лишь в finally генератора, поэтому слот остаётся занят. Код идентичен main.",
    "current_location": [
      "routes/mqtt_api.py:24-27",
      "routes/mqtt_api.py:257-269",
      "routes/mqtt_api.py:291-369",
      "routes/mqtt_api.py:330-335",
      "routes/mqtt_api.py:346-365",
      "templates/mqtt.html:350-405"
    ],
    "introduced_by_branch": false,
    "reproduction": "Открыть скан штатной кнопкой и оставить вкладку более чем на 300 секунд. После этого новые MQTT-сообщения исчезнут, но ping продолжатся и UI останется в состоянии активного скана. Счётчик соединений уменьшится только после закрытия EventSource/ответа.",
    "duplicates": [],
    "related_not_duplicate": [
      116
    ]
  },
  {
    "inventory_index": 116,
    "title": "scan-sse: тред MQTT-клиента обёрнут в `except queue.Full` — сбой connect убивает тред без сообщения, SSE вечно шлёт ping по мёртвому скану",
    "real": true,
    "reason": "Внешний except MQTT-треда ловит только queue.Full, хотя queue.Full от put_nowait уже обработан внутри callback. ConnectionRefusedError/OSError из client.connect завершает daemon-thread и не передаётся SSE-генератору; route-level except не может поймать ошибку другого треда. Клиент всё равно получает success/open и последующие ping. Уточнение: ошибка обычно печатается серверным threading.excepthook, но не сообщается SSE-клиенту. Код идентичен main.",
    "current_location": [
      "routes/mqtt_api.py:295-341",
      "routes/mqtt_api.py:326",
      "routes/mqtt_api.py:340-341",
      "routes/mqtt_api.py:343-365",
      "templates/mqtt.html:368-405"
    ],
    "introduced_by_branch": false,
    "reproduction": "В in-process harness Client.connect был подменён на ConnectionRefusedError(\"broker down\"). MQTT-тред завершился с этой ошибкой, после чего response выдал event: open с success=true и event: ping; _scan_sse_connections сохранял слот до явного response.close().",
    "duplicates": [],
    "related_not_duplicate": [
      86
    ]
  }
]

### /root/d_mqtt_4_refuter

[
  {
    "inventory_index": 55,
    "title": "_publish_with_retries: последняя републикация в backoff-цикле отправляется, но никогда не ожидается — функция возвращает False после фактически ушедшей команды и провоцирует дубль через новый клиент",
    "real": true,
    "reason": "Для QoS>=1 цикл ожидает подтверждение первых трёх публикаций, но после третьего неуспешного ожидания выполняет четвёртый publish и сразу возвращает False, не вызывая для нового MQTTMessageInfo wait_for_publish/is_published. _publish_one после False инвалидирует клиента и повторяет тот же topic/value через новый клиент. Фактическая доставка старой четвёртой публикации и дубль условны, поскольку publish может только поставить сообщение в очередь; также masters_failed_publish не возникает, если новый клиент успешно повторит отправку. Однако подтверждённая четвёртая публикация действительно игнорируется и уже создаёт ложный mqtt_publish_failure.",
    "current_location": [
      "services/mqtt_pub.py:195-219",
      "services/mqtt_pub.py:219-242",
      "services/mqtt_pub.py:276-295"
    ],
    "introduced_by_branch": false,
    "reproduction": "Фокусная проба с четырьмя MQTTMessageInfo [false,false,false,true] вернула result=false, publish_calls=4 и wait_counts=[1,1,1,0]. Проба через _publish_one дополнительно показала invalidated=[7] и одну повторную публикацию новым клиентом. services/mqtt_pub.py идентичен main."
  },
  {
    "inventory_index": 40,
    "title": "reload_hub выполняет пересборку хаба (blocking connect всех клиентов под _SSE_HUB_LOCK) в треде HTTP-запроса; на время пересборки теряются эхо реле",
    "real": true,
    "reason": "reload_hub синхронно вызывается CRUD HTTP-хендлерами, останавливает старые клиенты и вызывает ensure_hub_started. Последний держит _SSE_HUB_LOCK на всей последовательной пересборке, включая блокирующий paho connect с default connect-timeout 5 секунд на сервер; broadcast/register/unregister в это время блокируются. Предусловие штатное: страница зон открывает EventSource и запускает хаб. При ошибке connect цикл всё равно выставляет _SSE_HUB_STARTED=True, оставляя отсутствующий клиент неустранимым обычным ensure_hub_started; эхо этого брокера теряются до следующего reload/restart. Retained-состояние помогает только при успешной повторной подписке. Блокирование start/stop через broadcast в основном относится к группам с master valve, и физическая stop-команда обычно уже опубликована.",
    "current_location": [
      "services/sse_hub.py:164-167",
      "services/sse_hub.py:381-400",
      "services/sse_hub.py:402-430",
      "routes/zones_crud_api.py:137,151,203,246",
      "routes/groups_api.py:125",
      "routes/zones_watering_api.py:258-262",
      "static/js/zones.js:1495-1501"
    ],
    "introduced_by_branch": true,
    "reproduction": "Thread-проба с блокируемым fake connect показала, что broadcast завершается только после освобождения connect. Проба с TimeoutError дала _SSE_HUB_STARTED=True и пустой _SSE_HUB_MQTT; последующий ensure после восстановления не сделал новой попытки. Таргетные тесты sse_hub и wiring reload: 15 passed. reload_hub и HTTP-вызовы добавлены веткой."
  },
  {
    "inventory_index": 117,
    "title": "Эвикция SSE-клиента при полной очереди теряет sentinel: вытесненный клиент навсегда остаётся «живым» зомби-соединением без обновлений",
    "real": true,
    "reason": "register_client сначала удаляет oldest из реестра и затем пытается put_nowait(None). При полной очереди queue.Full подавляется, поэтому генератор не получает сигнал завершения. Он дочитывает 100 старых сообщений, затем бесконечно получает Empty и отправляет ping; новые события уже не поступают, поскольку очередь удалена из реестра. EventSource не имеет собственного watchdog/onerror для такого формально живого соединения. «Навсегда» означает до закрытия вкладки/TCP, рестарта или другой внешней ошибки. Прямые MQTT fan-out пути также подавляют queue.Full без sentinel.",
    "current_location": [
      "services/sse_hub.py:459-464",
      "routes/zones_watering_api.py:262-280",
      "static/js/zones.js:1498-1515"
    ],
    "introduced_by_branch": false,
    "reproduction": "При MAX_SSE_CLIENTS=1 первая очередь была заполнена до maxsize=100, затем зарегистрирован второй клиент. Первая очередь исчезла из реестра; после чтения 100 элементов sentinel не встретился, следующий get дал Empty. Таргетные штатные тесты прошли, но существующий eviction-тест проверяет только неполную очередь. Блок идентичен main."
  },
  {
    "inventory_index": 121,
    "title": "mqtt.html: создание/сохранение MQTT-сервера молча проглатывает ошибку сервера; нечисловой порт даёт непойманный TypeError → 500",
    "real": true,
    "reason": "api.request возвращает тело даже при HTTP 4xx/5xx, а createServer/updateRow показывают уведомление только при success=true и не имеют else. Поэтому JSON success:false и стандартный HTML 500 игнорируются. Backend вызывает int(port); null вызывает TypeError, строка abc — ValueError, причём repository и route эти типы не ловят. Исходный create-сценарий неточен: type=number санитизирует abc, а createServer подставляет 1883 при пустом значении. Реальный штатный путь — очистить Port существующей строки: updateRow не имеет fallback, отправляет NaN как JSON null и стабильно получает молча проглоченный 500.",
    "current_location": [
      "templates/mqtt.html:24-25",
      "templates/mqtt.html:143-183",
      "static/js/app.js:79-109",
      "routes/mqtt_api.py:50-66",
      "routes/mqtt_api.py:85-101",
      "db/mqtt.py:43-76",
      "db/mqtt.py:78-124"
    ],
    "introduced_by_branch": false,
    "reproduction": "Изолированный Flask client: POST port=null → 500/TypeError; POST port=abc → 500/ValueError; PUT port=null → 500/TypeError. UI-воспроизведение: очистить Port существующего сервера и нажать «Сохранить» — отправляется null, приходит HTML 500, уведомления нет. Четыре таргетных MQTT CRUD-теста прошли. Поведение унаследовано от main."
  },
  {
    "inventory_index": 109,
    "title": "mqtt.html: createServer/updateRow молча игнорируют ответ {success:false} — валидационная ошибка сервера не показывается вообще",
    "real": true,
    "reason": "Основной UI-дефект подтверждён: api.request возвращает JSON при !response.ok, а createServer/updateRow имеют только ветку success=true. Пример именно с серверной валидацией неверен: пустой host и порты 0/70000 сейчас сохраняются без проверки. Однако штатно достижимы success:false от общего rate limit, auth guard и отказа repository; все они молча игнорируются. Очистка порта при update также даёт молча проглоченный 500, но это отдельный невалидированный путь.",
    "current_location": [
      "templates/mqtt.html:143-184",
      "static/js/app.js:79-106",
      "routes/mqtt_api.py:50-101",
      "app.py:439-477",
      "app.py:671-706",
      "db/mqtt.py:44-124"
    ],
    "introduced_by_branch": false,
    "reproduction": "После 30 разрешённых mutating API-запросов с одного IP следующий «Создать»/«Сохранить» получает 429 application/json с success:false и error_code=RATE_LIMITED. api.request возвращает объект, success-ветка не выполняется, catch не срабатывает, уведомления нет. Четыре таргетных MQTT CRUD-теста прошли; затронутый код не изменён относительно main."
  },
  {
    "inventory_index": 104,
    "title": "mqtt.html: buildDisplayRegex ломает MQTT-wildcard '+' — сканер топиков показывает пустое дерево при фильтре с '+'",
    "real": true,
    "reason": "buildDisplayRegex сначала экранирует плюс до \\+, затем заменяет сам плюс на [^/]+, оставляя перед ним обратный слэш. Получается некорректный для ожидаемой семантики сегмент \\[^/]+, который не совпадает с одним уровнем MQTT-топика. Живой путь toggleScan → startSSE передаёт исходный '+' брокеру как корректный subscription filter, но затем отбрасывает полученные сообщения этим display regex. Мёртвость legacy startScanLoop дефект не опровергает, поскольку startSSE использует ту же функцию.",
    "current_location": [
      "templates/mqtt.html:233-243",
      "templates/mqtt.html:350-375",
      "templates/mqtt.html:408-426"
    ],
    "introduced_by_branch": false,
    "reproduction": "Node-проба: buildDisplayRegex('/devices/+/controls/K1') создал /^\\/?\\/devices\\/\\[^\\/]+\\/controls\\/K1/ и test('/devices/wb-mr6c_101/controls/K1') вернул false; буквальный фильтр и /devices/# вернули true. templates/mqtt.html побайтно идентичен main."
  }
]

### /root/e_web_1_refuter

[
  {
    "inventory_index": 91,
    "title": "POST /api/env (запись конфигурации env-датчиков) доступен анонимно и освобождён от CSRF — межсайтовая форма стирает конфиг датчиков",
    "real": true,
    "reason": "Основная auth-находка подтверждена: гость без Flask-сессии и CSRF-токена может отправить JSON POST и изменить конфигурацию. /api/env одновременно находится в публичном whitelist и CSRF-exempt; обработчик не имеет admin_required и сохраняет данные через set_env_config. Пустой JSON отключает оба датчика и очищает топики. Публичность не нужна текущему UI: единственный POST-call-site находится на admin-only странице зон, а общий JS уже добавляет CSRF-токен. Однако заявленный drive-by через обычную HTML-форму неверен: при Flask 3.1 request.get_json() для form-urlencoded возвращает 415, а cross-origin application/json требует успешного CORS preflight. Viewer также получает 403 до whitelist. Реальная атака — прямой анонимный JSON-клиент к открытому :8080 либо клиент, прошедший внешний Basic Auth.",
    "current_location": "app.py:156-173, app.py:404-407, app.py:448-466; routes/system_config_api.py:236-273; db/settings.py:160-174; static/js/zones.js:909; routes/zones.py:8-11",
    "introduced_by_branch": false,
    "reproduction": "При TESTING=false и включённом CSRF отправить без сессии и X-CSRFToken: POST /api/env, Content-Type: application/json, body={}. Ответ 200 success=true; обе env-конфигурации становятся disabled с пустыми topic. Контрольный form-urlencoded POST возвращает 415 и ничего не меняет. Точечный test_guest_endpoints_are_exempt с -p no:cacheprovider прошёл. На main whitelist, exemption, handler и DB-семантика те же."
  },
  {
    "inventory_index": 135,
    "title": "Ручное открытие мастер-клапана не отменяет взведённый таймер отложенного закрытия — клапан молча закрывается через delay после открытия",
    "real": true,
    "reason": "Подтверждено. Ручной action=open публикует OPEN, обновляет observed и ставит отдельный 24-часовой APScheduler cap, но не удаляет threading.Timer из services.zone_control._PENDING_CLOSE_TIMERS. Callback отложенного закрытия проверяет только зоны в состояниях on/starting; факт ручного открытия мастер-клапана и 24h cap он не учитывает. Поэтому при отсутствии активной зоны старый таймер публикует CLOSE. Единственная штатная отмена таких таймеров происходит при новой _schedule_master_close для того же topic либо emergency_stop_all.",
    "current_location": "routes/groups_api.py:458-559, особенно 525-548; services/zone_control.py:71-78, 129-166, 213-239; irrigation_scheduler.py:1469-1514",
    "introduced_by_branch": false,
    "reproduction": "Остановить зону группы с master_close_delay_sec=60, затем через 30 секунд вызвать POST /api/groups/<gid>/master-valve/open. Не запускать зоны. Через оставшиеся 30 секунд callback старого таймера видит отсутствие on/starting зон и публикует CLOSE, несмотря на успешный ручной OPEN и новый 24h cap. Соответствующая логика присутствует и на main."
  },
  {
    "inventory_index": 118,
    "title": "Общий rate-limit-бакет «mqtt_control» (10 req/мин на IP) объединяет старт и СТОП зоны — ручная остановка получает 429, зона продолжает поливать",
    "real": true,
    "reason": "Подтверждено. Оба endpoint используют одну group=\"mqtt_control\", а ключ лимитера равен (remote_addr, group); HTTP path и start/stop в ключ не входят. Декоратор выполняется до handler, поэтому 11-й запрос блокируется независимо от того, является ли он безопасным STOP. Основные zone-level UI call-sites используют именно /mqtt/stop и fallback на другой endpoint не делают. Масштаб в исходном сценарии немного неточен: после шести START первые четыре STOP будут запросами 7-10, пятый STOP станет 11-м и получит 429. Остановить полив всё ещё можно через отдельный /api/zones/<id>/stop, group stop или emergency stop, поэтому это не абсолютная невозможность остановки, а отказ обычной кнопки зоны.",
    "current_location": "routes/zones_watering_api.py:292-295 и 386-389; services/api_rate_limiter.py:40-66, 83-109; app.py:681-688; static/js/status.js:692-719, 1803-1819, 2340-2371; static/js/zones.js:1107-1158",
    "introduced_by_branch": false,
    "reproduction": "С одного IP за менее чем минуту запустить шесть долгих зон в разных группах, затем останавливать их через /mqtt/stop. Запросы 7-10 проходят, пятый STOP является 11-м и получает 429; соответствующая зона остаётся включённой до auto-stop либо альтернативной команды. Точечный тест лимитера с -p no:cacheprovider подтвердил блокировку 11-го вызова. Декораторы и ключ бакета идентичны main."
  },
  {
    "inventory_index": 96,
    "title": "user_required отвергает роль \"viewer\": гостевой просмотр (/login?guest=1) полностью нерабочий — редирект на логин вместо статус-страницы",
    "real": true,
    "reason": "Несовместимость ролей реальна: /login?guest=1 записывает viewer и редиректит на status_bp.index, но user_required разрешает только guest/user/admin, поэтому следующий GET / возвращает 302 на /login. После этого пользователь видит форму входа; бесконечного redirect-loop нет. Практический масштаб меньше заявленного: текущая кнопка «Продолжить без пароля» в templates/login.html ведёт на /?guest=1, а не /login?guest=1; query там игнорируется, before_request создаёт role=guest, и страница открывается. То есть сломан явно реализованный viewer-route, но текущий UI обходит его и остаётся в более привилегированной guest-роли. Viewer также сохраняет GET-доступ к API.",
    "current_location": "services/security.py:42-53; routes/auth.py:32-40; routes/status.py:31-40; routes/files.py:8-11; app.py:448-465; templates/login.html:129",
    "introduced_by_branch": false,
    "reproduction": "С TESTING=false выполнить GET /login?guest=1 с follow redirects. Цепочка: 302 /, затем 302 /login, затем 200 с формой входа; в сессии остаётся role=viewer. Для сравнения, GET /?guest=1 новой сессией открывает статус как role=guest. routes/auth.py и services/security.py идентичны main."
  },
  {
    "inventory_index": 52,
    "title": "decrypt_secret: при смене/утере ключа AES-ветка бросает ValueError('MAC check failed') — рушатся get_mqtt_server, все publish-пути и список серверов в API",
    "real": true,
    "reason": "Подтверждено фактическим вызовом: decrypt_secret перехватывает в AES-ветке только ImportError, поэтому неверный AES-GCM key даёт необработанный ValueError: MAC check failed. MqttRepository передаёт его наружу, поскольку get_mqtt_server(s) ловит только sqlite3.Error. API списка серверов, SSE hub и большинство start/stop/publish путей ValueError также не ловят. При наличии зон с MQTT ошибка может возникнуть уже в boot sync app_init и сорвать импорт/готовность приложения. Обычное production-обновление ключ не меняет: systemd фиксирует WorkingDirectory, а ignored .irrig_secret_key переживает git reset и попадает в backup snapshot. Реальный триггер — потеря ключа, перенос одной БД без ключа, ошибочный IRRIG_SECRET_KEY либо нестандартный CWD.",
    "current_location": "utils.py:84-116 и 141-168; db/mqtt.py:15-40; services/mqtt_pub.py:307-325, 396-399; services/sse_hub.py:151-170; routes/mqtt_api.py:35-82; services/app_init.py:164-198; wb-irrigation.service:10-14",
    "introduced_by_branch": false,
    "reproduction": "Зашифровать пароль при IRRIG_SECRET_KEY=base64(32 байта A), затем в том же процессе заменить env на base64(32 байта B) и вызвать decrypt_secret(ciphertext). Получен ValueError: MAC check failed. Далее запись ENC:<ciphertext> в mqtt_servers заставляет get_mqtt_server/get_mqtt_servers пробросить тот же ValueError. utils.py и чтение паролей в db/mqtt.py в релевантной части совпадают с main."
  },
  {
    "inventory_index": 57,
    "title": "GET /login?guest=1 меняет сессию (сброс + role=viewer) без CSRF — можно принудительно понизить/разлогинить авторизованного админа",
    "real": true,
    "reason": "State-changing GET подтверждён: login_page без проверки текущей роли вызывает session.clear() и записывает viewer. Top-level переход или redirect злоумышленника на /login?guest=1 отправляет Lax cookie и заменяет admin-сессию; повторный парольный вход необходим. Заявленный вариант через <img> для современных браузеров ненадёжен: Flask фактически не задаёт SameSite явно, а браузеры применяют Lax-by-default и обычно не отправляют такую cookie в cross-site subresource. Кроме того, текущий UI вообще вызывает /?guest=1, а не этот route. Ущерб ограничен временным downgrade/logout, без повышения привилегий, и после redirect пользователь заметно оказывается на login page.",
    "current_location": "routes/auth.py:13-40; services/security.py:42-53; app.py:372-376; templates/login.html:129",
    "introduced_by_branch": false,
    "reproduction": "Создать admin-сессию, затем выполнить top-level GET /login?guest=1 с той же cookie. Ответ устанавливает новую session cookie с logged_in=true, role=viewer; последующий GET / приводит на /login из-за finding 96, а мутации API получают 403. routes/auth.py идентичен main."
  },
  {
    "inventory_index": 56,
    "title": "Все Flask-рейт-лимитеры используют request.remote_addr без ProxyFix — за nginx все клиенты схлопываются в одно ведро, блокировка логина затрагивает всех",
    "real": true,
    "reason": "Условный дефект подтверждён: login limiter, общий API limiter и MQTT SSE limit используют request.remote_addr; ProxyFix или другой доверенный разбор X-Forwarded-For отсутствует. При одном reverse proxy backend действительно видит IP proxy и делит бакет между клиентами. После пяти записанных ошибок шестой login-check блокирует общий ключ на 900 секунд. Но native WB deployment из install_wb.sh/systemd запускает Hypercorn напрямую на 0.0.0.0:8080 и nginx не устанавливает, поэтому на поддерживаемом LAN-пути remote_addr является адресом клиента. Репозиторий содержит лишь reference nginx snippet, не auto-deployed полный config; live proxy topology по репо окончательно не доказуема. Эффект реален именно для документированного внешнего nginx-host, а не для каждого production-запуска.",
    "current_location": "routes/auth.py:50-70; services/api_rate_limiter.py:83-109; routes/mqtt_api.py:250-269; app.py:671-706; run.py:62-78; configs/nginx-rate-limit.conf:1-18; wb-irrigation.service:10-14",
    "introduced_by_branch": false,
    "reproduction": "Проксировать два разных клиента через один nginx к Hypercorn без ProxyFix. Оба Flask-request имеют одинаковый remote_addr. Записать пять неверных логинов от клиента A; правильный шестой запрос клиента B получает 429 с Retry-After около 900. При прямом обращении клиентов к :8080 их remote_addr различаются, и проблема не воспроизводится. Точечный LoginRateLimiter test с -p no:cacheprovider прошёл; основной код совпадает с main."
  }
]

### /root/e_web_2_refuter

[
  {
    "inventory_index": 125,
    "title": "PUT /api/programs/<id> не прогоняет _validate_program_v2_fields (в отличие от POST) и обращается к data[\"time\"]/[\"zones\"]/[\"days\"] без .get — асимметричный контракт: апдейтом сохраняется программа, которую планировщик молча не планирует; частичный PUT падает 500",
    "real": true,
    "reason": "PUT каноникализирует только even_odd, но не вызывает _validate_program_v2_fields; POST вызывает его. ProgramRepository.update_program динамически сохраняет переданные поля без CHECK-ограничений. После сохранения schedule_program сначала снимает прежние jobs, а _schedule_single_time для неизвестного schedule_type не выполняет ни одной ветки и оставляет пустой job_ids. Для interval с NULL interval_days int(None) перехватывается внутри планировщика, также оставляя программу без jobs. Обращение к data[\"time\"], data[\"zones\"], data[\"days\"] происходит до update и ловит только sqlite3.Error/OSError, поэтому валидный JSON без любого из ключей даёт необработанный KeyError и 500. Даже если не считать частичный PUT нормативным контрактом HTTP, плохой ввод всё равно не должен превращаться в 500. Эндпоинт в production admin-only и требует CSRF. В main обе вредные семантики уже есть; ветка лишь добавила валидацию POST, сделав асимметрию заметнее.",
    "current_location": "routes/programs_api.py:25-43,52-94,109-150; db/programs.py:96-159; irrigation_scheduler.py:1140-1208,1210-1297; app.py:131-180,440-470",
    "introduced_by_branch": false,
    "reproduction": "Создать обычную weekdays-программу, затем от admin с CSRF выполнить PUT /api/programs/<id> с полным payload и schedule_type=\"invalid-type\": ответ 200, GET возвращает сохранённое значение, а старые jobs сняты и новые отсутствуют. Отдельно выполнить PUT с {\"name\":\"New\"}: data[\"time\"] вызывает KeyError, в production получается 500."
  },
  {
    "inventory_index": 102,
    "title": "/api/logs: фильтры from/to/type применяются в Python ПОСЛЕ 'LIMIT 1000 новейших' — запросы за старые периоды возвращают пусто при живых данных в БД",
    "real": true,
    "reason": "Роут читает from/to/type, но вызывает db.get_logs() без них. Репозиторий при таком вызове выполняет ORDER BY timestamp DESC LIMIT 1000, и только затем роут фильтрует полученный срез в Python. При этом facade/repository уже поддерживают SQL-фильтры event_type/from_date/to_date. GET-доступ разрешён гостю глобальным auth-hook. Релевантный код на main совпадает.",
    "current_location": "routes/system_status_api.py:670-701; db/logs.py:20-46; database.py:331-332; app.py:440-458",
    "introduced_by_branch": false,
    "reproduction": "Вставить одну строку type=\"target\" за 2026-06-10 и после неё 1000 строк с более новыми timestamp. GET /api/logs?type=target&from=2026-06-01&to=2026-06-15 вернёт [], тогда как db.get_logs(\"target\", \"2026-06-01\", \"2026-06-15\") вернёт старую строку."
  },
  {
    "inventory_index": 94,
    "title": "/api/zones/*/history: SQL-окно сравнивает локальные строки 'YYYY-MM-DD HH:MM:SS' с UTC-ISO границами 'YYYY-MM-DDTHH:MM:SSZ' — первый день окна выпадает целиком при TZ контроллера UTC+0 и западнее",
    "real": true,
    "reason": "Прод-вызовы create_zone_run передают datetime.now().strftime(\"%Y-%m-%d %H:%M:%S\"), то есть локальную naive-строку с пробелом. История строит UTC RFC3339-границы с T/Z и сравнивает их непосредственно как TEXT в SQLite. При одинаковой дате пробел сортируется раньше T, поэтому ни одна запись первого дня не удовлетворяет нижней границе в UTC и западных TZ. Тот же fetch используется JSON и CSV; summary/runs получают уже усечённый набор. Безопасная in-memory SQLite-проверка дала count=0 для '2026-07-12 05:00:00' при lower='2026-07-12T00:00:00Z'. Код совпадает с main. Дополнительно тесты маскируют дефект, поскольку напрямую вставляют корректные UTC-Z строки, отличающиеся от прод-записи.",
    "current_location": "routes/zones_history_api.py:61-94,169-224,324-336; services/zone_control.py:294,337-339; irrigation_scheduler.py:1715,1808,1932-1940; services/history_calc.py:191-225",
    "introduced_by_branch": false,
    "reproduction": "Запустить с TZ=UTC, создать zone_run на первой дате 7-дневного окна со start_utc='2026-07-12 05:00:00', затем запросить /api/zones/<id>/history?days=7 при today=2026-07-18. SQL использует lower='2026-07-12T00:00:00Z', запись отсутствует в runs/daily/summary и history.csv."
  },
  {
    "inventory_index": 137,
    "title": "Загрузка изображений: PIL DecompressionBombError не перехватывается — 500 вместо 400 (заявленный 50 MP декомпрешн-кап обходится)",
    "real": true,
    "reason": "На Pillow 12.3.0 Image.DecompressionBombError наследуется непосредственно от Exception, не от OSError/ValueError. Для 200000000 пикселей Image.open бросает его до img.load и до пользовательского ImageTooLargeError. Photo/map handlers ловят ImageTooLargeError и OSError/ValueError, но не этот тип; audit_log записывает исключение и повторно его бросает, а общего 500-handler нет. Поэтому production Flask отвечает 500. Уточнение: для >178956970 пикселей встроенная защита Pillow действительно предотвращает декодирование, поэтому это дефект HTTP-контракта/логирования, а не фактический OOM-обход встроенного guard. Код существовал на main.",
    "current_location": "services/image_pipeline.py:40-53; routes/zones_photo_api.py:160-265; routes/system_config_api.py:115-177; services/audit.py:299-337",
    "introduced_by_branch": false,
    "reproduction": "Admin с CSRF загружает корректный сильно сжатый PNG 20000x10000 с допустимыми filename/MIME на POST /api/zones/<id>/photo или /api/map. Image.open бросает DecompressionBombError; ни один route-except его не ловит, результат — 500 вместо IMAGE_TOO_LARGE/400. Безопасная проверка одного IHDR 20000x10000 воспроизвела исключение уже на Image.open."
  },
  {
    "inventory_index": 97,
    "title": "Decompression-bomb cap (50 MP) проверяется ПОСЛЕ полного декодирования img.load() — не предотвращает выделение памяти при загрузке изображений",
    "real": true,
    "reason": "Порядок однозначен: Image.open, затем img.load, и только потом чтение size и проверка >50M. Размер известен уже после Image.open, поэтому decode не нужен для guard. В Pillow 12.3.0 MAX_IMAGE_PIXELS=89478485: 150M пикселей вызывает лишь DecompressionBombWarning, а исключение начинается после 178956970. Предупреждения приложением не превращаются в ошибки. Следовательно, диапазон 50M..178956970 полностью декодируется до пользовательского отказа. Для RGB 150M это порядка 450 MB только под пиксели. Оба upload call-site используют этот helper; доступ admin-only и ограничен размером тела, но сильно сжатое изображение укладывается в лимит. На main порядок тот же.",
    "current_location": "services/image_pipeline.py:27-60,75-94; routes/zones_photo_api.py:46-78,160-232; routes/system_config_api.py:115-174; app.py:120-123,440-470",
    "introduced_by_branch": false,
    "reproduction": "Не выполнять на WB: в изолированном процессе подать валидный одноцветный PNG/JPEG 12000x12500. Image.open только выдаёт DecompressionBombWarning, затем img.load выделяет/декодирует полный 150 MP буфер, и лишь после этого строка 50 поднимает ImageTooLargeError. Монитор RSS фиксирует рост до срабатывания cap."
  },
  {
    "inventory_index": 131,
    "title": "next_watering: in-progress-окно программы строится по номинальным длительностям — при погодном коэффициенте <100% зонам показываются слоты, которых не будет",
    "real": true,
    "reason": "Калькулятор строит offsets и total_prog_min исключительно из zone.duration, а in_progress/elapsed и сегодняшний кандидат — из этих номинальных значений. Scheduled runner для каждой зоны вызывает _get_weather_adjusted_duration и ждёт именно скорректированную длительность. Калькулятор не сверяется ни с текущей scheduler-сессией, ни с zone_runs/state, поэтому после фактического раннего завершения продолжает считать программу активной. GET single, публичный bulk POST и /api/status используют общий helper. Ветка вынесла семантику из inline-кода main; на main номинальные offsets/in_progress уже присутствуют, а single endpoint также возвращал будущий номинальный слот.",
    "current_location": "services/next_watering.py:104-124,145-175,189-234; irrigation_scheduler.py:801-880,1029-1060; routes/zones_crud_api.py:256-304; routes/system_status_api.py:360-391",
    "introduced_by_branch": false,
    "reproduction": "Программа сегодня в 06:00, зоны id A<B<C по 20 минут, стабильный коэффициент 50%, weather skip=false. Runner фактически выполняет A 06:00-06:10, B 06:10-06:20, C 06:20-06:30. При now=06:35 compute_next_watering для C всё ещё считает номинальное окно до 07:00 активным и возвращает 06:40 — уже несуществующий будущий слот."
  },
  {
    "inventory_index": 138,
    "title": "/api/postpone: параметр days не валидируется — ValueError/OverflowError → 500 на гость-доступном CSRF-exempt эндпоинте",
    "real": true,
    "reason": "Route валидирует только group_id и без try/except передаёт days в postpone_group. Сервис сначала выполняет int(days), затем timedelta(days=days): 'abc' даёт ValueError, огромное целое — OverflowError. audit_log повторно бросает исключение, поэтому production отвечает 500; вычисление происходит до чтений/записей БД, частичного состояния нет. /api/postpone находится одновременно в _ALLOWED_PUBLIC_POSTS и явном csrf.exempt-списке, то есть доступен guest за внешним nginx perimeter. На main сервис ещё не был выделен, но inline timedelta с теми же payload давал TypeError/OverflowError и тот же 500.",
    "current_location": "routes/system_config_api.py:309-343; services/postpone.py:27-39; services/audit.py:299-337; app.py:142-173,404-413,430-466",
    "introduced_by_branch": false,
    "reproduction": "Без admin-сессии и без CSRF отправить application/json POST /api/postpone с {\"action\":\"postpone\",\"group_id\":1,\"days\":\"abc\"}: int(days) даёт ValueError и ответ 500. Payload с days=1000000000000000 достигает timedelta и даёт OverflowError/500."
  }
]

### /root/f_front_1_refuter/verify_22_76

[
  {
    "inventory_index": 22,
    "title": "loadZonesData глушит ошибки fetch в пустой массив: при недоступном сервере страница показывает «Зоны не найдены» и прячет баннер потери связи",
    "real": true,
    "reason": "Сетевой reject, HTML-ответ или не-JSON 5xx превращается во внутреннем catch в []; JSON-ошибка/объект также превращается в [] через Array.isArray. Поэтому внешний catch недостижим: zonesData затирается, карточки и статистика рендерятся как пустые, затем hideConnectionError() снимает баннер. Параллельный loadStatusData может показать баннер, но между загрузчиками есть гонка, и loadZonesData способен скрыть его последним. Уточнение: сценарий с истечением сессии/401 сейчас неприменим — GET /api/* публичны в app.py:456-458; сетевой сбой и 5xx полностью подтверждаются.",
    "current_location": "static/js/status.js:85-140, 1023-1025, 1536-1555, 1687-1710; templates/status.html:12,135-140,168-170,315-319; routes/status.py:31-40; routes/zones_crud_api.py:67-70",
    "introduced_by_branch": false,
    "reproduction": "Exact-source Node harness извлёк loadZonesData из текущего status.js, задал zonesData=[{id:7}], connectionError=true и fetch=Promise.reject(...). Результат: {\"zonesData\":[],\"renders\":[0],\"connectionError\":false}. В браузере эквивалентно временно подменить window.fetch на rejected Promise, вызвать showConnectionError(); await loadZonesData(); — #zoneList содержит «Зоны не найдены», statZonesTotal=0, connection-status без класса show. Сравнение с main: diff текущих строк 85-140 с main:176-231 и строк 1536-1555 с main:1834-1853 завершилось exit 0."
  },
  {
    "inventory_index": 76,
    "title": "Гонка оптимистичного состояния зоны с 5-секундным поллингом: только что запущенная зона визуально откатывается в «off» и приглашает повторный старт",
    "real": true,
    "reason": "Первичное доказательство inventory неточно: wantOn-ветка toggleZoneRun недостижима через обычную кнопку старта — выключенная карточка вызывает showRunPopup. Но реальный путь showRunPopup → confirmRun содержит тот же дефект: startFn оптимистично пишет state='on' и рендерит до POST; loadZonesData без sequence/dirty guard заменяет весь zonesData устаревшим GET-снимком state='off'. Обработчик успешного POST не возвращает state='on' и не перерисовывает карточки: initZoneTimer получает старый объект z, уже отсутствующий в новом zonesData, а через 2 секунды вызывается только loadStatusData. Поэтому карточка остаётся off до следующего зонного poll. Вторичный тезис про второй open zone_run не подтверждается: повторный POST сейчас попадает в already-ON ветку и reschedule; зато он действительно продлевает planned_end_time.",
    "current_location": "static/js/status.js:98-104,1023-1025,1655-1659,2143-2186,2187-2205,2317-2365; templates/status.html:270-291,319; routes/zones_crud_api.py:67-70; routes/zones_watering_api.py:292-380; services/zone_control.py:537-584",
    "introduced_by_branch": false,
    "reproduction": "Exact-source Node harness вызвал текущий confirmRun с mocked успешным /mqtt/start, после оптимистичного render заменил zonesData устаревшим off-снимком и завершил POST. Результат: {\"finalState\":\"off\",\"renders\":[\"on\",\"off\"],\"toasts\":[[\"▶ #7 Test запущена: 10 мин\",\"success\"]],\"timers\":[2000]}. Это доказывает, что успех не восстанавливает карточку. Сравнение с main: poll, кнопка старта и полный confirmRun-блок дали exit 0 (`HEAD:1023-1025 ↔ main:1265-1267`, `1655-1659 ↔ 1953-1957`, `2187-2375 ↔ 2485-2673`)."
  }
]

Рабочее дерево не изменялось; pytest не запускался.

---

## Продолжение Фазы 1 в Codex Multi-Agent V2

Дата: 2026-07-18. Проверяемый production-code HEAD: `ef830fa7d05c2f07590dfd87462327e68b8abf44`;
документационный parent-checkpoint: `42dda0a1ae1ca4f17b78d5fdb48ca52b29565e36`.

Все агенты работали read-only: production-код, тесты, ветки и коммиты не изменялись;
полный pytest не запускался. Использовались только точечные тесты с
`-p no:cacheprovider`, временные SQLite-БД и локальные mock/barrier probes.

### Недостающие опровергатели

- `/root/refuter_r1`: #20, #21, #23, #24, #25, #26, #27 — все REAL, сужены impact/guards.
- `/root/refuter_r2`: #58, #60, #61, #74, #75, #78 — REAL; #77 — FALSE как branch regression.
- `/root/refuter_r3`: #79, #93, #95, #106 — REAL; #105 — FALSE; #92 — условный REAL для Safari 15-era.
- `/root/refuter_r4`: #107, #119, #122, #123, #124 — REAL; #108 — REAL только для подписанного `/meta`/wildcard topic.

### Практики для исходных UNVERIFIED

Финальные покрытия по фактически проверенным 1-based inventory entries:

- `/root/pract_a1`: #63, #65, #87, #127, #133 — REAL.
- `/root/pract_a2`: #66, #88, #90, #128 — REAL; #126 — FALSE.
- `/root/pract_b2`: #35, #89, #111, #112, #115, #136 — REAL; #115 duplicate #136.
- `/root/pract_c1`: #41, #45, #59, #70, #71, #98, #99, #100 — REAL.
- `/root/pract_c2`: #37, #42, #43, #44, #68, #101, #129 — REAL.
- `/root/pract_d1`: #39, #49, #51, #53, #69, #80, #114 — REAL.
- `/root/pract_d3`: #55, #82, #85, #86, #103, #116 — REAL.
- `/root/pract_d4`: #40, #104, #109, #117, #120, #121 — REAL; #120 duplicate #103.
- `/root/pract_e2`: #94, #97, #102, #125, #131, #137, #138 — REAL.
- `/root/pract_f1`: #22, #58, #74, #76, #92, #93 — REAL; #105 — FALSE.
- `/root/pract_f2`: #20, #21, #60, #61, #75, #106, #119 — REAL.
- `/root/pract_f3` (исправленный отчёт): #23, #25, #26, #27, #108, #124 — REAL; #77 — FALSE.
- `/root/pract_f4`: #24, #78, #79, #95, #107, #122, #123 — REAL.

Четыре исходных practitioner-отчёта использовали переданный номер как zero-based
JSON index и фактически проверили соседние записи. Оркестратор не засчитал эти
отчёты механически и запустил gap-fill по точному `jq '.[N-1]'`:

- `/root/pract_gap_b1`: #34, #36, #38, #50, #64, #110, #132 — все REAL.
- `/root/pract_gap_c3`: #46, #62, #72, #130 — все REAL.
- `/root/pract_gap_d2`: #54, #67, #81, #83, #113, #134 — все REAL.
- `/root/pract_gap_e1`: #52, #56, #91, #96, #118 — все REAL с оговорками ниже.

Ключевые уточнения gap-fill:

- #34: already-on/starting path действительно создаёт новый open `zone_run`.
- #36: master close scan/publish и pending timer registry не атомарны.
- #38: boot forced-stop закрывает crash-run как ok/failed до aborted mop-up.
- #50: retained base-topic loopback подтверждает команду без физического relay.
- #64: verifier не имеет generation token и ретраит устаревшее состояние.
- #110: `fault` не блокирует scheduler/start и сбрасывается обычным stop.
- #132: failed OFF/ON publish всё равно фиксируется как успешное DB-состояние.
- #46: строковый `"999"` сохраняется как 999, но не исключает зону из программ.
- #62: UI-shaped PUT MQTT server обнуляет отсутствующие TLS fields.
- #72: поздний echo может дать `confirmed=1,status=failed`.
- #130: postpone anchor не учитывает zone offset и уводит metadata на сутки.
- #54: server только с master topic не получает SSE MQTT client.
- #67: `mark_zone_stopped` не вызывается production stop paths.
- #81: hub/probe/status/scan не применяют TLS settings.
- #83: emergency counter-OFF — QoS0/non-retained, результат игнорируется.
- #113: NO master echo интерпретируется инвертированно.
- #134: постоянный echo hub стартует только после открытия `/zones`.
- #52: wrong/lost AES key пробрасывает `ValueError: MAC check failed`.
- #56: shared rate-limit bucket реален за внешним proxy, но не на native direct port.
- #91: anonymous JSON mutation реальна; обычная cross-site HTML form получает 415.
- #96: сломан viewer-route `/login?guest=1`, но текущий `/ ?guest=1` guest flow работает.
- #118: общий start/stop bucket может вернуть 429 на Stop; существуют альтернативные emergency/group stop paths.

### Перепроверка старых CONFIRMED

- `/root/confirmed_sse`: #2, #4, #33 — REAL; #4/#33 один root cause.
- `/root/confirmed_next_enabled`: #5 REAL; #11/#15 duplicates #5.
- `/root/confirmed_next_slots`: #12 REAL; #28 duplicate #12; #13 REAL.
- `/root/confirmed_tests`: #6, #7, #9 — REAL test-quality defects.
- `/root/confirmed_api_db`: #10, #17, #19 — REAL.
- `/root/confirmed_next_edges`: #29, #32 — REAL.
- `/root/confirmed_zone`: #3, #18 — REAL.

### Попытка опровергнуть старые REFUTED

- `/root/refuted_challenge_1`: #1, #8, #14 — прежний REFUTED отменён, все три REAL.
- `/root/refuted_challenge_2`: #30 — прежний REFUTED отменён, REAL; #16/#31 — окончательно FALSE.

### Арбитраж расхождений

- `/root/arb_90`: #90 — REAL после исправления consumer-сценария. Ложное время видно
  в глобальном active-zone sidebar, а не в group card; баг есть на main.
- `/root/arb_100`: #100 — REAL для group 999, которую wizard ошибочно разрешает
  выбрать и scheduler исключает из session guard. Для обычных групп результат —
  полный skip из #129. Искусственный TOCTOU-barrier отдельной находкой не признан.
- #92 классифицирован REAL условно: Safari 15-era не парсит пробельный datetime;
  современный Safari парсит, а minimum browser policy в проекте отсутствует.

### Пять diff-review линз и независимая перепроверка новых находок

#### Security

`/root/diff_security` + `/root/arb_status_db_amp`:

- #139 REAL/medium/branch: публичный `/api/status` вызывает cancellation lookup для
  каждой пары zone×program. Реальный blueprint с 32 зонами и 16 программ выполнил
  512 SQLite connections/SELECT; UI status+bulk способен создавать около 205
  соединений/с на вкладку в этой конфигурации.

#### Test quality

`/root/diff_test_quality` + `/root/arb_diff_test_gaps`:

- #140 REAL/low: program log/stats всегда пустые/нулевые; снятые xfail проверяют форму.
- #141 REAL/medium: smart/time-based не читаются scheduler/runner и ведут себя одинаково.
- #142 REAL/low: weekday/even-odd/extra-time tests пропустили mutation «всё Tuesday 23:59».

#### Contracts

`/root/diff_contracts` + `/root/arb_contract_new`:

- #143 REAL/medium: POST принимает отрицательный/нулевой/строковый `interval_days`;
  отрицательный interval делает APScheduler `_get_run_times` неограниченным, ноль
  нормализуется до 1 секунды.
- #144 REAL/low/branch: удалены явно stable `get_merged_weather` export и
  backward-compatibility `services.weather_merged`, хотя актуальная spec их документирует.

#### Concurrency

`/root/diff_concurrency`, `/root/arb_runtime_invariants_a`,
`/root/arb_runtime_invariants_b`, `/root/arb_conc_health_shutdown`:

- #145 REAL/high: concurrent PUT/DELETE оставляет orphan persistent program job.
- #146 REAL/high: Stop до регистрации group-start event/job теряется; start продолжает.
- #147 REAL/high: Telegram Stop не ставит cancel event, runner запускает следующую зону.
- #148 REAL/medium: Health cancel ручной зоны оставляет ownerless set Event и блокирует cron.
- #149 REAL/high: shutdown OFF-sweep не quiesce workers; late retained ON возможен после OFF.

#### Correctness

`/root/diff_correctness` + `/root/arb_correctness_new`:

- #150 REAL/low/branch: cache-only next-watering принимает weather cache любого возраста
  и может расходиться с live scheduler decision.
- #151 REAL/low: weather skip + daily 00:00 даёт первой зоне `next_dt=None`.
- #152 REAL/low: malformed program time отображается как midnight, scheduler jobs не создаёт.

### Финальный арбитраж оркестратора

- Исходные 138: `VERIFIED_REAL=133`, `VERIFIED_FALSE=5`, `UNVERIFIED=0`.
- Окончательно FALSE: #16, #31, #77, #105, #126.
- Новые diff findings: #139–#152, все `VERIFIED_REAL`.
- Финальный inventory: 152 записи, 147 REAL, 5 FALSE.
- Exact duplicates сохранены через `duplicate_of`: #11→#5, #15→#5, #28→#12,
  #31→#16, #33→#4, #51→#4, #59→#10, #61→#23, #69→#17,
  #115→#3, #120→#103, #136→#3.

На момент завершения всех сабагентов tracked diff отсутствовал, единственный status
entry — исходный пользовательский `?? AGENTS.md`.
