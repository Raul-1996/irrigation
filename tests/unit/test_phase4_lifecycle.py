"""Regression tests for fail-closed application boot and shutdown."""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def health_api(client):
    """Resolve the route module reloaded by the Flask app fixture."""
    return sys.modules["routes.health_api"]


def _configured_zone(test_db, *, state: str = "on") -> dict:
    server = test_db.create_mqtt_server(
        {
            "name": "Lifecycle broker",
            "host": "127.0.0.1",
            "port": 1883,
            "enabled": 1,
        }
    )
    zone = test_db.create_zone(
        {
            "name": "Lifecycle zone",
            "duration": 10,
            "group_id": 1,
            "topic": "/devices/test/controls/K1",
            "mqtt_server_id": int(server["id"]),
            "state": state,
        }
    )
    test_db.update_zone(int(zone["id"]), {"state": state})
    return test_db.get_zone(int(zone["id"]))


def test_boot_sync_keeps_readiness_closed_when_zone_off_is_unconfirmed(test_db):
    from services import app_init

    _configured_zone(test_db)
    app_init.reset_init()
    with patch("services.mqtt_pub.publish_mqtt_value", return_value=False):
        result = app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.2)

    assert result is False
    assert app_init._boot_sync_done is False
    assert "zone" in app_init._boot_reconcile_error


def test_boot_sync_uses_one_global_deadline_for_blocked_broker(test_db):
    from services import app_init

    _configured_zone(test_db)
    release = threading.Event()

    def blocked_publish(*_args, **_kwargs):
        release.wait(2.0)
        return False

    started = time.monotonic()
    try:
        with patch("services.mqtt_pub.publish_mqtt_value", side_effect=blocked_publish):
            result = app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.05)
    finally:
        release.set()

    assert result is False
    assert time.monotonic() - started < 0.25


def test_boot_sync_includes_master_valve_off_in_readiness_decision(test_db):
    from services import app_init

    server = test_db.create_mqtt_server(
        {"name": "Master broker", "host": "127.0.0.1", "port": 1883, "enabled": 1}
    )
    group = test_db.create_group("Master group")
    test_db.update_group_fields(
        int(group["id"]),
        {
            "use_master_valve": 1,
            "master_mqtt_server_id": int(server["id"]),
            "master_mqtt_topic": "/devices/test/controls/MV",
            "master_mode": "NC",
        },
    )

    with patch("services.mqtt_pub.publish_mqtt_value", return_value=False) as publish:
        result = app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.2)

    assert result is False
    assert publish.call_args.args[2] == "0"
    assert "master:" in app_init._boot_reconcile_error


def test_boot_sync_does_not_claim_unmapped_active_zone_is_off(test_db):
    from services import app_init

    zone = test_db.create_zone({"name": "Unmapped active", "duration": 10, "group_id": 1})
    test_db.update_zone(int(zone["id"]), {"state": "on"})

    assert app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.2) is False
    assert test_db.get_zone(int(zone["id"]))["state"] == "on"


def test_initialize_never_resumes_or_notifies_ready_after_failed_boot_sync(test_db):
    import irrigation_scheduler
    import routes.health_api as health_api
    import services.systemd_notify as systemd_notify
    import services.watchdog as watchdog
    from services import app_init

    scheduler = MagicMock()
    scheduler.complete_boot_recovery.return_value = True
    flask_app = MagicMock()
    flask_app.config = {"TESTING": False}

    app_init.reset_init()
    with (
        patch.object(irrigation_scheduler, "init_scheduler"),
        patch.object(irrigation_scheduler, "get_scheduler", return_value=scheduler),
        patch.object(watchdog, "start_watchdog"),
        patch.object(app_init, "_boot_sync", return_value=False),
        patch.object(app_init, "_start_monitors"),
        patch.object(app_init, "_warm_mqtt_clients"),
        patch.object(app_init, "_register_shutdown_handlers"),
        patch.object(app_init, "_start_health_bound_heartbeat", create=True) as start_heartbeat,
        patch.object(health_api, "init_metrics"),
        patch.object(systemd_notify, "start_heartbeat") as legacy_heartbeat,
        patch.object(systemd_notify, "notify_ready") as notify_ready,
    ):
        app_init.initialize_app(flask_app, test_db, start_watchdog_fn=MagicMock())

    scheduler.complete_boot_recovery.assert_not_called()
    start_heartbeat.assert_not_called()
    legacy_heartbeat.assert_not_called()
    notify_ready.assert_not_called()


def test_initialize_keeps_ready_closed_when_complete_boot_recovery_returns_false(test_db):
    import irrigation_scheduler
    import routes.health_api as health_api
    import services.systemd_notify as systemd_notify
    import services.watchdog as watchdog
    from services import app_init

    scheduler = MagicMock()
    scheduler.complete_boot_recovery.return_value = False
    flask_app = MagicMock()
    flask_app.config = {"TESTING": False}

    app_init.reset_init()
    with (
        patch.object(irrigation_scheduler, "init_scheduler"),
        patch.object(irrigation_scheduler, "get_scheduler", return_value=scheduler),
        patch.object(watchdog, "start_watchdog"),
        patch.object(app_init, "_boot_sync", return_value=True),
        patch.object(app_init, "_start_monitors"),
        patch.object(app_init, "_warm_mqtt_clients"),
        patch.object(app_init, "_register_shutdown_handlers"),
        patch.object(app_init, "_start_health_bound_heartbeat", create=True) as start_heartbeat,
        patch.object(health_api, "init_metrics"),
        patch.object(systemd_notify, "start_heartbeat") as legacy_heartbeat,
        patch.object(systemd_notify, "notify_ready") as notify_ready,
    ):
        app_init.initialize_app(flask_app, test_db, start_watchdog_fn=MagicMock())

    scheduler.complete_boot_recovery.assert_called_once_with()
    assert app_init._boot_recovery_done is False
    start_heartbeat.assert_not_called()
    legacy_heartbeat.assert_not_called()
    notify_ready.assert_not_called()


def test_boot_abort_timestamp_is_controller_local_naive_and_preserves_confirmation(test_db):
    from services import app_init

    zone = test_db.create_zone({"name": "Crash-open", "duration": 10, "group_id": 1})
    run_id = test_db.create_zone_run(
        int(zone["id"]),
        1,
        "2026-07-19 09:00:00",
        time.monotonic(),
        None,
        1,
        None,
    )
    test_db.mark_zone_run_confirmed(int(zone["id"]))

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is None
            return cls(2026, 7, 19, 14, 25, 30)

    with patch.object(app_init, "datetime", FixedDateTime, create=True):
        assert app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.2) is True

    with sqlite3.connect(test_db.db_path) as conn:
        row = conn.execute(
            "SELECT status, end_utc, updated_at, confirmed FROM zone_runs WHERE id = ?",
            (int(run_id),),
        ).fetchone()
    assert row == ("aborted", "2026-07-19 14:25:30", "2026-07-19 14:25:30", 1)


def test_shutdown_stops_before_publish_when_scheduler_did_not_quiesce(test_db):
    from services import shutdown

    _configured_zone(test_db)
    mqtt_client = MagicMock()
    shutdown.reset_shutdown()
    with (
        patch.object(shutdown, "_quiesce_scheduler", return_value=False),
        patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client),
    ):
        result = shutdown.shutdown_all_zones_off(timeout_sec=0.1, db=test_db)

    assert result is False
    mqtt_client.publish.assert_not_called()


def test_shutdown_silent_paho_timeout_does_not_write_zone_off(test_db):
    from services import shutdown

    zone = _configured_zone(test_db, state="on")
    info = MagicMock()
    info.rc = 0
    info.wait_for_publish.return_value = None
    info.is_published.return_value = False
    mqtt_client = MagicMock()
    mqtt_client.publish.return_value = info

    shutdown.reset_shutdown()
    with (
        patch.object(shutdown, "_quiesce_scheduler", return_value=True),
        patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client),
    ):
        result = shutdown.shutdown_all_zones_off(timeout_sec=0.1, db=test_db)

    assert result is False
    assert test_db.get_zone(int(zone["id"]))["state"] == "on"
    assert info.is_published.call_count >= 1


def test_shutdown_rejects_nonzero_paho_publish_rc(test_db):
    from services import shutdown

    zone = _configured_zone(test_db, state="on")
    info = MagicMock()
    info.rc = 4
    mqtt_client = MagicMock()
    mqtt_client.publish.return_value = info

    shutdown.reset_shutdown()
    with (
        patch.object(shutdown, "_quiesce_scheduler", return_value=True),
        patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client),
    ):
        result = shutdown.shutdown_all_zones_off(timeout_sec=0.1, db=test_db)

    assert result is False
    info.wait_for_publish.assert_not_called()
    assert test_db.get_zone(int(zone["id"]))["state"] == "on"


def test_shutdown_does_not_claim_unmapped_active_zone_is_off(test_db):
    from services import shutdown

    zone = test_db.create_zone({"name": "Unmapped active", "duration": 10, "group_id": 1})
    test_db.update_zone(int(zone["id"]), {"state": "on"})

    shutdown.reset_shutdown()
    with patch.object(shutdown, "_quiesce_scheduler", return_value=True):
        result = shutdown.shutdown_all_zones_off(timeout_sec=0.1, db=test_db)

    assert result is False
    assert test_db.get_zone(int(zone["id"]))["state"] == "on"


def test_shutdown_uses_one_deadline_even_if_paho_wait_ignores_timeout(test_db):
    from services import shutdown

    _configured_zone(test_db, state="on")
    release = threading.Event()
    info = MagicMock()
    info.rc = 0
    info.wait_for_publish.side_effect = lambda **_kwargs: release.wait(2.0)
    info.is_published.return_value = False
    mqtt_client = MagicMock()
    mqtt_client.publish.return_value = info

    shutdown.reset_shutdown()
    started = time.monotonic()
    try:
        with (
            patch.object(shutdown, "_quiesce_scheduler", return_value=True),
            patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client),
        ):
            result = shutdown.shutdown_all_zones_off(timeout_sec=0.05, db=test_db)
    finally:
        release.set()

    assert result is False
    assert time.monotonic() - started < 0.25


def test_scheduler_check_rejects_paused_boot_recovery(client, health_api):
    scheduler = MagicMock()
    scheduler.is_running = True
    scheduler._boot_recovery_completed = False

    with patch("irrigation_scheduler.get_scheduler", return_value=scheduler):
        result = health_api._check_scheduler()

    assert result["status"] == "fail"
    assert "boot recovery" in result["reason"]


def test_metrics_use_scheduler_wrapper_and_keep_fault_starting_out_of_off(client, health_api):
    scheduler = MagicMock()
    scheduler.is_running = True
    scheduler._boot_recovery_completed = True
    scheduler.scheduler.running = True
    scheduler.scheduler.get_jobs.return_value = [MagicMock(), MagicMock()]
    db = MagicMock()
    db.get_zones.return_value = [
        {"state": "on"},
        {"state": "off"},
        {"state": "fault"},
        {"state": "starting"},
    ]
    client.application.db = db

    with patch("irrigation_scheduler.get_scheduler", return_value=scheduler):
        response = client.get("/metrics")

    assert response.status_code == 200
    assert health_api.WB_SCHEDULER_RUNNING._value.get() == 1
    assert health_api.WB_SCHEDULER_JOBS._value.get() == 2
    scheduler.scheduler.get_jobs.assert_called_once_with()
    assert health_api.WB_ZONES_TOTAL.labels(state="on")._value.get() == 1
    assert health_api.WB_ZONES_TOTAL.labels(state="off")._value.get() == 1
    assert health_api.WB_ZONES_TOTAL.labels(state="fault")._value.get() == 1
    assert health_api.WB_ZONES_TOTAL.labels(state="starting")._value.get() == 1


def test_systemd_heartbeat_is_sent_only_after_real_http_health_probe(monkeypatch):
    from services import app_init, systemd_notify

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    urlopen = MagicMock(return_value=Response())
    notify = MagicMock(return_value=True)
    monkeypatch.setattr(systemd_notify, "notify_watchdog", notify)

    assert app_init._health_heartbeat_once(urlopen_fn=urlopen, port=8080, timeout_sec=0.1) is True
    urlopen.assert_called_once()
    notify.assert_called_once_with()

    urlopen.side_effect = TimeoutError("HTTP loop wedged")
    assert app_init._health_heartbeat_once(urlopen_fn=urlopen, port=8080, timeout_sec=0.1) is False
    assert notify.call_count == 1
