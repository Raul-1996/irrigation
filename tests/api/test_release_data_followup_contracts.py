"""Release regressions for zone CAS responses and schedule reconciliation."""

from __future__ import annotations

import sqlite3
from unittest.mock import Mock, patch


def _program_payload(zone_ids: list[int]) -> dict:
    return {
        "name": "Group 999 unlink",
        "enabled": True,
        "type": "time-based",
        "time": "06:00",
        "schedule_type": "weekdays",
        "days": [0],
        "zones": zone_ids,
    }


def test_put_returns_exact_enriched_committed_revision_without_later_writer_mix(admin_client, app):
    zone = app.db.create_zone({"name": "Before", "duration": 10, "group_id": 1})
    run_end = "2026-07-22T04:05:06Z"
    with sqlite3.connect(app.db.db_path) as conn:
        conn.execute(
            "UPDATE zones SET updated_at = '2001-01-01 00:00:00' WHERE id = ?",
            (zone["id"],),
        )
        conn.execute(
            "INSERT INTO zone_runs("
            "zone_id, group_id, start_utc, start_monotonic, end_utc, end_monotonic, "
            "status, confirmed, source"
            ") VALUES (?, 1, ?, 1.0, ?, 2.0, 'ok', 1, 'manual')",
            (zone["id"], "2026-07-22T04:00:00Z", run_end),
        )
        conn.commit()

    before = app.db.get_zone(zone["id"])
    assert before is not None
    first_version = int(before["version"])

    def commit_later_writer(*_args, **_kwargs):
        assert app.db.update_zone(zone["id"], {"name": "Later writer"}) is not None

    with patch.object(app.db, "add_log", side_effect=commit_later_writer):
        response = admin_client.put(
            f"/api/zones/{zone['id']}",
            json={"name": "CAS winner", "expected_version": first_version},
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["name"] == "CAS winner"
    assert payload["version"] == first_version + 1
    assert payload["group"] == payload["group_id"] == 1
    assert payload["group_name"] == before["group_name"]
    assert payload["last_watering_time"] == run_end
    assert payload["updated_at"] != "2001-01-01 00:00:00"

    persisted = app.db.get_zone(zone["id"])
    assert persisted["name"] == "Later writer"
    assert persisted["version"] == first_version + 2


def test_put_group_999_reconciles_every_program_returned_by_atomic_unlink(admin_client, app):
    first = app.db.create_zone({"name": "Excluded", "duration": 10, "group_id": 1})
    second = app.db.create_zone({"name": "Still scheduled", "duration": 10, "group_id": 1})
    program = app.db.create_program(_program_payload([first["id"], second["id"]]))
    scheduler = Mock()
    scheduler.reconcile_program_from_db.return_value = True

    with patch("routes.zones_crud_api.get_scheduler", return_value=scheduler):
        response = admin_client.put(
            f"/api/zones/{first['id']}",
            json={"group_id": 999, "expected_version": first["version"]},
        )

    assert response.status_code == 200
    scheduler.reconcile_program_from_db.assert_called_once_with(program["id"])
    assert app.db.get_program(program["id"])["zones"] == [second["id"]]


def test_bulk_import_group_999_reconciles_every_affected_program(admin_client, app):
    first = app.db.create_zone({"name": "Bulk excluded", "duration": 10, "group_id": 1})
    second = app.db.create_zone({"name": "Bulk retained", "duration": 10, "group_id": 1})
    program = app.db.create_program(_program_payload([first["id"], second["id"]]))
    scheduler = Mock()
    scheduler.reconcile_program_from_db.return_value = True

    with patch("routes.zones_crud_api.get_scheduler", return_value=scheduler):
        response = admin_client.post(
            "/api/zones/import",
            json={"zones": [{"id": first["id"], "group_id": 999}]},
        )

    assert response.status_code == 200
    scheduler.reconcile_program_from_db.assert_called_once_with(program["id"])
    assert app.db.get_program(program["id"])["zones"] == [second["id"]]


def test_mqtt_stop_does_not_republish_from_stale_snapshot_after_central_rejection(admin_client, app):
    server = app.db.create_mqtt_server({"name": "Stop safety", "host": "127.0.0.1", "port": 1883})
    zone = app.db.create_zone(
        {
            "name": "CAS rejected after OFF publish",
            "duration": 10,
            "group_id": 1,
            "mqtt_server_id": server["id"],
            "topic": "/devices/test/controls/K1",
        }
    )
    app.db.update_zone(
        zone["id"],
        {"state": "on", "commanded_state": "on", "observed_state": "on"},
    )

    with (
        patch("routes.zones_watering_api.get_scheduler", return_value=None),
        patch("services.zone_control.stop_zone", return_value=False),
        patch("services.mqtt_pub.publish_mqtt_value") as raw_publish,
    ):
        response = admin_client.post(f"/api/zones/{zone['id']}/mqtt/stop")

    assert response.status_code == 500
    assert response.get_json()["success"] is False
    assert response.get_json()["error_code"] == "ZONE_STOP_UNRESOLVED"
    assert app.db.get_zone(zone["id"])["state"] == "on"
    raw_publish.assert_not_called()


def test_physical_stop_endpoints_report_pending_until_observed_off(admin_client, app):
    server = app.db.create_mqtt_server({"name": "Pending OFF", "host": "127.0.0.1", "port": 1883})
    zone = app.db.create_zone(
        {
            "name": "Pending response",
            "duration": 10,
            "group_id": 1,
            "mqtt_server_id": server["id"],
            "topic": "/devices/test/controls/K2",
        }
    )
    app.db.update_zone(zone["id"], {"state": "on", "commanded_state": "on", "observed_state": "on"})

    def accept_pending_stop(*_args, **_kwargs):
        return (
            app.db.update_zone(
                zone["id"],
                {"state": "stopping", "commanded_state": "off", "observed_state": "unconfirmed"},
            )
            is not None
        )

    for endpoint in ("stop", "mqtt/stop"):
        app.db.update_zone(zone["id"], {"state": "on", "commanded_state": "on", "observed_state": "on"})
        with (
            patch("routes.zones_watering_api.get_scheduler", return_value=None),
            patch("services.zone_control.stop_zone", side_effect=accept_pending_stop),
        ):
            response = admin_client.post(f"/api/zones/{zone['id']}/{endpoint}")

        assert response.status_code == 200
        assert response.get_json()["success"] is True
        assert response.get_json()["state"] == "stopping"
        assert response.get_json()["pending_confirmation"] is True
        assert response.get_json()["message"] == "Команда OFF отправлена"
