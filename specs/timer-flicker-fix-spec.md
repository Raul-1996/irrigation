# Timer/Progress Bar Flicker Fix — Specification

## Problem
Status page polls every 5 seconds, destroys and recreates all DOM via `innerHTML`. This causes:
1. Timers flash `--:--` / `00:00` for ~0.5s on each cycle
2. Progress bars jump to 0% then back
3. Group cards flash/re-animate

## Root Causes & Fixes

### 1. Full innerHTML re-render → DOM patching

**Affected functions:**
- `renderZoneCards()` — currently rebuilds all zone cards via `c.innerHTML = html`
- `updateStatusDisplay()` — currently does `container.innerHTML = ''` then rebuilds group cards

**Fix:**
- On **first render** (container empty or zone/group count changed): use innerHTML as before
- On **subsequent renders**: find existing elements by `data-zone-id` / `data-group-id` and patch only changed attributes/text
- Elements managed by `tickCountdowns()` (`.zc-running-timer`, `.zc-progress-bar`, `.group-timer`) are **NOT touched** during polling updates — only their `data-*` attributes are updated so tickCountdowns picks up new values

**New data-attributes used:**
- Zone cards: `data-zone-id` (already exists), `data-zone-state`, `data-zone-duration`
- Group cards: `data-group-id` (already exists), `data-group-status`

### 2. Double render in `loadZonesData()` → Single render after all data

**Current flow:**
1. Fetch zones → `renderZoneCards()` (shows `—` for next watering)
2. Fetch next-watering-bulk → `renderZoneCards()` (shows correct times)

**New flow:**
1. `Promise.all([fetchZones, fetchNextWateringBulk])` — parallel
2. Merge next-watering data into zonesData
3. Single `renderZoneCards()` call
4. If next-watering-bulk fails → use cached `_nextWatering` from previous cycle (don't wipe)

### 3. Safari date parsing → `parseDate()` helper

**New function at top of file:**
```js
function parseDate(s) {
    if (!s) return null;
    var d = new Date(String(s).replace(' ', 'T'));
    return isNaN(d.getTime()) ? null : d;
}
```

**Usage locations (replace `new Date(...)` calls):**
- `initGroupTimer()` — `new Date(zone.planned_end_time)`
- `initZoneTimer()` — `new Date(zone.planned_end_time)`, `new Date(zone.watering_start_time)`
- `renderZoneCards()` — inline timer computation: `new Date(z.planned_end_time)`, `new Date(z.watering_start_time)`
- `updateActiveZoneIndicator()` — `new Date(active.planned_end_time)`, `new Date(active.watering_start_time)`
- `tickCountdowns()` — **NOT changed** (per spec), but its `new Date()` calls on zone data will benefit from parseDate being used upstream to set correct data-attributes

### 4. `Math.random()` in group cards → Deterministic

**Current:** `const flowActive = group.status === 'watering' && Math.random() > 0.3;`
**New:** `const flowActive = group.status === 'watering';`

**Locations:**
- `updateStatusDisplay()` — 1 occurrence
- `refreshSingleGroup()` — 1 occurrence

### 5. Timer freeze when app is backgrounded (iOS/mobile)

**Problem:** When user switches away from the browser, the OS freezes JS timers. `tickCountdowns()` uses `sec--` (decrement by 1), so when the user returns after N minutes the timer still shows the stale value from when they left.

**Solution:** New standalone function `recalcTimersFromRealTime()` + `visibilitychange` handler:

```js
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    recalcTimersFromRealTime();
    Promise.all([loadStatusData(), loadZonesData()]).catch(function(){});
  }
});
```

`recalcTimersFromRealTime()` does:
1. Finds all `.zc-running-timer` elements
2. Gets the zone's `planned_end_time` from `zonesData`
3. Calculates `remaining = Math.max(0, Math.floor((parseDate(planned_end_time) - Date.now()) / 1000))`
4. Updates `el.dataset.remainingSeconds` and `el.textContent`
5. Same for `.group-timer` elements (using `data-zone-id` to look up `planned_end_time`)
6. Updates progress bars accordingly

**Note:** `tickCountdowns()` itself is NOT modified. `recalcTimersFromRealTime()` is a new, separate function.

### 6. Polling interval 5s → 30s

**Current:** `setInterval(() => { Promise.all([loadStatusData(), loadZonesData()]) }, 5000)`
**New:** `setInterval(..., 30000)`

### 7. Error resilience — preserve last known state

**Current:** On fetch error in `loadZonesData()`, `showConnectionError()` is called but zonesData may be empty
**New:** On fetch error, keep previous `zonesData` and `statusData`, don't re-render with empty data

## Functions Modified

| Function | Change |
|---|---|
| `parseDate()` | **NEW** — date parsing helper |
| `recalcTimersFromRealTime()` | **NEW** — recalculates all timers from real time on visibility change |
| `renderZoneCards()` | DOM patching on re-render |
| `updateStatusDisplay()` | DOM patching for group cards |
| `loadZonesData()` | Single render after all data fetched |
| `updateStatusDisplay()` | Remove Math.random() |
| `refreshSingleGroup()` | Remove Math.random() |
| `initGroupTimer()` | Use parseDate() |
| `initZoneTimer()` | Use parseDate() |
| `updateActiveZoneIndicator()` | Use parseDate() |
| DOMContentLoaded handler | Change interval to 30s, add visibilitychange handler |

## Functions NOT Modified

| `tickCountdowns()` | Drift correction added (parseDate + Math.abs threshold > 2) for both zone and group timers |

## Functions NOT Modified (beyond drift correction)

| Function | Reason |
| All business logic functions | Not related to rendering |
| CSS/styles | Not related |
| API endpoints | Not related |

## Architecture

```
Polling (30s) → updates JS data-store + data-attributes in DOM
                ↓
tickCountdowns (1s) → reads data-attributes, updates timer text + progress bar width
                ↓
visibilitychange → recalcTimersFromRealTime() fixes drift, then fetches fresh data
                ↓
First render → innerHTML
Subsequent → DOM patching (find by data-zone-id / data-group-id, update text/classes/attrs)
```
