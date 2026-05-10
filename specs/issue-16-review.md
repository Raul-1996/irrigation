# Issue #16 Code Review — fix/16-stop-cancels-queue

Reviewer: independent, ran tests, walked the code.
Branch: `fix/16-stop-cancels-queue` (6 commits ahead of `origin/main`).
Spec: `specs/issue-16-architecture.md`.

## Verdict

**CHANGES REQUESTED** — one real concurrency bug in §6.4 implementation, plus
one factually-wrong code comment, plus one weaker-than-spec'd test. None block
the immediate user-facing fix from working in the common case, but they should
be fixed before merge to avoid silent loss of cancel signal under concurrent
program runs.

If the team wants to ship "good enough today, refactor next sprint," this is
APPROVE-with-followup-issue. If they want it tight, address the items in
"Critical findings" first.

---

## Critical findings (blocking)

### C1. Comment lies, and the actual `if-then-set` pattern has a TOCTOU race that the spec'd `setdefault` does not.

`irrigation_scheduler.py:539` (comment) says:

> We use setdefault so a concurrent start_group_sequence's already-planted
> Event is preserved.

The actual code at `irrigation_scheduler.py:564-566`:

```python
for gid in program_gids:
    if gid not in self.group_cancel_events:
        self.group_cancel_events[gid] = threading.Event()
        registered_gids.append(gid)
```

This is **not** `setdefault`. It is the classic `if-not-in / dict[k]=v` TOCTOU
pattern. Two concurrent `_run_program_threaded` invocations for overlapping
gids can both observe `gid not in dict`, both insert, both append to their
private `registered_gids`. CPython's GIL makes each individual bytecode atomic,
but the TWO bytecodes (`__contains__` then `__setitem__`) are not — a context
switch between them is the documented failure mode for this pattern.

Concrete consequence (the real, not theoretical, bug):

1. Thread A starts `_run_program_threaded` for program P1 touching gid=5.
2. Thread A: `5 not in dict` → True; (preempt).
3. Thread B starts `_run_program_threaded` for program P2 also touching gid=5.
4. Thread B: `5 not in dict` → still True; `dict[5] = EventB`; `registered_gids_B = [5]`.
5. Thread A resumes: `dict[5] = EventA` → **overwrites EventB**; `registered_gids_A = [5]`.
6. Both runner loops now read `dict.get(5)` per-iteration → both read EventA (B's
   reference is orphaned).
7. Thread A finishes first → `finally` pops `dict[5]`. EventA is still floating
   inside Thread A but it's already dead.
8. Thread B is still running. Per-iteration read `dict.get(5)` now returns
   `None`. **A user pressing stop on a zone in this program now silently fails
   to abort B's run** — `is_group_session_active(5)` returns False because the
   dict entry was popped, so `api_zone_mqtt_stop` falls through to the solo
   path. Issue #16 reproduces for program B.

Probability is low (overlapping concurrent program runs on a shared gid) but
this is the exact bug class issue #16 was filed against, just with one extra
condition. The §6.4 fix should not introduce a sibling of the bug it's fixing.

**Required fix** — use `dict.setdefault` (atomic in CPython, single bytecode
`PyDict_SetDefault`) and rely on identity check to know if WE planted it:

```python
for gid in program_gids:
    new_event = threading.Event()
    planted = self.group_cancel_events.setdefault(gid, new_event)
    if planted is new_event:
        registered_gids.append(gid)
```

Same shape, no TOCTOU, comment now matches code.

Or — preferably — wrap the helper in a small per-scheduler `threading.Lock`
around the `group_cancel_events` mutation surface (also covers
`start_group_sequence` line 1197 which unconditionally overwrites whatever
was there — see C2). Spec §6.7 already flagged that the dict is unlocked and
deferred it; that deferral is fine for *reads*, but writes-with-conditional
are a different beast.

**Note on cleanup pop:** even with `setdefault`, the cleanup at
`irrigation_scheduler.py:789-796` `pop(gid, None)` is unconditional — it does
NOT check that the dict entry is still the one this thread planted. If
`start_group_sequence` was called concurrently for the same gid AFTER our
program registered (line 1197 unconditionally writes a *new* Event into the
dict), our cleanup would prematurely pop the sequence's Event and the running
sequence loses its cancel signal too. Fix: store the planted Event alongside
the gid (`registered_gids: List[Tuple[int, Event]]`) and on cleanup only pop
if `dict.get(gid) is our_event`. Cheap, surgical, removes the foot-gun.

### C2. `start_group_sequence` line 1197 is the symmetric foot-gun and was not touched by the §6.4 fix.

`irrigation_scheduler.py:1197`:

```python
self.group_cancel_events[group_id] = cancel_event
```

This is unconditional — replaces whatever was there. If
`_run_program_threaded` planted EventP for this gid and is mid-run, this
overwrites it with EventS. Program runner reads `dict.get(gid)` per iteration
→ now sees EventS, not EventP. If anyone calls `cancel_group_jobs(gid)` to
abort the program, line 1503 sets EventS — but the program runner is now
gated on a different EventS that the SEQUENCE owns. Convoluted but works for
abort: both threads will see the .set() on EventS.

The breakage is on *cleanup*: `_run_program_threaded`'s finally pops gid →
sequence's EventS becomes orphaned in the dict (well, popped from the dict),
sequence's `cancel_event` local is fine but now `is_group_session_active`
returns False even though the sequence is still running.

**Required fix** is the symmetric one: gate line 1197 on a similar
`setdefault` or use a small lock that guards both write sites. Spec §6.7
already foresaw this; the §6.4 fix made the case marginally worse by adding a
second writer.

### C3. The reproducer integration test is weaker than spec §4.3 #10 demanded.

Spec §4.3 #10 required:

> Verify zones 2 and 3 NEVER reach state='on' (poll for the full duration
> that would have elapsed if the bug persisted, plus margin)

The actual test `test_full_watering_cycle_zone_stop_aborts_sequence`
(`tests/integration/test_session_abort_issue16.py:20-101`) does NOT do this.
In TESTING mode `_run_group_sequence` short-circuits to "set zone 1 ON, return"
(`irrigation_scheduler.py:1256-1282`) — the sequencer thread never enters the
`while remaining > 0:` loop, so the bug's user-visible effect (zones 2 and 3
auto-starting) cannot occur regardless of fix. The test instead asserts
`cancel_event.is_set()` flipped, which is the *mechanism* that prevents the
bug, not the *outcome* the spec wants.

The test is honest about it (docstring lines 23-40 explain), and the assertion
IS meaningful — I verified it fails with a clear "BUG: api_zone_mqtt_stop did
not abort the group session" message when the fix is reverted. So it's not a
tautology. But it's not the integration test the spec asked for either.

**Recommended action:** keep this test, also add a real integration test that
either (a) bypasses the TESTING short-circuit for this one test by directly
invoking `_run_group_sequence` on a thread, or (b) spins up a 3-zone group
with very short durations and asserts on `db.get_zone(z2).state == 'off'` and
`db.get_zone(z3).state == 'off'` after `(total_duration + margin)` seconds.

Until then, the spec's §4.3 #10 acceptance criterion is technically unmet.
This is the difference between "I'm sure the fix works" (mechanism asserted)
and "I'm sure the bug doesn't reproduce" (outcome asserted). For an issue
filed about user-visible behaviour, you want the latter.

---

## Non-blocking findings (should fix, can do follow-up)

### N1. The `test_zone_stop_during_scheduled_program_aborts_program_run` test races and rarely catches anything.

`tests/integration/test_session_abort_issue16.py:137-188`. The test starts
`_run_program_threaded` in a daemon thread and then polls
`is_group_session_active` in 100ms ticks for 2 seconds. Two problems:

- The thread spends maybe a few hundred microseconds in the pre-register block
  (the slow part is the per-iteration sleeps that happen AFTER pre-register).
  In TESTING mode the program weather-skips immediately or runs the truncated
  6-second loop; either way, by the time the polling loop's first 100ms
  tick fires, the pre-register block has often already finished AND its
  finally block has popped the gid.
- The test ends with `assert not t.is_alive()` and a docstring saying "the
  real assertion is that `_run_program_threaded` does NOT throw and does
  pre-register" — but there is no assertion on pre-registration having
  happened. The test is effectively `assert program_runner_does_not_crash`,
  which is a much weaker contract than "§6.4 fix lands the cancel-event in
  the dict."

The unit test `test_pre_register_cleans_up_after_run` covers this contract
correctly without timing fragility. Recommend deleting test #12 entirely
(spec §4.3 #12 said "mark xfail until §6.4 is implemented; flip when ready"
— now §6.4 is implemented, the contract is testable directly via the unit
test, and this integration test adds nothing but a 1-2s of test runtime).

### N2. Singleton `init_scheduler` shared across API tests can leak `group_cancel_events` state.

`init_scheduler` returns the module-level singleton on subsequent calls
(`irrigation_scheduler.py:1683-1710`). API tests `init_scheduler(app.db)`
returns the existing instance with whatever `group_cancel_events` were planted
by prior tests. Group ids are autoincrement-fresh per DB so collisions are
unlikely, but the test isolation is implicit, not explicit.

Concrete risk: if a future test doesn't drop its planted Event, a flaky
"why did `is_group_session_active` return True" failure becomes hard to
diagnose. Add a `clear()` of `group_cancel_events` in test setup or convert
to per-test scheduler instances (the unit tests already do this — they
construct `IrrigationScheduler(test_db)` directly).

Not blocking, but worth a small followup.

### N3. Default `group_id=1` in legacy tests now triggers the new abort path on `/api/zones/1/stop` if any test plants `group_cancel_events[1]`.

The pre-existing `test_stop_zone` at `tests/api/test_zones_api.py:83-89`
creates a zone with `group_id=1` (default group) and expects 200. If a prior
test in the same run planted `group_cancel_events[1]` and didn't clean it up
(see N2), this test will now route through `cancel_group_jobs` instead of
solo stop. Both paths return 200 so the test still passes — but for the
wrong reason. Same risk surface as N2; consolidating fixture cleanup fixes
both.

### N4. `record_audit` is already best-effort internally — endpoint-level `try/except Exception` wraps are redundant.

`services/audit.py:395-416` has its own `except Exception` that logs and
swallows. The added wrappers in
`routes/zones_watering_api.py:127`, `:496`, and `routes/groups_api.py:174`
add a second wide-net catch + `logger.exception`. Net effect: a single audit
failure logs twice. Harmless, just noise. Defensible if you want belt-and-
braces; remove if you want clarity. No action required.

### N5. `session_aborted_by_user` is emitted even when the group has no active session (group_stop endpoint).

`routes/groups_api.py:165-175` emits the audit unconditionally — there is no
`is_group_session_active(group_id)` gate. This matches a literal reading of
spec §3.5 ("Behaviour is unchanged; only the audit signal is added"), so
it's spec-compliant, but it does mean clicking the group-stop button on a
group that wasn't running emits a `session_aborted_by_user` row for a
session that never was. In analytics, this conflates "user cancelled an
active session" with "user pressed stop just to be sure." Minor noise.

If desired, gate on `if scheduler.is_group_session_active(int(group_id)):`
to keep the audit signal pure — but that's a spec change, not a defect.

### N6. Comments reference the wrong line numbers.

`irrigation_scheduler.py:543-544`: "Mirrors the lifecycle in `_run_group_sequence` (lines 1409-1418)."
The actual finally block in `_run_group_sequence` is now at lines 1460-1469
(after several edits to that file). Lines 1409-1418 are inside the per-zone
countdown loop, not the cleanup. Comment will rot further every time
`_run_group_sequence` shifts. Drop the explicit line numbers; just say "the
`finally` block of `_run_group_sequence`".

Same nit at `irrigation_scheduler.py:788`.

---

## Things I verified that are fine

- **Spec §3.2** — `is_group_session_active` matches spec body exactly,
  including the `int()` coerce + try/except. `irrigation_scheduler.py:1478-1490`.
- **Spec §3.3** — `api_zone_mqtt_stop` adds session-active short-circuit BEFORE
  the legacy solo path; preserves the central-stop + direct-MQTT fallback
  branch unchanged for the solo case. `routes/zones_watering_api.py:467-538`.
- **Spec §3.4** — `stop_zone` (legacy) gets the symmetric short-circuit with a
  distinct `endpoint='api_zone_stop'` audit token.
  `routes/zones_watering_api.py:101-154`.
- **Spec §3.5** — `api_stop_group` audit emit is added after `cancel_group_jobs`.
  `routes/groups_api.py:160-175`. Emission is unconditional (see N5 above).
- **Audit resilience** — both endpoints wrap `record_audit` in `try/except`
  (redundant per N4 but defensible). `record_audit` itself is best-effort
  per `services/audit.py:415`. Audit failure cannot block the abort.
- **Solo-zone stop regression** — pre-existing `test_stop_zone` and
  `test_zone_mqtt_stop_solo_does_not_call_cancel_group_jobs` both pass,
  confirming the solo path is unchanged when no session is active.
- **Security: type coercion** — `gid = int(z.get('group_id') or 0)` correctly
  defangs any non-int value (the cast raises ValueError on `"1; DROP"` and
  is wrapped in `try/except` upstream). Confirmed with the helper's own
  unit test `test_is_group_session_active_handles_string_input`.
- **Security: log injection** — audit payload contains only literal strings
  (`'api_zone_mqtt_stop'`, `'api_zone_stop'`, `'api_stop_group'`) and ints
  (`zone_id`, `gid`) — no user-controlled body fields reach the audit row.
- **Frontend compatibility** — only `data.success` and `data.message` are
  consumed at `static/js/status.js:914-918`, `:1979`, `:2330` and
  `static/js/zones.js:1156`. The new additive `session_aborted: True` /
  `state: 'off'` fields are ignored by all four call sites. No template uses
  the legacy `/api/zones/<id>/stop` endpoint.
- **Test #4.3 #10 reproducer fails on main** — verified by reverting only the
  endpoint changes (keeping the helper) and re-running the test: it fails
  with the meaningful assertion message "BUG: api_zone_mqtt_stop did not
  abort the group session". So the test, while weaker than spec asked, IS
  catching the right bug. (Reverting all three files fails earlier with
  AttributeError on `is_group_session_active`, which is a tautology — but
  that's only the on-`origin/main` shape, not the test's contract.)
- **Commit hygiene** — 6 commits, ordered logically:
  `b6e5400` add helper → `675c7e5` red test → `e9b7dfb` fix endpoints
  (turns red→green) → `a0b22a3` §6.4 pre-register → `22aeff2` more tests
  → `828f699` group_stop audit. The red commit at `675c7e5` is intentional
  per its commit message; bisect lands cleanly on `e9b7dfb` for the
  red→green transition. No commit leaves the tree in a broken state for
  unit/api tests.
- **Test suite** — 7 unit + 9 integration/api new tests pass on the branch
  HEAD; no regression in the 88 pre-existing zones/groups/scheduler-area
  tests. (One unrelated flake in `tests/api/test_coverage_boost.py::TestLoginLogout::test_login_with_password`
  exists on `origin/main` too — not introduced by this branch.)
- **`scheduler/program_runner.py` is dead** — confirmed: only one importer
  (itself), and `IrrigationScheduler` does not inherit it. Spec §7
  acknowledges this; no symmetric fix needed there.
- **Edge cases the spec called out** — §6.1 (race during inter-zone gap),
  §6.2 (double-tap), §6.3 (zone already off), §6.5 (multi-group programs),
  §6.6 (stop_all_in_group republishes OFF) all handled correctly by reusing
  `cancel_group_jobs`. No regressions.

---

## Summary for the writer

The user-facing behaviour fix (single-zone stop now aborts the whole group
session, audit row emitted) lands cleanly and the spec §3.2/§3.3/§3.4/§3.5
shape is right. The §6.4 implementation has a real concurrency footgun (C1)
that should be fixed before merge — it's a cousin of the bug being fixed.
C2 is a pre-existing latent issue that §6.4 made marginally worse and
deserves a coordinated fix. C3 weakens the integration-test acceptance
criterion the spec laid out; the test still has signal but isn't the
"watch-the-bug-not-reproduce" test the spec demanded.

Once C1 is corrected (small, isolated change — one `setdefault` swap and a
tuple-tracking tweak) this is APPROVE.

---

# Re-review (iter 2)

**Verdict: APPROVE**

Senior pushed 4 commits (`65a8250..cd91acc`) addressing the three blockers and N1.
Targeted re-verification below.

## Per-item check

### C1 — `_run_program_threaded` setdefault + identity-tracked cleanup ✅ FIXED
`irrigation_scheduler.py:546` — `registered_gids: List[Tuple[int, threading.Event]]`.
`irrigation_scheduler.py:565-568` — atomic `setdefault`; only tracks `(gid, new_event)` when
`planted is new_event`.
`irrigation_scheduler.py:793-797` — finally pops only when `dict.get(gid) is our_event`.

Race walk (two concurrent program runs, overlapping gids):
- A `setdefault(2, ev_A2)` → planted, tracks `(2, ev_A2)`.
- B `setdefault(2, ev_B2)` → returns `ev_A2`, identity check fails, B does NOT track gid=2.
- A's finally pops gid=2 (still holds `ev_A2`); B's finally never touches gid=2.

Original orphan/double-pop scenarios cannot reproduce. Stale line-number refs
("lines 1409-1418") replaced with "the `finally` block of `_run_group_sequence`".

### C2 — `start_group_sequence` symmetric setdefault ✅ FIXED
`irrigation_scheduler.py:1205-1212` — `setdefault(group_id, new_cancel_event)` with
`sequence_owns_event` tracking and an info log when reusing.
`irrigation_scheduler.py:1304-1307` — `cancel_event` capture moved BEFORE the
weather-skip early-return so finally always has it bound.
`irrigation_scheduler.py:1490-1492` — finally only pops when `dict.get(group_id) is cancel_event`.

Race walk (program runner plants ev_P, then sequence runs same gid):
- Sequence's `setdefault` returns existing `ev_P`, sets `sequence_owns_event=False`,
  logs reuse.
- `_run_group_sequence` continues with `cancel_event = ev_P` — the same Event the program
  runner is checking. Cancel signal flows correctly to both.
- Whichever finally runs second finds `dict.get` returns None and silently no-ops.
  No KeyError, no stomp.

Note (💭 nit, not blocking): the comment claims "the planter pops" but the actual
behavior is "last-finisher pops" — both threads have ev_P in their local var, so
both pass the identity check the first time it's run; only one actually executes the
pop because the other will then see None. Semantically equivalent for the C2 contract
(no different-owner stomp), but the comment slightly overstates determinism.
There is also a theoretical narrow window where the sequence pops while the program
runner is still iterating — `is_group_session_active(gid)` would briefly return False
for that gid even though work is in flight. That's a degenerate config (manual
sequence concurrent with scheduled program on same gid) and is not the C2 contract;
mentioning for awareness, not a blocker.

### C3 — outcome-asserting reproducer ✅ FIXED
`tests/integration/test_session_abort_issue16.py:138-249` —
`test_zone_stop_aborts_full_sequence_outcome` runs the genuine `_run_group_sequence`
per-zone loop (not a mock), polls every 50ms for 5s, and asserts zones 2/3 NEVER
reach `state='on'` at any tick. Test passes.

The senior introduced a NEW test-only env var bypass:
`SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ` at `irrigation_scheduler.py:1277`.
Production-side risk audit:
- Guard is `if TESTING and not os.environ.get(...)`. `TESTING=False` in production →
  the outer condition is False regardless of env var, so the env var has ZERO effect
  in prod. Even if accidentally set, it can't trigger the test-only branch because
  the production code path doesn't traverse this `if` block at all.
- Comment at lines 1271-1276 explicitly marks it test-only with
  "Production never sets this env var".
- Variable name is self-documenting (`SKIP_TESTING_*`).
- Doubly-gated, named clearly, commented — safe.

### N1 — racy test removed; coverage preserved ✅ DONE
`test_zone_stop_during_scheduled_program_aborts_program_run` deleted (cd91acc).
`tests/unit/test_session_abort.py:58` — `test_pre_register_cleans_up_after_run`
asserts both gids popped from `group_cancel_events` after `_run_program_threaded`
returns. `tests/unit/test_session_abort.py:81` — `test_pre_register_does_not_replace_existing_event`
plants an Event, runs the program, asserts the SAME object (`is`) still present.
These two tests assert the contract the deleted test only "documented" — strict
improvement.

### N6 — comments updated ✅ DONE
Stale "lines 1409-1418" reference removed. New comments accurately describe the
setdefault + identity-tracked cleanup pattern. Commit messages are detailed and
correct.

### Parallel suite green ✅
`bash scripts/run_tests_parallel.sh` from `/opt/claude-agents/irrigation`:
**1743 passed, 15 skipped, 29 xfailed, 55 xpassed, rc=0, 81.73s** (within ~80s budget).
Targeted run of issue-16 tests (10 tests) all pass in 11.09s.

## Final word

All three blockers and N1 land cleanly. New env-var bypass is properly gated and
documented. Two unit tests now lock down the pre-register contract. Outcome test
is genuine and assertive. Suite green. Ship it.
