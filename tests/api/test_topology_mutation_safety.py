"""Regression tests for fail-closed hardware topology mutations."""

import sqlite3
import threading
from contextlib import contextmanager
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


def _confirm_published(_server_id, _topic, _expected_payload, publish_command, **_kwargs):
    return bool(publish_command())


def _publish_without_echo(_server_id, _topic, _expected_payload, publish_command, **_kwargs):
    publish_command()
    return False


def _core_close_confirmed(_server_id, _topic, _mode, publish_command):
    return bool(publish_command())


def _core_close_without_echo(_server_id, _topic, _mode, publish_command):
    publish_command()
    return False


class TestZoneTopologyMutationSafety:
    def test_logically_off_virtual_zone_can_move_and_delete_without_physical_echo(self, admin_client, app):
        source = _group(app, "Virtual source")
        target = _group(app, "Virtual target")
        zone = _zone(app, source["id"], server_id=None, topic="")

        moved = admin_client.put(
            f"/api/zones/{zone['id']}",
            json={"group_id": target["id"], "expected_version": zone["version"]},
        )
        deleted = admin_client.delete(f"/api/zones/{zone['id']}")

        assert moved.status_code == 200
        assert deleted.status_code == 204
        assert app.db.get_zone(zone["id"]) is None

    @pytest.mark.parametrize(
        ("change", "field"),
        [
            ({"topic": "/zone/new"}, "topic"),
            ({"mqtt_server_id": None, "topic": ""}, "mqtt_server_id"),
            ({"group_id": 999}, "group_id"),
        ],
    )
    def test_active_zone_cannot_be_rewired(self, admin_client, app, change, field):
        group = _group(app)
        server = _server(app)
        zone = _zone(app, group["id"], server["id"])
        app.db.update_zone(zone["id"], {"state": "on", "commanded_state": "on", "observed_state": "on"})
        before = app.db.get_zone(zone["id"])

        response = admin_client.put(
            f"/api/zones/{zone['id']}",
            json={**change, "expected_version": before["version"]},
        )

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
        server = _server(app)
        zone = _zone(app, group["id"], server["id"])
        app.db.update_zone(zone["id"], {"state": "off", "commanded_state": "off", "observed_state": "off"})
        current = app.db.get_zone(zone["id"])

        response = admin_client.put(
            f"/api/zones/{zone['id']}",
            json={"topic": "/zone/new", "expected_version": current["version"]},
        )

        assert response.status_code == 200
        assert app.db.get_zone(zone["id"])["topic"] == "/zone/new"

    @pytest.mark.parametrize(
        "runtime_fields",
        [
            {"state": "off", "commanded_state": None, "observed_state": "off"},
            {"state": "off", "commanded_state": "", "observed_state": "off"},
            {"state": "off", "commanded_state": "off", "observed_state": None},
            {"state": "off", "commanded_state": "off", "observed_state": ""},
        ],
    )
    def test_null_or_empty_confirmation_is_unknown_and_blocks_delete(self, admin_client, app, runtime_fields):
        group = _group(app)
        zone = _zone(app, group["id"])
        app.db.update_zone(zone["id"], runtime_fields)

        response = admin_client.delete(f"/api/zones/{zone['id']}")

        assert response.status_code == 409
        assert app.db.get_zone(zone["id"]) is not None

    def test_rewire_rejects_missing_group_without_mutation(self, admin_client, app):
        group = _group(app)
        zone = _zone(app, group["id"])

        response = admin_client.put(
            f"/api/zones/{zone['id']}",
            json={"group_id": 424242, "expected_version": zone["version"]},
        )

        assert response.status_code == 400
        assert app.db.get_zone(zone["id"])["group_id"] == group["id"]

    def test_rewire_rejects_missing_mqtt_server_without_mutation(self, admin_client, app):
        group = _group(app)
        zone = _zone(app, group["id"])

        response = admin_client.put(
            f"/api/zones/{zone['id']}",
            json={"mqtt_server_id": 424242, "expected_version": zone["version"]},
        )

        assert response.status_code == 400
        assert app.db.get_zone(zone["id"])["mqtt_server_id"] is None

    @pytest.mark.parametrize("method", ["update", "delete"])
    def test_topology_mutation_relocks_a_concurrently_changed_group(self, admin_client, app, method):
        from services.locks import group_lock as real_group_lock

        source = _group(app, "Initial lock group")
        target = _group(app, "Requested target group")
        raced = _group(app, "Concurrent target group")
        server = _server(app)
        zone = _zone(app, source["id"], server["id"])
        app.db.update_zone(zone["id"], {"state": "off", "commanded_state": "off", "observed_state": "off"})
        expected_version = app.db.get_zone(zone["id"])["version"]
        acquired_group_ids = []
        moved = False

        def racing_group_lock(group_id):
            nonlocal moved
            acquired_group_ids.append(int(group_id))
            if not moved:
                moved = True
                assert app.db.update_zone(zone["id"], {"group_id": raced["id"]})
            return real_group_lock(int(group_id))

        with patch("routes.zones_crud_api.group_lock", side_effect=racing_group_lock):
            if method == "update":
                response = admin_client.put(
                    f"/api/zones/{zone['id']}",
                    json={"group_id": target["id"], "expected_version": expected_version},
                )
            else:
                response = admin_client.delete(f"/api/zones/{zone['id']}")

        # A concurrent topology writer owns the newer revision.  Update must
        # reject the stale caller instead of silently overwriting it; DELETE
        # has no caller CAS token and may proceed after acquiring the new lock.
        assert response.status_code == (409 if method == "update" else 204)
        assert int(raced["id"]) in acquired_group_ids
        if method == "update":
            assert app.db.get_zone(zone["id"])["group_id"] == raced["id"]


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
        server = _server(app)
        safe_zone = _zone(app, group["id"], topic="", name="Safe zone")
        live_zone = _zone(app, group["id"], server["id"], topic="/zone/live", name="Live zone")
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
        app.db.update_zone(zone["id"], {"state": "off", "commanded_state": "off", "observed_state": "off"})

        response = admin_client.post(
            "/api/zones/import",
            json={"zones": [{"id": zone["id"], "name": "must-not-commit"}, {"name": "bad", **change}]},
        )

        assert response.status_code == 400
        assert app.db.get_zone(zone["id"])["name"] == "Original"

    def test_repository_rollback_is_not_reported_as_partial_success(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        zone = _zone(app, group["id"], server["id"], topic="/zone/original", name="Original")
        app.db.update_zone(zone["id"], {"state": "off", "commanded_state": "off", "observed_state": "off"})
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

    def test_import_relocks_group_created_by_a_concurrent_zone_move(self, admin_client, app):
        from services.locks import group_lock as real_group_lock

        source = _group(app, "Bulk initial group")
        server = _server(app)
        zone = _zone(app, source["id"], server["id"])
        app.db.update_zone(zone["id"], {"state": "off", "commanded_state": "off", "observed_state": "off"})
        acquired_group_ids = []
        raced_group_id = None

        def racing_group_lock(group_id):
            nonlocal raced_group_id
            acquired_group_ids.append(int(group_id))
            if raced_group_id is None:
                raced = _group(app, "Bulk concurrent new group")
                raced_group_id = int(raced["id"])
                assert app.db.update_zone(zone["id"], {"group_id": raced_group_id})
            return real_group_lock(int(group_id))

        with patch("routes.zones_crud_api.group_lock", side_effect=racing_group_lock):
            response = admin_client.post(
                "/api/zones/import",
                json={"zones": [{"id": zone["id"], "topic": "/zone/new"}]},
            )

        assert response.status_code == 200
        assert raced_group_id is not None
        assert raced_group_id in acquired_group_ids


class TestGroupTopologyMutationSafety:
    def test_successful_group_crud_reconfigures_water_monitor(self, admin_client, app):
        from services.monitors import water_monitor

        server = _server(app, name="Water runtime")
        with patch.object(water_monitor, "reconfigure", return_value=True, create=True) as reconfigure:
            created = admin_client.post("/api/groups", json={"name": "Runtime group"})
            group_id = created.get_json()["id"]
            updated = admin_client.put(
                f"/api/groups/{group_id}",
                json={
                    "use_water_meter": True,
                    "water_mqtt_topic": "/water/runtime",
                    "water_mqtt_server_id": server["id"],
                },
            )
            deleted = admin_client.delete(f"/api/groups/{group_id}")

        assert (created.status_code, updated.status_code, deleted.status_code) == (201, 200, 204)
        assert reconfigure.call_count == 2

    def test_failed_water_monitor_reconfigure_rolls_back_group_update(self, admin_client, app):
        from services.monitors import water_monitor

        group = _group(app)
        server = _server(app, name="Water rollback")
        before = app.db.get_group_storage_snapshot(group["id"])

        with patch.object(water_monitor, "reconfigure", return_value=False, create=True):
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={
                    "use_water_meter": True,
                    "water_mqtt_topic": "/water/rollback",
                    "water_mqtt_server_id": server["id"],
                },
            )

        assert response.status_code == 409
        assert response.get_json()["error_code"] == "WATER_MONITOR_RECONFIGURE_FAILED"
        assert app.db.get_group_storage_snapshot(group["id"]) == before

    def test_master_topology_rollback_tolerates_stale_old_echo(self, admin_client, app):
        from services.monitors import water_monitor

        group = _group(app)
        server = _server(app, name="Master water rollback")
        _configure_master(app, group["id"], server["id"], observed="open")
        before = app.db.get_group_storage_snapshot(group["id"])

        def reject_after_old_hub_echo():
            # The hub can finish dispatching the old mapping after the DB
            # topology commit but before the staged water runtime rejects it.
            assert app.db.update_group_fields(
                group["id"],
                {"master_valve_observed": "closed"},
            )
            return False

        with (
            patch("routes.groups_api._publish_mqtt_value", return_value=True),
            patch("routes.groups_api._close_master_valve_confirmed", side_effect=_core_close_confirmed),
            patch.object(
                water_monitor,
                "reconfigure",
                side_effect=reject_after_old_hub_echo,
                create=True,
            ),
        ):
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={
                    "master_mqtt_topic": "/master/new",
                    "use_water_meter": True,
                    "water_mqtt_topic": "/water/new",
                    "water_mqtt_server_id": server["id"],
                },
            )

        assert response.status_code == 409
        assert response.get_json()["error_code"] == "WATER_MONITOR_RECONFIGURE_FAILED"
        restored = app.db.get_group_storage_snapshot(group["id"])
        assert restored == {**before, "master_valve_observed": None}

    def test_water_monitor_rollback_never_clobbers_concurrent_group_update(self, admin_client, app):
        from services.monitors import water_monitor

        group = _group(app)
        server = _server(app, name="Water rollback race")

        def concurrent_update_then_reject():
            assert app.db.update_group(group["id"], "Concurrent operator update")
            return False

        with patch.object(
            water_monitor,
            "reconfigure",
            side_effect=concurrent_update_then_reject,
            create=True,
        ):
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={
                    "use_water_meter": True,
                    "water_mqtt_topic": "/water/race",
                    "water_mqtt_server_id": server["id"],
                },
            )

        assert response.status_code == 500
        assert response.get_json()["error_code"] == "WATER_MONITOR_ROLLBACK_CONFLICT"
        current = _get_group(app, group["id"])
        assert current["name"] == "Concurrent operator update"
        assert current["water_mqtt_topic"] == "/water/race"

    def test_group_update_cas_rejects_change_between_preflight_and_commit(self, admin_client, app):
        from services.monitors import water_monitor

        group = _group(app)
        server = _server(app, name="Group precommit race")
        original_update = app.db.update_group_config_with_snapshot

        def concurrent_update_before_commit(group_id, updates, **kwargs):
            assert app.db.update_group(group_id, "Concurrent precommit rename")
            return original_update(group_id, updates, **kwargs)

        with (
            patch.object(
                app.db,
                "update_group_config_with_snapshot",
                side_effect=concurrent_update_before_commit,
            ),
            patch.object(water_monitor, "reconfigure", return_value=True, create=True) as reconfigure,
        ):
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={
                    "use_water_meter": True,
                    "water_mqtt_topic": "/water/precommit-race",
                    "water_mqtt_server_id": server["id"],
                },
            )

        assert response.status_code == 409
        assert response.get_json()["error_code"] == "GROUP_UPDATE_CONFLICT"
        current = _get_group(app, group["id"])
        assert current["name"] == "Concurrent precommit rename"
        assert current["use_water_meter"] == 0
        reconfigure.assert_not_called()

    def test_failed_water_monitor_reconfigure_restores_deleted_group(self, admin_client, app):
        from services.monitors import water_monitor

        group = _group(app)
        server = _server(app, name="Water delete rollback")
        assert app.db.update_group_fields(
            group["id"],
            {
                "use_water_meter": 1,
                "water_mqtt_topic": "/water/delete",
                "water_mqtt_server_id": server["id"],
            },
        )
        before = app.db.get_group_storage_snapshot(group["id"])

        with patch.object(water_monitor, "reconfigure", return_value=False, create=True):
            response = admin_client.delete(f"/api/groups/{group['id']}")

        assert response.status_code == 409
        assert response.get_json()["error_code"] == "WATER_MONITOR_RECONFIGURE_FAILED"
        assert app.db.get_group_storage_snapshot(group["id"]) == before

    def test_group_delete_cas_preserves_concurrent_update(self, admin_client, app):
        group = _group(app)
        original_delete = app.db.delete_group_if_unchanged

        def concurrent_update_before_delete(expected, **kwargs):
            assert kwargs == {"allow_observed_drift": True}
            assert app.db.update_group(group["id"], "Concurrent delete rename")
            return original_delete(expected, **kwargs)

        with patch.object(
            app.db,
            "delete_group_if_unchanged",
            side_effect=concurrent_update_before_delete,
        ):
            response = admin_client.delete(f"/api/groups/{group['id']}")

        assert response.status_code == 409
        assert response.get_json()["error_code"] == "GROUP_DELETE_CONFLICT"
        assert _get_group(app, group["id"])["name"] == "Concurrent delete rename"

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

    def test_unknown_zone_confirmation_blocks_master_rewire(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"])
        zone = _zone(app, group["id"], server["id"])
        app.db.update_zone(
            zone["id"],
            {"state": "off", "commanded_state": None, "observed_state": "off"},
        )

        response = admin_client.put(
            f"/api/groups/{group['id']}",
            json={"master_mqtt_topic": "/master/new"},
        )

        assert response.status_code == 409
        assert _get_group(app, group["id"])["master_mqtt_topic"] == "/master/old"

    def test_strict_zone_scan_failure_blocks_master_rewire(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"])

        with patch.object(
            app.db,
            "get_zones_strict",
            side_effect=sqlite3.OperationalError("safety scan failed"),
            create=True,
        ):
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={"master_mqtt_topic": "/master/new"},
            )

        assert response.status_code == 503
        assert _get_group(app, group["id"])["master_mqtt_topic"] == "/master/old"

    def test_open_master_is_closed_on_old_channel_before_rewire(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")

        events = []

        def publish(actual_server, topic, value, **_kwargs):
            events.append((actual_server["id"], topic, value))
            return True

        with (
            patch("routes.groups_api._publish_mqtt_value", side_effect=publish),
            patch("routes.groups_api._close_master_valve_confirmed", side_effect=_core_close_confirmed),
        ):
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={"master_mqtt_topic": "/master/new", "master_mode": "NO"},
            )

        assert response.status_code == 200
        assert events == [
            (server["id"], "/master/old", "0"),
            (server["id"], "/master/new", "1"),
        ]
        after = _get_group(app, group["id"])
        assert after["master_mqtt_topic"] == "/master/new"
        assert after["master_mode"] == "NO"
        assert after["master_valve_observed"] is None

    def test_fresh_old_master_echo_does_not_conflict_with_topology_commit(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")
        confirmations = 0

        def close_core(_server_id, _topic, _mode, publish_command):
            nonlocal confirmations
            assert publish_command()
            if confirmations == 0:
                # The permanent SSE subscriber receives the same fresh old-
                # channel echo as the command verifier and persists it before
                # the route reaches its topology commit.
                assert app.db.update_group_fields(
                    group["id"],
                    {"master_valve_observed": "closed"},
                )
            confirmations += 1
            return True

        with (
            patch("routes.groups_api._publish_mqtt_value", return_value=True),
            patch("routes.groups_api._close_master_valve_confirmed", side_effect=close_core),
        ):
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={"master_mqtt_topic": "/master/new"},
            )

        assert response.status_code == 200
        assert confirmations == 2
        after = _get_group(app, group["id"])
        assert after["master_mqtt_topic"] == "/master/new"
        assert after["master_valve_observed"] is None

    def test_master_observation_does_not_advance_config_revision(self, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")
        marker = "2001-02-03 04:05:06.000001"
        with app.db.groups._connect() as connection:
            connection.execute(
                "UPDATE groups SET updated_at = ? WHERE id = ?",
                (marker, group["id"]),
            )
            connection.commit()

        assert app.db.update_group_fields(group["id"], {"master_valve_observed": "closed"})

        after = app.db.get_group_storage_snapshot(group["id"])
        assert after["master_valve_observed"] == "closed"
        assert after["updated_at"] == marker

    def test_fresh_master_close_echo_does_not_block_group_delete(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")

        def close_core(_server_id, _topic, _mode, publish_command):
            assert publish_command()
            assert app.db.update_group_fields(
                group["id"],
                {"master_valve_observed": "closed"},
            )
            return True

        with (
            patch("routes.groups_api._publish_mqtt_value", return_value=True),
            patch("routes.groups_api._close_master_valve_confirmed", side_effect=close_core),
            patch("routes.groups_api._sse_hub.reload_hub"),
        ):
            response = admin_client.delete(f"/api/groups/{group['id']}")

        assert response.status_code == 204
        assert app.db.get_group_storage_snapshot(group["id"]) is None

    def test_failed_old_master_close_leaves_group_unchanged(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")
        before = _get_group(app, group["id"])

        with (
            patch("routes.groups_api._publish_mqtt_value", return_value=True),
            patch("routes.groups_api._close_master_valve_confirmed", side_effect=_core_close_without_echo),
        ):
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={"name": "must-not-commit", "use_master_valve": False},
            )

        assert response.status_code == 503
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

        with (
            patch("routes.groups_api._publish_mqtt_value", return_value=True) as publish,
            patch("routes.groups_api._close_master_valve_confirmed", side_effect=_core_close_confirmed),
        ):
            response = admin_client.delete(f"/api/groups/{group['id']}")

        assert response.status_code == 204
        publish.assert_called_once()
        assert all(int(g["id"]) != int(group["id"]) for g in app.db.get_groups())

    def test_failed_master_close_preserves_group_on_delete(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")

        with (
            patch("routes.groups_api._publish_mqtt_value", return_value=True),
            patch("routes.groups_api._close_master_valve_confirmed", side_effect=_core_close_without_echo),
        ):
            response = admin_client.delete(f"/api/groups/{group['id']}")

        assert response.status_code == 503
        assert _get_group(app, group["id"])["master_valve_observed"] == "open"

    def test_start_zone_must_belong_to_url_group(self, admin_client, app):
        first = _group(app, "First group")
        second = _group(app, "Second group")
        zone = _zone(app, second["id"], topic="")

        with patch("services.zone_control.start_zone_orchestrated") as start:
            response = admin_client.post(f"/api/groups/{first['id']}/start-zone/{zone['id']}")

        assert response.status_code == 409
        start.assert_not_called()

    def test_start_zone_rechecks_membership_after_waiting_for_group_lock(self, admin_client, app):
        import routes.groups_api as groups_api

        first = _group(app, "Start URL group")
        second = _group(app, "Concurrent destination")
        zone = _zone(app, first["id"], topic="")
        route_reached_lock = threading.Event()
        move_committed = threading.Event()
        real_group_lock = groups_api.group_lock

        def move_zone_between_preflight_and_lock():
            assert route_reached_lock.wait(1)
            assert app.db.update_zone(zone["id"], {"group_id": second["id"]})
            move_committed.set()

        mover = threading.Thread(target=move_zone_between_preflight_and_lock, daemon=True)
        mover.start()

        @contextmanager
        def barrier_group_lock(group_id):
            route_reached_lock.set()
            assert move_committed.wait(1)
            with real_group_lock(group_id):
                yield

        with (
            patch("routes.groups_api.group_lock", side_effect=barrier_group_lock),
            patch("services.zone_control.start_zone_orchestrated") as start,
        ):
            response = admin_client.post(f"/api/groups/{first['id']}/start-zone/{zone['id']}")

        mover.join(timeout=1)
        assert response.status_code == 409
        assert app.db.get_zone(zone["id"])["group_id"] == second["id"]
        start.assert_not_called()


class TestRainGroupEnforcement:
    def test_opt_in_enforces_after_commit_before_success(self, admin_client, app):
        from services.monitors import rain_monitor

        group = _group(app, "Rain opt-in")

        def enforce(group_id):
            persisted = _get_group(app, group_id)
            assert int(persisted["use_rain_sensor"] or 0) == 1
            return True

        with patch.object(rain_monitor, "enforce_group", side_effect=enforce, create=True) as enforce_group:
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={"use_rain_sensor": True},
            )

        assert response.status_code == 200
        enforce_group.assert_called_once_with(group["id"])
        assert int(_get_group(app, group["id"])["use_rain_sensor"] or 0) == 1

    @pytest.mark.parametrize("enforcement_result", [False, None, {"success": True}])
    def test_failed_or_noncanonical_enforcement_rolls_back_exactly(
        self,
        admin_client,
        app,
        enforcement_result,
    ):
        from services.monitors import rain_monitor

        group = _group(app, "Rain rollback")

        with patch.object(
            rain_monitor,
            "enforce_group",
            return_value=enforcement_result,
            create=True,
        ):
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={"use_rain_sensor": True},
            )

        assert response.status_code == 409
        assert response.get_json()["error_code"] == "RAIN_GROUP_ENFORCEMENT_FAILED"
        assert int(_get_group(app, group["id"])["use_rain_sensor"] or 0) == 0

    def test_enforcement_rollback_never_clobbers_concurrent_operator_update(self, admin_client, app):
        from services.monitors import rain_monitor

        group = _group(app, "Rain concurrent")

        def concurrent_update(_group_id):
            assert app.db.update_group_config(group["id"], {"name": "Operator won"})
            return False

        with patch.object(
            rain_monitor,
            "enforce_group",
            side_effect=concurrent_update,
            create=True,
        ):
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={"use_rain_sensor": True},
            )

        assert response.status_code == 500
        assert response.get_json()["error_code"] == "RAIN_GROUP_ENFORCEMENT_ROLLBACK_CONFLICT"
        persisted = _get_group(app, group["id"])
        assert persisted["name"] == "Operator won"
        assert int(persisted["use_rain_sensor"] or 0) == 1

    def test_combined_water_update_is_rebound_to_restored_snapshot_after_rain_failure(self, admin_client, app):
        from services.monitors import rain_monitor, water_monitor

        group = _group(app, "Rain and water rollback")
        server = _server(app, "Rain and water broker")

        with (
            patch.object(rain_monitor, "enforce_group", return_value=False, create=True),
            patch.object(water_monitor, "reconfigure", side_effect=[True, True], create=True) as water_reconfigure,
        ):
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={
                    "use_rain_sensor": True,
                    "use_water_meter": True,
                    "water_mqtt_server_id": server["id"],
                    "water_mqtt_topic": "/meter/pulses",
                },
            )

        assert response.status_code == 409
        assert response.get_json()["error_code"] == "RAIN_GROUP_ENFORCEMENT_FAILED"
        assert water_reconfigure.call_count == 2
        persisted = _get_group(app, group["id"])
        assert int(persisted["use_rain_sensor"] or 0) == 0
        assert int(persisted["use_water_meter"] or 0) == 0

    def test_non_opt_in_group_updates_do_not_enforce_rain(self, admin_client, app):
        from services.monitors import rain_monitor

        group = _group(app, "Rain unchanged")
        assert app.db.set_group_use_rain(group["id"], True)

        with patch.object(rain_monitor, "enforce_group", return_value=True, create=True) as enforce_group:
            same = admin_client.put(f"/api/groups/{group['id']}", json={"use_rain_sensor": True})
            disabled = admin_client.put(f"/api/groups/{group['id']}", json={"use_rain_sensor": False})

        assert same.status_code == 200
        assert disabled.status_code == 200
        enforce_group.assert_not_called()


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
            patch("routes.groups_api._close_master_valve_confirmed", side_effect=_core_close_confirmed),
        ):
            response = admin_client.post(f"/api/groups/{group['id']}/master-valve/close")

        assert response.status_code == 409
        publish.assert_not_called()
        assert _get_group(app, group["id"])["master_valve_observed"] == "open"

    def test_manual_close_delegates_to_core_and_publishes_inside_its_callback(self, admin_client, app):
        from services import zone_control

        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")
        events = []

        def close_core(server_id, topic, mode, publish_command):
            assert server_id == server["id"]
            assert topic == "/master/old"
            assert mode == "NC"
            events.append("core_enter")
            result = publish_command()
            events.append("fresh_echo")
            return result

        def publish(_server, topic, value, **_kwargs):
            assert topic == "/master/old"
            assert value == "0"
            events.append("publish")
            return True

        with (
            patch.object(zone_control, "cancel_pending_master_close") as cancel,
            patch("routes.groups_api._publish_mqtt_value", side_effect=publish),
            patch("routes.groups_api._close_master_valve_confirmed", side_effect=close_core),
        ):
            response = admin_client.post(f"/api/groups/{group['id']}/master-valve/close")

        assert response.status_code == 200
        assert events == ["core_enter", "publish", "fresh_echo"]
        cancel.assert_not_called()

    def test_publish_ack_without_fresh_echo_returns_503_and_does_not_claim_observed_state(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="open")

        with (
            patch("routes.groups_api._publish_mqtt_value", return_value=True) as publish,
            patch("routes.groups_api._close_master_valve_confirmed", side_effect=_core_close_without_echo),
        ):
            response = admin_client.post(f"/api/groups/{group['id']}/master-valve/close")

        assert response.status_code == 503
        assert response.get_json()["error_code"] == "MASTER_CONFIRMATION_TIMEOUT"
        publish.assert_called_once()
        assert _get_group(app, group["id"])["master_valve_observed"] == "open"

    def test_mode_only_rewire_confirms_both_old_and_new_close_values(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _configure_master(app, group["id"], server["id"], observed="closed")
        confirmations = []

        def close_core(server_id, topic, mode, publish_command):
            expected_payload = "1" if mode == "NO" else "0"
            confirmations.append((server_id, topic, expected_payload))
            return bool(publish_command())

        with (
            patch("routes.groups_api._publish_mqtt_value", return_value=True),
            patch("routes.groups_api._close_master_valve_confirmed", side_effect=close_core),
        ):
            response = admin_client.put(f"/api/groups/{group['id']}", json={"master_mode": "NO"})

        assert response.status_code == 200
        assert confirmations == [
            (server["id"], "/master/old", "0"),
            (server["id"], "/master/old", "1"),
        ]
        assert _get_group(app, group["id"])["master_valve_observed"] is None


class TestGroupStopConfirmationSafety:
    @staticmethod
    def _result(group_id, *, stopped=None, unresolved=None, **overrides):
        result = {
            "success": not unresolved,
            "group_id": group_id,
            "aggregate_valid": True,
            "stopped": list(stopped or []),
            "unresolved": list(unresolved or []),
            "unverified_zone_ids": [],
            "retry_scheduled": bool(unresolved),
        }
        result.update(overrides)
        return result

    def test_malformed_scheduler_aggregate_is_never_reported_as_success(self, admin_client, app):
        group = _group(app)
        zone = _zone(app, group["id"], topic="")
        valid = self._result(group["id"], stopped=[zone["id"]])
        malformed_results = [
            None,
            {},
            {key: value for key, value in valid.items() if key != "group_id"},
            {**valid, "extra": False},
            {**valid, "group_id": group["id"] + 1},
            {**valid, "group_id": True},
            {**valid, "stopped": []},
            {**valid, "unresolved": [zone["id"]]},
            {**valid, "success": False},
            {**valid, "stopped": [True]},
            {**valid, "stopped": [zone["id"], zone["id"]]},
            {**valid, "unverified_zone_ids": [zone["id"]]},
            {**valid, "retry_scheduled": True},
            {
                **valid,
                "success": False,
                "aggregate_valid": False,
                "stopped": [],
                "unverified_zone_ids": [],
            },
        ]

        for malformed in malformed_results:
            scheduler = Mock()
            scheduler.cancel_group_jobs.return_value = malformed
            with (
                patch("services.zone_control.stop_all_in_group") as core_stop,
                patch("routes.groups_api.get_scheduler", return_value=scheduler),
            ):
                response = admin_client.post(f"/api/groups/{group['id']}/stop")

            assert response.status_code == 503
            assert response.get_json()["unresolved"] == [zone["id"]]
            scheduler.cancel_group_jobs.assert_called_once_with(group["id"])
            core_stop.assert_not_called()

    @pytest.mark.parametrize("scheduler_value", [None, object()])
    def test_missing_scheduler_barrier_fails_closed_without_core_fallback(self, admin_client, app, scheduler_value):
        group = _group(app)
        zone = _zone(app, group["id"], topic="")

        with (
            patch("routes.groups_api.get_scheduler", return_value=scheduler_value),
            patch("services.zone_control.stop_all_in_group") as core_stop,
        ):
            response = admin_client.post(f"/api/groups/{group['id']}/stop")

        assert response.status_code == 503
        assert response.get_json()["unverified_zone_ids"] == [zone["id"]]
        core_stop.assert_not_called()

    def test_scheduler_exception_fails_closed_without_core_fallback(self, admin_client, app):
        group = _group(app)
        zone = _zone(app, group["id"], topic="")
        scheduler = Mock()
        scheduler.cancel_group_jobs.side_effect = RuntimeError("pending-job scan unavailable")

        with (
            patch("routes.groups_api.get_scheduler", return_value=scheduler),
            patch("services.zone_control.stop_all_in_group") as core_stop,
        ):
            response = admin_client.post(f"/api/groups/{group['id']}/stop")

        assert response.status_code == 503
        assert response.get_json()["unverified_zone_ids"] == [zone["id"]]
        core_stop.assert_not_called()

    def test_route_does_not_hold_group_lock_while_scheduler_waits_for_runner_ack(self, admin_client, app):
        from services.locks import group_lock

        group = _group(app)
        zone = _zone(app, group["id"], topic="")
        scheduler = Mock()
        lock_acquired = threading.Event()
        probe_thread = None

        def cancel_group_jobs(_group_id):
            nonlocal probe_thread

            def probe_group_lock():
                with group_lock(group["id"]):
                    lock_acquired.set()

            probe_thread = threading.Thread(target=probe_group_lock, daemon=True)
            probe_thread.start()
            if not lock_acquired.wait(0.5):
                raise RuntimeError("route held group lock across runner acknowledgement")
            return self._result(group["id"], stopped=[zone["id"]])

        scheduler.cancel_group_jobs.side_effect = cancel_group_jobs
        with patch("routes.groups_api.get_scheduler", return_value=scheduler):
            response = admin_client.post(f"/api/groups/{group['id']}/stop")

        if probe_thread is not None:
            probe_thread.join(timeout=1)
        assert response.status_code == 200
        assert lock_acquired.is_set()

    def test_scheduler_barrier_precedes_route_inventory_and_still_runs_if_inventory_fails(self, admin_client, app):
        group = _group(app)
        events = []
        scheduler = Mock()

        def cancel_group_jobs(_group_id):
            events.append("cancel")
            return self._result(group["id"])

        def fail_inventory():
            events.append("inventory")
            raise sqlite3.OperationalError("strict inventory unavailable")

        scheduler.cancel_group_jobs.side_effect = cancel_group_jobs
        with (
            patch("routes.groups_api.get_scheduler", return_value=scheduler),
            patch("routes.groups_api._strict_zones", side_effect=fail_inventory),
        ):
            response = admin_client.post(f"/api/groups/{group['id']}/stop")

        assert response.status_code == 503
        assert events == ["cancel", "inventory"]

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
        result = self._result(group["id"], unresolved=[zone["id"]])
        scheduler.cancel_group_jobs.return_value = result

        with (
            patch("routes.groups_api.get_scheduler", return_value=scheduler) as get_scheduler,
            patch("services.zone_control.stop_all_in_group") as core_stop,
            patch.object(app.db, "clear_group_scheduled_starts") as clear_starts,
            patch.object(app.db, "reschedule_group_to_next_program") as reschedule,
        ):
            response = admin_client.post(f"/api/groups/{group['id']}/stop")

        assert response.status_code == 503
        assert response.get_json() == {
            "success": False,
            "message": "Не все зоны и задания группы подтверждены безопасными",
            "stopped": [],
            "unresolved": [zone["id"]],
            "unverified_zone_ids": [],
            "retry_scheduled": True,
        }
        get_scheduler.assert_called_once_with()
        scheduler.cancel_group_jobs.assert_called_once_with(group["id"])
        core_stop.assert_not_called()
        clear_starts.assert_not_called()
        reschedule.assert_not_called()
        app.db.update_zone(zone["id"], {"observed_state": "off"})

    def test_scheduler_invalid_partition_is_never_reported_as_success(self, admin_client, app):
        group = _group(app)
        zone = _zone(app, group["id"], topic="")
        scheduler = Mock()
        scheduler.cancel_group_jobs.return_value = self._result(
            group["id"],
            success=False,
            aggregate_valid=False,
            stopped=[],
            unresolved=[],
            unverified_zone_ids=[zone["id"]],
            retry_scheduled=False,
        )

        with (
            patch("routes.groups_api.get_scheduler", return_value=scheduler),
            patch.object(app.db, "clear_group_scheduled_starts") as clear_starts,
            patch.object(app.db, "reschedule_group_to_next_program") as reschedule,
        ):
            response = admin_client.post(f"/api/groups/{group['id']}/stop")

        assert response.status_code == 503
        assert response.get_json()["unverified_zone_ids"] == [zone["id"]]
        clear_starts.assert_not_called()
        reschedule.assert_not_called()

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
        scheduler = Mock()
        scheduler.cancel_group_jobs.return_value = self._result(group["id"], stopped=[zone["id"]])

        with (
            patch("routes.groups_api.get_scheduler", return_value=scheduler),
            patch("services.zone_control.stop_all_in_group") as core_stop,
        ):
            response = admin_client.post(f"/api/groups/{group['id']}/stop")

        assert response.status_code == 200
        scheduler.cancel_group_jobs.assert_called_once_with(group["id"])
        core_stop.assert_not_called()
        assert app.db.is_program_run_cancelled_for_group(program["id"], today, group["id"]) is False


class TestMqttServerTopologySafety:
    def test_server_read_models_expose_nonsecret_reference_metadata(self, admin_client, app):
        group = _group(app)
        server = app.db.create_mqtt_server(
            {
                "name": "Referenced MQTT",
                "host": "referenced-host",
                "port": 1883,
                "username": "operator",
                "password": "top-secret",
            }
        )
        zone = _zone(app, group["id"], server["id"], topic="")

        listed_response = admin_client.get("/api/mqtt/servers")
        detail_response = admin_client.get(f"/api/mqtt/servers/{server['id']}")

        assert (listed_response.status_code, detail_response.status_code) == (200, 200)
        listed = next(item for item in listed_response.get_json()["servers"] if int(item["id"]) == int(server["id"]))
        detail = detail_response.get_json()["server"]
        for item in (listed, detail):
            assert item["is_referenced"] is True
            assert item["references"] == {"zones": [zone["id"]]}
            assert item["password"] == "***"
            assert "top-secret" not in str(item["references"])

    def test_unreferenced_server_read_models_return_false_and_empty_references(self, admin_client, app):
        server = _server(app)

        listed_response = admin_client.get("/api/mqtt/servers")
        detail_response = admin_client.get(f"/api/mqtt/servers/{server['id']}")

        listed = next(item for item in listed_response.get_json()["servers"] if int(item["id"]) == int(server["id"]))
        detail = detail_response.get_json()["server"]
        for item in (listed, detail):
            assert item["is_referenced"] is False
            assert item["references"] == {}

    @pytest.mark.parametrize("path", ["/api/mqtt/servers", "/api/mqtt/servers/{server_id}"])
    def test_reference_scan_failure_does_not_report_server_as_unreferenced(self, admin_client, app, path):
        server = _server(app)

        with patch.object(
            app.db,
            "get_mqtt_server_references",
            side_effect=sqlite3.OperationalError("reference scan failed"),
            create=True,
        ):
            response = admin_client.get(path.format(server_id=server["id"]))

        assert response.status_code == 500
        assert response.get_json()["success"] is False

    def test_server_crud_does_not_rebind_unreferenced_monitors(self, admin_client, app):
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
        water.assert_not_called()
        env.assert_not_called()

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

        with patch("routes.mqtt_api._refresh_mqtt_runtime") as refresh:
            response = admin_client.put(f"/api/mqtt/servers/{server['id']}", json={"name": "Renamed"})

        assert response.status_code == 200
        assert app.db.get_mqtt_server(server["id"])["name"] == "Renamed"
        refresh.assert_not_called()

    def test_referenced_server_full_form_with_unchanged_runtime_values_is_allowed(self, admin_client, app):
        group = _group(app)
        server = app.db.create_mqtt_server(
            {
                "name": "Full form",
                "host": "same-host",
                "port": 1883,
                "username": "operator",
                "password": "secret",
                "enabled": True,
                "tls_enabled": False,
                "tls_insecure": False,
            }
        )
        _zone(app, group["id"], server["id"])

        payload = {
            "name": "Full form renamed",
            "host": server["host"],
            "port": server["port"],
            "username": server["username"],
            "password": "***",
            "client_id": server["client_id"],
            "enabled": bool(server["enabled"]),
            "tls_enabled": bool(server["tls_enabled"]),
            "tls_ca_path": server["tls_ca_path"],
            "tls_cert_path": server["tls_cert_path"],
            "tls_key_path": server["tls_key_path"],
            "tls_insecure": bool(server["tls_insecure"]),
            "tls_version": server["tls_version"],
        }
        with patch("routes.mqtt_api._refresh_mqtt_runtime") as refresh:
            response = admin_client.put(f"/api/mqtt/servers/{server['id']}", json=payload)

        assert response.status_code == 200
        assert app.db.get_mqtt_server(server["id"])["name"] == "Full form renamed"
        refresh.assert_not_called()

    def test_stale_full_form_never_reverts_concurrent_runtime_rotation(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        zone = _zone(app, group["id"], server["id"])

        def rotate_runtime_after_preflight(_server_id):
            assert app.db.update_mqtt_server(server["id"], {"host": "concurrent-host"})
            return {"zones": [zone["id"]]}

        with (
            patch(
                "routes.mqtt_api._mqtt_server_references",
                side_effect=rotate_runtime_after_preflight,
            ),
            patch("routes.mqtt_api._refresh_mqtt_runtime") as refresh,
        ):
            response = admin_client.put(
                f"/api/mqtt/servers/{server['id']}",
                json={"name": "Cosmetic after race", "host": server["host"]},
            )

        assert response.status_code == 200
        current = app.db.get_mqtt_server(server["id"])
        assert current["name"] == "Cosmetic after race"
        assert current["host"] == "concurrent-host"
        refresh.assert_not_called()

    def test_referenced_server_reports_only_actual_blocked_runtime_delta(self, admin_client, app):
        group = _group(app)
        server = _server(app)
        _zone(app, group["id"], server["id"])

        response = admin_client.put(
            f"/api/mqtt/servers/{server['id']}",
            json={"name": "Cosmetic", "host": "changed", "port": server["port"]},
        )

        assert response.status_code == 409
        assert response.get_json()["blocked_fields"] == ["host"]

    def test_enabled_rain_only_reference_stages_runtime_before_accepting_update(self, admin_client, app):
        server = _server(app)
        assert app.db.set_rain_config({"enabled": True, "topic": "/rain", "type": "NO", "server_id": server["id"]})

        with (
            patch("routes.mqtt_api._reconfigure_rain_monitor", return_value=True) as reconfigure,
            patch("routes.mqtt_api._refresh_mqtt_runtime") as refresh,
        ):
            response = admin_client.put(f"/api/mqtt/servers/{server['id']}", json={"host": "rain-new"})

        assert response.status_code == 200
        assert app.db.get_mqtt_server(server["id"])["host"] == "rain-new"
        reconfigure.assert_called_once_with(
            {"enabled": True, "topic": "/rain", "type": "NO", "server_id": server["id"]}
        )
        refresh.assert_called_once_with(server["id"])

    def test_failed_rain_stage_cas_rolls_back_server_update(self, admin_client, app):
        server = _server(app)
        assert app.db.set_rain_config({"enabled": True, "topic": "/rain", "type": "NC", "server_id": server["id"]})
        before = app.db.get_mqtt_server_storage_snapshot(server["id"])

        with (
            patch("routes.mqtt_api._reconfigure_rain_monitor", return_value=False),
            patch("routes.mqtt_api._refresh_mqtt_runtime") as refresh,
        ):
            response = admin_client.put(
                f"/api/mqtt/servers/{server['id']}",
                json={"host": "must-rollback", "password": "rotated"},
            )

        assert response.status_code == 409
        assert response.get_json()["error_code"] == "RAIN_MONITOR_RECONFIGURE_FAILED"
        assert app.db.get_mqtt_server_storage_snapshot(server["id"]) == before
        refresh.assert_not_called()

    def test_rain_rollback_never_clobbers_concurrent_server_update(self, admin_client, app):
        server = _server(app)
        assert app.db.set_rain_config({"enabled": True, "topic": "/rain", "type": "NO", "server_id": server["id"]})

        def concurrent_update_then_reject(_config):
            assert app.db.update_mqtt_server(server["id"], {"name": "Concurrent broker rename"})
            return False

        with (
            patch(
                "routes.mqtt_api._reconfigure_rain_monitor",
                side_effect=concurrent_update_then_reject,
            ),
            patch("routes.mqtt_api._refresh_mqtt_runtime") as refresh,
        ):
            response = admin_client.put(
                f"/api/mqtt/servers/{server['id']}",
                json={"host": "rain-race"},
            )

        assert response.status_code == 500
        assert response.get_json()["error_code"] == "RAIN_MONITOR_ROLLBACK_CONFLICT"
        current = app.db.get_mqtt_server(server["id"])
        assert current["name"] == "Concurrent broker rename"
        assert current["host"] == "rain-race"
        refresh.assert_not_called()

    def test_rain_referenced_cosmetic_rename_never_reconnects(self, admin_client, app):
        server = _server(app)
        assert app.db.set_rain_config({"enabled": True, "topic": "/rain", "type": "NO", "server_id": server["id"]})

        with (
            patch("routes.mqtt_api._reconfigure_rain_monitor") as reconfigure,
            patch("routes.mqtt_api._refresh_mqtt_runtime") as refresh,
        ):
            response = admin_client.put(f"/api/mqtt/servers/{server['id']}", json={"name": "Rain renamed"})

        assert response.status_code == 200
        reconfigure.assert_not_called()
        refresh.assert_not_called()

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
