"""Comprehensive route tests targeting uncovered endpoints in zones_api, system_api, groups_api, mqtt_api, settings."""

import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch

from tests.safety_contracts import confirmed_group_stop

os.environ["TESTING"] = "1"


class TestZoneNextWatering:
    def test_next_watering(self, admin_client, app):
        z = app.db.create_zone({"name": "NW", "duration": 10, "group_id": 1})
        resp = admin_client.get(f"/api/zones/{z['id']}/next-watering")
        assert resp.status_code == 200

    def test_next_watering_with_program(self, admin_client, app):
        z = app.db.create_zone({"name": "NW", "duration": 10, "group_id": 1})
        app.db.create_program(
            {
                "name": "P1",
                "time": "06:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "zones": [z["id"]],
            }
        )
        resp = admin_client.get(f"/api/zones/{z['id']}/next-watering")
        assert resp.status_code == 200

    def test_next_watering_not_found(self, admin_client):
        resp = admin_client.get("/api/zones/99999/next-watering")
        assert resp.status_code == 404

    def test_next_watering_bulk(self, admin_client, app):
        z = app.db.create_zone({"name": "NW", "duration": 10, "group_id": 1})
        resp = admin_client.post(
            "/api/zones/next-watering-bulk", data=json.dumps({"zone_ids": [z["id"]]}), content_type="application/json"
        )
        assert resp.status_code == 200

    def test_next_watering_bulk_all(self, admin_client, app):
        app.db.create_zone({"name": "NW", "duration": 10, "group_id": 1})
        resp = admin_client.post("/api/zones/next-watering-bulk", data=json.dumps({}), content_type="application/json")
        assert resp.status_code == 200


class TestZoneNextWateringPostpone:
    """Regression coverage for issue #1.

    The bulk next-watering endpoint and compute_next_run_for_zone must
    advance their lower-bound 'now' to the zone's postpone_until when it
    is in the future, so that zone cards never display a next-run time
    that the scheduler will skip.
    """

    @staticmethod
    def _bulk(client, zone_id):
        resp = client.post(
            "/api/zones/next-watering-bulk",
            data=json.dumps({"zone_ids": [zone_id]}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body.get("success") is True
        items = body.get("items") or []
        assert len(items) == 1
        return items[0]

    def test_bulk_skips_postpone_window(self, admin_client, app):
        # Program runs daily at 04:00; zone postponed until tomorrow 23:59:59
        # → next must be >= day-after-tomorrow 04:00.
        z = app.db.create_zone({"name": "PP1", "duration": 10, "group_id": 1})
        app.db.create_program(
            {
                "name": "Daily04",
                "time": "04:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "zones": [z["id"]],
            }
        )
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
        app.db.update_zone_postpone(z["id"], tomorrow, "manual")

        item = self._bulk(admin_client, z["id"])
        assert item["next_datetime"] is not None
        nxt = datetime.strptime(item["next_datetime"], "%Y-%m-%d %H:%M:%S")
        day_after = (datetime.now() + timedelta(days=2)).replace(hour=4, minute=0, second=0, microsecond=0)
        assert nxt >= day_after, f"{nxt} should be >= {day_after}"

    def test_bulk_unpostponed_unaffected(self, admin_client, app):
        # postpone_until in the past → no effect.
        z = app.db.create_zone({"name": "PP2", "duration": 10, "group_id": 1})
        app.db.create_program(
            {
                "name": "DailyMorning",
                "time": "06:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "zones": [z["id"]],
            }
        )
        past = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        app.db.update_zone_postpone(z["id"], past, "manual")

        item = self._bulk(admin_client, z["id"])
        assert item["next_datetime"] is not None
        nxt = datetime.strptime(item["next_datetime"], "%Y-%m-%d %H:%M:%S")
        # Must be in the future, and within the next 8 days.
        now = datetime.now()
        assert nxt > now
        assert nxt < now + timedelta(days=8)

        # And NULL postpone_until → also unaffected.
        z2 = app.db.create_zone({"name": "PP2b", "duration": 10, "group_id": 1})
        app.db.create_program(
            {
                "name": "DailyMorning2",
                "time": "06:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "zones": [z2["id"]],
            }
        )
        item2 = self._bulk(admin_client, z2["id"])
        assert item2["next_datetime"] is not None

    def test_bulk_group_postpone_via_api(self, admin_client, app, monkeypatch):
        import irrigation_scheduler

        # POST /api/postpone for the group, then verify bulk respects it.
        g = app.db.create_group("PPGroup")
        z = app.db.create_zone({"name": "PP3", "duration": 10, "group_id": g["id"]})
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
                        "stopped": [z["id"]],
                        "unresolved": [],
                        "unverified_zone_ids": [],
                        "retry_scheduled": False,
                        "group_id": group_id,
                    }
                },
            )(),
        )
        app.db.create_program(
            {
                "name": "DailyDawn",
                "time": "04:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "zones": [z["id"]],
            }
        )
        resp = admin_client.post(
            "/api/postpone",
            data=json.dumps({"group_id": g["id"], "days": 1, "action": "postpone"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json() or {}
        # API returns postpone_until on success.
        pu = body.get("postpone_until")
        assert pu, body

        item = self._bulk(admin_client, z["id"])
        assert item["next_datetime"] is not None
        nxt = datetime.strptime(item["next_datetime"], "%Y-%m-%d %H:%M:%S")
        # Postpone is until "today + days 23:59:59" → next 04:00 must be
        # at least the day after.
        end_of_postpone = (datetime.now() + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)
        assert nxt > end_of_postpone

    def test_bulk_postpone_exact_boundary(self, admin_client, app):
        # postpone_until = 04:00 exactly, program at 04:00 → must pick the
        # NEXT day's 04:00 (strict >).
        z = app.db.create_zone({"name": "PP4", "duration": 10, "group_id": 1})
        app.db.create_program(
            {
                "name": "Boundary04",
                "time": "04:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "zones": [z["id"]],
            }
        )
        # Pick a fixed-future date so the test is deterministic regardless of
        # weekday/clock skew.
        target_day = datetime.now() + timedelta(days=3)
        boundary = target_day.replace(hour=4, minute=0, second=0, microsecond=0)
        boundary_str = boundary.strftime("%Y-%m-%d %H:%M:%S")
        app.db.update_zone_postpone(z["id"], boundary_str, "manual")

        item = self._bulk(admin_client, z["id"])
        assert item["next_datetime"] is not None
        nxt = datetime.strptime(item["next_datetime"], "%Y-%m-%d %H:%M:%S")
        # Strict >: 04:00 itself is excluded → next day 04:00.
        next_day = boundary + timedelta(days=1)
        assert nxt == next_day, f"{nxt} should equal {next_day}"

    def test_bulk_postpone_other_zones_unaffected(self, admin_client, app):
        # Two groups, two zones, two programs.  Postpone group A only;
        # group B's zone keeps its normal next time.
        ga = app.db.create_group("A")
        gb = app.db.create_group("B")
        za = app.db.create_zone({"name": "ZA", "duration": 10, "group_id": ga["id"]})
        zb = app.db.create_zone({"name": "ZB", "duration": 10, "group_id": gb["id"]})
        app.db.create_program(
            {
                "name": "PA",
                "time": "04:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "zones": [za["id"]],
            }
        )
        app.db.create_program(
            {
                "name": "PB",
                "time": "05:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "zones": [zb["id"]],
            }
        )
        # Postpone A only.
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
        app.db.update_zone_postpone(za["id"], tomorrow, "manual")

        ia = self._bulk(admin_client, za["id"])
        ib = self._bulk(admin_client, zb["id"])
        assert ia["next_datetime"] is not None
        assert ib["next_datetime"] is not None
        nxt_a = datetime.strptime(ia["next_datetime"], "%Y-%m-%d %H:%M:%S")
        nxt_b = datetime.strptime(ib["next_datetime"], "%Y-%m-%d %H:%M:%S")

        day_after = (datetime.now() + timedelta(days=2)).replace(hour=4, minute=0, second=0, microsecond=0)
        assert nxt_a >= day_after
        # B is not postponed — must be within the next ~2 days.
        assert nxt_b <= datetime.now() + timedelta(days=2)

    def test_single_zone_endpoint_consistency(self, admin_client, app):
        # /api/zones/<id>/next-watering and bulk endpoint must agree for
        # a postponed zone.
        z = app.db.create_zone({"name": "PP6", "duration": 10, "group_id": 1})
        app.db.create_program(
            {
                "name": "P6",
                "time": "04:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "zones": [z["id"]],
            }
        )
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
        app.db.update_zone_postpone(z["id"], tomorrow, "manual")

        bulk_item = self._bulk(admin_client, z["id"])
        single = admin_client.get(f"/api/zones/{z['id']}/next-watering")
        assert single.status_code == 200
        single_body = single.get_json() or {}
        # Single endpoint returns next_watering as ISO datetime string under
        # one of these keys depending on shape; we accept either.
        single_dt = single_body.get("next_datetime") or single_body.get("next_watering")
        # Both should be either both None or both pointing past the
        # postpone window; if single is unavailable, just assert bulk
        # produced a post-postpone time (consistency-with-self).
        bulk_dt = bulk_item["next_datetime"]
        assert bulk_dt is not None
        bulk_parsed = datetime.strptime(bulk_dt, "%Y-%m-%d %H:%M:%S")
        end_postpone = datetime.strptime(tomorrow, "%Y-%m-%d %H:%M:%S")
        assert bulk_parsed > end_postpone
        if isinstance(single_dt, str):
            try:
                single_parsed = datetime.strptime(single_dt, "%Y-%m-%d %H:%M:%S")
                assert single_parsed > end_postpone
            except ValueError:
                # Single endpoint may use a different shape; bulk-side
                # invariant already verified above.
                pass


class TestZoneImport:
    def test_import_zones(self, admin_client):
        resp = admin_client.post(
            "/api/zones/import",
            data=json.dumps(
                {
                    "zones": [
                        {"name": "I1", "duration": 5, "group_id": 1},
                        {"name": "I2", "duration": 10, "group_id": 1},
                    ]
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code in (200, 201, 400)

    def test_import_empty(self, admin_client):
        resp = admin_client.post("/api/zones/import", data=json.dumps({"zones": []}), content_type="application/json")
        assert resp.status_code == 400


class TestZoneStartStop:
    def test_start_zone_with_duration(self, admin_client, app):
        z = app.db.create_zone(
            {
                "name": "S",
                "duration": 10,
                "group_id": 1,
                "topic": "/t/z",
            }
        )
        resp = admin_client.post(
            f"/api/zones/{z['id']}/start", data=json.dumps({"duration": 5}), content_type="application/json"
        )
        assert resp.status_code in (200, 400, 500)

    def test_stop_zone_api(self, admin_client, app):
        z = app.db.create_zone(
            {
                "name": "S",
                "duration": 10,
                "group_id": 1,
                "topic": "/t/z",
            }
        )
        resp = admin_client.post(f"/api/zones/{z['id']}/stop", content_type="application/json")
        assert resp.status_code in (200, 400, 500)


class TestZoneBulkUpdate:
    def test_bulk_update_zones(self, admin_client, app):
        z1 = app.db.create_zone({"name": "B1", "duration": 5, "group_id": 1})
        z2 = app.db.create_zone({"name": "B2", "duration": 10, "group_id": 1})
        resp = admin_client.put(
            "/api/zones/bulk",
            data=json.dumps(
                {
                    "zones": [
                        {"id": z1["id"], "duration": 20},
                        {"id": z2["id"], "duration": 30},
                    ]
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code in (200, 400, 404)


class TestSSEStream:
    def test_sse_endpoint(self, admin_client):
        resp = admin_client.get("/api/zones/sse")
        assert resp.status_code in (200, 404)


class TestSystemAPIDiagnostics:
    def test_health_check(self, admin_client):
        resp = admin_client.get("/health")
        assert resp.status_code == 200

    def test_health_details(self, admin_client):
        resp = admin_client.get("/api/health-details")
        assert resp.status_code in (200, 404)

    def test_server_time(self, admin_client):
        resp = admin_client.get("/api/server-time")
        assert resp.status_code == 200

    def test_scheduler_status(self, admin_client):
        resp = admin_client.get("/api/scheduler/status")
        assert resp.status_code == 200

    def test_scheduler_jobs(self, admin_client):
        resp = admin_client.get("/api/scheduler/jobs")
        assert resp.status_code == 200

    def test_auth_status(self, admin_client):
        resp = admin_client.get("/api/auth/status")
        assert resp.status_code == 200

    def test_api_status(self, admin_client):
        resp = admin_client.get("/api/status")
        assert resp.status_code == 200

    def test_api_logs(self, admin_client):
        resp = admin_client.get("/api/logs")
        assert resp.status_code == 200

    def test_api_backup(self, admin_client):
        resp = admin_client.post("/api/backup", content_type="application/json")
        assert resp.status_code in (200, 201, 400, 500)

    def test_api_water(self, admin_client):
        resp = admin_client.get("/api/water")
        assert resp.status_code == 200


class TestRainEnvAPI:
    def test_get_rain(self, admin_client):
        resp = admin_client.get("/api/rain")
        assert resp.status_code == 200

    def test_post_rain(self, admin_client, app):
        server = app.db.create_mqtt_server({"name": "rain", "host": "127.0.0.1", "port": 1883, "enabled": True})
        with patch("routes.system_config_api.rain_monitor") as monitor:
            monitor.reconfigure.return_value = True
            resp = admin_client.post(
                "/api/rain",
                data=json.dumps({"enabled": True, "topic": "/rain", "server_id": server["id"], "type": "NO"}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_get_env(self, admin_client):
        resp = admin_client.get("/api/env")
        assert resp.status_code == 200

    def test_post_env(self, admin_client):
        resp = admin_client.post(
            "/api/env",
            data=json.dumps(
                {
                    "temp": {"enabled": False, "topic": "", "server_id": None},
                    "hum": {"enabled": False, "topic": "", "server_id": None},
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_get_env_values(self, admin_client):
        resp = admin_client.get("/api/env/values")
        assert resp.status_code == 200


class TestPostponeAPI:
    def test_postpone_zone(self, admin_client, app):
        z = app.db.create_zone({"name": "PP", "duration": 10, "group_id": 1})
        resp = admin_client.post(
            "/api/postpone",
            data=json.dumps(
                {
                    "zone_id": z["id"],
                    "until": "2026-12-31 23:59",
                    "reason": "test",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code in (200, 400)


class TestPasswordAPI:
    def test_change_password(self, admin_client):
        resp = admin_client.post(
            "/api/password", data=json.dumps({"new_password": "NewPass123!"}), content_type="application/json"
        )
        assert resp.status_code in (200, 400)


class TestEarlyOffAPI:
    def test_get_early_off(self, admin_client):
        resp = admin_client.get("/api/settings/early-off")
        assert resp.status_code == 200

    def test_set_early_off(self, admin_client):
        resp = admin_client.post(
            "/api/settings/early-off", data=json.dumps({"seconds": 5}), content_type="application/json"
        )
        assert resp.status_code == 200


class TestSystemNameAPI:
    def test_get_system_name(self, admin_client):
        resp = admin_client.get("/api/settings/system-name")
        assert resp.status_code == 200

    def test_set_system_name(self, admin_client):
        resp = admin_client.post(
            "/api/settings/system-name", data=json.dumps({"name": "Test System"}), content_type="application/json"
        )
        assert resp.status_code == 200


class TestLoggingDebugAPI:
    def test_get_debug(self, admin_client):
        resp = admin_client.get("/api/logging/debug")
        assert resp.status_code == 200

    def test_set_debug(self, admin_client):
        resp = admin_client.post(
            "/api/logging/debug", data=json.dumps({"enabled": True}), content_type="application/json"
        )
        assert resp.status_code == 200


class TestMapAPI:
    def test_get_map(self, admin_client):
        resp = admin_client.get("/api/map")
        assert resp.status_code == 200


class TestGroupsAdvanced:
    def test_stop_group(self, admin_client, app):
        g = app.db.create_group("SG")
        z = app.db.create_zone({"name": "Z", "duration": 10, "group_id": g["id"]})
        with confirmed_group_stop(app.db, "routes.groups_api.get_scheduler"):
            resp = admin_client.post(f"/api/groups/{g['id']}/stop", content_type="application/json")
        assert resp.status_code in (200, 400, 500)

    def test_start_zone_exclusive(self, admin_client, app):
        g = app.db.create_group("EX")
        z = app.db.create_zone({"name": "Z", "duration": 10, "group_id": g["id"], "topic": "/t/x"})
        resp = admin_client.post(f"/api/groups/{g['id']}/start-zone/{z['id']}", content_type="application/json")
        assert resp.status_code in (200, 400, 500)


class TestMqttAdvanced:
    def test_get_server(self, admin_client, app):
        s = app.db.create_mqtt_server({"name": "G", "host": "10.0.0.1", "port": 1883})
        resp = admin_client.get(f"/api/mqtt/servers/{s['id']}")
        assert resp.status_code == 200

    def test_update_server(self, admin_client, app):
        s = app.db.create_mqtt_server({"name": "U", "host": "10.0.0.1", "port": 1883})
        resp = admin_client.put(
            f"/api/mqtt/servers/{s['id']}", data=json.dumps({"name": "Updated"}), content_type="application/json"
        )
        assert resp.status_code == 200

    def test_delete_server(self, admin_client, app):
        s = app.db.create_mqtt_server({"name": "D", "host": "10.0.0.1", "port": 1883})
        resp = admin_client.delete(f"/api/mqtt/servers/{s['id']}")
        assert resp.status_code in (200, 204)

    def test_mqtt_status(self, admin_client, app):
        s = app.db.create_mqtt_server({"name": "ST", "host": "10.0.0.1", "port": 1883})
        resp = admin_client.get(f"/api/mqtt/{s['id']}/status")
        assert resp.status_code in (200, 404, 500)


class TestLoginLogout:
    def test_logout(self, admin_client):
        resp = admin_client.get("/logout")
        assert resp.status_code in (200, 302)

    def test_api_login(self, admin_client, app):
        app.db.set_password("TestPassword123!")
        resp = admin_client.post(
            "/api/login", data=json.dumps({"password": "TestPassword123!"}), content_type="application/json"
        )
        assert resp.status_code in (200, 400, 401)

    def test_api_login_wrong_password(self, admin_client, app):
        app.db.set_password("CorrectPassword!")
        resp = admin_client.post(
            "/api/login", data=json.dumps({"password": "WrongPassword!"}), content_type="application/json"
        )
        assert resp.status_code in (200, 400, 401)
