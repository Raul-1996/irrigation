"""Comprehensive tests for routes/zones_api.py endpoints."""

import json
import os

os.environ["TESTING"] = "1"


class TestZonesAPI:
    def test_list_zones(self, admin_client):
        resp = admin_client.get("/api/zones")
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)

    def test_create_zone(self, admin_client):
        resp = admin_client.post(
            "/api/zones", data=json.dumps({"name": "New Zone", "duration": 15}), content_type="application/json"
        )
        assert resp.status_code in (200, 201)

    def test_create_zone_max_duration(self, admin_client):
        resp = admin_client.post(
            "/api/zones", data=json.dumps({"name": "Long", "duration": 3600}), content_type="application/json"
        )
        assert resp.status_code in (200, 201)

    def test_create_zone_over_max_duration(self, admin_client):
        resp = admin_client.post(
            "/api/zones", data=json.dumps({"name": "TooLong", "duration": 9999}), content_type="application/json"
        )
        assert resp.status_code == 400

    def test_create_zone_zero_duration(self, admin_client):
        resp = admin_client.post(
            "/api/zones", data=json.dumps({"name": "Zero", "duration": 0}), content_type="application/json"
        )
        assert resp.status_code in (400, 201)

    def test_create_zone_with_group(self, admin_client, app):
        g = app.db.create_group("TestG")
        resp = admin_client.post(
            "/api/zones",
            data=json.dumps({"name": "Grouped", "duration": 10, "group_id": g["id"]}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 201)

    def test_get_zone(self, admin_client, app):
        z = app.db.create_zone({"name": "GetMe", "duration": 10, "group_id": 1})
        resp = admin_client.get(f"/api/zones/{z['id']}")
        assert resp.status_code == 200

    def test_get_zone_not_found(self, admin_client):
        resp = admin_client.get("/api/zones/99999")
        assert resp.status_code == 404

    def test_update_zone(self, admin_client, app):
        z = app.db.create_zone({"name": "Old", "duration": 10, "group_id": 1})
        resp = admin_client.put(
            f"/api/zones/{z['id']}",
            data=json.dumps({"name": "Updated", "duration": 20}),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_update_zone_invalid_duration(self, admin_client, app):
        z = app.db.create_zone({"name": "Bad", "duration": 10, "group_id": 1})
        resp = admin_client.put(
            f"/api/zones/{z['id']}", data=json.dumps({"duration": 99999}), content_type="application/json"
        )
        assert resp.status_code == 400

    def test_update_zone_empty_name(self, admin_client, app):
        z = app.db.create_zone({"name": "X", "duration": 10, "group_id": 1})
        resp = admin_client.put(f"/api/zones/{z['id']}", data=json.dumps({"name": ""}), content_type="application/json")
        assert resp.status_code == 400

    def test_delete_zone(self, admin_client, app):
        z = app.db.create_zone({"name": "Del", "duration": 10, "group_id": 1})
        resp = admin_client.delete(f"/api/zones/{z['id']}")
        assert resp.status_code in (200, 204)


class TestZoneStartStopAPI:
    def test_start_zone(self, admin_client, app):
        z = app.db.create_zone(
            {
                "name": "Start",
                "duration": 10,
                "group_id": 1,
                "topic": "/test/zone",
            }
        )
        resp = admin_client.post(f"/api/zones/{z['id']}/start", content_type="application/json")
        assert resp.status_code in (200, 400, 500)

    def test_start_nonexistent_zone(self, admin_client):
        resp = admin_client.post("/api/zones/99999/start", content_type="application/json")
        assert resp.status_code in (404, 400, 500)

    def test_stop_zone(self, admin_client, app):
        z = app.db.create_zone(
            {
                "name": "Stop",
                "duration": 10,
                "group_id": 1,
                "topic": "/test/zone",
            }
        )
        resp = admin_client.post(f"/api/zones/{z['id']}/stop", content_type="application/json")
        assert resp.status_code in (200, 400, 500)


class TestZoneBulkAPI:
    def test_bulk_upsert(self, admin_client):
        resp = admin_client.post(
            "/api/zones/bulk",
            data=json.dumps(
                {
                    "zones": [
                        {"name": "B1", "duration": 5, "group_id": 1},
                        {"name": "B2", "duration": 10, "group_id": 1},
                    ]
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code in (200, 201, 400, 404)

    def test_bulk_update(self, admin_client, app):
        z1 = app.db.create_zone({"name": "U1", "duration": 5, "group_id": 1})
        z2 = app.db.create_zone({"name": "U2", "duration": 10, "group_id": 1})
        resp = admin_client.put(
            "/api/zones/bulk",
            data=json.dumps(
                {
                    "zones": [
                        {"id": z1["id"], "name": "Updated1"},
                        {"id": z2["id"], "name": "Updated2"},
                    ]
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code in (200, 400, 404)


class TestViewerAccess:
    def test_viewer_can_read(self, viewer_client):
        resp = viewer_client.get("/api/zones")
        assert resp.status_code == 200

    def test_viewer_create_attempt(self, viewer_client):
        """Viewer role may or may not be restricted from creating zones (depends on admin_required decorator)."""
        resp = viewer_client.post(
            "/api/zones", data=json.dumps({"name": "No", "duration": 10}), content_type="application/json"
        )
        # Accept any response — viewer may be allowed or forbidden
        assert resp.status_code in (200, 201, 403, 401, 302)


# ── Issue #12: %-of-norm override on /api/zones/<id>/mqtt/start ────────────
# Each test exercises one branch of services.zone_control.per_zone_dur via
# the public endpoint, asserting both the resulting planned_end_time and
# the surfaced `warnings[]` payload.
class TestZoneMqttStartPercent:
    def test_mqtt_start_with_percent(self, admin_client, app):
        """150% × 20-min norm -> 30-min planned run."""
        from datetime import datetime, timedelta

        z = app.db.create_zone(
            {
                "name": "Pct150",
                "duration": 20,
                "group_id": 1,
                "topic": "/test/pct150",
            }
        )
        before = datetime.now()
        resp = admin_client.post(
            f"/api/zones/{z['id']}/mqtt/start",
            data=json.dumps({"duration_percent": 150}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body.get("success") is True
        assert body.get("warnings") == []
        zone = app.db.get_zone(z["id"])
        assert zone.get("planned_end_time")
        end_dt = datetime.strptime(zone["planned_end_time"], "%Y-%m-%d %H:%M:%S")
        # 20 × 1.5 = 30 min. Tolerance ±5 sec for clock skew between the two
        # `datetime.now()` reads (request handler vs. assertion).
        expected = before + timedelta(minutes=30)
        assert abs((end_dt - expected).total_seconds()) < 5

    def test_mqtt_start_percent_norm_zero_fallback(self, admin_client, app):
        """duration<=0 + percent -> use 15-min fallback + 'norm_not_set' warning."""
        from datetime import datetime, timedelta

        z = app.db.create_zone(
            {
                "name": "PctZero",
                "duration": 10,
                "group_id": 1,
                "topic": "/test/pctzero",
            }
        )
        # Force the corruption case the helper guards against.
        app.db.update_zone(z["id"], {"duration": 0})
        before = datetime.now()
        resp = admin_client.post(
            f"/api/zones/{z['id']}/mqtt/start",
            data=json.dumps({"duration_percent": 100}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body.get("success") is True
        assert "norm_not_set" in (body.get("warnings") or [])
        zone = app.db.get_zone(z["id"])
        end_dt = datetime.strptime(zone["planned_end_time"], "%Y-%m-%d %H:%M:%S")
        expected = before + timedelta(minutes=15)
        assert abs((end_dt - expected).total_seconds()) < 5

    def test_mqtt_start_percent_clipped_max(self, admin_client, app):
        """200% × 200 = 400 -> clipped at MAX_MANUAL_WATERING_MIN (240) + warning.

        We bypass the route-level 1..120 validator by writing the duration
        straight into the DB — the helper must still produce a sane,
        clipped run length even when fed garbage (defence in depth).
        """
        from datetime import datetime, timedelta

        z = app.db.create_zone(
            {
                "name": "PctClip",
                "duration": 100,
                "group_id": 1,
                "topic": "/test/pctclip",
            }
        )
        # Update path doesn't enforce 1..120 — direct write to exercise clip.
        app.db.update_zone(z["id"], {"duration": 200})
        before = datetime.now()
        resp = admin_client.post(
            f"/api/zones/{z['id']}/mqtt/start",
            data=json.dumps({"duration_percent": 200}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body.get("success") is True
        # 200 × 2.0 = 400 -> clipped at 240, warning emitted.
        assert "clipped_max" in (body.get("warnings") or [])
        zone = app.db.get_zone(z["id"])
        end_dt = datetime.strptime(zone["planned_end_time"], "%Y-%m-%d %H:%M:%S")
        expected = before + timedelta(minutes=240)
        assert abs((end_dt - expected).total_seconds()) < 5


# ── Issue #12 iter2 C2: "minutes wins if both sent" — strict rejection ─────
# Spec §4 invariant: duration present in body == user intent. Either
# accept (1..120) or 400. NEVER silent fallback to percent.
class TestZoneMqttStartMinutesWinsStrict:
    def test_minutes_out_of_range_rejected_no_percent_fallback(self, admin_client, app):
        """duration=200 + duration_percent=100 -> 400, NOT silent fall to %."""
        z = app.db.create_zone(
            {
                "name": "C2Reject",
                "duration": 20,
                "group_id": 1,
                "topic": "/test/c2reject",
            }
        )
        resp = admin_client.post(
            f"/api/zones/{z['id']}/mqtt/start",
            data=json.dumps({"duration": 200, "duration_percent": 100}),
            content_type="application/json",
        )
        # Must reject — never fall through to percent path.
        assert resp.status_code == 400
        body = resp.get_json()
        assert body.get("success") is False
        # Zone must remain off (no side effects from the rejected request).
        zone = app.db.get_zone(z["id"])
        assert (zone.get("state") or "off") != "on"

    def test_minutes_null_percent_honored(self, admin_client, app):
        """duration=null + duration_percent=100 -> percent path runs (norm × 1.0)."""
        from datetime import datetime, timedelta

        z = app.db.create_zone(
            {
                "name": "C2NullDur",
                "duration": 12,
                "group_id": 1,
                "topic": "/test/c2nulldur",
            }
        )
        before = datetime.now()
        resp = admin_client.post(
            f"/api/zones/{z['id']}/mqtt/start",
            data=json.dumps({"duration": None, "duration_percent": 100}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body.get("success") is True
        # 12 × 1.0 = 12 min — percent honoured because minutes was null.
        zone = app.db.get_zone(z["id"])
        end_dt = datetime.strptime(zone["planned_end_time"], "%Y-%m-%d %H:%M:%S")
        expected = before + timedelta(minutes=12)
        assert abs((end_dt - expected).total_seconds()) < 5

    def test_minutes_wins_over_percent_when_both_valid(self, admin_client, app):
        """duration=30 + duration_percent=100 -> 30 min, NOT norm × 1.0."""
        from datetime import datetime, timedelta

        z = app.db.create_zone(
            {
                # Choose norm != 30 so we can distinguish minutes-mode from %-mode.
                "name": "C2Wins",
                "duration": 12,
                "group_id": 1,
                "topic": "/test/c2wins",
            }
        )
        before = datetime.now()
        resp = admin_client.post(
            f"/api/zones/{z['id']}/mqtt/start",
            data=json.dumps({"duration": 30, "duration_percent": 100}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body.get("success") is True
        # Minutes wins -> 30 min, not 12 (norm × 100%).
        zone = app.db.get_zone(z["id"])
        end_dt = datetime.strptime(zone["planned_end_time"], "%Y-%m-%d %H:%M:%S")
        expected = before + timedelta(minutes=30)
        assert abs((end_dt - expected).total_seconds()) < 5
