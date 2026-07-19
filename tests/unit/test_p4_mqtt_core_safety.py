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
    verifier.verify.assert_any_call(peer["id"], "off")


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
    test_db.update_zone(zone["id"], {"commanded_state": "off"})
    scheduler = MagicMock()
    verifier = StateVerifier()
    verifier._db = test_db

    with (
        patch.object(verifier, "_subscribe_and_wait", return_value=True),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        assert verifier.verify(zone["id"], "off", timeout=0.001, retries=1) is True

    scheduler.cancel_zone_cap.assert_called_once_with(zone["id"])
