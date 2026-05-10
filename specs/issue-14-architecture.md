# Issue #14 — "Пропустить зону" во время группового / программного полива

Branch: `feat/14-skip-zone-in-group-watering`
Author: Backend Architect
Approach: **Option A** — per-group `skip_current_event: threading.Event`,
in-thread loop checks it after each `zone-on`, short-circuits the inner sleep,
stops current zone, **continues** (not breaks) to the next iteration.

---

## 1. Audit findings

### 1.1 Sequencer loops (in-thread, no persistent queue)

Two near-identical loops drive multi-zone watering. Both live in
`scheduler/program_runner.py` (mixin on `IrrigationScheduler`):

- **Group sequence loop** — `scheduler/program_runner.py:433-631`
  (`_run_group_sequence`). Iterates `zone_ids` synchronously inside an APScheduler
  worker thread (`group_seq:<gid>:<ts>` job), runs `_uzs(state='on')` →
  MQTT publish ON → 1-sec-tick wait loop (lines 569-593) → centralized stop →
  optional "early-off" make-up wait (lines 606-607).
- **Program runner loop** — `scheduler/program_runner.py:88-291`
  (`_run_program_threaded`). Same shape but per-zone the `group_id` can vary
  (program may span multiple groups). Inner wait loop at lines 250-258.

The `irrigation_scheduler.py` versions at `:540-745` and `:1203-1418` are the
**legacy in-class copies** that the mixin overrides — they're functionally
identical. Both must be updated by this change OR confirmed unreachable.
(Quick check: `IrrigationScheduler` declares `class IrrigationScheduler(ProgramRunnerMixin, …)`,
so the mixin wins by MRO. The in-class definitions are dead — but I will leave
them alone per Karpathy #3, only touching the mixin.)

### 1.2 Cancel-event mechanism

- Storage: `IrrigationScheduler.group_cancel_events: Dict[int, threading.Event]`
  declared at `irrigation_scheduler.py:307`.
- Populated by `start_group_sequence` at `program_runner.py:378-379`
  (manual / multi-start) and by `cancel_group_jobs` at `program_runner.py:297-298`
  (which **sets** an existing event but does NOT create one).
  `_run_program_threaded` only **reads** — it does not pre-register an event for
  scheduled programs (issue #16 §6.4 latent gap, not yet fixed on this branch).
- Cleared in the `finally` block at `program_runner.py:625-630` (clear + pop).
- The 1-sec-tick wait loops poll `cancel_event.is_set()` per iteration
  (`program_runner.py:251-254` and `:582-589`).

### 1.3 Stop endpoints

- Zone stop: `routes/zones_watering_api.py:101-127`. Calls
  `services.zone_control.stop_zone(zone_id, reason='manual', force=False)`.
  Does **not** touch `group_cancel_events`.
- Group stop: `routes/groups_api.py:146-191`. Calls
  `stop_all_in_group` then `scheduler.cancel_group_jobs(gid)` (which sets the
  cancel event). After issue #16 lands, this remains the canonical "kill the
  whole session" path.

### 1.4 `is_group_session_active` — branch dependency

`is_group_session_active(gid)` is referenced in your prompt as "from #16 fix"
but is **not yet on this branch**. It exists on `fix/16-stop-cancels-queue` as
commit `b6e5400` (`feat(scheduler): add is_group_session_active helper`).

Two options:
1. **Wait for #16 to merge to main, rebase this branch, then implement #14.**
   Cleanest. Recommended.
2. Implement #14 first, inline the helper as a private. When #16 merges, swap.

Spec assumes (1). All API code paths below call
`scheduler.is_group_session_active(int(gid))`.

### 1.5 `services/program_queue.py` — DEAD CODE

Confirmed: production callers ZERO. Only test files import it:

```
tests/unit/test_program_queue.py
tests/unit/test_program_queue_concurrency.py
tests/unit/test_completion_tracker.py
```

`grep -r "from services.program_queue" --include='*.py' | grep -v tests/` is
empty. No spec-execute path uses `ProgramQueueManager`. Issue #14 must NOT
build on it.

### 1.6 Master-valve close path

`services/zone_control.py:516-525`: `_schedule_master_close(g, immediate=False)`.
Uses per-group `master_close_delay_sec` (cancellable timer). When the next zone
in the SAME group starts within that delay, the timer is **cancelled** by the
next start path (`exclusive_start_zone` / pre-open MV branch in the runner at
`program_runner.py:516-547`). So MV will not bounce closed-open between
adjacent zones provided we don't introduce a stop with `master_close_immediately=True`.
**Skip path inherits this for free** — see §5.

---

## 2. Design — Option A (skip_current_event)

### 2.1 New per-group event

Add a sibling dict to `group_cancel_events`:

```python
# irrigation_scheduler.py:307 (next to group_cancel_events)
self.group_skip_current_events: Dict[int, threading.Event] = {}
```

Populated when (and only when) `group_cancel_events[gid]` is. Cleared in the
same `finally` block. Reusing the lifetime keeps the invariants identical.

### 2.2 New scheduler method

```python
def request_skip_current_zone(self, group_id: int) -> bool:
    """
    Mark the currently running zone in this group's sequencer as 'skip me'.
    The in-thread loop will turn the current zone OFF and proceed to the next
    iteration. No effect if no session is active for this group.
    Returns True if a skip was scheduled, False otherwise.
    """
    gid = int(group_id)
    if not self.is_group_session_active(gid):
        return False
    ev = self.group_skip_current_events.get(gid)
    if ev is None:
        ev = threading.Event()
        self.group_skip_current_events[gid] = ev
    ev.set()
    return True
```

`is_group_session_active` (from #16) is the gate — same gate that protects the
stop endpoint. A skip on a non-active group returns False → API returns 400.

### 2.3 In-thread loop changes (per loop)

Change in **two** places — `_run_group_sequence` (`program_runner.py:582-593`)
and `_run_program_threaded` (`program_runner.py:250-258`).

Inside the per-iteration sleep loop, also poll the skip event:

```python
# Inside the `while remaining > 0:` loop, alongside the cancel/shutdown checks
skip_event = self.group_skip_current_events.get(group_id)
if skip_event and skip_event.is_set():
    skip_event.clear()                # one-shot per zone
    skipped_this_zone = True
    logger.info(f"Группа {group_id}: skip current zone {zone_id}")
    break
```

Then after the centralized stop call:

```python
if skipped_this_zone:
    try:
        self.db.add_log('zone_skip', json.dumps({
            'group_id': group_id, 'zone_id': zone_id,
            'zone_name': zone.get('name'),
            'reason': 'manual_skip',
        }))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    # KEY: do NOT break and do NOT honour the post-zone "early" make-up wait.
    # Move on to the next iteration immediately.
    skipped_this_zone = False
    continue
```

The `continue` jumps to the top of the `for` loop where the next zone is
picked up. The cancel-event check (top of loop) still runs, so a Stop pressed
between skip-arrival and next-iteration kills the session cleanly.

`skipped_this_zone` is a local in the runner method — fresh per loop entry,
fresh per zone iteration. No cross-zone bleed.

### 2.4 Why Option A, not Option B (cancel + restart with offset)

Option B would `cancel_group_jobs(gid)` and re-call `start_group_sequence` with
the remaining-zones tail. Three problems:

1. Issue #16 fix wires Stop → `cancel_group_jobs` → "kill session". The user
   sees "session ended". Skip would race the new session against status SSE,
   any audit trail would show `group_stop` then a fresh `group_seq_zone_start`
   — confusing in history and broken for "this is one session".
2. `cancel_group_jobs` removes scheduler jobs by id prefix, sets the cancel
   event, calls `stop_all_in_group(force=True)` — which **immediately**
   triggers MV close path (no `master_close_immediately`, but the timer fires
   normally). Then start_group_sequence opens MV again. Bouncing.
3. `cancel_group_jobs` also calls `db.reschedule_group_to_next_program` —
   that's a side effect we definitely don't want for a mid-session skip.

Option A keeps a single in-thread iteration alive, reuses the existing MV
optimization, and never enters the cancel path.

---

## 3. API

### 3.1 Endpoint choice

`POST /api/groups/<int:group_id>/skip-current` — most consistent with:
- `/api/groups/<gid>/stop` (existing, group-scoped)
- `/api/groups/<gid>/start-from-first` (existing, group-scoped)

**Not** zone-scoped (`/api/zones/<zid>/skip`) because:
- The "current zone" is implicit in group state. Zone-scoped invites races
  ("client thinks zone 5 is current, server has already moved to zone 6, skip
  arrives, do we skip 5 — already done — or 6?"). Group-scoped + server-side
  "what's running now" eliminates that.

**Not** session-scoped (`/api/run/skip`) because there is no session id object
in this codebase — `group_cancel_events` is keyed by `group_id`, status is
keyed by `group_id`. Inventing a session id just for this endpoint is
abstraction we don't need (Karpathy #2).

### 3.2 Contract

```
POST /api/groups/<int:group_id>/skip-current
Content-Type: application/json
Body: {} (empty allowed)

200: { "success": true,  "skipped_zone_id": <int>,
                          "next_zone_id": <int|null> }
400: { "success": false, "message": "Нет активного полива в группе" }
404: { "success": false, "message": "Группа не найдена" }
500: { "success": false, "message": "Ошибка пропуска зоны" }
```

`next_zone_id` is **best-effort, advisory** — derived from
`db.get_group_scheduled_starts(group_id)` (already populated by
`start_group_sequence`) by picking the next entry after the current one. If
the loop has already moved on, `null` is acceptable; the client will
re-render from `/api/status` regardless.

If the current zone is the **last** in the queue, `next_zone_id` is `null` and
the loop will exit naturally after the skip-induced stop. The response is
still 200 — skip was honored — but the UI will hide the skip button on the
next status poll because there's no longer an active watering.

### 3.3 Handler skeleton

```python
# routes/groups_api.py  (next to api_stop_group at line 146)

@groups_api_bp.route('/api/groups/<int:group_id>/skip-current', methods=['POST'])
@audit_log('zone_skip', target_extractor=lambda *a, **kw: f"group:{kw.get('group_id', a[0] if a else '?')}")
def api_skip_current_zone(group_id):
    """Skip the currently running zone in the group's sequence; next zone starts now."""
    try:
        group = next((g for g in (db.get_groups() or []) if int(g['id']) == int(group_id)), None)
        if not group:
            return jsonify({'success': False, 'message': 'Группа не найдена'}), 404

        scheduler = get_scheduler()
        if not scheduler:
            return jsonify({'success': False, 'message': 'Планировщик недоступен'}), 500
        if not scheduler.is_group_session_active(int(group_id)):
            return jsonify({'success': False, 'message': 'Нет активного полива в группе'}), 400

        # Capture "current" + "next" from authoritative state BEFORE setting the event.
        zones = db.get_zones()
        active = [z for z in zones if int(z.get('group_id') or 0) == int(group_id) and z.get('state') == 'on']
        if not active:
            return jsonify({'success': False, 'message': 'Нет активной зоны для пропуска'}), 400
        current_zone_id = int(active[0]['id'])

        next_zone_id = _compute_next_zone_id(int(group_id), current_zone_id)

        scheduled = scheduler.request_skip_current_zone(int(group_id))
        if not scheduled:
            return jsonify({'success': False, 'message': 'Нет активного полива в группе'}), 400

        db.add_log('zone_skip', json.dumps({
            'group_id': int(group_id), 'zone_id': current_zone_id,
            'next_zone_id': next_zone_id, 'source': 'manual',
        }))
        return jsonify({
            'success': True,
            'skipped_zone_id': current_zone_id,
            'next_zone_id': next_zone_id,
        })
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка пропуска зоны в группе {group_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка пропуска зоны'}), 500
```

`_compute_next_zone_id` — read `db.get_group_scheduled_starts(group_id)` (a
`Dict[zone_id, planned_start_str]`) ordered by planned_start; return the entry
after `current_zone_id` (or `None`). This mirrors the `schedule_map` written
in `start_group_sequence` at `program_runner.py:367-374`.

### 3.4 Validation

| Condition                                            | Response          |
|------------------------------------------------------|-------------------|
| Group does not exist                                 | 404               |
| Group exists but no session active                   | 400 "нет активного полива" |
| Session active, race: zone state already 'off'       | 400 "нет активной зоны для пропуска" |
| Session active, valid current zone                   | 200 with ids      |
| Two skip presses ~50ms apart                         | First 200 (event was clear), second 200 but skip already processed by loop — see §9 |

---

## 4. Single-zone case — frontend hide rule

Per the issue: "When only ONE zone is running (no queue): hide the button."

**Source of truth**: `db.get_group_scheduled_starts(group_id)` — already
populated by `start_group_sequence` and consumed elsewhere. Today
`/api/status` does not surface it. We need to expose the queue length:

Add to each group entry in `/api/status` response (`routes/system_status_api.py`):

```python
# After `current_zone = active_zones[0]['id']` (around line 336)
queue_remaining = 0
try:
    if status == 'watering' and current_zone:
        starts = db.get_group_scheduled_starts(group_id) or {}
        # entries with planned_start strictly AFTER the current zone's start
        cur_zone_row = next((z for z in group_zones if int(z['id']) == int(current_zone)), None)
        cur_start = (cur_zone_row or {}).get('watering_start_time')
        if cur_start:
            queue_remaining = sum(
                1 for zid, ps in starts.items()
                if ps and ps > cur_start and int(zid) != int(current_zone)
            )
except (sqlite3.Error, OSError, ValueError, TypeError):
    queue_remaining = 0
```

Add `'queue_remaining': queue_remaining` to the group dict.

**Frontend rule** in `static/js/status/status-groups.js`:

```js
// In groupActionHtml block (line 57-59)
const skipBtnHtml = (anyZoneOnThisGroup && Number(group.queue_remaining || 0) > 0)
    ? `<button class="group-action-btn group-action-skip" onclick="skipCurrentZone(${group.id})">${_m ? '⏭ Пропустить' : 'Пропустить зону'}</button>`
    : '';
```

Append `skipBtnHtml` to the existing `groupActionHtml` (next to "Стоп").

When `queue_remaining === 0` (single zone OR last zone of a queue), button is
hidden — exactly as the issue specifies.

---

## 5. Master-valve optimization

**Trace**: when a zone stops via the runner (`zone_control.stop_zone` called
at `program_runner.py:596-600`), `_schedule_master_close` is invoked with
`immediate=False` → MV close fires after `master_close_delay_sec` via a
`threading.Timer`. Next zone start in the same group calls
`exclusive_start_zone` which pre-opens MV; the open path **cancels** the
pending close timer (verified: `_schedule_master_close` keeps a per-group
`Timer` reference and cancels on subsequent open).

**Skip path inherits this for free** — we call `stop_zone(reason='auto')`
just like the natural end-of-zone path, then immediately fall through to
the next iteration which calls the start path. The MV close timer is set
then cancelled within the per-zone gap (well under typical `master_close_delay_sec`
of 10-60 sec).

**No change needed** to MV logic. Just don't pass
`master_close_immediately=True` from the skip path.

---

## 6. Audit log event

**Action type**: `zone_skip` (distinct from `zone_stop`, `zone_auto_stop`).
**Source**: `manual_skip` (in payload) — frontend-triggered.
**Target**: `group:<gid>` (matches existing `api_stop_group` audit pattern at
`groups_api.py:147`).

Two audit emissions:
1. **At endpoint entry** — via `@audit_log('zone_skip', target_extractor=...)`
   decorator (matches existing pattern). Captures the request.
2. **Inside the loop**, after the centralized stop, via
   `db.add_log('zone_skip', json.dumps({...}))` — captures the actual stop
   event with `zone_id`, `zone_name`, `group_id`, `reason='manual_skip'`.

Distinct from:
- `zone_stop` (manual stop of a single zone via `/api/zones/<id>/stop`)
- `group_stop` (kill the whole session)
- `zone_auto_stop` / `auto` (natural end-of-zone)

---

## 7. Frontend

### 7.1 Where to add the button

`static/js/status/status-groups.js`, in the `groupActionHtml` template at
line 57-59. New skip button rendered next to "Стоп" when
`queue_remaining > 0`.

CSS class `group-action-skip` — add to `static/css/` (whichever file holds
`group-action-stop` / `group-action-start`). Visual: same shape as Stop, but
neutral or info color (e.g. blue/gray), not red.

### 7.2 Handler in `static/js/status.js`

Next to `stopGroup` (line 846), add:

```js
let _skipInFlight = new Set();   // module-scoped debounce — one in-flight per group
async function skipCurrentZone(groupId) {
    const key = String(groupId);
    if (_skipInFlight.has(key)) return;       // debounce: ignore double-click
    _skipInFlight.add(key);
    try {
        const res = await fetch(`/api/groups/${groupId}/skip-current`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}'
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data && data.success) {
            showNotification('Зона пропущена', 'success');
            // SSE/poll will refresh state. Force one immediate refresh too.
            await Promise.all([loadStatusData(), loadZonesData()]);
        } else {
            showNotification((data && data.message) || 'Не удалось пропустить зону', 'warning');
        }
    } catch (e) {
        showNotification('Ошибка при пропуске зоны', 'error');
    } finally {
        // Release after a short delay to absorb double-clicks during the
        // server's own zone transition window (~1-2 sec).
        setTimeout(() => _skipInFlight.delete(key), 1500);
    }
}
```

**No `confirm()`** — skip is reversible-ish (you can press again, you can
press Stop). Confirm dialogs on a one-shot button are friction. (If the user
wants confirm, add it — but it's not in the issue.)

### 7.3 Show/hide rule

Already documented in §4: button rendered only when
`group.queue_remaining > 0` AND `group.status === 'watering'`.

---

## 8. Test plan (4 tests, outcome-based)

All in `tests/integration/test_skip_current_zone.py`. `TESTING=true` mode
ensures synchronous-ish runner where feasible; for skip we need real timing,
so several tests use small durations (~5 sec) and poll.

```python
def test_skip_advances_to_next_zone(client, db, scheduler):
    """3-zone group, start sequence, skip first → second runs, third runs."""
    # setup: group with zones [101, 102, 103], each duration=1 (min, but TESTING shrinks to ~5 sec)
    client.post(f'/api/groups/{gid}/start-from-first')
    poll_until(lambda: db.get_zone(101)['state'] == 'on', timeout=3)
    rv = client.post(f'/api/groups/{gid}/skip-current')
    assert rv.status_code == 200
    assert rv.get_json()['skipped_zone_id'] == 101
    assert rv.get_json()['next_zone_id'] == 102
    poll_until(lambda: db.get_zone(102)['state'] == 'on', timeout=3)
    poll_until(lambda: db.get_zone(101)['state'] == 'off', timeout=3)
    # confirm zone 101 was NOT auto-finished — log says zone_skip
    logs = db.get_logs(action='zone_skip')
    assert any(l['details_json']['zone_id'] == 101 for l in logs)

def test_skip_last_zone_ends_session(client, db, scheduler):
    """Skip last zone → session ends cleanly, group_cancel_events popped."""
    client.post(f'/api/groups/{gid}/start-from-first')
    # wait for last zone (103) to start
    poll_until(lambda: db.get_zone(103)['state'] == 'on', timeout=15)
    rv = client.post(f'/api/groups/{gid}/skip-current')
    assert rv.status_code == 200
    assert rv.get_json()['next_zone_id'] is None
    poll_until(lambda: not scheduler.is_group_session_active(gid), timeout=5)
    poll_until(lambda: db.get_zone(103)['state'] == 'off', timeout=3)

def test_skip_without_active_session_returns_400(client, scheduler):
    rv = client.post(f'/api/groups/{gid}/skip-current')
    assert rv.status_code == 400
    assert rv.get_json()['success'] is False

def test_skip_double_click_only_advances_once(client, db, scheduler):
    """Two skips ~50ms apart → only one zone gets skipped, not two."""
    client.post(f'/api/groups/{gid}/start-from-first')
    poll_until(lambda: db.get_zone(101)['state'] == 'on', timeout=3)
    rv1 = client.post(f'/api/groups/{gid}/skip-current')
    rv2 = client.post(f'/api/groups/{gid}/skip-current')   # immediate
    assert rv1.status_code == 200
    # rv2: either 200 (event was already cleared and re-set, but loop
    # captured the first) or 400 (zone already off). Both acceptable;
    # what matters is zone 103 does NOT start in this window.
    poll_until(lambda: db.get_zone(102)['state'] == 'on', timeout=3)
    # Confirm 103 is still off — i.e., we didn't double-skip
    assert db.get_zone(103)['state'] == 'off'
```

Optional 5th test if time:

```python
def test_skip_during_inter_zone_pause(client, db, scheduler):
    """Set early_off_seconds=10, fire skip during the make-up wait window.
    The pause should be cancelled, next zone starts immediately."""
    # set early_off_seconds to a measurable value
    # poll for zone 101 to reach 'off' but session still active
    # fire skip → zone 102 should start within ~1 sec, not after the full early gap
```

---

## 9. Risks

### 9.1 Skip arrives between zone-off and next-zone-start

The 1-sec sleep tick has a window of ~0..1 sec where `remaining == 0` and the
loop exits naturally. If skip arrives in that window:

- The skip event gets set on `group_skip_current_events[gid]`.
- The current iteration completes naturally (zone-off path runs anyway).
- Next iteration starts, runs zone-on, enters its own sleep loop, sees the
  event, immediately stops the new zone, jumps to iteration after that.

**Effect**: an extra zone gets skipped along with the originally-current one.
This is the cost of not holding a lock around the entire iteration.

**Mitigation**: clear the event at TWO points:
1. Inside the inner loop when consumed (already in §2.3).
2. **At the top of the `for` loop**, immediately after picking the next zone
   but BEFORE starting it, if `cancel_event` was just checked and not set:

```python
# Top of for loop, after cancel-event check
sk = self.group_skip_current_events.get(group_id)
if sk and sk.is_set():
    # Stale skip arrived during inter-zone gap. Consume but do NOT skip the
    # zone we haven't started yet — the user wanted to skip the previous one,
    # which already ended.
    sk.clear()
```

This makes the skip event strictly tied to "the zone currently running",
not "any future zone". Safer.

### 9.2 Skip arrives AFTER session naturally ended

`is_group_session_active(gid)` returns False (the `finally` block popped the
event dict entry). API returns 400. Clean.

### 9.3 Two skip presses ~50ms apart

- Press A: API reads `is_group_session_active=True`, calls
  `request_skip_current_zone(gid)` → event set.
- Press B (50ms later): API reads `is_group_session_active=True` (still),
  calls `request_skip_current_zone(gid)` → event already set, idempotent.
- Loop wakes (within ~1 sec), consumes event (clear), stops zone, advances.

**Both API calls return 200**. The loop only advances once.

If press B arrives AFTER the loop has already cleared the event AND moved on
to the next zone — press B sets the event again, the new zone gets skipped
too. This is the §9.1 case. The mitigation in §9.1 (clear-at-top) addresses it.

**Frontend debounce** (`_skipInFlight` Set with 1500ms timeout in §7.2)
prevents this on the client side too — defense in depth.

### 9.4 Concurrent stop + skip

User presses Stop, then Skip ~50ms later (or vice-versa).

- Stop sets `group_cancel_events[gid]`.
- Loop wakes, `cancel_event.is_set()` true, `break` out of inner loop, `break`
  out of outer for-loop (the "if cancel_event… continue" path stays put for
  per-group programs).
- Skip arrives, sets `group_skip_current_events[gid]` — too late, loop is
  already winding down via the `finally` clearing both events.

Or skip arrives first, sets event. Loop wakes, sees skip, consumes, stops
zone, `continue`s. Top of loop: `cancel_event.is_set()` — break. Session
ends. Stop wins.

Either order: session ends. Acceptable.

### 9.5 Skip during weather-skip / postpone branches in `_run_program_threaded`

The program-runner skips a zone via `continue` (lines 122, 128, 138)
**without** entering the inner sleep loop. If the user clicks Skip during a
weather/postpone iteration, the event sits set until the next zone's sleep
loop. Then it gets consumed there → that zone gets skipped.

**Effect**: misalignment. User sees zone-being-postponed-anyway, clicks Skip,
the NEXT actually-running zone gets skipped instead.

**Mitigation**: same as §9.1 — clear at top of loop. The clear-at-top makes
the skip event strictly "skip the zone whose sleep loop you arrive at first,
and only if you arrive within ~1 sec". Stale skips get dropped.

This is acceptable because the user only sees the Skip button when the
group is `status === 'watering'` (an `on` zone exists). Postpone/weather
branches don't have an `on` zone, so the button isn't visible, so the click
shouldn't happen in practice.

### 9.6 In-thread loops in `irrigation_scheduler.py` are dead, but I should double-check

Quick MRO sanity test before merge:

```python
from irrigation_scheduler import IrrigationScheduler
from scheduler.program_runner import ProgramRunnerMixin
assert IrrigationScheduler._run_group_sequence is ProgramRunnerMixin._run_group_sequence
assert IrrigationScheduler._run_program_threaded is ProgramRunnerMixin._run_program_threaded
```

If false, the in-class copies in `irrigation_scheduler.py:540-745` and
`:1203-1418` ARE reachable and need the same patch. Add this assertion to
the test file as a guard — it's a one-liner.

---

## 10. Backwards compatibility

- New endpoint: additive. Existing `/api/groups/<gid>/stop`,
  `/start-from-first`, `/api/zones/<zid>/stop` unchanged.
- New scheduler dict `group_skip_current_events`: only populated/read inside
  the new method and the patched loops. If skip is never called, dict stays
  empty, loops behave identically (one extra `dict.get` call per second per
  active sequence — negligible).
- New `queue_remaining` field in `/api/status`: additive. Existing clients
  ignore unknown fields.
- Frontend skip button: only rendered when `queue_remaining > 0`. Existing
  pre-deploy clients that render against an old `/api/status` simply don't
  see the button — graceful.
- Audit log: new `zone_skip` action type. Existing log consumers that filter
  by known types unaffected. Log retention sweeper at
  `irrigation_scheduler.py` (audit cleanup job) is type-agnostic.
- DB schema: zero changes.

---

## 11. Implementation order (for senior to execute)

1. **Pre-req**: confirm `is_group_session_active` is on this branch (rebase
   `feat/14-skip-zone-in-group-watering` on top of merged #16). If not yet
   merged, block until it is.
2. Add `group_skip_current_events` dict at `irrigation_scheduler.py:307`.
3. Add `request_skip_current_zone` method on `IrrigationScheduler`.
4. Patch both `_run_group_sequence` and `_run_program_threaded` in
   `scheduler/program_runner.py`:
   - Local `skipped_this_zone` flag at start of each iteration.
   - Skip-event check inside the inner sleep loop (alongside cancel/shutdown).
   - Clear-at-top guard (§9.1 mitigation).
   - Post-stop `add_log('zone_skip', …)` + `continue`.
   - In the `finally` block, also pop `group_skip_current_events[gid]`.
5. Verify MRO assertion (§9.6); patch in-class copies if needed.
6. Add `queue_remaining` to `/api/status` response in
   `routes/system_status_api.py`.
7. Add `POST /api/groups/<gid>/skip-current` handler in `routes/groups_api.py`.
8. Add `_compute_next_zone_id` helper alongside it (reads
   `db.get_group_scheduled_starts`).
9. Frontend: skip button in `status-groups.js`, handler + debounce in
   `status.js`, CSS class `group-action-skip`.
10. Tests: 4 outcome-based integration tests (§8). Add MRO guard assertion.

---

## 12. Out of scope (do NOT do)

- Do not refactor `_run_group_sequence` and `_run_program_threaded` to share
  code. They're nearly-identical and have been for ages; this issue is not
  the time. Karpathy #3.
- Do not delete `services/program_queue.py` even though it's confirmed dead.
  It's noise but it's not THIS issue's noise.
- Do not delete the in-class copies of the runners in `irrigation_scheduler.py`
  even if MRO confirms they're unreachable. Same reason.
- Do not change MV close delay logic. It already does the right thing.
- Do not add a session-id concept. Group-id is the key today; that's enough.
