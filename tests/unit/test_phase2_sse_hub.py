"""Phase 2 regressions for the MQTT-to-SSE safety hub."""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services import sse_hub


def _wait_for(predicate, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


class _Message:
    def __init__(self, topic: str, payload: str, *, retain: bool = False):
        self.topic = topic
        self.payload = payload.encode()
        self.retain = retain


class _Client:
    def __init__(self, *, connect_gate: threading.Event | None = None, stop_gate: threading.Event | None = None):
        self.connect_gate = connect_gate
        self.stop_gate = stop_gate
        self.connect_entered = threading.Event()
        self.stop_entered = threading.Event()
        self.on_connect = None
        self.on_message = None
        self.subscriptions: list[tuple[str, int]] = []
        self.unsubscriptions: list[str] = []
        self.connect_args = None
        self.tls_kwargs = None
        self.tls_insecure = None
        self.disconnected = False

    def username_pw_set(self, username, password):
        self.credentials = (username, password)

    def tls_set(self, **kwargs):
        self.tls_kwargs = kwargs

    def tls_insecure_set(self, value):
        self.tls_insecure = value

    def reconnect_delay_set(self, **kwargs):
        self.reconnect_delay = kwargs

    def connect(self, host, port, keepalive):
        self.connect_args = (host, port, keepalive)
        self.connect_entered.set()
        if self.connect_gate is not None:
            assert self.connect_gate.wait(timeout=2)

    def loop_start(self):
        if self.on_connect is not None:
            self.on_connect(self, None, None, 0, None)

    def loop_stop(self):
        self.stop_entered.set()
        if self.stop_gate is not None:
            assert self.stop_gate.wait(timeout=2)

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))
        return 0, len(self.subscriptions)

    def unsubscribe(self, topic):
        self.unsubscriptions.append(topic)
        return 0, len(self.unsubscriptions)


class _MqttModule:
    CallbackAPIVersion = SimpleNamespace(VERSION2=2)

    def __init__(self, client_factory=None):
        self.clients: list[_Client] = []
        self._client_factory = client_factory or _Client

    def Client(self, *args, **kwargs):
        client = self._client_factory()
        self.clients.append(client)
        return client


class _Db:
    def __init__(self, *, zones=None, groups=None, server=None):
        self.zones = list(zones or [])
        self.subscription_zones = self.zones
        self.groups = list(groups or [])
        self.server = server or {
            "id": 1,
            "host": "broker",
            "port": 1883,
            "enabled": 1,
        }
        self.zone_updates: list[tuple[int, dict]] = []
        self.group_updates: list[tuple[int, dict]] = []

    def get_zones(self):
        return [dict(zone) for zone in self.subscription_zones]

    def get_groups(self):
        return [dict(group) for group in self.groups]

    def get_mqtt_server(self, server_id):
        return dict(self.server) if int(server_id) == int(self.server["id"]) else None

    def get_zone(self, zone_id):
        return next((dict(zone) for zone in self.zones if int(zone["id"]) == int(zone_id)), None)

    def update_zone(self, zone_id, updates):
        self.zone_updates.append((int(zone_id), dict(updates)))
        for zone in self.zones:
            if int(zone["id"]) == int(zone_id):
                zone.update(updates)
        return self.get_zone(zone_id)

    def update_group_fields(self, group_id, updates):
        self.group_updates.append((int(group_id), dict(updates)))
        return True

    def mark_zone_run_confirmed(self, zone_id):
        return None

    def get_open_zone_run(self, zone_id):
        return None


@pytest.fixture(autouse=True)
def _isolated_hub(monkeypatch):
    monkeypatch.setattr(sse_hub, "_SSE_HUB_STARTED", False)
    monkeypatch.setattr(sse_hub, "_SSE_HUB_CLIENTS", [])
    monkeypatch.setattr(sse_hub, "_SSE_HUB_MQTT", {})
    monkeypatch.setattr(sse_hub, "_SSE_HUB_SERVER_KEYS", {})
    monkeypatch.setattr(sse_hub, "_SSE_HUB_ZONE_TOPICS", {})
    monkeypatch.setattr(sse_hub, "_SSE_HUB_MV_TOPICS", {})
    monkeypatch.setattr(sse_hub, "_SSE_HUB_REQUESTED_GENERATION", 0)
    monkeypatch.setattr(sse_hub, "_SSE_HUB_APPLIED_GENERATION", 0)
    monkeypatch.setattr(sse_hub, "_SSE_HUB_REBUILD_RUNNING", False)
    monkeypatch.setattr(sse_hub, "_LAST_MANUAL_STOP", {})
    monkeypatch.setattr(sse_hub, "_app_config", {})
    monkeypatch.setattr(sse_hub, "_db", None)
    monkeypatch.setattr(sse_hub, "_mqtt", None)
    monkeypatch.setattr(sse_hub, "_publish_mqtt_value_fn", MagicMock(return_value=True))
    monkeypatch.setattr(sse_hub, "_get_scheduler_fn", lambda: None)
    monkeypatch.setattr(
        "services.zones_state.update_zone_state_internal",
        lambda zone_id, updates, *, snapshot, audit_reason, db: (
            bool(db.update_zone(zone_id, updates)),
            snapshot,
        ),
    )
    yield
    # Let any short-lived asynchronous rebuild finish before monkeypatch restores
    # the module globals for another test module.
    deadline = time.monotonic() + 1
    while getattr(sse_hub, "_SSE_HUB_REBUILD_RUNNING", False) and time.monotonic() < deadline:
        time.sleep(0.01)


def _configure(db: _Db, mqtt_module: _MqttModule, *, scheduler=None, config=None, publisher=None):
    sse_hub._db = db
    sse_hub._mqtt = mqtt_module
    sse_hub._app_config = dict(config or {})
    sse_hub._get_scheduler_fn = lambda: scheduler
    sse_hub._publish_mqtt_value_fn = publisher or MagicMock(return_value=True)


def _start_and_get_client() -> _Client:
    sse_hub.ensure_hub_started()
    _wait_for(lambda: bool(sse_hub._SSE_HUB_MQTT))
    return next(iter(sse_hub._SSE_HUB_MQTT.values()))


def test_active_retained_on_does_not_reset_override_or_hard_stop():
    zone = {
        "id": 7,
        "mqtt_server_id": 1,
        "topic": "/zone/7",
        "duration": 30,
        "state": "on",
        "commanded_state": "on",
        "watering_start_time": "2026-07-19 10:00:00",
        "planned_end_time": "2026-07-19 10:05:00",
    }
    db = _Db(zones=[zone])
    db.get_open_zone_run = MagicMock(return_value={"confirmed": 0})
    db.mark_zone_run_confirmed = MagicMock(return_value=True)
    scheduler = MagicMock()
    _configure(db, _MqttModule(), scheduler=scheduler)
    client = _start_and_get_client()

    client.on_message(client, None, _Message("/zone/7", "1", retain=True))
    _wait_for(lambda: bool(db.zone_updates))

    scheduler.cancel_zone_jobs.assert_not_called()
    scheduler.schedule_zone_stop.assert_not_called()
    scheduler.schedule_zone_hard_stop.assert_not_called()
    db.mark_zone_run_confirmed.assert_called_once_with(7)


def test_live_base_topic_on_does_not_self_confirm_physical_watering():
    zone = {
        "id": 22,
        "mqtt_server_id": 1,
        "topic": "/zone/22",
        "duration": 10,
        "state": "on",
        "commanded_state": "on",
        "watering_start_time": "2026-07-19 10:00:00",
    }
    db = _Db(zones=[zone])
    db.mark_zone_run_confirmed = MagicMock()
    _configure(db, _MqttModule())
    client = _start_and_get_client()

    # The long-lived hub sees the app's own base-topic publish as live
    # (retain=False), so freshness alone is not physical relay evidence.
    client.on_message(client, None, _Message("/zone/22", "1", retain=False))
    time.sleep(0.05)

    assert not db.zone_updates
    db.mark_zone_run_confirmed.assert_not_called()


def test_emergency_counter_off_is_reliable_and_observation_stays_truthful_on_failure():
    zone = {
        "id": 8,
        "mqtt_server_id": 1,
        "topic": "/zone/8",
        "duration": 10,
        "state": "off",
        "commanded_state": "off",
        "observed_state": "off",
    }
    db = _Db(zones=[zone])
    publisher = MagicMock(return_value=False)
    scheduler = MagicMock()
    _configure(db, _MqttModule(), scheduler=scheduler, config={"EMERGENCY_STOP": True}, publisher=publisher)
    client = _start_and_get_client()

    client.on_message(client, None, _Message("/zone/8", "1"))
    _wait_for(lambda: bool(db.zone_updates))

    publisher.assert_called_once_with(
        db.server,
        "/zone/8",
        "0",
        min_interval_sec=0.0,
        qos=2,
        retain=True,
    )
    update = db.zone_updates[-1][1]
    assert update["state"] == "on"
    assert update["observed_state"] == "unconfirmed"
    assert update["watering_start_time"]
    scheduler.schedule_zone_stop.assert_not_called()
    scheduler.schedule_zone_hard_stop.assert_called_once()
    scheduler.schedule_zone_cap.assert_called_once()


def test_recent_commanded_stop_blocks_late_on_without_external_marker_call():
    zone = {
        "id": 9,
        "mqtt_server_id": 1,
        "topic": "/zone/9",
        "duration": 10,
        "state": "off",
        "commanded_state": "off",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    db = _Db(zones=[zone])
    publisher = MagicMock(return_value=True)
    _configure(db, _MqttModule(), publisher=publisher)
    client = _start_and_get_client()

    client.on_message(client, None, _Message("/zone/9", "1", retain=True))
    _wait_for(lambda: bool(db.zone_updates))

    assert publisher.call_args.kwargs == {"min_interval_sec": 0.0, "qos": 2, "retain": True}
    assert db.zone_updates[-1][1]["observed_state"] == "unconfirmed"
    assert db.zone_updates[-1][1]["watering_start_time"]


def test_master_only_tls_server_subscribes_with_safe_keepalive_and_no_mode():
    group = {
        "id": 3,
        "use_master_valve": 1,
        "master_mqtt_server_id": 1,
        "master_mqtt_topic": "/master/3",
        "master_mode": "NO",
    }
    server = {
        "id": 1,
        "host": "secure-broker",
        "port": 8883,
        "enabled": 1,
        "tls_enabled": 1,
        "tls_ca_path": "/ca.pem",
        "tls_cert_path": "/cert.pem",
        "tls_key_path": "/key.pem",
        "tls_insecure": 1,
        "tls_version": "TLS",
    }
    db = _Db(groups=[group], server=server)
    mqtt_module = _MqttModule()
    _configure(db, mqtt_module)
    client = _start_and_get_client()

    assert client.tls_kwargs["ca_certs"] == "/ca.pem"
    assert client.tls_kwargs["certfile"] == "/cert.pem"
    assert client.tls_kwargs["keyfile"] == "/key.pem"
    assert client.tls_insecure is True
    assert client.connect_args == ("secure-broker", 8883, 60)
    assert ("/master/3", 1) in client.subscriptions
    assert client.subscriptions.count(("/master/3", 1)) == 1

    # Reconnect must restore the master-only subscription too.
    before = len(client.subscriptions)
    client.on_connect(client, None, None, 0, None)
    assert len(client.subscriptions) > before
    assert client.subscriptions[-1] == ("/master/3", 1)

    client.on_message(client, None, _Message("/master/3", "1"))
    _wait_for(lambda: bool(db.group_updates))
    assert db.group_updates[-1] == (3, {"master_valve_observed": "closed"})


def test_new_client_is_published_as_current_before_network_loop_starts():
    class InspectingClient(_Client):
        def loop_start(self):
            assert sse_hub._SSE_HUB_MQTT.get(1) is self
            super().loop_start()

    db = _Db(zones=[{"id": 17, "mqtt_server_id": 1, "topic": "/zone/17", "duration": 10, "state": "off"}])
    client = InspectingClient()
    _configure(db, _MqttModule(client_factory=lambda: client))

    sse_hub.ensure_hub_started()
    _wait_for(lambda: not sse_hub._SSE_HUB_REBUILD_RUNNING)

    assert sse_hub._SSE_HUB_MQTT[1] is client


def test_startup_prefers_nonblocking_paho_connect_async():
    class AsyncClient(_Client):
        def connect_async(self, host, port, keepalive):
            self.connect_args = (host, port, keepalive)
            self.connect_entered.set()

        def connect(self, host, port, keepalive):
            raise AssertionError("blocking connect should not be used when connect_async exists")

    db = _Db(zones=[{"id": 18, "mqtt_server_id": 1, "topic": "/zone/18", "duration": 10, "state": "off"}])
    client = AsyncClient()
    _configure(db, _MqttModule(client_factory=lambda: client))

    sse_hub.ensure_hub_started()
    _wait_for(lambda: not sse_hub._SSE_HUB_REBUILD_RUNNING)

    assert client.connect_args == ("broker", 1883, 60)
    assert sse_hub._SSE_HUB_MQTT[1] is client


def test_external_on_schedules_both_remote_stop_guards():
    zone = {"id": 19, "mqtt_server_id": 1, "topic": "/zone/19", "duration": 12, "state": "off"}
    db = _Db(zones=[zone])
    scheduler = MagicMock()
    _configure(db, _MqttModule(), scheduler=scheduler)
    client = _start_and_get_client()

    client.on_message(client, None, _Message("/zone/19", "1"))
    _wait_for(lambda: bool(db.zone_updates))

    scheduler.schedule_zone_stop.assert_called_once()
    scheduler.schedule_zone_hard_stop.assert_called_once()
    assert db.zone_updates[-1][1]["watering_start_source"] == "remote"


def test_paho_callback_returns_before_blocking_database_work_finishes():
    zone = {
        "id": 10,
        "mqtt_server_id": 1,
        "topic": "/zone/10",
        "duration": 10,
        "state": "on",
        "watering_start_time": "2026-07-19 10:00:00",
    }
    db = _Db(zones=[zone])
    entered = threading.Event()
    release = threading.Event()
    original_get_zone = db.get_zone

    def blocking_get_zone(zone_id):
        entered.set()
        assert release.wait(timeout=2)
        return original_get_zone(zone_id)

    db.get_zone = blocking_get_zone
    _configure(db, _MqttModule())
    client = _start_and_get_client()
    callback_returned = threading.Event()

    thread = threading.Thread(
        target=lambda: (client.on_message(client, None, _Message("/zone/10", "1")), callback_returned.set()),
        daemon=True,
    )
    thread.start()
    try:
        assert entered.wait(timeout=1)
        assert callback_returned.wait(timeout=0.1)
    finally:
        release.set()
        thread.join(timeout=1)


def test_event_worker_survives_unexpected_handler_exception(monkeypatch):
    handled_second = threading.Event()
    calls = 0

    def process(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AssertionError("unexpected handler failure")
        handled_second.set()

    monkeypatch.setattr(sse_hub, "_process_mqtt_message", process)
    sse_hub._enqueue_mqtt_message(1, object(), _Message("/zone/fault", "1"))
    sse_hub._enqueue_mqtt_message(1, object(), _Message("/zone/next", "0"))

    assert handled_second.wait(timeout=1)


def test_connect_does_not_hold_hub_lock():
    release = threading.Event()
    client = _Client(connect_gate=release)
    mqtt_module = _MqttModule(client_factory=lambda: client)
    db = _Db(zones=[{"id": 11, "mqtt_server_id": 1, "topic": "/zone/11", "duration": 10, "state": "off"}])
    _configure(db, mqtt_module)

    starter = threading.Thread(target=sse_hub.ensure_hub_started, daemon=True)
    starter.start()
    try:
        assert client.connect_entered.wait(timeout=1)
        assert sse_hub._SSE_HUB_LOCK.acquire(timeout=0.1)
        sse_hub._SSE_HUB_LOCK.release()
    finally:
        release.set()
        starter.join(timeout=1)


def test_reload_returns_while_terminal_client_shutdown_is_blocked():
    release = threading.Event()
    old_client = _Client(stop_gate=release)
    db = _Db(zones=[])
    _configure(db, _MqttModule())
    sse_hub._SSE_HUB_STARTED = True
    sse_hub._SSE_HUB_MQTT = {1: old_client}

    returned = threading.Event()
    caller = threading.Thread(target=lambda: (sse_hub.reload_hub(), returned.set()), daemon=True)
    caller.start()
    try:
        assert old_client.stop_entered.wait(timeout=1)
        assert returned.wait(timeout=0.1)
    finally:
        release.set()
        caller.join(timeout=1)


def test_stale_client_callback_is_terminal_after_reload():
    zone = {"id": 12, "mqtt_server_id": 1, "topic": "/zone/12", "duration": 10, "state": "off"}
    db = _Db(zones=[zone])
    _configure(db, _MqttModule())
    old_client = _start_and_get_client()

    db.subscription_zones = []
    sse_hub.reload_hub()
    _wait_for(lambda: not sse_hub._SSE_HUB_MQTT)
    old_client.on_message(old_client, None, _Message("/zone/12", "1"))
    time.sleep(0.05)

    assert db.zone_updates == []


def test_reload_reuses_connection_and_unsubscribes_removed_topic():
    zone = {"id": 14, "mqtt_server_id": 1, "topic": "/zone/old", "duration": 10, "state": "off"}
    db = _Db(zones=[zone])
    _configure(db, _MqttModule())
    client = _start_and_get_client()

    zone["topic"] = "/zone/new"
    requested_before = sse_hub._SSE_HUB_REQUESTED_GENERATION
    sse_hub.reload_hub()
    _wait_for(lambda: requested_before < sse_hub._SSE_HUB_APPLIED_GENERATION)

    assert sse_hub._SSE_HUB_MQTT[1] is client
    assert client.stop_entered.is_set() is False
    assert "/zone/old" in client.unsubscriptions
    assert ("/zone/new", 1) in client.subscriptions


def test_unchanged_reload_does_not_resubscribe_retained_topics():
    zone = {"id": 20, "mqtt_server_id": 1, "topic": "/zone/20", "duration": 10, "state": "off"}
    db = _Db(zones=[zone])
    _configure(db, _MqttModule())
    client = _start_and_get_client()
    before = list(client.subscriptions)

    requested_before = sse_hub._SSE_HUB_REQUESTED_GENERATION
    sse_hub.reload_hub()
    _wait_for(lambda: requested_before < sse_hub._SSE_HUB_APPLIED_GENERATION)

    assert client.subscriptions == before


def test_unreadable_server_credential_does_not_abort_other_server_startup():
    class UnreadableCredential(Exception):
        pass

    db = _Db(
        zones=[
            {"id": 15, "mqtt_server_id": 1, "topic": "/zone/bad", "duration": 10, "state": "off"},
            {"id": 16, "mqtt_server_id": 2, "topic": "/zone/good", "duration": 10, "state": "off"},
        ]
    )

    def get_mqtt_server(server_id):
        if int(server_id) == 1:
            raise UnreadableCredential("wrong encryption key")
        return {"id": 2, "host": "good-broker", "port": 1883, "enabled": 1}

    db.get_mqtt_server = get_mqtt_server
    _configure(db, _MqttModule())

    sse_hub.ensure_hub_started()
    _wait_for(lambda: not sse_hub._SSE_HUB_REBUILD_RUNNING)

    assert 1 not in sse_hub._SSE_HUB_MQTT
    assert 2 in sse_hub._SSE_HUB_MQTT


def test_unreadable_credential_during_reload_retires_live_client_authority():
    class UnreadableCredential(Exception):
        pass

    zone = {"id": 21, "mqtt_server_id": 1, "topic": "/zone/21", "duration": 10, "state": "off"}
    db = _Db(zones=[zone])
    _configure(db, _MqttModule())
    client = _start_and_get_client()

    db.get_mqtt_server = lambda server_id: (_ for _ in ()).throw(UnreadableCredential("wrong key"))
    requested_before = sse_hub._SSE_HUB_REQUESTED_GENERATION
    sse_hub.reload_hub()
    _wait_for(lambda: requested_before < sse_hub._SSE_HUB_APPLIED_GENERATION)

    assert 1 not in sse_hub._SSE_HUB_MQTT
    assert client.stop_entered.is_set() is True


def test_evicted_full_client_queue_gets_immediate_terminal_sentinel(monkeypatch):
    monkeypatch.setattr(sse_hub, "MAX_SSE_CLIENTS", 1)
    oldest = sse_hub.register_client()
    for index in range(oldest.maxsize):
        oldest.put_nowait(str(index))

    replacement = sse_hub.register_client()

    assert oldest.get_nowait() is None
    assert replacement in sse_hub._SSE_HUB_CLIENTS


def test_init_starts_hub_without_waiting_for_first_sse_client():
    zone = {"id": 13, "mqtt_server_id": 1, "topic": "/zone/13", "duration": 10, "state": "off"}
    db = _Db(zones=[zone])
    mqtt_module = _MqttModule()

    sse_hub.init(
        db=db,
        mqtt_module=mqtt_module,
        app_config={"TESTING": False},
        publish_mqtt_value=MagicMock(return_value=True),
        normalize_topic=lambda topic: topic,
        get_scheduler=lambda: None,
    )

    _wait_for(lambda: bool(mqtt_module.clients))
    assert mqtt_module.clients[0].connect_entered.is_set()
