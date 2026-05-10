# Issue #15 — «Запустить выбранные»: ad-hoc multi-zone run via existing queue

GitHub: https://github.com/Raul-1996/irrigation/issues/15
Branch: `feat/15-run-selected-zones` (off `main`)

## TL;DR

Add a manual "select N zones in the current group → run them sequentially" path
that goes through the **existing** `start_group_sequence` →
`_run_group_sequence` machinery. No new queue, no new sequencer, no new
sentinel for ad-hoc programs. The only "ad-hoc"-specific code is:

- one new endpoint `POST /api/groups/<gid>/run-selected` (lives in
  `routes/groups_api.py` next to `start-from-first`),
- a tiny extension to `start_group_sequence` to accept an explicit
  `zone_ids` subset (today it always takes ALL zones of the group),
- frontend mode-toggle on the existing "▶ Запустить" button (split into
  two halves) plus reuse of the issue #12 duration popup.

Manual-vs-scheduled "manual wins" policy: **delegate to the misfire
mechanism that already exists in APScheduler**. We do NOT build a new
arbiter. Specifically: at the start of `_run_program_threaded` (the entry
point for scheduled programs), check `is_group_session_active(gid)` for
every group the program touches. If active → emit
`prog_skipped_manual_running` audit and `return`. APScheduler's
`misfire_grace_time=3600` already merges later-fired-than-planned runs into
one (`coalesce=False` per program — see §4 caveat). The "only the last
missed scheduled" policy maps cleanly onto APScheduler's recovery: missed
fires that fall outside `misfire_grace_time` are dropped; the next scheduled
fire (that lands AFTER manual ends) is the one that runs. We log the
dropped ones from the misfire-detection path.

`program_id` for ad-hoc: **negative sentinel** — `-int(time.time())`
(unique-per-call). `QueueEntry` and the `program_id`-as-int contract stay
intact across `is_program_run_cancelled_for_group`, audit payloads,
DB logs. No `Optional[int]` plumbing through the codebase. `is_ad_hoc`
flag is **not added** — `program_id < 0` is the test where it matters
(audit/history). See §1.4.

Override durations: piggyback on the **already-extended** `override_duration` /
`override_percent` kwargs of `start_group_sequence` (PR #21 / issue #12).
We do NOT add fields to `QueueEntry`. Reasoning in §1.5.

---

## 0. Audit — what already exists

| Concern | Where | Status for #15 |
|---|---|---|
| Existing per-group queue & worker | `services/program_queue.py::ProgramQueueManager` | **Not used by live runs.** It exists, has tests, but `_run_entry` is a stub (`# TODO: actual zone control integration`, line 512). The actual live path is `IrrigationScheduler._run_program_threaded` / `_run_group_sequence`. The issue text says "use the existing queue" — practically that means "go through `start_group_sequence` / `_run_group_sequence`", which IS the live FIFO-per-group path. Don't enqueue into `ProgramQueueManager` — its zone control is a stub. |
| Manual all-zones-in-group | `IrrigationScheduler.start_group_sequence(gid, override_duration, override_percent)` | Reuse. Today it always takes ALL `db.get_zones()` filtered by `group_id`. Extend to accept a `zone_ids` subset (default = all, unchanged behaviour). |
| Manual program run | `routes/programs_api.py::api_run_program` (PR #19, NOT merged) | Pattern reference for our endpoint. Spawns `threading.Thread(target=job_run_program, ...)` — bypasses the APScheduler thread pool. We follow the same pattern. |
| Skip Current Zone | `irrigation_scheduler.py::request_skip_current_zone` (PR #22, NOT merged) | Works on `group_skip_current_events[gid]`. Since ad-hoc goes through `_run_group_sequence`, skip works without ad-hoc-specific branches. |
| Stop | `cancel_group_jobs(gid)` + per-zone stop | Same — works through the same `group_cancel_events[gid]` Event. |
| Issue #16 race-free cancel | `start_group_sequence` uses `setdefault` to merge events with concurrent program runners (PR #17) | Same path; no new code needed. |
| Duration / % selector | `templates/status.html` `#runPopup` + `static/js/status.js` `confirmRun()` (PR #21) | Reuse. We open the SAME popup with a pre-built `zone_ids` payload. |

**Bottom line:** the only thing that's "ad-hoc specific" is

1. a UI mode that lets the user select a subset,
2. an endpoint that takes that subset and calls `start_group_sequence`,
3. a one-liner in `start_group_sequence` to honour the subset,
4. a manual-wins guard at the top of `_run_program_threaded`.

Everything else — queue ordering, audit, master valve, cancel, skip,
weather, MQTT publish — is already there.

---

## 1. Backend

### 1.1 New endpoint

**Route:** `POST /api/groups/<int:gid>/run-selected`
**File:** `routes/groups_api.py` — adjacent to `api_start_group_from_first`
(decision below).

**Why `groups_api.py`, not `zones_watering_api.py`:**
- The semantic unit of work is "run zones in this group sequentially" —
  same as `start-from-first`. They share validation, scheduler call,
  warnings preflight.
- `zones_watering_api.py` is per-zone (`api_zone_mqtt_start(zone_id)`).
  Our endpoint is per-group with a zone subset.
- The 80% diff between the new endpoint and `api_start_group_from_first`
  is the validation block. Lifting that into a shared helper
  (`_validate_run_overrides(body)`) deduplicates and is surgical.

**Request body:**
```json
{
  "zones": [int, int, ...],
  "duration": 1..120,            // optional, minutes mode
  "duration_percent": 50|75|100|125|150|200  // optional, percent mode
}
```

Contract: same as PR #21 — minutes wins if **both** sent. Use the existing
`per_zone_dur` helper (PR #21, `services/zone_control.py`). Do **not**
re-implement validation; copy the parsing from `api_start_group_from_first`
or extract a shared `_parse_overrides(body) -> (override_dur, override_pct)`
helper (recommended, ~15 lines lifted out of `groups_api.py`).

**Validation order:**

1. Group exists (404 if not).
2. `zones` is a non-empty list of ints (400 with `message: "zones обязательны"`).
3. Each zone in `zones`:
   - exists (`db.get_zone(z_id)`),
   - `int(zone['group_id']) == gid`,
   - `zone.get('enabled', 1) != 0` (NB: confirm the column name — see §6
     Open questions).
   400 with explicit `message` listing the offending zone_id on first failure.
4. Override parsing: same as PR #21's
   `api_start_group_from_first` — minutes mode (1..120) or percent mode
   (whitelist {50,75,100,125,150,200}). Minutes wins if both sent.

**Successful response:**
```json
{
  "success": true,
  "message": "Группа <name>: запущены выбранные зоны (N)",
  "warnings": [...],   // same warnings as start-from-first (norm_not_set, clipped_*, etc.)
  "ad_hoc_program_id": -1715000000  // see §1.4
}
```

**Pseudocode:**
```python
@groups_api_bp.route('/api/groups/<int:gid>/run-selected', methods=['POST'])
@rate_limit('groups', max_requests=10, window_sec=60)
@audit_log('prog_manual_run_selected',
           target_extractor=lambda *a, **kw: f"group:{kw.get('gid', a[0] if a else '?')}")
def api_run_selected(gid):
    group = next((g for g in (db.get_groups() or []) if int(g['id']) == int(gid)), None)
    if not group:
        return jsonify({'success': False, 'message': 'Группа не найдена'}), 404

    body = request.get_json(silent=True) or {}
    zone_ids = body.get('zones')
    if not isinstance(zone_ids, list) or not zone_ids:
        return jsonify({'success': False, 'message': 'zones обязательны'}), 400
    try:
        zone_ids = [int(z) for z in zone_ids]
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': 'zones должны быть int[]'}), 400

    # Per-zone validation (group membership + enabled).
    all_zones = {int(z['id']): z for z in (db.get_zones() or [])}
    for zid in zone_ids:
        z = all_zones.get(zid)
        if z is None:
            return jsonify({'success': False, 'message': f'Зона {zid} не найдена'}), 400
        if int(z.get('group_id') or 0) != gid:
            return jsonify({'success': False,
                            'message': f'Зона {zid} не принадлежит группе {gid}'}), 400
        if int(z.get('enabled') or 0) == 0:  # see §6 — confirm column
            return jsonify({'success': False, 'message': f'Зона {zid} отключена'}), 400

    # Reuse PR #21 override parsing — see §6 merge note.
    override_dur, override_pct, parse_err = _parse_overrides(body)
    if parse_err is not None:
        return jsonify({'success': False, 'message': parse_err}), 400

    scheduler = get_scheduler()
    if not scheduler:
        return jsonify({'success': False, 'message': 'Планировщик недоступен'}), 500

    # Ad-hoc program_id: negative sentinel, unique per call.
    ad_hoc_id = -int(datetime.now().timestamp())
    name = _build_ad_hoc_name(zone_ids, override_dur, override_pct)
    # e.g. "Ad-hoc: Z1, Z2, Z3 (15 мин)" or "Ad-hoc: Z1, Z2 (150% от нормы)"

    # Warnings preflight (mirrors start-from-first PR #21 logic, but only
    # over the SELECTED zones, not the whole group).
    warnings = _preflight_warnings(zone_ids, all_zones, override_dur, override_pct)

    ok = scheduler.start_group_sequence(
        gid,
        override_duration=override_dur,
        override_percent=override_pct,
        zone_ids=zone_ids,           # NEW kwarg, see §1.2
        ad_hoc_program_id=ad_hoc_id, # NEW kwarg, see §1.4
        ad_hoc_program_name=name,
    )
    if not ok:
        return jsonify({'success': False, 'message': 'Не удалось запустить'}), 400

    db.add_log('prog_manual_run_selected', json.dumps({
        'group_id': gid, 'zones': zone_ids, 'ad_hoc_id': ad_hoc_id,
        'override_duration': override_dur, 'override_percent': override_pct,
    }))
    return jsonify({
        'success': True,
        'message': f"Группа {group.get('name')}: запущены {len(zone_ids)} зон(ы)",
        'warnings': warnings,
        'ad_hoc_program_id': ad_hoc_id,
    })
```

### 1.2 `start_group_sequence` — extend signature

Today (post PR #21):
```python
def start_group_sequence(self, group_id: int,
                         override_duration: int = None,
                         override_percent: int = None) -> bool:
    zones = self.db.get_zones()
    group_zones = sorted([z for z in zones if z['group_id'] == group_id], key=lambda x: x['id'])
    ...
```

Change:
```python
def start_group_sequence(self, group_id: int,
                         override_duration: int = None,
                         override_percent: int = None,
                         zone_ids: Optional[List[int]] = None,
                         ad_hoc_program_id: Optional[int] = None,
                         ad_hoc_program_name: Optional[str] = None) -> bool:
    zones = self.db.get_zones()
    group_zones = sorted(
        [z for z in zones if z['group_id'] == group_id],
        key=lambda x: x['id'],
    )
    if zone_ids is not None:
        wanted = set(int(z) for z in zone_ids)
        group_zones = [z for z in group_zones if int(z['id']) in wanted]
        # Preserve user-requested order if it differs from id-asc:
        order = {int(z): i for i, z in enumerate(zone_ids)}
        group_zones.sort(key=lambda z: order.get(int(z['id']), 9999))
    ...
```

The rest of the method is unchanged. Cumulative-start planner already calls
`per_zone_dur(z, override_duration, override_percent)`, so per-zone math
auto-covers the subset. `_run_group_sequence` runs whatever zones it gets —
no logic change needed.

`ad_hoc_program_id` / `ad_hoc_program_name` plumbing: see §1.4. They
ride along into the audit/log payload that `_run_group_sequence`
emits today (`group_seq_zone_start`, `group_seq_complete`). Add them
to the payload only — no behavioural change in the runner.

**Default behaviour (old callers):** `zone_ids=None` → method behaves
exactly as before. `start-from-first` is untouched.

### 1.3 Why NOT add fields to `QueueEntry` / NOT enqueue into `ProgramQueueManager`

The issue says "use the existing queue". Practically:

- `ProgramQueueManager._run_entry` is a stub (line 512:
  `# TODO: actual zone control integration`). It does not turn zones on.
  All real runs go through `_run_program_threaded` (scheduled programs)
  or `_run_group_sequence` (manual group / single zone).
- Per-group serialisation of manual runs is already handled by
  `start_group_sequence` reusing `group_cancel_events[gid]` — concurrent
  starts on the same group either co-own the cancel event (PR #17 logic)
  or queue up via `cancel_group_jobs` + restart pattern.
- Adding `override_duration` / `override_percent` to `QueueEntry` would be
  dead code — nothing reads them in the live path.

Therefore: **`QueueEntry` is NOT modified. `ProgramQueueManager.enqueue` is
NOT called from the new endpoint.** The "queue" the user means in the
issue is the per-group serialisation that `_run_group_sequence` already
provides. This is the pragmatic reading of "не дублируй логику запуска зон".

If a future refactor moves live runs into `ProgramQueueManager`, the
ad-hoc path is a one-line change there too — but that refactor is its
own issue.

### 1.4 Ad-hoc `program_id`: negative sentinel

**Choice: `program_id = -int(datetime.now().timestamp())`.**

Reasoning vs. `Optional[int]`:

| Aspect | Negative sentinel (chosen) | `Optional[int]` |
|---|---|---|
| Existing call sites that take `program_id: int` | No change. `is_program_run_cancelled_for_group(int(program_id), today, gid)` works (returns False — there's no row with negative id). Audit payloads accept any int. | Need `Optional[int]` plumbing through `_run_program_threaded`, `program_queue.py` audit, `is_program_run_cancelled_for_group`, `db.add_log`. ~10 sites. |
| Unique-per-call (avoid collisions in audit/history) | Yes — timestamp millisecond-unique enough; if collision, log_id PK still distinguishes rows. | N/A — would need separate uniqueness mechanism. |
| Distinguishability in queries | `WHERE program_id < 0` is one easy filter. Add an `'is_ad_hoc'` boolean to log payloads for clarity (cheap). | `WHERE program_id IS NULL` works but kills any FK semantics. |
| FK to `programs` table | Sentinel is NOT a valid FK — but `program_id` in `audit_log` / `logs` is **not** FK-constrained today (it's just an int). Verified: no `FOREIGN KEY (program_id) REFERENCES programs(id)` in schema. | Same. |
| Conflict with future negative IDs | Real `programs.id` is `INTEGER PRIMARY KEY AUTOINCREMENT`, always positive. Negative space is permanently free. | N/A. |

Action: emit it from the endpoint as `-int(datetime.now().timestamp())`,
pass through `start_group_sequence(..., ad_hoc_program_id=...,
ad_hoc_program_name=...)`. The runner uses these in audit emits that
currently hardcode `'group_id': group_id` — extend the payload with
`'program_id'` and `'program_name'` keys and `'is_ad_hoc': True`.

We do **not** add an `is_ad_hoc: bool` field to `QueueEntry` (we don't
use `QueueEntry` — see §1.3). For runtime detection in audit / log
consumers, the `program_id < 0` check is the canonical test, and we also
include the explicit `is_ad_hoc` boolean in payloads for clarity.

### 1.5 Override durations — pass through method kwargs, NOT QueueEntry

Today `start_group_sequence` already accepts `override_duration` and
`override_percent` (PR #21). Both are plumbed through `_run_group_sequence`
(positional `args=[group_id, zone_ids, override_duration, override_percent]`
on the APScheduler job, see PR #21 diff). Per-zone unfolding via
`per_zone_dur`.

**The new endpoint reuses this exact path.** Nothing new in the runner.
Nothing in `QueueEntry`.

This is option (a) from the parent prompt's framing — chosen because
issue #15 explicitly says "use existing queue / no duplicate logic", and
the existing override path IS the manual-run path.

### 1.6 Manual-vs-scheduled — delegate to APScheduler misfire

**Policy:**
1. While manual run is active in any group of a scheduled program, the
   scheduled run **does not start**.
2. Scheduled program waits until manual ends.
3. After manual ends, if exactly one scheduled fire was missed → run it.
   If multiple were missed → run only the LAST one. Earlier missed runs
   logged as `prog_skipped_manual_running`.

**Implementation:** the cleanest place is **at the top of
`_run_program_threaded`** (the actual entry point of any scheduled program
on the live path). Concretely:

```python
def _run_program_threaded(self, program_id, zones, program_name):
    # ----- Issue #15 manual-vs-scheduled guard -----
    # Compute group_ids touched by this program.
    program_gids = set()
    for z in zones:
        zd = self.db.get_zone(z)
        if zd:
            g = int(zd.get('group_id') or 0)
            if g and g != 999:
                program_gids.add(g)

    blocking_gids = [g for g in program_gids
                     if self.is_group_session_active(g)]
    if blocking_gids:
        # A manual run owns at least one group — drop this scheduled run.
        # The next scheduled fire (after manual ends) will run via
        # APScheduler's normal cron tick. misfire_grace_time=3600 is the
        # window; longer-stalled fires get coalesced/dropped by APScheduler.
        try:
            from services.audit import record_audit
            record_audit(
                action_type='prog_skipped_manual_running',
                source='scheduler',
                target=f'program:{program_id}',
                payload={
                    'program_id': program_id,
                    'program_name': program_name,
                    'blocking_gids': blocking_gids,
                    'scheduled_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                },
                actor='system',
            )
        except Exception:
            logger.exception("prog_skipped_manual_running audit failed")
        try:
            self.db.add_log('prog_skipped_manual_running', json.dumps({
                'program_id': program_id, 'program_name': program_name,
                'blocking_gids': blocking_gids,
            }))
        except Exception:
            logger.exception("prog_skipped_manual_running log failed")
        return  # do not run

    # ----- existing pre-register cancel-events code from PR #17/#22 -----
    ...
```

**Why this works for the "only the last missed" requirement:**

APScheduler with `misfire_grace_time=3600, coalesce=False`
(`schedule_program`, lines 832/861/887) will:
- queue ONE pending fire per missed slot,
- as soon as the worker is free, dispatch each pending fire IN ORDER.

Without our guard: each fire calls `_run_program_threaded` → tries to
start. Our guard says: "if manual still running, drop". So all fires that
land DURING manual get logged-and-dropped. The fire that lands AFTER
manual ends is the first one with no blocking session — it runs.

What about fires that land DURING manual, queue up, then APScheduler
dispatches them all in sequence right after manual ends? — They each
call `_run_program_threaded` synchronously? **No** — APScheduler dispatches
on a thread pool, and our `_run_program_threaded` checks
`is_group_session_active` at start. The first one that runs ACQUIRES the
group cancel event, so subsequent fires from the same program will see it
and drop. This is "only the last missed" emergent behaviour, except
without explicit tracking.

**Caveat — race:** if APScheduler dispatches fire #1 and fire #2 within
microseconds of each other, both might pass the `is_group_session_active`
check before either plants its cancel event. Mitigation:
`_run_program_threaded` already plants events via `setdefault` (PR #17 /
#22 — atomic). The second fire will see the existing event and continue,
trying to register zones — but it'll just re-enter the loop with the
same zones. The race window is sub-millisecond and self-resolves because
both fires of the SAME program have identical zones — running it twice
back-to-back is benign (post-zone OFF settles before second pass).

If the senior wants belt-and-suspenders: add a `program_run_in_flight:
Set[int]` guarded by a lock at the top of `_run_program_threaded`,
program_id-keyed. Out of scope for this spec — flag in §6.

**For "many missed → only last":** APScheduler's `misfire_grace_time=3600`
caps how long a missed fire stays valid. Manual run lasting >1h means
older fires fall outside the grace window and APScheduler drops them
silently (no `_run_program_threaded` call). We DON'T see them, so we
DON'T log them as skipped — which contradicts the AC "пропущенные
фиксируются в истории".

**Workaround (recommended, simple):** in `recover_missed_runs()` (already
exists, line 633 — runs at scheduler boot), add a pass that scans
`programs` for programs whose `time` is between `last_seen_run` and
`now`, and emits `prog_skipped_manual_running` for each that falls
within the manual-run window. **Simpler alternative:** when manual
run completes, `_run_group_sequence` finally clause inspects
"what scheduled fires SHOULD have happened during my run" by computing
each program's expected fires from cron triggers — and emits
`prog_skipped_manual_running` for all but the latest.

I recommend punting this to a separate minor issue ("history visibility
of misfire-dropped programs") because:
1. APScheduler's misfire window is a config value (`MISFIRE_GRACE = 3600`)
   — for any manual run <1h, fires are NOT dropped, our top-of-method
   guard logs them all.
2. Manual runs >1h are rare in irrigation (would be a 240-min cap × N
   zones; 60+ min is plausible only for "all 12 zones at 200%").
3. The "primary" requirement (manual ≯ scheduled, scheduled waits) is
   satisfied without this.

Note in §6 Open questions.

**Auditing:**
- `prog_skipped_manual_running` — the dropped scheduled fire. Payload:
  `{program_id, program_name, blocking_gids, scheduled_at}`.
- `prog_manual_run_selected` — the ad-hoc start. Payload:
  `{group_id, zones, ad_hoc_id, override_duration, override_percent}`.
- Existing `program_run_started` / `program_run_completed` etc. work
  unchanged for ad-hoc since we go through `_run_group_sequence`.

### 1.7 Stop / Skip Current Zone — works for free

**Stop:** the existing `cancel_group_jobs(gid)` (live path, used by zone
stop / group stop) sets `group_cancel_events[gid]`. The
`_run_group_sequence` loop polls this Event each second (line ~1463 in
`_run_group_sequence`). Works for ad-hoc identically because ad-hoc IS
running through `_run_group_sequence`.

**Skip Current Zone (PR #22):** `request_skip_current_zone(gid)` checks
`is_group_session_active(gid)` (which only requires `group_cancel_events`
to have a key for the gid — `start_group_sequence` plants that). For
ad-hoc, the same key is planted. The `group_skip_current_events[gid]`
event polled by the loop fires identically. No ad-hoc branching.

This is exactly what the issue's AC requires: "Кнопки «Стоп» и
«Пропустить зону» работают без специальных веток для ad-hoc". Confirmed:
no branches needed.

---

## 2. Frontend

### 2.1 Files

- `templates/status.html` — split the quick-actions row, add cancel button
  for select-mode (lines 145-150).
- `static/js/status.js` — `groupSelectMode` state machine, checkbox
  toggling, "Далее (N)" / "Отмена" buttons, dispatch via existing
  `showRunPopup`-style flow.
- `static/css/status.css` — `.zone-quick-actions.split` two-button layout,
  `.zone-card.selectable` checkbox visuals.

### 2.2 HTML changes (`templates/status.html`, lines 145-150)

**Today (post PR #18 — search button hidden via `display:none`):**
```html
<div class="zone-quick-actions">
  <button class="zq-btn zq-filled" id="zoneRunGroupBtn" onclick="runSelectedGroup()"
          data-audit-action="group_run_click">▶ Запустить</button>
  <button class="zq-btn zq-search" onclick="toggleZoneSearch()" style="display:none">🔍</button>
</div>
```

**After:**
```html
<div class="zone-quick-actions" id="zoneQuickActions">
  <!-- Default mode: two halves -->
  <button class="zq-btn zq-filled zq-half" id="zoneRunGroupBtn"
          onclick="runSelectedGroup()"
          data-audit-action="group_run_click">▶ Запустить все</button>
  <button class="zq-btn zq-outlined zq-half" id="zoneRunSelectedBtn"
          onclick="enterRunSelectedMode()"
          data-audit-action="group_run_selected_click">☑ Запустить выбранные</button>

  <!-- Select-mode: replaces the two halves (toggled via CSS class on parent) -->
  <button class="zq-btn zq-filled zq-half hidden-default" id="zoneSelectNextBtn"
          onclick="confirmRunSelectedNext()" disabled
          data-audit-action="group_run_selected_next">Далее (0)</button>
  <button class="zq-btn zq-outlined zq-half hidden-default" id="zoneSelectCancelBtn"
          onclick="exitRunSelectedMode()"
          data-audit-action="group_run_selected_cancel">Отмена</button>

  <!-- Search btn unchanged (still display:none from PR #18). -->
  <button class="zq-btn zq-search" onclick="toggleZoneSearch()" style="display:none">🔍</button>
</div>
```

CSS toggles which pair is visible based on
`document.body.classList.contains('mode-select-zones')`. ~6 lines of CSS.

### 2.3 JS changes (`static/js/status.js`)

**State (top-level, near `currentGroupFilter`):**
```js
var groupSelectMode = null; // null = off; { gid: int, selected: Set<int> } when on
```

**Mode entry — replaces or wraps current `runSelectedGroup`:**
```js
// New: enter select mode for the currently-active group tab
function enterRunSelectedMode() {
    var gid = currentGroupFilter;
    if (!gid) {
        // "All groups" view: select-mode is per-group only. Bail.
        showZoneToast('Выберите группу для выбора зон', 'info');
        return;
    }
    groupSelectMode = { gid: gid, selected: new Set() };
    document.body.classList.add('mode-select-zones');
    _renderSelectableZones();   // re-render zone cards with checkboxes
    _updateSelectedCounter();
}
function exitRunSelectedMode() {
    groupSelectMode = null;
    document.body.classList.remove('mode-select-zones');
    _renderSelectableZones();   // re-render without checkboxes
}
function toggleZoneSelected(zoneId) {
    if (!groupSelectMode) return;
    if (groupSelectMode.selected.has(zoneId)) {
        groupSelectMode.selected.delete(zoneId);
    } else {
        groupSelectMode.selected.add(zoneId);
    }
    _updateSelectedCounter();
    // Visual toggle on the card — flip a `.selected` class.
    var card = document.querySelector('[data-zone-id="' + zoneId + '"]');
    if (card) card.classList.toggle('selected', groupSelectMode.selected.has(zoneId));
}
function _updateSelectedCounter() {
    var n = groupSelectMode ? groupSelectMode.selected.size : 0;
    var btn = document.getElementById('zoneSelectNextBtn');
    if (btn) {
        btn.textContent = 'Далее (' + n + ')';
        btn.disabled = n === 0;
    }
}
```

**Zone card render hook** — find the existing zone-card render function
(`renderZoneCard` or similar in `status-groups.js` / `status.js`; search by
`data-zone-id` or `zone-card`). Add ONE branch:

```js
// In zone card click handler:
function onZoneCardClick(zoneId, event) {
    if (groupSelectMode && groupSelectMode.gid === currentGroupFilter) {
        toggleZoneSelected(zoneId);
        return;  // do NOT expand details
    }
    // existing expand-details path
    ...
}
```

The checkbox visual is purely a CSS overlay on the zone card when `body.mode-select-zones`
is set — no per-card DOM change needed. CSS:

```css
body.mode-select-zones .zone-card { padding-left: 36px; position: relative; }
body.mode-select-zones .zone-card::before {
  content: ''; position: absolute; left: 12px; top: 50%; margin-top: -10px;
  width: 20px; height: 20px; border: 2px solid #999; border-radius: 4px;
  background: white;
}
body.mode-select-zones .zone-card.selected::before {
  background: #00838f; border-color: #00838f;
}
body.mode-select-zones .zone-card.selected::after {
  content: '✓'; position: absolute; left: 16px; top: 50%; margin-top: -10px;
  color: white; font-size: 14px; font-weight: bold;
}
```

**"Далее" → reuse the existing duration popup (#runPopup):**

```js
function confirmRunSelectedNext() {
    if (!groupSelectMode || groupSelectMode.selected.size === 0) return;
    var gid = groupSelectMode.gid;
    var selectedZones = Array.from(groupSelectMode.selected);
    // Pass selected zones into the run popup. Reuse showGroupRunPopup,
    // augmented with a new payload field.
    showRunSelectedPopup(gid, selectedZones);
}

function showRunSelectedPopup(gid, zoneIds) {
    runPopupGroupId = gid;
    runPopupZoneId = null;
    _runPopupAllGroups = false;
    _runPopupSelectedZones = zoneIds;     // NEW global, read in confirmRun
    document.getElementById('runPopupTitle').textContent =
        '▶ Выбранные зоны (' + zoneIds.length + ')';
    runPopupDur = 15;
    runPopupMode = 'min';
    runPopupPct = null;
    var defBtn = document.getElementById('runPopupDefaults');
    if (defBtn) defBtn.style.display = 'block'; // "with defaults" still valid
    _refreshRunPopupModeUI();
    initDialTicks(); updateDial();
    document.getElementById('runPopupOverlay').classList.add('show');
    document.getElementById('runPopup').classList.add('show');
    setTimeout(initDialDrag, 100);
}
```

**Modify `confirmRun()`** (existing function, post PR #21):

```js
function confirmRun() {
    if (_runPopupSelectedZones && _runPopupSelectedZones.length > 0) {
        // Ad-hoc selected-zones path
        var body = (runPopupMode === 'pct')
            ? { zones: _runPopupSelectedZones, duration_percent: runPopupPct }
            : { zones: _runPopupSelectedZones, duration: runPopupDur };
        fetch('/api/groups/' + runPopupGroupId + '/run-selected', {
            method: 'POST',
            headers: apiHeaders(),
            body: JSON.stringify(body),
        }).then(function(r) { return r.json(); }).then(function(data) {
            if (data && data.success) {
                showZoneToast('▶ Запущены ' + _runPopupSelectedZones.length + ' зон(ы)', 'success');
                exitRunSelectedMode();
                closeRunPopup();
                setTimeout(function() {
                    Promise.all([loadStatusData(), loadZonesData()]);
                }, 1500);
            } else {
                showZoneToast((data && data.message) || 'Ошибка', 'error');
            }
        }).catch(function() { showZoneToast('Ошибка', 'error'); });
        return;
    }
    // ... existing single-zone / group / all-groups branches unchanged
}
```

`_runPopupSelectedZones` is reset to `null` in `closeRunPopup`,
`showRunPopup`, `showGroupRunPopup` (defensive — opening any other run
popup must clear stale state).

**`runGroupWithDefaults`** — out of scope. We do NOT add an
"ad-hoc with defaults" path because PR #21 already shows that 100% =
defaults. User picks 100% if they want defaults. If product wants the
explicit "📋 Запустить с настройками зон" button for the selected-zones
case, that's a follow-up.

### 2.4 Edge cases

| Case | Behaviour |
|---|---|
| 0 zones selected | `Далее (0)` disabled (`btn.disabled = true`). |
| All zones in group selected | Backend treats it as a normal `run-selected` with N=group_size. Equivalent to `start-from-first`, but explicit (and `program_id` is the ad-hoc sentinel — hist record marks it ad-hoc, not "scheduled program run"). User-visible behaviour identical to pressing "Запустить все" + same duration. |
| User hits "Все группы" tab while in select mode | `currentGroupFilter` change must call `exitRunSelectedMode()` (group context is lost). Hook into the existing tab-change handler. |
| Page reload / navigation away | State is in JS module scope (`groupSelectMode`), no localStorage — lost. Acceptable per issue ("уход со страницы → state сбрасывается"). |
| Tap on card body in select mode | `toggleZoneSelected(zoneId)`. Don't expand. (Edit: the existing expand handler must check `groupSelectMode` — see §2.3 onZoneCardClick.) |
| Tap on "Запустить все" while in select mode | Hidden via CSS — not reachable. Defensive: `runSelectedGroup` first calls `exitRunSelectedMode()` if mode is on (in case the CSS hide breaks). |
| "Выбрать все" / "Снять все" | **Skip for v1.** Issue says optional. Adding it requires another button slot in the action row (already at 2/2). Future: long-press group tab → "Select all in group". Out of scope. |

### 2.5 Reuse of duration selector — exact callout

The popup at `#runPopup` (`templates/status.html` lines 226-273) is the
component built by PR #21. After PR #21 it has:

- Dial (`#dialSvg` / `#dialValue`) — minutes mode.
- Minute-presets row `#runPopupPresets` — `setRunDur(N)`.
- Percent-presets row `#runPopupPctPresets` — `setRunPct(P)`.
- "Запустить" / "Отмена" / optional "📋 С настройками зон".

We **don't add anything** to this popup. We just feed `_runPopupSelectedZones`
into the existing `confirmRun()` dispatch and let it route to our new endpoint.

The "📋 С настройками зон" path (`runGroupWithDefaults`) is currently a
group-wide call to `start-from-first` (no override). For selected-zones,
"defaults" = each zone's own `duration` = our new endpoint with
`duration_percent: 100` and ALL selected zone_ids. The current
implementation calls `runGroupWithDefaults` against the WHOLE group,
which is wrong for our selected-zones flow. Either:

- (a) Hide the "📋 С настройками зон" button in select-mode popup
  (`if (_runPopupSelectedZones) defBtn.style.display = 'none';`).
- (b) Route it through `run-selected` with `duration_percent: 100`.

**Recommendation: (a) — hide it.** Selecting "100%" gives the same effect.
(b) is more code and the button label confuses ("настройки зон" implies
all zones).

---

## 3. Tests

### 3.1 Backend (`tests/api/test_run_selected_api.py` — new file)

Conventions match `test_groups_api_comprehensive.py` (existing).

```python
import json
import pytest

class TestRunSelected:
    def test_run_selected_minutes_ok(self, admin_client, app):
        # Group with 3 zones (durations 10/20/30); request 2 of them at 15 min.
        # Use the test fixture pattern from test_groups_api_comprehensive.
        ...
        resp = admin_client.post(f'/api/groups/{gid}/run-selected',
                                 data=json.dumps({'zones': [z1, z2], 'duration': 15}),
                                 content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['ad_hoc_program_id'] < 0
        # In TESTING mode, _run_group_sequence short-circuits to first zone:
        assert app.db.get_zone(z1)['planned_end_time'] is not None
        # ~now + 15 min:
        # (delta-check against now() with ±2s tolerance)

    def test_run_selected_percent_ok(self, admin_client, app):
        # zones [z1=20, z2=40], select both, percent=80 → durations [16, 32]
        resp = admin_client.post(f'/api/groups/{gid}/run-selected',
                                 data=json.dumps({'zones': [z1, z2], 'duration_percent': 80}),
                                 content_type='application/json')
        assert resp.status_code == 200
        # planned_end_time of z1 ~= now + 16 min (TESTING short-circuit on first zone)

    def test_run_selected_empty_zones(self, admin_client):
        resp = admin_client.post(f'/api/groups/{gid}/run-selected',
                                 data=json.dumps({'zones': []}),
                                 content_type='application/json')
        assert resp.status_code == 400

    def test_run_selected_zone_not_in_group(self, admin_client, app):
        # zone z_other belongs to gid=2; we request it under gid=1
        resp = admin_client.post(f'/api/groups/1/run-selected',
                                 data=json.dumps({'zones': [z_other]}),
                                 content_type='application/json')
        assert resp.status_code == 400
        assert 'не принадлежит' in resp.get_json()['message']

    def test_run_selected_zone_disabled(self, admin_client, app):
        # mark z2 as disabled; request [z1, z2]
        # NB: confirm column name — may be 'enabled' or 'is_enabled'. See §6.
        resp = admin_client.post(f'/api/groups/{gid}/run-selected',
                                 data=json.dumps({'zones': [z1, z2]}),
                                 content_type='application/json')
        assert resp.status_code == 400
        assert 'отключена' in resp.get_json()['message']

    def test_run_selected_minutes_wins_over_percent(self, admin_client, app):
        # Both sent — minutes mode is taken; percent is ignored.
        # Per PR #21 contract: if `duration` is present at all, validate it
        # OR 400. We MUST mirror this behaviour. Test: send {duration:15,
        # duration_percent:200} → endpoint accepts duration=15, ignores 200,
        # planned_end_time ~= now+15 (NOT now+ceil(z1.duration*2)).
        resp = admin_client.post(...)
        assert resp.status_code == 200
        # check planned_end ≈ 15 min, not 200% of z1

    def test_manual_blocks_scheduled(self, admin_client, app, scheduler):
        # Start ad-hoc on group 1.
        # Synthesize a scheduled program-fire for a program touching group 1.
        # Assert: prog_skipped_manual_running entry in db.add_log;
        #   no zone in group 1 transitioned to 'on' from the scheduled call.
        admin_client.post('/api/groups/1/run-selected',
                          data=json.dumps({'zones': [z1], 'duration': 5}),
                          content_type='application/json')
        # Now invoke _run_program_threaded directly (bypassing APScheduler):
        scheduler._run_program_threaded(prog_id, [z1, z2], 'Test Sched')
        # Assert audit / log row added with prog_skipped_manual_running;
        # no second `program_run_started` audit row for prog_id.
        rows = [r for r in app.db.get_logs() if r.get('action') == 'prog_skipped_manual_running']
        assert len(rows) == 1
        assert rows[0]['payload']['program_id'] == prog_id

    def test_only_last_missed_runs_after_manual(self, app, scheduler):
        # TODO: requires time-mocking of APScheduler — non-trivial.
        # Skip with rationale: emergent behaviour from
        # is_group_session_active+misfire_grace_time. Manual integration
        # test on device is the practical verification path.
        pytest.skip('Time-mocking APScheduler needed; manual QA path used.')
```

**Notes on test #6 (manual blocks scheduled):**
The cleanest path is to invoke `scheduler._run_program_threaded(...)`
directly while a manual run is "in flight" (i.e.,
`group_cancel_events[gid]` is set by `start_group_sequence`). In TESTING
mode, `_run_group_sequence` short-circuits after the first zone's DB
update — perfect for our test, the cancel-event stays in the dict for
the duration of the test.

### 3.2 Frontend (Playwright, run on device after merge)

To be added to existing Playwright suite (the project already has one
per the file layout). Cases:

- Mode entry: tap "☑ Запустить выбранные" → `body.mode-select-zones` set,
  `Далее (0)` visible & disabled, `Отмена` visible.
- Counter: tap 3 zone cards → `Далее (3)` enabled, all 3 cards have
  `.selected`.
- Untoggle: tap selected card → counter decrements.
- 0 selected → disabled state of `Далее` confirmed.
- Full flow: select 2 zones → Далее → set 15 min → Запустить →
  `body.mode-select-zones` removed, popup closed, toast shown, status
  refreshes within 2s.
- Switching to "Все группы" tab while in select mode → mode exits.

NOT in this PR — Playwright comes after merge, executed on
`http://10.2.5.244:8000` (or whatever the test target is).

---

## 4. Files touched

| File | Change | LOC |
|---|---|---|
| `routes/groups_api.py` | New endpoint `api_run_selected(gid)`. Optional: extract `_parse_overrides(body)` helper if not done in PR #21. Optional: extract `_preflight_warnings(...)` helper. | +60-80 |
| `irrigation_scheduler.py` | `start_group_sequence`: add 3 kwargs (`zone_ids`, `ad_hoc_program_id`, `ad_hoc_program_name`); ~15 lines for subset filtering + audit payload extension. `_run_program_threaded`: 25-line manual-vs-scheduled guard at top of method. | +40-50 |
| `templates/status.html` | Split quick-actions into 2 default + 2 select-mode buttons. ~10 lines added. | +10 |
| `static/js/status.js` | `groupSelectMode` state, `enterRunSelectedMode` / `exitRunSelectedMode` / `toggleZoneSelected` / `_updateSelectedCounter` / `showRunSelectedPopup`; tweak `confirmRun()` to dispatch to `run-selected` when `_runPopupSelectedZones` set. Extend zone-card click handler. | +90 |
| `static/js/status/status-groups.js` | If zone-card render is here: extend with `data-zone-id` attribute (likely already present) and a click branch that calls `toggleZoneSelected` in select mode. | +5 |
| `static/css/status.css` | `.zone-quick-actions` split layout, `body.mode-select-zones` checkbox visuals. | +25 |
| `tests/api/test_run_selected_api.py` | New file — 7 tests per §3.1. | +250 |
| `specs/issue-15-architecture.md` | This file. | (out of code count) |

**Total estimate:** ~480 LOC, of which ~250 are tests. Code: ~230 LOC.

**NOT touched (intentionally):**
- `services/program_queue.py` — see §1.3.
- `scheduler/program_runner.py` — dead mixin per parent prompt; live code is in `irrigation_scheduler.py`.
- `scheduler/jobs.py` — `job_run_group_sequence` already takes
  `override_duration` / `override_percent` (post PR #21). It does NOT need
  to know about `zone_ids` because that's resolved before the APScheduler
  job is created — only the resolved `zone_ids` array is passed in `args=`.
- `routes/zones_watering_api.py` — single-zone path stays.
- `routes/programs_api.py` — manual-program-run (PR #19) stays; gets the
  same manual-vs-scheduled benefit because it ends up in
  `_run_program_threaded` with positive program_id (registered as a
  manual run via `is_group_session_active` post-#17).
- `database.py` schema — no migration. Negative `program_id` requires no
  schema change.

---

## 5. Merge notes

| PR | Branch | Files we both touch | Conflict | Order |
|---|---|---|---|---|
| #17 | `fix/16-stop-cancels-queue` | `irrigation_scheduler.py` (the `setdefault` cancel-event pre-register block at top of `_run_program_threaded`) | **YES — overlapping.** PR #17 plants cancel events at top of `_run_program_threaded`; we plant a manual-vs-scheduled GUARD at top. Both touch the same area. | **#17 → #15.** The guard goes BEFORE the cancel-event pre-register block (we exit early if blocking). |
| #22 | `feat/14-skip-zone-in-group-watering` | `irrigation_scheduler.py` (similar block; also adds `is_group_session_active`, `request_skip_current_zone`, `group_skip_current_events`) | **YES — overlapping.** Adds `is_group_session_active` which our guard CALLS. Without #22, our guard has nothing to call. | **#22 → #15.** Hard dep. If #22 not merged first, we add a vendored copy of `is_group_session_active` (inline 3-liner) and TODO-remove it post-merge. |
| #21 | `feat/12-duration-percent-of-norm` | `routes/groups_api.py` (`_parse_overrides`-style validation), `irrigation_scheduler.py` (`override_percent` plumbing), `static/js/status.js` (`confirmRun` percent dispatch + `runPopupMode`), `templates/status.html` (% row in popup) | **YES.** We rely on `runPopupMode`/`runPopupPct` JS state and `per_zone_dur` Python helper. We rely on `start_group_sequence` already accepting `override_percent`. | **#21 → #15.** Hard dep. Without #21 we cannot pass `duration_percent` through the stack. If #21 doesn't land first: drop `duration_percent` from the new endpoint contract for v1 (minutes-only), add it in a follow-up. |
| #18 | `feat/13-hide-search-on-main` | `templates/status.html` lines 145-150 | YES — the same line range. PR #18 hides the search button (`style="display:none"`) and frees room for our split-button row. | **#18 → #15.** PR #18 is a 1-line change; trivial to merge after if reversed. |
| #19 | `feat/10-program-run-endpoint` | `routes/programs_api.py` only. | **NO.** We don't touch `programs_api.py`. PR #19 adds `api_run_program(prog_id)` which uses `threading.Thread(target=job_run_program, ...)`. After #15, that path ALSO benefits from manual-vs-scheduled (positive `program_id` runs through `_run_program_threaded`, plants `group_cancel_events` per #17/#22). No conflict. | Independent. |
| #20 | `feat/11-zone-photos-thumb-lightbox` | `templates/status.html` (zone card area), `static/js/status.js` (zone card render) | **POSSIBLE but minor.** PR #20 adds image elements to zone cards. Our select-mode adds `body.mode-select-zones .zone-card.selected::before/::after` styling. The CSS `::before`/`::after` is on `.zone-card`, not on the photo element — should be visually compatible. | Independent — pick whichever order; cosmetic conflict possible. |

**Recommended merge order:**
`#18 → #17 → #22 → #19 → #20 → #21 → #15`.

Hard deps for #15: **#22 and #21.** Without them, the spec needs to fork
into a degraded mode (no skip-current ad-hoc support; no percent ad-hoc
support). #17 is soft-dep (we add the guard, but it would still log the
skip; the no-#17 race window is sub-millisecond and survivable).

---

## 6. Open questions

1. **`enabled` column name on zones.** Confirm whether the column is
   `enabled`, `is_enabled`, or implicit (no column → all zones runnable).
   If absent — drop the "all enabled" check from validation (just check
   exists + group). One-line decision; affects test #5.

2. **Misfire-window logging for "older missed scheduled runs".** Current
   spec covers fires that DO reach `_run_program_threaded` (within
   `misfire_grace_time=3600`). Older fires are dropped silently by
   APScheduler — not logged as `prog_skipped_manual_running`. Two paths:
   (a) accept the gap (manual runs >1h are rare); (b) extend
   `recover_missed_runs()` (already exists, line 633) to backfill skipped
   entries. **Decision needed: a or b?** I recommend (a) for v1 and a
   separate issue for (b).

3. **Belt-and-suspenders manual-vs-scheduled lock.** The race in §1.6
   (two APScheduler fires of the SAME program within microseconds) is
   sub-millisecond and benign. Do we want a `program_run_in_flight: Set[int]`
   guarded by a lock, keyed by `program_id`? Cost: +5 lines + 1 RLock.
   Benefit: tiny. **Recommendation: no, document and skip.**

4. **"Все группы" view + select mode.** Issue says "in one group". We
   bail when `currentGroupFilter` is null ("Все группы" tab). UX: maybe
   gray-out the "☑ Запустить выбранные" button when on "Все группы"?
   Minor polish — flag for senior.

5. **Negative-ID sentinel collision risk.** `int(datetime.now().timestamp())`
   is second-resolution. Two ad-hoc runs in the same second → same ID. In
   practice impossible (UI rate-limits to ~1/sec, and even with collision
   the audit `entry_id` (UUID) and DB log row PKs distinguish them). If
   the senior wants belt-and-suspenders: use `int(time.monotonic_ns() // 1000)`
   and negate. **Recommendation: keep `-int(timestamp())`, document.**

6. **PR #19 "manual_run" detection by guard.** Manual-program-run from
   PR #19 spawns `job_run_program` in a thread, which lands in
   `_run_program_threaded` with a POSITIVE `program_id`. Our guard would
   THEN block IT against itself if a scheduled fire of the same program
   races. That's correct: the manual run wins (got there first, planted
   cancel-events), the scheduled fire hits the guard, drops, logged as
   `prog_skipped_manual_running`. Documented for clarity, no change needed.

---

## 7. Karpathy check

- **Simplicity first:** no new sequencer, no new queue, no new sentinel
  abstraction, no new module. One endpoint, one method extension, one
  guard, one frontend mode. The spec resists the temptation to "wire
  ad-hoc through `ProgramQueueManager`" — that would have required adding
  zone-control to the stub `_run_entry`, doubling the surface.

- **Surgical changes:** `start_group_sequence` gets 3 new keyword args
  with defaults — old callers untouched. `_run_program_threaded` gets a
  guard at top, no other changes. `confirmRun()` gets one new branch.
  `QueueEntry` not modified. `services/program_queue.py` not touched.

- **Goal-driven:** every test in §3.1 maps to an AC bullet from the
  issue. Tests come BEFORE implementation in the dev order (TDD).

- **Push back logged:** §6 lists the 6 places where this spec defers
  judgment to senior review. The most important is #2 (misfire-window
  logging gap), which is a real semantic limitation of the chosen
  manual-vs-scheduled implementation.
