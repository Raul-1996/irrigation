"""Regression guards for the Phase 2 status-page frontend fixes."""

from pathlib import Path

STATUS_JS = Path(__file__).parents[2] / "static" / "js" / "status.js"


def _source() -> str:
    return STATUS_JS.read_text(encoding="utf-8")


def _function(name: str) -> str:
    """Return a named JavaScript function, including its balanced body."""
    source = _source()
    start = source.index(f"function {name}(")
    body_start = source.index("{", start)
    depth = 0
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    index = body_start
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
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
        if char == "/" and next_char == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
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
                return source[start : index + 1]
        index += 1
    raise AssertionError(f"Unbalanced JavaScript function: {name}")


def test_server_time_normalizes_legacy_space_datetime_for_webkit():
    body = _function("syncServerTime")
    assert ".replace(' ','T')" in body or '.replace(" ","T")' in body


def test_zone_load_does_not_turn_transport_errors_into_empty_configuration():
    body = _function("loadZonesData")
    assert "assertJsonResponse" in body
    assert "return [];" not in body


def test_optimistic_timestamps_keep_their_explicit_utc_designator():
    source = _source()
    assert "toISOString().slice" not in source
    assert "function optimisticTimestamp" in source


def test_zone_control_server_failure_always_hides_loading_overlay():
    body = _function("toggleZoneRun")
    failure_branch = body.index("} else {")
    catch_branch = body.index("}).catch", failure_branch)
    assert "hideLoading();" in body[failure_branch:catch_branch]


def test_zone_edit_treats_explicit_soft_error_as_failure():
    body = _function("saveZoneEdit")
    assert "data.success === false" in body
    assert "data.message" in body


def test_status_zone_icon_and_derived_label_are_escaped():
    body = _function("renderZoneCards")
    assert "escapeHtml(z.icon || '🌿')" in body
    assert "escapeHtml(t.label)" in body


def test_dial_drag_handlers_are_installed_only_once():
    body = _function("initDialDrag")
    assert "__dialDragInitialized" in body
    assert "if (svg.__dialDragInitialized) return;" in body


def test_environment_probe_remembers_exhaustion_until_sensor_recovers():
    source = _source()
    body = _function("updateStatusDisplay")
    assert "envProbeExhausted" in source
    assert "!envProbeExhausted" in body
    assert "envProbeExhausted = true" in body


def test_optimistic_zone_state_is_merged_over_overlapping_poll_results():
    source = _source()
    load_body = _function("loadZonesData")
    confirm_body = _function("confirmRun")
    assert "pendingZoneStates" in source
    assert "applyPendingZoneStates" in load_body
    assert "rememberOptimisticZoneState" in confirm_body
    assert "reconcileOptimisticZoneState" in confirm_body


def test_all_group_run_paths_inspect_each_server_result():
    source = _source()
    confirm_body = _function("confirmRun")
    defaults_body = _function("confirmRunWithDefaults")
    assert "runAllGroups" in confirm_body
    assert "runAllGroups" in defaults_body
    assert "function runAllGroups" in source
    assert "okCount" in _function("reportGroupRunResults")


def test_control_fetch_error_paths_surface_failure_and_hide_overlay():
    confirm_body = _function("confirmRun")
    defaults_body = _function("confirmRunWithDefaults")
    assert ".catch(function()" in confirm_body
    assert "hideLoading();" in confirm_body
    assert ".catch(function()" in defaults_body
    assert "hideLoading();" in defaults_body
