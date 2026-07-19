"""Regression tests for MQTT monitor callback and client lifecycle safety."""

from __future__ import annotations

import importlib
import inspect
import sqlite3
import ssl
from types import SimpleNamespace
from unittest.mock import patch

import pytest


class _Message:
    def __init__(self, payload: str, topic: str = "sensor/value"):
        self.payload = payload.encode()
        self.topic = topic


class _HookEvent:
    """Delegate to an Event and run a deterministic hook at first wait."""

    def __init__(self, event, hook):
        self._event = event
        self._hook = hook

    def set(self):
        return self._event.set()

    def is_set(self):
        return self._event.is_set()

    def wait(self, timeout=None):
        hook, self._hook = self._hook, None
        if hook is not None:
            hook()
        return self._event.wait(timeout)


class _FakeClient:
    def __init__(
        self,
        api_version,
        client_id=None,
        *,
        tls_error: Exception | None = None,
        connect_reason=0,
        emit_connect: bool = True,
        suback_reasons: list | None = None,
        emit_suback: bool = True,
        disconnect_before_suback: bool = False,
        ready_wait_hook=None,
    ):
        self.api_version = api_version
        self.client_id = client_id
        self.tls_error = tls_error
        self.connect_reason = connect_reason
        self.emit_connect = emit_connect
        self.suback_reasons = [0] if suback_reasons is None else suback_reasons
        self.emit_suback = emit_suback
        self.disconnect_before_suback = disconnect_before_suback
        self.ready_wait_hook = ready_wait_hook
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.on_subscribe = None
        self.username = None
        self.password = None
        self.tls = None
        self.tls_insecure = None
        self.connect_calls = []
        self.subscribe_calls = []
        self.loop_start_calls = 0
        self.loop_stop_calls = 0
        self.disconnect_calls = 0

    def username_pw_set(self, username, password=None):
        self.username = username
        self.password = password

    def tls_set(self, **kwargs):
        if self.tls_error is not None:
            raise self.tls_error
        self.tls = kwargs

    def tls_insecure_set(self, value):
        self.tls_insecure = value

    def connect(self, host, port, keepalive):
        self.connect_calls.append((host, port, keepalive))

    def subscribe(self, topic, qos=0):
        self.subscribe_calls.append((topic, qos))
        return (0, len(self.subscribe_calls))

    def loop_start(self):
        self.loop_start_calls += 1
        if not self.emit_connect or self.on_connect is None:
            return
        self.on_connect(self, None, {}, self.connect_reason, None)
        if self.disconnect_before_suback:
            self.on_disconnect(self, None, {}, 128, None)
        elif self.emit_suback and self.subscribe_calls and self.on_subscribe is not None:
            self.on_subscribe(self, None, len(self.subscribe_calls), self.suback_reasons, None)
        if self.ready_wait_hook is not None:
            self._water_monitor_ready_event = _HookEvent(self._water_monitor_ready_event, self.ready_wait_hook)

    def disconnect(self):
        self.disconnect_calls += 1
        if self.on_disconnect is not None:
            self.on_disconnect(self, None, {}, 0, None)

    def loop_stop(self):
        self.loop_stop_calls += 1


class _FakeMQTT:
    CallbackAPIVersion = SimpleNamespace(VERSION2="v2")

    def __init__(
        self,
        *,
        tls_error: Exception | None = None,
        tls_error_at: int | None = None,
        connect_reason=0,
        emit_connect: bool = True,
        suback_reasons: list | None = None,
        emit_suback: bool = True,
        disconnect_before_suback: bool = False,
        ready_wait_hook_at: int | None = None,
        ready_wait_hook=None,
    ):
        self.clients: list[_FakeClient] = []
        self._tls_error = tls_error
        self._tls_error_at = tls_error_at
        self._connect_reason = connect_reason
        self._emit_connect = emit_connect
        self._suback_reasons = suback_reasons
        self._emit_suback = emit_suback
        self._disconnect_before_suback = disconnect_before_suback
        self._ready_wait_hook_at = ready_wait_hook_at
        self._ready_wait_hook = ready_wait_hook

    def Client(self, api_version, client_id=None):
        client_number = len(self.clients) + 1
        tls_error = self._tls_error if self._tls_error_at in (None, client_number) else None
        client = _FakeClient(
            api_version,
            client_id,
            tls_error=tls_error,
            connect_reason=self._connect_reason,
            emit_connect=self._emit_connect,
            suback_reasons=self._suback_reasons,
            emit_suback=self._emit_suback,
            disconnect_before_suback=self._disconnect_before_suback,
            ready_wait_hook=self._ready_wait_hook if self._ready_wait_hook_at == client_number else None,
        )
        self.clients.append(client)
        return client


class _FakeDB:
    def __init__(
        self,
        servers: dict[int, dict],
        groups: list[dict] | None = None,
        *,
        strict_error: Exception | None = None,
    ):
        self.servers = servers
        self.groups = groups or []
        self.strict_error = strict_error

    def get_mqtt_server(self, server_id):
        return self.servers.get(int(server_id))

    def get_groups(self):
        return self.groups

    def get_groups_strict(self):
        if self.strict_error is not None:
            raise self.strict_error
        return self.groups


def _tls_server(host: str = "mqtt.example") -> dict:
    return {
        "host": host,
        "port": 8883,
        "username": "sensor",
        "password": "secret",
        "tls_enabled": 1,
        "tls_ca_path": "/certs/ca.pem",
        "tls_cert_path": "/certs/client.pem",
        "tls_key_path": "/certs/client.key",
        "tls_insecure": 1,
        "tls_version": "TLSv1.2",
    }


def _assert_v2_callbacks(client: _FakeClient) -> None:
    assert list(inspect.signature(client.on_connect).parameters) == [
        "client",
        "userdata",
        "connect_flags",
        "reason_code",
        "properties",
    ]
    assert list(inspect.signature(client.on_disconnect).parameters) == [
        "client",
        "userdata",
        "disconnect_flags",
        "reason_code",
        "properties",
    ]
    client.on_connect(client, None, {}, 0, None)


def test_env_reconfigure_uses_v2_tls_and_retires_old_client() -> None:
    mod = importlib.import_module("services.monitors.env_monitor")
    fake_mqtt = _FakeMQTT()
    fake_db = _FakeDB({1: _tls_server("old.example"), 2: _tls_server("new.example")})
    monitor = mod.EnvMonitor()

    with patch.object(mod, "mqtt", fake_mqtt), patch.object(mod, "db", fake_db):
        assert monitor.reconfigure({"temp": {"enabled": True, "topic": "old/temp", "server_id": 1}})
        old_client = fake_mqtt.clients[0]
        _assert_v2_callbacks(old_client)
        old_client.on_disconnect(old_client, None, {}, 1, None)
        assert monitor.temp_client is old_client
        assert old_client.api_version == "v2"
        assert old_client.username == "sensor"
        assert old_client.tls == {
            "ca_certs": "/certs/ca.pem",
            "certfile": "/certs/client.pem",
            "keyfile": "/certs/client.key",
            "tls_version": ssl.PROTOCOL_TLSv1_2,
        }
        assert old_client.tls_insecure is True
        old_client.on_message(old_client, None, _Message("11", "old/temp"))
        assert monitor.temp_value == 11

        assert monitor.reconfigure({"temp": {"enabled": True, "topic": "new/temp", "server_id": 2}})
        new_client = fake_mqtt.clients[1]
        assert monitor.temp_client is new_client
        assert old_client.disconnect_calls == 1
        assert old_client.loop_stop_calls == 1

        # A queued callback from the retired subscription must not mutate the
        # new sensor binding or clear its live client handle.
        old_client.on_message(old_client, None, _Message("99", "old/temp"))
        old_client.on_disconnect(old_client, None, {}, 0, None)
        assert monitor.temp_value == 11
        assert monitor.temp_client is new_client

        monitor.disable()
        monitor.disable()
        assert new_client.disconnect_calls == 1
        assert new_client.loop_stop_calls == 1
        assert monitor.temp_client is None


def test_env_tls_failure_is_fail_closed() -> None:
    mod = importlib.import_module("services.monitors.env_monitor")
    fake_mqtt = _FakeMQTT(tls_error=OSError("bad CA"))
    monitor = mod.EnvMonitor()

    with patch.object(mod, "mqtt", fake_mqtt), patch.object(mod, "db", _FakeDB({1: _tls_server()})):
        assert not monitor.reconfigure({"temp": {"enabled": True, "topic": "env/temp", "server_id": 1}})

    client = fake_mqtt.clients[0]
    assert client.connect_calls == []
    assert client.loop_start_calls == 0
    assert monitor.temp_client is None


def test_water_reconfigure_retires_old_binding_and_discards_stale_pulses() -> None:
    mod = importlib.import_module("services.monitors.water_monitor")
    fake_mqtt = _FakeMQTT()
    groups = [
        {
            "id": 7,
            "use_water_meter": 1,
            "water_mqtt_topic": "old/pulses",
            "water_mqtt_server_id": 1,
            "water_pulse_size": "10l",
        }
    ]
    fake_db = _FakeDB({1: _tls_server("old.example"), 2: _tls_server("new.example")}, groups)
    monitor = mod.WaterMonitor()

    with patch.object(mod, "mqtt", fake_mqtt), patch.object(mod, "db", fake_db):
        assert monitor.reconfigure()
        old_client = fake_mqtt.clients[0]
        _assert_v2_callbacks(old_client)
        old_client.on_disconnect(old_client, None, {}, 1, None)
        assert monitor._clients[7] is old_client
        assert old_client.tls is not None
        assert old_client.tls["tls_version"] == ssl.PROTOCOL_TLSv1_2
        assert old_client.tls_insecure is True
        old_client.on_message(old_client, None, _Message("100", "old/pulses"))
        assert monitor.get_raw_pulses(7) == 100

        groups[0]["water_mqtt_topic"] = "new/pulses"
        groups[0]["water_mqtt_server_id"] = 2
        assert monitor.reconfigure()
        new_client = fake_mqtt.clients[1]
        assert old_client.disconnect_calls == 1
        assert old_client.loop_stop_calls == 1
        assert monitor._clients[7] is new_client
        assert monitor.get_raw_pulses(7) is None

        old_client.on_message(old_client, None, _Message("999", "old/pulses"))
        assert monitor.get_raw_pulses(7) is None
        new_client.on_message(new_client, None, _Message("5", "new/pulses"))
        assert monitor.get_raw_pulses(7) == 5

        groups[0]["use_water_meter"] = 0
        assert monitor.reconfigure()
        assert new_client.disconnect_calls == 1
        assert new_client.loop_stop_calls == 1
        assert monitor._clients == {}
        assert monitor._topics == {}
        assert monitor._samples == {}


def test_water_reconfigure_build_failure_preserves_live_generation() -> None:
    mod = importlib.import_module("services.monitors.water_monitor")
    good_mqtt = _FakeMQTT()
    groups = [
        {
            "id": gid,
            "use_water_meter": 1,
            "water_mqtt_topic": f"water/{gid}",
            "water_mqtt_server_id": 1,
            "water_pulse_size": "1l",
        }
        for gid in (1, 2)
    ]
    fake_db = _FakeDB({1: _tls_server()}, groups)
    monitor = mod.WaterMonitor()

    with patch.object(mod, "mqtt", good_mqtt), patch.object(mod, "db", fake_db):
        assert monitor.reconfigure()
        first_generation = list(good_mqtt.clients)

    failing_mqtt = _FakeMQTT(tls_error=OSError("bad client cert"), tls_error_at=2)
    with patch.object(mod, "mqtt", failing_mqtt), patch.object(mod, "db", fake_db):
        assert not monitor.reconfigure()

    assert all(client.disconnect_calls == 0 for client in first_generation)
    assert all(client.loop_stop_calls == 0 for client in first_generation)
    assert len(failing_mqtt.clients) == 2
    assert failing_mqtt.clients[0].connect_calls == [("mqtt.example", 8883, 10)]
    assert failing_mqtt.clients[0].loop_start_calls == 1
    assert failing_mqtt.clients[1].connect_calls == []
    assert failing_mqtt.clients[1].loop_start_calls == 0
    assert all(client.disconnect_calls == 1 for client in failing_mqtt.clients)
    assert all(client.loop_stop_calls == 1 for client in failing_mqtt.clients)
    assert list(monitor._clients.values()) == first_generation


def test_water_strict_load_failure_preserves_existing_subscriptions_and_samples() -> None:
    mod = importlib.import_module("services.monitors.water_monitor")
    fake_mqtt = _FakeMQTT()
    groups = [
        {
            "id": 7,
            "use_water_meter": 1,
            "water_mqtt_topic": "water/pulses",
            "water_mqtt_server_id": 1,
            "water_pulse_size": "1l",
        }
    ]
    fake_db = _FakeDB({1: _tls_server()}, groups)
    monitor = mod.WaterMonitor()

    with patch.object(mod, "mqtt", fake_mqtt), patch.object(mod, "db", fake_db):
        assert monitor.reconfigure()
        client = monitor._clients[7]
        client.on_message(client, None, _Message("42", "water/pulses"))
        assert monitor.get_raw_pulses(7) == 42

        fake_db.strict_error = sqlite3.OperationalError("database is locked")
        assert not monitor.reconfigure()

    assert fake_mqtt.clients == [client]
    assert monitor._clients[7] is client
    assert monitor.get_raw_pulses(7) == 42
    assert client.disconnect_calls == 0
    assert client.loop_stop_calls == 0


def test_water_invalid_config_is_rejected_before_live_generation_is_swapped() -> None:
    mod = importlib.import_module("services.monitors.water_monitor")
    fake_mqtt = _FakeMQTT()
    groups = [
        {
            "id": 3,
            "use_water_meter": 1,
            "water_mqtt_topic": "water/valid",
            "water_mqtt_server_id": 1,
            "water_pulse_size": "10l",
        }
    ]
    fake_db = _FakeDB({1: _tls_server()}, groups)
    monitor = mod.WaterMonitor()

    with patch.object(mod, "mqtt", fake_mqtt), patch.object(mod, "db", fake_db):
        assert monitor.reconfigure()
        client = monitor._clients[3]
        groups[0]["water_mqtt_topic"] = ""
        assert not monitor.reconfigure()

    assert fake_mqtt.clients == [client]
    assert monitor._clients[3] is client
    assert monitor._topics[3] == "water/valid"
    assert client.disconnect_calls == 0
    assert client.loop_stop_calls == 0


def test_water_successful_generation_waits_for_connack_and_suback() -> None:
    mod = importlib.import_module("services.monitors.water_monitor")
    fake_mqtt = _FakeMQTT()
    groups = [
        {
            "id": 8,
            "use_water_meter": 1,
            "water_mqtt_topic": "water/ready",
            "water_mqtt_server_id": 1,
            "water_pulse_size": "1l",
        }
    ]
    monitor = mod.WaterMonitor()

    with patch.object(mod, "mqtt", fake_mqtt), patch.object(mod, "db", _FakeDB({1: _tls_server()}, groups)):
        assert monitor.reconfigure()

    client = fake_mqtt.clients[0]
    assert list(inspect.signature(client.on_subscribe).parameters) == [
        "client",
        "userdata",
        "mid",
        "reason_code_list",
        "properties",
    ]
    assert client.subscribe_calls == [("water/ready", 0)]
    assert monitor._clients[8] is client


@pytest.mark.parametrize(
    ("mqtt_options", "expected_subscribe_count"),
    [
        ({"connect_reason": 135}, 0),
        ({"suback_reasons": [135]}, 1),
        ({"disconnect_before_suback": True, "emit_suback": False}, 1),
        ({"emit_suback": False}, 1),
    ],
    ids=["connack-not-authorized", "suback-rejected", "disconnect-before-suback", "suback-timeout"],
)
def test_water_unready_generation_rolls_back_and_preserves_live_client(
    mqtt_options: dict,
    expected_subscribe_count: int,
) -> None:
    mod = importlib.import_module("services.monitors.water_monitor")
    groups = [
        {
            "id": 4,
            "use_water_meter": 1,
            "water_mqtt_topic": "water/live",
            "water_mqtt_server_id": 1,
            "water_pulse_size": "1l",
        }
    ]
    fake_db = _FakeDB({1: _tls_server()}, groups)
    monitor = mod.WaterMonitor()
    live_mqtt = _FakeMQTT()

    with patch.object(mod, "mqtt", live_mqtt), patch.object(mod, "db", fake_db):
        assert monitor.reconfigure()
        live_client = monitor._clients[4]
        live_client.on_message(live_client, None, _Message("17", "water/live"))

    groups[0]["water_mqtt_topic"] = "water/replacement"
    unready_mqtt = _FakeMQTT(**mqtt_options)
    with (
        patch.object(mod, "mqtt", unready_mqtt),
        patch.object(mod, "db", fake_db),
        patch.object(mod, "_CONNECT_READY_TIMEOUT_SECONDS", 0.01),
    ):
        assert not monitor.reconfigure()

    staged_client = unready_mqtt.clients[0]
    assert monitor._clients[4] is live_client
    assert monitor._topics[4] == "water/live"
    assert monitor.get_raw_pulses(4) == 17
    assert live_client.disconnect_calls == 0
    assert live_client.loop_stop_calls == 0
    assert len(staged_client.subscribe_calls) == expected_subscribe_count
    assert staged_client.disconnect_calls == 1
    assert staged_client.loop_stop_calls == 1


def test_water_disconnect_after_first_wait_but_before_swap_rolls_back_all_staged_clients() -> None:
    mod = importlib.import_module("services.monitors.water_monitor")
    groups = [
        {
            "id": 9,
            "use_water_meter": 1,
            "water_mqtt_topic": "water/live",
            "water_mqtt_server_id": 1,
            "water_pulse_size": "1l",
        }
    ]
    fake_db = _FakeDB({1: _tls_server()}, groups)
    monitor = mod.WaterMonitor()
    live_mqtt = _FakeMQTT()

    with patch.object(mod, "mqtt", live_mqtt), patch.object(mod, "db", fake_db):
        assert monitor.reconfigure()
        live_client = monitor._clients[9]
        live_client.on_message(live_client, None, _Message("23", "water/live"))

    groups[:] = [
        {
            "id": group_id,
            "use_water_meter": 1,
            "water_mqtt_topic": f"water/new/{group_id}",
            "water_mqtt_server_id": 1,
            "water_pulse_size": "1l",
        }
        for group_id in (1, 2)
    ]
    staged_mqtt = _FakeMQTT()

    def disconnect_first_staged_client() -> None:
        first_client = staged_mqtt.clients[0]
        first_client.on_disconnect(first_client, None, {}, 128, None)

    staged_mqtt._ready_wait_hook_at = 2
    staged_mqtt._ready_wait_hook = disconnect_first_staged_client
    with patch.object(mod, "mqtt", staged_mqtt), patch.object(mod, "db", fake_db):
        assert not monitor.reconfigure()

    assert monitor._clients == {9: live_client}
    assert monitor._topics == {9: "water/live"}
    assert monitor.get_raw_pulses(9) == 23
    assert live_client.disconnect_calls == 0
    assert live_client.loop_stop_calls == 0
    assert len(staged_mqtt.clients) == 2
    assert all(client.disconnect_calls == 1 for client in staged_mqtt.clients)
    assert all(client.loop_stop_calls == 1 for client in staged_mqtt.clients)
