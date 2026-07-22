"""Regression tests for the Phase 2 MQTT publisher and secret-safety fixes."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def test_finding_39_dead_broker_connect_does_not_hold_global_client_lock():
    """A blocked connect for server A must not stall cached server B."""
    import services.mqtt_pub as mqtt_pub

    connect_started = threading.Event()
    release_connect = threading.Event()
    healthy_done = threading.Event()
    healthy = MagicMock(name="healthy_client")
    healthy.is_connected.return_value = True

    class BlockingClient:
        def connect(self, host, port, keepalive):
            connect_started.set()
            release_connect.wait(timeout=2)

        def reconnect_delay_set(self, **kwargs):
            return None

        def max_inflight_messages_set(self, value):
            return None

        def loop_start(self):
            return None

        def loop_stop(self):
            return None

        def disconnect(self):
            return None

    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=lambda *args, **kwargs: BlockingClient(),
    )
    results: dict[str, object] = {}

    def connect_dead_server():
        results["dead"] = mqtt_pub.get_or_create_mqtt_client({"id": 901, "host": "dead.invalid", "port": 1883})

    def get_healthy_server():
        results["healthy"] = mqtt_pub.get_or_create_mqtt_client({"id": 902})
        healthy_done.set()

    with patch.object(mqtt_pub, "mqtt", fake_mqtt), patch.object(mqtt_pub, "_MQTT_CLIENTS", {902: healthy}):
        dead_thread = threading.Thread(target=connect_dead_server)
        healthy_thread = threading.Thread(target=get_healthy_server)
        dead_thread.start()
        assert connect_started.wait(timeout=1)
        healthy_thread.start()
        try:
            assert healthy_done.wait(timeout=0.2), "healthy server was blocked by another server's connect()"
        finally:
            release_connect.set()
            dead_thread.join(timeout=1)
            healthy_thread.join(timeout=1)

    assert results["healthy"] is healthy


def test_finding_49_disconnect_callback_accepts_paho_v2_signature():
    import services.mqtt_pub as mqtt_pub

    client = MagicMock(name="client")
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(return_value=client),
    )

    with patch.object(mqtt_pub, "mqtt", fake_mqtt), patch.object(mqtt_pub, "_MQTT_CLIENTS", {}):
        assert mqtt_pub.get_or_create_mqtt_client({"id": 49, "host": "broker", "port": 1883}) is client

    callback = client.on_disconnect
    callback(client, None, SimpleNamespace(is_disconnect_packet_from_server=True), 7, None)


def test_finding_52_wrong_key_raises_safe_explicit_secret_error():
    import utils

    with patch.object(utils, "_get_secret_key", return_value=b"A" * 32):
        encrypted = utils.encrypt_secret("broker-password")

    expected_error = getattr(utils, "SecretDecryptionError", RuntimeError)
    with (
        patch.object(utils, "_get_secret_key", return_value=b"B" * 32),
        pytest.raises(expected_error) as exc_info,
    ):
        utils.decrypt_secret(encrypted)

    message = str(exc_info.value).lower()
    assert "secret" in message and "key" in message
    assert "mac check failed" not in message


def test_finding_53_public_invalidation_clears_all_publisher_caches():
    import services.mqtt_pub as mqtt_pub

    client = MagicMock(name="cached_client")
    clients = {53: client}
    server_cache = {53: ({"id": 53, "host": "old-broker"}, 1.0)}
    topic_cache = {(53, "/devices/relay"): ("1", 2.0), (54, "/other"): ("0", 3.0)}

    assert hasattr(mqtt_pub, "invalidate_mqtt_server")
    with (
        patch.object(mqtt_pub, "_MQTT_CLIENTS", clients),
        patch.object(mqtt_pub, "_SERVER_CACHE", server_cache),
        patch.object(mqtt_pub, "_TOPIC_LAST_SEND", topic_cache),
    ):
        mqtt_pub.invalidate_mqtt_server(53)

    assert 53 not in clients
    assert 53 not in server_cache
    assert (53, "/devices/relay") not in topic_cache
    assert (54, "/other") in topic_cache
    client.loop_stop.assert_called_once()
    client.disconnect.assert_called_once()


def test_finding_55_last_qos_retry_is_waited_for_and_can_succeed():
    import services.mqtt_pub as mqtt_pub

    infos = []
    for delivered in (False, False, False, True):
        info = MagicMock()
        info.rc = 0
        info.is_published.return_value = delivered
        infos.append(info)
    client = MagicMock()
    client.publish.side_effect = infos

    with patch.object(mqtt_pub.time, "sleep"):
        assert mqtt_pub._publish_with_retries(client, "/devices/relay", "0", 2, True) is True

    assert infos[-1].wait_for_publish.call_count == 1
    assert client.publish.call_count == 4


def test_finding_84_stale_worker_cannot_invalidate_replacement_client():
    import services.mqtt_pub as mqtt_pub

    stale = MagicMock(name="stale")
    replacement = MagicMock(name="replacement")
    clients = {84: replacement}

    with patch.object(mqtt_pub, "_MQTT_CLIENTS", clients):
        mqtt_pub._invalidate_client(84, expected_client=stale)

    assert clients[84] is replacement
    replacement.loop_stop.assert_not_called()
    replacement.disconnect.assert_not_called()


def test_finding_84_in_use_client_teardown_is_deferred_until_last_release():
    import services.mqtt_pub as mqtt_pub

    client = MagicMock(name="shared")
    clients = {84: client}
    users = {id(client): 1}
    retired: dict[int, object] = {}

    with (
        patch.object(mqtt_pub, "_MQTT_CLIENTS", clients),
        patch.object(mqtt_pub, "_MQTT_CLIENT_USERS", users, create=True),
        patch.object(mqtt_pub, "_RETIRED_MQTT_CLIENTS", retired, create=True),
    ):
        mqtt_pub._invalidate_client(84, expected_client=client)
        assert 84 not in clients
        client.loop_stop.assert_not_called()
        client.disconnect.assert_not_called()

        mqtt_pub._release_mqtt_client(client)

    client.loop_stop.assert_called_once()
    client.disconnect.assert_called_once()
