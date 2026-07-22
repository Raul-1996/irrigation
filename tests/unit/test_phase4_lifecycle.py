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
    info = MagicMock(rc=0)
    info.is_published.return_value = False
    mqtt_client = MagicMock()
    mqtt_client.publish.return_value = info
    app_init.reset_init()
    with patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client):
        result = app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.2)

    assert result is False
    assert app_init._boot_sync_done is False
    assert "zone" in app_init._boot_reconcile_error


def test_boot_sync_publishes_only_actuator_command_topic(test_db):
    from services import app_init

    _configured_zone(test_db)
    info = MagicMock(rc=0)
    info.is_published.return_value = True
    mqtt_client = MagicMock()
    mqtt_client.publish.return_value = info

    with patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client):
        assert app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.2) is True

    assert [call.args[0] for call in mqtt_client.publish.call_args_list] == ["/devices/test/controls/K1/on"]


def test_boot_sync_uses_one_global_deadline_for_blocked_broker(test_db):
    from services import app_init

    _configured_zone(test_db)
    release = threading.Event()

    def blocked_client(*_args, **_kwargs):
        release.wait(2.0)
        return None

    started = time.monotonic()
    try:
        with patch("services.mqtt_pub.get_or_create_mqtt_client", side_effect=blocked_client):
            result = app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.05)
    finally:
        release.set()

    assert result is False
    assert time.monotonic() - started < 0.25


def test_boot_sync_ignores_swallowing_repository_reads_and_obeys_deadline(test_db):
    """A slow get_zones facade must not sit outside the boot deadline."""
    from services import app_init

    with patch.object(test_db, "get_zones", side_effect=lambda: time.sleep(0.3) or []):
        started = time.monotonic()
        result = app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.05)

    assert result is True
    assert time.monotonic() - started < 0.2


def test_boot_sync_fails_closed_when_strict_topology_snapshot_is_broken(test_db):
    from services import app_init

    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("DROP TABLE groups")
        conn.commit()

    assert app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.2) is False
    assert "snapshot" in app_init._boot_reconcile_error


def test_boot_sync_bounds_blocked_strict_sqlite_connect(test_db):
    import services.lifecycle_storage as lifecycle_storage
    from services import app_init

    real_connect = lifecycle_storage.sqlite3.connect
    release = threading.Event()

    def blocked_connect(*args, **kwargs):
        release.wait(1.0)
        return real_connect(*args, **kwargs)

    started = time.monotonic()
    try:
        with patch.object(lifecycle_storage.sqlite3, "connect", side_effect=blocked_connect):
            result = app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.05)
    finally:
        release.set()

    assert result is False
    assert time.monotonic() - started < 0.2


def test_boot_sync_bounds_blocked_confirmed_state_transaction(test_db):
    import services.lifecycle_storage as lifecycle_storage
    from services import app_init

    zone = _configured_zone(test_db, state="on")
    release = threading.Event()
    info = MagicMock(rc=0)
    info.is_published.return_value = True
    mqtt_client = MagicMock()
    mqtt_client.publish.return_value = info

    def blocked_persist(*_args, **_kwargs):
        release.wait(1.0)

    started = time.monotonic()
    try:
        with (
            patch.object(lifecycle_storage, "persist_boot_zones_off", side_effect=blocked_persist),
            patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client),
        ):
            result = app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.05)
    finally:
        release.set()

    assert result is False
    assert time.monotonic() - started < 0.2
    assert test_db.get_zone(int(zone["id"]))["state"] == "on"


def test_boot_sync_includes_master_valve_off_in_readiness_decision(test_db):
    from services import app_init

    server = test_db.create_mqtt_server({"name": "Master broker", "host": "127.0.0.1", "port": 1883, "enabled": 1})
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

    info = MagicMock(rc=4)
    mqtt_client = MagicMock()
    mqtt_client.publish.return_value = info
    with patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client):
        result = app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.2)

    assert result is False
    assert mqtt_client.publish.call_args.args[0] == "/devices/test/controls/MV/on"
    assert mqtt_client.publish.call_args.kwargs["payload"] == "0"
    assert "master:" in app_init._boot_reconcile_error


def test_boot_sync_keeps_active_evidence_durable_when_master_off_fails(test_db):
    from services import app_init

    zone = _configured_zone(test_db, state="on")
    group = test_db.create_group("Failing master")
    test_db.update_group_fields(
        int(group["id"]),
        {
            "use_master_valve": 1,
            "master_mqtt_server_id": int(zone["mqtt_server_id"]),
            "master_mqtt_topic": "/devices/test/controls/MV",
            "master_mode": "NC",
        },
    )
    ok_info = MagicMock(rc=0)
    ok_info.is_published.return_value = True
    failed_info = MagicMock(rc=4)
    mqtt_client = MagicMock()
    mqtt_client.publish.side_effect = lambda topic, **_kwargs: failed_info if topic.endswith("/MV/on") else ok_info

    with patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client):
        assert app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.2) is False

    assert test_db.get_zone(int(zone["id"]))["state"] == "on"

    mqtt_client.publish.side_effect = None
    mqtt_client.publish.return_value = ok_info
    app_init.reset_init()
    with patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client):
        assert app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.2) is True
    assert int(zone["id"]) in app_init._boot_interrupted_zone_ids


def test_boot_sync_persists_interrupted_evidence_across_process_reset(test_db):
    from services import app_init

    zone = _configured_zone(test_db, state="on")
    info = MagicMock(rc=0)
    info.is_published.return_value = True
    mqtt_client = MagicMock()
    mqtt_client.publish.return_value = info

    with patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client):
        assert app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.2) is True
    assert test_db.get_zone(int(zone["id"]))["state"] == "off"

    app_init.reset_init()
    with patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client):
        assert app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.2) is True
    assert int(zone["id"]) in app_init._boot_interrupted_zone_ids


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
    scheduler.boot_recovery_handoff_is_durable.return_value = True
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
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            ("lifecycle.boot_interrupted_zone_ids", "[9]"),
        )
        conn.commit()

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
    with sqlite3.connect(test_db.db_path) as conn:
        marker = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("lifecycle.boot_interrupted_zone_ids",),
        ).fetchone()
    assert marker == ("[9]",)


def test_initialize_clears_durable_interrupted_evidence_only_after_scheduler_handoff(test_db):
    import irrigation_scheduler
    import routes.health_api as health_api
    import services.watchdog as watchdog
    from services import app_init

    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            ("lifecycle.boot_interrupted_zone_ids", "[41]"),
        )
        conn.commit()

    scheduler = MagicMock()
    scheduler.complete_boot_recovery.return_value = True
    scheduler._boot_interrupted_zone_ids = set()
    scheduler.boot_recovery_handoff_is_durable.return_value = True
    flask_app = MagicMock()
    flask_app.config = {"TESTING": False}

    app_init.reset_init()
    with (
        patch.object(app_init, "_boot_sync", return_value=True),
        patch.object(irrigation_scheduler, "init_scheduler"),
        patch.object(irrigation_scheduler, "get_scheduler", return_value=scheduler),
        patch.object(watchdog, "start_watchdog"),
        patch.object(app_init, "_start_monitors"),
        patch.object(app_init, "_warm_mqtt_clients"),
        patch.object(app_init, "_register_shutdown_handlers"),
        patch.object(app_init, "_start_health_bound_heartbeat"),
        patch.object(health_api, "init_metrics"),
        patch("services.systemd_notify.notify_ready"),
    ):
        app_init.initialize_app(flask_app, test_db, start_watchdog_fn=MagicMock())

    with sqlite3.connect(test_db.db_path) as conn:
        marker = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("lifecycle.boot_interrupted_zone_ids",),
        ).fetchone()
    assert marker is None

    app_init.reset_init()
    assert app_init._boot_sync(MagicMock(), test_db, timeout_sec=0.2) is True
    assert app_init._boot_interrupted_zone_ids == set()


def test_initialize_keeps_marker_when_scheduler_has_no_durable_handoff_ack(test_db):
    import irrigation_scheduler
    import routes.health_api as health_api
    import services.watchdog as watchdog
    from services import app_init

    marker_key = "lifecycle.boot_interrupted_zone_ids"
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (marker_key, "[42]"))
        conn.commit()

    scheduler = MagicMock()
    scheduler.complete_boot_recovery.return_value = True
    scheduler.boot_recovery_handoff_is_durable = None
    scheduler._boot_interrupted_zone_ids = set()
    flask_app = MagicMock()
    flask_app.config = {"TESTING": False}

    app_init.reset_init()
    with (
        patch.object(app_init, "_boot_sync", return_value=True),
        patch.object(irrigation_scheduler, "init_scheduler"),
        patch.object(irrigation_scheduler, "get_scheduler", return_value=scheduler),
        patch.object(watchdog, "start_watchdog"),
        patch.object(app_init, "_start_monitors"),
        patch.object(app_init, "_warm_mqtt_clients"),
        patch.object(app_init, "_register_shutdown_handlers"),
        patch.object(app_init, "_start_health_bound_heartbeat") as start_heartbeat,
        patch.object(health_api, "init_metrics"),
        patch("services.systemd_notify.notify_ready") as notify_ready,
    ):
        app_init.initialize_app(flask_app, test_db, start_watchdog_fn=MagicMock())

    with sqlite3.connect(test_db.db_path) as conn:
        marker = conn.execute("SELECT value FROM settings WHERE key = ?", (marker_key,)).fetchone()
    assert marker == ("[42]",)
    assert app_init._boot_recovery_done is False
    scheduler.stop.assert_called_once_with()
    start_heartbeat.assert_not_called()
    notify_ready.assert_not_called()


def test_initialize_aborts_crash_open_history_before_scheduler_init(test_db):
    import irrigation_scheduler
    import routes.health_api as health_api
    import services.watchdog as watchdog
    from services import app_init

    zone = test_db.create_zone({"name": "Crash ordering", "duration": 10, "group_id": 1})
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

    status_seen_by_scheduler = []

    def inspect_history_before_scheduler(_db):
        with sqlite3.connect(test_db.db_path) as conn:
            status_seen_by_scheduler.append(
                conn.execute("SELECT status FROM zone_runs WHERE id = ?", (int(run_id),)).fetchone()[0]
            )

    scheduler = MagicMock()
    scheduler.complete_boot_recovery.return_value = True
    scheduler._boot_interrupted_zone_ids = set()
    scheduler.boot_recovery_handoff_is_durable.return_value = True
    flask_app = MagicMock()
    flask_app.config = {"TESTING": False}

    app_init.reset_init()
    with (
        patch.object(irrigation_scheduler, "init_scheduler", side_effect=inspect_history_before_scheduler),
        patch.object(irrigation_scheduler, "get_scheduler", return_value=scheduler),
        patch.object(watchdog, "start_watchdog"),
        patch.object(app_init, "_start_monitors"),
        patch.object(app_init, "_warm_mqtt_clients"),
        patch.object(app_init, "_register_shutdown_handlers"),
        patch.object(app_init, "_start_health_bound_heartbeat"),
        patch.object(health_api, "init_metrics"),
        patch("services.systemd_notify.notify_ready"),
    ):
        app_init.initialize_app(flask_app, test_db, start_watchdog_fn=MagicMock())

    assert status_seen_by_scheduler == ["aborted"]


def test_initialize_preserves_pre_reconcile_active_zone_evidence_for_recovery(test_db):
    import irrigation_scheduler
    import routes.health_api as health_api
    import services.watchdog as watchdog
    from services import app_init

    zone = _configured_zone(test_db, state="on")
    scheduler = MagicMock()
    scheduler.complete_boot_recovery.return_value = True
    scheduler._boot_interrupted_zone_ids = set()
    scheduler.boot_recovery_handoff_is_durable.return_value = True
    flask_app = MagicMock()
    flask_app.config = {"TESTING": False}
    info = MagicMock(rc=0)
    info.is_published.return_value = True
    mqtt_client = MagicMock()
    mqtt_client.publish.return_value = info

    app_init.reset_init()
    with (
        patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client),
        patch.object(irrigation_scheduler, "init_scheduler"),
        patch.object(irrigation_scheduler, "get_scheduler", return_value=scheduler),
        patch.object(watchdog, "start_watchdog"),
        patch.object(app_init, "_start_monitors"),
        patch.object(app_init, "_warm_mqtt_clients"),
        patch.object(app_init, "_register_shutdown_handlers"),
        patch.object(app_init, "_start_health_bound_heartbeat"),
        patch.object(health_api, "init_metrics"),
        patch("services.systemd_notify.notify_ready"),
    ):
        app_init.initialize_app(flask_app, test_db, start_watchdog_fn=MagicMock())

    assert int(zone["id"]) in scheduler._boot_interrupted_zone_ids
    scheduler.complete_boot_recovery.assert_called_once_with()


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


def test_shutdown_publishes_only_actuator_command_topic(test_db):
    from services import shutdown

    _configured_zone(test_db, state="on")
    info = MagicMock(rc=0)
    info.is_published.return_value = True
    mqtt_client = MagicMock()
    mqtt_client.publish.return_value = info

    shutdown.reset_shutdown()
    with (
        patch.object(shutdown, "_quiesce_scheduler", return_value=True),
        patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client),
    ):
        assert shutdown.shutdown_all_zones_off(timeout_sec=0.2, db=test_db) is True

    assert [call.args[0] for call in mqtt_client.publish.call_args_list] == ["/devices/test/controls/K1/on"]


def test_shutdown_ignores_swallowing_repository_reads_and_obeys_deadline(test_db):
    from services import shutdown

    shutdown.reset_shutdown()
    with (
        patch.object(shutdown, "_quiesce_scheduler", return_value=True),
        patch.object(test_db, "get_zones", side_effect=lambda: time.sleep(0.3) or []),
    ):
        started = time.monotonic()
        result = shutdown.shutdown_all_zones_off(timeout_sec=0.05, db=test_db)

    assert result is True
    assert time.monotonic() - started < 0.2


def test_shutdown_fails_closed_when_strict_topology_snapshot_is_broken(test_db):
    from services import shutdown

    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute("DROP TABLE groups")
        conn.commit()

    shutdown.reset_shutdown()
    with patch.object(shutdown, "_quiesce_scheduler", return_value=True):
        assert shutdown.shutdown_all_zones_off(timeout_sec=0.2, db=test_db) is False


def test_shutdown_bounds_blocked_strict_sqlite_connect(test_db):
    import services.lifecycle_storage as lifecycle_storage
    from services import shutdown

    real_connect = lifecycle_storage.sqlite3.connect
    release = threading.Event()

    def blocked_connect(*args, **kwargs):
        release.wait(1.0)
        return real_connect(*args, **kwargs)

    shutdown.reset_shutdown()
    started = time.monotonic()
    try:
        with (
            patch.object(shutdown, "_quiesce_scheduler", return_value=True),
            patch.object(lifecycle_storage.sqlite3, "connect", side_effect=blocked_connect),
        ):
            result = shutdown.shutdown_all_zones_off(timeout_sec=0.05, db=test_db)
    finally:
        release.set()

    assert result is False
    assert time.monotonic() - started < 0.2


def test_shutdown_bounds_blocked_confirmed_state_transaction(test_db):
    import services.lifecycle_storage as lifecycle_storage
    from services import shutdown

    zone = _configured_zone(test_db, state="on")
    release = threading.Event()
    info = MagicMock(rc=0)
    info.is_published.return_value = True
    mqtt_client = MagicMock()
    mqtt_client.publish.return_value = info

    def blocked_persist(*_args, **_kwargs):
        release.wait(1.0)

    shutdown.reset_shutdown()
    started = time.monotonic()
    try:
        with (
            patch.object(shutdown, "_quiesce_scheduler", return_value=True),
            patch.object(lifecycle_storage, "persist_confirmed_shutdown_off", side_effect=blocked_persist),
            patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client),
        ):
            result = shutdown.shutdown_all_zones_off(timeout_sec=0.05, db=test_db)
    finally:
        release.set()

    assert result is False
    assert time.monotonic() - started < 0.2
    assert test_db.get_zone(int(zone["id"]))["state"] == "on"


def test_shutdown_atomically_closes_confirmed_open_run_with_zone_state(test_db):
    from services import shutdown

    zone = _configured_zone(test_db, state="on")
    run_id = test_db.create_zone_run(
        int(zone["id"]),
        int(zone["group_id"]),
        "2026-07-19 14:00:00",
        time.monotonic(),
        None,
        1,
        None,
    )
    test_db.mark_zone_run_confirmed(int(zone["id"]))
    info = MagicMock(rc=0)
    info.is_published.return_value = True
    mqtt_client = MagicMock()
    mqtt_client.publish.return_value = info

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is None
            return cls(2026, 7, 19, 14, 25, 30)

    shutdown.reset_shutdown()
    with (
        patch.object(shutdown, "_quiesce_scheduler", return_value=True),
        patch.object(shutdown, "datetime", FixedDateTime, create=True),
        patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client),
    ):
        assert shutdown.shutdown_all_zones_off(timeout_sec=0.2, db=test_db) is True

    assert test_db.get_zone(int(zone["id"]))["state"] == "off"
    assert test_db.get_open_zone_run(int(zone["id"])) is None
    with sqlite3.connect(test_db.db_path) as conn:
        row = conn.execute(
            "SELECT end_utc, status, confirmed FROM zone_runs WHERE id = ?",
            (int(run_id),),
        ).fetchone()
    assert row == ("2026-07-19 14:25:30", "ok", 1)


def test_shutdown_closes_unconfirmed_open_run_as_failed(test_db):
    from services import shutdown

    zone = _configured_zone(test_db, state="on")
    run_id = test_db.create_zone_run(
        int(zone["id"]),
        int(zone["group_id"]),
        "2026-07-19 14:00:00",
        time.monotonic(),
        None,
        1,
        None,
    )
    info = MagicMock(rc=0)
    info.is_published.return_value = True
    mqtt_client = MagicMock()
    mqtt_client.publish.return_value = info

    shutdown.reset_shutdown()
    with (
        patch.object(shutdown, "_quiesce_scheduler", return_value=True),
        patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client),
    ):
        assert shutdown.shutdown_all_zones_off(timeout_sec=0.2, db=test_db) is True

    with sqlite3.connect(test_db.db_path) as conn:
        row = conn.execute(
            "SELECT status, confirmed, end_utc IS NOT NULL FROM zone_runs WHERE id = ?",
            (int(run_id),),
        ).fetchone()
    assert row == ("failed", 0, 1)


def test_shutdown_keeps_confirmed_state_transition_in_audit_log(test_db):
    from services import shutdown

    zone = _configured_zone(test_db, state="on")
    info = MagicMock(rc=0)
    info.is_published.return_value = True
    mqtt_client = MagicMock()
    mqtt_client.publish.return_value = info

    shutdown.reset_shutdown()
    with (
        patch.object(shutdown, "_quiesce_scheduler", return_value=True),
        patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client),
    ):
        assert shutdown.shutdown_all_zones_off(timeout_sec=0.2, db=test_db) is True

    with sqlite3.connect(test_db.db_path) as conn:
        row = conn.execute(
            "SELECT source, action_type, target, payload_json FROM audit_log "
            "WHERE action_type = 'zone_state_change' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row[:3] == ("zones_state", "zone_state_change", f"zone:{int(zone['id'])}")
    assert '"reason": "graceful_shutdown_confirmed"' in row[3]


def test_shutdown_rolls_back_zone_state_when_open_run_close_fails(test_db):
    from services import shutdown

    zone = _configured_zone(test_db, state="on")
    test_db.create_zone_run(
        int(zone["id"]),
        int(zone["group_id"]),
        "2026-07-19 14:00:00",
        time.monotonic(),
        None,
        1,
        None,
    )
    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute(
            "CREATE TRIGGER reject_zone_run_close BEFORE UPDATE OF end_utc ON zone_runs "
            "BEGIN SELECT RAISE(ABORT, 'reject close'); END"
        )
        conn.commit()
    info = MagicMock(rc=0)
    info.is_published.return_value = True
    mqtt_client = MagicMock()
    mqtt_client.publish.return_value = info

    shutdown.reset_shutdown()
    with (
        patch.object(shutdown, "_quiesce_scheduler", return_value=True),
        patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mqtt_client),
    ):
        assert shutdown.shutdown_all_zones_off(timeout_sec=0.2, db=test_db) is False

    assert test_db.get_zone(int(zone["id"]))["state"] == "on"
    assert test_db.get_open_zone_run(int(zone["id"])) is not None


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


def test_shutdown_closes_open_run_for_unmapped_zone_already_off(test_db):
    from services import shutdown

    zone = test_db.create_zone({"name": "Unmapped off", "duration": 10, "group_id": 1})
    run_id = test_db.create_zone_run(
        int(zone["id"]),
        int(zone["group_id"]),
        "2026-07-19 14:00:00",
        time.monotonic(),
        None,
        1,
        None,
    )
    test_db.mark_zone_run_confirmed(int(zone["id"]))

    shutdown.reset_shutdown()
    with patch.object(shutdown, "_quiesce_scheduler", return_value=True):
        assert shutdown.shutdown_all_zones_off(timeout_sec=0.2, db=test_db) is True

    assert test_db.get_open_zone_run(int(zone["id"])) is None
    with sqlite3.connect(test_db.db_path) as conn:
        row = conn.execute(
            "SELECT status, end_utc IS NOT NULL FROM zone_runs WHERE id = ?",
            (int(run_id),),
        ).fetchone()
    assert row == ("ok", 1)


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
