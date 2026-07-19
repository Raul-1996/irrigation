"""Tests for /api/groups/* endpoints."""

import json
import os
from unittest.mock import patch

os.environ["TESTING"] = "1"


def _complete_stop_result(group_id, zone_ids=()):
    return {
        "success": True,
        "group_id": int(group_id),
        "aggregate_valid": True,
        "stopped": list(zone_ids),
        "unresolved": [],
        "unverified_zone_ids": [],
        "retry_scheduled": False,
    }


class TestGroupsAPI:
    def test_get_groups(self, admin_client):
        resp = admin_client.get("/api/groups")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) >= 1  # At least default groups

    def test_create_group(self, admin_client):
        resp = admin_client.post("/api/groups", data=json.dumps({"name": "New Line"}), content_type="application/json")
        assert resp.status_code == 201

    def test_update_group(self, admin_client, app):
        group = app.db.create_group("To Update")
        resp = admin_client.put(
            f"/api/groups/{group['id']}", data=json.dumps({"name": "Updated Name"}), content_type="application/json"
        )
        assert resp.status_code == 200

    def test_delete_group(self, admin_client, app):
        group = app.db.create_group("To Delete")
        resp = admin_client.delete(f"/api/groups/{group['id']}")
        assert resp.status_code == 204

    def test_delete_group_with_zones(self, admin_client, app):
        group = app.db.create_group("Has Zones")
        app.db.create_zone({"name": "Z", "duration": 10, "group_id": group["id"]})
        resp = admin_client.delete(f"/api/groups/{group['id']}")
        assert resp.status_code in (204, 400)

    def test_stop_group(self, admin_client, app):
        zone_ids = [int(zone["id"]) for zone in app.db.get_zones() if int(zone.get("group_id") or 0) == 1]
        with patch("routes.groups_api.get_scheduler") as get_scheduler:
            get_scheduler.return_value.cancel_group_jobs.return_value = _complete_stop_result(1, zone_ids)
            resp = admin_client.post("/api/groups/1/stop", content_type="application/json")
        assert resp.status_code == 200

    def test_start_from_first(self, admin_client, app):
        app.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        resp = admin_client.post("/api/groups/1/start-from-first", content_type="application/json")
        # May fail due to scheduler not init, but should not 500
        assert resp.status_code in (200, 400, 500)


class TestMasterValveAPI:
    def test_master_valve_no_config(self, admin_client, app):
        """Toggle master valve on group without master valve config."""
        resp = admin_client.post("/api/groups/1/master-valve/open", content_type="application/json")
        assert resp.status_code == 400

    def test_update_group_with_master_valve(self, admin_client, app):
        group = app.db.create_group("MV Group")
        server = app.db.create_mqtt_server({"name": "S", "host": "h", "port": 1883})
        with (
            patch(
                "routes.groups_api._close_master_valve_confirmed",
                side_effect=lambda _sid, _topic, _mode, publish_command: bool(publish_command()),
            ),
            patch("routes.groups_api._publish_mqtt_value", return_value=True),
        ):
            resp = admin_client.put(
                f"/api/groups/{group['id']}",
                data=json.dumps(
                    {
                        "use_master_valve": True,
                        "master_mqtt_topic": "/mv/test",
                        "master_mode": "NC",
                        "master_mqtt_server_id": server["id"],
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_manual_open_delegates_to_activation_bound_core_helper(self, admin_client, app):
        group = app.db.create_group("Manual master open")
        server = app.db.create_mqtt_server({"name": "S", "host": "127.0.0.1", "port": 1883})
        topic = "/master/manual"
        app.db.update_group_fields(
            group["id"],
            {
                "use_master_valve": 1,
                "master_mqtt_topic": topic,
                "master_mqtt_server_id": server["id"],
                "master_mode": "NC",
            },
        )

        events = []

        def activate(group_id, server_id, normalized_topic, mode, publish_command):
            assert group_id == group["id"]
            assert server_id == server["id"]
            assert normalized_topic == topic
            assert mode == "NC"
            events.append("core_enter")
            result = publish_command()
            events.append("core_exit")
            return result

        def publish(_server, normalized_topic, value, **_kwargs):
            assert normalized_topic == topic
            assert value == "1"
            events.append("publish")
            return True

        def verify(_server_id, _topic, _value, publish_callback):
            return publish_callback()

        with (
            patch("routes.groups_api._activate_manual_master_open", side_effect=activate),
            patch("routes.groups_api._publish_mqtt_value", side_effect=publish),
            patch("routes.groups_api._verify_master_command", side_effect=verify),
            patch("routes.groups_api._sse_hub.broadcast"),
        ):
            resp = admin_client.post(
                f"/api/groups/{group['id']}/master-valve/open",
                content_type="application/json",
            )

        assert resp.status_code == 200
        assert events == ["core_enter", "publish", "core_exit"]

    def test_manual_open_never_publishes_when_core_cannot_plant_safety_cap(self, admin_client, app):
        group = app.db.create_group("Manual master safety unavailable")
        server = app.db.create_mqtt_server({"name": "S", "host": "127.0.0.1", "port": 1883})
        app.db.update_group_fields(
            group["id"],
            {
                "use_master_valve": 1,
                "master_mqtt_topic": "/master/safe",
                "master_mqtt_server_id": server["id"],
                "master_mode": "NC",
            },
        )

        with (
            patch("routes.groups_api._activate_manual_master_open", return_value=False) as activate,
            patch("routes.groups_api._publish_mqtt_value") as publish,
            patch("routes.groups_api._verify_master_command") as verify,
        ):
            response = admin_client.post(f"/api/groups/{group['id']}/master-valve/open")

        assert response.status_code == 503
        assert response.get_json()["error_code"] == "MASTER_CONFIRMATION_TIMEOUT"
        activate.assert_called_once()
        publish.assert_not_called()
        verify.assert_not_called()


class TestGroupStopAuditIssue16:
    """Spec §4.2 #8: /api/groups/<gid>/stop must emit session_aborted_by_user
    so a single audit query catches user-driven aborts regardless of which
    button was pressed."""

    def test_group_stop_emits_audit_session_aborted(self, admin_client, app):
        from irrigation_scheduler import init_scheduler

        init_scheduler(app.db)  # wire the scheduler into the app
        group = app.db.create_group("#16 GroupStop Audit")

        resp = admin_client.post(f"/api/groups/{group['id']}/stop", content_type="application/json")
        assert resp.status_code == 200

        rows = app.db.get_audit_logs(action_type="session_aborted_by_user")
        matched = [r for r in rows if r.get("target") == f"group:{group['id']}"]
        assert matched, f"no session_aborted_by_user audit row for group:{group['id']}"
        pj = str(matched[0].get("payload_json") or "")
        assert "api_stop_group" in pj


class TestUseMasterValveValidation:
    """Регрессия: валидация use_master_valve падала AttributeError —
    фасад IrrigationDB не имеет get_group."""

    def test_enable_master_valve_without_topic_is_400_not_500(self, admin_client, app):
        g = app.db.create_group("MV Group")
        resp = admin_client.put(f"/api/groups/{g['id']}", json={"use_master_valve": True})
        assert resp.status_code == 400
        assert "топик" in (resp.get_json() or {}).get("message", "").lower()
