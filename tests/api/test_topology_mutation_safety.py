"""Regression tests for fail-closed hardware topology mutations."""

from datetime import datetime
from unittest.mock import Mock, patch

import pytest


def _group(app, name="Topology group"):
    return app.db.create_group(name)


def _server(app, name="Topology MQTT", host="old-host"):
    return app.db.create_mqtt_server({"name": name, "host": host, "port": 1883})


def _zone(app, group_id, server_id=None, topic="/zone/old", **extra):
    payload = {
        "name": "Topology zone",
        "duration": 10,
        "group_id": group_id,
        "topic": topic,
        "mqtt_server_id": server_id,
    }
    payload.update(extra)
    return app.db.create_zone(payload)


def _configure_master(app, group_id, server_id, *, topic="/master/old", observed="closed"):
    assert app.db.update_group_fields(
        group_id,
        {
            "use_master_valve": 1,
            "master_mqtt_topic": topic,
            "master_mqtt_server_id": server_id,
            "master_mode": "NC",
            "master_valve_observed": observed,
        },
    )


def _get_group(app, group_id):
    return next(g for g in app.db.get_groups() if int(g["id"]) == int(group_id))


class TestZoneTopologyMutationSafety:
    @pytest.mark.parametrize(
        ("change", "field"),
        [
            ({"topic": "/zone/new"}, "topic"),
            ({"mqtt_server_id": None}, "mqtt_server_id"),
            ({"group_id": 999}, "group_id"),
        ],
    )
    def test_active_zone_cannot_be_rewired(self, admin_client, app, change, field):
        group = _group(app)
        server = _server(app)
        zone = _zone(app, group["id"], server["id"])
        app.db.update_zone(zone["id"], {"state": "on", "commanded_state": "on", "observed_state": "on"})
        before = app.db.get_zone(zone["id"])

        response = admin_client.put(f"/api/zones/{zone['id']}", json=change)

        assert response.status_code == 409
        assert app.db.get_zone(zone["id"])[field] == before[field]
        app.db.update_zone(
            zone["id"],
            {"state": "off", "commanded_state": "off", "observed_state": "off", "topic": ""},
        )

    @pytest.mark.parametrize(
        "runtime_fields",
        [
            {"state": "fault", "commanded_state": "off", "observed_state": "unconfirmed"},
            {"state": "off", "commanded_state": "off", "observed_state": "unconfirmed"},
            {"state": "stopping", "commanded_state": "off", "observed_state": "on"},
        ],
    )
    def test_physically_uncertain_zone_cannot_be_deleted(self, admin_client, app, runtime_fields):
        group = _group(app)
        zone = _zone(app, group["id"])
        app.db.update_zone(zone["id"], runtime_fields)

        response = admin_client.delete(f"/api/zones/{zone['id']}")

        assert response.status_code == 409
        assert app.db.get_zone(zone["id"]) is not None

    def test_confirmed_off_zone_can_be_rewired(self, admin_client, app):
        group = _group(app)
        zone = _zone(app, group["id"])
        app.db.update_zone(zone["id"], {"state": "off", "commanded_state": "off", "observed_state": "off"})

        response = admin_client.put(f"/api/zones/{zone['id']}", json={"topic": "/zone/new"})

        assert response.status_code == 200
        assert app.db.get_zone(zone["id"])["topic"] == "/zone/new"

    def test_rewire_rejects_missing_group_without_mutation(self, admin_client, app):
        group = _group(app)
        zone = _zone(app, group["id"])

        response = admin_client.put(f"/api/zones/{zone['id']}", json={"group_id": 424242})

        assert response.status_code == 400
        assert app.db.get_zone(zone["id"])["group_id"] == group["id"]

    def test_rewire_rejects_missing_mqtt_server_without_mutation(self, admin_client, app):
        group = _group(app)
        zone = _zone(app, group["id"])

        response = admin_client.put(f"/api/zones/{zone['id']}", json={"mqtt_server_id": 424242})

        assert response.status_code == 400
        assert app.db.get_zone(zone["id"])["mqtt_server_id"] is None


class TestZoneBulkImportSafety:
    @pytest.mark.parametrize("duration", [None, True, 1.5, "15", 0, 3601])
    def test_import_rejects_noncanonical_duration_atomically(self, admin_client, app, duration):
        group = _group(app)
        zone = _zone(app, group["id"], topic="")

        response = admin_client.post(
            "/api/zones/import",
            json={
                "zones": [
                    {"id": zone["id"], "name": "must-not-commit"},
                    {"name": "invalid", "duration": duration, "group_id": group["id"]},
                ]
            },
        )

        assert response.status_code == 400
        assert app.db.get_zone(zone["id"])["name"] == "Topology zone"
        assert not any(z["name"] == "invalid" for z in app.db.get_zones())

    def test_import_accepts_canonical_duration(self, admin_client, app):
        group = _group(app)
        zone = _zone(app, group["id"], topic="")

        response = admin_client.post(
            "/api/zones/import",
            json={"zones": [{"id": zone["id"], "duration": 25}]},
        )

        assert response.status_code == 200
        assert app.db.get_zone(zone["id"])["duration"] == 25

    def test_import_active_rewire_rejects_entire_batch(self, admin_client, app):
        group = _group(app)
        safe_zone = _zone(app, group["id"], topic="", name="Safe zone")
        live_zone = _zone(app, group["id"], topic="/zone/live", name="Live zone")
        app.db.update_zone(
            live_zone["id"],
            {"state": "on", "commanded_state": "on", "observed_state": "on"},
        )

        response = admin_client.post(
            "/api/zones/import",
            json={
                "zones": [
                    {"id": safe_zone["id"], "name": "must-not-commit"},
                    {"id": live_zone["id"], "topic": "/zone/new"},
                ]
            },
        )

        assert response.status_code == 409
        assert app.db.get_zone(safe_zone["id"])["name"] == "Safe zone"
        assert app.db.get_zone(live_zone["id"])["topic"] == "/zone/live"

    @pytest.mark.parametrize(
        "change",
        [
            {"group_id": 424242},
            {"mqtt_server_id": 424242},
        ],
    )
    def test_import_rejects_dangling_hardware_reference_atomically(self, admin_client, app, change):
        group = _group(app)
        zone = _zone(app, group["id"], topic="", name="Original")

        response = admin_client.post(
            "/api/zones/import",
            json={"zones": [{"id": zone["id"], "name": "must-not-commit"}, {"name": "bad", **change}]},
        )

        assert response.status_code == 400
        assert app.db.get_zone(zone["id"])["name"] == "Original"

    def test_repository_rollback_is_not_reported_as_partial_success(self, admin_client, app):
        group = _group(app)
        zone = _zone(app, group["id"], topic="", name="Original")
        rolled_back = {
            "success": False,
            "created": 0,
            "updated": 0,
            "failed": 1,
            "rolled_back": True,
            "errors": [{"index": 0, "id": zone["id"], "code": "constraint_error"}],
        }

        with (
            patch.object(app.db, "bulk_upsert_zones", return_value=rolled_back),
            patch.object(app.db, "add_log") as add_log,
            patch("routes.zones_crud_api._sse_hub.reload_hub") as reload_hub,
        ):
            response = admin_client.post(
                "/api/zones/import",
                json={"zones": [{"id": zone["id"], "topic": "/zone/new"}]},
            )

        assert response.status_code == 409
        assert response.get_json()["rolled_back"] is True
        assert response.get_json()["success"] is False
        add_log.assert_not_called()
        reload_hub.assert_not_called()


class TestGroupTopologyMutationSafety:
    def test_successful_group_crud_reconfigures_water_monitor(self, admin_client, app):
        from services.monitors import water_monitor

        with patch.object(water_monitor, "reconfigure", return_value=True, create=True) as reconfigure:
            created = admin_client.post("/api/groups", json={"name": "Runtime group"})
            group_id = created.get_json()["id"]
            updated = admin_client.put(f"/api/groups/{group_id}", json={"name": "Runtime group updated"})
            deleted = admin_client.delete(f"/api/groups/{group_id}")

        assert (created.status_code, updated.status_code, deleted.status_code) == (201, 200, 204)
        assert reconfigure.call_count == 3

    def test_active_group_cannot_change_master_wiring(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"])
        zone = _zone(app, group["id"], server["id"])
        app.db.update_zone(zone["id"], {"state": "starting", "commanded_state": "on"})

        with patch("routes.groups_api._publish_mqtt_value") as publish:
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={"master_mqtt_topic": "/master/new"},
            )

        assert response.status_code == 409
        assert _get_group(app, group["id"])["master_mqtt_topic"] == "/master/old"
        publish.assert_not_called()
        app.db.update_zone(
            zone["id"],
            {"state": "off", "commanded_state": "off", "observed_state": "off", "topic": ""},
        )

    def test_open_master_is_closed_on_old_channel_before_rewire(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")

        events = []

        def publish(actual_server, topic, value, **_kwargs):
            events.append((actual_server["id"], topic, value))
            return True

        with patch("routes.groups_api._publish_mqtt_value", side_effect=publish):
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={"master_mqtt_topic": "/master/new", "master_mode": "NO"},
            )

        assert response.status_code == 200
        assert events == [(server["id"], "/master/old", "0")]
        after = _get_group(app, group["id"])
        assert after["master_mqtt_topic"] == "/master/new"
        assert after["master_mode"] == "NO"
        assert after["master_valve_observed"] == "closed"

    def test_failed_old_master_close_leaves_group_unchanged(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")
        before = _get_group(app, group["id"])

        with patch("routes.groups_api._publish_mqtt_value", return_value=False):
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={"name": "must-not-commit", "use_master_valve": False},
            )

        assert response.status_code == 409
        after = _get_group(app, group["id"])
        assert after["name"] == before["name"]
        assert after["use_master_valve"] == 1
        assert after["master_valve_observed"] == "open"

    def test_invalid_group_update_does_not_close_or_mutate_old_master(self, admin_client, app):
        group = _group(app)
        other = _group(app, "Existing name")
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")

        with patch("routes.groups_api._publish_mqtt_value") as publish:
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={"name": other["name"], "use_master_valve": False},
            )

        assert response.status_code == 400
        publish.assert_not_called()
        after = _get_group(app, group["id"])
        assert after["name"] == "Topology group"
        assert after["use_master_valve"] == 1
        assert after["master_valve_observed"] == "open"

    def test_open_master_is_closed_before_group_delete(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")

        with patch("routes.groups_api._publish_mqtt_value", return_value=True) as publish:
            response = admin_client.delete(f"/api/groups/{group['id']}")

        assert response.status_code == 204
        publish.assert_called_once()
        assert all(int(g["id"]) != int(group["id"]) for g in app.db.get_groups())

    def test_failed_master_close_preserves_group_on_delete(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")

        with patch("routes.groups_api._publish_mqtt_value", return_value=False):
            response = admin_client.delete(f"/api/groups/{group['id']}")

        assert response.status_code == 409
        assert _get_group(app, group["id"])["master_valve_observed"] == "open"

    def test_start_zone_must_belong_to_url_group(self, admin_client, app):
        first = _group(app, "First group")
        second = _group(app, "Second group")
        zone = _zone(app, second["id"], topic="")

        with patch("services.zone_control.start_zone_orchestrated") as start:
            response = admin_client.post(f"/api/groups/{first['id']}/start-zone/{zone['id']}")

        assert response.status_code == 409
        start.assert_not_called()


class TestManualMasterSafety:
    def test_unknown_action_is_rejected_without_publish(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"])

        with patch("routes.groups_api._publish_mqtt_value") as publish:
            response = admin_client.post(f"/api/groups/{group['id']}/master-valve/toggle")

        assert response.status_code == 400
        publish.assert_not_called()

    def test_manual_close_blocks_starting_zone(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")
        zone = _zone(app, group["id"], server["id"])
        app.db.update_zone(zone["id"], {"state": "starting", "commanded_state": "on"})

        with patch("routes.groups_api._publish_mqtt_value") as publish:
            response = admin_client.post(f"/api/groups/{group['id']}/master-valve/close")

        assert response.status_code == 409
        publish.assert_not_called()
        app.db.update_zone(
            zone["id"],
            {"state": "off", "commanded_state": "off", "observed_state": "off", "topic": ""},
        )

    def test_manual_close_rechecks_shared_groups_inside_master_lock(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")

        with (
            patch("routes.groups_api._master_has_unsafe_zone", side_effect=[False, True]),
            patch("routes.groups_api._publish_mqtt_value") as publish,
        ):
            response = admin_client.post(f"/api/groups/{group['id']}/master-valve/close")

        assert response.status_code == 409
        publish.assert_not_called()
        assert _get_group(app, group["id"])["master_valve_observed"] == "open"

    def test_manual_close_uses_public_master_topic_lock(self, admin_client, app):
        from services import zone_control

        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")
        events = []
        lock_held = False

        class TrackingLock:
            def __enter__(self):
                nonlocal lock_held
                lock_held = True
                events.append("lock_enter")

            def __exit__(self, *_exc):
                nonlocal lock_held
                events.append("lock_exit")
                lock_held = False

        def lock_for(server_id, topic):
            assert server_id == server["id"]
            assert topic == "/master/old"
            return TrackingLock()

        def cancel(server_id, topic):
            assert lock_held
            assert server_id == server["id"]
            assert topic == "/master/old"
            events.append("cancel")

        def publish(_server, topic, value, **_kwargs):
            assert lock_held
            assert topic == "/master/old"
            assert value == "0"
            events.append("publish")
            return True

        with (
            patch.object(zone_control, "master_topic_lock", side_effect=lock_for),
            patch.object(zone_control, "cancel_pending_master_close", side_effect=cancel),
            patch("routes.groups_api._publish_mqtt_value", side_effect=publish),
        ):
            response = admin_client.post(f"/api/groups/{group['id']}/master-valve/close")

        assert response.status_code == 200
        assert events == ["lock_enter", "cancel", "publish", "lock_exit"]


class TestGroupStopConfirmationSafety:
    @pytest.mark.parametrize("observed_state", ["on", "unconfirmed"])
    def test_unresolved_off_keeps_recovery_jobs_and_returns_503(self, admin_client, app, observed_state):
        group = _group(app)
        zone = _zone(app, group["id"], topic="")
        app.db.update_zone(
            zone["id"],
            {
                "state": "off",
                "commanded_state": "off",
                "observed_state": observed_state,
            },
        )
        scheduler = Mock()
        result = {"success": False, "stopped": [], "unresolved": [zone["id"]]}

        with (
            patch("services.zone_control.stop_all_in_group", return_value=result) as stop_all,
            patch("routes.groups_api.get_scheduler", return_value=scheduler) as get_scheduler,
            patch.object(app.db, "clear_group_scheduled_starts") as clear_starts,
            patch.object(app.db, "reschedule_group_to_next_program") as reschedule,
        ):
            response = admin_client.post(f"/api/groups/{group['id']}/stop")

        assert response.status_code == 503
        assert response.get_json() == {
            "success": False,
            "message": "Не все зоны подтвердили состояние OFF",
            "stopped": [],
            "unresolved": [zone["id"]],
        }
        stop_all.assert_called_once_with(group["id"], reason="group_stop", force=True)
        get_scheduler.assert_not_called()
        scheduler.cancel_group_jobs.assert_not_called()
        clear_starts.assert_not_called()
        reschedule.assert_not_called()
        app.db.update_zone(zone["id"], {"observed_state": "off"})

    def test_group_stop_does_not_cancel_future_program_slots_for_the_day(self, admin_client, app):
        group = _group(app)
        zone = _zone(app, group["id"], topic="")
        program = app.db.create_program(
            {
                "name": "Group stop future slot",
                "time": "00:00",
                "extra_times": ["23:59"],
                "days": [datetime.now().weekday()],
                "zones": [zone["id"]],
            }
        )
        today = datetime.now().strftime("%Y-%m-%d")

        with (
            patch(
                "services.zone_control.stop_all_in_group",
                return_value={"success": True, "stopped": [zone["id"]], "unresolved": []},
            ),
            patch("routes.groups_api.get_scheduler", return_value=None),
        ):
            response = admin_client.post(f"/api/groups/{group['id']}/stop")

        assert response.status_code == 200
        assert app.db.is_program_run_cancelled_for_group(program["id"], today, group["id"]) is False


class TestMqttServerTopologySafety:
    def test_successful_server_crud_reconfigures_water_and_env_monitors(self, admin_client, app):
        from services.monitors import env_monitor, water_monitor

        with (
            patch("routes.mqtt_api._refresh_mqtt_runtime"),
            patch.object(water_monitor, "reconfigure", return_value=True, create=True) as water,
            patch.object(env_monitor, "reconfigure", return_value=True, create=True) as env,
        ):
            created = admin_client.post(
                "/api/mqtt/servers",
                json={"name": "Runtime MQTT", "host": "runtime-host", "port": 1883},
            )
            server_id = created.get_json()["server"]["id"]
            updated = admin_client.put(f"/api/mqtt/servers/{server_id}", json={"name": "Runtime MQTT 2"})
            deleted = admin_client.delete(f"/api/mqtt/servers/{server_id}")

        assert (created.status_code, updated.status_code, deleted.status_code) == (201, 200, 204)
        assert water.call_count == 3
        assert env.call_count == 3
        for call in env.call_args_list:
            assert call.args == (app.db.get_env_config(),)

    def test_referenced_server_cannot_be_reconfigured(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _zone(app, group["id"], server["id"])

        with patch("routes.mqtt_api._refresh_mqtt_runtime") as refresh:
            response = admin_client.put(f"/api/mqtt/servers/{server['id']}", json={"host": "new-host"})

        assert response.status_code == 409
        assert app.db.get_mqtt_server(server["id"])["host"] == "old-host"
        refresh.assert_not_called()

    def test_server_referenced_by_group_hardware_cannot_be_deleted(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"])

        response = admin_client.delete(f"/api/mqtt/servers/{server['id']}")

        assert response.status_code == 409
        assert app.db.get_mqtt_server(server["id"]) is not None

    def test_server_referenced_by_sensor_settings_cannot_be_deleted(self, admin_client, app):
        server = _server(app)
        assert app.db.set_rain_config({"enabled": True, "topic": "/rain", "type": "NO", "server_id": server["id"]})

        response = admin_client.delete(f"/api/mqtt/servers/{server['id']}")

        assert response.status_code == 409
        assert response.get_json()["references"]["settings"] == ["rain.server_id"]
        assert app.db.get_mqtt_server(server["id"]) is not None

    def test_cosmetic_server_rename_is_allowed_while_referenced(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _zone(app, group["id"], server["id"])

        response = admin_client.put(f"/api/mqtt/servers/{server['id']}", json={"name": "Renamed"})

        assert response.status_code == 200
        assert app.db.get_mqtt_server(server["id"])["name"] == "Renamed"

    def test_missing_server_put_and_delete_return_404(self, admin_client):
        assert admin_client.put("/api/mqtt/servers/424242", json={"host": "new"}).status_code == 404
        assert admin_client.delete("/api/mqtt/servers/424242").status_code == 404

    def test_server_post_refreshes_runtime(self, admin_client):
        with patch("routes.mqtt_api._refresh_mqtt_runtime") as refresh:
            response = admin_client.post(
                "/api/mqtt/servers",
                json={"name": "Created", "host": "created-host", "port": 1883},
            )

        assert response.status_code == 201
        refresh.assert_called_once_with(response.get_json()["server"]["id"])
