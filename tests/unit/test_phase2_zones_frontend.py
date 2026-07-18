"""Regression coverage for the Phase 2 /zones frontend fixes."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ZONES_JS = PROJECT_ROOT / "static/js/zones.js"


def _source() -> str:
    return ZONES_JS.read_text(encoding="utf-8")


def _function(source: str, name: str, *, async_function: bool = False) -> str:
    prefix = r"async\s+function" if async_function else r"function"
    match = re.search(rf"{prefix}\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
    assert match, f"{name}() not found"

    depth = 1
    index = match.end()
    while index < len(source) and depth:
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
        index += 1
    assert depth == 0, f"could not extract {name}()"
    return source[match.start() : index]


def test_untrusted_zone_and_mqtt_labels_are_escaped() -> None:
    source = _source()
    render_zones = _function(source, "renderZonesTable")
    render_groups = _function(source, "renderGroupsGrid")

    assert "${escapeHtml(zone.icon || '🌿')}" in render_zones
    assert "${zone.icon}" not in render_zones
    assert render_groups.count("${escapeHtml(s.name)}") == 3
    assert re.search(r"\$\{s\.name\}", render_groups) is None


def test_zone_without_mqtt_server_has_an_explicit_empty_option() -> None:
    render_zones = _function(_source(), "renderZonesTable")

    assert '<option value=""' in render_zones
    assert "zone.mqtt_server_id == null" in render_zones


def test_delete_zone_uses_the_http_status_for_empty_204() -> None:
    delete_zone = _function(_source(), "deleteZone", async_function=True)

    assert "method: 'DELETE'" in delete_zone
    assert "response.ok" in delete_zone
    assert "api.delete" not in delete_zone


def test_group_setting_writes_reject_http_and_success_false_errors() -> None:
    source = _source()
    helper = _function(source, "putGroupSettings", async_function=True)

    assert "response.ok" in helper
    assert "data.success === false" in helper
    for name in (
        "toggleGroupUseMaster",
        "scheduleAutoSave",
        "saveGroupMasterTopic",
        "saveGroupMasterMode",
        "saveGroupMasterCloseDelay",
    ):
        body = _function(source, name, async_function=name != "scheduleAutoSave")
        assert "putGroupSettings" in body, f"{name} still bypasses status-aware writes"


def test_sse_open_resynchronizes_zone_state() -> None:
    source = _source()
    resync = _function(source, "resyncZoneStates", async_function=True)

    assert "api.get('/api/zones')" in resync
    assert "es.onopen" in source
    assert "resyncZoneStates" in source[source.index("es.onopen") : source.index("es.onopen") + 300]


def test_group_modal_identity_is_preserved_across_grid_render() -> None:
    source = _source()
    render_groups = _function(source, "renderGroupsGrid")
    close_modal = _function(source, "closeModalById")

    assert "preservedModals" in render_groups
    assert "replacement.remove()" in render_groups
    assert "card.appendChild(m)" in close_modal


def test_csv_export_round_trips_commas_quotes_and_newlines() -> None:
    source = _source()
    encode = _function(source, "encodeCSVCell")
    parse = _function(source, "parseCSV")
    rows = [
        ["id", "name", "icon", "topic"],
        ["5", "Газон, север", '<img title="x">', "/topic\nnext"],
    ]
    program = f"""
{encode}
{parse}
const rows = {json.dumps(rows, ensure_ascii=False)};
const csv = rows.map(row => row.map(encodeCSVCell).join(',')).join('\\n');
process.stdout.write(JSON.stringify(parseCSV(csv)));
"""
    completed = subprocess.run(
        ["node", "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == rows
    export_zones = _function(source, "exportZonesCSV")
    import_zones = _function(source, "handleCSVImport", async_function=True)
    assert "encodeCSVCell" in export_zones
    assert "parseCSV" in import_zones
