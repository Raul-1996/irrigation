"""Deep tests for zones CRUD API routes."""

import json
from unittest.mock import patch


def _create_test_server(app):
    return app.db.create_mqtt_server({"name": "S1", "host": "127.0.0.1", "port": 1883, "enabled": 1})["id"]


class TestZonesCRUD:
    """Tests for /api/zones CRUD."""

    def test_list_zones(self, admin_client):
        resp = admin_client.get("/api/zones")
        assert resp.status_code == 200

    def test_create_zone(self, admin_client):
        resp = admin_client.post(
            "/api/zones",
            data=json.dumps(
                {
                    "name": "Тест Газон",
                    "duration": 15,
                    "group_id": 1,
                    "icon": "🌿",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code in (200, 201)

    def test_get_zone(self, admin_client, app):
        app.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        zones = app.db.get_zones()
        zid = zones[0]["id"]
        resp = admin_client.get(f"/api/zones/{zid}")
        assert resp.status_code == 200

    def test_get_zone_not_found(self, admin_client):
        resp = admin_client.get("/api/zones/99999")
        assert resp.status_code in (200, 404)

    def test_update_zone(self, admin_client, app):
        app.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        zones = app.db.get_zones()
        zid = zones[0]["id"]
        resp = admin_client.put(
            f"/api/zones/{zid}",
            data=json.dumps({"name": "Z1 Updated", "duration": 20, "expected_version": zones[0]["version"]}),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_delete_zone(self, admin_client, app):
        app.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        zones = app.db.get_zones()
        zid = zones[0]["id"]
        app.db.update_zone(zid, {"state": "off", "commanded_state": "off", "observed_state": "off"})
        resp = admin_client.delete(f"/api/zones/{zid}")
        assert resp.status_code in (200, 204)


class TestZonesWatering:
    """Tests for zone watering start/stop."""

    def test_start_zone(self, admin_client, app):
        server_id = _create_test_server(app)
        app.db.create_zone(
            {
                "name": "Z1",
                "duration": 10,
                "group_id": 1,
                "topic": "/devices/test/K1",
                "mqtt_server_id": server_id,
            }
        )
        zones = app.db.get_zones()
        zid = zones[0]["id"]
        with patch(
            "services.zone_control.start_zone_orchestrated",
            return_value=("started", {"warnings": [], "duration": 10}),
        ):
            resp = admin_client.post(f"/api/zones/{zid}/start")
        assert resp.status_code == 200

    def test_stop_zone(self, admin_client, app):
        server_id = _create_test_server(app)
        app.db.create_zone(
            {
                "name": "Z1",
                "duration": 10,
                "group_id": 1,
                "topic": "/devices/test/K1",
                "mqtt_server_id": server_id,
            }
        )
        zones = app.db.get_zones()
        zid = zones[0]["id"]
        with patch("services.zone_control.stop_zone"):
            resp = admin_client.post(f"/api/zones/{zid}/stop")
        assert resp.status_code == 200

    def test_start_nonexistent_zone(self, admin_client):
        resp = admin_client.post("/api/zones/99999/start")
        assert resp.status_code in (200, 404)

    def test_next_watering_bulk(self, admin_client, app):
        """POST /api/zones/next-watering-bulk."""
        app.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        zones = app.db.get_zones()
        resp = admin_client.post(
            "/api/zones/next-watering-bulk",
            data=json.dumps({"zone_ids": [zones[0]["id"]]}),
            content_type="application/json",
        )
        assert resp.status_code == 200


class TestSseHubReloadOnWiringChange:
    """Zone/group MQTT wiring changes must trigger sse_hub.reload_hub."""

    def test_zone_put_topic_change_reloads_hub(self, admin_client, app):
        server_id = _create_test_server(app)
        z = app.db.create_zone(
            {"name": "Z1", "duration": 10, "group_id": 1, "topic": "/devices/test/K1", "mqtt_server_id": server_id}
        )
        app.db.update_zone(z["id"], {"state": "off", "commanded_state": "off", "observed_state": "off"})
        current = app.db.get_zone(z["id"])
        with patch("services.sse_hub.reload_hub") as mock_reload:
            resp = admin_client.put(
                f"/api/zones/{z['id']}",
                data=json.dumps({"topic": "/devices/test/K2", "expected_version": current["version"]}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        assert mock_reload.called

    def test_zone_put_unrelated_change_does_not_reload(self, admin_client, app):
        server_id = _create_test_server(app)
        z = app.db.create_zone(
            {"name": "Z1", "duration": 10, "group_id": 1, "topic": "/devices/test/K1", "mqtt_server_id": server_id}
        )
        app.db.update_zone(z["id"], {"state": "off", "commanded_state": "off", "observed_state": "off"})
        current = app.db.get_zone(z["id"])
        with patch("services.sse_hub.reload_hub") as mock_reload:
            resp = admin_client.put(
                f"/api/zones/{z['id']}",
                data=json.dumps({"name": "Renamed", "duration": 20, "expected_version": current["version"]}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        assert not mock_reload.called

    def test_zone_put_same_topic_does_not_reload(self, admin_client, app):
        server_id = _create_test_server(app)
        z = app.db.create_zone(
            {"name": "Z1", "duration": 10, "group_id": 1, "topic": "/devices/test/K1", "mqtt_server_id": server_id}
        )
        with patch("services.sse_hub.reload_hub") as mock_reload:
            resp = admin_client.put(
                f"/api/zones/{z['id']}",
                data=json.dumps({"topic": "/devices/test/K1", "expected_version": z["version"]}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        assert not mock_reload.called

    def test_zone_delete_with_topic_reloads_hub(self, admin_client, app):
        server_id = _create_test_server(app)
        z = app.db.create_zone(
            {"name": "Z1", "duration": 10, "group_id": 1, "topic": "/devices/test/K1", "mqtt_server_id": server_id}
        )
        app.db.update_zone(z["id"], {"state": "off", "commanded_state": "off", "observed_state": "off"})
        with patch("services.sse_hub.reload_hub") as mock_reload:
            resp = admin_client.delete(f"/api/zones/{z['id']}")
        assert resp.status_code in (200, 204)
        assert mock_reload.called

    def test_group_put_master_topic_change_reloads_hub(self, admin_client, app):
        g = app.db.create_group("MV Group")
        with patch("services.sse_hub.reload_hub") as mock_reload:
            resp = admin_client.put(
                f"/api/groups/{g['id']}",
                data=json.dumps({"master_mqtt_topic": "/devices/mv/K1"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        assert mock_reload.called
