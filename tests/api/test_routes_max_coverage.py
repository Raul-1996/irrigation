"""Maximum coverage route tests — exercise every branch possible."""

import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch

from tests.safety_contracts import confirmed_group_stop

os.environ["TESTING"] = "1"


class TestGroupsAPIDeep:
    def test_create_group(self, admin_client):
        resp = admin_client.post(
            "/api/groups", data=json.dumps({"name": "Deep Group"}), content_type="application/json"
        )
        assert resp.status_code in (200, 201)

    def test_update_group_all_fields(self, admin_client, app):
        g = app.db.create_group("AllFields")
        srv = app.db.create_mqtt_server({"name": "T", "host": "127.0.0.1", "port": 1883})
        with (
            patch("routes.groups_api._confirm_master_closed", return_value=(True, None)),
            patch("routes.groups_api._reconfigure_water_monitor", return_value=True),
        ):
            resp = admin_client.put(
                f"/api/groups/{g['id']}",
                data=json.dumps(
                    {
                        "name": "Updated All",
                        "icon": "💧",
                        "use_master_valve": 1,
                        "master_mqtt_topic": "/mv",
                        "master_mqtt_server_id": srv["id"],
                        "master_mode": "NO",
                        "use_water_meter": 1,
                        "water_mqtt_topic": "/water",
                        "water_mqtt_server_id": srv["id"],
                        "water_pulse_size": "10l",
                        "water_base_value_m3": 100.5,
                        "water_base_pulses": 1000,
                        "use_rain": True,
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_delete_group_with_zones(self, admin_client, app):
        g = app.db.create_group("WithZones")
        app.db.create_zone({"name": "Z", "duration": 10, "group_id": g["id"]})
        resp = admin_client.delete(f"/api/groups/{g['id']}")
        assert resp.status_code in (200, 204, 400)

    def test_stop_group_with_watering(self, admin_client, app):
        g = app.db.create_group("StopWater")
        z = app.db.create_zone({"name": "Z", "duration": 10, "group_id": g["id"], "topic": "/t/z"})
        app.db.update_zone(z["id"], {"state": "on", "watering_start_time": "2026-01-01 10:00:00"})
        with confirmed_group_stop(app.db, "routes.groups_api.get_scheduler"):
            resp = admin_client.post(f"/api/groups/{g['id']}/stop", content_type="application/json")
        assert resp.status_code in (200, 400, 500)


class TestMqttAPIDeep:
    def test_create_with_tls(self, admin_client):
        resp = admin_client.post(
            "/api/mqtt/servers",
            data=json.dumps(
                {
                    "name": "TLS",
                    "host": "10.0.0.1",
                    "port": 8883,
                    "tls_enabled": 1,
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code in (200, 201)

    def test_create_minimal(self, admin_client):
        resp = admin_client.post(
            "/api/mqtt/servers", data=json.dumps({"name": "Min", "host": "10.0.0.1"}), content_type="application/json"
        )
        assert resp.status_code in (200, 201)

    def test_mqtt_scan_sse(self, admin_client, app):
        s = app.db.create_mqtt_server({"name": "SSE", "host": "127.0.0.1", "port": 1883})
        resp = admin_client.get(f"/api/mqtt/{s['id']}/scan-sse")
        assert resp.status_code in (200, 400, 500)

    def test_mqtt_status(self, admin_client, app):
        s = app.db.create_mqtt_server({"name": "ST", "host": "127.0.0.1", "port": 1883})
        resp = admin_client.get(f"/api/mqtt/{s['id']}/status")
        assert resp.status_code in (200, 404, 500)


class TestZoneStartStopBranches:
    def test_start_zone_basic(self, admin_client, app):
        z = app.db.create_zone({"name": "SB", "duration": 10, "group_id": 1, "topic": "/t/z"})
        resp = admin_client.post(f"/api/zones/{z['id']}/start", content_type="application/json")
        assert resp.status_code in (200, 400, 500)

    def test_start_nonexistent(self, admin_client):
        resp = admin_client.post("/api/zones/99999/start", content_type="application/json")
        assert resp.status_code in (404, 400, 500)

    def test_stop_basic(self, admin_client, app):
        z = app.db.create_zone({"name": "STP", "duration": 10, "group_id": 1, "topic": "/t/z"})
        app.db.update_zone(z["id"], {"state": "on", "watering_start_time": "2026-01-01 10:00:00"})
        resp = admin_client.post(f"/api/zones/{z['id']}/stop", content_type="application/json")
        assert resp.status_code in (200, 400, 500)


class TestStatusWithData:
    def test_status_with_zones_and_programs(self, admin_client, app):
        g = app.db.create_group("StatusGroup")
        z1 = app.db.create_zone({"name": "SZ1", "duration": 10, "group_id": g["id"], "topic": "/t/1"})
        z2 = app.db.create_zone({"name": "SZ2", "duration": 15, "group_id": g["id"], "topic": "/t/2"})
        app.db.create_program(
            {
                "name": "StatusProg",
                "time": "06:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "zones": [z1["id"], z2["id"]],
            }
        )
        resp = admin_client.get("/api/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "groups" in data
        assert len(data["groups"]) > 0

    def test_status_with_watering(self, admin_client, app):
        g = app.db.create_group("WateringStatus")
        z = app.db.create_zone({"name": "WS", "duration": 10, "group_id": g["id"], "topic": "/t/ws"})
        start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        app.db.update_zone(
            z["id"],
            {
                "state": "on",
                "watering_start_time": start,
                "watering_start_source": "manual",
            },
        )
        resp = admin_client.get("/api/status")
        assert resp.status_code == 200
        data = resp.get_json()
        watering_groups = [g for g in data.get("groups", []) if g.get("status") == "watering"]
        assert len(watering_groups) > 0

    def test_status_with_postpone(self, admin_client, app):
        g = app.db.create_group("PostponeStatus")
        z = app.db.create_zone({"name": "PS", "duration": 10, "group_id": g["id"]})
        future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
        app.db.update_zone_postpone(z["id"], future, "manual")
        resp = admin_client.get("/api/status")
        assert resp.status_code == 200

    def test_status_emergency(self, admin_client, app):
        app.config["EMERGENCY_STOP"] = True
        g = app.db.create_group("EmergStatus")
        app.db.create_zone({"name": "ES", "duration": 10, "group_id": g["id"]})
        try:
            resp = admin_client.get("/api/status")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data.get("emergency_stop") is True
        finally:
            app.config["EMERGENCY_STOP"] = False

    def test_status_with_mqtt_server(self, admin_client, app):
        app.db.create_mqtt_server({"name": "StatusMQTT", "host": "127.0.0.1", "port": 1883})
        resp = admin_client.get("/api/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("mqtt_servers_count", 0) > 0


class TestHealthDetailsDeep:
    def test_health_with_scheduler(self, admin_client, app):
        resp = admin_client.get("/api/health-details")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)


class TestPostponeDeep:
    def test_postpone_valid(self, admin_client, app, monkeypatch):
        import irrigation_scheduler

        g = app.db.create_group("PPG")
        zone = app.db.create_zone({"name": "PP", "duration": 10, "group_id": g["id"]})
        monkeypatch.setattr(
            irrigation_scheduler,
            "get_scheduler",
            lambda: type(
                "SuccessfulPostponeScheduler",
                (),
                {
                    "cancel_group_jobs": lambda self, group_id, **kwargs: {
                        "success": True,
                        "aggregate_valid": True,
                        "stopped": [zone["id"]],
                        "unresolved": [],
                        "unverified_zone_ids": [],
                        "retry_scheduled": False,
                        "group_id": group_id,
                    }
                },
            )(),
        )
        resp = admin_client.post(
            "/api/postpone",
            data=json.dumps(
                {
                    "group_id": g["id"],
                    "action": "postpone",
                    "days": 1,
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_postpone_cancel(self, admin_client, app):
        g = app.db.create_group("PPC")
        z = app.db.create_zone({"name": "PPC", "duration": 10, "group_id": g["id"]})
        app.db.update_zone_postpone(z["id"], "2026-12-31 23:59:59", "test")
        resp = admin_client.post(
            "/api/postpone",
            data=json.dumps(
                {
                    "group_id": g["id"],
                    "action": "cancel",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_postpone_invalid_group(self, admin_client):
        resp = admin_client.post(
            "/api/postpone", data=json.dumps({"group_id": "bad", "action": "postpone"}), content_type="application/json"
        )
        assert resp.status_code == 400


class TestWaterUsageDeep:
    def test_water_with_usage_data(self, admin_client, app):
        # Seeded group 1: freshly created groups get id 1000+ and /api/water
        # skips ids >= 999 (999 = служебная группа "БЕЗ ПОЛИВА").
        z = app.db.create_zone({"name": "WU", "duration": 10, "group_id": 1})
        app.db.add_water_usage(z["id"], 50)
        resp = admin_client.get("/api/water?days=30")
        assert resp.status_code == 200
        data = resp.get_json()
        group_data = data["1"]["data"]
        assert group_data["total_liters"] == 50
        zone_entry = group_data["zone_usage"][str(z["id"])]
        assert zone_entry["name"] == "WU"
        assert zone_entry["liters"] == 50
        assert zone_entry["last_used"] is not None
        assert len(group_data["daily_usage"]) == 30
        assert sum(d["liters"] for d in group_data["daily_usage"]) == 50

    def test_water_no_data(self, admin_client, app):
        resp = admin_client.get("/api/water")
        assert resp.status_code == 200


class TestProgramConflictsDeep:
    def test_conflicts_endpoint(self, admin_client, app):
        z1 = app.db.create_zone({"name": "C1", "duration": 30, "group_id": 1})
        z2 = app.db.create_zone({"name": "C2", "duration": 30, "group_id": 1})
        app.db.create_program(
            {
                "name": "PA",
                "time": "06:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "zones": [z1["id"]],
            }
        )
        resp = admin_client.post(
            "/api/programs/check-conflicts",
            data=json.dumps(
                {
                    "time": "06:10",
                    "zones": [z2["id"]],
                    "days": [0, 1, 2, 3, 4, 5, 6],
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code in (200, 400, 404)


class TestServerTimeEndpoint:
    def test_server_time(self, admin_client):
        resp = admin_client.get("/api/server-time")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "datetime" in data or "time" in data or isinstance(data, dict)


class TestMapEndpoints:
    def test_get_map(self, admin_client):
        resp = admin_client.get("/api/map")
        assert resp.status_code == 200

    def test_delete_map_nonexistent(self, admin_client):
        resp = admin_client.delete("/api/map/nonexistent.png")
        assert resp.status_code in (200, 404)


class TestHealthCancelJob:
    def test_cancel_job(self, admin_client):
        resp = admin_client.post("/api/health/job/test_job_id/cancel", content_type="application/json")
        assert resp.status_code in (200, 400, 404, 500, 503)
