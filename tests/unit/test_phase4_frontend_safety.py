"""Regression contracts for the post-review frontend safety package."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATUS = ROOT / "static/js/status.js"
ZONES = ROOT / "static/js/zones.js"
HISTORY = ROOT / "static/js/history.js"
SETTINGS = ROOT / "templates/settings.html"
PROGRAMS = ROOT / "templates/programs.html"
MQTT = ROOT / "templates/mqtt.html"
LOGS = ROOT / "templates/logs.html"
MAP = ROOT / "templates/map.html"
SW = ROOT / "static/sw.js"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
    assert match, f"{name}() not found"
    body_start = source.index("{", match.start())
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = body_start
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


def test_selected_run_popup_resets_minutes_mode_and_percent_ui() -> None:
    body = _function(_source(STATUS), "confirmRunSelectedNext")
    assert "runPopupMode = 'min'" in body
    assert "runPopupPct = null" in body
    assert "_refreshRunPopupModeUI()" in body


def test_status_polling_uses_latest_request_and_feed_specific_errors() -> None:
    source = _source(STATUS)
    status = _function(source, "loadStatusData")
    zones = _function(source, "loadZonesData")
    assert "statusRequestGeneration" in status
    assert "requestGeneration !== statusRequestGeneration" in status
    assert "zonesRequestGeneration" in zones
    assert "requestGeneration !== zonesRequestGeneration" in zones
    assert "showConnectionError('status')" in status
    assert "hideConnectionError('status')" in status
    assert "showConnectionError('zones')" in zones
    assert "hideConnectionError('zones')" in zones
    assert "invalidateLiveDataRequests" in _function(source, "emergencyStop")
    assert "invalidateLiveDataRequests" in _function(source, "resumeSchedule")


def test_mqtt_warning_state_respects_disabled_unknown_and_degraded_health() -> None:
    helper = _function(_source(STATUS), "deriveMqttWarningState")
    script = f"""
{helper}
const cases = [
  deriveMqttWarningState({{
    mqtt_servers_count: 1,
    mqtt_enabled_count: 0,
    mqtt_connected: false,
    mqtt_health: {{status: 'disabled'}},
  }}),
  deriveMqttWarningState({{
    mqtt_servers_count: 1,
    mqtt_enabled_count: 1,
    mqtt_connected: false,
    mqtt_health: {{status: 'unknown'}},
  }}),
  deriveMqttWarningState({{
    mqtt_servers_count: 1,
    mqtt_enabled_count: 1,
    mqtt_connected: false,
    mqtt_health: {{status: 'degraded'}},
  }}),
  deriveMqttWarningState({{
    mqtt_servers_count: 1,
    mqtt_enabled_count: 1,
    mqtt_connected: true,
    mqtt_health: {{status: 'ok'}},
  }}),
  deriveMqttWarningState({{
    mqtt_servers_count: 1,
    mqtt_enabled_count: 1,
    mqtt_connected: false,
  }}),
];
console.log(JSON.stringify(cases));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    disabled, unknown, degraded, healthy, legacy_disconnected = json.loads(result.stdout)
    assert disabled == {"noServers": False, "connectionProblem": False, "degraded": False}
    assert unknown == {"noServers": False, "connectionProblem": False, "degraded": False}
    assert degraded == {"noServers": False, "connectionProblem": True, "degraded": True}
    assert healthy == {"noServers": False, "connectionProblem": False, "degraded": False}
    assert legacy_disconnected == {"noServers": False, "connectionProblem": True, "degraded": False}


def test_zones_sse_resync_cannot_overwrite_a_newer_event() -> None:
    source = _source(ZONES)
    load = _function(source, "loadData")
    resync = _function(source, "resyncZoneStates")
    assert "loadDataGeneration" in load
    assert "zoneStateVersions" in load
    assert "versionNow !== versionBefore" in load
    assert "versionNow === versionBefore" in resync
    assert "es.onerror" in source
    assert "zoneStateFeedFailed" in source


def test_zone_control_response_cannot_overwrite_a_newer_sse_event() -> None:
    source = _source(ZONES)
    for name in ("startZone", "stopZone"):
        body = _function(source, name)
        assert "stateVersionAtRequest" in body
        assert "zoneStateVersions.get(zoneId)" in body
        assert "=== stateVersionAtRequest" in body


def test_duration_mutations_validate_and_conflict_check_fail_closed() -> None:
    source = _source(ZONES)
    bulk = _function(source, "applyBulkAction")
    imported = _function(source, "handleCSVImport")
    checker = _function(source, "checkDurationConflicts")
    assert "parseZoneDuration" in bulk
    assert "parseZoneDuration" in imported
    assert "response.ok" in checker
    assert "missing" in checker
    assert "throw new Error" in checker
    assert "checkDurationConflicts" in bulk
    assert "checkDurationConflicts" in imported


def test_active_hardware_mapping_is_locked_in_zone_and_mqtt_ui() -> None:
    zones = _source(ZONES)
    mqtt = _source(MQTT)
    assert "isZoneHardwareLocked" in _function(zones, "renderZonesTable")
    assert "hardwareLocked ? 'disabled'" in _function(zones, "renderZonesTable")
    assert "isGroupHardwareLocked" in _function(zones, "putGroupSettings")
    assert "isZoneHardwareLocked" in _function(zones, "deleteZone")
    assert "mqttServerIsActive" in _function(mqtt, "updateRow")
    assert "mqttServerIsActive" in _function(mqtt, "deleteServer")


def test_bulk_delete_requires_confirmation_and_reports_each_response() -> None:
    body = _function(_source(ZONES), "applyBulkAction")
    assert "confirm(" in body
    assert "response.ok" in body
    assert "success === false" in body
    assert "failedIds" in body


def test_csv_ids_are_canonical_and_cells_are_formula_neutralized() -> None:
    source = _source(ZONES)
    canonical = _function(source, "parseCanonicalPositiveInt")
    encode = _function(source, "encodeCSVCell")
    program = f"""
{canonical}
{encode}
const values = ['1', '01', '1junk', '0', '-1', '=2+2', '+cmd', 'normal'];
process.stdout.write(JSON.stringify({{
  parsed: values.slice(0, 5).map(v => parseCanonicalPositiveInt(v)),
  encoded: values.slice(5).map(v => encodeCSVCell(v))
}}));
"""
    completed = subprocess.run(["node", "-e", program], check=True, capture_output=True, text=True)
    result = json.loads(completed.stdout)
    assert result["parsed"] == [1, None, None, None, None]
    assert result["encoded"][0].startswith("'") or result["encoded"][0].startswith("\"'")
    assert result["encoded"][1].startswith("'") or result["encoded"][1].startswith("\"'")
    assert result["encoded"][2] == "normal"


def test_program_execution_tab_loads_authoritative_status() -> None:
    source = _source(PROGRAMS)
    switch = _function(source, "switchTab")
    load = _function(source, "loadExecutionState")
    assert "loadExecutionState" in switch
    assert "/api/status" in load
    assert "group.status === 'watering'" in load
    assert "current_zone" in load


def test_program_enabled_payload_and_wizard_defaults_use_json_booleans() -> None:
    source = _source(PROGRAMS)
    toggle = _function(source, "toggleEnabled")
    edit = _function(source, "editProgram")
    create = _function(source, "openWizard")
    assert "const newEnabled = !Boolean(prog.enabled)" in toggle
    assert "enabled: Boolean(p.enabled)" in edit
    assert "enabled: true" in create


def test_password_confirmation_and_whitespace_contract() -> None:
    source = _source(SETTINGS)
    assert 'id="new_password2"' in source
    assert 'id="new_password2" name="new_password2" minlength="8" maxlength="32" required' in source
    assert "document.getElementById('old_password').value.trim()" in source
    assert "document.getElementById('new_password').value.trim()" in source
    assert "document.getElementById('new_password2').value.trim()" in source
    assert "new_password !== new_password2" in source


def test_geo_detection_does_not_violate_connect_src_self() -> None:
    source = _source(SETTINGS)
    assert "ipapi.co" not in source
    assert "{timeout:5000}" not in source


def test_mqtt_tree_is_prototype_safe_and_enforces_count_and_byte_caps() -> None:
    source = _source(MQTT)
    build = _function(source, "buildTreeFromTopics")
    store = _function(source, "storeTopicValue")
    clear = _function(source, "clearBrowser")
    assert "Object.create(null)" in build
    assert "Map" in source
    assert "MAX_TOPIC_COUNT" in store
    assert "MAX_TOPIC_BYTES" in store
    assert "resetTopicState" in store
    assert "resetTopicState" in clear


def test_mqtt_topic_store_counts_replacements_and_resets_at_hard_limits() -> None:
    source = _source(MQTT)
    functions = "\n".join(
        _function(source, name)
        for name in ("topicEntryBytes", "resetTopicState", "storeTopicValue", "buildTreeFromTopics")
    )
    program = f"""
const MAX_TOPIC_COUNT = 2;
const MAX_TOPIC_BYTES = 40;
const MAX_TOPIC_DEPTH = 4;
let lastTopicMap = new Map();
let topicStateBytes = 0;
let pendingCount = 0;
let expandedPaths = new Set();
let rebuildTimer = null;
const notices = [];
const document = {{ getElementById: () => null }};
function appendLog(value) {{ notices.push(value); }}
{functions}
storeTopicValue('/a', 'x');
storeTopicValue('/a', 'yy');
const exactReplacementBytes = topicStateBytes === topicEntryBytes('/a', 'yy');
storeTopicValue('/b', 'z');
storeTopicValue('/c', 'z');
const tree = buildTreeFromTopics([{{topic:'/__proto__/polluted', payload:'x'}}]);
process.stdout.write(JSON.stringify({{
  exactReplacementBytes,
  keys: Array.from(lastTopicMap.keys()),
  resetNotice: notices.some(value => value.includes('сброшено')),
  nullPrototype: Object.getPrototypeOf(tree) === null && Object.getPrototypeOf(tree.children) === null,
  globalPolluted: ({{}}).polluted !== undefined
}}));
"""
    completed = subprocess.run(["node", "-e", program], check=True, capture_output=True, text=True)
    result = json.loads(completed.stdout)
    assert result == {
        "exactReplacementBytes": True,
        "keys": ["/c"],
        "resetNotice": True,
        "nullPrototype": True,
        "globalPolluted": False,
    }


def test_map_cache_key_is_stable_and_service_worker_prunes_maps() -> None:
    map_source = _source(MAP)
    sw_source = _source(SW)
    assert "Date.now()" not in _function(map_source, "loadMap")
    assert "it.mtime" in _function(map_source, "loadMap")
    assert "MAX_MAP_CACHE_ENTRIES" in sw_source
    assert "putBoundedMapResponse" in sw_source
    assert "url.pathname.startsWith('/static/media/maps/')" in sw_source


def test_history_and_log_filters_ignore_stale_responses() -> None:
    history = _source(HISTORY)
    logs = _source(LOGS)
    refresh = _function(history, "refresh")
    close = _function(history, "close")
    assert "refreshGeneration" in refresh
    assert "generation !== state.refreshGeneration" in refresh
    assert "state.refreshGeneration" in close
    assert "eventRequestGeneration" in _function(logs, "applyFilter")
    assert "auditRequestGeneration" in _function(logs, "loadAudit")


def test_history_day_totals_use_backend_counts_as_actual_truth() -> None:
    helper = _function(_source(HISTORY), "summarizeActualRuns")
    script = f"""
{helper}
process.stdout.write(JSON.stringify(summarizeActualRuns([
  {{status: 'aborted', confirmed: false, counts_as_actual: false, duration_min: 10}},
  {{status: 'failed', confirmed: false, counts_as_actual: false, duration_min: 7}},
  {{status: 'ok', confirmed: true, counts_as_actual: true, duration_min: 4.5}},
])));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout) == {"count": 1, "minutes": 4.5}


def test_owned_frontend_paths_do_not_treat_soft_error_objects_as_success() -> None:
    zones = _source(ZONES)
    mqtt = _source(MQTT)
    assert "function responseSucceeded" in zones
    assert "responseSucceeded" in _function(zones, "createZone")
    assert "responseSucceeded" in _function(zones, "deleteGroup")
    assert "mqttErrorMessage" in _function(mqtt, "deleteServer")


@pytest.mark.parametrize("path", ["/zones", "/settings", "/programs", "/mqtt", "/map", "/logs"])
def test_rendered_inline_javascript_parses(path: str, admin_client) -> None:
    response = admin_client.get(path)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, flags=re.DOTALL | re.IGNORECASE)
    inline = "\n;\n".join(script for script in scripts if script.strip())
    completed = subprocess.run(
        ["node", "--check", "-"],
        input=inline,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
