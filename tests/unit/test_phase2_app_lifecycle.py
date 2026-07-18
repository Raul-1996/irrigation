"""Regression tests for Phase-2 boot and shutdown lifecycle safety."""

import sqlite3
import time
from unittest.mock import MagicMock, patch


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

    run_seen_by_stop = []

    def ordinary_stop_finalization(_group_id, **_kwargs):
        if int(_group_id) != int(group["id"]):
            return
        run = test_db.get_open_zone_run(int(zone["id"]))
        run_seen_by_stop.append(run)
        if run is not None:
            test_db.finish_zone_run(
                int(run["id"]),
                "2026-07-19 10:00:00",
                time.monotonic(),
                None,
                None,
                None,
                status="ok",
            )

    import services.master_valve as master_valve
    import services.mqtt_pub as mqtt_pub
    import services.zone_control as zone_control
    from services import app_init

    with (
        patch.object(zone_control, "stop_all_in_group", side_effect=ordinary_stop_finalization),
        patch.object(mqtt_pub, "publish_mqtt_value", return_value=True),
        patch.object(master_valve, "close_all_master_valves"),
    ):
        app_init._boot_sync(MagicMock(), test_db)

    assert run_seen_by_stop == [None]
    with sqlite3.connect(test_db.db_path) as conn:
        status, end_utc = conn.execute(
            "SELECT status, end_utc FROM zone_runs WHERE id = ?",
            (int(stale_run_id),),
        ).fetchone()
    assert status == "aborted"
    assert end_utc is not None


def test_shutdown_quiesces_scheduler_before_final_retained_off(test_db):
    """No final OFF may be published before scheduler workers are drained."""
    test_db.create_zone(
        {
            "name": "Shutdown zone",
            "duration": 10,
            "group_id": 1,
            "topic": "/devices/test/controls/K1",
            "mqtt_server_id": 1,
        }
    )
    test_db.create_mqtt_server(
        {
            "name": "Shutdown broker",
            "host": "127.0.0.1",
            "port": 1883,
            "enabled": 1,
        }
    )

    events = []
    scheduler = MagicMock()
    scheduler.quiesce.side_effect = lambda **_kwargs: events.append("quiesced") or True
    publish_result = MagicMock()
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

    scheduler.quiesce.assert_called_once_with(timeout_seconds=3)
    assert events[0] == "quiesced"
    assert events[1:] == [
        ("/devices/test/controls/K1", "0", 2, True),
        ("/devices/test/controls/K1/on", "0", 2, True),
    ]
