# Прогресс аудита и исправлений wb-irrigation

Текущий checkpoint: 2026-07-19. Ветка: `refactor/audit-2026-07`, post-review
baseline `84f0b3df9712234a504ecbaff2e10b909c6e5975`, последний интегрированный
code checkpoint `8047d662bd3e058064146cdbf670914a15deaf85`. Push не выполнялся.

## Текущее состояние после полного post-review

- Полный read-only review всего текущего проекта завершён; P0 не найдено.
- Исторический префикс `#1–#152` не переупорядочен и не переименован.
- Добавлены стабильные post-review ID `PR-001…PR-068`: `PR-001…PR-059` и
  `PR-061…PR-068` — подтверждённые новые либо переоткрытые root causes,
  `PR-060` — намеренный H2 shadow-mode, не дефект.
- Финальный реестр сейчас содержит **226 записей**: `VERIFIED_REAL` — 217,
  `VERIFIED_FALSE` — 7, `INTENTIONAL_LIMITATION` — 2, `UNVERIFIED` — 0.
- Remediation disposition: **208 fixed · 9 deferred · 0 pending ·
  9 not_applicable**. Все 67 подтверждённых `PR-*` root causes закрыты
  интегрированным кодом и regression suites; H2 (`PR-060`) остаётся намеренным
  shadow-mode и не считается дефектом.
- Девять индивидуально отложенных записей имеют проверяемую причину: исторические
  `#37`, `#56`, `#73`, `#99`, `#140`, `#141` и platform decisions `KD-001…KD-003`.
- Reopened evidence связано с историческими `#38`, `#45`, `#50`, `#53`, `#81`,
  `#89`, `#93`, `#94`, `#100`, `#127`, `#129`, `#132`, `#149` и `#152` через
  поле `reopens`; старые записи остаются историей прежнего решения, а новые PR-ID
  являются отдельными remediation roots.
- Источник истины — `findings-inventory.json`; `findings-inventory.md` полностью
  генерируется renderer-ом и показывает evidence, reproduction, owner packages,
  reopen links и disposition.

### Owner packages post-review

- `p4-auth-http`: auth/CSRF, SSE executor, correlation teardown, cookie/TZ boundary.
- `p4-topology-api`: mutation barriers зон/групп/MQTT и строгие API contracts.
- `p4-db-integrity`: identity, referential integrity, migrations, backup fallback,
  retry/index/query integrity.
- `p4-mqtt-core`: physical echo, OFF truth, master identity/interlocks, TLS.
- `p4-lifecycle`: boot/readiness/shutdown/systemd health truth.
- `p4-scheduler`: persisted jobs, safety caps/claims, program execution lifecycle.
- `p4-programs`: program validation, conflicts, transactional CRUD semantics.
- `p4-weather`: freeze/rain/cache/settings semantics; H2 не включается.
- `p4-deploy`: updater/backup/workflows/permissions/production gates.
- `p4-telegram`: loop, stop truth, subscriptions, token/polling lifecycle.
- `p4-frontend`: stale responses, duration/bulk/CSV, status panels, browser storage.
- Cross-owner пакеты `p4-monitors`, `p4-status-api`, `p4-history`,
  `p4-performance` и `p4-test-quality` также интегрированы; multi-owner root
  закрывался только последним необходимым пакетом.

### Интегрированные post-review пакеты

- Ранее интегрированы security/auth/test-quality checkpoints:
  `6905ee35554ad6216e33b3bfec4a86a4a120e6c9`,
  `98c0234471fe6ba2a8403a22c1cf2f2194f024ff`,
  `28609d2a97928b83227edc1f359426bb79d3905e`.
- Финальная серия интеграции: DB `8d3db8b`, MQTT core `420b174`, scheduler
  `b542f1f`, postpone/auth `98aa12d…75c8c44`, programs `1e2097f`, lifecycle
  `7f658ae`, monitors `1a1be9e`, weather `f05bbf4`, history `5d247b3`, Telegram
  `f5f6851`, topology `096487b`, status API `25a1b98`, frontend `c0a6517` и
  native WB deploy `96a520c`; финальные интеграционные regression fixtures и
  virtual-zone topology edge закрыты `8047d66`.
- Точечные post-integration suites прошли для каждого пакета; deploy дополнительно
  прошёл 27 тестов, `bash -n` и проверку versioned Telegram blob. Финальный общий
  serial gate записывается ниже после запуска.
- Узкие frozen exceptions использованы только для forward migrations
  (`8d3db8b`), legacy credential fixture (`cbbe1b1`), закрытия CSS response
  (`6456748`) и минимальных CI/deploy workflow contracts (`96a520c`).

### Reconciliation post-package reviewer residuals

- Финальный residual map сохранил стабильные ID; owner-branch commits были
  засчитаны только после локальной интеграции и профильных regression tests.
- Восемь независимых root causes добавлены append-only как `#217–#224`:
  - `PR-061` — monotonic и serialized postpone across HTTP/Telegram/rain;
  - `PR-062` — unbounded квадратичный `extra_times` conflict-check;
  - `PR-063` — network weather fetch под program mutation lock;
  - `PR-064` — orphan default `group_id=1` при POST/bulk;
  - `PR-065` — start-zone membership TOCTOU;
  - `PR-066` — intent-before-result throttle/MQTT debounce false success;
  - `PR-067` — photo/rotate prevalidation и atomic media mutation;
  - `PR-068` — weather factor diagnostics/source provenance.
- Остальные reviewer residuals сопоставлены существующим инвариантам без новых
  ID: `PR-002…PR-009`, `PR-011…PR-022`, `PR-024…PR-025`, `PR-029`, `PR-033`,
  `PR-035…PR-041`, `PR-043`, `PR-045`, `PR-047…PR-052`, `PR-054…PR-056` и
  `PR-059` по точным строкам final residual map. Где post-package evidence
  уточнило сам контракт, расширены notes `PR-003` и `PR-009`; historical
  `reopens` не менялись, потому что новых связей с `#1–#152` map не установил.
- Запрос активировать H2 в scheduler отклонён frozen objective: `PR-060`
  остаётся `INTENTIONAL_LIMITATION`, H2 — только shadow-mode.

### Решение оркестратора по `db/migrations.py`

Прежний blanket-freeze снят **только** для узких изменений, необходимых для
`PR-007`, `PR-008`, `PR-030`, `PR-032`, `PR-043`, `PR-057` и `PR-064`:

- запрещение повторного использования identity зон, MQTT-серверов и программ;
- transactional referential-integrity policy: zone program references очищаются
  без удаления retained `zone_runs`, MQTT hardware references дают RESTRICT/409,
  program cancellations удаляются вместе с программой;
- fail-closed named migrations и восстановление обязательных индексов;
- только forward-remediation `PR-043`: удалить известный default seed `1234`,
  сохранив без изменения все существующие password hashes и уже интегрированный
  private recovery flow; reset/rotation существующих credentials запрещены;
- только forward-remediation `PR-064`: positive group-reference guards и repair
  отсутствующей default group для existing/fresh DB до разрешения zone POST/bulk;
  общий redesign group schema не разрешён.

Каждое такое изменение обязательно получает migration regression tests на
existing DB, повторный startup/idempotency, сохранность history, отсутствие
переназначения hardware и корректный forward upgrade. Разрешение не охватывает
общий рефакторинг migrations, live downgrade `#73` или неоднозначный historical
backfill `#99`: они остаются индивидуально deferred. Любая rollback-specific
работа `c019` также явно исключена; исключения разрешают только forward path.

### Решение оркестратора по frozen `.github/**`

Freeze снят **только** для минимальных workflow-изменений `PR-024` и `PR-052`:

- deploy workflow обязан сначала stage-ить updater ровно из target revision и
  затем запускать именно этот staged artifact; текущий/старый updater на target и
  divergent inline deploy path использовать нельзя;
- CI обязан запускать default pytest suite без ручного урезания collection и
  считать `PytestUnraisableExceptionWarning` и
  `PytestUnhandledThreadExceptionWarning` fatal failures.

Другие workflow refactors, action/version upgrades, matrix/caching/trigger/
permissions changes этим решением не разрешены. Узкий контракт `PR-024`/`PR-052`
интегрирован коммитом `96a520c` и закреплён deploy contract tests.

### Intentional и rejected решения post-review

- `PR-060`: H2 water-balance остаётся намеренным shadow-mode. Его нельзя
  подключать к physical scheduler без отдельного product decision и доказанного
  сохранения freeze/rain/wind hard-safety gates.
- `KD-001`: запуск native WB unit от root подтверждён и индивидуально deferred;
  сначала нужна privilege/device/path/capability model и migration plan.
- `KD-002`: повторный bootstrap не имеет полного rollback code/venv/unit;
  существующие установки обновляются transactional `update_server.sh`.
- `KD-003`: install/update пока не имеют общего межпроцессного lock; до
  WB-specific contention regression запуски сериализуются оператором/CI.
- `IL-001`: Docker остаётся только local-development target; production parity
  с native WB systemd не требуется.
- `RD-001`: гипотеза о false-positive total-duration conflict отклонена — runtime
  последовательно выполняет все зоны и удерживает group claims на весь invocation.
- `RD-002`: архивные legacy/Docker runbooks не являются автоматическим production
  path и не создают отдельный runtime defect.

## Исторический checkpoint до post-review

Дата исходного checkpoint: 2026-07-18.

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

## Фаза 2 — локальная интеграция

Начало: 2026-07-19. Интеграцию выполняет только главный оркестратор; push запрещён,
пока origin публичный. Каждый fix-пакет проходит полный серийный pytest и полный
`uvx ruff check .` перед переходом к следующему merge.

Учёт disposition теперь детерминирован:

- `findings-inventory.json` остаётся источником истины и хранит для каждой записи
  `implementation` с disposition, причиной, пакетом, fix commit и regression tests;
- `PHASE2-PACKAGES.json` хранит интеграционный ledger пакетов и полных проверок;
- `render_findings_inventory.py` применяет ledger, наследует disposition точным
  дублям, защищает исторический префикс `#1–#152`, валидирует append-only PR/decision
  ID и целиком регенерирует Markdown.

### Интегрированные пакеты

1. `phase2-baseline-history`
   - fix `458dab2afb138870a01614223ee9d9a5ad228b78`;
   - merge `65493eaed00b036ffd7d6aad0580a3a8a6dd9c33`;
   - production-код истории корректно относит запуск к локальной дате старта;
     baseline-тест больше не предполагает, что «45 минут назад» всегда сегодня;
   - targeted: 14 passed; full gate: 1872 passed, 2 skipped, 17 deselected;
     ruff и diff check чисты.

2. `phase2-migration-password` — finding #98 (`fixed`)
   - fix `29708009a2a14ea59d82160b1aa69bddbe05e389`;
   - merge `2b8981209d68bf8d4b3e811c3872fbc09e7a3427`;
   - это узкое явно разрешённое исключение для замороженного `db/migrations.py`:
     только bootstrap-вставка `password_hash` изменена с `INSERT OR REPLACE` на
     `INSERT OR IGNORE`; свежая БД всё ещё получает начальный пароль, а существующий
     hash больше не сбрасывается при рестарте с пустой таблицей `zones`;
   - regression:
     `tests/unit/test_phase2_migration_password.py::test_restart_with_empty_zones_preserves_existing_admin_password`;
   - targeted: 12 passed; full gate: 1873 passed, 2 skipped, 17 deselected;
     ruff и diff check чисты.

3. `phase2-db-programs` — findings #10, #41, #70, #71 (`fixed`), #59 наследует #10
   - fix `ee453c30039997f7bf5ff74b671a96eea030b100`;
   - merge `702b1ee02a09d38429840c0373a0b9a39e6ef366`;
   - удалена legacy-эвристика сдвига weekday; существующие строки не мигрировались,
     потому что корректные и ранее повреждённые значения неразличимы;
   - `zones`/`extra_times` JSON null отклоняются атомарно, disabled-программы не
     участвуют в конфликте, duplicate сохраняет zero-based days;
   - repository умеет сравнивать stored/candidate extra slots; полное закрытие
     #100/#129 ожидает интеграции route wiring из следующего P02-пакета;
   - targeted: 79 passed; full gate: 1883 passed, 2 skipped, 17 deselected;
     ruff и diff check чисты.

4. `phase2-programs-web` — #19, #58, #74, #79, #100, #125, #129, #143
   `fixed`; #140 `deferred`
   - fixes `d0a30b346c1f520b9368ca39d8b6f97b0b3c0e1c` и
     `684d0cd4d1ea8fb445c9035dabedadc2a2c70b44`;
   - merge `9d1879aa53133653b16d1f50d47a6968eab4179e`;
   - PUT использует тот же v2 validation contract, что POST, partial update не
     даёт KeyError, interval_days ограничен безопасными integer 1..30;
   - primary и extra candidate slots проходят conflict check против всех stored
     slots, включая validation для группы 999 без изменения её runtime-семантики;
   - wizard обрабатывает non-OK/success:false и экранирует имя зоны;
   - #140 отложен индивидуально: без program/run identity и определённых метрик
     честно реализовать program log/stats невозможно; требуется новая storage и
     scheduler-writer модель, а не ложные shape-only значения;
   - targeted: 130 passed; full gate: 1903 passed, 2 skipped, 17 deselected;
     ruff и diff check чисты.

5. `phase2-mqtt-safety` — #17, #39, #49, #53, #55, #62, #84, #85, #86,
   #114, #116 `fixed`; #69 наследует #17
   - fix `7d750b49fe5612f52d6121bdb4c923f5e42df3ad`;
   - merge `ec38e36f74863e0cb547267d74b85fe48f1c25b6`;
   - secrets не возвращаются в POST/PUT, partial MQTT update сохраняет TLS;
   - per-server creation lock исключает глобальную блокировку connect, callback
     соответствует Paho VERSION2, client invalidation generation/lease-safe;
   - QoS retry ожидает последнюю отправку, probe/scan validation bounded, scan
     timeout/connect error завершают SSE и освобождают слот, status admin-only;
   - #52 остаётся pending до обработки typed SecretDecryptionError владельцем
     `sse_hub`; #80 остаётся pending до замены последнего temporary client_id в
     `routes/system_status_api.py`;
   - targeted: 173 passed; full gate: 1923 passed, 2 skipped, 17 deselected;
     ruff и diff check чисты.

6. `phase2-zone-state-safety` — #3, #18, #34, #35, #36, #64, #89, #111,
   #112, #132 `fixed`; #115/#136 наследуют #3
   - fix `42142ddea539625fe948bdaeb358b338aff58cef`;
   - merge `622c0d5`;
   - failed ON/OFF и peer OFF больше не маскируются успешным DB-state; fault
     сохраняет физическую неопределённость и позволяет повторный safety-stop;
   - start/stop и master close/open сериализованы, duplicate runs и чужое снятие
     hard-stop исключены, stale verifier generation не републикует старую команду;
   - day-wide cancellation заменена отменой только активной runtime session;
   - #50 остаётся pending до удаления hub-side ложного confirmation в P06;
     #110 остаётся pending до устранения scheduler bypass в P03;
   - targeted: 98 passed; full gate: 1940 passed, 2 skipped, 17 deselected;
     ruff и diff check чисты.

7. `phase2-sse-safety` — #1, #2, #4, #40, #50, #52, #54, #67, #82, #83,
   #113, #117, #134 `fixed`; #33/#51 наследуют #4
   - fix `b4797a5cc8f4ff2f9738274dfa617a786d2a1527`;
   - merge `fb9a3efa1e85c1d1dbdd20be5064e1a109014783`;
   - generation-based rebuild атомарно меняет clients/topic maps, не держит hub
     lock или HTTP request на connect/loop_stop и сохраняет живые subscriptions;
   - MQTT callback перенесён на ordered worker, keepalive увеличен, hub стартует
     при init даже без браузера, master-only/TLS/NO paths получают корректные echo;
   - emergency/anti-bounce OFF теперь QoS 2 retained и не маскирует физический ON,
     retained/self-publish не подтверждает command и не сбрасывает override/hard-stop;
   - P05+P06 полностью закрыли #50, P07+P06 — #52; #81 остаётся pending,
     поскольку TLS ещё требуется diagnostic clients в `routes/mqtt_api.py`;
   - targeted: 19 passed; full gate: 1959 passed, 2 skipped, 17 deselected;
     ruff и diff check чисты.

8. `phase2-mqtt-diagnostic-tls` — #81 `fixed`
   - fix `e71db24703adb6bf70d382bb36cd009f3b978684`;
   - merge `aadac5e11983f46ef980063676df0b26942d9b46`;
   - P06 уже применял TLS к permanent hub; follow-up P07 добавил единый TLS setup
     для probe/status/scan с уникальными ephemeral client IDs;
   - CA/client cert/key, version и insecure mode применяются до connect, а любая
     setup-ошибка завершает diagnostic path без попытки plaintext fallback;
   - targeted: 80 passed; full gate: 1963 passed, 2 skipped, 17 deselected;
     ruff и diff check чисты.

9. `phase2-test-quality` — #6, #7, #8, #9, #142 `fixed`
   - fix `9d65cb6f41894ca16712c86e4d54c2a197be8472`;
   - merge `f987a517399fbe4ad5c5c064bf297f25b4cde6d1`;
   - recovery/next-watering tests больше не зависят от текущей даты или полуночи;
   - NULL/matching/wrong even-odd parity проверяются на обеих датах, interval
     требует точный interval/start_date, CronTrigger — точные fields всех slots;
   - targeted: 92 passed; full gate: 1968 passed, 2 skipped, 17 deselected;
     ruff и diff check чисты.

10. `phase2-scheduler-safety` — #30, #63, #65, #66, #87, #88, #90, #110,
    #127, #128, #133, #145, #146 `fixed`; #141 `deferred`
    - fixes `1aa4406c3088cace9abfe084fda974e0d44bd9de` и
      `a91116a14fea7e6df07bb2869b0ae726fe562edd`;
    - merge `81a3676b39ca60015cc81faa394bd13f0c7e7bbd`;
    - persistent program jobs сериализованы/ревалидируются, invalid update и
      concurrent DELETE не оставляют orphan jobs;
    - boot scheduler paused до reconciliation; recovery требует interrupted
      evidence, учитывает полночь и earliest overlapping slot;
    - group session захватывается до initialization, физический OFF проверяется
      до нового sequence, emergency/quiesce fences закрывают поздние ON;
    - scheduler больше не prewrite'ит `on`: P05 central start решает fault/MQTT/MV
      и failed result не получает hard-stop, active-state либо success log;
    - #141 отложен индивидуально: smart/time-based semantics не определена, а
      включать H2 либо выдумывать decision policy запрещено;
    - #149 остаётся pending до P15 shutdown call-site, хотя quiesce primitive и
      его strict-fence regression уже готовы;
    - targeted: 164 passed; full gate: 1991 passed, 2 skipped, 17 deselected;
      ruff и diff check чисты.

11. `phase2-app-lifecycle` — #38, #149 `fixed`
    - fixes `dcd0e30ecea830f3e09968c40e50ca79da9c0875` и
      `3e6b14e67520371f78806722539260984b9ce9f4`;
    - merge `9f8b2a2e8a5899f4ee22eea9cb43380fd9add505`;
    - crash-open run получает `end_utc`/`aborted` до обычного force-stop и больше
      не может быть финализирован как успешный;
    - после `_boot_sync` вызывается `complete_boot_recovery`, затем стартуют
      monitors; persistent jobs не выходят из paused раньше physical OFF;
    - shutdown сначала ставит P03 quiesce fence и ждёт admitted runners, после
      чего публикует финальные QoS2 retained OFF для зон/мастеров;
    - targeted: 17 passed; full gate: 1994 passed, 2 skipped, 17 deselected;
      ruff и diff check чисты.

12. `phase2-master-route` — #135 `fixed`
    - fix `b519a57e25a92873305d66f26cd09382a8880bc3`;
    - merge `d227b5552cb214b2852b306c9c3fd872ab41215e`;
    - manual OPEN под P05 topic lock атомарно отменяет pending delayed close до
      публикации, поэтому старый timer больше не закрывает свежую команду;
    - targeted: 14 passed; full gate: 1995 passed, 2 skipped, 17 deselected;
      ruff и diff check чисты.

13. `phase2-telegram-stop` — #147 `fixed`
    - fix `40b961f7d3761c8752c25886570b6568c8ec8531`;
    - merge `5faec6e9e33da3d7c4a4c3e76021e6ab3b45bfb7`;
    - Telegram Stop делегирует P03 `cancel_group_jobs`, поэтому отменяет Event
      runner и jobs вместе с физической остановкой; fallback сохранён без scheduler;
    - targeted: 11 passed; full gate: 1997 passed, 2 skipped, 17 deselected;
      ruff и diff check чисты.

14. `phase2-watering-rate-limit` — #118 `fixed`
    - fix `e32125c1c42242858dd4f2f3826eba88212c0462`;
    - merge `b7827513d721fbab0ded9fd3f54ed7fc1d04f981`;
    - start limiter 10/min сохранён, explicit MQTT OFF не использует его bucket;
      central auth/guest policy и direct-WB network model не менялись;
    - targeted: 90 passed; full gate: 1998 passed, 2 skipped, 17 deselected;
      ruff и diff check чисты.

15. `phase2-zone-db` — #14, #42, #43, #44, #45, #46, #47, #68, #72,
    #130 `fixed`; #37 `deferred`
    - fixes `b803fbf7dc29ec7a4e4edd26f811bc93bc5b319c` и
      `e1878ce81f4cc3ea8adeaf51eb926b6db2f00df5`;
    - merge `2b91ae47de5b4518c73257ead1d246a5d33cdf69`;
    - partial zone UPDATE пишет только payload и не переигрывает stale state/timing;
      string group 999 исключает зону из программ, invalid integer и import row
      сохраняют repository/bulk contracts;
    - BUSY/LOCKED проходит мимо внутренних sqlite catches только внутри
      `retry_on_busy`, BEGIN IMMEDIATE действительно ретраится, а недекорированные
      методы по-прежнему получают обычный `sqlite3.OperationalError`;
    - run confirmation закрывается одним atomic UPDATE; multi-day interval не
      публикует выдуманный daily anchor, postpone учитывает offset зоны;
    - #37 отложен индивидуально: честный caller-owned optimistic CAS требует
      expected_version через facade/services/scheduler и запрета raw fallback;
      внутри одного `db/zones.py` такого version contract нет;
    - targeted: 363 passed; full gate: 2011 passed, 2 skipped, 17 deselected;
      ruff и diff check чисты.

16. `phase2-next-watering` — #5, #12, #13, #29, #32, #131, #150, #151,
    #152 `fixed`; #11/#15 наследуют #5, #28 наследует #12
    - fix `8c51a0f10e6b2b4166c1bac632ad43eb980d34c6`;
    - merge `d256de2b7cc355dc01d2e571bfaf07db205de0c5`;
    - disabled/malformed programs и slots не создают phantom runs, extra_times
      участвуют, interval anchor читается из live APScheduler job metadata;
    - active cross-midnight run требует scheduler-owned state и использует текущий
      H1 coefficient для оставшихся offsets; будущая погода остаётся nominal,
      H2 не включался;
    - postpone сохраняет per-date cancellation, weather skip не теряет tomorrow
      midnight, cache-only weather ограничен обычным TTL без сетевого запроса;
    - cancellation memoized до одного lookup на program/date/group; полное
      закрытие публичного amplification #139 ожидает single-precompute call-site P09;
    - targeted: 285 passed, 1 skipped; full gate: 2023 passed, 2 skipped,
      17 deselected; ruff и diff check чисты. Единичный APScheduler teardown
      warning не воспроизвёлся в отдельном повторе owning test (1 passed).

17. `phase2-system-status` — #80, #102, #139, #148 `fixed`
    - fixes `4409dea6e78cdb010a5ef695984330fe619a3537` и
      `8e18b552430e58f4ea9af437120f9cb084c12c7e`;
    - merge `7c4bea54e2975eb0a7b926a9d019bf6b5943c05f`;
    - `/api/status` делает один all-zone P08 snapshot; cancellation memo даёт
      максимум 16 program/date/group lookup для regression 32 zones × 16 programs;
    - status health probe использует уникальный ephemeral MQTT client_id и не
      вытесняет permanent hub session;
    - logs type/date filters выполняются до LIMIT 1000 и используют
      `date(timestamp,'localtime')`, поэтому UTC storage и controller-local UI
      совпадают на границе суток;
    - idle health cancel не создаёт ownerless Event и делегирует P03 scheduler;
    - targeted combined P08+P09: 274 passed; full gate: 2028 passed, 2 skipped,
      17 deselected; ruff и diff check чисты.

18. `phase2-env-security` — #91, #101, #122, #123 `fixed`
    - fix `c030921f2f1b5537c70e71100463455dc358c6b2`;
    - merge `24ecc7e83ee88edbcd420530cac7b811235bffba`;
    - GET `/api/env` остаётся публичным, POST теперь admin-only и не exempt от CSRF;
    - temp/hum objects и server IDs валидируются до единой six-key транзакции,
      ошибка одной записи откатывает весь env config;
    - password UI соответствует backend minimum 8; locale comma coordinates
      проходят finite/range validation, success требует удачи location и weather;
    - targeted: 174 passed; full gate: 2034 passed, 2 skipped, 17 deselected;
      ruff и diff check чисты.

19. `phase2-auth-roles` — #57, #96 `fixed`; #56 `deferred`
    - fix `dce0f8d9c1ad824f3f214a27955c98bf76c70197`;
    - merge `1cdd724445155bf956c0faafda01ed5aafb012b6`;
    - guest link сохраняет существующую viewer/user/admin session и не может
      downgrade администратора; viewer допущен к заявленному read-only status;
    - #56 отложен индивидуально: supported WB target работает direct Hypercorn,
      а proxy-aware IP требует explicit trusted-proxy allowlist; слепое доверие
      forwarding headers создало бы spoofing уязвимость;
    - targeted: 50 passed; full gate: 2036 passed, 2 skipped, 17 deselected;
      ruff и diff check чисты.

20. `phase2-image-safety` — #97, #137 `fixed`
    - fix `66a6c0a2a8c828630b89eb72ebee3ae1a72641de`;
    - merge `964f9a26e8d5bddaf883b9c9e17cfae97459d364`;
    - 50 MP guard срабатывает по headers до `img.load()` и не декодирует
      заведомо oversized bitmap;
    - Pillow bomb error нормализован в `ImageTooLargeError`; map и zone-photo
      возвращают `400 / IMAGE_TOO_LARGE` без persistence side effects;
    - targeted: 30 passed; full gate: 2038 passed, 2 skipped, 17 deselected;
      ruff и diff check чисты.

21. `phase2-postpone-validation` — #138 `fixed`
    - fix `640e88264560f8ccf1b34114bdbe30f961634e11`;
    - merge `314f965baa7b8537b18e66b21f3a818847a668bd`;
    - malformed и overflowing days получают 400 до первого DB/scheduler write;
      обычный postpone contract сохранён;
    - targeted: 19 passed; full gate: 2040 passed, 2 skipped, 17 deselected;
      ruff и diff check чисты.

22. `phase2-history-time-contract` — #48, #93, #94 `fixed`
    - fix `d1759a7a4a8b374076a6c82fa2fd74dddee7248c`;
    - merge `ff5df9c7520184075e50091ce07eefc2810ea531`;
    - audit local calendar filters переводятся в UTC storage bounds с exclusive
      next-local-midnight; get/count идентичны, P09 legacy logs совместимы;
    - zone_runs production storage признан controller-local naive: SQL bounds
      остаются в том же domain, API добавляет RFC 3339 controller offset;
    - remote browser получает однозначный instant, первая локальная дата не
      выпадает из history window;
    - targeted: 58 passed; full gate: 2043 passed, 2 skipped, 17 deselected;
      ruff и diff check чисты.

23. `phase2-weather-compatibility` — #144 `fixed`
    - fixes `a17a81d66415404ec63f73159ce841e79fa79fb2` и
      `af7f2ad73427514b07dd7125ca03c6d2b3a36099`;
    - merge `97fe06cb807cac0d1556a23b2403c02225444d0c`;
    - documented package и legacy imports экспортируют один callable, который
      делегирует canonical weather service/sensor merge без второй реализации;
    - cold legacy-first и package-first imports проверяются отдельными Python
      процессами; найденный review import-cycle устранён lazy resolution;
    - targeted: 123 passed, 1 skipped; full gate: 2049 passed, 2 skipped,
      17 deselected; ruff и diff check чисты.

24. `phase2-status-frontend` — #20, #21, #22, #23, #26, #27, #76, #78,
    #92, #124 `fixed`; #61 наследует #23; #25 status-side partial
    - fix `3a90ff10c943d046499e5b3422984f4a65aec978`;
    - merge `468339e9ba5e0a1e65ad8900cfb6bcfd096e877d`;
    - transport errors сохраняют last-known zones; loading overlays, soft errors
      и network/non-JSON failures получают честные recovery/toast paths;
    - optimistic timestamps сохраняют Z, pending mutations versioned против
      overlapping polls, server time нормализуется для WebKit;
    - dial handlers ставятся один раз, env probe bounded, all-groups responses
      агрегируются как full/partial/total failure;
    - status.js sinks #25 escaped, но root остаётся pending до P11 `zones.js`;
    - targeted: 125 passed; full gate: 2060 passed, 2 skipped, 17 deselected;
      node syntax, ruff и diff check чисты.

25. `phase2-zones-frontend` — #24, #25, #60, #75, #95, #106, #107,
    #119 `fixed`
    - fix `27cd91e0b41c61939509b4508367d789ccff0d22`;
    - merge `99a6cc25b922bce1bd9058e892a1314dac4811df`;
    - zone icons и MQTT labels escaped; вместе с P10 полностью закрыт #25;
    - explicit empty MQTT option, raw 204 DELETE и status-aware group writes
      устраняют ложные привязки/успехи;
    - SSE onopen resync versioned против concurrent events, modal node identity
      сохраняется между rerenders;
    - CSV encoder/parser round-trip'ит commas, quotes и embedded newlines;
    - targeted: 178 passed; full gate: 2067 passed, 2 skipped, 17 deselected;
      node syntax, ruff и diff check чисты.

26. `phase2-mqtt-frontend` — #103, #104, #109, #121 `fixed`; #120 наследует #103
    - fix `1ea701ca4305e46fd0ccfc8671a5683acc5ea771`;
    - merge `88d9162784494a4ca23866c336ebe024a3e1aa3f`;
    - MQTT status использует одну replaceable polling chain; `+/#/*` display
      wildcards компилируются в правильном порядке;
    - create/update ports валидируются как integer 1..65535 до API, а
      `{success:false}`/structured server errors показываются пользователю;
    - #121 закрыт для верифицированного UI path; crafted direct API payload не
      выдаётся за часть этой frontend-находки;
    - targeted/runtime probes прошли; full gate: 2071 passed, 2 skipped,
      17 deselected; ruff и diff check чисты.

27. `phase2-health-panel-xss` — #108 `fixed`
    - fix `49f0670784c3923b4f9cfe2d855ac7aa3dbb50b3`;
    - merge `bc9c42a14194331d03d34bbfffd03b361e0a63e1`;
    - health metadata остаётся текстом при создании cancel actions через DOM;
      broker-controlled MQTT topic/payload больше не декодируются в `innerHTML`;
    - targeted: 36 passed; `node --check` чист; full gate: 2072 passed,
      2 skipped, 17 deselected; ruff и diff check чисты.

28. `phase2-frozen-migration-decisions` — #73, #99 `deferred`, decision-only
    - `db/migrations.py` не изменялся; фиктивных fix/merge commit нет;
    - #73: production caller у `rollback_migration` отсутствует, WB update
      создаёт DB backup; constraint-preserving live downgrade требует отдельного
      решения о поддерживаемом контракте и разморозки migrations;
    - #99: one-time backfill уже применён, а исторические записи не хранят
      program identity; точная повторная классификация после edits/deletes
      невозможна без ambiguity policy и previewable forward repair;
    - выполнен read-only caller/migration/deployment evidence review; последний
      code gate остаётся 2072 passed, 2 skipped, 17 deselected.

Этот исторический checkpoint был superseded финальной post-review интеграцией.

## Финальная локальная точка Фаз 2–3

- Inventory: 208 `fixed`, 9 индивидуально `deferred`, 0 `pending`,
  9 `not_applicable`; blanket-defer не использовался.
- Deferred: #37 (cross-layer CAS), #56 (trusted proxy), #73 (live downgrade),
  #99 (неразличимая history provenance), #140 (program-run identity), #141
  (неопределённая smart semantics), KD-001 (service privileges), KD-002
  (bootstrap rollback) и KD-003 (deploy serialization).
- Запрещённые расширения scope не выполнялись: scheduler/status.js не
  распиливались, semantics группы 999 не менялась, H2 не включался, Docker не
  объявлялся production target, hardware E2E и `mqtt_real` не запускались.
- Финальный serial pytest с fatal `PytestUnraisableExceptionWarning` и
  `PytestUnhandledThreadExceptionWarning`: **3305 passed, 3 skipped,
  17 deselected** за 5:29; Ruff, renderer и diff-check подтверждаются финальным
  статическим gate этого checkpoint.
- Пользовательский untracked `AGENTS.md` сохранён и не индексировался.
- `git push` не выполнялся и запрещён без отдельного подтверждения: inventory и
  raw reports содержат подробные данные внутреннего аудита.
