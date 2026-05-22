"""Regression tests for issue #44: /zones inline save lied.

zones.js::saveZone() ранее спредил весь объект зоны (~27 полей), включая
state-machine поля. Бэк отвергал такие payload с 400, но фронт показывал
зелёный тост 'Зона сохранена' из-за truthy-проверки на error-объекте.

Эти тесты ловят регрессию:
1. payload содержит только редактируемые поля
2. ответ проверяется через result.success !== false, а не через truthy
"""

import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read_file(relpath):
    with open(os.path.join(PROJECT_ROOT, relpath), encoding="utf-8") as f:
        return f.read()


def _extract_save_zone(js):
    """Return the body of zones.js::saveZone()."""
    m = re.search(r"async function saveZone\(zoneId\)\s*\{", js)
    assert m, "saveZone function not found"
    start = m.end()
    depth = 1
    i = start
    while i < len(js) and depth > 0:
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
        i += 1
    return js[start:i]


class TestZonesInlineSave:
    def test_save_zone_does_not_spread_full_zone(self):
        """saveZone must NOT spread the full zone object into the PUT payload."""
        js = read_file("static/js/zones.js")
        body = _extract_save_zone(js)
        # Original bug: `const updatedZone = { ...zone, name: ... }` shipped
        # 27 fields including state-machine ones. The PUT payload object must
        # not start with `...zone` (the loop-local zone var); spreading
        # `...zonesData[i]` for in-memory state is fine.
        bad = re.search(r"\{\s*\.\.\.zone\b(?!sData)", body)
        assert bad is None, (
            "saveZone() spreads full zone object → state-machine fields leak into PUT payload → 400"
        )

    def test_save_zone_sends_only_editable_fields(self):
        """Payload must contain only fields the user can edit on /zones."""
        js = read_file("static/js/zones.js")
        body = _extract_save_zone(js)
        # Editable: name, duration, group_id, icon, topic (optional), mqtt_server_id (optional)
        for field in ("name:", "duration:", "group_id:", "icon:"):
            assert field in body, f"editable field {field!r} missing from saveZone payload"
        # Must NOT mention state-machine / read-only fields
        for forbidden in ("state:", "commanded_state", "observed_state", "fault_count", "last_fault"):
            assert forbidden not in body, f"saveZone leaks state-machine field {forbidden!r} into payload"

    def test_save_zone_checks_success_false(self):
        """Result must be checked via result.success !== false, not truthy."""
        js = read_file("static/js/zones.js")
        body = _extract_save_zone(js)
        # Backend on error returns `{success: false, message: ...}` — a truthy object.
        # Frontend must distinguish; `if (success)` on the raw result is the bug.
        assert "result.success === false" in body, (
            "saveZone must check `result.success === false` to detect error envelope"
        )
        # The old `if (success)` pattern (or `if (result)`) without success-check is the bug
        assert "if (success)" not in body, "old truthy check `if (success)` still present"

    def test_save_zone_shows_error_on_failure(self):
        js = read_file("static/js/zones.js")
        body = _extract_save_zone(js)
        # Some error notification path must exist for the failure branch
        assert "Ошибка сохранения" in body or "Ошибка автосохранения" in body
