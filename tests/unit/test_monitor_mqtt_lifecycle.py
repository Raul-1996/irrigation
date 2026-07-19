"""Regression tests for MQTT monitor callback and client lifecycle safety."""

from __future__ import annotations

import importlib
import inspect
import ssl
from types import SimpleNamespace
from unittest.mock import patch


class _Message:
    def __init__(self, payload: str, topic: str = "sensor/value"):
        self.payload = payload.encode()
        self.topic = topic


class _FakeClient:
    def __init__(self, api_version, client_id=None, *, tls_error: Exception | None = None):
        self.api_version = api_version
        self.client_id = client_id
        self.tls_error = tls_error
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
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

    def disconnect(self):
        self.disconnect_calls += 1
        if self.on_disconnect is not None:
            self.on_disconnect(self, None, {}, 0, None)

    def loop_stop(self):
        self.loop_stop_calls += 1


class _FakeMQTT:
    CallbackAPIVersion = SimpleNamespace(VERSION2="v2")

    def __init__(self, *, tls_error: Exception | None = None):
        self.clients: list[_FakeClient] = []
        self._tls_error = tls_error

    def Client(self, api_version, client_id=None):
        client = _FakeClient(api_version, client_id, tls_error=self._tls_error)
        self.clients.append(client)
        return client


class _FakeDB:
    def __init__(self, servers: dict[int, dict], groups: list[dict] | None = None):
        self.servers = servers
        self.groups = groups or []

    def get_mqtt_server(self, server_id):
        return self.servers.get(int(server_id))

    def get_groups(self):
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


def test_water_reconfigure_closes_every_client_and_tls_failure_does_not_leak() -> None:
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

    failing_mqtt = _FakeMQTT(tls_error=OSError("bad client cert"))
    with patch.object(mod, "mqtt", failing_mqtt), patch.object(mod, "db", fake_db):
        assert not monitor.reconfigure()

    assert all(client.disconnect_calls == 1 for client in first_generation)
    assert all(client.loop_stop_calls == 1 for client in first_generation)
    assert all(client.connect_calls == [] for client in failing_mqtt.clients)
    assert all(client.loop_start_calls == 0 for client in failing_mqtt.clients)
    assert monitor._clients == {}
