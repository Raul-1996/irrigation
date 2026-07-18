"""Regression contracts for the MQTT settings page (phase 2 package P12)."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _mqtt_html() -> str:
    return (PROJECT_ROOT / "templates" / "mqtt.html").read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    marker = f"function {name}("
    function_start = source.index(marker)
    body_start = source.index("{", function_start)
    depth = 0
    for index in range(body_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[body_start + 1 : index]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def test_status_polling_has_one_replaceable_timer_chain():
    html = _mqtt_html()
    update_body = _function_body(html, "updateStatuses")
    scheduler_body = _function_body(html, "scheduleStatusPolling")
    load_body = _function_body(html, "loadServers")

    assert "setTimeout" not in update_body
    assert "let statusPollTimer = null" in html
    assert "clearTimeout(statusPollTimer)" in scheduler_body
    assert "statusPollTimer = setTimeout" in scheduler_body
    assert "await updateStatuses()" in scheduler_body
    assert "await updateStatuses()" in load_body
    assert "scheduleStatusPolling()" in load_body
    assert "setTimeout(updateStatuses" not in html


def test_display_regex_compiles_plus_before_escaping_literals():
    body = _function_body(_mqtt_html(), "buildDisplayRegex")

    assert "char === '+'" in body
    assert "pattern += '[^/]+'" in body
    assert "char === '#'" in body
    assert "char === '*'" in body
    assert "replace(/\\+/g" not in body


def test_port_parser_rejects_invalid_values_before_api_calls():
    html = _mqtt_html()
    parser_body = _function_body(html, "parseMqttPort")
    create_body = _function_body(html, "createServer")
    update_body = _function_body(html, "updateRow")

    assert "Number.isInteger(port)" in parser_body
    assert "port < 1 || port > 65535" in parser_body
    assert "showNotification" in parser_body
    assert "parseMqttPort" in create_body
    assert "if (port === null) return" in create_body
    assert "parseMqttPort" in update_body
    assert "if (mqttPort === null) return" in update_body


def test_create_and_update_surface_structured_api_errors():
    html = _mqtt_html()
    helper_body = _function_body(html, "mqttErrorMessage")
    create_body = _function_body(html, "createServer")
    update_body = _function_body(html, "updateRow")

    assert "response.message" in helper_body
    assert "response.error" in helper_body
    assert "mqttErrorMessage(res" in create_body
    assert "mqttErrorMessage(res" in update_body
