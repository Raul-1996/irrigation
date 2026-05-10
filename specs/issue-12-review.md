# Issue #12 Review — `feat/12-duration-percent-of-norm` @ `1bc5f95`

Reviewer: Critic
Branch: `feat/12-duration-percent-of-norm`
Senior commit: `1bc5f95 feat(watering): %-of-norm duration selector (50/75/100/125/150/200%)`
Spec: `specs/issue-12-architecture.md`
Tests run: 6/6 new tests pass (~6s).

---

## 1. Verdict: **CHANGES REQUESTED**

No production-blocking correctness bugs in the happy paths. Math, clipping, fallback, and back-compat are all correct. But there are **two real defects** the reviewer expected the senior to catch:

- **C1** — group endpoint contract is inconsistent with the single-zone endpoint (`warnings[]` missing from one of the two responses). Spec said both should carry it.
- **C2** — "minutes wins if both sent" invariant from spec §4 is silently violated when minutes value is out-of-range (1..120). Implementation falls through to percent in that case.

Both are 2–6 line fixes. Everything else is acceptable nits / future cleanup.

---

## 2. Blockers

### C1 — `warnings[]` missing from `/api/groups/<id>/start-from-first` response

**File:** `routes/groups_api.py:240`
**Why:** Senior added `warnings[]` to the single-zone response (`routes/zones_watering_api.py:357, 358, 454`) but the group endpoint returns:

```python
return jsonify({"success": True, "message": f"Группа {group_id}: запущен последовательный полив"})
```

No `warnings` key. So:

- Inconsistent API contract between the two endpoints that share the same `duration_percent` input. Frontend can't blindly read `data.warnings` — has to know which endpoint it called.
- `static/js/status.js:2346` — group success path never reads `data.warnings`. If a group has zones with `duration<=0` and user picks 100%, the user gets **silent fallback to 15 min** with no UI feedback. Same for `clipped_max` (e.g. `200% × 200` after a corrupt write).
- Architecture spec §3.2 explicitly said: "Response includes `warnings: [...]`". Senior partially implemented (added `warnings` local in groups_api but never returned it — actually, no, it was never even computed there).

**Fix (~10 lines):**

Either:
(a) Compute warnings in `/api/groups/<id>/start-from-first` by calling `per_zone_dur(zone, override_dur, override_pct)` once per group zone, dedupe, and return. This is what the spec implies.
(b) At minimum, **always** return `'warnings': []` so the frontend can rely on a single response shape and the contract stays uniform across endpoints.

Recommend (a) — it's ~5 lines: loop the group's zones, collect warning tags into a `set`, return `sorted(list(...))`. Then add the same warning-toast handler to `confirmRun()` group branch (status.js:2346) that already exists for single-zone (status.js:2382).

---

### C2 — "minutes wins if both sent" violated when minutes is out-of-range

**File:** `routes/zones_watering_api.py:286-319`
**Why:** Spec §4 table:
> Both `duration` and `duration_percent` sent → `duration` wins (minutes mode is explicit, simpler invariant).

Implementation:

```python
override_dur = None
req_d = body.get('duration')
if req_d is not None:
    req_d = int(req_d)
    if 1 <= req_d <= 120:        # <-- gate
        override_dur = req_d

# percent branch only fires if override_dur is None
req_pct = body.get('duration_percent')
if override_dur is None and req_pct is not None:
    ...                           # percent path takes over
```

POST `{duration: 200, duration_percent: 100}`:
- `req_d = 200`, fails `<=120`, so `override_dur` stays `None`.
- Percent block fires. User watered with 100% of zone norm, despite explicitly sending `duration: 200`.

Spec invariant is "minutes wins if **sent**", not "minutes wins if **valid**". Either:

**Fix (a, recommended):** if `body.get('duration') is not None`, skip the percent branch entirely (and let the existing 1..120 clamp drop into the fallback "no override" branch). 1-line change at `zones_watering_api.py:307`:

```python
if override_dur is None and req_pct is not None and body.get('duration') is None:
```

**Fix (b):** explicitly return 400 when `duration` is out-of-range, instead of silently dropping. Larger behavioural change, matches the user's stated intent.

The same hole exists in `routes/groups_api.py:209-230` for `override_duration` — same fix (skip percent block if `body.get('override_duration') is not None`).

This is exactly the kind of subtle misbehaviour the spec was warning about ("Minutes mode (duration) wins if both are sent"). Caller cannot ever expect that an invalid minutes value falls through to percent — that's a foot-gun.

---

## 3. Non-blocking nits

### N1 — `clipped_min` warning is unreachable code

**File:** `services/zone_control.py:61-63`

```python
if computed < 1:
    computed = 1
    warnings.append('clipped_min')
```

With `PERCENT_PRESETS = (50, 75, 100, 125, 150, 200)` (smallest = 50) and the fallback `base = 15` when zone duration ≤ 0, the smallest `ceil(base * pct / 100)` is `ceil(1 * 50 / 100) = ceil(0.5) = 1`. Math.ceil never returns < 1 for positive operands. The branch is dead.

The frontend has a string for it (`status.js:2386`: "округлено до 1 мин"). Both can be removed, or kept as defensive scaffolding for future presets `<50%`. Either way: flag.

### N2 — No test for the helper directly (spec §5 test #7 was optional)

**File:** `tests/unit/` — no `test_duration_calc.py`.

The senior skipped the optional pure-function table test of `per_zone_dur`. Without it, edge cases are only exercised indirectly through API. A 15-line table-driven test would:
- Lock the math (`ceil`, `clip`, fallback) against future drift.
- Cover the dead `clipped_min` branch (helper called with `pct=0` direct).
- Catch float `pct=50.5` if someone ever tightens validation.

Skipped tests cost more in 6 months than they save now. Spec called it optional but recommended.

### N3 — Group-mode optimistic-UI clamp is 240, but server cap is `MAX_MANUAL_WATERING_MIN`

**File:** `static/js/status.js:2331, 2366`

```js
pdur = Math.max(1, Math.min(240, Math.ceil(base * pct / 100)));
```

Hard-coded `240` mirrors `constants.MAX_MANUAL_WATERING_MIN`. If product later raises the constant, the JS optimistic preview will silently desync from the server. Document or extract to a JS constant. Cosmetic.

### N4 — Invalid-percent silent fallback gives no UI feedback

**Files:** `routes/zones_watering_api.py:319` / `routes/groups_api.py:230`

POST `{duration_percent: 87}` (or `"abc"`, `-50`, `9999`) → 200 OK, watered at base norm. No error, no warning. Spec §4 says "no error response — defensive". OK as-spec, but the FE button row sends only 6 valid values, so any non-whitelisted value coming in is by definition a payload-pollution attempt or a bug. Worth a `'invalid_percent_ignored'` warning tag at minimum, or a 400. Not a blocker per spec.

### N5 — `scheduler/jobs.py:48` has stale `job_run_group_sequence` signature

**File:** `scheduler/jobs.py:48`

```python
def job_run_group_sequence(group_id: int, zone_ids: list, override_duration: int = None):
```

Three args, no `override_percent`. This module-level function is not the one bound by the active scheduler (`irrigation_scheduler.py:111` is). It's only imported by `scheduler/program_runner.py:342`, which appears unused (`ProgramRunnerMixin` is never mixed into anything). Dead code path → not a #12 issue, but if some refactor ever activates `program_runner`, the percent feature will silently disappear for that codepath. Out of scope to fix. Flag for cleanup.

### N6 — Test #1's tolerance of `±5 sec` is generous

**File:** `tests/api/test_zones_api_comprehensive.py:169, 191, 219`

`abs((end_dt - expected).total_seconds()) < 5` — fine for CI noise. Note: the underlying `planned_end_time` is computed from `datetime.now()` inside the request handler, with seconds-truncation at strftime, so the actual skew is bounded by 1–2 sec. Tolerance could be 2 sec; not worth changing.

### N7 — Senior deviation re: helper visibility (`per_zone_dur` vs `_per_zone_dur`)

Senior already disclosed this and the rationale (cross-module import). Acceptable. Architect's `_` prefix was a Python convention nudge, not a hard rule. Move on.

### N8 — `MAX_MANUAL_WATERING_MIN` clip vs architect's "120 cap" suggestion

**File:** `services/zone_control.py:64`

Architect's spec was internally inconsistent: §1.5 said "clip percent results at 120", §4 table and §5 test #3 said "240 (`MAX_MANUAL_WATERING_MIN`)". Senior followed §4/§5 (240). Tests confirm 240 is the design intent. No action needed; flag for the architect — clean up §1.5 in any future spec.

---

## 4. Verified-OK list

- **Math (`ceil`, fallback, clip)** — `services/zone_control.py:60-67`. Verified table-driven by hand: `(10, 50)→5`, `(10, 75)→8`, `(0, 100)→15+norm_not_set`, `(120, 200)→240`, `(200, 200)→240+clipped_max`, `(1, 50)→1`. All correct.
- **120-cap on minutes mode UNTOUCHED** — `routes/zones_watering_api.py:293`, `routes/groups_api.py:213`. Both still `1 <= x <= 120`. Percent path is the only producer of 121..240.
- **Sequencer threading** — `irrigation_scheduler.py:1114, 1157, 1196-1197, 1201, 1227, 1241, 1288`. `override_percent` is forwarded through every layer: `start_group_sequence → cumulative planner → job_run_group_sequence → _run_group_sequence` (incl. TESTING short-circuit). Helper called in BOTH the planner (cumulative time math) AND the per-zone runner (actual run length). Per-zone math is real, not uniform — confirmed by test_start_group_percent_per_zone (zone[1] starts at T0+15, not T0+45).
- **APScheduler back-compat** — `irrigation_scheduler.py:111-112`. New kwarg `override_percent: int = None` is trailing with default. In-flight jobs from prior deploy (3 args) deserialize cleanly.
- **TESTING-mode short-circuit** — `irrigation_scheduler.py:1234-1260`. Calls `per_zone_dur` correctly. Test `test_start_group_percent_per_zone` proves the percent calc fires in TESTING (z1 ends at T0+15, not T0+10).
- **Already-on reschedule branch** — `routes/zones_watering_api.py:326-358`. Uses `override_dur` computed by the percent block above, returns `warnings`. Correct.
- **Frontend mode toggle** — `static/js/status.js:2156-2174, 2229-2231, 2272-2283`. `runPopupMode` flips correctly: minute click sets `'min'`, percent click sets `'pct'`, dial drag forces `'min'`. CSS `.mode-pct` dim + `.active` highlight on selected pct button only.
- **Mode reset on popup open** — `static/js/status.js:2249-2250` (single zone), `2037-2038` (group). State doesn't leak across reopens.
- **Request body shape** — `static/js/status.js:2299-2300`. Group/all-groups → `{override_duration | duration_percent}`; single zone → `{duration | duration_percent}`. Matches both endpoints.
- **Toast for warnings (single zone path)** — `status.js:2382-2390`. All three warning tags translated. `norm_not_set`, `clipped_max`, `clipped_min`. (Note: group path doesn't even call out to warnings — see C1.)
- **Backwards compatibility — minutes paths untouched** — verified by reading `routes/zones_watering_api.py:289-299` and `routes/groups_api.py:209-216`. `duration_percent` only fires when `override_dur is None` (modulo the C2 hole).
- **Sequencer signature back-compat** — `tests/unit/test_scheduler_comprehensive.py:229-241` proves `start_group_sequence(gid)` (no kwargs) still works, returns True, marks first zone ON. Helper falls into "neither override" branch and returns `zone['duration']`. Bytewise-equivalent to pre-#12.
- **Test outcome quality** — All 5 API tests assert effective behaviour (`planned_end_time`, `state`, `scheduled_start_time`), not just status codes. The unit test asserts state, also outcome-based. 6/6 are outcome tests, not status-code-only. Good.
- **Input safety** — `int(req_pct)` wrapped in `try/except (ValueError, TypeError)` in both endpoints. NaN, "abc", null → caught, fall back to None. Float `50.5` → `int(50.5)=50` accepted (in whitelist) — minor surface but harmless. No path traversal / SQLi risk: percent is integer-converted before any use, never concatenated.
- **PERCENT_PRESETS whitelist** — `services/zone_control.py:23` is `(50, 75, 100, 125, 150, 200)`. Both endpoints check `p in PERCENT_PRESETS`. Outside-whitelist int → `override_pct = None` → defaults. Defensive as designed.
- **Test count** — 6 new tests (5 API + 1 unit), matches spec target. Senior skipped only the optional 7th (helper-direct table test) — see N2.
- **Full suite** — senior reports 1733 passed, 0 failed, 87s parallel. Re-ran the 6 new tests in isolation: all pass in ~6s.

---

## 5. Summary

Two non-cosmetic gaps to close before merge:

1. **C1 (~10 lines):** add `warnings[]` to group endpoint response (or at minimum return `[]`), then surface them in `confirmRun()`'s group branch.
2. **C2 (~2 lines):** when `duration` (or `override_duration`) is **present in body** — even if invalid — do not silently fall through to percent. Spec invariant is "minutes wins if sent".

Everything else is acceptable. The math is right, back-compat is clean, tests are outcome-based, the helper is reused (no duplication), and the senior's deviations from spec are minor and disclosed. After C1+C2 are fixed, this is a clean ship.

---

## Iter 2 re-review (2026-05-10)

Reviewer: Critic
Senior commit: `148543f fix(watering #12): enforce minutes-wins + propagate warnings on group endpoint`
Diff vs iter 1: 5 files, +180 / −39.

### Verdict: **APPROVE**

### C1 fix verification: **PASS**

Senior chose option (a) from the original review — pre-compute warnings in the group route by calling the **same** `services.zone_control.per_zone_dur` helper the sequencer uses (`routes/groups_api.py:247-252`). No parallel implementation, no drift risk: any future change to the helper's clipping/fallback rules automatically reaches both the run-time path and the response. Response shape is uniform — `warnings: list = []` is initialised before the percent branch, so the key is **always** present in the success body, even for empty groups, all-OK groups, or minutes mode (skips the loop entirely). Edge cases hold: empty group → loop doesn't execute → `[]`; all zones with valid norms → `wset` empty → `[]`; minutes mode → `override_pct is None` → loop skipped → `[]`. Frontend (`status.js:2351`) reads `data.warnings && data.warnings.length` — defensive, no JS crash on legacy/old API responses where `warnings` is `undefined`. The dedupe via `set` then `sorted()` gives deterministic output (no flaky tests). Outer try/except catches helper exceptions and falls back to `warnings = []` instead of failing the whole start. New test `test_start_group_percent_warnings_propagated` asserts `'norm_not_set' in warns` (outcome) — not just "warnings is a list".

### C2 fix verification: **PASS**

Senior chose option (b) — explicit 400 rejection rather than a silent skip — and applied it consistently in **both** routes (`zones_watering_api.py:289-304` and `groups_api.py:213-224`). The "minutes sent" gate uses `is not None`, which is the strict reading of the spec invariant: only `duration: null` (or omitted) routes to percent; `0`, `"abc"`, `200`, `-1` all 400 with a meaningful message. Side-effect-free: a rejected request never touches the scheduler (verified by the new test asserting zone state stayed `off`). No regression on existing callers — the only test sending `override_duration` to start-from-first uses `24` and `30` (both valid 1..120). The frontend toggle path (`status.js:1970`) sends an empty body, so `body.get('duration') is None` and the percent branch correctly applies its own logic (no `duration` field present). The new `test_minutes_null_percent_honored` covers the subtle `duration: null + duration_percent: 100` path that the strict gate must NOT block — it correctly passes through to percent. The `test_minutes_wins_over_percent_when_both_valid` test verifies the precedence (30 min, not norm × 100% = 12 min). All three branches of the truth-table are now outcome-tested.

### New regressions (if any)

None observed. Behaviour change is strictly tighter than before (4xx where it used to be silent fallback), and the spec mandates that direction. `duration: 0` now 400s instead of silently dropping to base — no caller sends `0` (verified by grep over tests/static/js). The helper-via-route call adds one DB read per group start (`db.get_zones()`) and a small loop — negligible cost on a manual control endpoint that already involves MQTT publishes and DB writes downstream.

### Final note

Both blockers fixed at the root, not worked around; warnings now share a single source of truth with the run-time path; strict rejection is consistent across both endpoints; new tests are outcome-based. Clean ship.
