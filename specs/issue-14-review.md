# Issue #14 Review — `feat/14-skip-zone-in-group-watering` @ `1cc312c`

Reviewer: Critic
Branch: `feat/14-skip-zone-in-group-watering`
Senior commit: `1cc312c feat(skip-zone): add Skip Current Zone button to active watering card`
Spec: `specs/issue-14-architecture.md`
Tests: 6/6 new pass (~10s).

---

## 1. Verdict: **CHANGES REQUESTED**

The MRO claim is **verified true** — `IrrigationScheduler.__mro__ == (IrrigationScheduler, object)`. Senior was right to patch live code in `irrigation_scheduler.py` instead of the dead mixin. Karpathy #1 (push back when warranted) executed correctly.

Group-sequence path (manual `start-from-first`, `start_group_sequence`) is fully correct. MV optimization, stale-clear, finally cleanup, queue_remaining math, frontend gating — all good.

But the program-watering path (the **other half** of the issue title — "программного полива") **silently 400s**. This is C1 below — the same gap the spec §11.1 explicitly told the senior to wait for #16 to merge before implementing. Senior cherry-picked the helper but not the fix.

Two real issues. C1 ships a half-broken feature; C2 is a concurrency race architect documented in §9 but only partially mitigated.

---

## 2. Blockers

### C1 — Skip endpoint returns 400 for ALL scheduled-program watering

**File:** `irrigation_scheduler.py:1488-1499` (gate), `irrigation_scheduler.py:535-773` (program runner — no `group_cancel_events[gid]` registration).

**Why:**
The skip endpoint is gated on `scheduler.is_group_session_active(gid)` (`routes/groups_api.py:230`):

```python
if not scheduler.is_group_session_active(int(group_id)):
    return jsonify({'success': False, 'message': 'Нет активного полива в группе'}), 400
```

`is_group_session_active` is implemented as "exists key in `group_cancel_events`" (line 1497).

The key is created in **only one place**: `start_group_sequence` at `irrigation_scheduler.py:1173`. That's the *manual* path (button "Запустить полив группы", endpoint `POST /api/groups/<id>/start-from-first`).

`_run_program_threaded` (the function that runs **scheduled programs** at their cron time, `irrigation_scheduler.py:535-773`) does NOT register the event. Verified via inspection: zero assignments to `group_cancel_events[...]` in that method's body — only `.get()` reads. Confirmed by `grep -rn 'group_cancel_events\['` — only `tests/`, `scheduler/program_runner.py` (dead), `system_status_api.py:140` (cancel endpoint), and `irrigation_scheduler.py:1173` (manual path).

**Therefore**: when the user opens the UI during a program-driven session and clicks Skip, the endpoint returns `400 "Нет активного полива в группе"` — even though the zone is actively running. The button **shows** (because frontend gates on `queue_remaining > 0`, which is computed from `scheduled_start_time`, which programs DO set in `scheduler/schedule_calc.py:60`). The patches inside `_run_program_threaded` (lines 718-723, 738-747) are unreachable from the UI in production.

The architect's spec §11.1 EXPLICITLY warned about this:
> "Pre-req: confirm `is_group_session_active` is on this branch (rebase on top of merged #16). If not yet merged, **block until it is**."

Senior implemented the helper locally (line 1488) but did NOT cherry-pick #16's commit `a0b22a3 fix(scheduler): pre-register group_cancel_events for scheduled programs (#16 §6.4)`. Verified: `git merge-base feat/14-skip-zone-in-group-watering fix/16-stop-cancels-queue` = `7c5527c` (preceding commit, no #16 fixes pulled in).

**Result**: half the feature title ("программного полива") is broken at deploy. Tests pass because they all create `sch.group_cancel_events[1] = threading.Event()` manually (lines 74, 142, 205, 254 in test file) — simulating a state the program path never reaches.

**Fix (one of):**
1. Rebase `feat/14` onto merged `#16` (the spec's recommendation). After #16 lands on main, `_run_program_threaded` will pre-register the cancel event via `setdefault` in commits `65a8250` / `286e8f9` / `a0b22a3`.
2. Cherry-pick those three commits onto this branch.
3. As a stop-gap: change `is_group_session_active` to also check "any zone in the group has `state == 'on'`" — but this loses the cancel-in-flight semantics #16 needs, and is a sticking plaster the spec already rejected. **Not recommended.**

**Verification once fixed**: add a test that actually runs `_run_program_threaded` (not `_run_group_sequence`) end-to-end, asserts skip event fires, asserts API returns 200 not 400.

---

### C2 — Server has no debounce; second skip after first consumes wrongly skips next zone

**File:** `routes/groups_api.py:218-259` + sequencer at `irrigation_scheduler.py:1397-1411`.

**Why:**
Architect's spec §9.3 documents this race: when a second skip arrives AFTER the loop has consumed the first event AND advanced to the next zone, the second skip sets the event again on the next zone, which is then immediately skipped on its first poll. The mitigation in the spec was the **frontend 1500ms debounce** (`static/js/status.js:851-878`) plus stale-clear-at-top.

The stale-clear-at-top is correctly implemented (`irrigation_scheduler.py:1281-1283` and `:578-581`). It clears events that arrive in the inter-zone gap (between `stop_zone(prev)` and `zone_on(next)`). Good.

But the racy window is NOT inter-zone gap — it's **zone-on(N+1) just started + sleep loop entered**. The stale-clear at line 1281 fires BEFORE the zone-on call. By the time the inner sleep loop reaches line 1406, the second skip has been set fresh, and the loop consumes it → user-visible "double-skip" on a single intentional click that the user pressed twice because the UI was slow.

The frontend debounce of 1500ms is the only barrier. A few realistic ways to bypass it:
- Two open browser tabs: one tab's `_skipInFlight` doesn't see the other's.
- Mobile + desktop both showing the status card, both fire skip on user double-tap.
- Curl/scripted callers (the senior's test file even simulates this with `sch.request_skip_current_zone(1); sch.request_skip_current_zone(1)` back-to-back — the test only passes because the loop hasn't consumed the first yet).
- Device with slow JS: 1500ms isn't enough if the zone transition takes 2s.

The test `test_skip_double_click_advances_only_once` ONLY tests the case where both calls land within the same zone's sleep window (event idempotently set). It does NOT test the harder case: skip → sleep tick consumes → next zone starts → second skip arrives → next zone is wrongly skipped. **Coverage gap that hides this bug.**

**Fix (~5 lines):**
Add a server-side "last-skip-monotonic-clock" debounce at the API level OR (simpler) attach a per-(gid, zone_id) check in `request_skip_current_zone`:

```python
def request_skip_current_zone(self, group_id: int) -> bool:
    gid = int(group_id)
    if not self.is_group_session_active(gid):
        return False
    # Server-side debounce: ignore skip requests within 2s of last successful skip.
    now = time.monotonic()
    last = getattr(self, '_last_skip_ts', {}).get(gid, 0.0)
    if now - last < 2.0:
        return False  # API: 200 with skipped=False, or 429
    self._last_skip_ts = getattr(self, '_last_skip_ts', {})
    self._last_skip_ts[gid] = now
    ev = self.group_skip_current_events.get(gid)
    if ev is None:
        ev = threading.Event()
        self.group_skip_current_events[gid] = ev
    ev.set()
    return True
```

Then add a test: skip → poll until zone N+1 starts → skip immediately → assert zone N+2 is still 'off' after 1s. (Mirror of `test_skip_double_click_advances_only_once` but with the consume-between race.)

Architect's §9.3 says "frontend debounce ... defense in depth" — but the only depth right now is one layer thick.

---

## 3. Non-blocking nits

### N1 — `_run_program_threaded` has no `finally` block — skip event not popped on program exit

**File:** `irrigation_scheduler.py:535-773`.

`_run_group_sequence` has a `finally:` at line 1461 that pops both `group_cancel_events` and `group_skip_current_events`. `_run_program_threaded` ends at line 772-773 with only `try/except`, no `finally`. If C1 is fixed and a skip event gets set during a program, then:
- Program ends naturally → event stays in `group_skip_current_events[gid]`.
- Next session for same gid (e.g., manual start-from-first 5 minutes later) → stale-clear-at-top (line 1281) catches it and clears. Functionally OK.
- But across program run boundaries: dict accumulates stale entries forever (one per gid that ever got a skip request). Memory leak, slow grow.

When fixing C1, add a `finally:` to `_run_program_threaded` symmetric to the one in `_run_group_sequence`:

```python
finally:
    for gid_seen in {int(z.get('group_id') or 0) for z in (self.db.get_zones() or [])
                     if int(z.get('group_id') or 0)}:
        try:
            sk = self.group_skip_current_events.get(gid_seen)
            if sk:
                sk.clear()
            self.group_skip_current_events.pop(gid_seen, None)
        except (KeyError, TypeError, ValueError):
            pass
```

(Or track `group_id`s touched during the run in a local set, which is cleaner.)

### N2 — `next_zone_id` advisory but no explicit comment

**File:** `routes/groups_api.py:239`.

`_compute_next_zone_id` runs BEFORE `request_skip_current_zone`. If the loop has already moved on between the API capturing state and setting the event, the returned `next_zone_id` is stale. The architect's spec §3.2 explicitly calls this advisory ("best-effort"). The handler doesn't comment that. UI re-renders from `/api/status`, so it's harmless — but a one-line comment would save the next reader 5 minutes.

### N3 — Three audit/log emissions per skip click

For one user click:
1. `@audit_log('zone_skip', ...)` decorator → writes `audit_log` table.
2. `db.add_log('zone_skip', ...)` at `routes/groups_api.py:246` → writes `logs` table.
3. `db.add_log('zone_skip', ...)` inside the loop at `irrigation_scheduler.py:740` (program) or `:1431` (group seq) → writes `logs` table.

Same as `api_stop_group` does (decorator + handler add_log), so partial precedent. But the in-loop emission means `logs` table gets two entries per skip with slightly different payloads (API has `next_zone_id`; loop has `program_id`/`zone_name`). Distinguishable but spammy. Consider dropping the API-level `add_log` since the decorator + loop already cover it.

### N4 — `_compute_next_zone_id` lexical sort assumes ISO-8601 strings everywhere

**File:** `routes/groups_api.py:205`.

`gz.sort(key=lambda z: str(z.get('scheduled_start_time') or ''))`. Works because all writers use `'%Y-%m-%d %H:%M:%S'` format (verified at `irrigation_scheduler.py:1163` and `scheduler/schedule_calc.py:57`). If anyone ever writes a different format, sort silently misbehaves. Not happening today; just brittle.

### N5 — Test coverage gaps (5 missing scenarios)

Tests cover the happy paths and one race. Missing:
- **No test exercises `_run_program_threaded`.** All sequencer tests target `_run_group_sequence`. C1 would have been caught.
- **No test for "skip last zone → session ends".** Spec §8 listed it as test #2 (`test_skip_last_zone_ends_session`). Skipped.
- **No test for "skip during inter-zone pause"** (early_off_seconds gap). Spec §8 5th-test.
- **No test for the C2 race** (skip → loop consumes → second skip wrongly skips N+2).
- **No test for `_compute_next_zone_id` returning `null` on last zone** (validates response contract).

### N6 — `assert 1 in sch.group_skip_current_events` is testing an internal detail

**File:** `tests/integration/test_skip_current_zone.py:273`.

`test_skip_event_cleared_in_finally` reaches into the dict to assert presence. The architect's §8 spec was outcome-based ("zone is off, next zone is on, audit logged"). This test asserts on a private data structure. Refactor cost: trivial. Not actually wrong, just a step backward from the spec's outcome-only philosophy.

### N7 — Stale-skip clear in `_run_program_threaded` clears between zones of *different groups*, but `skipped_this_zone` reset is only at one site

**File:** `irrigation_scheduler.py:582`.

In `_run_program_threaded`, a single program can iterate zones from multiple groups. `skipped_this_zone = False` at line 582 sets the flag PER zone iteration — correct. But the skip event itself is per-group. Trace:
- Zone A1 (group A) → user presses skip on group A → event set → consumed inside zone A1's sleep loop → `continue`.
- Next zone is A2 (group A) → top-of-loop, stale-clear on group A (event already clear) → A2 starts.
- OK so far.
- But: if next zone is B1 (group B), the stale-clear at line 579 only checks `group_id` of B1 (group B). User's stale skip for group A would NOT be cleared at B1's iteration. Then if loop returns to a group A zone later (same program iterates A1, B1, A2 — possible if sort-by-zid mixes groups), the stale skip persists into A2.

This is contrived (programs usually don't interleave groups), and `_compute_next_zone_id` only handles single-group sequences. But it's a real gap if it occurs. Mitigation: clear ALL stale skip events at the very top of the for-loop, not just for the current zone's group.

Low priority — likely 0 production impact, but flag.

---

## 4. Verified-OK list (what I actively checked)

- **MRO claim**: ran `python -c "from irrigation_scheduler import IrrigationScheduler; print(IrrigationScheduler.__mro__)"` → `(IrrigationScheduler, object)`. Confirmed. Senior was right. (`irrigation_scheduler.py:222`)
- **Mixin truly dead**: `grep -rn 'ProgramRunnerMixin' --include='*.py'` shows no mixing-in. Senior's deviation is correct.
- **Skip event polling is between cancel-event and shutdown-event**: at lines 715-723 (program) and 1397-1414 (group seq). Order is fine: cancel wins over skip wins over shutdown — matches architect's intent.
- **Stale-clear-at-top runs before zone start**: lines 578-581 (program) and 1281-1283 (group seq). Both before the zone-on call → catches the inter-zone-gap race (§9.1).
- **Skip path uses `stop_zone(reason='auto'/'group_sequence')` without `master_close_immediately=True`**: lines 731 (program: `_stop_central(zone_id, reason='auto', force=False)`) and 1419 (group seq: `_stop_zone_central(zone_id, reason='group_sequence')`). No bouncing of MV between adjacent same-group zones — inherits existing delayed-close behavior. (`services/zone_control.py:400`)
- **`_run_group_sequence` finally pops both events**: line 1461-1477. Correct.
- **`request_skip_current_zone` is idempotent**: line 1501-1518. `ev.set()` on already-set Event is a no-op. Returns True regardless. Correct.
- **API endpoint correctly checks both group existence (404) and session active (400)**: lines 222-231. Correct order (404 before 400). Body shape matches spec §3.2.
- **`queue_remaining` arithmetic is correct in all 3 cases (first zone of N, mid zone, last zone)**: traced manually with [10:00, 10:05, 10:10] schedule. Last zone → 0 → button hidden. ✓ (`routes/system_status_api.py:461-480`)
- **Frontend button gated on `queue_remaining > 0` AND `anyZoneOnThisGroup`**: in all 3 render paths (`status-groups.js:57-59`, `status.js:447-449`, `status.js:602-604`). Single-zone group: `queue_remaining = 0` → button hidden. ✓
- **Tests run green**: `./venv/bin/python -m pytest tests/integration/test_skip_current_zone.py` → 6 passed in 10.5s.
- **`scheduled_start_time` populated for both manual and scheduled paths**: `start_group_sequence` calls `set_group_scheduled_starts` (line 1167); `schedule_program`/`_schedule_single_time` call `update_zone(zid, {'scheduled_start_time': ts})` (`scheduler/schedule_calc.py:60`). So `queue_remaining` works for both.
- **Frontend 1500ms debounce in `_skipInFlight` Set**: `static/js/status.js:851-878`. Per-group key. Released after timeout. Correct for single-tab scenarios; insufficient for multi-tab/scripted (see C2).
- **CSS `.group-action-skip` rendered blue, not red**: `status.css:243` — `background: #1976d2`. Distinct from destructive `.group-action-stop` (red). Matches issue wording "не «опасная»".
- **Tests pass in ~10s — outcome-based for the most part**: assertions check zone state via `db.get_zone(...).state`, not status codes alone. Sequencer tests poll real DB state. Good. One minor regression: `test_skip_event_cleared_in_finally` asserts on dict membership (N6).
- **Audit log decorator `@audit_log('zone_skip', ...)` is registered on the route**: `routes/groups_api.py:219`. Distinct from `group_stop` and `zone_stop`. Captures actor, IP, payload, status. ✓

---

## 5. Recommendation

**CHANGES REQUESTED — fix C1 then re-review.**

C1 alone makes the feature half-broken at deploy: scheduled programs are the more common watering path (most users don't manually start groups). Shipping this branch will produce confused user reports of "Skip button doesn't work, says no active session, but it's clearly running". C2 is real but a second-order race; can be a follow-up if C1 fix takes a separate PR.

If senior's plan is to merge #16 first and rebase #14, that's the right path and the spec already laid it out. Just don't merge `feat/14` to main standalone.

---

## Iter 2 re-review (2026-05-10)

Reviewer: Critic
Iter 2 commit: `5cb3999 fix #14 iter2: register skip-eligible events in program runner + server debounce` on top of `1cc312c`.
Scope: ONLY iter 2 fixes (C1 + C2). Iter 1 nits N1–N7 not relitigated.

### Verdict: **APPROVE**

Both blockers fixed at the root, not papered over. New tests prove outcomes (zone state transitions, HTTP status codes), not just internal flags. 8/8 tests pass in 13s; parallel suite reportedly 1735 passed. Implementation matches the cherry-pick pattern from #16 closely enough that the merge conflict will be trivial (single block, identical setdefault semantics).

### C1 fix verification: **PASS**

Senior fixed the root cause exactly as spec §11.1 required. `_run_program_threaded` now pre-registers `group_cancel_events[gid]` for every distinct group the program will touch (`irrigation_scheduler.py:556-575`), using the **atomic `dict.setdefault`** primitive (line 571: `self.group_cancel_events.setdefault(gid, new_ev)`) — not a racy `if gid not in dict: dict[gid] = ev` check. Identity-tracked cleanup is in place: `registered_gids` records only the gids THIS invocation owned (line 572: `if existing is new_ev: registered_gids.append(gid)`), and the new `finally` block at lines 812-832 pops only those (`for gid in registered_gids:`). A concurrent `start_group_sequence` running on a shared gid will keep its own Event untouched. N1 (the missing finally-pop of skip events) is also resolved here — the same finally clears `group_skip_current_events` for owned gids, killing the slow memory leak. The new test `test_skip_current_program_watering_works` ACTUALLY drives `_run_program_threaded` in a background thread (not `_run_group_sequence`), waits for zone1 to start via real DB polling, asserts `is_group_session_active(1)` is True (would fail without the fix), then POSTs the real `/api/groups/1/skip-current` endpoint and asserts 200 + `body['skipped_zone_id'] == z1['id']` + zone1 stops + zone2 starts within 5s. This is genuine end-to-end outcome coverage; if anyone reverts the setdefault block, the test breaks at the assertion AND at the POST returning 400.

### C2 fix verification: **PASS**

Server-side debounce uses `time.monotonic()` (line 1582) — correct choice over `time.time()`, immune to wall-clock jumps from NTP/manual time changes. Per-group state stored in `self._last_skip_ts: Dict[int, float]` initialized in `__init__` (line 314). 1.0s window via instance attribute `self._skip_debounce_seconds` (line 316), so tests can shorten it for fast assertion (used at line 372 of the new unit test). Crucially, the timestamp is updated **only on successful skips** (line 1586, after the debounce check passes) — a 429 does NOT extend the window, so a legitimate retry 1.01s after the original click goes through. Status string contract is clean (`'ok' | 'no_session' | 'debounced'`); endpoint translates correctly: 429 for `'debounced'` (`routes/groups_api.py:242-250`), 400 for `'no_session'` (`:251-252`), 200 for `'ok'` (continues). The rewritten `test_skip_double_click_advances_only_once` sends two real HTTP POSTs via Flask test client, asserts `resp1.status_code == 200` and `resp2.status_code == 429`, then verifies zone3 stays `off` after 0.5s — proving the debounce **rejected** the second request (not merely absorbed it via Event idempotency). The unit test `test_skip_debounce_unit_returns_status_strings` locks down all four status transitions including the post-window reset (`'ok'` again after 0.06s with `_skip_debounce_seconds=0.05`).

### New regressions

None observed.

- `request_skip_current_zone` return type changed `bool` → `str`, but the only non-test caller (`routes/groups_api.py:241`) was updated in the same commit; greppable confirmation: zero remaining `if scheduled:` / `if not scheduled:` callsites against this method.
- `_run_program_threaded` weather-skip early-return (line 594) now correctly hits the new finally and pops the registered cancel event — no orphaned dict entries even on weather-skipped programs.
- `_last_skip_ts` dict grows by one entry per group ever skipped (bounded by group count); not cleaned across program runs by design — that's correct, otherwise debounce would reset every run boundary and a click straddling the run boundary could double-skip.
- Existing test `test_skip_advances_to_next_zone` updated to use the new string contract (line 165: `== 'ok'`); other call sites in `test_skip_event_cleared_in_finally` ignore the return value, unaffected.

### Final note

Ready to merge once #16 lands; the `setdefault` block in `_run_program_threaded` will conflict trivially with #16's identical block (keep one copy, delete the duplicate comment about overlap).

