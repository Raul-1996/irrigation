"""Regression tests for Phase-2 boot and shutdown lifecycle safety."""

import sqlite3
import time
from unittest.mock import MagicMock, patch


def test_initialize_completes_scheduler_boot_only_after_off_reconciliation(test_db):
    """The paused scheduler resumes only after boot OFF reconciliation."""
    import irrigation_scheduler
    import routes.health_api as health_api
    import services.systemd_notify as systemd_notify
    import services.watchdog as watchdog
    from services import app_init

    events = []
    scheduler = MagicMock()
    scheduler.complete_boot_recovery.side_effect = lambda: events.append("complete_boot_recovery") or True
    scheduler.boot_recovery_handoff_is_durable.return_value = True
    flask_app = MagicMock()
    flask_app.config = {"TESTING": False}

    app_init.reset_init()
    with (
        patch.object(irrigation_scheduler, "init_scheduler", side_effect=lambda _db: events.append("init")),
        patch.object(irrigation_scheduler, "get_scheduler", return_value=scheduler),
        patch.object(watchdog, "start_watchdog"),
        patch.object(app_init, "_boot_sync", side_effect=lambda _app, _db: events.append("boot_sync") or True),
        patch.object(app_init, "_start_monitors", side_effect=lambda _app, _db: events.append("monitors")),
        patch.object(app_init, "_warm_mqtt_clients"),
        patch.object(app_init, "_register_shutdown_handlers"),
        patch.object(health_api, "init_metrics"),
        patch.object(app_init, "_start_health_bound_heartbeat"),
        patch.object(systemd_notify, "notify_ready"),
    ):
        app_init.initialize_app(flask_app, test_db, start_watchdog_fn=MagicMock())

    scheduler.complete_boot_recovery.assert_called_once_with()
    assert events == ["boot_sync", "init", "complete_boot_recovery", "monitors"]


def test_boot_aborts_crash_open_run_before_forced_zone_stop(test_db):
    """A boot OFF sweep must not finalize a pre-crash run as successful."""
    group = test_db.create_group("Crash recovery")
    assert group is not None
    zone = test_db.create_zone(
        {
            "name": "Crash-open zone",
            "duration": 10,
            "group_id": int(group["id"]),
        }
    )
    stale_run_id = test_db.create_zone_run(
        int(zone["id"]),
        int(group["id"]),
        "2026-07-18 09:00:00",
        time.monotonic(),
        None,
        1,
        None,
    )
    assert stale_run_id is not None
    test_db.mark_zone_run_confirmed(int(zone["id"]))

    from services import app_init

    assert app_init._boot_sync(MagicMock(), test_db) is True

    assert test_db.get_open_zone_run(int(zone["id"])) is None
    with sqlite3.connect(test_db.db_path) as conn:
        status, end_utc = conn.execute(
            "SELECT status, end_utc FROM zone_runs WHERE id = ?",
            (int(stale_run_id),),
        ).fetchone()
    assert status == "aborted"
    assert end_utc is not None


def test_shutdown_quiesces_scheduler_before_final_retained_off(test_db):
    """No final OFF may be published before scheduler workers are drained."""
    server = test_db.create_mqtt_server(
        {
            "name": "Shutdown broker",
            "host": "127.0.0.1",
            "port": 1883,
            "enabled": 1,
        }
    )
    test_db.create_zone(
        {
            "name": "Shutdown zone",
            "duration": 10,
            "group_id": 1,
            "topic": "/devices/test/controls/K1",
            "mqtt_server_id": server["id"],
        }
    )

    events = []
    scheduler = MagicMock()
    scheduler.quiesce.side_effect = lambda **_kwargs: events.append("quiesced") or True
    publish_result = MagicMock()
    publish_result.rc = 0
    publish_result.is_published.return_value = True
    mqtt_client = MagicMock()

    def record_publish(topic, *, payload, qos, retain):
        events.append((topic, payload, qos, retain))
        return publish_result

    mqtt_client.publish.side_effect = record_publish

    from services import shutdown

    shutdown.reset_shutdown()
    with (
        patch("irrigation_scheduler.get_scheduler", return_value=scheduler),
        patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client),
    ):
        shutdown.shutdown_all_zones_off(timeout_sec=3, db=test_db)

    scheduler.quiesce.assert_called_once()
    assert 0 < scheduler.quiesce.call_args.kwargs["timeout_seconds"] <= 3
    assert events[0] == "quiesced"
    assert events[1:] == [
        ("/devices/test/controls/K1/on", "0", 2, True),
    ]
