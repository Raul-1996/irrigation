"""Regression coverage for the post-review MQTT/relay safety package."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


def test_mqtt_client_db_rotation_cannot_relabel_old_client_before_invalidation():
    """A DB update seen by publish must not change the cached client's origin."""
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
    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=MagicMock(side_effect=[old_client, new_client]),
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

        assert during_rotation.client is old_client
        assert during_rotation.generation == before.generation
        assert during_rotation.config_fingerprint == mqtt_pub.mqtt_server_config_fingerprint(old_server)
        assert during_rotation.config_fingerprint != mqtt_pub.mqtt_server_config_fingerprint(new_server)

        mqtt_pub.invalidate_mqtt_server(706)
        assert mqtt_pub.snapshot_mqtt_clients() == {}

        assert mqtt_pub.get_or_create_mqtt_client(new_server) is new_client
        after = mqtt_pub.snapshot_mqtt_clients()[706]

    assert after.client is new_client
    assert after.generation == before.generation + 1
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


def test_new_stop_resets_stale_off_and_preserves_activation_until_fresh_echo(test_db):
    from services import zone_control

    server = _server(test_db)
    zone = _mqtt_zone(test_db, server, "stop-generation", state="on")
    activation = "2026-07-19 12:00:00"
    test_db.update_zone(
        zone["id"],
        {
            "observed_state": "off",
            "commanded_state": "on",
            "watering_start_time": activation,
        },
    )

    def publish(_server, _topic, value, **_kwargs):
        during = test_db.get_zone(zone["id"])
        assert value == "0"
        assert during["commanded_state"] == "off"
        assert during["observed_state"] == "unconfirmed"
        assert during["watering_start_time"] == activation
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


def test_configured_zone_with_missing_server_is_not_virtual(test_db):
    from services import zone_control

    zone = test_db.create_zone(
        {
            "name": "broken-physical-zone",
            "duration": 10,
            "group_id": 1,
            "topic": "/zones/broken",
            "mqtt_server_id": 987654,
        }
    )

    with (
        patch.object(zone_control, "db", test_db),
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


def test_stop_all_reports_unresolved_failed_off(test_db):
    from services import zone_control

    server = _server(test_db)
    failed = _mqtt_zone(test_db, server, "failed-off", state="on")
    stopped = _mqtt_zone(test_db, server, "stopped-off", state="on")

    with patch.object(zone_control, "db", test_db), patch.object(
        zone_control,
        "stop_zone",
        side_effect=lambda zone_id, **_kwargs: int(zone_id) == int(stopped["id"]),
    ):
        result = zone_control.stop_all_in_group(1, force=True)

    assert result == {"success": False, "stopped": [stopped["id"]], "unresolved": [failed["id"]]}


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

    with patch.object(zone_control, "db", test_db), patch.object(
        zone_control, "publish_mqtt_value", side_effect=publish
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

    with patch.object(observed_state, "mqtt", fake_mqtt), patch(
        "irrigation_scheduler.get_scheduler", return_value=MagicMock()
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
        assert verifier.verify(
            zone["id"],
            "off",
            timeout=0.001,
            retries=1,
            generation=generation,
            prepared=prepared,
        ) is True

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
        assert verifier.verify(
            zone["id"],
            "off",
            timeout=0.001,
            retries=2,
            generation=generation,
            prepared=prepared,
        ) is True

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
    assert events == ["subscribed", "published"]
    assert fake_mqtt.Client.call_count == 1


def test_subscription_topics_are_canonical_base_topics_and_command_echo_is_rejected():
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

    assert zone_topics == {11: {"/devices/relay/K1": [1]}}
    assert master_topics == {11: {"/devices/master": [(2, "NC")]}}

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
