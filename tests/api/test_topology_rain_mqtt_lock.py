"""Regression tests for rain/broker topology transaction serialization."""

from __future__ import annotations

import threading
from unittest.mock import patch

from services.monitors import rain_config_transaction_lock, rain_monitor


def _rain_server(app):
    server = app.db.create_mqtt_server(
        {
            "name": "Rain broker",
            "host": "old-rain-host",
            "port": 1883,
            "enabled": True,
        }
    )
    assert server is not None
    assert app.db.set_rain_config(
        {
            "enabled": True,
            "topic": "/rain",
            "type": "NO",
            "server_id": server["id"],
        }
    )
    return server


class _TrackingLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owner: int | None = None
        self._depth = 0
        self.enter_count = 0
        self.exit_count = 0

    def __enter__(self):
        self._lock.acquire()
        owner = threading.get_ident()
        assert self._owner in (None, owner)
        self._owner = owner
        self._depth += 1
        self.enter_count += 1
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.assert_held()
        self._depth -= 1
        self.exit_count += 1
        if self._depth == 0:
            self._owner = None
        self._lock.release()

    def assert_held(self) -> None:
        assert self._owner == threading.get_ident()
        assert self._depth > 0


class _LockCheckedResult(dict):
    def __init__(self, lock: _TrackingLock, value: dict) -> None:
        super().__init__(value)
        self._transaction_lock = lock

    def get(self, key, default=None):
        self._transaction_lock.assert_held()
        return super().get(key, default)


class _SignalingLock:
    def __init__(self, lock: threading.RLock, attempted: threading.Event) -> None:
        self._lock = lock
        self._attempted = attempted

    def __enter__(self):
        self._attempted.set()
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._lock.release()


def test_runtime_update_holds_rain_lock_through_snapshot_and_exact_runtime_acceptance(admin_client, app) -> None:
    server = _rain_server(app)
    transaction_lock = _TrackingLock()
    guarded_update = app.db.update_mqtt_server_reference_guarded

    def checked_guarded_update(*args, **kwargs):
        transaction_lock.assert_held()
        return _LockCheckedResult(transaction_lock, guarded_update(*args, **kwargs))

    def checked_reconfigure(_config):
        transaction_lock.assert_held()
        return True

    with (
        patch("services.monitors.rain_config_transaction_lock", return_value=transaction_lock),
        patch(
            "routes.mqtt_api.db.update_mqtt_server_reference_guarded",
            side_effect=checked_guarded_update,
        ),
        patch("routes.mqtt_api._reconfigure_rain_monitor", side_effect=checked_reconfigure),
        patch("routes.mqtt_api._refresh_mqtt_runtime"),
    ):
        response = admin_client.put(
            f"/api/mqtt/servers/{server['id']}",
            json={"host": "new-rain-host"},
        )

    assert response.status_code == 200
    assert transaction_lock.enter_count == 1
    assert transaction_lock.exit_count == 1


def test_failed_runtime_stage_holds_rain_lock_through_guarded_cas_rollback(admin_client, app) -> None:
    server = _rain_server(app)
    before = app.db.get_mqtt_server_storage_snapshot(server["id"])
    transaction_lock = _TrackingLock()
    guarded_update = app.db.update_mqtt_server_reference_guarded
    guarded_restore = app.db.restore_mqtt_server_snapshot_reference_guarded

    def checked_guarded_update(*args, **kwargs):
        transaction_lock.assert_held()
        return _LockCheckedResult(transaction_lock, guarded_update(*args, **kwargs))

    def reject_runtime(_config):
        transaction_lock.assert_held()
        return False

    def checked_guarded_restore(*args, **kwargs):
        transaction_lock.assert_held()
        return guarded_restore(*args, **kwargs)

    with (
        patch("services.monitors.rain_config_transaction_lock", return_value=transaction_lock),
        patch(
            "routes.mqtt_api.db.update_mqtt_server_reference_guarded",
            side_effect=checked_guarded_update,
        ),
        patch(
            "routes.mqtt_api.db.restore_mqtt_server_snapshot_reference_guarded",
            side_effect=checked_guarded_restore,
        ),
        patch("routes.mqtt_api._reconfigure_rain_monitor", side_effect=reject_runtime),
        patch("routes.mqtt_api._refresh_mqtt_runtime"),
    ):
        response = admin_client.put(
            f"/api/mqtt/servers/{server['id']}",
            json={"host": "rejected-rain-host"},
        )

    assert response.status_code == 409
    assert response.get_json()["error_code"] == "RAIN_MONITOR_RECONFIGURE_FAILED"
    assert app.db.get_mqtt_server_storage_snapshot(server["id"]) == before
    assert transaction_lock.enter_count == 1
    assert transaction_lock.exit_count == 1


def test_cosmetic_broker_update_does_not_take_rain_transaction_lock(admin_client, app) -> None:
    server = _rain_server(app)

    with (
        patch("services.monitors.rain_config_transaction_lock") as lock_factory,
        patch("routes.mqtt_api._reconfigure_rain_monitor") as reconfigure,
        patch("routes.mqtt_api._refresh_mqtt_runtime") as refresh,
    ):
        response = admin_client.put(
            f"/api/mqtt/servers/{server['id']}",
            json={"name": "Cosmetic rename"},
        )

    assert response.status_code == 200
    lock_factory.assert_not_called()
    reconfigure.assert_not_called()
    refresh.assert_not_called()


def test_rain_api_cannot_overtake_broker_commit_before_runtime_stage_finishes(app) -> None:
    server = _rain_server(app)
    shared_lock = rain_config_transaction_lock()
    broker_at_runtime_stage = threading.Event()
    release_broker_stage = threading.Event()
    rain_lock_attempted = threading.Event()
    broker_done = threading.Event()
    rain_done = threading.Event()
    responses = {}

    def paused_broker_stage(config):
        assert config == {
            "enabled": True,
            "topic": "/rain",
            "type": "NO",
            "server_id": server["id"],
        }
        assert app.db.get_mqtt_server(server["id"])["host"] == "serialized-rain-host"
        broker_at_runtime_stage.set()
        assert release_broker_stage.wait(5)
        return True

    def update_broker() -> None:
        try:
            client = app.test_client()
            responses["broker"] = client.put(
                f"/api/mqtt/servers/{server['id']}",
                json={"host": "serialized-rain-host"},
            )
        finally:
            broker_done.set()

    def update_rain() -> None:
        try:
            client = app.test_client()
            responses["rain"] = client.post(
                "/api/rain",
                json={"enabled": False, "topic": "", "server_id": None, "type": "NC"},
            )
        finally:
            rain_done.set()

    broker_thread = threading.Thread(target=update_broker, daemon=True)
    rain_thread = threading.Thread(target=update_rain, daemon=True)
    rain_lock = _SignalingLock(shared_lock, rain_lock_attempted)
    rain_overtook_broker = False
    try:
        with (
            patch("routes.mqtt_api._reconfigure_rain_monitor", side_effect=paused_broker_stage),
            patch("routes.mqtt_api._refresh_mqtt_runtime"),
            patch("routes.system_config_api.rain_config_transaction_lock", return_value=rain_lock),
            patch.object(rain_monitor, "reconfigure", return_value=True),
        ):
            broker_thread.start()
            assert broker_at_runtime_stage.wait(5)
            rain_thread.start()
            assert rain_lock_attempted.wait(5)
            rain_overtook_broker = rain_done.wait(0.2)
            release_broker_stage.set()
            broker_thread.join(5)
            rain_thread.join(5)
    finally:
        release_broker_stage.set()
        broker_thread.join(5)
        rain_thread.join(5)

    assert rain_overtook_broker is False
    assert broker_done.is_set()
    assert rain_done.is_set()
    assert responses["broker"].status_code == 200
    assert responses["rain"].status_code == 200
