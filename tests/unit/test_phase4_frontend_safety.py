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
    # The regex ends on the function body's opening brace.  Using index()
    # from the signature start breaks on default object parameters (`= {}`).
    body_start = match.end() - 1
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
    mqtt_health: {{status: 'healthy'}},
  }}),
  deriveMqttWarningState({{
    mqtt_servers_count: 0,
    mqtt_enabled_count: 0,
    mqtt_connected: false,
    mqtt_health: {{status: 'degraded', error_code: 'MQTT_SECRET_UNAVAILABLE'}},
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
    disabled, unknown, degraded, healthy, credentials_error, legacy_disconnected = json.loads(result.stdout)
    assert disabled == {"noServers": False, "connectionProblem": False, "degraded": False}
    assert unknown == {"noServers": False, "connectionProblem": False, "degraded": False}
    assert degraded == {"noServers": False, "connectionProblem": True, "degraded": True}
    assert healthy == {"noServers": False, "connectionProblem": False, "degraded": False}
    assert credentials_error == {"noServers": False, "connectionProblem": True, "degraded": True}
    assert legacy_disconnected == {"noServers": False, "connectionProblem": True, "degraded": False}


def test_single_group_refresh_shares_global_status_request_generation() -> None:
    body = _function(_source(STATUS), "refreshSingleGroup")
    assert "++statusRequestGeneration" in body
    assert "requestGeneration !== statusRequestGeneration" in body
    assert "statusData = data" in body
    assert "updateStatusDisplay()" in body


def test_single_group_refresh_renders_the_entire_winning_status_snapshot() -> None:
    source = _source(STATUS)
    functions = "\n".join(
        _function(source, name) for name in ("assertJsonResponse", "loadStatusData", "refreshSingleGroup")
    )
    script = f"""
let statusData = null;
let statusRequestGeneration = 0;
const cards = Object.create(null);
const pending = [];
function deferredResponse() {{
  let resolve;
  const promise = new Promise(done => {{ resolve = done; }});
  return {{promise, resolve}};
}}
function fetch() {{
  const deferred = deferredResponse();
  pending.push(deferred);
  return deferred.promise;
}}
function response(payload) {{
  return {{ok: true, status: 200, json: async () => payload}};
}}
function updateStatusDisplay() {{
  statusData.groups.forEach(group => {{ cards[group.id] = group.status; }});
}}
function updateWaterMeter() {{}}
function updateZoneStats() {{}}
function hideConnectionError() {{}}
function showConnectionError() {{}}
function updateMqttWarnings() {{}}
const zonesData = [];
{functions}
const globalPoll = loadStatusData();
const focusedRefresh = refreshSingleGroup(1);
pending[1].resolve(response({{groups: [
  {{id: 1, status: 'watering'}},
  {{id: 2, status: 'postponed'}},
]}}));
focusedRefresh.then(() => {{
  pending[0].resolve(response({{groups: [
    {{id: 1, status: 'waiting'}},
    {{id: 2, status: 'waiting'}},
  ]}}));
  return globalPoll;
}}).then(() => process.stdout.write(JSON.stringify({{cards, statusData}})));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    result = json.loads(completed.stdout)
    assert result["cards"] == {"1": "watering", "2": "postponed"}
    assert [group["status"] for group in result["statusData"]["groups"]] == ["watering", "postponed"]


def test_water_widgets_use_authoritative_controller_day_total_only() -> None:
    source = _source(STATUS)
    helper = _function(source, "getAuthoritativeWaterToday")
    meter = _function(source, "updateWaterMeter")
    stats = _function(source, "updateZoneStats")
    script = f"""
{helper}
process.stdout.write(JSON.stringify([
  getAuthoritativeWaterToday({{water_today: {{liters: 12.5, date: '2026-07-19', source: 'zone_runs', has_data: true}}}}),
  getAuthoritativeWaterToday({{water_today: {{liters: 99, date: '2026-07-19', source: 'none', has_data: false}}}}),
  getAuthoritativeWaterToday({{water_today: {{liters: 0, source: 'unavailable', error_code: 'WATER_REPORT_UNAVAILABLE'}}}}),
  getAuthoritativeWaterToday({{water_today: null}}),
]));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout) == [
        {"available": True, "liters": 12.5, "date": "2026-07-19", "source": "zone_runs"},
        {"available": True, "liters": 0, "date": "2026-07-19", "source": "none"},
        {"available": False, "liters": None, "date": None, "source": "unavailable"},
        {"available": False, "liters": None, "date": None, "source": None},
    ]
    assert "water_today" in helper
    assert "last_total_liters" not in meter
    assert "last_total_liters" not in stats


def test_status_inner_html_escapes_dynamic_group_and_weather_strings() -> None:
    source = _source(STATUS)
    extra = _function(source, "renderGroupExtraHtml")
    display = _function(source, "updateStatusDisplay")
    single = _function(source, "refreshSingleGroup")
    script = f"""
function escapeHtml(value) {{
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}}
{extra}
const postponed = renderGroupExtraHtml({{
  id: 7,
  status: 'postponed',
  postpone_until: '<img src=x onerror=alert(1)>',
  postpone_reason: '<svg onload=alert(2)>',
}}, []);
const failed = renderGroupExtraHtml({{
  id: 8,
  status: 'error',
  error_message: '<script>alert(3)</script>',
}}, []);
process.stdout.write(JSON.stringify({{postponed, failed}}));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    rendered = json.loads(completed.stdout)
    assert "<img" not in rendered["postponed"]
    assert "<svg" not in rendered["postponed"]
    assert "&lt;img" in rendered["postponed"]
    assert "<script" not in rendered["failed"]
    assert "&lt;script" in rendered["failed"]
    assert "renderGroupExtraHtml" in display
    assert "updateStatusDisplay" in single
    assert "escapeHtml(group.error_message)" in extra
    for name in (
        "renderForecast24h",
        "renderForecast3d",
        "renderWeatherDetails",
        "renderWeatherFactors",
        "renderWeatherHistory",
    ):
        assert "escapeHtml" in _function(source, name)


def test_weather_renderers_neutralize_html_payloads_at_runtime() -> None:
    source = _source(STATUS)
    helpers = "\n".join(
        _function(source, name)
        for name in (
            "formatTemp",
            "formatWeatherNumber",
            "formatWeatherMetric",
            "renderForecast24h",
            "renderWeatherDetails",
            "renderWeatherFactors",
        )
    )
    script = f"""
function escapeHtml(value) {{
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}}
function getWeatherIcon() {{ return 'safe'; }}
function weatherReasonPhrase() {{ return ''; }}
const elements = {{
  'w-hours': {{innerHTML: ''}},
  'w-details': {{innerHTML: ''}},
  'w-factors': {{innerHTML: ''}},
}};
const document = {{getElementById(id) {{ return elements[id] || null; }}}};
{helpers}
renderForecast24h([{{
  time: '<img src=x onerror=alert(1)>',
  icon: '<svg onload=alert(2)>',
  temp: '<script>alert(3)</script>',
  precip: '<iframe>',
  wind: '<object>',
}}]);
renderWeatherDetails({{
  astronomy: {{sunrise: '<img src=x>', sunset: '<svg onload=x>'}},
  stats: {{precipitation_24h: '<script>', precipitation_forecast_6h: '<iframe>', daily_et0: '<object>'}},
}});
renderWeatherFactors({{factors: {{rain: {{status: 'warn', detail: '<img src=x onerror=x>'}}}}}});
process.stdout.write(JSON.stringify(elements));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    rendered = " ".join(item["innerHTML"] for item in json.loads(completed.stdout).values())
    for dangerous in ("<img", "<svg", "<script", "<iframe", "<object"):
        assert dangerous not in rendered
    assert "&lt;img" in rendered


def test_weather_ui_keeps_h1_applied_and_h2_shadow_status_truthful() -> None:
    status_source = _source(STATUS)
    second_opinion = _function(status_source, "renderCoeffSecondOpinion")
    summary = _function(status_source, "renderWeatherSummary")
    settings_source = _source(SETTINGS)
    script = f"""
function escapeHtml(value) {{ return String(value ?? '').replaceAll('<', '&lt;').replaceAll('>', '&gt;'); }}
{second_opinion}
process.stdout.write(JSON.stringify([
  renderCoeffSecondOpinion({{
    mode: 'shadow', coefficient_balance: 88, balance_status: 'fresh',
    balance_last_recalc_date: '2026-07-19', balance_age_days: 0,
  }}),
  renderCoeffSecondOpinion({{
    mode: 'shadow', coefficient_balance: 91, balance_status: 'stale',
    balance_last_recalc_date: '2026-07-16', balance_age_days: 3,
  }}),
  renderCoeffSecondOpinion({{
    mode: 'legacy', coefficient_balance: null, balance_status: 'unavailable',
    balance_last_recalc_date: null, balance_age_days: null,
  }}),
  renderCoeffSecondOpinion({{
    mode: 'balance', coefficient_balance: 120, balance_status: 'future',
    balance_last_recalc_date: '2026-07-22', balance_age_days: -3,
  }}),
]));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    fresh, stale, unavailable, future = json.loads(completed.stdout)
    for rendered in (fresh, stale, unavailable, future):
        assert "Зимм. (H1)" in rendered
        assert "H2 shadow" in rendered
        assert "Баланс" not in rendered
    assert "свеж" in fresh and "2026-07-19" in fresh
    assert "устар" in stale and "3" in stale and "2026-07-16" in stale
    assert "нет данных" in unavailable
    assert "будущ" in future and "2026-07-22" in future
    assert "coefficient_legacy" in summary
    assert 'id="balance_enabled"' not in settings_source
    assert "H2 работает только в теневом режиме" in settings_source
    assert "enabled: document.getElementById('balance_enabled')" not in settings_source


def test_rain_sensor_status_never_reports_dry_for_degraded_states() -> None:
    source = _source(STATUS)
    helper = _function(source, "rainSensorStatusText")
    display = _function(source, "updateStatusDisplay")
    script = f"""
{helper}
process.stdout.write(JSON.stringify([
  rainSensorStatusText({{rain_sensor_state: 'disabled', rain_sensor_online: false}}),
  rainSensorStatusText({{rain_sensor_state: 'offline', rain_sensor_online: false}}),
  rainSensorStatusText({{rain_sensor_state: 'unknown', rain_sensor_online: true}}),
  rainSensorStatusText({{rain_sensor_state: 'reconnecting', rain_sensor_online: false}}),
  rainSensorStatusText({{rain_sensor_state: 'dry', rain_sensor_online: false}}),
  rainSensorStatusText({{rain_sensor_state: 'rain', rain_sensor_online: true}}),
  rainSensorStatusText({{rain_sensor_state: 'dry', rain_sensor_online: true}}),
]));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    disabled, offline, unknown, reconnecting, stale_dry, rain, dry = json.loads(completed.stdout)
    assert "выключен" in disabled
    assert "нет связи" in offline and "заблокирован" in offline
    assert "нет данных" in unknown and "заблокирован" in unknown
    assert "подключ" in reconnecting and "заблокирован" in reconnecting
    assert "нет связи" in stale_dry and "дождя нет" not in stale_dry
    assert rain == "идёт дождь"
    assert dry == "дождя нет"
    assert all("дождя нет" not in value for value in (disabled, offline, unknown, reconnecting, stale_dry))
    assert "rainSensorStatusText(statusData)" in display


def test_global_rain_save_applies_authoritative_config_and_group_snapshot_before_render() -> None:
    source = _source(ZONES)
    save = _function(source, "saveRainConfig")
    script = f"""
const elements = {{
  'rain-enabled': {{checked: true}},
  'rain-type': {{value: 'NO'}},
  'rain-topic': {{value: '/rain', style: {{}}}},
  'rain-server': {{value: '7'}},
}};
const document = {{getElementById(id) {{ return elements[id] || null; }}}};
const window = {{rainConfig: {{enabled: true, topic: '/old'}}}};
let groupsData = [
  {{id: 1, use_rain_sensor: false}},
  {{id: 2, use_rain_sensor: true}},
  {{id: 999, use_rain_sensor: false}},
];
let requestPayload = null;
const rendered = [];
const notifications = [];
async function fetch(_url, options) {{
  requestPayload = JSON.parse(options.body);
  return {{
    ok: true,
    json: async () => ({{
      success: true,
      config: {{enabled: false, type: 'NC', topic: '/server', server_id: 8}},
      groups: [
        {{id: 1, use_rain_sensor: true}},
        {{id: 2, use_rain_sensor: false}},
      ],
    }}),
  }};
}}
function showNotification(message, level) {{ notifications.push([message, level]); }}
function initRainUi() {{}}
function updateGlobalToggleTitles() {{}}
function renderGroupsGrid() {{
  rendered.push({{
    config: {{...window.rainConfig}},
    groups: groupsData.map(group => [group.id, group.use_rain_sensor]),
  }});
}}
{save}
saveRainConfig().then(() => process.stdout.write(JSON.stringify({{
  requestPayload,
  config: window.rainConfig,
  groups: groupsData.map(group => [group.id, group.use_rain_sensor]),
  rendered,
  notifications,
}})));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    result = json.loads(completed.stdout)
    assert result["requestPayload"]["enabled"] is True
    assert result["config"] == {"enabled": False, "type": "NC", "topic": "/server", "server_id": 8}
    assert result["groups"] == [[1, True], [2, False], [999, False]]
    assert result["rendered"] == [{"config": result["config"], "groups": result["groups"]}]
    assert result["notifications"] == [["Конфигурация датчика дождя сохранена", "success"]]


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


def test_csv_input_is_reset_in_finally_on_every_validation_exit() -> None:
    body = _function(_source(ZONES), "handleCSVImport")
    assert "finally" in body
    assert "input.value = ''" in body


def test_water_calibration_uses_one_combined_autosave_and_live_probe_is_serialized() -> None:
    source = _source(ZONES)
    increment = _function(source, "waterIncDigit")
    flush = _function(source, "waterFlushSave")
    perform = _function(source, "performWaterSave")
    close = _function(source, "closeWaterSettings")
    live = _function(source, "waterStartLive")
    stop = _function(source, "waterStopLive")
    assert "scheduleWaterAutoSave" in increment
    assert "water_base_value_m3" in perform
    assert "water_base_pulses" in perform
    assert perform.count("putGroupSettings(") == 1
    assert "clearTimeout(__waterSaveTimers[groupId])" in flush
    assert "__waterSaveInFlight" in flush
    assert "active.promise" in flush
    assert "await waterFlushSave" in close
    assert "restartLive: false" in close
    assert "waterStopLive" in _function(source, "scheduleWaterAutoSave")
    assert "waterStartLive" in flush
    assert "__waterLiveInFlight" in live
    assert "setTimeout" in live
    assert "setInterval" not in live
    assert "generation" in live
    assert ".abort()" in stop


def test_water_close_reuses_the_current_autosave_request() -> None:
    source = _source(ZONES)
    perform = _function(source, "performWaterSave")
    flush = _function(source, "waterFlushSave")
    script = f"""
const __waterSaveTimers = {{}};
const __waterSaveRevisions = {{1: 4}};
const __waterSaveInFlight = {{}};
const __waterCalibrationOverrides = {{}};
const __waterClosing = {{1: true}};
const __waterState = {{1: {{editing: false}}}};
const groupsData = [];
const elements = {{
  'water-server-1': {{value: '3'}},
  'water-topic-1': {{value: '/meter'}},
  'water-pulse-1': {{value: '1l'}},
}};
const document = {{getElementById(id) {{ return elements[id] || null; }}}};
function waterDigitsToValue() {{ return 0; }}
function waterModalIsOpen() {{ return false; }}
function waterStartLive() {{ throw new Error('must not restart while closing'); }}
function showNotification() {{}}
let requestCount = 0;
let resolveRequest;
function putGroupSettings() {{
  requestCount += 1;
  return new Promise(resolve => {{ resolveRequest = resolve; }});
}}
{perform}
{flush}
const autosave = waterFlushSave(1);
const closeFlush = waterFlushSave(1, {{restartLive: false}});
resolveRequest({{ok: true}});
Promise.all([autosave, closeFlush]).then(results => {{
  process.stdout.write(JSON.stringify({{requestCount, results}}));
}});
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout) == {"requestCount": 1, "results": [True, True]}


def test_water_cancel_serializes_a_compensating_baseline_save() -> None:
    source = _source(ZONES)
    functions = "\n".join(_function(source, name) for name in ("waterCancel", "performWaterSave", "waterFlushSave"))
    script = f"""
const __waterSaveTimers = {{}};
const __waterSaveRevisions = {{1: 1}};
const __waterSaveInFlight = {{}};
const __waterCalibrationOverrides = {{}};
const __waterClosing = {{}};
const __waterState = {{1: {{editing: true, baseValueM3: 10, basePulses: 100, currentPulses: 200}}}};
const groupsData = [{{id: 1, water_base_value_m3: 10, water_base_pulses: 100}}];
const digits = [];
const elements = {{
  'water-server-1': {{value: '3'}},
  'water-topic-1': {{value: '/meter'}},
  'water-pulse-1': {{value: '1l'}},
  'water-pulses-1': {{value: '200'}},
  'water-actions-1': {{style: {{display: 'flex'}}}},
}};
const document = {{getElementById(id) {{ return elements[id] || null; }}}};
function waterDigitsToValue() {{ return 20; }}
function waterSetDigits(_groupId, value) {{ digits.push(value); }}
function waterModalIsOpen() {{ return false; }}
function waterStartLive() {{}}
function waterStopLive() {{}}
function showNotification() {{}}
const requests = [];
function putGroupSettings(_groupId, payload) {{
  let resolve;
  const promise = new Promise(done => {{ resolve = done; }});
  requests.push({{payload, resolve}});
  return promise;
}}
{functions}
const autosave = waterFlushSave(1, {{restartLive: false}});
const cancel = waterCancel(1);
requests[0].resolve({{ok: true}});
setImmediate(() => {{
  requests[1].resolve({{ok: true}});
  Promise.all([autosave, cancel]).then(() => process.stdout.write(JSON.stringify({{
    payloads: requests.map(request => request.payload),
    state: __waterState[1],
    group: groupsData[0],
    digits,
  }})));
}});
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    result = json.loads(completed.stdout)
    assert [payload["water_base_value_m3"] for payload in result["payloads"]] == [20, 10]
    assert [payload["water_base_pulses"] for payload in result["payloads"]] == [200, 100]
    assert result["state"]["baseValueM3"] == 10
    assert result["state"]["basePulses"] == 100
    assert result["state"]["editing"] is False
    assert result["group"]["water_base_value_m3"] == 10
    assert result["digits"][-1] == 10


def test_all_sensor_mqtt_selectors_can_preserve_explicit_null() -> None:
    source = _source(ZONES)
    groups = _function(source, "renderGroupsGrid")
    selectors = _function(source, "loadGroupSelectors")
    rain = _function(source, "initRainUi")
    env = _function(source, "initEnvUi")
    assert groups.count('<option value=""') >= 3
    assert "Не выбран" in groups
    assert "nullOption" in selectors
    assert "cfg.server_id == null ? ''" in rain
    assert "tempServerId == null ? ''" in env
    assert "humServerId == null ? ''" in env


def test_active_hardware_mapping_is_locked_in_zone_and_mqtt_ui() -> None:
    zones = _source(ZONES)
    mqtt = _source(MQTT)
    assert "isZoneHardwareLocked" in _function(zones, "renderZonesTable")
    assert "hardwareLocked ? 'disabled'" in _function(zones, "renderZonesTable")
    assert "isGroupHardwareLocked" in _function(zones, "putGroupSettings")
    assert "isZoneHardwareLocked" in _function(zones, "deleteZone")
    assert "mqttServerGuardReason" in _function(mqtt, "updateRow")
    assert "mqttServerGuardReason" in _function(mqtt, "deleteServer")


def test_mqtt_server_switch_retargets_scan_and_referenced_servers_are_locked() -> None:
    source = _source(MQTT)
    change = _function(source, "onServerChange")
    load = _function(source, "loadServers")
    update = _function(source, "updateRow")
    delete = _function(source, "deleteServer")
    assert "scanning" in change
    assert "startSSE()" in change
    assert "is_referenced" in load
    assert "references" in load
    assert "mqttServerGuardReason" in update
    assert "mqttServerGuardReason" in delete
    assert "if (scanning) startSSE()" in load


def test_mqtt_sse_retarget_ignores_queued_old_stream_callbacks() -> None:
    source = _source(MQTT)
    functions = "\n".join(_function(source, name) for name in ("stopSSE", "startSSE"))
    script = f"""
let scanning = true;
let sseSource = null;
let sseGeneration = 0;
let rebuildTimer = null;
let pendingCount = 0;
let lastTopicMap = new Map();
const stored = [];
const notices = [];
const sources = [];
const select = {{value: '1'}};
const elements = {{
  serverSelect: select,
  filterInput: {{value: ''}},
  scanBtn: {{textContent: ''}},
  topicTree: {{innerHTML: ''}},
}};
const document = {{getElementById(id) {{ return elements[id] || null; }}}};
class EventSource {{
  constructor(url) {{ this.url = url; this.closed = false; sources.push(this); }}
  close() {{ this.closed = true; }}
  addEventListener() {{}}
}}
function hasMqttWildcards() {{ return false; }}
function buildDisplayRegex() {{ return /.*/; }}
function storeTopicValue(topic, payload) {{ stored.push([topic, payload]); return true; }}
function buildTreeFromTopics() {{ return {{}}; }}
function renderTree() {{}}
function appendLog(value) {{ notices.push(value); }}
const setTimeout = () => 1;
{functions}
startSSE();
const oldSource = sources[0];
select.value = '2';
startSSE();
const currentSource = sources[1];
oldSource.onmessage({{data: JSON.stringify({{topic: '/old', payload: 'stale'}})}});
oldSource.onerror();
currentSource.onmessage({{data: JSON.stringify({{topic: '/new', payload: 'fresh'}})}});
process.stdout.write(JSON.stringify({{
  stored,
  scanning,
  oldClosed: oldSource.closed,
  currentClosed: currentSource.closed,
  currentIsGlobal: sseSource === currentSource,
  notices,
}}));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout) == {
        "stored": [["/new", "fresh"]],
        "scanning": True,
        "oldClosed": True,
        "currentClosed": False,
        "currentIsGlobal": True,
        "notices": [],
    }


def test_mqtt_reference_metadata_covers_every_backend_category() -> None:
    source = _source(MQTT)
    helpers = "\n".join(_function(source, name) for name in ("describeMqttReferences", "rememberMqttServerReferences"))
    script = f"""
let mqttServerReferenceInfo = new Map();
{helpers}
const result = rememberMqttServerReferences([{{
  id: 3,
  is_referenced: true,
  references: {{
    zones: [1],
    groups_master: [2],
    groups_pressure: [3],
    groups_water: [4],
    groups_float: [5],
    settings: ['rain'],
  }},
}}]).get(3);
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    result = json.loads(completed.stdout)
    assert result["referenced"] is True
    for label in (
        "зоны",
        "мастер-клапаны групп",
        "датчики давления групп",
        "счётчики воды групп",
        "поплавковые датчики групп",
        "системные настройки",
    ):
        assert label in result["reason"]


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


def test_program_wizard_does_not_offer_or_submit_unsupported_smart_type() -> None:
    source = _source(PROGRAMS)
    wizard = _function(source, "renderWizardStep")
    save = _function(source, "saveWizard")

    assert "wizardData.type = 'smart'" not in wizard
    assert "Smart Weather больше не поддерживается" in wizard
    assert '<span class="badge time-based">Time-Based</span>' in wizard
    assert "wizardData.type !== 'time-based'" in save


def test_legacy_smart_program_is_rendered_read_only_with_clear_warning() -> None:
    source = _source(PROGRAMS)
    safe_color = _function(source, "safeProgramColor")
    render = _function(source, "renderList")
    script = f"""
function escapeHtml(value) {{ return String(value ?? ''); }}
const DEFAULT_PROGRAM_COLOR = '#42a5f5';
{safe_color}
const container = {{innerHTML: ''}};
const document = {{getElementById() {{ return container; }}}};
const currentSort = 'time';
const zones = [];
const programs = [{{
  id: 7,
  name: 'Legacy smart',
  time: '06:00',
  type: 'smart',
  schedule_type: 'weekdays',
  days: [0],
  zones: [],
  enabled: true,
}}];
{render}
renderList();
process.stdout.write(container.innerHTML);
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    rendered = completed.stdout
    assert "Smart — не поддерживается" in rendered
    assert "Редактирование, включение, дублирование и запуск отключены" in rendered
    assert 'aria-disabled="true"' in rendered
    for action in ("toggleEnabled(7)", "editProgram(7)", "duplicateProgram(7)", "runProgram(7)"):
        assert action not in rendered
    assert "deleteProgram(7)" in rendered


def test_legacy_smart_actions_have_defensive_read_only_guards() -> None:
    source = _source(PROGRAMS)
    for name in ("toggleEnabled", "duplicateProgram", "runProgram", "editProgram"):
        assert "rejectUnsupportedProgramAction(id)" in _function(source, name)


def test_program_color_is_canonical_before_style_interpolation() -> None:
    source = _source(PROGRAMS)
    helper = _function(source, "safeProgramColor")
    render = _function(source, "renderList")
    script = f"""
const DEFAULT_PROGRAM_COLOR = '#42a5f5';
{helper}
process.stdout.write(JSON.stringify([
  safeProgramColor('#abcdef'),
  safeProgramColor('red;\" onmouseover=\"alert(1)'),
  safeProgramColor(null),
]));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout) == ["#abcdef", "#42a5f5", "#42a5f5"]
    assert "safeProgramColor(p.color)" in render


def test_program_legacy_time_is_escaped_in_list_wizard_and_summary() -> None:
    source = _source(PROGRAMS)
    render_list = _function(source, "renderList")
    render_wizard = _function(source, "renderWizardStep")
    safe_color = _function(source, "safeProgramColor")
    script = f"""
function escapeHtml(value) {{
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}}
const DEFAULT_PROGRAM_COLOR = '#42a5f5';
const COLORS = [DEFAULT_PROGRAM_COLOR];
{safe_color}
const listContainer = {{innerHTML: ''}};
const wizardContainer = {{innerHTML: ''}};
const document = {{getElementById(id) {{
  return id === 'list-content' ? listContainer : wizardContainer;
}}}};
const currentSort = 'name';
const zones = [];
const maliciousTime = '"><img src=x onerror=alert(1)>';
const programs = [{{
  id: 1,
  name: 'legacy',
  time: maliciousTime,
  type: 'time-based',
  schedule_type: 'weekdays',
  days: [],
  zones: [],
  enabled: true,
}}];
let wizardStep = 3;
const wizardData = {{
  name: 'legacy', type: 'time-based', color: DEFAULT_PROGRAM_COLOR,
  schedule_type: 'weekdays', days: [], interval_days: 3, even_odd: null,
  time: maliciousTime, zones: [], enabled: true,
}};
{render_list}
{render_wizard}
renderList();
renderWizardStep();
process.stdout.write(JSON.stringify({{
  list: listContainer.innerHTML,
  wizard: wizardContainer.innerHTML,
}}));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    rendered = json.loads(completed.stdout)
    assert "<img" not in rendered["list"]
    assert rendered["list"].count("&lt;img") == 1
    assert "<img" not in rendered["wizard"]
    assert rendered["wizard"].count("&lt;img") == 2
    assert 'value="&quot;&gt;&lt;img' in rendered["wizard"]
    assert _function(source, "renderWizardStep").count("escapeHtml(wizardData.time)") == 2


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
    availability = _function(source, "configureGeolocationAvailability")
    assert "window.isSecureContext" in availability
    assert "button.disabled" in availability
    assert "Введите координаты вручную" in availability


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


def test_map_ui_never_renders_more_than_the_retention_limit() -> None:
    source = _source(MAP)
    load = _function(source, "loadMap")
    script = f"""
const MAX_MAP_ITEMS = 20;
const container = {{
  children: [],
  innerHTML: '',
  appendChild(node) {{ this.children.push(node); }},
}};
function makeNode() {{
  return {{
    children: [],
    appendChild(node) {{ this.children.push(node); }},
    addEventListener() {{}},
    setAttribute() {{}},
  }};
}}
const document = {{
  getElementById(id) {{ return id === 'map-container' ? container : null; }},
  createElement() {{ return makeNode(); }},
}};
const window = {{AUTH_ROLE: 'guest'}};
const api = {{get: async () => ({{
  success: true,
  items: Array.from({{length: 27}}, (_, i) => ({{
    name: `map-${{i}}.webp`, path: `media/maps/map-${{i}}.webp`, mtime: 100 - i,
  }})),
}})}};
function showNotification() {{}}
{load}
loadMap().then(() => process.stdout.write(String(container.children.length)));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert completed.stdout == "20"


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


def test_failed_history_refresh_clears_every_stale_surface() -> None:
    source = _source(HISTORY)
    refresh = _function(source, "refresh")
    clear = _function(source, "clearHistoryView")
    assert "clearHistoryView" in refresh
    assert "state.lastData = null" in clear
    assert "state.chart.destroy()" in clear
    for element_id in (
        "historyTotalMinutes",
        "historyTotalRuns",
        "historyLitersCard",
        "historyChartEmpty",
        "historySavingsBanner",
        "historyNoPlanNote",
        "historyRunsList",
        "historyFooterStats",
        "historyTitle",
    ):
        assert element_id in clear


def test_history_savings_waits_for_backend_availability_contract() -> None:
    banner = _function(_source(HISTORY), "renderBanner")
    chart = _function(_source(HISTORY), "renderChart")
    assert "savings_available" in banner
    assert "plan_available" in banner
    assert "cohort_matches_current" in banner
    assert "savings_unavailable_reason" in banner
    assert "plan_unavailable" in banner
    assert "historical_zone_cohort_changed" in banner
    assert "actual_run_open" in banner
    assert "actuals_complete" in banner
    assert "plan_available" in chart


def test_failed_log_refresh_clears_stale_rows_and_export_state() -> None:
    source = _source(LOGS)
    events = _function(source, "clearEventLogsError")
    audit = _function(source, "clearAuditError")
    assert "logsData = []" in events
    assert "filteredLogs = []" in events
    assert "updateEventTypeOptions" in events
    assert "auditRows = []" in audit
    assert "auditStats" in audit
    assert "clearEventLogsError" in _function(source, "loadLogs")
    assert "clearEventLogsError" in _function(source, "applyFilter")
    assert "clearAuditError" in _function(source, "loadAudit")


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
