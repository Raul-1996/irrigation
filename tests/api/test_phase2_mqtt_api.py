"""Regression tests for Phase 2 MQTT API and repository safety contracts."""

from __future__ import annotations

import json
import ssl
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def test_findings_17_69_create_and_partial_update_never_echo_saved_secret(admin_client, app):
    created_response = admin_client.post(
        "/api/mqtt/servers",
        json={"name": "Secret", "host": "broker", "port": 1883, "password": "SuperSecret123"},
    )
    assert created_response.status_code == 201
    created = created_response.get_json()["server"]
    assert created["password"] == "***"
    assert "SuperSecret123" not in created_response.get_data(as_text=True)

    updated_response = admin_client.put(
        f"/api/mqtt/servers/{created['id']}",
        json={"name": "Secret renamed", "host": "broker-2"},
    )
    assert updated_response.status_code == 200
    assert updated_response.get_json()["server"]["password"] == "***"
    assert "SuperSecret123" not in updated_response.get_data(as_text=True)
    assert app.db.get_mqtt_server(created["id"])["password"] == "SuperSecret123"


def test_finding_52_api_returns_explicit_safe_lost_key_contract(admin_client):
    import routes.mqtt_api as mqtt_api
    import utils

    error_type = getattr(utils, "SecretDecryptionError", RuntimeError)
    with patch.object(mqtt_api.db, "get_mqtt_servers_strict", side_effect=error_type("unsafe crypto detail")):
        response = admin_client.get("/api/mqtt/servers")

    assert response.status_code == 500
    payload = response.get_json()
    assert payload["error_code"] == "MQTT_SECRET_UNAVAILABLE"
    assert "unsafe crypto detail" not in response.get_data(as_text=True)


def test_finding_53_update_invalidates_publisher_and_reloads_hub(admin_client, app):
    server = app.db.create_mqtt_server({"name": "Old", "host": "old-broker", "port": 1883})

    with (
        patch("services.mqtt_pub.invalidate_mqtt_server") as invalidate,
        patch("services.sse_hub.reload_hub") as reload_hub,
    ):
        response = admin_client.put(
            f"/api/mqtt/servers/{server['id']}",
            json={"name": "New", "host": "new-broker", "port": 1884},
        )

    assert response.status_code == 200
    invalidate.assert_called_once_with(server["id"])
    reload_hub.assert_called_once_with()


def test_finding_62_partial_put_preserves_tls_configuration(admin_client, app):
    server = app.db.create_mqtt_server(
        {
            "name": "TLS",
            "host": "secure-broker",
            "port": 8883,
            "tls_enabled": True,
            "tls_ca_path": "/etc/ssl/ca.pem",
            "tls_cert_path": "/etc/ssl/client.pem",
            "tls_key_path": "/etc/ssl/client.key",
            "tls_insecure": True,
            "tls_version": "TLS_CLIENT",
        }
    )

    response = admin_client.put(
        f"/api/mqtt/servers/{server['id']}",
        json={"name": "TLS renamed", "host": "secure-broker-2"},
    )

    assert response.status_code == 200
    updated = app.db.get_mqtt_server(server["id"])
    assert updated["tls_enabled"] == 1
    assert updated["tls_ca_path"] == "/etc/ssl/ca.pem"
    assert updated["tls_cert_path"] == "/etc/ssl/client.pem"
    assert updated["tls_key_path"] == "/etc/ssl/client.key"
    assert updated["tls_insecure"] == 1
    assert updated["tls_version"] == "TLS_CLIENT"


def test_finding_80_diagnostic_clients_use_unique_ephemeral_ids():
    import routes.mqtt_api as mqtt_api

    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(side_effect=lambda *args, **kwargs: MagicMock()),
    )
    configured = {"id": 80, "client_id": "live-sse-client"}

    assert hasattr(mqtt_api, "_new_diagnostic_client")
    with patch.object(mqtt_api, "mqtt", fake_mqtt):
        for purpose in ("probe", "status", "scan"):
            mqtt_api._new_diagnostic_client(configured, purpose)

    ids = [call.kwargs["client_id"] for call in fake_mqtt.Client.call_args_list]
    assert len(ids) == len(set(ids)) == 3
    assert "live-sse-client" not in ids
    assert all(client_id.startswith("wb-") and len(client_id) <= 23 for client_id in ids)


@pytest.mark.parametrize("purpose", ["probe", "status", "scan"])
def test_finding_81_diagnostic_client_applies_tls_before_route_connect(purpose):
    import routes.mqtt_api as mqtt_api

    events = []
    client = MagicMock(name=f"{purpose}_client")
    client.tls_set.side_effect = lambda **kwargs: events.append(("tls_set", kwargs))
    client.tls_insecure_set.side_effect = lambda value: events.append(("tls_insecure_set", value))
    client.connect.side_effect = lambda *args: events.append(("connect", args))
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(return_value=client),
    )
    server = {
        "id": 81,
        "host": "secure-broker",
        "port": 8883,
        "username": "mqtt-user",
        "password": "mqtt-password",
        "client_id": "live-sse-client",
        "tls_enabled": 1,
        "tls_ca_path": "/etc/ssl/ca.pem",
        "tls_cert_path": "/etc/ssl/client.pem",
        "tls_key_path": "/etc/ssl/client.key",
        "tls_insecure": 1,
        "tls_version": "TLS_CLIENT",
    }

    with patch.object(mqtt_api, "mqtt", fake_mqtt):
        configured = mqtt_api._new_diagnostic_client(server, purpose)
        configured.connect(server["host"], server["port"], 5)

    assert [event[0] for event in events] == ["tls_set", "tls_insecure_set", "connect"]
    tls_kwargs = events[0][1]
    assert tls_kwargs == {
        "ca_certs": "/etc/ssl/ca.pem",
        "certfile": "/etc/ssl/client.pem",
        "keyfile": "/etc/ssl/client.key",
        "tls_version": ssl.PROTOCOL_TLS_CLIENT,
    }
    client.username_pw_set.assert_called_once_with("mqtt-user", "mqtt-password")
    assert fake_mqtt.Client.call_args.kwargs["client_id"] != "live-sse-client"


def test_finding_81_tls_setup_failure_aborts_before_plaintext_connect():
    import routes.mqtt_api as mqtt_api

    client = MagicMock(name="tls_failure_client")
    client.tls_set.side_effect = OSError("invalid CA")
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(return_value=client),
    )
    server = {
        "id": 81,
        "host": "secure-broker",
        "port": 8883,
        "tls_enabled": 1,
        "tls_ca_path": "/missing/ca.pem",
        "tls_version": "TLS_CLIENT",
    }

    with patch.object(mqtt_api, "mqtt", fake_mqtt), pytest.raises(OSError, match="invalid CA"):
        configured = mqtt_api._new_diagnostic_client(server, "probe")
        configured.connect(server["host"], server["port"], 5)

    client.connect.assert_not_called()


@pytest.mark.parametrize("duration", ["not-a-number", "1e999", 0, -1, 31])
def test_finding_85_probe_duration_is_finite_and_bounded(admin_client, app, duration):
    import routes.mqtt_api as mqtt_api

    server = app.db.create_mqtt_server({"name": "Probe", "host": "broker", "port": 1883})
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(),
    )

    with patch.object(mqtt_api, "mqtt", fake_mqtt):
        response = admin_client.post(f"/api/mqtt/{server['id']}/probe", json={"duration": duration})

    assert response.status_code == 400
    assert response.get_json()["error_code"] == "INVALID_DURATION"
    fake_mqtt.Client.assert_not_called()


def test_finding_86_scan_timeout_ends_stream_and_releases_slot(app):
    import routes.mqtt_api as mqtt_api

    server = {"id": 86, "host": "broker", "port": 1883}
    client = MagicMock(name="scan_client")

    def confirm_connect():
        client.on_connect(client, None, {}, 0)

    client.loop_start.side_effect = confirm_connect
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(return_value=client),
    )
    worker_calls = 0

    def fake_time():
        nonlocal worker_calls
        if threading.current_thread() is threading.main_thread():
            return 301.0
        worker_calls += 1
        return 0.0 if worker_calls == 1 else 301.0

    app.config["TESTING"] = False
    mqtt_api._scan_sse_connections.clear()
    with (
        app.test_request_context("/api/mqtt/86/scan-sse", environ_base={"REMOTE_ADDR": "10.0.0.86"}),
        patch.object(mqtt_api.db, "get_mqtt_server_strict", return_value=server),
        patch.object(mqtt_api, "mqtt", fake_mqtt),
        patch("time.monotonic", side_effect=fake_time),
    ):
        response = mqtt_api.api_mqtt_scan_sse.__wrapped__(86)
        iterator = iter(response.response)
        assert "event: open" in next(iterator)
        assert "event: end" in next(iterator)
        with pytest.raises(StopIteration):
            next(iterator)
        response.close()

    assert "10.0.0.86" not in mqtt_api._scan_sse_connections


def test_finding_114_status_requires_admin_before_loading_credentials(client, app):
    import routes.mqtt_api as mqtt_api

    app.config["TESTING"] = False
    with patch.object(mqtt_api.db, "get_mqtt_server") as get_server:
        response = client.get("/api/mqtt/114/status")

    assert response.status_code == 401
    assert response.get_json()["error_code"] == "UNAUTHENTICATED"
    get_server.assert_not_called()


def test_finding_116_scan_connect_error_is_reported_and_stream_ends(app):
    import routes.mqtt_api as mqtt_api

    server = {"id": 116, "host": "offline-broker", "port": 1883}
    client = MagicMock(name="scan_client")
    client.connect.side_effect = ConnectionRefusedError("broker unavailable")
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(return_value=client),
    )

    app.config["TESTING"] = False
    mqtt_api._scan_sse_connections.clear()
    with (
        app.test_request_context("/api/mqtt/116/scan-sse", environ_base={"REMOTE_ADDR": "10.0.0.116"}),
        patch.object(mqtt_api.db, "get_mqtt_server_strict", return_value=server),
        patch.object(mqtt_api, "mqtt", fake_mqtt),
    ):
        response = mqtt_api.api_mqtt_scan_sse.__wrapped__(116)
        iterator = iter(response.response)
        assert "event: open" in next(iterator)
        error_frame = next(iterator)
        assert "event: error" in error_frame
        error_payload = json.loads(error_frame.split("data: ", 1)[1])
        assert error_payload["error_code"] == "MQTT_CONNECT_FAILED"
        assert "broker unavailable" not in error_frame
        with pytest.raises(StopIteration):
            next(iterator)
        response.close()

    assert "10.0.0.116" not in mqtt_api._scan_sse_connections
