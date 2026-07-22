"""Regression guards for the zone optimistic-lock frontend contract."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ZONES = ROOT / "static/js/zones.js"
STATUS = ROOT / "static/js/status.js"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
    assert match, f"{name}() not found"

    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = match.end() - 1
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char == "/" and nxt == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and nxt == "*":
            block_comment = True
            index += 2
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : index + 1]
        index += 1
    raise AssertionError(f"unbalanced function {name}")


def test_every_direct_zone_put_sends_the_loaded_database_version() -> None:
    zones_source = _source(ZONES)
    status_source = _source(STATUS)
    direct_puts = [
        line
        for line in (zones_source + "\n" + status_source).splitlines()
        if "api.put(" in line and "/api/zones/" in line
    ]
    assert len(direct_puts) == 3, direct_puts

    inline_save = _function(zones_source, "saveZone")
    duration_change = _function(status_source, "changeZoneDur")
    duration_save = _function(status_source, "saveZoneDuration")
    sheet_save = _function(status_source, "saveZoneEdit")

    assert "expected_version: zone.version" in inline_save
    assert "expectedVersion: pending ? pending.expectedVersion : z.version" in duration_change
    assert "expectedVersion = pending.expectedVersion" in duration_save
    assert "expected_version: expectedVersion" in duration_save
    assert "expectedVersion = zone.version" in sheet_save
    assert "expected_version: expectedVersion" in sheet_save

    # zoneStateVersions is an SSE ordering counter, not the database CAS token.
    assert "zoneStateVersions" not in inline_save + duration_save + sheet_save


def test_zone_cas_error_codes_are_recognized_at_runtime() -> None:
    for path in (ZONES, STATUS):
        helper = _function(_source(path), "isZoneCasConflict")
        script = f"""
{helper}
process.stdout.write(JSON.stringify([
  isZoneCasConflict({{error_code: 'ZONE_VERSION_CONFLICT'}}),
  isZoneCasConflict({{error_code: 'EXPECTED_VERSION_REQUIRED'}}),
  isZoneCasConflict({{error_code: 'INVALID_DURATION'}}),
  isZoneCasConflict(null),
]));
"""
        completed = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(completed.stdout) == [True, True, False, False]


def test_conflicts_warn_reload_and_short_circuit_success_paths() -> None:
    zones_source = _source(ZONES)
    status_source = _source(STATUS)

    zones_recovery = _function(zones_source, "recoverFromZoneCasConflict")
    status_recovery = _function(status_source, "recoverFromZoneCasConflict")
    assert "'warning'" in zones_recovery
    assert "await loadData()" in zones_recovery
    assert "'warning'" in status_recovery
    assert "await loadZonesData()" in status_recovery
    assert "closeZoneSheet()" in status_recovery

    for body in (
        _function(zones_source, "saveZone"),
        _function(status_source, "saveZoneDuration"),
        _function(status_source, "saveZoneEdit"),
    ):
        conflict = body.index("isZoneCasConflict")
        soft_error = body.index("success === false")
        assert conflict < soft_error
        assert "recoverFromZoneCasConflict" in body[conflict:soft_error]
        assert "return" in body[conflict:soft_error]


def test_successful_zone_writes_advance_only_the_database_version() -> None:
    zones_source = _source(ZONES)
    status_source = _source(STATUS)
    inline_save = _function(zones_source, "saveZone")
    duration_save = _function(status_source, "saveZoneDuration")
    sheet_save = _function(status_source, "saveZoneEdit")

    assert "version: result.version" in inline_save
    assert "current.version = data.version" in duration_save
    assert "version: data.version" in sheet_save
    for body in (inline_save, duration_save, sheet_save):
        assert "Number.isInteger" in body
        assert "zoneStateVersions" not in body


def test_debounced_duration_writes_survive_polling_and_are_serialized_per_zone() -> None:
    source = _source(STATUS)
    change = _function(source, "changeZoneDur")
    queue = _function(source, "queueZoneDurationSave")
    load = _function(source, "loadZonesData")
    apply_pending = _function(source, "applyPendingZoneDurations")
    save = _function(source, "saveZoneDuration")

    assert "queueZoneDurationSave(id)" in change
    assert "pendingZoneDurations[id]" in change
    assert "++zonesDataRevision" in change
    assert "expectedVersion: pending ? pending.expectedVersion : z.version" in change
    assert "durWriteInFlight[id]" in queue
    assert "durWritePending[id]" in queue
    assert "saveZoneDuration(id)" in queue
    assert "applyPendingZoneDurations(results[0])" in load
    assert "pendingZoneDurations[zone.id]" in apply_pending
    assert "zone.version = pending.expectedVersion" in apply_pending
    assert "durationRevision" in save
    assert "expectedVersion = pending.expectedVersion" in save
    assert "pendingZoneDurations[id].expectedVersion = data.version" in save
    assert "zonesDataRevision += 1" in save


def test_table_autosave_serializes_edits_and_keeps_newer_revision_dirty() -> None:
    source = _source(ZONES)
    update = _function(source, "updateZone")
    save = _function(source, "saveZone")
    schedule = _function(source, "scheduleZoneAutoSave")
    queue = _function(source, "queueZoneAutoSave")

    assert "__zoneEditRevisions[zoneId]" in update
    assert "saveRevision" in save
    assert "hasNewerEdit" in save
    assert "__zoneSavePending[zoneId] = true" in save
    assert "queueZoneAutoSave(zoneId)" in schedule
    assert "__zoneSaveInFlight[zoneId]" in queue
    assert "__zoneSavePending[zoneId]" in queue


def test_state_resync_refreshes_db_version_without_using_sse_counter_as_cas_token() -> None:
    source = _source(ZONES)
    resync = _function(source, "resyncZoneStates")
    guard = _function(source, "canApplyResyncedZoneVersion")

    assert "localZone.version = zone.version" in resync
    assert "canApplyResyncedZoneVersion" in resync
    assert "zoneStateVersions" in resync
    assert "expected_version" not in resync
    assert "modifiedZones.has(zoneId)" in guard
    assert "__zoneSaveInFlight[zoneId]" in guard
    assert "__zoneSavePending[zoneId]" in guard
    assert "incomingVersion >= localZone.version" in guard


def test_poll_snapshot_keeps_pending_duration_at_runtime() -> None:
    helper = _function(_source(STATUS), "applyPendingZoneDurations")
    script = f"""
const pendingZoneDurations = {{7: {{duration: 23, revision: 4, expectedVersion: 7}}}};
{helper}
const polled = [{{id: 7, duration: 10, version: 8}}];
applyPendingZoneDurations(polled);
process.stdout.write(JSON.stringify(polled[0]));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout) == {"id": 7, "duration": 23, "version": 7}


def test_zone_resync_neither_rebases_pending_edit_nor_downgrades_success_at_runtime() -> None:
    guard = _function(_source(ZONES), "canApplyResyncedZoneVersion")
    script = f"""
const zonesData = [{{id: 7, version: 5}}];
const modifiedZones = new Set();
const __zoneSaveInFlight = {{}};
const __zoneSavePending = {{}};
{guard}
const staleAfterSuccess = canApplyResyncedZoneVersion(7, 4);
const cleanNewer = canApplyResyncedZoneVersion(7, 6);
modifiedZones.add(7);
const concurrentWhilePending = canApplyResyncedZoneVersion(7, 6);
modifiedZones.delete(7);
__zoneSaveInFlight[7] = true;
const concurrentWhileInFlight = canApplyResyncedZoneVersion(7, 6);
process.stdout.write(JSON.stringify({{
  staleAfterSuccess,
  cleanNewer,
  concurrentWhilePending,
  concurrentWhileInFlight,
}}));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout) == {
        "staleAfterSuccess": False,
        "cleanNewer": True,
        "concurrentWhilePending": False,
        "concurrentWhileInFlight": False,
    }


def test_table_autosave_queue_never_overlaps_puts_at_runtime() -> None:
    source = _source(ZONES)
    cancel = _function(source, "cancelZoneAutoSave")
    queue = _function(source, "queueZoneAutoSave")
    script = f"""
const __zoneSaveTimers = {{}};
const __zoneSaveInFlight = {{}};
const __zoneSavePending = {{}};
const resolvers = [];
let calls = 0;
function saveZone() {{
  calls += 1;
  return new Promise(resolve => resolvers.push(resolve));
}}
{cancel}
{queue}
(async () => {{
  queueZoneAutoSave(7);
  queueZoneAutoSave(7);
  const beforeFirstCompletes = calls;
  resolvers.shift()(true);
  await new Promise(resolve => setImmediate(resolve));
  const afterFirstCompletes = calls;
  resolvers.shift()(true);
  await new Promise(resolve => setImmediate(resolve));
  process.stdout.write(JSON.stringify({{
    beforeFirstCompletes,
    afterFirstCompletes,
    finalCalls: calls,
    inFlight: __zoneSaveInFlight[7],
    pending: __zoneSavePending[7],
  }}));
}})();
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout) == {
        "beforeFirstCompletes": 1,
        "afterFirstCompletes": 2,
        "finalCalls": 2,
        "inFlight": False,
        "pending": False,
    }
