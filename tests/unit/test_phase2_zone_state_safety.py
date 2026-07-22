"""Regression tests for the Phase-2 zone command/state safety package."""

from __future__ import annotations

import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.safety_contracts import complete_group_stop_scheduler


def _make_mqtt_zone(test_db, *, group_id: int = 1, state: str = "off") -> tuple[dict, dict]:
    server = test_db.create_mqtt_server(
        {
            "name": f"broker-{time.monotonic_ns()}",
            "host": "127.0.0.1",
            "port": 1883,
        }
    )
    zone = test_db.create_zone(
        {
            "name": f"zone-{time.monotonic_ns()}",
            "duration": 10,
            "group_id": group_id,
            "topic": f"/zones/{time.monotonic_ns()}",
            "mqtt_server_id": server["id"],
        }
    )
    test_db.update_zone(zone["id"], {"state": state})
    return test_db.get_zone(zone["id"]), server


class _Job:
    def __init__(self, job_id: str):
        self.id = job_id
        self.removed = False

    def remove(self) -> None:
        self.removed = True


def test_finding_3_restart_removes_only_exact_hard_stop(test_db):
    """#3/#115/#136: zone 1 must not remove zone_hard_stop:12."""
    zone = test_db.create_zone({"name": "z1", "duration": 10, "group_id": 1})
    test_db.update_zone(zone["id"], {"state": "on"})
    own = _Job(f"zone_hard_stop:{zone['id']}")
    foreign = _Job(f"zone_hard_stop:{zone['id']}2")
    scheduler = SimpleNamespace(
        scheduler=SimpleNamespace(get_jobs=lambda: [own, foreign]),
        group_cancel_events={},
    )

    with (
        patch("services.zone_control.db", test_db),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        from services.zone_control import start_zone_orchestrated

        status, _ctx = start_zone_orchestrated(zone["id"], restart_if_on=True)

    assert status == "rescheduled"
    assert own.removed is True
    assert foreign.removed is False


def test_finding_18_restart_cancels_active_group_program(test_db):
    """#18: extending an already-on zone must stop its program runner."""
    zone = test_db.create_zone({"name": "z1", "duration": 10, "group_id": 1})
    test_db.update_zone(zone["id"], {"state": "on"})
    cancel_event = threading.Event()
    scheduler = SimpleNamespace(
        scheduler=SimpleNamespace(get_jobs=lambda: []),
        group_cancel_events={1: cancel_event},
    )

    with (
        patch("services.zone_control.db", test_db),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        from services.zone_control import start_zone_orchestrated, stop_zone

        status, _ctx = start_zone_orchestrated(zone["id"], restart_if_on=True)
        # The cancelled program thread unconditionally calls stop_zone(reason="auto")
        # after noticing its event.  That stale owner must not erase the manual takeover.
        assert stop_zone(zone["id"], reason="auto") is True

    assert status == "rescheduled"
    assert cancel_event.is_set()
    assert test_db.get_zone(zone["id"])["state"] == "on"


def test_finding_34_repeated_exclusive_start_has_one_open_run(test_db):
    """#34: serialized duplicate starts must not create a second open run."""
    zone, _server = _make_mqtt_zone(test_db)

    with (
        patch("services.zone_control.db", test_db),
        patch("services.zone_control.publish_mqtt_value", return_value=True),
        patch("services.zone_control.state_verifier"),
        patch("services.zone_control.water_monitor"),
    ):
        from services.zone_control import exclusive_start_zone

        assert exclusive_start_zone(zone["id"]) is True
        assert exclusive_start_zone(zone["id"]) is True

    with sqlite3.connect(test_db.db_path) as conn:
        open_runs = conn.execute(
            "SELECT COUNT(*) FROM zone_runs WHERE zone_id = ? AND end_utc IS NULL",
            (zone["id"],),
        ).fetchone()[0]
    assert open_runs == 1


def test_finding_35_stop_holds_group_serialization_until_off_publish_returns(test_db):
    """#35: a start cannot overtake an in-flight OFF command for the zone."""
    zone, _server = _make_mqtt_zone(test_db, state="on")
    off_entered = threading.Event()
    release_off = threading.Event()
    calls: list[str] = []

    def publish(_server, _topic, value, **_kwargs):
        calls.append(value)
        if value == "0":
            off_entered.set()
            assert release_off.wait(timeout=2)
        return True

    with (
        patch("services.zone_control.db", test_db),
        patch("services.zone_control.publish_mqtt_value", side_effect=publish),
        patch("services.zone_control.state_verifier"),
        patch("services.zone_control.water_monitor"),
    ):
        from services.zone_control import exclusive_start_zone, stop_zone

        stop_thread = threading.Thread(target=stop_zone, args=(zone["id"],))
        start_thread = threading.Thread(target=exclusive_start_zone, args=(zone["id"],))
        stop_thread.start()
        assert off_entered.wait(timeout=2)
        start_thread.start()
        try:
            time.sleep(0.05)
            assert calls == ["0"]
        finally:
            release_off.set()
            stop_thread.join(timeout=2)
            start_thread.join(timeout=2)


def test_finding_36_master_open_cancels_pending_close(test_db):
    """#36: opening a shared master must disarm its pending close timer."""
    server = test_db.create_mqtt_server({"name": "broker", "host": "127.0.0.1", "port": 1883})
    group = test_db.create_group("master-group")
    group_id = group["id"]
    master_topic = "/master/shared"
    test_db.update_group_fields(
        group_id,
        {
            "use_master_valve": 1,
            "master_mqtt_topic": master_topic,
            "master_mqtt_server_id": server["id"],
            "master_mode": "NC",
        },
    )
    zone = test_db.create_zone(
        {
            "name": "z1",
            "duration": 10,
            "group_id": group_id,
            "topic": "/zones/z1",
            "mqtt_server_id": server["id"],
        }
    )
    pending = MagicMock()

    with (
        patch("services.zone_control.db", test_db),
        patch("services.zone_control.publish_mqtt_value", return_value=True),
        patch("services.zone_control.state_verifier"),
        patch("services.zone_control.water_monitor"),
    ):
        from services import zone_control
        from utils import normalize_topic

        zone_control._PENDING_CLOSE_TIMERS[normalize_topic(master_topic)] = pending
        try:
            assert zone_control.exclusive_start_zone(zone["id"]) is True
        finally:
            zone_control._PENDING_CLOSE_TIMERS.clear()

    pending.cancel.assert_called_once_with()


def test_finding_36_concurrent_master_schedules_leave_one_live_timer():
    """#36: replacement of a pending master timer must be atomic."""
    from services import zone_control

    barrier = threading.Barrier(2)
    timers = []

    class FakeTimer:
        def __init__(self, _delay, _callback):
            self.cancelled = False
            timers.append(self)
            try:
                barrier.wait(timeout=0.1)
            except threading.BrokenBarrierError:
                pass

        def cancel(self):
            self.cancelled = True

        def start(self):
            return None

    group = {
        "id": 1,
        "use_master_valve": 1,
        "master_mqtt_topic": "/master/atomic",
        "master_mqtt_server_id": 1,
        "master_close_delay_sec": 60,
    }
    zone_control._PENDING_CLOSE_TIMERS.clear()
    with (
        patch.object(zone_control, "TESTING", False),
        patch.object(zone_control.threading, "Timer", FakeTimer),
    ):
        threads = [threading.Thread(target=zone_control._schedule_master_close, args=(group,)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
    try:
        assert len([timer for timer in timers if not timer.cancelled]) == 1
    finally:
        zone_control._PENDING_CLOSE_TIMERS.clear()


def test_finding_36_firing_close_finishes_before_concurrent_master_open(test_db):
    """#36: an already-firing stale close cannot land after a new open."""
    from services import zone_control

    server = test_db.create_mqtt_server({"name": "broker", "host": "127.0.0.1", "port": 1883})
    group = test_db.create_group("master-group")
    master_topic = "/master/serialized"
    test_db.update_group_fields(
        group["id"],
        {
            "use_master_valve": 1,
            "master_mqtt_topic": master_topic,
            "master_mqtt_server_id": server["id"],
            "master_mode": "NC",
            "master_close_delay_sec": 1,
        },
    )
    zone = test_db.create_zone(
        {
            "name": "z1",
            "duration": 10,
            "group_id": group["id"],
            "topic": "/zones/serialized",
            "mqtt_server_id": server["id"],
        }
    )
    group_dict = next(item for item in test_db.get_groups() if item["id"] == group["id"])
    timers = []

    class FakeTimer:
        def __init__(self, _delay, callback):
            self.callback = callback
            timers.append(self)

        def cancel(self):
            return None

        def start(self):
            return None

    close_scanned = threading.Event()
    release_close = threading.Event()
    original_get_server = test_db.get_mqtt_server

    def get_server(server_id):
        if threading.current_thread().name == "master-close":
            close_scanned.set()
            assert release_close.wait(timeout=2)
        return original_get_server(server_id)

    master_values: list[str] = []
    verifier = MagicMock()
    verifier.verify_master_command.side_effect = lambda _sid, _topic, _expected, publish_command, **_kwargs: (
        publish_command()
    )
    scheduler = MagicMock()
    scheduler.schedule_zone_hard_stop.return_value = True
    scheduler.schedule_zone_cap.return_value = True

    def publish(_server, topic, value, **_kwargs):
        if topic == master_topic:
            master_values.append(value)
        return True

    zone_control._PENDING_CLOSE_TIMERS.clear()
    with (
        patch.object(zone_control, "TESTING", False),
        patch.object(zone_control.threading, "Timer", FakeTimer),
        patch.object(zone_control, "db", test_db),
        patch.object(test_db, "get_mqtt_server", side_effect=get_server),
        patch.object(zone_control, "publish_mqtt_value", side_effect=publish),
        patch.object(zone_control, "state_verifier", verifier),
        patch.object(zone_control, "water_monitor"),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        zone_control._schedule_master_close(group_dict)
        close_thread = threading.Thread(target=timers[0].callback, name="master-close")
        start_thread = threading.Thread(target=zone_control.exclusive_start_zone, args=(zone["id"],))
        close_thread.start()
        assert close_scanned.wait(timeout=2)
        start_thread.start()
        try:
            time.sleep(0.05)
            assert master_values == []
        finally:
            release_close.set()
            close_thread.join(timeout=2)
            start_thread.join(timeout=2)
            zone_control._PENDING_CLOSE_TIMERS.clear()

    assert master_values == ["0", "1"]


@pytest.mark.parametrize(("retained", "expected"), [(True, False), (False, True)])
def test_finding_50_verifier_rejects_retained_command_echo(retained, expected):
    """#50: a retained value published by this app is not relay evidence."""
    from services import observed_state

    class FakeClient:
        on_connect = None
        on_message = None

        def username_pw_set(self, *_args, **_kwargs):
            return None

        def connect(self, *_args, **_kwargs):
            return None

        def loop_start(self):
            self.on_connect(self, None, None, 0)
            msg = SimpleNamespace(payload=b"1", retain=retained)
            self.on_message(self, None, msg)

        def subscribe(self, *_args, **_kwargs):
            return None

        def loop_stop(self):
            return None

        def disconnect(self):
            return None

    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=2),
        Client=lambda *_args, **_kwargs: FakeClient(),
    )
    verifier = observed_state.StateVerifier()
    with patch.object(observed_state, "mqtt", fake_mqtt):
        confirmed = verifier._subscribe_and_wait(
            {"host": "127.0.0.1", "port": 1883},
            "/zones/z1",
            {"1"},
            timeout=0.001,
        )

    assert confirmed is expected


def test_finding_64_stale_verifier_does_not_republish_or_fault(test_db):
    """#64: a newer opposite command invalidates all older verifier retries."""
    zone, _server = _make_mqtt_zone(test_db, state="off")
    test_db.update_zone(zone["id"], {"commanded_state": "off"})
    verifier_calls = 0

    def wait_then_supersede(*_args, **_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        if verifier_calls == 1:
            test_db.update_zone(zone["id"], {"state": "on", "commanded_state": "on"})
        return False

    from services.observed_state import StateVerifier

    verifier = StateVerifier()
    verifier._db = test_db
    verifier._notifier = MagicMock()
    with (
        patch.object(StateVerifier, "_subscribe_and_wait", side_effect=wait_then_supersede),
        patch("services.mqtt_pub.publish_mqtt_value") as publish,
        patch("services.events.publish"),
    ):
        assert verifier.verify(zone["id"], "off", timeout=0.001, retries=3) is False

    assert publish.call_count == 0
    assert verifier_calls == 1
    assert test_db.get_zone(zone["id"])["state"] == "on"


def test_finding_89_manual_start_does_not_cancel_future_extra_slot(test_db):
    """#89: an elapsed main slot must not cancel the program's evening slot."""
    zone = test_db.create_zone({"name": "z1", "duration": 10, "group_id": 1})
    program = test_db.create_program(
        {
            "name": "morning-and-evening",
            "time": "00:00",
            "days": [0, 1, 2, 3, 4, 5, 6],
            "zones": [zone["id"]],
            "extra_times": ["23:59"],
            "enabled": True,
        }
    )
    scheduler = complete_group_stop_scheduler(test_db)

    with (
        patch("services.zone_control.db", test_db),
        patch("services.zone_control.exclusive_start_zone", return_value=True),
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
    ):
        from services.zone_control import start_zone_orchestrated

        status, _ctx = start_zone_orchestrated(zone["id"])

    assert status == "started"
    today = time.strftime("%Y-%m-%d")
    assert test_db.is_program_run_cancelled_for_group(program["id"], today, 1) is False


def test_finding_110_fault_zone_cannot_start_or_be_cleared_by_stop(test_db):
    """#110: fault is sticky and blocks starts until an explicit repair action."""
    zone, _server = _make_mqtt_zone(test_db, state="fault")

    with (
        patch("services.zone_control.db", test_db),
        patch("services.zone_control.publish_mqtt_value", return_value=True) as publish,
        patch("services.zone_control.state_verifier"),
        patch("services.zone_control.water_monitor"),
    ):
        from services.zone_control import exclusive_start_zone, stop_zone

        assert exclusive_start_zone(zone["id"]) is False
        assert publish.call_count == 0
        assert stop_zone(zone["id"], force=True) is True

    assert test_db.get_zone(zone["id"])["state"] == "fault"


def test_finding_111_failed_on_publish_is_not_reported_as_started(test_db):
    """#111: a failed broker publish leaves a fault, never state=on/success."""
    zone, _server = _make_mqtt_zone(test_db)

    with (
        patch("services.zone_control.db", test_db),
        patch("services.zone_control.publish_mqtt_value", return_value=False),
        patch("services.zone_control.state_verifier") as verifier,
        patch("services.zone_control.water_monitor"),
    ):
        from services.zone_control import exclusive_start_zone

        assert exclusive_start_zone(zone["id"]) is False

    after = test_db.get_zone(zone["id"])
    assert after["state"] == "fault"
    verifier.verify_async.assert_not_called()


def test_finding_111_failed_peer_off_aborts_exclusive_start(test_db):
    """#111: failed peer OFF cannot be recorded ok/off while target stays on."""
    server = test_db.create_mqtt_server({"name": "broker", "host": "127.0.0.1", "port": 1883})
    peer = test_db.create_zone(
        {
            "name": "peer",
            "duration": 10,
            "group_id": 1,
            "topic": "/zones/peer",
            "mqtt_server_id": server["id"],
        }
    )
    target = test_db.create_zone(
        {
            "name": "target",
            "duration": 10,
            "group_id": 1,
            "topic": "/zones/target",
            "mqtt_server_id": server["id"],
        }
    )
    test_db.update_zone(peer["id"], {"state": "on", "watering_start_time": "2026-01-01 10:00:00"})

    def publish(_server, topic, value, **_kwargs):
        return not (topic == "/zones/peer" and value == "0")

    with (
        patch("services.zone_control.db", test_db),
        patch("services.zone_control.publish_mqtt_value", side_effect=publish),
        patch("services.zone_control.state_verifier"),
        patch("services.zone_control.water_monitor") as monitor,
    ):
        monitor.summarize_run.return_value = (None, None)
        from services.zone_control import exclusive_start_zone

        assert exclusive_start_zone(target["id"]) is False

    assert test_db.get_zone(peer["id"])["state"] == "fault"
    assert test_db.get_zone(target["id"])["state"] != "on"


def test_finding_112_missing_zone_topic_never_opens_master(test_db):
    """#112: a zone without a control channel cannot pre-open its master."""
    server = test_db.create_mqtt_server({"name": "broker", "host": "127.0.0.1", "port": 1883})
    group = test_db.create_group("master-group")
    test_db.update_group_fields(
        group["id"],
        {
            "use_master_valve": 1,
            "master_mqtt_topic": "/master/no-zone-topic",
            "master_mqtt_server_id": server["id"],
        },
    )
    zone = test_db.create_zone({"name": "z1", "duration": 10, "group_id": group["id"]})

    with (
        patch("services.zone_control.db", test_db),
        patch("services.zone_control.publish_mqtt_value", return_value=True) as publish,
        patch("services.zone_control.state_verifier"),
        patch("services.zone_control.water_monitor"),
    ):
        from services.zone_control import exclusive_start_zone

        exclusive_start_zone(zone["id"])

    publish.assert_not_called()


def test_finding_112_stop_without_zone_topic_still_schedules_master_close(test_db):
    """#112: every completed stop path must consider closing the master."""
    group = test_db.create_group("master-group")
    test_db.update_group_fields(group["id"], {"use_master_valve": 1})
    zone = test_db.create_zone({"name": "z1", "duration": 10, "group_id": group["id"]})
    test_db.update_zone(zone["id"], {"state": "starting"})

    with (
        patch("services.zone_control.db", test_db),
        patch("services.zone_control._schedule_master_close") as schedule_close,
        patch("services.zone_control.water_monitor"),
    ):
        from services.zone_control import stop_zone

        assert stop_zone(zone["id"]) is True

    schedule_close.assert_called_once()


def test_finding_132_failed_off_publish_stays_fault_and_retries(test_db):
    """#132: a failed OFF is physically unknown and later safety stops retry it."""
    zone, _server = _make_mqtt_zone(test_db, state="on")
    started_at = "2026-01-01 10:00:00"
    test_db.update_zone(zone["id"], {"watering_start_time": started_at})

    with (
        patch("services.zone_control.db", test_db),
        patch("services.zone_control.publish_mqtt_value", return_value=False) as publish,
        patch("services.zone_control.state_verifier"),
        patch("services.zone_control.water_monitor"),
    ):
        from services.zone_control import stop_zone

        assert stop_zone(zone["id"], reason="auto_stop") is False
        assert stop_zone(zone["id"], reason="hard_stop") is False

    after = test_db.get_zone(zone["id"])
    assert after["state"] == "fault"
    assert after["watering_start_time"] == started_at
    assert publish.call_count == 2
