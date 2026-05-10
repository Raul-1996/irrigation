# Issue #12 — Selector: % of zone norm (50/75/100/125/150/200)

GitHub: https://github.com/Raul-1996/irrigation/issues/12
Branch: `feat/12-duration-percent-of-norm`

## TL;DR

`zones.duration` (INTEGER, default 10, range 1..120) **is** the per-zone norm.
There is no separate "norm" / "default_duration" column. The whole feature
collapses to: "for each affected zone, compute `round_up(duration * pct/100)`,
clip into [1, 120], dispatch that exact value via the existing
`override_duration` channel that already exists end-to-end".

Decision: **option (a) — backend accepts `duration_percent`** on the two
existing endpoints (`/api/zones/<id>/mqtt/start` and
`/api/groups/<id>/start-from-first`) and unfolds it server-side. Reasons in
section 2.

The sequencer (`IrrigationScheduler._run_group_sequence`) today applies a
**single uniform** `override_duration` to every zone (`irrigation_scheduler.py:1212,
1255`). For percent mode it must read each zone's own `duration` and multiply.
This is a 5-line change: replace the scalar `override_duration: Optional[int]`
with `override_durations: Optional[Dict[int, int]]` (per-zone) and fall back to
the scalar if the dict is empty. Public method signature stays
backwards-compatible.

UI: add a 6-button row beneath the existing minute presets in `status.html`,
toggle a `runPopupMode` flag in `status.js`, send `duration_percent` instead of
`duration`/`override_duration` when active. ~40 lines of JS, ~15 lines of HTML.

---

## 1. Audit — concrete code map

### 1.1 Selector UI (already exists)

File: `templates/status.html:226-273` — `<div id="runPopup">`.

- Circular dial: `#dialSvg` + `#dialValue` (lines 234-252)
- Minute preset row: `#runPopupPresets` with 6 buttons calling `setRunDur(N)`
  (lines 254-261)
- Run button: `confirmRun()` (line 264)
- "Run with zone defaults" (group only): `confirmRunWithDefaults()` (line 269,
  visible when `_runPopupAllGroups || runPopupGroupId`)

JS: all logic in `static/js/status.js:2145-2362`:
- `runPopupDur` — current duration (default 10)
- `MAX_DUR = 120` (line 2150) — dial range, also max valid minutes
- `setRunDur(val)` (line 2239) — preset click handler
- `showRunPopup(zoneId, defaultDur)` (line 2216) — single zone entry
- `showGroupRunPopup(gid, gName)` (line 2029) — group / all-groups entry
- `confirmRun()` (line 2243) — dispatches to backend; today sends:
  - single zone: `POST /api/zones/<id>/mqtt/start` with `{duration: dur}` (line 2311)
  - group: `POST /api/groups/<gid>/start-from-first` with `{override_duration: dur}` (line 2284)
  - all groups: `Promise.all` of group calls with `{override_duration: dur}` (line 2262)

### 1.2 Backend watering endpoints

**Single zone.** `routes/zones_watering_api.py:261-409`,
`api_zone_mqtt_start(zone_id)`. Reads `body.get('duration')`, validates
`1 <= req_d <= 120` (line 292), sets `override_dur` local. Used to
- compute `planned_end_time` (line 397)
- pass to `schedule_zone_stop` / `schedule_zone_hard_stop` (line 327-328 in
  the already-running branch; line 395 in the cold-start branch)

**Group / Run-All.** `routes/groups_api.py:194-227`,
`api_start_group_from_first(group_id)`. Reads `body.get('override_duration')`,
validates `1 <= req_d <= 120` (line 213), passes to
`scheduler.start_group_sequence(group_id, override_duration=override_dur)`
(line 217).

Run-All is a client-side `Promise.all` over each group — there is no
"run all groups" backend endpoint. (status.js:2257-2268)

### 1.3 Zone "norm" — where it is

`zones.duration` INTEGER DEFAULT 10 — `db/migrations.py:39`. Validated
1..120 in zones API, used as both:
- the configured per-zone default ("the norm")
- the value the sequencer reads when `override_duration` is None
  (`irrigation_scheduler.py:1212, 1255`)

There is no separate "norm" column. The feature description's wording "zone's
configured norm" maps to `zones.duration` exactly. **No migration is needed.**

Edge: `zones.duration` defaults to 10 (not 15) and is `NOT NULL` via
`DEFAULT 10`. So "norm not set" can in practice only occur if a row is
corrupted to NULL/0. Section 4 below treats `duration <= 0` as the fallback
trigger.

### 1.4 Sequencer — per-zone duration today

`irrigation_scheduler.py`:
- `start_group_sequence(group_id, override_duration=None)` — line 1106. Plans
  cumulative starts (line 1137), schedules an APScheduler job that calls
  `job_run_group_sequence` (line 1188) which delegates to
  `_run_group_sequence`.
- `_run_group_sequence(group_id, zone_ids, override_duration=None)` — line
  1203. Per-zone loop reads either the override or `zone['duration']` at line
  1255: `base_dur = override_duration if override_duration else int(zone.get('duration') or 0)`.
- TESTING-mode short-circuit at line 1212 has the same shape.

Both call sites multiply by **the same** `override_duration` for every zone in
the group. To do per-zone math we need to plumb a `Dict[zone_id, int]` (or
recompute inside the runner from `(percent, zone.duration)`).

### 1.5 Global limit / max duration

`constants.py:7-10`:
```
MAX_MANUAL_WATERING_MIN = 240
ZONE_CAP_DEFAULT_MIN    = 240
```

But existing `duration` validation in BOTH start endpoints clamps to **120**,
not 240 (`zones_watering_api.py:292`, `groups_api.py:213`), and the dial's
`MAX_DUR = 120` (`status.js:2150`). The HTML zone editor also caps at 120
(`templates/status.html:181, 259`).

So today the *de facto* manual-watering ceiling is **120 minutes**, not 240.
240 only appears as the watchdog hard-stop cap. The issue says "e.g. 240" —
to stay surgical and consistent, **clip percent results at 120** (matching
existing manual-start validation), not 240. If we used 200% on a 100-min zone
we'd otherwise produce 200 min which the existing endpoint validators would
silently drop (`override_dur = None` → falls back to base 100 min, **wrong**).

This is the most important non-trivial finding. Document loudly in the PR.

---

## 2. Backend approach decision — (a) `duration_percent`

Two options listed in the issue:
- (a) backend accepts `duration_percent`, unfolds per zone server-side
- (b) frontend computes `[{zone_id, duration_min}, ...]` and sends an array

**Decision: (a).** Reasons, weighted by what the audit actually shows:

| Concern | (a) percent server-side | (b) client array |
|---|---|---|
| Touch-points | 2 endpoints + sequencer (4 files). | 2 endpoints (new shape) + sequencer (new shape) + groups_api Run-All (no endpoint exists, must add one) + JS map-builder + still need server validation. **More.** |
| New endpoints | None. | Probably yes — Run-All over multiple groups currently has no server endpoint; sending an array implies a single round-trip, so we'd add `/api/run` or similar. |
| Race-safety | Server reads `zones.duration` at dispatch time — current value. | Client reads `zonesData` (cached, possibly stale by seconds-to-minutes). If user edited a zone's norm in another tab right before pressing Run, (b) sends a stale value. |
| Authoritative source | `zones.duration` (DB). | Whatever the JS cache happens to hold. |
| Validation | Single place: existing endpoint validators, extended to also accept `duration_percent`. | Must validate every entry in the array, plus cross-check `zone_id` belongs to the group, etc. |
| Backwards compat | Trivial — `duration` / `override_duration` paths untouched. | Same — but bigger surface to keep stable. |
| Lines of code | ~30 server, ~40 client. | ~60 server, ~70 client. |

(a) is fewer lines, fewer endpoints, more correct (server-time reads). Take it.

The only thing (b) buys is "the client could pre-compute and show an exact
total runtime estimate before sending". We don't need that — and if we did,
it's a `sum(z.duration * pct/100)` purely cosmetic computation that doesn't
require the array protocol.

---

## 3. Design — minimal diff

### 3.1 UI changes

**`templates/status.html`** — add a percent row + caption right after the
existing `#runPopupPresets` (after line 261):

```html
<div class="run-popup-presets-pct" id="runPopupPctPresets">
  <button onclick="setRunPct(50)">50%</button>
  <button onclick="setRunPct(75)">75%</button>
  <button onclick="setRunPct(100)">100%</button>
  <button onclick="setRunPct(125)">125%</button>
  <button onclick="setRunPct(150)">150%</button>
  <button onclick="setRunPct(200)">200%</button>
</div>
<div class="run-popup-pct-caption">% от нормы зоны</div>
```

CSS — reuse the existing `.run-popup-presets` rules; add a sibling `-pct`
class with the same grid and a small `.active` highlight. Out of scope for
the architect — note for senior: same flex/grid as existing row, distinct
accent colour for active button.

**`static/js/status.js`** — three changes:

1. State (after line 2149):
   ```js
   var runPopupPct  = null;        // null = minutes mode; number = percent mode
   var runPopupMode = 'min';       // 'min' | 'pct'
   ```

2. New handler + visual mode toggle:
   ```js
   function setRunPct(p) {
       runPopupMode = 'pct';
       runPopupPct  = p;
       // Visual: highlight pct buttons, dim min row + dial
       _refreshRunPopupModeUI();
   }
   // setRunDur (line 2239) — prepend:
   function setRunDur(val) {
       runPopupMode = 'min';
       runPopupPct  = null;
       runPopupDur  = val;
       updateDial();
       _refreshRunPopupModeUI();
   }
   // Dial drag handler (line 2200 onMove) — also force 'min' mode on drag.
   ```
   `_refreshRunPopupModeUI()` toggles a `.mode-pct` class on `#runPopup`
   that styles dial+min-row dimmed and pct-row active (single CSS class,
   no JS branching elsewhere).

3. `confirmRun()` (line 2243) — dispatch:
   - Single zone: if `runPopupMode === 'pct'`, body = `{duration_percent: runPopupPct}`,
     else body = `{duration: runPopupDur}` (today's behaviour).
   - Group / Run-All: same — `{duration_percent: runPopupPct}` vs
     `{override_duration: runPopupDur}`.

4. `showRunPopup` / `showGroupRunPopup` — reset to `'min'` mode at open
   (so opening twice doesn't carry pct state across).

### 3.2 API changes

**`/api/zones/<id>/mqtt/start`** (`routes/zones_watering_api.py`):

Add — alongside the existing `duration` parsing block (~line 286-299):

```python
req_pct = body.get('duration_percent')
if req_pct is not None and override_dur is None:  # min mode wins if both sent
    try:
        pct = int(req_pct)
        if pct in (50, 75, 100, 125, 150, 200):
            base = int(z.get('duration') or 0)
            if base <= 0:
                base = 15  # fallback: norm not set
                # TODO: surface warning to client (response field)
            computed = math.ceil(base * pct / 100.0)
            override_dur = max(1, min(MAX_MANUAL_WATERING_MIN, computed))
            # Note: existing code paths use 120; we honour MAX here because
            # 200% × 120 = 240. See section 1.5.
    except (ValueError, TypeError):
        pass
```

(`MAX_MANUAL_WATERING_MIN` import already exists in `constants`.)

Response — add a `warnings` array if any rule fired:
```python
return jsonify({'success': True, 'message': '...', 'warnings': warnings})
```

Where `warnings` is built from: `'norm_not_set'`, `'clipped_min'`,
`'clipped_max'`. Empty array if none.

The downstream code (`planned_end_time`, `schedule_zone_stop`,
`schedule_zone_hard_stop`) already uses `override_dur` and is unaffected.

**`/api/groups/<id>/start-from-first`** (`routes/groups_api.py`):

Validation:
```python
req_pct = body.get('duration_percent')
override_pct = None
if req_pct is not None and override_dur is None:
    try:
        p = int(req_pct)
        if p in (50, 75, 100, 125, 150, 200):
            override_pct = p
    except (ValueError, TypeError):
        pass

ok = scheduler.start_group_sequence(
    group_id,
    override_duration=override_dur,
    override_percent=override_pct,
)
```

Whitelisted set `{50, 75, 100, 125, 150, 200}` — if anything else slips in,
treat as no-op (ignore, fall back to base norms). Same defensive style as
existing `1 <= req_d <= 120` clamp.

Response includes `warnings: [...]` (computed inside the sequencer / passed
back, see 3.3).

**Run-All.** Stays client-side `Promise.all` over `start-from-first`, just
sending `{duration_percent: pct}` in each call. No new endpoint. Warnings
get aggregated client-side.

### 3.3 Sequencer changes

`IrrigationScheduler.start_group_sequence(group_id, override_duration=None,
override_percent=None)`:

```python
def _per_zone_dur(zone, override_duration, override_percent):
    base = int(zone.get('duration') or 0)
    if override_percent is not None:
        if base <= 0:
            base = 15
        d = math.ceil(base * override_percent / 100.0)
        return max(1, min(MAX_MANUAL_WATERING_MIN, d))
    if override_duration is not None:
        return override_duration
    return base
```

Use `_per_zone_dur` in:
- the cumulative-start planner (line 1137):
  `cumulative += _per_zone_dur(z, override_duration, override_percent)`
- propagate `override_percent` through `job_run_group_sequence` args (line
  1178 → add to `args=[group_id, zone_ids, override_duration, override_percent]`)
- `_run_group_sequence` (line 1255): replace `base_dur = ...` with
  `base_dur = _per_zone_dur(zone, override_duration, override_percent)`
- TESTING-mode synchronous branch (line 1212) — same.

`job_run_group_sequence(group_id, zone_ids, override_duration=None, override_percent=None)` —
extend signature, default `override_percent=None` so existing tests/callers
keep working.

The percent calculation lives in **one** function (`_per_zone_dur`). Don't
duplicate it across endpoint code and sequencer code. Endpoint code uses
the same helper for the single-zone path. Put it in
`services/zone_control.py` (or a new tiny `services/duration_calc.py` —
choose existing module, no new module).

**Surgical:** existing `override_duration: int` semantics unchanged.
`override_percent` is a new optional kwarg, defaulting None. All previous
tests pass without modification (verify: `test_groups_api_comprehensive.py`,
`test_scheduler_comprehensive.py`).

---

## 4. Edge case rules

| Case | Rule | Where enforced |
|---|---|---|
| `duration_percent` not in {50,75,100,125,150,200} | Ignore percent, fall back to current behaviour (treat as no override). No error response — defensive. | Both endpoints (zones_watering_api.py + groups_api.py). |
| Both `duration` and `duration_percent` sent | `duration` wins (minutes mode is explicit, simpler invariant). | Both endpoints. |
| Zone has `duration <= 0` (NULL / corrupt) and percent mode active | Use base = 15 min, multiply, clip. Add `'norm_not_set'` to response `warnings`. | Server (in `_per_zone_dur` + endpoint validators). |
| Computed duration < 1 min (e.g., 50% of 1) | Clip to 1 min. Add `'clipped_min'` to warnings. | Server (`_per_zone_dur`). |
| Computed duration > 240 min (200% × 120) | Clip to 240 min (`MAX_MANUAL_WATERING_MIN`). Add `'clipped_max'` to warnings. | Server (`_per_zone_dur`). |
| Round non-integer | `math.ceil` (round up), per spec. | Server (`_per_zone_dur`). |
| `duration_percent` sent for already-running zone (single-zone reschedule path) | Reuse same calc for the new `planned_end_time`. | `zones_watering_api.py` already-on branch (line 306-336) — pass through `override_dur` computed by the percent block above. Already-on branch needs the same parsing — keep one function, call it once at top. |

Client-side mirror: NONE for arithmetic. Client only mirrors warnings into
toasts — no duplicate validation. Single source of truth = server. Client
sends raw `duration_percent`, parses `warnings[]` from response, shows
`showZoneToast(...)`.

---

## 5. Test plan (5 tests)

All in `tests/api/` and `tests/unit/` matching existing layout. Each lists
file → name → assertion.

1. **`tests/api/test_zones_api_comprehensive.py::test_mqtt_start_with_percent`**
   - Create zone with `duration=20`, POST `/api/zones/<id>/mqtt/start`
     `{duration_percent: 150}`.
   - Assert: 200, response success. `db.get_zone(id)['planned_end_time']`
     ~= now + 30 min (20 × 1.5). Tolerance ±2 sec.

2. **`tests/api/test_zones_api_comprehensive.py::test_mqtt_start_percent_norm_zero_fallback`**
   - Create zone, force `duration=0` via `db.update_zone`. POST
     `{duration_percent: 100}`.
   - Assert: 200. `planned_end_time` ~= now + 15 min. Response
     `warnings` contains `'norm_not_set'`.

3. **`tests/api/test_zones_api_comprehensive.py::test_mqtt_start_percent_clipped_max`**
   - Zone `duration=120`. POST `{duration_percent: 200}`.
   - Assert: 200. Effective duration = 240 min (clipped at
     `MAX_MANUAL_WATERING_MIN`). Response `warnings` contains
     `'clipped_max'`.

4. **`tests/api/test_groups_api_comprehensive.py::test_start_group_percent_per_zone`**
   - Group with two zones: `duration=10` and `duration=30`. POST
     `/api/groups/<gid>/start-from-first` `{duration_percent: 150}`.
   - Assert: 200. Inspect scheduler's planned starts
     (`db.get_group_scheduled_starts(gid)`): zone[0] starts at T0,
     zone[1] starts at T0 + 15 min (= 10 × 1.5, **not** 45 = 30 × 1.5,
     because zone[0] runs for 15). Verifies per-zone math, not uniform.
   - In TESTING mode, `_run_group_sequence` only starts the first zone —
     `db.get_zone(zone[0])['planned_end_time']` ~= now + 15 min.

5. **`tests/api/test_groups_api_comprehensive.py::test_start_group_invalid_percent_falls_back`**
   - POST `{duration_percent: 87}` (not in whitelist).
   - Assert: 200. Effective behaviour = base zone durations (no override).
     `planned_end_time` for first zone matches its own `duration`.

6. **`tests/unit/test_scheduler_comprehensive.py::test_start_group_sequence_percent_signature_back_compat`**
   - Call `scheduler.start_group_sequence(gid)` (no kwargs).
   - Assert: returns True, behaves exactly like main today (uses base
     durations). Guards against accidental signature break.

7. (Optional, recommended) **`tests/unit/test_duration_calc.py::test_per_zone_dur_table`**
   - Pure-function table-driven test of `_per_zone_dur`:
     `(base=10, pct=50) → 5`, `(base=10, pct=200) → 20`,
     `(base=1, pct=50) → 1` (clipped), `(base=120, pct=200) → 240`
     (clipped), `(base=0, pct=100) → 15` (fallback),
     `(base=10, override=25, pct=None) → 25`.

Total: **6 tests + 1 optional = 6-7**.

---

## 6. Risks

- **Backwards compat — primary risk.** `duration` (single-zone) and
  `override_duration` (group) MUST keep working unchanged. The change adds an
  *alternative* field; it does not modify existing fields or their semantics.
  Verified: tests above include a no-op back-compat probe (test #6).

- **TypeScript-style `1..120` validation in current code.** When percent
  mode produces 121-240, we need to relax the clamp **only inside the percent
  branch**, not in the existing `duration` branch. Section 3.2 above keeps
  them separate. If a senior accidentally unifies them with
  `1 <= x <= MAX_MANUAL_WATERING_MIN`, the existing manual-minutes path
  would suddenly accept 240 — that's an unrelated behaviour change. **Don't
  do that.** Keep the 120-cap on minutes mode as today.

- **Sequencer signature change.** `job_run_group_sequence` signature is
  consumed by APScheduler via `args=[...]`. Any in-flight jobs persisted in
  `jobs.db` from before deploy will deserialize against the new signature.
  APScheduler binds positionally, so adding a trailing `override_percent=None`
  with a default is safe. Adding it positionally (no default) would break
  in-flight jobs. **Default-None it.**

- **Run-All client-side fan-out semantics.** Today's Run-All sends
  `{override_duration: dur}` to N groups in parallel — same scalar to all.
  Percent mode preserves identical pattern: `{duration_percent: pct}` to N
  groups, each unfolds per-zone server-side. Aggregated warnings are noisy
  but acceptable (toast each group's warnings separately, dedupe client-side).

- **Stale `zonesData` cache vs server `zones.duration`.** Already addressed
  by choosing option (a) — server reads at dispatch time. No risk.

- **Concurrency / sequencer state.** Not relevant — the percent unfolding
  happens before `start_group_sequence` even creates the
  `group_cancel_events[gid]` entry (or inside, but using only zone records,
  not active state). No locks needed.

- **NOT in scope but worth noting** (do not action without explicit ask):
  the "📋 Запустить с настройками зон" button (`confirmRunWithDefaults`)
  is functionally `100%`. With percent row added, this button becomes
  largely redundant for groups (selecting `100%` = same effect). Don't
  remove it now. Mention to product owner.

---

## 7. Migration

**None.** `zones.duration` already plays the role of "norm". Adding a
`base_norm` / `default_duration` column would duplicate state and require
a data backfill. Stay surgical.

If product later wants to track "base norm" separately from "current run
duration" (e.g. ET-driven adjustments persist), that's a separate issue —
introduce a `zones.base_duration` column then, with a one-shot migration
copying current `duration` values. Out of scope for #12.

---

## 8. Summary for senior

- Decision: option (a). One new request field on two existing endpoints.
- New helper: `_per_zone_dur(zone, override_duration, override_percent)`
  in `services/` — pure function, the only place math lives.
- Sequencer: extend `start_group_sequence` and friends with
  `override_percent=None` kwarg. Per-zone dispatch via the helper.
- Client: minute-row stays. Add 6-button percent row + `runPopupMode`
  toggle. Send `duration_percent` instead of `duration` /
  `override_duration` when in pct mode.
- Edge cases enforced server-side, mirrored to client only as toast text
  via response `warnings[]`.
- Tests: 6 (1 unit, 5 API). Plus 1 optional pure-function table test.
- No DB migration. No new endpoint. No new module.

Files touched (estimated):
- `templates/status.html` — +15 lines
- `static/js/status.js` — +40 lines
- `routes/zones_watering_api.py` — +20 lines
- `routes/groups_api.py` — +15 lines
- `irrigation_scheduler.py` — +25 lines (signature + helper call)
- `services/zone_control.py` (or new `services/duration_calc.py`) — +20 lines
- tests — +120 lines

Total: ~250 LoC, 6-7 files.
