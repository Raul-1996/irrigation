"""Regression coverage for the post-review MQTT/relay safety package."""

from __future__ import annotations

import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest


def _server(test_db, name: str = "broker") -> dict:
    return test_db.create_mqtt_server({"name": name, "host": "127.0.0.1", "port": 1883})


def _mqtt_zone(test_db, server: dict, name: str, *, group_id: int = 1, state: str = "off") -> dict:
    zone = test_db.create_zone(
        {
            "name": name,
            "duration": 10,
            "group_id": group_id,
            "topic": f"/zones/{name}",
            "mqtt_server_id": server["id"],
        }
    )
    test_db.update_zone(zone["id"], {"state": state})
    return test_db.get_zone(zone["id"])


def _scheduler_group_stop_result(group_id: int, stopped: list[int]) -> dict:
    return {
        "success": True,
        "group_id": group_id,
        "aggregate_valid": True,
        "stopped": list(stopped),
        "unresolved": [],
        "unverified_zone_ids": [],
        "retry_scheduled": False,
    }


def test_start_does_not_report_success_when_post_publish_cas_loses(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "start-final-cas", state="off")
    original_update = zone_control._versioned_update

    def reject_final_state(zone_id, updates, *, audit_reason=""):
        if audit_reason == "mqtt_ack_on":
            return False, test_db.get_zone(zone_id)
        return original_update(zone_id, updates, audit_reason=audit_reason)

    verifier = MagicMock()
    verifier.prepare_verification.return_value = object()
    published_values: list[str] = []

    def publish(_server, _topic, value, **_kwargs):
        published_values.append(value)
        return True

    verifier.verify_master_command.side_effect = lambda _sid, _topic, _expected, callback: callback()
    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "_versioned_update", side_effect=reject_final_state),
        patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(zone_control, "water_monitor"),
        patch.object(zone_control, "_emergency_stop_active", return_value=False),
        patch.object(zone_control, "_rain_group_blocked", return_value=False),
        patch("services.events.publish") as publish_event,
    ):
        assert zone_control.exclusive_start_zone(zone["id"]) is False

    verifier.verify_async.assert_not_called()
    assert published_values[-2:] == ["1", "0"]
    assert not any(call.args[0].get("type") == "zone_start" for call in publish_event.call_args_list)


def test_start_verify_launch_failure_counter_stops_energized_relay(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "start-verify-launch", state="off")
    verifier = MagicMock()
    verifier.prepare_verification.return_value = object()
    verifier.verify_async.side_effect = RuntimeError("thread launch failed")
    values: list[str] = []

    def publish(_server, _topic, value, **_kwargs):
        values.append(value)
        return True

    verifier.verify_master_command.side_effect = lambda _sid, _topic, _expected, callback: callback()
    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(zone_control, "water_monitor"),
        patch.object(zone_control, "_emergency_stop_active", return_value=False),
        patch.object(zone_control, "_rain_group_blocked", return_value=False),
        patch("services.events.publish") as publish_event,
    ):
        assert zone_control.exclusive_start_zone(zone["id"]) is False

    assert values[-2:] == ["1", "0"]
    assert test_db.get_zone(zone["id"])["state"] == "fault"
    publish_event.assert_not_called()


def test_stop_rejects_stale_activation_before_any_side_effect(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "stale-stop-owner", state="on")
    test_db.update_zone(zone["id"], {"command_id": "new-owner", "watering_start_time": "2026-07-23 10:00:00"})
    verifier = MagicMock()
    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value") as publish,
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(zone_control, "_maybe_schedule_master_close") as close_master,
    ):
        assert zone_control.stop_zone(zone["id"], activation_token="old-owner") is False

    publish.assert_not_called()
    verifier.register_command.assert_not_called()
    close_master.assert_not_called()
    assert test_db.get_zone(zone["id"])["state"] == "on"


def test_stop_does_not_publish_when_stopping_cas_loses(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "stop-initial-cas", state="on")
    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "_versioned_update", return_value=(False, test_db.get_zone(zone["id"]))),
        patch.object(zone_control, "publish_mqtt_value") as publish,
        patch.object(zone_control, "state_verifier") as verifier,
        patch("services.events.publish") as publish_event,
    ):
        assert zone_control.stop_zone(zone["id"], force=True) is False

    publish.assert_not_called()
    verifier.register_command.assert_not_called()
    assert not any(call.args[0].get("type") == "zone_stop" for call in publish_event.call_args_list)


def test_stop_verify_launch_failure_faults_and_returns_false(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "stop-verify-launch", state="on")
    test_db.update_zone(zone["id"], {"command_id": "stop-owner", "watering_start_time": "2026-07-23 10:00:00"})
    verifier = MagicMock()
    verifier.prepare_verification.return_value = object()
    verifier.verify_async.side_effect = RuntimeError("thread launch failed")
    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", return_value=True),
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(zone_control, "_maybe_schedule_master_close") as close_master,
        patch("services.events.publish") as publish_event,
    ):
        assert zone_control.stop_zone(zone["id"], activation_token="stop-owner") is False

    assert test_db.get_zone(zone["id"])["state"] == "fault"
    close_master.assert_called_once()
    publish_event.assert_not_called()


def test_stop_revalidates_group_after_acquiring_original_group_lock(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "stop-group-race", state="on")
    original_get_zone = test_db.get_zone
    reads = 0

    def moved_after_first_read(zone_id):
        nonlocal reads
        reads += 1
        current = original_get_zone(zone_id)
        if current and reads >= 2:
            current = dict(current)
            current["group_id"] = 2
        return current

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(test_db, "get_zone", side_effect=moved_after_first_read),
        patch.object(zone_control, "publish_mqtt_value") as publish,
        patch.object(zone_control, "state_verifier"),
    ):
        assert zone_control.stop_zone(zone["id"], force=True) is False

    publish.assert_not_called()


def test_tls_setup_failure_never_connects_plaintext():
    from services import mqtt_pub

    client = MagicMock()
    client.tls_set.side_effect = ValueError("bad CA")
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(return_value=client),
    )

    with (
        patch.object(mqtt_pub, "mqtt", fake_mqtt),
        patch.object(mqtt_pub, "_MQTT_CLIENTS", {}),
        patch.object(mqtt_pub, "_MQTT_CLIENT_CREATE_LOCKS", {}),
    ):
        result = mqtt_pub.get_or_create_mqtt_client(
            {"id": 701, "host": "broker", "port": 8883, "tls_enabled": 1, "tls_ca_path": "/bad/ca"}
        )

    assert result is None
    client.connect.assert_not_called()
    client.loop_start.assert_not_called()


def test_verifier_tls_setup_failure_never_connects_plaintext():
    from services import observed_state

    client = MagicMock()
    client.tls_set.side_effect = OSError("unreadable certificate")
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(return_value=client),
    )

    with patch.object(observed_state, "mqtt", fake_mqtt):
        result = observed_state.StateVerifier()._subscribe_and_wait(
            {"host": "broker", "port": 8883, "tls_enabled": 1},
            "/zones/a",
            {"1"},
            0.001,
        )

    assert result is False
    client.connect.assert_not_called()


def test_publisher_writes_command_channel_only():
    """The base topic is relay-owned report truth, never desired app state."""
    from services import mqtt_pub

    with (
        patch.object(mqtt_pub, "_db", None),
        patch.object(mqtt_pub, "_TOPIC_LAST_SEND", {}),
        patch.object(mqtt_pub, "_publish_one", return_value=True) as publish_one,
    ):
        assert mqtt_pub.publish_mqtt_value({"id": 702}, "/devices/relay/K1", "1", min_interval_sec=0) is True

    assert [call.args[2] for call in publish_one.call_args_list] == ["/devices/relay/K1/on"]


@pytest.mark.parametrize(
    "topic",
    [None, "", " ", "/", "on", "/on", "/relay/on", "/relay/on/on", "/relay/+", "/relay/#", "/relay/\x00"],
)
def test_invalid_or_command_channel_topics_never_publish(topic):
    from services import mqtt_pub
    from utils import normalize_topic

    with (
        patch.object(mqtt_pub, "_db", None),
        patch.object(mqtt_pub, "_TOPIC_LAST_SEND", {}),
        patch.object(mqtt_pub, "_TOPIC_INFLIGHT", {}),
        patch.object(mqtt_pub, "_publish_one") as publish_one,
    ):
        assert normalize_topic(topic) == ""
        assert mqtt_pub.publish_mqtt_value({"id": 702}, topic, "1", min_interval_sec=0) is False

    publish_one.assert_not_called()


def test_topic_normalization_is_idempotent_for_one_canonical_base():
    from utils import normalize_topic

    canonical = normalize_topic("///devices/relay/K1")
    assert canonical == "/devices/relay/K1"
    assert normalize_topic(canonical) == canonical


def test_publisher_refuses_deleted_server_instead_of_using_stale_argument():
    from services import mqtt_pub

    db = MagicMock()
    db.get_mqtt_server.return_value = None
    with (
        patch.object(mqtt_pub, "_db", db),
        patch.object(mqtt_pub, "_SERVER_CACHE", {}),
        patch.object(mqtt_pub, "_TOPIC_LAST_SEND", {}),
        patch.object(mqtt_pub, "_publish_one") as publish_one,
    ):
        assert mqtt_pub.publish_mqtt_value({"id": 703, "host": "stale"}, "/relay", "0") is False

    publish_one.assert_not_called()


def test_failed_publish_never_debounces_immediate_safety_retry():
    from services import mqtt_pub

    with (
        patch.object(mqtt_pub, "_db", None),
        patch.object(mqtt_pub, "_TOPIC_LAST_SEND", {}),
        patch.object(mqtt_pub, "_publish_one", side_effect=[False, True]) as publish_one,
    ):
        assert mqtt_pub.publish_mqtt_value({"id": 704}, "/relay", "0", min_interval_sec=60) is False
        assert mqtt_pub.publish_mqtt_value({"id": 704}, "/relay", "0", min_interval_sec=60) is True

    assert publish_one.call_count == 2


def test_mqtt_client_warmup_exposes_immutable_config_provenance_before_publish():
    """Status can attribute a warmed client without consulting server TTL cache."""
    from services import mqtt_pub

    server = {
        "id": 705,
        "host": "broker.internal",
        "port": 8883,
        "username": "irrigation",
        "password": "low-entropy-secret",
        "client_id": "wb-irrigation",
        "enabled": 1,
        "tls_enabled": 1,
        "tls_ca_path": "/etc/ssl/ca.pem",
        "tls_cert_path": "/etc/ssl/client.pem",
        "tls_key_path": "/etc/ssl/client.key",
        "tls_insecure": 0,
        "tls_version": "TLS_CLIENT",
    }
    client = MagicMock(name="warmed_client")
    client.is_connected.return_value = True
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(return_value=client),
    )

    with (
        patch.object(mqtt_pub, "mqtt", fake_mqtt),
        patch.object(mqtt_pub, "_MQTT_CLIENTS", {}),
        patch.object(mqtt_pub, "_MQTT_CLIENT_CREATE_LOCKS", {}),
        patch.object(mqtt_pub, "_MQTT_CLIENT_PROVENANCE", {}, create=True),
        patch.object(mqtt_pub, "_MQTT_CLIENT_GENERATION", 0, create=True),
        patch.object(mqtt_pub, "_SERVER_CACHE", {705: ({"password": "wrong-source"}, 1.0)}),
    ):
        assert mqtt_pub.get_or_create_mqtt_client(server) is client
        snapshots = mqtt_pub.snapshot_mqtt_clients()
        expected_fingerprint = mqtt_pub.mqtt_server_config_fingerprint(server)

    assert snapshots.keys() == {705}
    snapshot = snapshots[705]
    assert snapshot.server_id == 705
    assert snapshot.client is client
    assert snapshot.generation == 1
    assert snapshot.config_fingerprint == expected_fingerprint
    assert "low-entropy-secret" not in snapshot.config_fingerprint
    client.publish.assert_not_called()


def test_mqtt_client_db_rotation_replaces_old_authority_without_relabelling_it():
    """A DB update replaces, rather than relabels, the cached old client."""
    from services import mqtt_pub

    old_server = {
        "id": 706,
        "host": "old-broker",
        "port": 1883,
        "username": "old-user",
        "password": "old-password",
        "client_id": "old-client",
        "enabled": 1,
        "tls_enabled": 0,
        "tls_ca_path": None,
        "tls_cert_path": None,
        "tls_key_path": None,
        "tls_insecure": 0,
        "tls_version": None,
    }
    new_server = {
        **old_server,
        "host": "new-broker",
        "port": 8883,
        "username": "new-user",
        "password": "new-password",
        "client_id": "new-client",
        "tls_enabled": 1,
        "tls_ca_path": "/new/ca.pem",
        "tls_cert_path": "/new/cert.pem",
        "tls_key_path": "/new/key.pem",
        "tls_insecure": 1,
        "tls_version": "TLS_CLIENT",
    }
    old_client = MagicMock(name="old_client")
    old_client.is_connected.return_value = True
    new_client = MagicMock(name="new_client")
    new_client.is_connected.return_value = True
    post_invalidation_client = MagicMock(name="post_invalidation_client")
    post_invalidation_client.is_connected.return_value = True
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(side_effect=[old_client, new_client, post_invalidation_client]),
    )
    current_db = MagicMock()
    current_db.get_mqtt_server.return_value = new_server

    with (
        patch.object(mqtt_pub, "mqtt", fake_mqtt),
        patch.object(mqtt_pub, "_MQTT_CLIENTS", {}),
        patch.object(mqtt_pub, "_MQTT_CLIENT_CREATE_LOCKS", {}),
        patch.object(mqtt_pub, "_MQTT_CLIENT_PROVENANCE", {}, create=True),
        patch.object(mqtt_pub, "_MQTT_CLIENT_GENERATION", 0, create=True),
        patch.object(mqtt_pub, "_SERVER_CACHE", {}),
        patch.object(mqtt_pub, "_TOPIC_LAST_SEND", {}),
        patch.object(mqtt_pub, "_db", current_db),
        patch.object(mqtt_pub, "_publish_with_retries", return_value=True),
    ):
        assert mqtt_pub.get_or_create_mqtt_client(old_server) is old_client
        before = mqtt_pub.snapshot_mqtt_clients()[706]

        assert mqtt_pub.publish_mqtt_value(old_server, "/relay", "1", min_interval_sec=0) is True
        during_rotation = mqtt_pub.snapshot_mqtt_clients()[706]

        assert during_rotation.client is new_client
        assert during_rotation.generation == before.generation + 1
        assert during_rotation.config_fingerprint == mqtt_pub.mqtt_server_config_fingerprint(new_server)
        old_client.loop_stop.assert_called_once()
        old_client.disconnect.assert_called_once()

        mqtt_pub.invalidate_mqtt_server(706)
        assert mqtt_pub.snapshot_mqtt_clients() == {}

        assert mqtt_pub.get_or_create_mqtt_client(new_server) is post_invalidation_client
        after = mqtt_pub.snapshot_mqtt_clients()[706]

    assert after.client is post_invalidation_client
    assert after.generation == before.generation + 2
    assert after.config_fingerprint == mqtt_pub.mqtt_server_config_fingerprint(new_server)


def test_mqtt_config_fingerprint_covers_every_effective_connection_field():
    from services import mqtt_pub

    base = {
        "id": 707,
        "host": "broker",
        "port": 8883,
        "username": "user",
        "password": "secret",
        "client_id": "client",
        "enabled": 1,
        "tls_enabled": 1,
        "tls_ca_path": "/ca",
        "tls_cert_path": "/cert",
        "tls_key_path": "/key",
        "tls_insecure": 0,
        "tls_version": "TLS_CLIENT",
    }
    variants = {
        "host": "other",
        "port": 1883,
        "username": "other",
        "password": "other",
        "client_id": "other",
        "enabled": 0,
        "tls_enabled": 0,
        "tls_ca_path": "/other-ca",
        "tls_cert_path": "/other-cert",
        "tls_key_path": "/other-key",
        "tls_insecure": 1,
        "tls_version": "TLS",
    }
    fingerprint = mqtt_pub.mqtt_server_config_fingerprint(base)

    assert len(fingerprint) == 64
    assert "secret" not in fingerprint
    for field, value in variants.items():
        assert mqtt_pub.mqtt_server_config_fingerprint({**base, field: value}) != fingerprint, field


def test_concurrent_duplicate_publish_waits_for_and_shares_failed_result():
    from services import mqtt_pub

    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def publish_one(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(timeout=2)
            return False
        return True

    results: dict[str, bool] = {}
    with (
        patch.object(mqtt_pub, "_db", None),
        patch.object(mqtt_pub, "_TOPIC_LAST_SEND", {}),
        patch.object(mqtt_pub, "_TOPIC_INFLIGHT", {}, create=True),
        patch.object(mqtt_pub, "_publish_one", side_effect=publish_one),
    ):
        first = threading.Thread(
            target=lambda: results.setdefault(
                "first",
                mqtt_pub.publish_mqtt_value({"id": 708}, "/relay", "0", min_interval_sec=60),
            )
        )
        duplicate = threading.Thread(
            target=lambda: results.setdefault(
                "duplicate",
                mqtt_pub.publish_mqtt_value({"id": 708}, "/relay", "0", min_interval_sec=60),
            )
        )
        first.start()
        assert entered.wait(timeout=1)
        duplicate.start()
        duplicate.join(timeout=0.05)
        assert "duplicate" not in results

        release.set()
        first.join(timeout=1)
        duplicate.join(timeout=1)

        assert results == {"first": False, "duplicate": False}
        assert calls == 1
        assert mqtt_pub.publish_mqtt_value({"id": 708}, "/relay", "0", min_interval_sec=60) is True

    assert calls == 2


def test_server_invalidation_epoch_isolates_aba_inflight_and_debounce():
    """An old A publish cannot satisfy a new A generation after A -> B -> A."""
    from services import mqtt_pub

    old_entered = threading.Event()
    release_old = threading.Event()
    calls = 0

    def publish_one(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            old_entered.set()
            assert release_old.wait(timeout=2)
            return True
        return calls != 2

    server_a = {"id": 709, "host": "broker-a", "port": 1883}
    old_result: list[bool] = []
    with (
        patch.object(mqtt_pub, "_db", None),
        patch.object(mqtt_pub, "_TOPIC_LAST_SEND", {}),
        patch.object(mqtt_pub, "_TOPIC_INFLIGHT", {}),
        patch.object(mqtt_pub, "_MQTT_SERVER_EPOCH", {}, create=True),
        patch.object(mqtt_pub, "_invalidate_client"),
        patch.object(mqtt_pub, "_publish_one", side_effect=publish_one),
    ):
        old = threading.Thread(
            target=lambda: old_result.append(mqtt_pub.publish_mqtt_value(server_a, "/relay", "0", min_interval_sec=60))
        )
        old.start()
        assert old_entered.wait(timeout=1)

        # Configuration rotates A -> B -> A while the first A generation is
        # still in flight. Equal config fingerprints must not collapse the
        # new A command onto the obsolete client generation.
        mqtt_pub.invalidate_mqtt_server(server_a["id"])
        mqtt_pub.invalidate_mqtt_server(server_a["id"])
        assert mqtt_pub.publish_mqtt_value(server_a, "/relay", "0", min_interval_sec=60) is False
        assert calls == 2

        release_old.set()
        old.join(timeout=1)
        assert old_result == [True]

        # The obsolete completion must not repopulate the current generation's
        # debounce cache after the new generation failed.
        assert mqtt_pub.publish_mqtt_value(server_a, "/relay", "0", min_interval_sec=60) is True

    assert calls == 3


def test_invalidation_during_connect_rejects_stale_client_install():
    from services import mqtt_pub

    connect_entered = threading.Event()
    release_connect = threading.Event()

    class BlockingClient:
        def reconnect_delay_set(self, **_kwargs):
            return None

        def max_inflight_messages_set(self, _value):
            return None

        def connect(self, *_args):
            connect_entered.set()
            assert release_connect.wait(timeout=2)

        def loop_start(self):
            return None

        def loop_stop(self):
            self.loop_stopped = True

        def disconnect(self):
            self.disconnected = True

    client = BlockingClient()
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(return_value=client),
    )
    result: list[object] = []
    with (
        patch.object(mqtt_pub, "mqtt", fake_mqtt),
        patch.object(mqtt_pub, "_MQTT_CLIENTS", {}),
        patch.object(mqtt_pub, "_MQTT_CLIENT_CREATE_LOCKS", {}),
        patch.object(mqtt_pub, "_MQTT_CLIENT_PROVENANCE", {}),
        patch.object(mqtt_pub, "_MQTT_SERVER_EPOCH", {}),
    ):
        creator = threading.Thread(
            target=lambda: result.append(
                mqtt_pub.get_or_create_mqtt_client({"id": 710, "host": "old-authority", "port": 1883})
            )
        )
        creator.start()
        assert connect_entered.wait(timeout=1)
        mqtt_pub.invalidate_mqtt_server(710)
        release_connect.set()
        creator.join(timeout=2)

        assert result == [None]
        assert mqtt_pub.snapshot_mqtt_clients() == {}
        assert mqtt_pub._MQTT_SERVER_EPOCH[710] == 1
    assert client.loop_stopped is True
    assert client.disconnected is True


def test_invalidation_during_db_cache_fill_refetches_current_authority_before_publish():
    from services import mqtt_pub

    old_server = {"id": 711, "host": "old-authority", "port": 1883}
    new_server = {"id": 711, "host": "new-authority", "port": 8883}
    first_read_entered = threading.Event()
    release_first_read = threading.Event()
    current = {"server": old_server}
    reads = 0

    def get_server(_sid):
        nonlocal reads
        reads += 1
        snapshot = dict(current["server"])
        if reads == 1:
            first_read_entered.set()
            assert release_first_read.wait(timeout=2)
        return snapshot

    db = MagicMock()
    db.get_mqtt_server.side_effect = get_server
    published: list[tuple[dict, int, str]] = []

    def publish_one(server, _sid, _topic, _value, _qos, _retain, **kwargs):
        published.append((dict(server), kwargs["server_epoch"], kwargs["config_fingerprint"]))
        return True

    result: list[bool] = []
    with (
        patch.object(mqtt_pub, "_db", db),
        patch.object(mqtt_pub, "_SERVER_CACHE", {}),
        patch.object(mqtt_pub, "_MQTT_SERVER_EPOCH", {}),
        patch.object(mqtt_pub, "_MQTT_CLIENTS", {}),
        patch.object(mqtt_pub, "_TOPIC_LAST_SEND", {}),
        patch.object(mqtt_pub, "_TOPIC_INFLIGHT", {}),
        patch.object(mqtt_pub, "_publish_one", side_effect=publish_one),
    ):
        publisher = threading.Thread(
            target=lambda: result.append(
                mqtt_pub.publish_mqtt_value(old_server, "/relay/cache-race", "1", min_interval_sec=0)
            )
        )
        publisher.start()
        assert first_read_entered.wait(timeout=1)
        current["server"] = new_server
        mqtt_pub.invalidate_mqtt_server(711)
        release_first_read.set()
        publisher.join(timeout=2)

        assert result == [True]
        assert reads >= 2
        cached_server, _cached_at, cached_epoch, cached_fingerprint = mqtt_pub._SERVER_CACHE[711]

    assert published == [(new_server, 1, mqtt_pub.mqtt_server_config_fingerprint(new_server))]
    assert cached_server == new_server
    assert cached_epoch == 1
    assert cached_fingerprint == mqtt_pub.mqtt_server_config_fingerprint(new_server)


@pytest.mark.parametrize("payload", ["", "UNKNOWN", "2", "null", "offline", "yes"])
def test_unknown_relay_payload_never_changes_state_or_jobs(payload):
    from services import sse_hub

    db = MagicMock()
    scheduler = MagicMock()
    with (
        patch.object(sse_hub, "_db", db),
        patch.object(sse_hub, "_app_config", {}),
        patch.object(sse_hub, "_get_scheduler_fn", return_value=scheduler),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {1: {"/zones/a": [7]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
        patch.object(sse_hub, "broadcast") as broadcast,
    ):
        sse_hub._process_mqtt_message(1, None, "/zones/a", payload)

    db.get_zone.assert_not_called()
    db.update_zone.assert_not_called()
    scheduler.cancel_zone_jobs.assert_not_called()
    broadcast.assert_not_called()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [("1", "on"), ("ON", "on"), ("true", "on"), ("0", "off"), ("OFF", "off"), ("false", "off")],
)
def test_relay_payload_allowlists_are_explicit(payload, expected):
    from services.observed_state import canonical_relay_state

    assert canonical_relay_state(payload) == expected


def test_new_start_resets_stale_observed_state_before_publish(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "start-generation", state="off")
    test_db.update_zone(zone["id"], {"observed_state": "off", "commanded_state": "off"})

    def publish(_server, _topic, value, **_kwargs):
        during = test_db.get_zone(zone["id"])
        assert value == "1"
        assert during["commanded_state"] == "on"
        assert during["observed_state"] == "unconfirmed"
        return True

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
        patch.object(zone_control, "state_verifier"),
        patch.object(zone_control, "water_monitor"),
    ):
        assert zone_control.exclusive_start_zone(zone["id"]) is True

    assert test_db.get_zone(zone["id"])["observed_state"] == "unconfirmed"


def test_ordinary_start_subscribes_before_on_publish(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "start-prepared", state="off")
    test_db.update_zone(zone["id"], {"observed_state": "off"})
    order: list[str] = []
    verifier = MagicMock()
    prepared = object()
    verifier.prepare_verification.side_effect = lambda *_args, **_kwargs: order.append("subscribed") or prepared

    def publish(_server, _topic, value, **_kwargs):
        assert value == "1"
        order.append("published")
        return True

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(zone_control, "water_monitor"),
    ):
        assert zone_control.exclusive_start_zone(zone["id"]) is True

    assert order == ["subscribed", "published"]
    verifier.verify_async.assert_called_once_with(
        zone["id"],
        "on",
        generation=verifier.register_command.return_value,
        prepared=prepared,
    )


def test_new_stop_resets_stale_off_and_preserves_activation_until_fresh_echo(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "stop-generation", state="on")
    activation = "2026-07-19 12:00:00"
    activation_id = "a" * 32
    test_db.update_zone(
        zone["id"],
        {
            "observed_state": "off",
            "commanded_state": "on",
            "watering_start_time": activation,
        },
    )
    test_db.update_zone_versioned(
        zone["id"],
        {"command_id": activation_id},
        expected_version=test_db.get_zone(zone["id"])["version"],
    )

    def publish(_server, _topic, value, **_kwargs):
        during = test_db.get_zone(zone["id"])
        assert value == "0"
        assert during["commanded_state"] == "off"
        assert during["observed_state"] == "unconfirmed"
        assert during["watering_start_time"] == activation
        assert during["command_id"] == activation_id
        return True

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
        patch.object(zone_control, "state_verifier"),
        patch.object(zone_control, "water_monitor"),
    ):
        assert zone_control.stop_zone(zone["id"], force=True) is True

    after = test_db.get_zone(zone["id"])
    assert after["observed_state"] == "unconfirmed"
    assert after["watering_start_time"] == activation
    assert after["command_id"] == activation_id


def test_ordinary_stop_subscribes_before_off_publish(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "stop-prepared", state="on")
    test_db.update_zone(zone["id"], {"observed_state": "on", "commanded_state": "on"})
    order: list[str] = []
    verifier = MagicMock()
    prepared = object()
    verifier.prepare_verification.side_effect = lambda *_args, **_kwargs: order.append("subscribed") or prepared

    def publish(_server, _topic, value, **_kwargs):
        assert value == "0"
        order.append("published")
        return True

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(zone_control, "water_monitor"),
    ):
        assert zone_control.stop_zone(zone["id"], force=True) is True

    assert order == ["subscribed", "published"]
    verifier.verify_async.assert_called_once_with(
        zone["id"],
        "off",
        generation=verifier.register_command.return_value,
        prepared=prepared,
    )


def test_retained_off_cannot_complete_active_command_but_fresh_off_can(test_db):
    from services import sse_hub

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "retained-off", state="on")
    activation = "2026-07-19 12:05:00"
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": "off",
            "observed_state": "unconfirmed",
            "watering_start_time": activation,
        },
    )
    scheduler = MagicMock()
    with (
        patch.object(sse_hub, "_db", test_db),
        patch.object(sse_hub, "_app_config", {}),
        patch.object(sse_hub, "_get_scheduler_fn", return_value=scheduler),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {server["id"]: {zone["topic"]: [zone["id"]]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
        patch.object(sse_hub, "broadcast"),
    ):
        sse_hub._process_mqtt_message(server["id"], None, zone["topic"], "0", retained=True)
        retained_result = test_db.get_zone(zone["id"])

        assert retained_result["state"] == "on"
        assert retained_result["observed_state"] == "unconfirmed"
        assert retained_result["watering_start_time"] == activation
        scheduler.cancel_zone_cap.assert_not_called()

        sse_hub._process_mqtt_message(server["id"], None, zone["topic"], "0", retained=False)

    fresh_result = test_db.get_zone(zone["id"])
    assert fresh_result["observed_state"] == "off"
    assert fresh_result["watering_start_time"] is None
    scheduler.cancel_zone_jobs.assert_called_once_with(zone["id"], include_cap=True)


def test_retained_off_never_disarms_active_confirmed_generation(test_db):
    from services import sse_hub

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "retained-confirmed-on", state="on")
    activation = "2026-07-19 12:06:00"
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": "on",
            "observed_state": "on",
            "watering_start_time": activation,
        },
    )
    scheduler = MagicMock()
    with (
        patch.object(sse_hub, "_db", test_db),
        patch.object(sse_hub, "_app_config", {}),
        patch.object(sse_hub, "_get_scheduler_fn", return_value=scheduler),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {server["id"]: {zone["topic"]: [zone["id"]]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
        patch.object(sse_hub, "broadcast"),
    ):
        sse_hub._process_mqtt_message(server["id"], None, zone["topic"], "0", retained=True)

    after = test_db.get_zone(zone["id"])
    assert after["state"] == "on"
    assert after["observed_state"] == "on"
    assert after["watering_start_time"] == activation
    scheduler.cancel_zone_jobs.assert_not_called()


def test_delayed_fresh_off_cannot_disarm_newer_commanded_on_activation(test_db):
    from services import sse_hub

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "delayed-fresh-off", state="starting")
    activation = "c" * 32
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": "on",
            "observed_state": "unconfirmed",
            "watering_start_time": "2026-07-19 12:07:00",
        },
    )
    test_db.update_zone_versioned(
        zone["id"],
        {"command_id": activation},
        expected_version=test_db.get_zone(zone["id"])["version"],
    )
    scheduler = MagicMock()
    with (
        patch.object(sse_hub, "_db", test_db),
        patch.object(sse_hub, "_app_config", {}),
        patch.object(sse_hub, "_get_scheduler_fn", return_value=scheduler),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {server["id"]: {zone["topic"]: [zone["id"]]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
        patch.object(sse_hub, "broadcast"),
    ):
        sse_hub._process_mqtt_message(server["id"], None, zone["topic"], "0", retained=False)

    after = test_db.get_zone(zone["id"])
    assert after["state"] == "starting"
    assert after["observed_state"] == "off"
    assert after["watering_start_time"] == "2026-07-19 12:07:00"
    assert after["command_id"] == activation
    scheduler.cancel_zone_jobs.assert_not_called()


def test_sse_fresh_off_snapshot_apply_aba_is_noop_for_new_activation(test_db):
    from services import sse_hub
    from services.locks import zone_lock

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "sse-off-aba", state="on")
    old_token = "d" * 32
    new_token = "e" * 32
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": "off",
            "observed_state": "unconfirmed",
            "watering_start_time": "2026-07-19 12:09:00",
        },
    )
    test_db.update_zone_versioned(
        zone["id"],
        {"command_id": old_token},
        expected_version=test_db.get_zone(zone["id"])["version"],
    )
    initial_read = threading.Event()
    release_initial_read = threading.Event()
    reads = 0

    def get_zone(zone_id):
        nonlocal reads
        reads += 1
        snapshot = dict(test_db.get_zone(zone_id) or {})
        if reads == 1:
            initial_read.set()
            assert release_initial_read.wait(timeout=2)
        return snapshot

    db_view = MagicMock()
    db_view.get_zone.side_effect = get_zone
    scheduler = MagicMock()
    with (
        patch.object(sse_hub, "_db", db_view),
        patch.object(sse_hub, "_app_config", {}),
        patch.object(sse_hub, "_get_scheduler_fn", return_value=scheduler),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {server["id"]: {zone["topic"]: [zone["id"]]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
        patch.object(sse_hub, "broadcast"),
    ):
        worker = threading.Thread(
            target=lambda: sse_hub._process_mqtt_message(server["id"], None, zone["topic"], "0", retained=False)
        )
        worker.start()
        assert initial_read.wait(timeout=1)
        with zone_lock(zone["id"]):
            test_db.update_zone(
                zone["id"],
                {
                    "state": "starting",
                    "commanded_state": "on",
                    "observed_state": "unconfirmed",
                    "watering_start_time": "2026-07-19 12:10:00",
                },
            )
            test_db.update_zone_versioned(
                zone["id"],
                {"command_id": new_token},
                expected_version=test_db.get_zone(zone["id"])["version"],
            )
        release_initial_read.set()
        worker.join(timeout=2)

    after = test_db.get_zone(zone["id"])
    assert after["state"] == "starting"
    assert after["commanded_state"] == "on"
    assert after["watering_start_time"] == "2026-07-19 12:10:00"
    assert after["command_id"] == new_token
    scheduler.cancel_zone_jobs.assert_not_called()


def test_queued_old_off_received_before_new_activation_cannot_complete_new_stop(test_db):
    from services import observed_state, sse_hub

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "queued-off-before-new-activation", state="on")
    new_token = "f" * 32
    scheduler = MagicMock()

    # OFF(A) entered the queue at t=0. Activation B starts and is commanded
    # OFF before the worker reaches that old physical report.
    observed_state.state_verifier.register_command(zone["id"], "on")
    test_db.update_zone(
        zone["id"],
        {
            "state": "starting",
            "commanded_state": "on",
            "observed_state": "unconfirmed",
            "watering_start_time": "2026-07-19 12:11:00",
        },
    )
    test_db.update_zone_versioned(
        zone["id"],
        {"command_id": new_token},
        expected_version=test_db.get_zone(zone["id"])["version"],
    )
    observed_state.state_verifier.register_command(zone["id"], "off")
    test_db.update_zone(zone["id"], {"state": "stopping", "commanded_state": "off"})

    with (
        patch.object(sse_hub, "_db", test_db),
        patch.object(sse_hub, "_app_config", {}),
        patch.object(sse_hub, "_get_scheduler_fn", return_value=scheduler),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {server["id"]: {zone["topic"]: [zone["id"]]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
        patch.object(sse_hub, "broadcast") as broadcast,
    ):
        sse_hub._process_mqtt_message(
            server["id"],
            None,
            zone["topic"],
            "0",
            retained=False,
            received_at=0.0,
        )

    after = test_db.get_zone(zone["id"])
    assert after["state"] == "stopping"
    assert after["observed_state"] == "unconfirmed"
    assert after["watering_start_time"] == "2026-07-19 12:11:00"
    assert after["command_id"] == new_token
    scheduler.cancel_zone_jobs.assert_not_called()
    broadcast.assert_not_called()


def test_off_received_before_fault_invalidation_still_completes_current_stop(test_db):
    from services import observed_state, sse_hub, zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "off-before-fault-invalidation", state="stopping")
    command_id = "e" * 32
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": "off",
            "observed_state": "unconfirmed",
            "watering_start_time": "2026-07-19 12:12:00",
        },
    )
    test_db.update_zone_versioned(
        zone["id"],
        {"command_id": command_id},
        expected_version=test_db.get_zone(zone["id"])["version"],
    )
    verifier = observed_state.StateVerifier()
    verifier._db = test_db
    scheduler = MagicMock()
    scheduler.cancel_zone_jobs.return_value = True

    # A real OFF command establishes the receipt fence at t=100. Its physical
    # echo is already in the SSE queue at t=110 when the temporary verifier
    # reports failure at t=120. The fault callback must invalidate that stale
    # verifier without pretending a newer relay command was sent.
    with patch.object(observed_state.time, "time", return_value=100.0):
        generation = verifier.register_command(zone["id"], "off")
    with (
        patch.object(observed_state.time, "time", return_value=120.0),
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "state_verifier", verifier),
    ):
        zone_control.mark_zone_command_fault(zone["id"], "off", reason="temporary_verifier_failure")

    assert verifier._is_current(zone["id"], "off", generation) is False

    with (
        patch.object(sse_hub, "_db", test_db),
        patch.object(sse_hub, "state_verifier", verifier),
        patch.object(sse_hub, "_app_config", {}),
        patch.object(sse_hub, "_get_scheduler_fn", return_value=scheduler),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {server["id"]: {zone["topic"]: [zone["id"]]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
        patch.object(sse_hub, "broadcast"),
    ):
        sse_hub._process_mqtt_message(
            server["id"],
            None,
            zone["topic"],
            "0",
            retained=False,
            received_at=110.0,
        )

    after = test_db.get_zone(zone["id"])
    assert verifier.command_registered_at(zone["id"]) == 100.0
    assert after["state"] == "off"
    assert after["observed_state"] == "off"
    assert after["watering_start_time"] is None
    assert after["command_id"] is None
    scheduler.cancel_zone_jobs.assert_called_once_with(zone["id"], include_cap=True)


def test_off_received_after_fault_invalidation_keeps_fault_and_closes_run_failed(test_db):
    from services import observed_state, sse_hub, zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "off-after-fault-invalidation", state="stopping")
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": "off",
            "observed_state": "unconfirmed",
            "command_id": "late-off-owner",
            "watering_start_time": "2026-07-23 10:00:00",
        },
    )
    run_id = test_db.create_zone_run(zone["id"], 1, "2026-07-23 10:00:00", 0.0, None, 1)
    verifier = observed_state.StateVerifier()
    verifier._db = test_db
    with patch.object(observed_state.time, "time", return_value=100.0):
        verifier.register_command(zone["id"], "off")
    with (
        patch.object(observed_state.time, "time", return_value=120.0),
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "state_verifier", verifier),
    ):
        assert zone_control.mark_zone_command_fault(zone["id"], "off", reason="timeout") is True

    scheduler = MagicMock()
    with (
        patch.object(sse_hub, "_db", test_db),
        patch.object(sse_hub, "state_verifier", verifier),
        patch.object(sse_hub, "_app_config", {}),
        patch.object(sse_hub, "_get_scheduler_fn", return_value=scheduler),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {server["id"]: {zone["topic"]: [zone["id"]]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
        patch.object(sse_hub, "broadcast"),
    ):
        sse_hub._process_mqtt_message(server["id"], None, zone["topic"], "0", retained=False, received_at=130.0)

    after = test_db.get_zone(zone["id"])
    assert after["state"] == "fault"
    assert after["observed_state"] == "off"
    assert after["command_id"] is None
    with sqlite3.connect(test_db.db_path) as conn:
        status = conn.execute("SELECT status FROM zone_runs WHERE id = ?", (run_id,)).fetchone()[0]
    assert status == "failed"
    scheduler.cancel_zone_jobs.assert_called_once_with(zone["id"], include_cap=True)


def test_sse_and_verifier_duplicate_off_share_one_terminal_commit(test_db):
    from services import observed_state, sse_hub

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "duplicate-off-observers", state="stopping")
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": "off",
            "observed_state": "unconfirmed",
            "command_id": "duplicate-owner",
            "watering_start_time": "2026-07-23 10:00:00",
        },
    )
    test_db.create_zone_run(zone["id"], 1, "2026-07-23 10:00:00", 0.0, None, 1)
    assert test_db.mark_zone_run_confirmed(zone["id"]) is True
    verifier = observed_state.StateVerifier()
    verifier._db = test_db
    generation = verifier.register_command(zone["id"], "off")
    scheduler = MagicMock()
    with (
        patch.object(sse_hub, "_db", test_db),
        patch.object(sse_hub, "state_verifier", verifier),
        patch.object(sse_hub, "_app_config", {}),
        patch.object(sse_hub, "_get_scheduler_fn", return_value=scheduler),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {server["id"]: {zone["topic"]: [zone["id"]]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
        patch.object(sse_hub, "broadcast"),
        patch("services.events.publish") as publish_event,
    ):
        sse_hub._process_mqtt_message(server["id"], None, zone["topic"], "0", retained=False)
        version_after_sse = test_db.get_zone(zone["id"])["version"]
        assert verifier._apply_confirmation_if_current(zone["id"], "off", generation) is True

    assert test_db.get_zone(zone["id"])["version"] == version_after_sse
    zone_stop_events = [c for c in publish_event.call_args_list if c.args[0].get("type") == "zone_stop"]
    assert len(zone_stop_events) == 1


def test_orchestrated_start_accepts_exact_confirmed_target_and_peer_partition(test_db):
    from services import zone_control

    server = _server(test_db)
    group = test_db.create_group("orchestrated-confirmed-partition")
    target = _mqtt_zone(test_db, server, "orchestrated-target", group_id=group["id"], state="off")
    peer = _mqtt_zone(test_db, server, "orchestrated-peer", group_id=group["id"], state="on")
    scheduler = MagicMock()
    scheduler.cancel_group_jobs.return_value = _scheduler_group_stop_result(
        group["id"], sorted([target["id"], peer["id"]])
    )

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "exclusive_start_zone", return_value=True) as start,
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        status, _ctx = zone_control.start_zone_orchestrated(target["id"])

    assert status == "started"
    scheduler.cancel_group_jobs.assert_called_once_with(group["id"])
    start.assert_called_once_with(target["id"], source="manual")


@pytest.mark.parametrize(
    "bad_result",
    [
        "aggregate_false",
        "success_false",
        "malformed",
        "wrong_partition",
        "wrong_group",
        "bool_zone_id",
        "unresolved",
        "retry_scheduled",
        "duplicate_id",
        "exception",
    ],
)
def test_orchestrated_start_rejects_unproven_target_or_peer_off(test_db, bad_result):
    from services import zone_control

    server = _server(test_db)
    group = test_db.create_group(f"orchestrated-reject-{bad_result}")
    target = _mqtt_zone(test_db, server, f"target-{bad_result}", group_id=group["id"], state="off")
    peer = _mqtt_zone(test_db, server, f"peer-{bad_result}", group_id=group["id"], state="on")
    expected_ids = sorted([target["id"], peer["id"]])
    outcome = _scheduler_group_stop_result(group["id"], expected_ids)
    if bad_result == "aggregate_false":
        outcome["aggregate_valid"] = False
    elif bad_result == "success_false":
        outcome["success"] = False
    elif bad_result == "malformed":
        outcome.pop("unverified_zone_ids")
    elif bad_result == "wrong_partition":
        outcome["stopped"] = [target["id"]]
    elif bad_result == "wrong_group":
        outcome["group_id"] = group["id"] + 1
    elif bad_result == "bool_zone_id":
        outcome["stopped"] = [target["id"], True]
    elif bad_result == "unresolved":
        outcome["success"] = False
        outcome["stopped"] = [target["id"]]
        outcome["unresolved"] = [peer["id"]]
    elif bad_result == "retry_scheduled":
        outcome["retry_scheduled"] = True
    elif bad_result == "duplicate_id":
        outcome["stopped"] = [*expected_ids, peer["id"]]
    scheduler = MagicMock()
    if bad_result == "exception":
        scheduler.cancel_group_jobs.side_effect = RuntimeError("scheduler stop failed")
    else:
        scheduler.cancel_group_jobs.return_value = outcome

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "exclusive_start_zone") as start,
        patch.object(zone_control, "publish_mqtt_value") as publish,
        patch.object(test_db, "create_zone_run", wraps=test_db.create_zone_run) as create_run,
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        status, ctx = zone_control.start_zone_orchestrated(target["id"])

    assert status == "failed"
    assert "group_stop_unconfirmed" in ctx["warnings"]
    start.assert_not_called()
    publish.assert_not_called()
    create_run.assert_not_called()
    assert test_db.get_zone(target["id"])["state"] == "off"
    assert test_db.get_zone(peer["id"])["state"] == "on"


def test_fresh_off_completes_external_activation_without_commanded_state(test_db):
    from services import sse_hub

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "external-fresh-off", state="on")
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": None,
            "observed_state": "on",
            "watering_start_time": "2026-07-19 12:08:00",
        },
    )
    scheduler = MagicMock()
    with (
        patch.object(sse_hub, "_db", test_db),
        patch.object(sse_hub, "_app_config", {}),
        patch.object(sse_hub, "_get_scheduler_fn", return_value=scheduler),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {server["id"]: {zone["topic"]: [zone["id"]]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
        patch.object(sse_hub, "broadcast"),
    ):
        sse_hub._process_mqtt_message(server["id"], None, zone["topic"], "0", retained=False)

    after = test_db.get_zone(zone["id"])
    assert after["state"] == "off"
    assert after["observed_state"] == "off"
    assert after["watering_start_time"] is None
    scheduler.cancel_zone_jobs.assert_called_once_with(zone["id"], include_cap=True)


def test_failed_counter_off_plants_activation_and_safety_retry_before_scheduling(test_db):
    from services import sse_hub

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "counter-failure", state="off")
    test_db.update_zone(zone["id"], {"commanded_state": "off", "observed_state": "off"})
    scheduler = MagicMock()

    def assert_token_already_persisted(*_args, **_kwargs):
        assert test_db.get_zone(zone["id"])["watering_start_time"]

    scheduler.schedule_zone_hard_stop.side_effect = assert_token_already_persisted
    scheduler.schedule_zone_cap.side_effect = assert_token_already_persisted
    with (
        patch.object(sse_hub, "_db", test_db),
        patch.object(sse_hub, "_app_config", {"EMERGENCY_STOP": True}),
        patch.object(sse_hub, "_publish_mqtt_value_fn", return_value=False),
        patch.object(sse_hub, "_get_scheduler_fn", return_value=scheduler),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {server["id"]: {zone["topic"]: [zone["id"]]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
        patch.object(sse_hub, "broadcast"),
    ):
        sse_hub._process_mqtt_message(server["id"], None, zone["topic"], "1", retained=False)

    after = test_db.get_zone(zone["id"])
    assert after["state"] == "on"
    assert after["commanded_state"] == "off"
    assert after["observed_state"] == "unconfirmed"
    assert after["watering_start_time"]
    scheduler.schedule_zone_hard_stop.assert_called_once()
    scheduler.schedule_zone_cap.assert_called_once()


def test_live_on_for_faulted_command_is_countered_without_clearing_fault(test_db):
    from services import sse_hub

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "faulted-late-on", state="fault")
    test_db.update_zone(
        zone["id"],
        {
            "state": "fault",
            "commanded_state": "on",
            "observed_state": "unconfirmed",
            "command_id": "fault-owner",
            "watering_start_time": "2026-07-23 10:00:00",
        },
    )
    scheduler = MagicMock()
    with (
        patch.object(sse_hub, "_db", test_db),
        patch.object(sse_hub, "_app_config", {}),
        patch.object(sse_hub, "_publish_mqtt_value_fn", return_value=True) as publish,
        patch.object(sse_hub, "_get_scheduler_fn", return_value=scheduler),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {server["id"]: {zone["topic"]: [zone["id"]]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
        patch.object(sse_hub, "broadcast"),
    ):
        sse_hub._process_mqtt_message(server["id"], None, zone["topic"], "1", retained=False)

    after = test_db.get_zone(zone["id"])
    assert after["state"] == "fault"
    assert after["commanded_state"] == "off"
    assert after["observed_state"] == "on"
    assert publish.call_args.args[2] == "0"
    scheduler.schedule_zone_hard_stop.assert_called_once()


def test_on_confirmation_rejects_run_confirmation_write_failure(test_db):
    from services import observed_state

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "on-history-reject", state="starting")
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": "on",
            "observed_state": "unconfirmed",
            "command_id": "on-owner",
            "watering_start_time": "2026-07-23 10:00:00",
        },
    )
    test_db.create_zone_run(zone["id"], 1, "2026-07-23 10:00:00", 0.0, None, 1)
    verifier = observed_state.StateVerifier()
    verifier._db = test_db
    with patch.object(test_db, "mark_zone_run_confirmed", return_value=False):
        assert verifier.apply_live_confirmation(zone["id"], "on", db_instance=test_db) is False


def test_off_confirmation_keeps_jobs_and_token_when_history_close_rejects(test_db):
    from services import observed_state

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "off-history-reject", state="stopping")
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": "off",
            "observed_state": "unconfirmed",
            "command_id": "off-owner",
            "watering_start_time": "2026-07-23 10:00:00",
        },
    )
    test_db.create_zone_run(zone["id"], 1, "2026-07-23 10:00:00", 0.0, None, 1)
    verifier = observed_state.StateVerifier()
    verifier._db = test_db
    scheduler = MagicMock()
    with (
        patch.object(test_db, "finish_zone_run", return_value=False),
        patch("services.events.publish") as publish_event,
    ):
        assert (
            verifier.apply_live_confirmation(
                zone["id"],
                "off",
                db_instance=test_db,
                scheduler_getter=lambda: scheduler,
            )
            is False
        )

    after = test_db.get_zone(zone["id"])
    assert after["observed_state"] == "off"
    assert after["command_id"] == "off-owner"
    scheduler.cancel_zone_jobs.assert_not_called()
    publish_event.assert_not_called()


def test_break_before_make_aborts_before_target_or_master_open(test_db):
    from services import zone_control

    server = _server(test_db)
    group = test_db.create_group("shared-master")
    test_db.update_group_fields(
        group["id"],
        {
            "use_master_valve": 1,
            "master_mqtt_topic": "/masters/main",
            "master_mqtt_server_id": server["id"],
            "master_mode": "NC",
        },
    )
    peer = _mqtt_zone(test_db, server, "peer", group_id=group["id"], state="on")
    target = _mqtt_zone(test_db, server, "target", group_id=group["id"], state="off")
    test_db.update_zone(target["id"], {"observed_state": "off"})
    publishes: list[tuple[str, str]] = []

    def publish(_server, topic, value, **_kwargs):
        publishes.append((topic, value))
        return True

    verifier = MagicMock()
    verifier.verify.return_value = False
    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(zone_control, "water_monitor"),
    ):
        assert zone_control.exclusive_start_zone(target["id"]) is False

    assert publishes == [(peer["topic"], "0")]
    assert test_db.get_zone(peer["id"])["state"] == "fault"
    assert test_db.get_zone(target["id"])["state"] != "on"


def test_break_before_make_orders_confirmed_peer_then_master_then_target(test_db):
    from services import zone_control

    server = _server(test_db)
    group = test_db.create_group("shared-master")
    test_db.update_group_fields(
        group["id"],
        {
            "use_master_valve": 1,
            "master_mqtt_topic": "/masters/main",
            "master_mqtt_server_id": server["id"],
            "master_mode": "NC",
        },
    )
    peer = _mqtt_zone(test_db, server, "peer-ok", group_id=group["id"], state="on")
    target = _mqtt_zone(test_db, server, "target-ok", group_id=group["id"], state="off")
    test_db.update_zone(target["id"], {"observed_state": "off"})
    sequence: list[tuple[str, str]] = []

    def publish(_server, topic, value, **_kwargs):
        sequence.append((topic, value))
        return True

    verifier = MagicMock()
    verifier.verify.return_value = True
    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(zone_control, "water_monitor"),
    ):
        assert zone_control.exclusive_start_zone(target["id"]) is True

    assert sequence[:3] == [(peer["topic"], "0"), ("/masters/main", "1"), (target["topic"], "1")]
    peer_verify = next(call for call in verifier.verify.call_args_list if call.args == (peer["id"], "off"))
    assert peer_verify.kwargs["prepared"] is verifier.prepare_verification.return_value
    assert peer_verify.kwargs["generation"] is verifier.register_command.return_value


def test_break_before_make_reverifies_logical_off_with_unconfirmed_physical_state(test_db):
    from services import zone_control

    server = _server(test_db)
    peer = _mqtt_zone(test_db, server, "bulk-ack-peer", state="off")
    target = _mqtt_zone(test_db, server, "bulk-ack-target", state="off")
    test_db.update_zone(target["id"], {"observed_state": "off"})
    test_db.update_zone(
        peer["id"],
        {
            "commanded_state": "off",
            "observed_state": "unconfirmed",
            "watering_start_time": "2026-07-19 14:00:00",
        },
    )
    publishes: list[tuple[str, str]] = []

    def publish(_server, topic, value, **_kwargs):
        publishes.append((topic, value))
        return True

    verifier = MagicMock()
    verifier.verify.return_value = False
    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(zone_control, "water_monitor"),
    ):
        assert zone_control.exclusive_start_zone(target["id"]) is False

    assert publishes == [(peer["topic"], "0")]
    assert test_db.get_zone(target["id"])["state"] == "off"
    assert verifier.prepare_verification.called
    assert verifier.verify.called


def test_new_activation_waits_for_selected_targets_prior_off_generation(test_db):
    from services import zone_control

    server = _server(test_db)
    target = _mqtt_zone(test_db, server, "selected-bulk-ack", state="off")
    test_db.update_zone(
        target["id"],
        {
            "commanded_state": "off",
            "observed_state": "unconfirmed",
            "watering_start_time": "2026-07-19 14:05:00",
        },
    )
    publishes: list[tuple[str, str]] = []
    verifier = MagicMock()
    verifier.verify.return_value = False

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(
            zone_control,
            "publish_mqtt_value",
            side_effect=lambda _server, topic, value, **_kwargs: publishes.append((topic, value)) or True,
        ),
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(zone_control, "water_monitor"),
    ):
        assert zone_control.exclusive_start_zone(target["id"]) is False

    assert publishes == [(target["topic"], "0")]
    assert all(value != "1" for _, value in publishes)


def test_central_start_rechecks_emergency_inside_group_admission_before_open(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "emergency-admission", state="off")
    test_db.update_zone(zone["id"], {"observed_state": "off"})
    publish = MagicMock(return_value=True)

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", publish),
        patch.object(zone_control, "state_verifier"),
        patch.object(zone_control, "water_monitor"),
        patch.object(zone_control, "_emergency_stop_active", side_effect=[False, True], create=True) as emergency,
        patch.object(zone_control, "_rain_group_blocked", return_value=False, create=True),
    ):
        assert zone_control.exclusive_start_zone(zone["id"]) is False

    assert emergency.call_count >= 2
    publish.assert_not_called()
    assert test_db.get_zone(zone["id"])["state"] == "off"


def test_central_start_fails_closed_when_rain_gate_blocks_group(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "rain-admission", state="off")
    test_db.update_zone(zone["id"], {"observed_state": "off"})

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value") as publish,
        patch.object(zone_control, "_emergency_stop_active", return_value=False, create=True),
        patch.object(zone_control, "_rain_group_blocked", return_value=True, create=True) as rain_gate,
    ):
        assert zone_control.exclusive_start_zone(zone["id"]) is False

    rain_gate.assert_called_with(zone["group_id"])
    publish.assert_not_called()


def test_activation_safety_jobs_and_uuid_are_durable_before_master_or_zone_open(test_db):
    from services import zone_control

    server = _server(test_db)
    group = test_db.create_group("activation-safety")
    test_db.update_group_fields(
        group["id"],
        {
            "use_master_valve": 1,
            "master_mqtt_topic": "/master/activation",
            "master_mqtt_server_id": server["id"],
            "master_mode": "NC",
        },
    )
    zone = _mqtt_zone(test_db, server, "activation-safety", group_id=group["id"], state="off")
    test_db.update_zone(zone["id"], {"observed_state": "off"})
    order: list[str] = []
    scheduler = MagicMock()
    scheduler.schedule_zone_hard_stop.side_effect = lambda *_args, **_kwargs: order.append("hard") or True
    scheduler.schedule_zone_cap.side_effect = lambda *_args, **_kwargs: order.append("cap") or True

    def publish(_server, topic, value, **_kwargs):
        persisted = test_db.get_zone(zone["id"])
        assert persisted["command_id"]
        order.append(f"publish:{topic}:{value}")
        return True

    verifier = MagicMock()
    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "TESTING", False),
        patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(zone_control, "water_monitor"),
        patch.object(zone_control, "_emergency_stop_active", return_value=False, create=True),
        patch.object(zone_control, "_rain_group_blocked", return_value=False, create=True),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        assert zone_control.exclusive_start_zone(zone["id"]) is True

    token = test_db.get_zone(zone["id"])["command_id"]
    assert len(token) == 32
    assert order[:2] == ["hard", "cap"]
    assert order[2:] == ["publish:/master/activation:1", f"publish:{zone['topic']}:1"]
    assert scheduler.schedule_zone_hard_stop.call_args.kwargs["activation_token"] == token
    assert scheduler.schedule_zone_cap.call_args.kwargs["activation_token"] == token


def test_uncertain_on_failure_retains_activation_token_and_preplanted_safety(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "uncertain-on", state="off")
    test_db.update_zone(zone["id"], {"observed_state": "off"})
    scheduler = MagicMock()
    scheduler.schedule_zone_hard_stop.return_value = True
    scheduler.schedule_zone_cap.return_value = True

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "TESTING", False),
        patch.object(zone_control, "publish_mqtt_value", return_value=False),
        patch.object(zone_control, "state_verifier"),
        patch.object(zone_control, "water_monitor"),
        patch.object(zone_control, "_emergency_stop_active", return_value=False, create=True),
        patch.object(zone_control, "_rain_group_blocked", return_value=False, create=True),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        assert zone_control.exclusive_start_zone(zone["id"]) is False

    failed = test_db.get_zone(zone["id"])
    assert failed["state"] == "fault"
    assert failed["command_id"]
    assert failed["watering_start_time"]
    scheduler.schedule_zone_hard_stop.assert_called_once()
    scheduler.schedule_zone_cap.assert_called_once()
    scheduler.cancel_zone_jobs.assert_not_called()


def test_start_fails_closed_when_activation_uuid_is_not_durably_persisted(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "activation-persist-fail", state="off")
    test_db.update_zone(zone["id"], {"observed_state": "off"})

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "_versioned_update", return_value=(False, None)),
        patch.object(zone_control, "publish_mqtt_value") as publish,
        patch.object(zone_control, "state_verifier"),
        patch.object(zone_control, "_emergency_stop_active", return_value=False),
        patch.object(zone_control, "_rain_group_blocked", return_value=False),
    ):
        assert zone_control.exclusive_start_zone(zone["id"]) is False

    publish.assert_not_called()


def test_consecutive_activations_never_reuse_durable_command_id(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "activation-uuid-aba", state="off")
    test_db.update_zone(zone["id"], {"observed_state": "off"})
    tokens: list[str] = []

    def publish(_server, _topic, value, **_kwargs):
        if value == "1":
            tokens.append(str((test_db.get_zone(zone["id"]) or {}).get("command_id")))
        return True

    verifier = MagicMock()
    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(zone_control, "water_monitor"),
        patch.object(zone_control, "_emergency_stop_active", return_value=False),
        patch.object(zone_control, "_rain_group_blocked", return_value=False),
    ):
        assert zone_control.exclusive_start_zone(zone["id"]) is True
        test_db.update_zone(zone["id"], {"state": "off", "observed_state": "off", "watering_start_time": None})
        test_db.update_zone_versioned(
            zone["id"],
            {"command_id": None},
            expected_version=test_db.get_zone(zone["id"])["version"],
        )
        assert zone_control.exclusive_start_zone(zone["id"]) is True

    assert len(tokens) == 2
    assert all(len(token) == 32 for token in tokens)
    assert tokens[0] != tokens[1]


def test_configured_zone_with_missing_server_is_not_virtual(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "broken-physical-zone", state="off")
    test_db.update_zone(zone["id"], {"observed_state": "off"})

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(test_db, "get_mqtt_server", return_value=None),
        patch.object(zone_control, "publish_mqtt_value") as publish,
        patch.object(zone_control, "state_verifier"),
        patch.object(zone_control, "water_monitor"),
    ):
        assert zone_control.exclusive_start_zone(zone["id"]) is False

    assert test_db.get_zone(zone["id"])["state"] == "fault"
    publish.assert_not_called()


def test_start_rereads_wiring_after_group_lock(test_db):
    from services import zone_control

    old_server = _server(test_db, "old-broker")
    new_server = _server(test_db, "new-broker")
    zone = _mqtt_zone(test_db, old_server, "rewired")
    test_db.update_zone(zone["id"], {"observed_state": "off"})
    original_get_zone = test_db.get_zone
    reads = 0

    def get_zone(zone_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            test_db.update_zone(
                zone_id,
                {"topic": "/zones/rewired-new", "mqtt_server_id": new_server["id"]},
            )
        return original_get_zone(zone_id)

    commands: list[tuple[int, str, str]] = []

    def publish(server, topic, value, **_kwargs):
        commands.append((int(server["id"]), topic, value))
        return True

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(test_db, "get_zone", side_effect=get_zone),
        patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
        patch.object(zone_control, "state_verifier"),
        patch.object(zone_control, "water_monitor"),
    ):
        assert zone_control.exclusive_start_zone(zone["id"]) is True

    assert commands == [(new_server["id"], "/zones/rewired-new", "1")]


def test_decryption_failed_server_is_not_virtual_success(test_db):
    from services import zone_control
    from utils import SecretDecryptionError

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "bad-secret")

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(test_db, "get_mqtt_server", side_effect=SecretDecryptionError("wrong key")),
        patch.object(zone_control, "publish_mqtt_value") as publish,
        patch.object(zone_control, "state_verifier"),
        patch.object(zone_control, "water_monitor"),
    ):
        assert zone_control.exclusive_start_zone(zone["id"]) is False

    assert test_db.get_zone(zone["id"])["state"] == "fault"
    publish.assert_not_called()


def test_master_runtime_identity_includes_server_id():
    from services import zone_control

    assert zone_control.master_identity(1, "master/a") == (1, "/master/a")
    assert zone_control.master_topic_lock(1, "master/a") is not zone_control.master_topic_lock(2, "/master/a")


def test_same_topic_on_two_brokers_gets_independent_pending_timers():
    from services import zone_control

    timers = []

    class FakeTimer:
        def __init__(self, _delay, _callback):
            self.cancelled = False
            timers.append(self)

        def cancel(self):
            self.cancelled = True

        def start(self):
            return None

    groups = [
        {
            "id": sid,
            "use_master_valve": 1,
            "master_mqtt_topic": "/master/same",
            "master_mqtt_server_id": sid,
            "master_close_delay_sec": 60,
        }
        for sid in (1, 2)
    ]
    zone_control._PENDING_CLOSE_TIMERS.clear()
    try:
        with (
            patch.object(zone_control, "TESTING", False),
            patch.object(zone_control.threading, "Timer", FakeTimer),
        ):
            for group in groups:
                zone_control._schedule_master_close(group)

        assert set(zone_control._PENDING_CLOSE_TIMERS) == {(1, "/master/same"), (2, "/master/same")}
        assert len([timer for timer in timers if not timer.cancelled]) == 2
    finally:
        zone_control._PENDING_CLOSE_TIMERS.clear()


def test_delayed_master_close_subscribes_before_publish_and_requires_fresh_echo(test_db):
    from services import zone_control

    server = _server(test_db)
    group = test_db.create_group("verified-delayed-close")
    test_db.update_group_fields(
        group["id"],
        {
            "use_master_valve": 1,
            "master_mqtt_topic": "/master/verified",
            "master_mqtt_server_id": server["id"],
            "master_mode": "NC",
        },
    )
    group = next(item for item in test_db.get_groups() if int(item["id"]) == int(group["id"]))
    order: list[str] = []

    class ImmediateTimer:
        def __init__(self, _delay, callback):
            self.callback = callback

        def cancel(self):
            return None

        def start(self):
            self.callback()

    def publish(_server, _topic, _value, **_kwargs):
        order.append("published")
        return True

    def verify(_server_id, _topic, _expected, publish_command, **_kwargs):
        order.append("subscribed")
        assert publish_command() is True
        order.append("fresh_echo")
        return True

    zone_control._PENDING_CLOSE_TIMERS.clear()
    try:
        with (
            patch.object(zone_control, "db", test_db),
            patch.object(zone_control, "TESTING", False),
            patch.object(zone_control.threading, "Timer", ImmediateTimer),
            patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
            patch.object(zone_control.state_verifier, "verify_master_command", side_effect=verify) as verifier,
        ):
            zone_control._schedule_master_close(group, immediate=True)
    finally:
        zone_control._PENDING_CLOSE_TIMERS.clear()

    assert order == ["subscribed", "published", "fresh_echo"]
    verifier.assert_called_once()


def test_manual_master_open_plants_exact_cap_then_persists_token_before_publish(test_db):
    from services import zone_control

    server = _server(test_db)
    scheduler = MagicMock()
    order: list[str] = []
    identity = (server["id"], "/master/manual")

    def schedule(group_id, server_id, topic, mode, token, **kwargs):
        assert (group_id, server_id, topic, mode) == (1, *identity, "NC")
        assert len(token) == 32
        assert kwargs == {"hours": 24}
        assert test_db.get_setting_value(zone_control._master_activation_key(identity)) is None
        order.append("cap")
        return True

    scheduler.schedule_master_valve_cap.side_effect = schedule

    def publish():
        raw = test_db.get_setting_value(zone_control._master_activation_key(identity))
        assert raw is not None
        assert zone_control._MASTER_ACTIVATIONS[identity].token in raw
        order.append("publish")
        return True

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "_MASTER_ACTIVATIONS", {}),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        assert zone_control.activate_manual_master_open(1, *identity, "NC", publish) is True

    assert order == ["cap", "publish"]
    scheduler.cancel_master_valve_cap.assert_not_called()


def test_confirmed_master_close_preserves_pending_retry_until_fresh_echo(test_db):
    from services import zone_control

    identity = (17, "/master/pending-retry")
    timer = MagicMock()
    publish = MagicMock(return_value=True)
    zone_control._PENDING_CLOSE_TIMERS.clear()
    try:
        zone_control._PENDING_CLOSE_TIMERS[identity] = timer
        with patch.object(zone_control.state_verifier, "verify_master_command", return_value=False):
            assert zone_control.close_master_valve_confirmed(*identity, "NC", publish) is False
        assert zone_control._PENDING_CLOSE_TIMERS[identity] is timer
        timer.cancel.assert_not_called()

        with patch.object(zone_control.state_verifier, "verify_master_command", return_value=True):
            assert zone_control.close_master_valve_confirmed(*identity, "NC", publish) is True
        assert identity not in zone_control._PENDING_CLOSE_TIMERS
        timer.cancel.assert_called_once()
    finally:
        zone_control._PENDING_CLOSE_TIMERS.clear()


@pytest.mark.parametrize("schedule_result", [False, None])
def test_rejected_manual_master_cap_preserves_previous_activation(test_db, schedule_result):
    from services import zone_control

    server = _server(test_db)
    identity = (server["id"], "/master/manual-existing")
    previous = zone_control.MasterActivation(1, *identity, "NC", "a" * 32, 10.0)
    scheduler = MagicMock()
    scheduler.schedule_master_valve_cap.return_value = schedule_result
    publish = MagicMock(return_value=True)

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "_MASTER_ACTIVATIONS", {}),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        assert zone_control._persist_master_activation_locked(previous) is True
        assert zone_control.activate_manual_master_open(1, *identity, "NC", publish) is False
        assert zone_control._load_master_activation_locked(identity) == previous

    publish.assert_not_called()
    scheduler.cancel_master_valve_cap.assert_not_called()


def test_uncertain_manual_master_open_retains_new_exact_cap_and_token(test_db):
    from services import zone_control

    server = _server(test_db)
    identity = (server["id"], "/master/manual-uncertain")
    scheduler = MagicMock()
    scheduler.schedule_master_valve_cap.return_value = True

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "_MASTER_ACTIVATIONS", {}),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        assert zone_control.activate_manual_master_open(1, *identity, "NC", lambda: False) is False
        current = zone_control._load_master_activation_locked(identity)
        assert current is not None
        assert len(current.token) == 32
        assert current.token == scheduler.schedule_master_valve_cap.call_args.args[4]

    scheduler.cancel_master_valve_cap.assert_not_called()


def test_master_cap_callback_is_token_exact_and_clears_only_after_fresh_echo(test_db):
    from services import zone_control

    server = _server(test_db)
    identity = (server["id"], "/master/cap-exact")
    current = zone_control.MasterActivation(1, *identity, "NC", "b" * 32, 20.0)
    scheduler = MagicMock()
    scheduler.cancel_master_valve_cap.return_value = True
    verifier = MagicMock(return_value=False)

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "_MASTER_ACTIVATIONS", {}),
        patch.object(zone_control.state_verifier, "verify_master_command", verifier),
        patch.object(zone_control, "publish_mqtt_value", return_value=True) as publish,
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        assert zone_control._persist_master_activation_locked(current) is True

        assert zone_control.close_master_valve_if_activation(1, *identity, "NC", "c" * 32) is True
        verifier.assert_not_called()
        publish.assert_not_called()

        assert zone_control.close_master_valve_if_activation(1, *identity, "NC", current.token) is False
        assert zone_control._load_master_activation_locked(identity) == current
        scheduler.cancel_master_valve_cap.assert_not_called()

        def fresh_echo(_sid, _topic, _expected, publish_command, **_kwargs):
            assert publish_command() is True
            return True

        verifier.side_effect = fresh_echo
        assert zone_control.close_master_valve_if_activation(1, *identity, "NC", current.token) is True
        assert zone_control._load_master_activation_locked(identity) is None

    scheduler.cancel_master_valve_cap.assert_called_once_with(1, *identity, "NC", current.token)
    publish.assert_called_once()


def test_queued_master_closed_echo_cannot_clear_newer_activation(test_db):
    from services import zone_control

    server = _server(test_db)
    identity = (server["id"], "/master/queued-echo")
    current = zone_control.MasterActivation(1, *identity, "NC", "d" * 32, 100.0)
    scheduler = MagicMock()
    scheduler.cancel_master_valve_cap.return_value = True

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "_MASTER_ACTIVATIONS", {}),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        assert zone_control._persist_master_activation_locked(current) is True
        assert zone_control.confirm_master_closed_from_echo(*identity, "NC", received_at=99.0) is False
        assert zone_control._load_master_activation_locked(identity) == current
        scheduler.cancel_master_valve_cap.assert_not_called()

        assert zone_control.confirm_master_closed_from_echo(*identity, "NC", received_at=101.0) is True
        assert zone_control._load_master_activation_locked(identity) is None

    scheduler.cancel_master_valve_cap.assert_called_once_with(1, *identity, "NC", current.token)


def test_gate_flip_after_master_open_freshly_closes_master_without_opening_zone(test_db):
    from services import zone_control

    server = _server(test_db)
    group = test_db.create_group("gate-flip-after-master")
    test_db.update_group_fields(
        group["id"],
        {
            "use_master_valve": 1,
            "master_mqtt_topic": "/master/gate-flip",
            "master_mqtt_server_id": server["id"],
            "master_mode": "NC",
        },
    )
    zone = _mqtt_zone(test_db, server, "gate-flip-target", group_id=group["id"], state="off")
    test_db.update_zone(zone["id"], {"observed_state": "off"})
    commands: list[tuple[str, str]] = []

    def publish(_server, topic, value, **_kwargs):
        commands.append((topic, value))
        return True

    verifier = MagicMock()

    def verify_close(_sid, _topic, _expected, publish_command, **_kwargs):
        assert publish_command() is True
        return True

    verifier.verify_master_command.side_effect = verify_close
    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(zone_control, "water_monitor"),
        patch.object(zone_control, "_emergency_stop_active", side_effect=[False, False, False, False, True]),
        patch.object(zone_control, "_rain_group_blocked", return_value=False),
    ):
        assert zone_control.exclusive_start_zone(zone["id"]) is False

    assert commands == [("/master/gate-flip", "1"), ("/master/gate-flip", "0")]
    assert all(topic != zone["topic"] for topic, _value in commands)
    assert test_db.get_zone(zone["id"])["state"] == "fault"


def test_cancel_generation_guard_is_rechecked_immediately_before_target_open(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "cancel-generation-target", state="off")
    test_db.update_zone(zone["id"], {"observed_state": "off"})
    cancel_guard = MagicMock(side_effect=[False, False, False, True])

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value") as publish,
        patch.object(zone_control, "state_verifier"),
        patch.object(zone_control, "water_monitor"),
        patch.object(zone_control, "_emergency_stop_active", return_value=False),
        patch.object(zone_control, "_rain_group_blocked", return_value=False),
    ):
        assert zone_control.exclusive_start_zone(zone["id"], cancel_guard=cancel_guard) is False

    assert cancel_guard.call_count == 4
    publish.assert_not_called()
    assert test_db.get_zone(zone["id"])["state"] == "fault"


def test_stop_all_reports_unresolved_failed_off(test_db):
    from services import zone_control

    server = _server(test_db)
    failed = _mqtt_zone(test_db, server, "failed-off", state="on")
    stopped = _mqtt_zone(test_db, server, "stopped-off", state="on")

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(
            zone_control,
            "stop_zone",
            side_effect=lambda zone_id, **_kwargs: int(zone_id) == int(stopped["id"]),
        ) as stop,
    ):
        result = zone_control.stop_all_in_group(1, force=True, require_observed_confirmation=True)

    assert result == {
        "success": False,
        "group_id": 1,
        "stopped": [stopped["id"]],
        "unresolved": [failed["id"]],
        "retry_scheduled": False,
    }
    assert all(call.kwargs["require_observed_confirmation"] is True for call in stop.call_args_list)


def test_stop_all_fails_closed_when_repository_collapses_sqlite_error_to_empty(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "strict-group-read", state="on")

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(test_db.zones, "_connect", side_effect=sqlite3.OperationalError("partition unreadable")),
        patch.object(zone_control, "stop_zone") as stop,
    ):
        # This is the repository behavior that previously became a false
        # successful empty partition in the central primitive.
        assert test_db.get_zones_by_group(zone["group_id"]) == []
        result = zone_control.stop_all_in_group(zone["group_id"], force=True)

    assert result == {
        "success": False,
        "group_id": zone["group_id"],
        "stopped": [],
        "unresolved": [],
        "retry_scheduled": False,
    }
    stop.assert_not_called()


def test_direct_stop_all_uses_strict_complete_group_partition(test_db):
    from services import zone_control

    server = _server(test_db)
    zones = [
        _mqtt_zone(test_db, server, "strict-direct-a", state="on"),
        _mqtt_zone(test_db, server, "strict-direct-b", state="on"),
    ]
    expected_ids = [zone["id"] for zone in zones]

    with (
        patch.object(zone_control, "db", test_db),
        # Prove the safety primitive is independent of the ambiguous helper.
        patch.object(test_db, "get_zones_by_group", return_value=[]),
        patch.object(zone_control, "stop_zone", return_value=True) as stop,
    ):
        result = zone_control.stop_all_in_group(1, force=True)

    assert result == {
        "success": True,
        "group_id": 1,
        "stopped": expected_ids,
        "unresolved": [],
        "retry_scheduled": False,
    }
    assert [call.args[0] for call in stop.call_args_list] == expected_ids


def test_emergency_stop_reports_failed_zone_and_never_counts_it_stopped(test_db):
    from services import zone_control

    server = _server(test_db)
    group = test_db.create_group("emergency")
    zone = _mqtt_zone(test_db, server, "emergency-fail", group_id=group["id"], state="on")

    with patch.object(zone_control, "db", test_db), patch.object(zone_control, "stop_zone", return_value=False):
        stats = zone_control.emergency_stop_all()

    assert stats["success"] is False
    assert stats["zones_stopped"] == 0
    assert stats["zones_failed"] == [zone["id"]]


def test_emergency_missing_group_snapshot_still_stops_complete_zone_partition(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "emergency-no-groups", state="on")
    test_db.update_zone(zone["id"], {"observed_state": "on", "commanded_state": "on"})

    def stop_zone(zone_id, **_kwargs):
        test_db.update_zone(zone_id, {"state": "off", "observed_state": "off", "commanded_state": "off"})
        return True

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(test_db, "get_groups", return_value=[]),
        patch.object(zone_control, "stop_zone", side_effect=stop_zone) as stop,
    ):
        stats = zone_control.emergency_stop_all()

    stop.assert_called_once_with(
        zone["id"],
        reason="emergency_stop",
        force=True,
        master_close_immediately=False,
        skip_master_close=True,
        require_observed_confirmation=True,
    )
    assert stats["success"] is False
    assert stats["zones_stopped"] == 1
    assert stats["zones_failed"] == []
    assert "groups:snapshot_empty_or_unavailable" in stats["errors"]


def test_emergency_broker_ack_without_fresh_zone_off_echo_is_unresolved(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "emergency-ack-only", state="on")
    test_db.update_zone(zone["id"], {"observed_state": "on", "commanded_state": "on"})

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "stop_zone", return_value=True) as stop,
    ):
        stats = zone_control.emergency_stop_all()

    assert stats["success"] is False
    assert stats["zones_stopped"] == 0
    assert stats["zones_failed"] == [zone["id"]]
    assert stop.call_args.kwargs["require_observed_confirmation"] is True


@pytest.mark.parametrize("observed", [None, "", "unconfirmed", "on"])
def test_emergency_empty_or_stale_physical_observation_is_unresolved(test_db, observed):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, f"emergency-observed-{observed}", state="off")
    test_db.update_zone(zone["id"], {"observed_state": observed, "commanded_state": "off"})

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "stop_zone", return_value=True),
    ):
        stats = zone_control.emergency_stop_all()

    assert stats["success"] is False
    assert stats["zones_stopped"] == 0
    assert stats["zones_failed"] == [zone["id"]]


def test_emergency_stop_includes_orphaned_zone_from_complete_snapshot(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "emergency-orphan", state="on")
    # The repository now rejects dangling group references.  Model a legacy
    # corrupted row directly so this emergency-path regression still proves
    # that the complete zone snapshot is used instead of a group join.
    with test_db.zones._connect() as conn:
        conn.execute("DROP TRIGGER trg_zones_group_update")
        conn.execute("UPDATE zones SET group_id = ? WHERE id = ?", (424242, zone["id"]))
        conn.commit()
    test_db.update_zone(zone["id"], {"observed_state": "on", "commanded_state": "on"})

    def stop_orphan(zone_id, **_kwargs):
        assert zone_id == zone["id"]
        test_db.update_zone(zone_id, {"state": "off", "observed_state": "off", "commanded_state": "off"})
        return True

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "stop_zone", side_effect=stop_orphan) as stop,
    ):
        stats = zone_control.emergency_stop_all()

    stop.assert_called_once()
    assert stats["success"] is True
    assert stats["zones_stopped"] == 1
    assert stats["zones_failed"] == []


def test_emergency_master_ack_without_fresh_close_echo_is_unresolved(test_db):
    from services import zone_control

    server = _server(test_db)
    group = test_db.create_group("emergency-master-echo")
    test_db.update_group_fields(
        group["id"],
        {
            "use_master_valve": 1,
            "master_mqtt_topic": "/master/emergency",
            "master_mqtt_server_id": server["id"],
            "master_mode": "NC",
        },
    )

    def ack_without_echo(_sid, _topic, _expected, publish_command, **_kwargs):
        assert publish_command() is True
        return False

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", return_value=True) as publish,
        patch.object(
            zone_control.state_verifier,
            "verify_master_command",
            side_effect=ack_without_echo,
        ) as verify,
    ):
        stats = zone_control.emergency_stop_all()

    assert publish.call_args.kwargs["retain"] is True
    verify.assert_called_once()
    assert stats["success"] is False
    assert stats["masters_closed"] == 0
    assert stats["masters_failed_publish"] == 1


def test_emergency_deduplicates_master_by_broker_and_topic(test_db):
    from services import zone_control

    servers = [_server(test_db, f"broker-{sid}") for sid in (1, 2)]
    for index, server in enumerate(servers, start=1):
        group = test_db.create_group(f"master-{index}")
        test_db.update_group_fields(
            group["id"],
            {
                "use_master_valve": 1,
                "master_mqtt_topic": "/master/same",
                "master_mqtt_server_id": server["id"],
                "master_mode": "NC",
            },
        )
    published_servers: list[int] = []

    def publish(server, _topic, _value, **_kwargs):
        published_servers.append(int(server["id"]))
        return True

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
        patch.object(
            zone_control.state_verifier,
            "verify_master_command",
            side_effect=lambda _sid, _topic, _expected, callback, **_kwargs: callback(),
        ),
    ):
        stats = zone_control.emergency_stop_all()

    assert sorted(published_servers) == sorted(server["id"] for server in servers)
    assert stats["masters_closed"] == 2
    assert stats["masters_skipped_dup_topic"] == 0


def test_emergency_missing_master_channel_is_reported_unresolved(test_db):
    from services import zone_control

    group = test_db.create_group("broken-master")
    test_db.update_group_fields(group["id"], {"use_master_valve": 1})

    with patch.object(zone_control, "db", test_db):
        stats = zone_control.emergency_stop_all()

    assert stats["success"] is False
    assert stats["masters_skipped_no_topic"] == 1
    assert stats["masters_failed_publish"] == 1


def test_close_all_master_valves_counts_only_confirmed_publishes():
    from services.master_valve import close_all_master_valves

    db = MagicMock()
    db.get_groups.return_value = [
        {
            "use_master_valve": 1,
            "master_mqtt_topic": "/master/a",
            "master_mqtt_server_id": 1,
            "master_mode": "NC",
        }
    ]
    db.get_mqtt_server.return_value = {"id": 1}

    with patch("services.master_valve.time.sleep"):
        assert close_all_master_valves(db, MagicMock(return_value=False), retries=3) == 0


@pytest.mark.parametrize("published", [True, False])
def test_zone_cap_survives_until_physical_off_confirmation(test_db, published):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, f"cap-{published}", state="on")
    scheduler = MagicMock()

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", return_value=published),
        patch.object(zone_control, "state_verifier"),
        patch.object(zone_control, "water_monitor"),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        assert zone_control.stop_zone(zone["id"], force=True) is published

    scheduler.cancel_zone_cap.assert_not_called()


def test_verifier_cancels_cap_after_fresh_physical_off(test_db):
    from services.observed_state import StateVerifier

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "confirmed-off", state="stopping")
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": "off",
            "observed_state": "unconfirmed",
            "watering_start_time": "2026-07-19 12:30:00",
        },
    )
    test_db.update_zone_versioned(
        zone["id"],
        {"command_id": "b" * 32},
        expected_version=test_db.get_zone(zone["id"])["version"],
    )
    scheduler = MagicMock()
    verifier = StateVerifier()
    verifier._db = test_db

    with (
        patch.object(verifier, "_subscribe_and_wait", return_value=True),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        assert verifier.verify(zone["id"], "off", timeout=0.001, retries=1) is True

    scheduler.cancel_zone_jobs.assert_called_once_with(zone["id"], include_cap=True)
    confirmed = test_db.get_zone(zone["id"])
    assert confirmed["observed_state"] == "off"
    assert confirmed["watering_start_time"] is None
    assert confirmed["command_id"] is None


def test_normal_stop_stays_pending_until_matching_physical_off_closes_run(test_db):
    from services import observed_state, zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "pending-off-run", state="on")
    started_at = "2026-07-19 12:30:00"
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": "on",
            "observed_state": "on",
            "watering_start_time": started_at,
        },
    )
    test_db.update_zone_versioned(
        zone["id"],
        {"command_id": "c" * 32},
        expected_version=test_db.get_zone(zone["id"])["version"],
    )
    run_id = test_db.create_zone_run(zone["id"], 1, started_at, 0.0, None, 1)
    assert run_id is not None
    assert test_db.mark_zone_run_confirmed(zone["id"]) is True

    verifier = observed_state.StateVerifier()
    verifier._db = test_db
    prepared = object()
    scheduler = MagicMock()
    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(verifier, "prepare_verification", return_value=prepared),
        patch.object(verifier, "verify_async") as verify_async,
        patch.object(zone_control, "publish_mqtt_value", return_value=True),
        patch.object(zone_control, "water_monitor") as water_monitor,
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch("services.events.publish") as publish_event,
    ):
        water_monitor.get_pulses_at_or_after.return_value = None
        water_monitor.summarize_run.return_value = (None, None)
        assert zone_control.stop_zone(zone["id"], force=True) is True

        pending = test_db.get_zone(zone["id"])
        assert pending["state"] == "stopping"
        assert pending["observed_state"] == "unconfirmed"
        assert pending["watering_start_time"] == started_at
        assert test_db.get_open_zone_run(zone["id"])["id"] == run_id
        assert not any(call.args[0].get("type") == "zone_stop" for call in publish_event.call_args_list)

        generation = verifier._generations[zone["id"]]
        assert verifier._apply_confirmation_if_current(zone["id"], "off", generation) is True

    verify_async.assert_called_once_with(
        zone["id"],
        "off",
        generation=generation,
        prepared=prepared,
    )
    completed = test_db.get_zone(zone["id"])
    assert completed["state"] == "off"
    assert completed["observed_state"] == "off"
    assert completed["watering_start_time"] is None
    assert completed["command_id"] is None
    assert test_db.get_open_zone_run(zone["id"]) is None
    with sqlite3.connect(test_db.db_path) as conn:
        status, end_utc = conn.execute("SELECT status, end_utc FROM zone_runs WHERE id = ?", (run_id,)).fetchone()
    assert status == "ok"
    assert end_utc is not None
    assert any(call.args[0].get("type") == "zone_stop" for call in publish_event.call_args_list)


def test_duplicate_stop_while_physical_off_pending_keeps_run_open(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "duplicate-pending-off", state="stopping")
    started_at = "2026-07-19 12:35:00"
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": "off",
            "observed_state": "unconfirmed",
            "watering_start_time": started_at,
        },
    )
    run_id = test_db.create_zone_run(zone["id"], 1, started_at, 0.0, None, 1)
    assert run_id is not None
    assert test_db.mark_zone_run_confirmed(zone["id"]) is True

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value") as publish,
        patch.object(zone_control, "water_monitor") as water_monitor,
    ):
        assert zone_control.stop_zone(zone["id"]) is True

    publish.assert_not_called()
    water_monitor.summarize_run.assert_not_called()
    assert test_db.get_zone(zone["id"])["state"] == "stopping"
    assert test_db.get_open_zone_run(zone["id"])["id"] == run_id


def test_logical_off_without_fresh_physical_off_reissues_command_and_keeps_run_open(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "legacy-ack-off", state="off")
    started_at = "2026-07-19 12:40:00"
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": "off",
            "observed_state": "unconfirmed",
            "watering_start_time": started_at,
        },
    )
    run_id = test_db.create_zone_run(zone["id"], 1, started_at, 0.0, None, 1)
    assert run_id is not None
    assert test_db.mark_zone_run_confirmed(zone["id"]) is True
    verifier = MagicMock()
    verifier.prepare_verification.return_value = object()

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "publish_mqtt_value", return_value=True) as publish,
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(zone_control, "water_monitor"),
    ):
        assert zone_control.stop_zone(zone["id"]) is True

    publish.assert_called_once()
    current = test_db.get_zone(zone["id"])
    assert current["state"] == "stopping"
    assert current["observed_state"] == "unconfirmed"
    assert test_db.get_open_zone_run(zone["id"])["id"] == run_id


def test_rejected_confirmation_has_no_run_or_job_side_effects(test_db):
    from services import observed_state

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "confirmation-cas-race", state="starting")
    test_db.update_zone(zone["id"], {"commanded_state": "on", "observed_state": "unconfirmed"})
    run_id = test_db.create_zone_run(zone["id"], 1, "2026-07-19 12:30:00", 0.0, None, 1)
    assert run_id is not None
    verifier = observed_state.StateVerifier()
    verifier._db = test_db
    generation = verifier.register_command(zone["id"], "on")
    scheduler = MagicMock()

    with (
        patch(
            "services.zones_state.update_zone_state_internal",
            return_value=(False, test_db.get_zone(zone["id"])),
        ),
        patch.object(test_db, "mark_zone_run_confirmed", wraps=test_db.mark_zone_run_confirmed) as mark_confirmed,
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        assert verifier._apply_confirmation_if_current(zone["id"], "on", generation) is False

    mark_confirmed.assert_not_called()
    scheduler.cancel_zone_jobs.assert_not_called()
    with sqlite3.connect(test_db.db_path) as conn:
        confirmed = conn.execute("SELECT confirmed FROM zone_runs WHERE id = ?", (run_id,)).fetchone()[0]
    assert confirmed == 0


def test_rejected_fault_cas_does_not_alert_or_claim_fault(test_db):
    from services import observed_state

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "fault-cas-race", state="starting")
    test_db.update_zone(zone["id"], {"commanded_state": "on", "observed_state": "unconfirmed"})
    verifier = observed_state.StateVerifier()
    verifier._db = test_db
    verifier._notifier = MagicMock()

    with (
        patch(
            "services.zones_state.update_zone_state_internal",
            return_value=(False, test_db.get_zone(zone["id"])),
        ),
        patch("services.events.publish") as publish_event,
        patch.object(observed_state.logger, "critical") as critical,
    ):
        assert verifier._record_fault(zone["id"], test_db.get_zone(zone["id"]), "on") is False

    assert test_db.get_zone(zone["id"])["state"] == "starting"
    publish_event.assert_not_called()
    verifier._notifier.send_text.assert_not_called()
    critical.assert_not_called()


def test_group_exclusivity_false_stop_uses_fault_fallback_and_truthful_log(test_db):
    import app as app_module

    newer = test_db.create_zone({"name": "kept", "duration": 10, "group_id": 1})
    older = test_db.create_zone({"name": "loser", "duration": 10, "group_id": 1})
    test_db.update_zone(
        newer["id"],
        {"state": "on", "watering_start_time": "2026-07-19 13:01:00"},
    )
    test_db.update_zone(
        older["id"],
        {
            "state": "on",
            "observed_state": "on",
            "watering_start_time": "2026-07-19 13:00:00",
        },
    )

    with (
        patch.object(app_module, "db", test_db),
        patch("services.zone_control.stop_zone", return_value=False) as stop,
    ):
        app_module._force_group_exclusive(1, "race-test")

    assert stop.call_args_list == [
        call(
            older["id"],
            reason="group_exclusive",
            force=True,
            master_close_immediately=False,
            skip_master_close=True,
            require_observed_confirmation=True,
        ),
        call(
            newer["id"],
            reason="group_exclusive",
            force=True,
            master_close_immediately=True,
            skip_master_close=False,
            require_observed_confirmation=True,
        ),
    ]
    assert test_db.get_zone(older["id"])["state"] == "fault"
    assert test_db.get_zone(older["id"])["observed_state"] == "on"
    assert test_db.get_zone(newer["id"])["state"] == "fault"
    log = test_db.get_logs("warning")[0]
    details = json.loads(log["details"])
    assert details["kept_zone"] is None
    assert details["group_shutdown"] is True
    assert details["turned_off"] == []
    assert details["faulted"] == [older["id"], newer["id"]]


def test_group_exclusivity_reads_election_after_waiting_for_group_lock(test_db):
    import app as app_module
    from services.locks import group_lock

    newer = test_db.create_zone({"name": "lock-kept", "duration": 10, "group_id": 1})
    older = test_db.create_zone({"name": "lock-old", "duration": 10, "group_id": 1})
    test_db.update_zone(newer["id"], {"state": "on", "watering_start_time": "2026-07-19 13:01:00"})
    test_db.update_zone(older["id"], {"state": "on", "watering_start_time": "2026-07-19 13:00:00"})
    entered = threading.Event()
    stop_entered = threading.Event()

    def run_watchdog():
        entered.set()
        app_module._force_group_exclusive(1, "lock-race")

    def stopped(*_args, **_kwargs):
        stop_entered.set()
        return True

    with (
        patch.object(app_module, "db", test_db),
        patch("services.zone_control.stop_zone", side_effect=stopped) as stop,
    ):
        with group_lock(1):
            worker = threading.Thread(target=run_watchdog)
            worker.start()
            assert entered.wait(timeout=1)
            assert stop_entered.wait(timeout=0.1) is False
            test_db.update_zone(older["id"], {"state": "off"})
        worker.join(timeout=2)

    assert worker.is_alive() is False
    stop.assert_not_called()


def test_group_exclusivity_rejected_one_shot_fallback_is_reported_failed(test_db):
    import app as app_module

    newer = test_db.create_zone({"name": "strict-kept", "duration": 10, "group_id": 1})
    older = test_db.create_zone({"name": "strict-old", "duration": 10, "group_id": 1})
    test_db.update_zone(newer["id"], {"state": "on", "watering_start_time": "2026-07-19 13:01:00"})
    test_db.update_zone(older["id"], {"state": "on", "watering_start_time": "2026-07-19 13:00:00"})

    with (
        patch.object(app_module, "db", test_db),
        patch("services.zone_control.stop_zone", return_value=False),
        patch(
            "services.zones_state.update_zone_state",
            return_value=(False, test_db.get_zone(older["id"])),
        ) as fallback,
    ):
        app_module._force_group_exclusive(1, "strict-race")

    assert fallback.call_count == 2
    assert fallback.call_args_list[0].kwargs["expected_version"] == test_db.get_zone(older["id"])["version"]
    assert test_db.get_zone(older["id"])["state"] == "on"
    details = json.loads(test_db.get_logs("warning")[0]["details"])
    assert details["turned_off"] == []
    assert details["faulted"] == []
    assert details["failed"] == [older["id"], newer["id"]]
    assert details["group_shutdown"] is True


def test_group_exclusivity_counts_transitional_and_faulted_physical_rows_without_double_fault(test_db):
    import app as app_module

    server = _server(test_db)
    keeper = _mqtt_zone(test_db, server, "transition-keeper", state="on")
    stopping = _mqtt_zone(test_db, server, "transition-stopping", state="stopping")
    faulted = _mqtt_zone(test_db, server, "transition-fault", state="fault")
    test_db.update_zone(keeper["id"], {"watering_start_time": "2026-07-23 10:02:00"})
    test_db.update_zone(
        stopping["id"],
        {"observed_state": "unconfirmed", "watering_start_time": "2026-07-23 10:01:00"},
    )
    test_db.update_zone(
        faulted["id"],
        {
            "state": "fault",
            "commanded_state": "off",
            "observed_state": "on",
            "fault_count": 4,
            "watering_start_time": "2026-07-23 10:00:00",
        },
    )
    with (
        patch.object(app_module, "db", test_db),
        patch("services.zone_control.stop_zone", return_value=False) as stop,
    ):
        app_module._force_group_exclusive(1, "transitional")

    assert [item.args[0] for item in stop.call_args_list] == [stopping["id"], faulted["id"], keeper["id"]]
    assert test_db.get_zone(faulted["id"])["fault_count"] == 4
    details = json.loads(test_db.get_logs("warning")[0]["details"])
    assert details["group_shutdown"] is True
    assert faulted["id"] in details["unresolved"]


def test_command_fault_preserves_last_confirmed_observed_state(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "fault-preserves-observed", state="on")
    test_db.update_zone(zone["id"], {"commanded_state": "on", "observed_state": "on"})

    with (
        patch.object(zone_control, "db", test_db),
        patch.object(zone_control, "state_verifier"),
    ):
        assert zone_control.mark_zone_command_fault(zone["id"], "off", reason="publish_failed") is True

    current = test_db.get_zone(zone["id"])
    assert current["state"] == "fault"
    assert current["observed_state"] == "on"


def test_stale_off_confirmation_cannot_clear_new_activation_after_recheck(test_db):
    from services import observed_state

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "generation-apply-race", state="on")
    old_token = "2026-07-19 13:00:00"
    new_token = "2026-07-19 13:01:00"
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": "off",
            "observed_state": "unconfirmed",
            "watering_start_time": old_token,
        },
    )
    verifier = observed_state.StateVerifier()
    verifier._db = test_db
    generation = verifier.register_command(zone["id"], "off")
    result = threading.Event()
    result.set()
    prepared = observed_state._PreparedVerification(
        zone_id=zone["id"],
        expected="off",
        generation=generation,
        server=server,
        topic=zone["topic"],
        expected_payloads={"0"},
        client=MagicMock(),
        result=result,
    )
    recheck_entered = threading.Event()
    allow_apply = threading.Event()
    new_command_done = threading.Event()
    checks = 0

    def is_current(*_args, **_kwargs):
        nonlocal checks
        checks += 1
        if checks == 2:
            recheck_entered.set()
            assert allow_apply.wait(timeout=2)
        return True

    def apply_stale_off(zone_id, _expected):
        test_db.update_zone(zone_id, {"observed_state": "off", "watering_start_time": None})

    def start_new_generation():
        verifier.register_command(zone["id"], "on")
        test_db.update_zone(
            zone["id"],
            {
                "commanded_state": "on",
                "observed_state": "unconfirmed",
                "watering_start_time": new_token,
            },
        )
        new_command_done.set()

    with (
        patch.object(observed_state, "mqtt", object()),
        patch.object(verifier, "_is_current", side_effect=is_current),
        patch.object(verifier, "_apply_confirmation", side_effect=apply_stale_off),
    ):
        old_verify = threading.Thread(
            target=lambda: verifier.verify(
                zone["id"],
                "off",
                generation=generation,
                prepared=prepared,
                retries=1,
                timeout=0.01,
            )
        )
        old_verify.start()
        assert recheck_entered.wait(timeout=1)
        new_command = threading.Thread(target=start_new_generation)
        new_command.start()
        new_command.join(timeout=0.05)
        assert new_command_done.is_set() is False
        allow_apply.set()
        old_verify.join(timeout=1)
        new_command.join(timeout=1)

    assert new_command_done.is_set()
    assert test_db.get_zone(zone["id"])["watering_start_time"] == new_token


def test_prepared_verifier_catches_immediate_nonretained_echo_after_subscription(test_db):
    from services import observed_state

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "prepared-immediate", state="on")
    test_db.update_zone(
        zone["id"],
        {
            "commanded_state": "off",
            "observed_state": "unconfirmed",
            "watering_start_time": "2026-07-19 13:00:00",
        },
    )
    events: list[str] = []

    class Client:
        def username_pw_set(self, *_args):
            return None

        def connect(self, *_args, **_kwargs):
            return None

        def subscribe(self, *_args, **_kwargs):
            events.append("subscribed")
            return 0, 1

        def loop_start(self):
            self.on_connect(self, None, None, 0, None)
            self.on_subscribe(self, None, 1, [1], None)

        def loop_stop(self):
            return None

        def disconnect(self):
            return None

    client = Client()
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(return_value=client),
    )
    verifier = observed_state.StateVerifier()
    verifier._db = test_db
    generation = verifier.register_command(zone["id"], "off")

    with (
        patch.object(observed_state, "mqtt", fake_mqtt),
        patch("irrigation_scheduler.get_scheduler", return_value=MagicMock()),
    ):
        prepared = verifier.prepare_verification(zone["id"], "off", generation=generation, timeout=0.1)
        assert prepared is not None
        assert events == ["subscribed"]

        events.append("published")
        client.on_message(
            client,
            None,
            SimpleNamespace(payload=b"0", retain=False),
        )
        assert (
            verifier.verify(
                zone["id"],
                "off",
                timeout=0.001,
                retries=1,
                generation=generation,
                prepared=prepared,
            )
            is True
        )

    assert events == ["subscribed", "published"]
    assert fake_mqtt.Client.call_count == 1
    after = test_db.get_zone(zone["id"])
    assert after["observed_state"] == "off"
    assert after["watering_start_time"] is None


def test_prepared_verifier_keeps_one_subscription_across_retry(test_db):
    from services import observed_state

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "prepared-retry", state="stopping")
    test_db.update_zone(zone["id"], {"commanded_state": "off", "observed_state": "unconfirmed"})
    subscriptions = 0

    class Client:
        def connect(self, *_args, **_kwargs):
            return None

        def subscribe(self, *_args, **_kwargs):
            nonlocal subscriptions
            subscriptions += 1
            return 0, subscriptions

        def loop_start(self):
            self.on_connect(self, None, None, 0, None)
            self.on_subscribe(self, None, 1, [1], None)

        def loop_stop(self):
            return None

        def disconnect(self):
            return None

    client = Client()
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(return_value=client),
    )
    verifier = observed_state.StateVerifier()
    verifier._db = test_db
    generation = verifier.register_command(zone["id"], "off")

    def retry_publish(*_args, **_kwargs):
        client.on_message(client, None, SimpleNamespace(payload=b"0", retain=False))
        return True

    with (
        patch.object(observed_state, "mqtt", fake_mqtt),
        patch("services.mqtt_pub.publish_mqtt_value", side_effect=retry_publish) as publish,
        patch("irrigation_scheduler.get_scheduler", return_value=MagicMock()),
    ):
        prepared = verifier.prepare_verification(zone["id"], "off", generation=generation, timeout=0.1)
        assert prepared is not None
        assert (
            verifier.verify(
                zone["id"],
                "off",
                timeout=0.001,
                retries=2,
                generation=generation,
                prepared=prepared,
            )
            is True
        )

    assert subscriptions == 1
    assert fake_mqtt.Client.call_count == 1
    publish.assert_called_once()


@pytest.mark.parametrize(("retained", "expected"), [(False, True), (True, False)])
def test_master_command_verifier_subscribes_before_publish_and_rejects_retained(test_db, retained, expected):
    from services import observed_state

    server = _server(test_db)
    events: list[str] = []

    class Client:
        def connect(self, *_args, **_kwargs):
            return None

        def subscribe(self, *_args, **_kwargs):
            events.append("subscribed")
            return 0, 1

        def loop_start(self):
            self.on_connect(self, None, None, 0, None)
            self.on_subscribe(self, None, 1, [1], None)

        def loop_stop(self):
            return None

        def disconnect(self):
            return None

    client = Client()
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(return_value=client),
    )

    def publish_command():
        events.append("published")
        client.on_message(client, None, SimpleNamespace(payload=b"0", retain=retained))
        return True

    with (
        patch.object(observed_state, "mqtt", fake_mqtt),
        patch.object(observed_state.state_verifier, "_db", test_db),
    ):
        result = observed_state.verify_master_command(
            server["id"],
            "/master/main",
            "0",
            publish_command,
            timeout=0.001,
        )

    assert result is expected
    expected_events = ["subscribed", "published"] if expected else ["subscribed", "published", "published", "published"]
    assert events == expected_events
    assert fake_mqtt.Client.call_count == 1


@pytest.mark.parametrize("failure", ["wrong_mid", "rejected", "disconnect"])
def test_master_verifier_never_publishes_without_matching_successful_suback(test_db, failure):
    from services import observed_state

    server = _server(test_db)

    class Client:
        def __init__(self):
            self.loop_stops = 0
            self.disconnects = 0

        def connect(self, *_args, **_kwargs):
            return None

        def subscribe(self, *_args, **_kwargs):
            return 0, 7

        def loop_start(self):
            self.on_connect(self, None, None, 0, None)
            if failure == "wrong_mid":
                self.on_subscribe(self, None, 8, [1], None)
            elif failure == "rejected":
                self.on_subscribe(self, None, 7, [128], None)
            else:
                self.on_disconnect(self, None, None, 1, None)

        def loop_stop(self):
            self.loop_stops += 1

        def disconnect(self):
            self.disconnects += 1

    client = Client()
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(return_value=client),
    )
    verifier = observed_state.StateVerifier()
    verifier._db = test_db
    publish = MagicMock(return_value=True)

    with patch.object(observed_state, "mqtt", fake_mqtt):
        assert verifier.verify_master_command(server["id"], "/master/suback", "0", publish, timeout=0.001) is False

    publish.assert_not_called()
    assert client.loop_stops >= 1
    assert client.disconnects >= 1


def test_configured_command_topics_are_rejected_and_never_self_subscribed():
    from services import sse_hub

    db = MagicMock()
    db.get_zones.return_value = [
        {"id": 1, "mqtt_server_id": 11, "topic": "//devices/relay/K1/on"},
    ]
    db.get_groups.return_value = [
        {
            "id": 2,
            "use_master_valve": 1,
            "master_mqtt_server_id": 11,
            "master_mqtt_topic": "devices/master/on",
            "master_mode": "NC",
        }
    ]

    with (
        patch.object(sse_hub, "_db", db),
        patch.object(sse_hub, "_normalize_topic_fn", lambda topic: topic),
    ):
        zone_topics, master_topics = sse_hub._rebuild_subscriptions()

    assert zone_topics == {}
    assert master_topics == {}

    with (
        patch.object(sse_hub, "_db", db),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", zone_topics),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", master_topics),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
    ):
        sse_hub._process_mqtt_message(11, None, "/devices/relay/K1/on", "1", retained=False)

    db.get_zone.assert_not_called()


def test_retained_master_echo_cannot_write_physical_observed_truth():
    from services import sse_hub

    db = MagicMock()
    with (
        patch.object(sse_hub, "_db", db),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {13: {"/master": [(4, "NC")]}}),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
        patch.object(sse_hub, "broadcast") as broadcast,
    ):
        sse_hub._process_mqtt_message(13, None, "/master", "0", retained=True)

    db.update_group_fields.assert_not_called()
    broadcast.assert_not_called()


def test_plaintext_subscriber_is_retired_when_tls_replacement_setup_fails():
    from services import sse_hub

    old_server = {"id": 12, "host": "broker", "port": 1883, "enabled": 1, "tls_enabled": 0}
    new_server = {
        **old_server,
        "port": 8883,
        "tls_enabled": 1,
        "tls_ca_path": "/broken/ca.pem",
    }
    old_client = MagicMock(name="plaintext_client")
    replacement = MagicMock(name="tls_replacement")
    replacement.tls_set.side_effect = OSError("bad CA")
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(return_value=replacement),
    )
    db = MagicMock()
    db.get_zones.return_value = [{"id": 1, "mqtt_server_id": 12, "topic": "/relay"}]
    db.get_groups.return_value = []
    db.get_mqtt_server.return_value = new_server

    with (
        patch.object(sse_hub, "_db", db),
        patch.object(sse_hub, "_mqtt", fake_mqtt),
        patch.object(sse_hub, "_SSE_HUB_REQUESTED_GENERATION", 1),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {12: old_client}),
        patch.object(sse_hub, "_SSE_HUB_SERVER_KEYS", {12: sse_hub._server_key(old_server)}),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {12: {"/relay": [1]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
    ):
        assert sse_hub._apply_rebuild(1) is True
        assert 12 not in sse_hub._SSE_HUB_MQTT

    old_client.loop_stop.assert_called_once()
    old_client.disconnect.assert_called_once()


def test_old_tls_subscriber_is_retired_when_different_tls_replacement_fails():
    from services import sse_hub

    old_server = {
        "id": 14,
        "host": "old-secure-broker",
        "port": 8883,
        "enabled": 1,
        "tls_enabled": 1,
        "tls_ca_path": "/old/ca.pem",
    }
    new_server = {
        **old_server,
        "host": "new-secure-broker",
        "tls_ca_path": "/new/broken-ca.pem",
    }
    old_client = MagicMock(name="old_tls_client")
    replacement = MagicMock(name="new_tls_replacement")
    replacement.tls_set.side_effect = OSError("bad replacement CA")
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(return_value=replacement),
    )
    db = MagicMock()
    db.get_zones.return_value = [{"id": 1, "mqtt_server_id": 14, "topic": "/relay"}]
    db.get_groups.return_value = []
    db.get_mqtt_server.return_value = new_server

    with (
        patch.object(sse_hub, "_db", db),
        patch.object(sse_hub, "_mqtt", fake_mqtt),
        patch.object(sse_hub, "_SSE_HUB_REQUESTED_GENERATION", 1),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {14: old_client}),
        patch.object(sse_hub, "_SSE_HUB_SERVER_KEYS", {14: sse_hub._server_key(old_server)}),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {14: {"/relay": [1]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
    ):
        assert sse_hub._apply_rebuild(1) is True
        assert 14 not in sse_hub._SSE_HUB_MQTT

    old_client.loop_stop.assert_called_once()
    old_client.disconnect.assert_called_once()


def test_unexpected_current_rebuild_exception_retires_all_old_authority():
    from services import sse_hub

    old_client = MagicMock()
    with (
        patch.object(sse_hub, "_SSE_HUB_REQUESTED_GENERATION", 9),
        patch.object(sse_hub, "_SSE_HUB_APPLIED_GENERATION", 8),
        patch.object(sse_hub, "_SSE_HUB_REBUILD_RUNNING", True),
        patch.object(sse_hub, "_SSE_HUB_STARTED", True),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {7: old_client}),
        patch.object(sse_hub, "_SSE_HUB_SERVER_KEYS", {7: ("old",)}),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {7: {"/relay": [1]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {7: {"/master": [(2, "NC")]}}),
        patch.object(sse_hub, "_apply_rebuild", side_effect=RuntimeError("unexpected dependency fault")),
    ):
        sse_hub._rebuild_loop()
        assert sse_hub._SSE_HUB_MQTT == {}
        assert sse_hub._SSE_HUB_SERVER_KEYS == {}
        assert sse_hub._SSE_HUB_ZONE_TOPICS == {}
        assert sse_hub._SSE_HUB_MV_TOPICS == {}
        assert sse_hub._SSE_HUB_REBUILD_RUNNING is False

    old_client.loop_stop.assert_called_once()
    old_client.disconnect.assert_called_once()
