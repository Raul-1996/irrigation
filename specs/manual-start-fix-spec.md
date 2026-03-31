# Spec: Manual Zone Start — Timer Delay, Progress Bar, Group Override

## BUG 1: Timer Countdown Delay

### Problem
In `confirmRun()` and `toggleZoneRun()`, `planned_end_time` is set AFTER the fetch response.
The timer appears with a delay of several seconds.

### Fix (status.js)
- **`confirmRun()`**: Set `z.state`, `z.watering_start_time`, `z.planned_end_time` BEFORE fetch. Call `renderZoneCards()` immediately.
- **`toggleZoneRun()`**: Same — set optimistic `watering_start_time` and `planned_end_time` BEFORE fetch when `wantOn=true`.

## BUG 2: Progress Bar Shows 0%

### Problem
In `tickCountdowns()`, `total` is calculated from `zone.duration * 60` (base from DB).
When override duration > base → progress stays at 0% or goes negative.
Also: `loadZonesData()` every 30s resets local override back to DB base.

### Fix (status.js)
- **`tickCountdowns()`**: Calculate `total` from `(planned_end_time - watering_start_time)` instead of `zone.duration * 60`.
- **`initZoneTimer()`**: Same — use `(planned_end_time - watering_start_time)` for total in `applyTimer()`.

## BUG 3: Group Dial Changes Base Duration in DB

### Problem
`confirmRun()` for group does `api.put('/api/zones/' + z.id, { duration: dur })` — permanently changes duration in DB.

### Fix
- **Frontend (status.js)**: Remove the `Promise.all(groupZones.map(api.put...))`. Instead, pass `override_duration` in the `start-from-first` POST body.
- **Backend (groups_api.py)**: In `api_start_group_from_first()`, read `override_duration` from request body, pass to `scheduler.start_group_sequence()`.
- **Backend (irrigation_scheduler.py)**: In `start_group_sequence()` and `_run_group_sequence()`, accept `override_duration` param and use it instead of `zone.duration` when provided.

## Files Changed

| File | Changes |
|------|---------|
| `static/js/status.js` | BUG 1 + BUG 2 + BUG 3 frontend |
| `routes/groups_api.py` | BUG 3 backend — accept override_duration |
| `irrigation_scheduler.py` | BUG 3 backend — use override_duration |
