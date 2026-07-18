# Непроверенные находки по областям

> **Архивный план распределения.** На checkpoint 2026-07-18 все перечисленные ниже
> находки проверены двумя независимыми ролями; `UNVERIFIED=0`. Актуальные решения —
> в `findings-inventory.json` и `findings-inventory.md`. Список ниже сохранён только
> для трассировки первоначального покрытия агентов.

Разбивка 113 находок со статусом `UNVERIFIED` для агентов-верификаторов (раздел B в `agent-prompts.md`).
Один агент — одна область. Полные доказательства и сценарии — в `findings-inventory.md` по заголовку находки.

| Область | Находок | high | medium | low |
|---|---|---|---|---|
| A. Планировщик | 10 | 6 | 3 | 1 |
| B. Управление зонами и состоянием | 13 | 4 | 8 | 1 |
| C. Слой данных (SQLite) | 22 | 3 | 12 | 7 |
| D. MQTT и SSE | 26 | 3 | 13 | 10 |
| E. Web-слой, роуты, сервисы | 14 | 1 | 4 | 9 |
| F. Frontend | 28 | 2 | 12 | 14 |


## A. Планировщик

- **[high]** `irrigation_scheduler.py:835` — EMERGENCY_STOP не блокирует запуски по расписанию: планировщик не проверяет флаг, а cancel_group_jobs гасит только уже идущие сессии — программа стартует полив во время активного аварийного режима
- **[high]** `irrigation_scheduler.py:1153` — PUT программы с пустыми zones/days оставляет старые персистентные cron-джобы — «фантомная» программа поливает прежний список зон и не удаляется
- **[high]** `irrigation_scheduler.py:1552` — start_group_sequence выключает бегущие зоны только в БД: без MQTT OFF и со снятием их стоп-джобов реле остаётся физически открытым и неуправляемым
- **[high]** `irrigation_scheduler.py:1599` — start_group_sequence не сериализует сессии группы: повторный запуск (или запуск во время идущей программы) порождает два параллельных секвенсора, интерливящих ON/OFF по одним зонам
- **[high]** `irrigation_scheduler.py:2329` — Recovery-запуск гонится с boot-ремедиацией: первая восстановленная зона молча «поливается» с выключенным реле/закрытым мастер-клапаном
- **[high]** `irrigation_scheduler.py:2380` — Boot-replay персистентного program-джоба гонится с recover_missed_runs — двойной параллельный запуск программы после рестарта
- **[medium]** `irrigation_scheduler.py:2294` — recover_missed_runs повторно поливает уже завершившийся (укороченный погодой или остановленный вручную) запуск — окно строится по номинальным длительностям без проверки фактического завершения
- **[medium]** `irrigation_scheduler.py:2298` — recover_missed_runs: при перекрытии окон main и extra_times берётся max(candidates) — восстанавливается не тот запуск, уже политые зоны поливаются повторно
- **[medium]** `irrigation_scheduler.py:2391` — Гонка recover_missed_runs со стартовой зачисткой: восстановленный запуск глушится stop_on_boot_active_zones/_boot_sync, первая зона не поливается, но получает фантомный zone_run 'ok'
- **[low]** `irrigation_scheduler.py:1177` — schedule_program затирает zones.scheduled_start_time «сегодняшним» временем программы независимо от дней недели и прошедшего времени — карточка группы показывает ложную «Следующая: HH:MM»

## B. Управление зонами и состоянием

- **[high]** `services/observed_state.py:147` — StateVerifier ретраит устаревшую команду: фоновая верификация перекрывает более свежую противоположную команду и может закрыть/открыть клапан вопреки текущему состоянию
- **[high]** `services/observed_state.py:199` — Observed-state верификация — тавтология: приложение само пишет retained-значение в report-топик, который затем читает верификатор («ложное подтверждение» реле)
- **[high]** `services/zone_control.py:298` — Контракт state='fault' не реализован: fault-зона свободно стартует по расписанию, а любой stop_zone молча сбрасывает fault в 'off'
- **[high]** `services/zone_control.py:831` — stop_zone/exclusive_start_zone игнорируют результат publish_mqtt_value — при сбое доставки OFF зона помечается 'off', реле остаётся физически открытым, и все страховочные стоп-слои слепнут
- **[medium]** `services/app_init.py:137` — _boot_sync: stop_all_in_group(force=True) закрывает «крашнутые» открытые zone_runs как status='ok' ДО mop-up'а в 'aborted'
- **[medium]** `services/zone_control.py:134` — TOCTOU в _do_close мастер-клапана + неатомарный pop/insert таймеров: закрытие мастера возможно сразу после открытия при старте зоны
- **[medium]** `services/zone_control.py:298` — Гонка двойного старта: ветка 'on/starting → pass' в exclusive_start_zone всё равно создаёт второй открытый zone_run
- **[medium]** `services/zone_control.py:399` — exclusive_start_zone игнорирует результат publish_mqtt_value: при подтверждённом сбое доставки зона всё равно переводится в 'on' с audit_reason='mqtt_ack_on', API отвечает «started»
- **[medium]** `services/zone_control.py:568` — Перезапуск уже включённой зоны снимает hard-stop джобы ЧУЖИХ зон: substring-матч 'zone_hard_stop:1' совпадает с 'zone_hard_stop:10..19'
- **[medium]** `services/zone_control.py:609` — Ручной старт одной зоны отменяет на сегодня ВСЕ программы группы, чьё основное время уже прошло — вечерние extra_times-слоты молча пропадают
- **[medium]** `services/zone_control.py:793` — stop_zone не сериализован с exclusive_start_zone: проверка state по снапшоту до лока, group_lock не берётся, publish вне локов
- **[medium]** `services/zone_control.py:847` — Зона без MQTT-топика/сервера в группе с мастер-клапаном: старт открывает мастер, но stop_zone никогда не планирует его закрытие — клапан остаётся открытым
- **[low]** `services/zone_control.py:568` — Перезапуск уже включённой зоны снимает zone_hard_stop чужой зоны: подстрочное сравнение id джоба без разделителя (зона 5 матчит zone_hard_stop:51)

## C. Слой данных (SQLite)

- **[high]** `db/migrations.py:267` — Рестарт сервиса с пустой таблицей zones молча сбрасывает пароль администратора на дефолтный "1234"
- **[high]** `db/programs.py:64` — Эвристика «1-базных» дней в БД портит выбор дней недели из UI, если не выбран понедельник
- **[high]** `db/programs.py:122` — JSON-null в programs.zones/extra_times принимается без валидации и валит старт всего сервиса
- **[medium]** `db/base.py:21` — retry_on_busy — мёртвый код во всех репозиториях: внутренние except sqlite3.Error глотают BUSY до декоратора
- **[medium]** `db/migrations.py:1250` — Миграция zone_runs_backfill_source трактует локальное naive-время start_utc как UTC — на не-UTC контроллере вся история программных запусков классифицируется как 'manual'
- **[medium]** `db/programs.py:186` — duplicate_program молча сдвигает дни недели копии на день назад, если в днях оригинала нет понедельника
- **[medium]** `db/programs.py:216` — check_program_conflicts игнорирует enabled: выключенная программа навсегда блокирует создание/сохранение пересекающейся программы
- **[medium]** `db/programs.py:216` — check_program_conflicts полностью игнорирует extra_times: пересечение по дополнительным слотам не детектируется, программы реально накладываются в рантайме
- **[medium]** `db/programs.py:216` — check_program_conflicts не видит extra_times — пересечение по extra-слоту проходит валидацию, а на fire-time вся программа молча пропускается guard'ом
- **[medium]** `db/settings.py:167` — set_env_config: неатомарная запись из шести отдельных коммитов + непойманный ValueError на нечисловом server_id — 500 и полуприменённая конфигурация датчиков
- **[medium]** `db/zones.py:68` — update_zone: int()-касты в новом _build_zone_update_fields бросают непойманный ValueError/TypeError — 500 вместо контрактного None
- **[medium]** `db/zones.py:260` — update_zone: полная перезапись строки из несвежего снапшота — read-modify-write без транзакции затирает конкурентные изменения state/postpone/watering_start_time
- **[medium]** `db/zones.py:323` — update_zone_versioned: `except sqlite3.Error: pass` вокруг BEGIN IMMEDIATE глотает lock-ошибку — TOCTOU-защита молча отключается, retry_on_busy не срабатывает
- **[medium]** `db/zones.py:326` — update_zone_versioned не реализует optimistic lock: version читается в той же транзакции, WHERE version=? не может не совпасть; fallback в zones_state дожимает запись при любой ошибке
- **[medium]** `db/zones.py:414` — bulk_upsert_zones: одна строка с нечисловым/null duration откатывает ВЕСЬ импорт с 500 вместо инкремента failed
- **[low]** `db/audit.py:100` — get_audit_logs/get_logs: фильтры дат сравниваются с UTC-временем, а отображение — localtime (сдвиг на TZ-офсет)
- **[low]** `db/migrations.py:344` — _recreate_table_without_columns теряет UNIQUE/FK-констрейнты при downgrade: после rollback telegram_add_fsm_and_notif ломается upsert_bot_user (ON CONFLICT без UNIQUE)
- **[low]** `db/mqtt.py:100` — Сохранение MQTT-сервера из UI молча сбрасывает TLS-настройки (частичный PUT перезаписывает отсутствующие поля дефолтами)
- **[low]** `db/zones.py:272` — update_zone: исключение зоны из программ при переводе в группу 999 не срабатывает для строкового group_id
- **[low]** `db/zones.py:738` — finish_zone_run: чтение confirmed и UPDATE статуса не атомарны — поздний echo реле помечает реальный полив как 'failed'
- **[low]** `db/zones.py:836` — compute_next_run_for_zone: для interval-программ с interval_days>1 пишется заведомо неверный scheduled_start_time
- **[low]** `db/zones.py:867` — compute_next_run_for_zone: postpone-якорь не сдвигается на offset зоны — scheduled_start_time уводит на следующий день, хотя планировщик польёт сегодня

## D. MQTT и SSE

- **[high]** `routes/mqtt_api.py:156` — Совпадающий client_id: probe/status/scan-sse из настроек MQTT выбивают live-подписку SSE-хаба (MQTT session takeover)
- **[high]** `services/mqtt_pub.py:120` — Публикующий MQTT-клиент: on_disconnect с сигнатурой VERSION1 при CallbackAPIVersion.VERSION2 — TypeError убивает сетевой тред paho, авто-reconnect мёртв
- **[high]** `services/sse_hub.py:268` — sse_hub._on_message на каждом наблюдаемом 'on' безусловно отменяет и перевзводит автостоп на БАЗОВУЮ длительность — override-длительность затирается, retained '1' при реконнекте продлевает полив
- **[medium]** `routes/mqtt_api.py:98` — PUT /api/mqtt/servers/<id> возвращает расшифрованный сохранённый пароль брокера — маскировка VULN-004 есть только в GET, а новая keep-семантика ветки делает утечку регулярной
- **[medium]** `routes/mqtt_api.py:213` — GET /api/mqtt/<id>/status — единственный MQTT-эндпоинт без @admin_required: аноним инициирует блокирующие подключения к брокеру с сохранёнными креденшалами
- **[medium]** `services/mqtt_pub.py:50` — Правка MQTT-сервера (host/port/пароль) не инвалидирует ни кэш клиентов публикатора, ни подписки SSE-хаба — команды продолжают уходить на старый брокер до рестарта
- **[medium]** `services/mqtt_pub.py:105` — Блокирующий cl.connect() под глобальным _MQTT_CLIENTS_LOCK: мёртвый брокер задерживает OFF-команды на здоровый брокер
- **[medium]** `services/mqtt_pub.py:291` — _publish_one/_invalidate_client разрушает разделяемый MQTT-клиент, которым в этот момент публикуют другие треды — каскад ложных сбоев OFF-команд
- **[medium]** `services/sse_hub.py:151` — SSE-хаб — единственный подписчик на эхо реле — запускается только при первом открытии страницы: после рестарта сервиса без веб-клиента внешние включения реле невидимы и никогда не останавливаются
- **[medium]** `services/sse_hub.py:168` — SSE-хаб не создаёт MQTT-клиента для сервера, на котором есть только топик мастер-клапана (без зон) — master_valve_observed никогда не обновляется
- **[medium]** `services/sse_hub.py:173` — SSE-хаб игнорирует TLS-настройки MQTT-сервера — для tls_enabled=1 брокера эхо реле не приходит вообще (как и probe/status/scan)
- **[medium]** `services/sse_hub.py:206` — SSE-хаб инвертирует master_valve_observed для мастер-клапанов в режиме NO: эхо '1' всегда трактуется как 'open', игнорируя master_mode
- **[medium]** `services/sse_hub.py:229` — Контр-команда OFF при EMERGENCY_STOP/анти-рестарте — fire-and-forget QoS0 без retain, а observed_state='off' пишется независимо от успеха публикации
- **[medium]** `services/sse_hub.py:236` — Анти-рестарт-окно SSE-хаба мертво: mark_zone_stopped не вызывается ни одним стоп-путём, recently_stopped всегда False — защита от эхо-включения после ручного стопа недостижима
- **[medium]** `services/sse_hub.py:381` — keepalive=5 c у клиента SSE-хаба + блокирующие DB/publish-вызовы внутри _on_message → брокер сам выкидывает хаб по таймауту пинга
- **[medium]** `templates/mqtt.html:219` — mqtt.html: updateStatuses запускает новую вечную цепочку опроса при каждом loadServers — опрос /api/mqtt/*/status умножается после каждого сохранения сервера
- **[low]** `routes/mqtt_api.py:150` — /api/mqtt/<id>/probe: duration из тела запроса не валидируется — гигантское значение/『1e999』 навсегда блокирует HTTP-воркер, нечисловое даёт 500
- **[low]** `routes/mqtt_api.py:333` — scan-sse: через 300 с MQTT-тред тихо завершается, а SSE-генератор продолжает слать ping вечно — скан «замерзает», слот per-IP-лимита остаётся занят
- **[low]** `routes/mqtt_api.py:340` — scan-sse: тред MQTT-клиента обёрнут в `except queue.Full` — сбой connect убивает тред без сообщения, SSE вечно шлёт ping по мёртвому скану
- **[low]** `services/mqtt_pub.py:216` — _publish_with_retries: последняя републикация в backoff-цикле отправляется, но никогда не ожидается — функция возвращает False после фактически ушедшей команды и провоцирует дубль через новый клиент
- **[low]** `services/sse_hub.py:381` — reload_hub выполняет пересборку хаба (blocking connect всех клиентов под _SSE_HUB_LOCK) в треде HTTP-запроса; на время пересборки теряются эхо реле
- **[low]** `services/sse_hub.py:462` — Эвикция SSE-клиента при полной очереди теряет sentinel: вытесненный клиент навсегда остаётся «живым» зомби-соединением без обновлений
- **[low]** `templates/mqtt.html:154` — mqtt.html: создание/сохранение MQTT-сервера молча проглатывает ошибку сервера; нечисловой порт даёт непойманный TypeError → 500
- **[low]** `templates/mqtt.html:177` — mqtt.html: createServer/updateRow молча игнорируют ответ {success:false} — валидационная ошибка сервера не показывается вообще
- **[low]** `templates/mqtt.html:219` — mqtt.html: каждый вызов loadServers() порождает новую вечную цепочку updateStatuses — поллинг статусов брокеров множится с каждым сохранением сервера
- **[low]** `templates/mqtt.html:241` — mqtt.html: buildDisplayRegex ломает MQTT-wildcard '+' — сканер топиков показывает пустое дерево при фильтре с '+'

## E. Web-слой, роуты, сервисы

- **[high]** `app.py:407` — POST /api/env (запись конфигурации env-датчиков) доступен анонимно и освобождён от CSRF — межсайтовая форма стирает конфиг датчиков
- **[medium]** `routes/groups_api.py:527` — Ручное открытие мастер-клапана не отменяет взведённый таймер отложенного закрытия — клапан молча закрывается через delay после открытия
- **[medium]** `routes/zones_watering_api.py:293` — Общий rate-limit-бакет «mqtt_control» (10 req/мин на IP) объединяет старт и СТОП зоны — ручная остановка получает 429, зона продолжает поливать
- **[medium]** `services/security.py:49` — user_required отвергает роль "viewer": гостевой просмотр (/login?guest=1) полностью нерабочий — редирект на логин вместо статус-страницы
- **[medium]** `utils.py:154` — decrypt_secret: при смене/утере ключа AES-ветка бросает ValueError('MAC check failed') — рушатся get_mqtt_server, все publish-пути и список серверов в API
- **[low]** `routes/auth.py:39` — GET /login?guest=1 меняет сессию (сброс + role=viewer) без CSRF — можно принудительно понизить/разлогинить авторизованного админа
- **[low]** `routes/auth.py:55` — Все Flask-рейт-лимитеры используют request.remote_addr без ProxyFix — за nginx все клиенты схлопываются в одно ведро, блокировка логина затрагивает всех
- **[low]** `routes/programs_api.py:62` — PUT /api/programs/<id> не прогоняет _validate_program_v2_fields (в отличие от POST) и обращается к data["time"]/["zones"]/["days"] без .get — асимметричный контракт: апдейтом сохраняется программа, которую планировщик молча не планирует; частичный PUT падает 500
- **[low]** `routes/system_status_api.py:676` — /api/logs: фильтры from/to/type применяются в Python ПОСЛЕ 'LIMIT 1000 новейших' — запросы за старые периоды возвращают пусто при живых данных в БД
- **[low]** `routes/zones_history_api.py:84` — /api/zones/*/history: SQL-окно сравнивает локальные строки 'YYYY-MM-DD HH:MM:SS' с UTC-ISO границами 'YYYY-MM-DDTHH:MM:SSZ' — первый день окна выпадает целиком при TZ контроллера UTC+0 и западнее
- **[low]** `routes/zones_photo_api.py:223` — Загрузка изображений: PIL DecompressionBombError не перехватывается — 500 вместо 400 (заявленный 50 MP декомпрешн-кап обходится)
- **[low]** `services/image_pipeline.py:48` — Decompression-bomb cap (50 MP) проверяется ПОСЛЕ полного декодирования img.load() — не предотвращает выделение памяти при загрузке изображений
- **[low]** `services/next_watering.py:157` — next_watering: in-progress-окно программы строится по номинальным длительностям — при погодном коэффициенте <100% зонам показываются слоты, которых не будет
- **[low]** `services/postpone.py:34` — /api/postpone: параметр days не валидируется — ValueError/OverflowError → 500 на гость-доступном CSRF-exempt эндпоинте

## F. Frontend

- **[high]** `templates/programs.html:489` — Stored XSS: имя зоны вставляется в innerHTML без escapeHtml в мастере программ (шаг 4 «Выбор зон»)
- **[high]** `templates/programs.html:630` — Мастер программ трактует конфликтный ответ 200 {success:false} как успешное сохранение
- **[medium]** `static/js/history.js:444` — История поливов: zone_runs.start_utc/end_utc хранят ЛОКАЛЬНОЕ naive-время, а history.js трактует их как UTC-ISO — на Safari/iOS список запусков без дат и времени
- **[medium]** `static/js/status.js:60` — status.js syncServerTime парсит now_iso ('YYYY-MM-DD HH:MM:SS') через new Date() без нормализации — на Safari/iOS шапка «Время:» навсегда показывает NaN
- **[medium]** `static/js/status.js:90` — loadZonesData глушит ошибки fetch в пустой массив: при недоступном сервере страница показывает «Зоны не найдены» и прячет баннер потери связи
- **[medium]** `static/js/status.js:393` — status.js: серверные planned_end_time/watering_start_time ('YYYY-MM-DD HH:MM:SS') парсятся new Date() без нормализации — на Safari/iOS таймер бегущей зоны показывает «NaN:NaN» и NaN%
- **[medium]** `static/js/status.js:1804` — Гонка оптимистичного состояния зоны с 5-секундным поллингом: только что запущенная зона визуально откатывается в «off» и приглашает повторный старт
- **[medium]** `static/js/status.js:1807` — Оптимистичные watering_start_time/planned_end_time пишутся как UTC-naive строка — на не-UTC клиентах таймер сразу показывает 00:00 и прогресс 100%
- **[medium]** `static/js/status.js:1818` — toggleZoneRun: при success:false от сервера не вызывается hideLoading — полноэкранный оверлей блокирует UI на 15 секунд
- **[medium]** `static/js/status.js:1975` — saveZoneEdit игнорирует success:false — валидационная ошибка 400 показывается как «Зона сохранена»
- **[medium]** `static/js/zones.js:102` — Таблица зон: select MQTT-сервера без пустой опции — зона с mqtt_server_id=NULL показывается привязанной к первому серверу, и любое автосохранение молча записывает эту привязку
- **[medium]** `static/js/zones.js:1052` — Удаление зоны: ответ 204 с пустым телом трактуется api.delete как falsy — UI молча не обновляется
- **[medium]** `static/js/zones.js:1500` — zones.js: SSE zones-sse — только дельты без снапшота и без ресинка при реконнекте; пропущенные события оставляют кнопки старт/стоп в противоположном состоянии навсегда
- **[medium]** `static/js/zones.js:1862` — CSV-экспорт зон без квотирования: запятая в имени зоны ломает раскладку колонок и портит данные при реимпорте
- **[low]** `static/js/app.js:296` — app.js: Health-панель (клавиша 'h') вставляет сырые строки meta_tail (MQTT topic/payload) в innerHTML — XSS через сообщение брокера
- **[low]** `static/js/status.js:265` — env-пробник перезапускается каждым 5-секундным обновлением статуса — вечный опрос /api/env раз в секунду вместо заявленных «до 10 попыток»
- **[low]** `static/js/status.js:1000` — Регрессия ветки: удалён SSR statusData (_ssrStatus) — карточки групп пусты до ~1-2с, admin-кнопки и emergency-guard отсутствуют на первом рендере
- **[low]** `static/js/status.js:1626` — zone.icon и производный label вставляются в innerHTML без экранирования (status.js карточки зон, zones.js таблица) — stored XSS через API/CSV-импорт
- **[low]** `static/js/status.js:1975` — saveZoneEdit игнорирует success:false — при отклонённом сервером сохранении показывается «✅ Зона сохранена»
- **[low]** `static/js/status.js:2137` — initDialDrag навешивает новые document-обработчики (в т.ч. non-passive touchmove) при каждом открытии run-popup — утечка слушателей на долгоживущей странице
- **[low]** `static/js/status.js:2262` — Запуск «всех групп» показывает «▶ Все группы запущены» безусловно — отказы отдельных групп (400/аварийный режим/занятый секвенсор) проглатываются
- **[low]** `static/js/status.js:2381` — confirmRunWithDefaults / confirmRun: цепочки fetch без .catch — при сетевой ошибке или не-JSON ответе полноэкранный оверлей блокирует UI на 15с без сообщения
- **[low]** `static/js/zones.js:187` — zones.js: имя MQTT-сервера вставляется в innerHTML без escapeHtml в трёх модалках настроек группы — stored XSS
- **[low]** `static/js/zones.js:446` — zones.js настройки группы: ответы 4xx с {success:false} — истинные объекты для truthiness-проверок, UI показывает «Сохранено» при отклонённом сервером сохранении
- **[low]** `static/js/zones.js:540` — zones.js: openModalById переносит модалку группы в body, а renderGroupsGrid создаёт дубликат с теми же id — открытая модалка «замерзает»: кнопка «Закрыть» и +/- цифр работают по скрытой копии
- **[low]** `templates/programs.html:244` — programs.html: toggleEnabled/duplicateProgram/deleteProgram молча игнорируют не-OK ответы сервера — ни тоста, ни отката UI
- **[low]** `templates/settings.html:209` — Контракт минимальной длины пароля разъехался: UI обещает и валидирует «≥ 4 символа», бэкенд требует MIN_PASSWORD_LENGTH = 8
- **[low]** `templates/settings.html:421` — Сохранение координат погоды: parseFloat обрезает запятую-десятичную, а результат PUT /api/settings/location игнорируется — «Настройки погоды сохранены» при потерянных/искажённых координатах
