# Issue #16 — Stop must abort the entire active session

GitHub: https://github.com/Raul-1996/irrigation/issues/16
Branch: `fix/16-stop-cancels-queue`

## TL;DR

The "queue" the user perceives is **not** the dataclass-based `ProgramQueueManager`
in `services/program_queue.py` — that class is **dead code** in production
(imported only by `tests/`). The real "queue" is the **in-thread `for zone_id in
zone_ids:` loop inside `IrrigationScheduler._run_group_sequence`** (and the
analogous loop inside `_run_program_threaded` for time-scheduled programs),
running on an APScheduler thread, gated by `self.group_cancel_events[group_id]`.

The single-zone stop endpoint `POST /api/zones/<id>/mqtt/stop` (and the legacy
`POST /api/zones/<id>/stop`) only calls `services.zone_control.stop_zone(zone_id)`
which closes the valve and updates DB state — it never touches
`group_cancel_events` and never removes the `group_seq:<gid>:*` APScheduler job.
The runner thread is sitting in its `while remaining > 0:` countdown
(`irrigation_scheduler.py:1362`); when the timer expires it advances to the
next zone in `zone_ids` and starts it.

The minimum fix is to change those two single-zone-stop endpoints so that, **iff
the zone belongs to a group that currently has an active session** (a populated
`group_cancel_events[gid]`), they additionally call
`scheduler.cancel_group_jobs(gid)` and emit a new audit event
`session_aborted_by_user`. Solo single-zone stops (no active session) keep
exactly today's behaviour.

---

## 1. Current state — concrete code map

### 1.1 What "active session" means today

There is no first-class `Session` entity. A "session" is defined implicitly:

| Trigger                                              | Sets `group_cancel_events[gid]`?                            | Sequencer thread                              |
| ---------------------------------------------------- | ----------------------------------------------------------- | --------------------------------------------- |
| `start_group_sequence` (manual group start)          | Yes — `irrigation_scheduler.py:1144-1146`                   | `_run_group_sequence` (via `job_run_group_sequence`, APScheduler) |
| Scheduled program firing (`schedule_program`)        | **No upfront** — only consults the dict per-iteration       | `_run_program_threaded`                       |
| Manual single-zone start (`/api/zones/<id>/mqtt/start` or `/start`) | No (in fact calls `cancel_group_jobs` first to clear it)    | None — fire-and-forget zone with `schedule_zone_stop`/`schedule_zone_hard_stop` only |

So:
- For a **manual group start** (issue #16 reproducer: "user starts a
  group/program watering"), `group_cancel_events[gid]` IS present while the
  sequence runs and is cleared in `_run_group_sequence`'s `finally` at
  `irrigation_scheduler.py:1409-1418`.
- For a **scheduled program**, `group_cancel_events[gid]` is **not pre-populated**
  but the sequencer reads the dict on each iteration. To abort it, you must put
  a set Event into the dict (`/api/health/group/<gid>/cancel` does exactly
  this at `routes/system_status_api.py:138-140`, and `cancel_group_jobs` does
  it via `.set()` at `irrigation_scheduler.py:1437-1438` — but only if the key
  already exists, which is a latent bug for the scheduled-program case; see
  §6.4).

So `group_cancel_events.get(gid)` returning a non-None Event is a **reliable
positive signal** that an active session is in flight for the manual group
case. For the scheduled-program case it's only reliable if we either
pre-populate it on schedule, or fall back to a secondary signal (see §6.4).

### 1.2 The runner loops

`irrigation_scheduler.py:1203-1418` `_run_group_sequence(group_id, zone_ids, override_duration)`:
- Captures `cancel_event = self.group_cancel_events.get(group_id)` once (line 1245).
- Iterates `for zone_id in zone_ids`:
  - Per-zone start (`update_zone_state` + zone hard-stop scheduled at
    `irrigation_scheduler.py:1283`).
  - Inner countdown `while remaining > 0:` at line 1362, polling
    `cancel_event.is_set()` every 1 sec.
  - On cancel: `break` zone loop, falls through to centralized `stop_zone(...,
    reason='group_sequence')`.
- On loop exit: clears + pops `group_cancel_events[group_id]`.

`irrigation_scheduler.py:530-745` `_run_program_threaded(program_id, zones, program_name)`:
- Per-zone iteration.
- Reads `self.group_cancel_events.get(group_id)` per-iteration.
- On cancel: `continue` (skip remaining zones of this group, but iterate to next
  zone if its group_id differs).

Both loops respond to `group_cancel_events[gid].set()` within ~1 second.

### 1.3 The single-zone stop call paths

Frontend:
- Per-zone card stop button → `static/js/status.js:911`
  → `POST /api/zones/<id>/mqtt/stop` (matches the issue reproducer).
- Per-zone card stop button (alternate handler) → `static/js/status.js:1970`,
  `:2330` → also `/api/zones/<id>/mqtt/stop`.
- Zones page handler → `static/js/zones.js:1156` → also `/api/zones/<id>/mqtt/stop`.

Backend:
- `routes/zones_watering_api.py:438-471` `api_zone_mqtt_stop(zone_id)`
  - `zone_control.stop_zone(zone_id, reason='manual', force=False)` only.
- `routes/zones_watering_api.py:101-125` `stop_zone(zone_id)` (legacy `/stop`)
  - Same: `zone_control.stop_zone(zone_id, reason='manual', force=False)` only.

Neither touches `group_cancel_events`, neither removes `group_seq:*`
APScheduler jobs, neither calls `cancel_group_jobs`.

### 1.4 What `services.zone_control.stop_zone` does

`services/zone_control.py:400-595`:
- Idempotent `state -> stopping -> off`.
- MQTT publish `0` on the zone topic.
- Schedules delayed master valve close (`_schedule_master_close`).
- Finishes open `zone_run` row.
- **Does NOT** consult or mutate `group_cancel_events`.
- **Does NOT** remove APScheduler jobs.

This is correct behaviour for it — `stop_zone` is the per-zone primitive and
must stay scope-limited. Session-level cancellation belongs one layer up.

### 1.5 What `cancel_group_jobs` does (the "right" abort)

`irrigation_scheduler.py:1427-1484`:
1. `group_cancel_events[gid].set()` (so any sequencer thread wakes within ~1s).
2. `stop_all_in_group(gid, force=True, master_close_immediately=...)` — every
   zone in the group goes OFF immediately, master scheduled.
3. `cancel_zone_jobs(zone_id)` for each zone in the group (removes
   `zone_stop:<zid>:*` and `zone_hard_stop:<zid>` APScheduler jobs).
4. Removes `group_seq:<gid>:*` APScheduler jobs (the future-scheduled sequence
   re-arming, if any was queued).
5. `db.reschedule_group_to_next_program(gid)`.

This is the canonical "abort the whole session" primitive. It's already used by:
- `POST /api/groups/<gid>/stop` — `routes/groups_api.py:159`
- `POST /api/emergency-stop` — `routes/system_emergency_api.py:59` (looped over
  every group)
- `POST /api/zones/<id>/start` and `/api/zones/<id>/mqtt/start` — they
  pre-cancel the group session before starting a single zone (this is also why
  starting a manual single zone implicitly aborts a running sequence — and is
  why the "solo zone after group abort" case ends up with no active session).

So `/api/groups/<gid>/stop` and `/api/emergency-stop` already satisfy the
issue's policy. **Only the single-zone stop endpoints are wrong.**

---

## 2. Root cause — the precise call path

User journey from the issue:

1. User taps "Group A — start from first" → `POST /api/groups/<gid>/start-from-first`
   → `start_group_sequence(gid)` (`irrigation_scheduler.py:1106`):
   - sets `group_cancel_events[gid] = Event()` (line 1146, NOT set);
   - `scheduler.add_job(job_run_group_sequence, ...)` with id `group_seq:<gid>:<ts>`.
2. APScheduler thread invokes `job_run_group_sequence` → `_run_group_sequence`.
   First iteration: zone A is started; per-zone `zone_hard_stop` watchdog is
   scheduled at `+duration_A`; sequencer enters its `while remaining > 0:`
   countdown.
3. User taps the stop icon on the card for zone A. UI sends
   `POST /api/zones/<zoneA_id>/mqtt/stop`.
4. `api_zone_mqtt_stop` calls `stop_zone(zoneA_id, reason='manual')`:
   - zone A valve OFF on MQTT, DB state `off`, master close scheduled.
   - **Does not** set `group_cancel_events[gid]`.
   - **Does not** remove `group_seq:<gid>:<ts>` job (it's already running, but
     more importantly its in-thread loop is still alive).
   - The `zone_hard_stop:<zoneA_id>` job is also left in place — it'll fire
     against an already-off zone, harmless but spurious.
5. Sequencer thread for `_run_group_sequence` is still inside its
   `while remaining > 0:` loop (line 1362). `cancel_event.is_set()` is False.
   It keeps decrementing `remaining` until the original duration of zone A
   elapses.
6. Sequencer falls through to `stop_zone(zoneA, reason='group_sequence')`
   (idempotent — already off), then advances to zone B in `zone_ids`, starts
   it. Bug visible to user: "zone B started by itself after the timer for the
   stopped zone A".

**Root cause in one line:** `api_zone_mqtt_stop` (and `api_zone_stop`) treat a
single zone as if it has no parent session, so they only stop the valve. The
sequencer thread, gated by `group_cancel_events[gid]`, is unaware and proceeds.

---

## 3. Proposed fix

### 3.1 Design principles

- **Stop is always a full-session abort**, by issue policy. There is no
  "stop-current-and-continue" semantic in the existing UI; that's reserved for
  the future `Skip` button (issue #14).
- **Solo zone stop must remain a no-op-on-session**: don't accidentally cancel
  a non-existent session.
- **Session detection must be reliable**: a non-`None` Event in
  `group_cancel_events[gid]` is the ground truth for "this group is in an
  active sequence/program right now". A small helper isolates the test so we
  can extend it later (e.g. for scheduled programs that don't pre-register).
- **Reuse `cancel_group_jobs`**: it already does everything required (sets
  cancel event, force-stops every zone in the group, removes group_seq /
  zone_stop / zone_hard_stop jobs). Don't reinvent it.
- **Audit explicitly**: emit a new `session_aborted_by_user` audit row so
  history can distinguish "user pressed stop on a sequence" from "natural
  completion" or "emergency stop".

### 3.2 New helper: `IrrigationScheduler.is_group_session_active`

Add to `irrigation_scheduler.py` (next to `cancel_group_jobs`):

```python
def is_group_session_active(self, group_id: int) -> bool:
    """True iff the group currently has an in-flight sequence or program run.

    Currently this is equivalent to "group_cancel_events[gid] exists", because
    that key is created by start_group_sequence and (per fix in §6.4) by
    schedule_program when it places a program job. The Event being set
    means cancel-in-progress; the existence of the Event is what indicates
    a session.
    """
    try:
        return self.group_cancel_events.get(int(group_id)) is not None
    except (TypeError, ValueError, KeyError):
        return False
```

Rationale: keep the discriminator in one place so when we extend session
tracking (e.g. fix §6.4), all call sites get the upgrade for free.

### 3.3 Modify `routes/zones_watering_api.py:api_zone_mqtt_stop`

Pseudocode of the change (delta only — preserve everything else, including the
existing `force=False` central stop semantics for the solo case and the
`fallback to direct publish` branch):

```python
@zones_watering_api_bp.route('/api/zones/<int:zone_id>/mqtt/stop', methods=['POST'])
def api_zone_mqtt_stop(zone_id: int):
    z = db.get_zone(zone_id)
    if not z:
        return jsonify({'success': False}), 404
    gid = int(z.get('group_id') or 0)

    # NEW: detect an active session for this zone's group.
    sched = get_scheduler()
    session_active = bool(sched and gid and sched.is_group_session_active(gid))

    if session_active:
        # Full session abort — same primitive used by /api/groups/<id>/stop.
        try:
            from services.audit import record_audit
            record_audit(
                action_type='session_aborted_by_user',
                source='zone_stop',
                target=f'group:{gid}',
                payload={'triggered_by_zone': int(zone_id),
                         'endpoint': 'api_zone_mqtt_stop'},
                actor='user',
            )
        except Exception:  # noqa: BLE001
            logger.exception('session_aborted_by_user audit failed')

        try:
            sched.cancel_group_jobs(int(gid))
            # cancel_group_jobs already invokes stop_all_in_group(force=True)
            # which stops THIS zone too, so we don't need an extra stop_zone.
            return jsonify({'success': True,
                            'message': 'Сессия группы остановлена',
                            'session_aborted': True})
        except (ValueError, TypeError, KeyError, RuntimeError):
            logger.exception('api_zone_mqtt_stop: cancel_group_jobs failed')
            # Fall through to legacy single-zone stop path as best-effort
            # safety net so the zone definitely goes off.

    # EXISTING solo-zone path — unchanged.
    try:
        from services.zone_control import stop_zone as _stop_central
        if _stop_central(int(zone_id), reason='manual', force=False):
            return jsonify({'success': True, 'message': 'Зона остановлена'})
    except (ValueError, TypeError, KeyError):
        logger.exception('api_zone_mqtt_stop: central stop failed, fallback to direct publish')
    # ... existing direct-MQTT fallback block stays as-is
```

### 3.4 Modify `routes/zones_watering_api.py:stop_zone` (the `/api/zones/<id>/stop`
legacy endpoint)

Apply the **same** session-active short-circuit. Same pseudocode, just before
the existing `_stop_central(zone_id, reason='manual', force=False)` call.
Distinct `endpoint` value in the audit payload (`'api_zone_stop'`) so the two
endpoints stay distinguishable in the audit log.

### 3.5 `/api/groups/<gid>/stop` — already correct

`routes/groups_api.py:146-191` already calls `cancel_group_jobs`. Add the
audit emit immediately after (or in lieu of) the existing `db.add_log('group_stop')`:

```python
record_audit(
    action_type='session_aborted_by_user',
    source='group_stop',
    target=f'group:{group_id}',
    payload={'endpoint': 'api_stop_group'},
    actor='user',
)
```

The behaviour is unchanged; only the audit signal is added so a single query
on `action_type='session_aborted_by_user'` lists all user-driven aborts
regardless of which button was pressed.

### 3.6 `/api/emergency-stop` — already correct

`routes/system_emergency_api.py` already calls `cancel_group_jobs` for every
group at line 59. Optionally emit a `session_aborted_by_user` per group inside
that loop (with `source='emergency_stop'`) so the audit trail is symmetric.
Whether to do this is a small judgement call — the existing
`emergency_stop` audit already covers it at the system level. Recommendation:
**do not** double-audit emergency; keep `session_aborted_by_user` reserved for
per-group user-driven aborts.

### 3.7 Solo zone stop — unchanged

If `is_group_session_active(gid)` is False (or `gid == 0`), the existing
`_stop_central(zone_id, reason='manual')` runs. No new audit, no behaviour
change. This is the "single zone running solo, user stops it" case from the
issue.

---

## 4. Test plan

### 4.1 Unit tests (`tests/unit/test_zone_control.py` or new `test_session_abort.py`)

1. `test_is_group_session_active_returns_false_when_no_event`
   - Fresh scheduler, no manipulation → `False`.
2. `test_is_group_session_active_returns_true_when_event_present`
   - `scheduler.group_cancel_events[5] = threading.Event()` → `True`
     (Event NOT set — existence is what matters).
3. `test_is_group_session_active_returns_true_when_event_set`
   - Event present and `.set()` → still `True`.

### 4.2 API tests (`tests/api/test_zones_api.py` + `test_groups_api.py`)

Use Flask test client + mocked scheduler (the existing pattern in those
files). Mock `get_scheduler()` to return an object whose
`group_cancel_events`, `is_group_session_active`, and `cancel_group_jobs`
are inspectable.

4. `test_zone_stop_solo_does_not_call_cancel_group_jobs`
   - No entry in `group_cancel_events`.
   - `POST /api/zones/<id>/mqtt/stop` → assert `stop_zone` was called,
     `cancel_group_jobs` was NOT called, response shape unchanged.
5. `test_zone_stop_during_session_calls_cancel_group_jobs`
   - `group_cancel_events[zone.group_id] = Event()` (not set).
   - `POST /api/zones/<id>/mqtt/stop` → assert `cancel_group_jobs(gid)` was
     called exactly once. Response includes `session_aborted: True`.
6. `test_zone_stop_during_session_emits_audit_session_aborted`
   - Same setup as #5, also assert an `audit_log` row with
     `action_type='session_aborted_by_user'`, `target='group:<gid>'`,
     `payload.endpoint='api_zone_mqtt_stop'`.
7. Repeat #5 + #6 for the legacy `POST /api/zones/<id>/stop` (different
   `endpoint` value in payload).
8. `test_group_stop_emits_audit_session_aborted`
   - `POST /api/groups/<gid>/stop` → audit row with
     `action_type='session_aborted_by_user'`, `payload.endpoint='api_stop_group'`.
9. `test_zone_stop_during_session_cancel_group_jobs_failure_falls_back`
   - Force `cancel_group_jobs` to raise. Endpoint must still attempt the legacy
     `stop_zone` so the valve is closed even if abort plumbing breaks.

### 4.3 Integration tests (`tests/integration/`)

10. `test_full_watering_cycle_zone_stop_aborts_sequence`
    - In `TESTING` mode (zones complete in seconds), call
      `start_group_sequence(gid)` for a 3-zone group.
    - Wait until zone 1 is `state='on'`.
    - `POST /api/zones/<zone1_id>/mqtt/stop`.
    - Assert: zone 1 is `off` within 1s; **zones 2 and 3 never go `state='on'`**
      (poll for `total_seconds_to_completion + 2s`); audit shows
      `session_aborted_by_user` exactly once and zero `program_run_completed` /
      `group_seq_zone_start` for zones 2 and 3.
11. `test_solo_zone_stop_unchanged_behaviour` (regression guard)
    - Manual `exclusive_start_zone(zid)` (no session).
    - `POST /api/zones/<zid>/mqtt/stop`.
    - Assert: zone goes `off`; no `session_aborted_by_user` audit row;
      `group_cancel_events` is unchanged (still empty for that gid).
12. `test_zone_stop_during_scheduled_program_aborts_program_run` (depends on
    §6.4 — only meaningful after pre-registering Event for scheduled programs).
    Mark `xfail` until §6.4 is implemented; flip when ready.

### 4.4 What "passes" looks like

- Zero new failures in `pytest tests/unit tests/api`.
- New test in §4.3 #10 must fail on `main`/`fix/16-stop-cancels-queue`-without-fix
  and pass after the fix. Use this as the reproducer-test.

---

## 5. Implementation checklist

1. Add `IrrigationScheduler.is_group_session_active(group_id)` near
   `cancel_group_jobs` in `irrigation_scheduler.py`.
2. Modify `routes/zones_watering_api.py:api_zone_mqtt_stop` per §3.3.
3. Modify `routes/zones_watering_api.py:stop_zone` per §3.4.
4. Add `record_audit('session_aborted_by_user', ...)` to
   `routes/groups_api.py:api_stop_group` per §3.5.
5. Add unit + API + integration tests per §4. The integration test in §4.3 #10
   should be written first (red), then the code change makes it green —
   classic Karpathy goal-driven step.
6. Update `CHANGELOG` / `BUGS-REPORT.md` if the project follows that pattern
   (this repo has both files; check `BUGS-REPORT.md`).

No DB migrations needed. No frontend changes needed (UI's existing per-zone
stop button keeps doing the same `POST /api/zones/<id>/mqtt/stop` and now also
carries the abort semantics correctly). New response field
`session_aborted: True` is additive and the existing UI handler in
`status.js:914-918` only reads `data.success` and `data.message`, so it's
backward-compatible.

---

## 6. Risks and edge cases (the landmines)

### 6.1 Race: stop arrives during the gap between zones

The sequencer thread executes:
```
stop_zone(zone_i, reason='group_sequence')    # zone i off
... possibly waits early-off seconds ...
checks cancel_event                            # if set: break
# ELSE: next iteration starts zone_{i+1}
```

If the user's `POST /mqtt/stop` for zone i lands AFTER the sequencer has
already checked `cancel_event` and started the next zone's `update_zone_state`
+ MQTT publish but BEFORE we set `cancel_event`, then:
- `cancel_group_jobs` will run, set the event.
- `stop_all_in_group(force=True)` will OFF zone i+1's valve.
- Sequencer's next per-iteration check (top of `while remaining > 0:`) sees
  the event and breaks → falls through to `stop_zone(reason='group_sequence')`
  on zone i+1, which is already off (idempotent).

Outcome: zone i+1 may have been ON for ~1s of MQTT propagation before
`stop_all_in_group` overrides it. This is acceptable (and is already the
existing behaviour for `/api/groups/<gid>/stop` — we're not making this worse).
Mitigation: the `force=True` argument to `stop_all_in_group` makes
`stop_zone` re-publish OFF even when the DB state shows `off` already.

### 6.2 Double cancellation

If the user mashes the stop button: each request finds
`group_cancel_events[gid]` (still present until the sequencer's `finally`
block runs). The second call invokes `cancel_group_jobs` again — that already
handles the case (`group_cancel_events[gid].set()` is idempotent;
`scheduler.remove_job` raises `JobLookupError` which is caught;
`stop_all_in_group(force=True)` is idempotent on already-off zones). We will
emit a second `session_aborted_by_user` audit row, which is fine for
post-incident clarity.

If we want to suppress duplicate audit, gate it on
`group_cancel_events[gid].is_set()` being False at entry. Recommendation:
**don't** suppress — duplicate user clicks are real signal worth keeping.

### 6.3 Zone is in DB state `off` when stop arrives

Possible if MQTT off was already propagated by another path (peer-off, weather
skip, watchdog). `_stop_central(force=False)` returns idempotently. With our
new session-abort branch we never reach it for the session case, and the
`cancel_group_jobs` path force-stops anyway. No change needed.

### 6.4 Latent gap: scheduled programs don't pre-register `group_cancel_events`

`_run_program_threaded` consults `self.group_cancel_events.get(group_id)` per
iteration but `schedule_program` does NOT pre-create the Event. So:
- A user clicks stop on the first zone of a SCHEDULED program (one that fired
  on time).
- Our new `is_group_session_active(gid)` returns False (no Event in dict).
- We fall through to the solo-zone stop path → bug from issue #16
  reproduces for scheduled programs.

This is a real edge case from the issue's wording ("group/program watering").

**Recommended secondary fix** (small, surgical):
In `IrrigationScheduler._run_program_threaded` at the top (line 530, just
after the entry log), pre-register an Event for each distinct group in
`zones`:

```python
group_ids = {int(self.db.get_zone(z).get('group_id') or 0)
             for z in zones if self.db.get_zone(z)}
for gid in group_ids:
    if gid and gid != 999:
        # Don't replace an existing one (start_group_sequence may have set it)
        self.group_cancel_events.setdefault(gid, threading.Event())
```

And in the matching `finally`-style cleanup at the end of
`_run_program_threaded` (currently absent — there's no `finally` at all),
clear and pop those keys. This mirrors what `_run_group_sequence` does at
lines 1409-1418.

This is a small extra change but makes the issue #16 fix complete for
scheduled programs too.

### 6.5 Multiple groups, one program

If a program contains zones from multiple groups, `_run_program_threaded`
iterates them all. A user stop on one zone today aborts only that zone's
group-segment of the program (because we set the Event for that gid only). The
other groups continue. This **matches the issue's wording**: "stopping a zone
during an active session aborts that session". We treat per-group as the
session boundary, consistent with how `/api/groups/<gid>/stop` already
behaves. Document this explicitly in the user-facing changelog.

### 6.6 `cancel_group_jobs` calls `stop_all_in_group(force=True)` which
publishes OFF for every zone in the group, including ones that never ran

This is desired: the policy is "abort the whole session", so even queued
zones-that-haven't-started-yet should not silently start. Side effect:
`stop_zone` for an already-off zone runs the water-stats finalisation block
(harmless) and may schedule a master-valve close (we want that too). No issue.

### 6.7 `session_active` flag is read from `group_cancel_events` without a
lock

`group_cancel_events` is a plain `dict` (`irrigation_scheduler.py:307`).
Concurrent read+write can in theory raise; in practice CPython dict reads are
GIL-protected for `get()`. This matches existing code pattern (e.g.
`system_status_api.py:81-83` does the same loop without a lock). If we want
to be belt-and-braces, wrap the helper in a class-level `threading.Lock` —
but recommend deferring this until profiling shows a problem.

### 6.8 Frontend optimistic UI

`status.js:911-925` already does refresh-all-UI on success. Our new
`session_aborted: True` flag is informational; a tiny follow-up could surface
a Russian-language toast like "Сессия группы остановлена" (already in the
proposed response message), but not strictly required by issue #16.

---

## 7. Out of scope

- Refactoring the dead `services/program_queue.py` module (file is
  test-only; removing or wiring it is its own ticket).
- Removing duplicate `_run_program_threaded` / `_run_group_sequence` /
  `start_group_sequence` definitions in the dead `scheduler/program_runner.py`
  mixin (also dead — `IrrigationScheduler` does not inherit it). Mention to
  ops, do not delete in this fix.
- Skip-current-zone button (issue #14).
- The legacy `/api/run` 404 (issue #10).
