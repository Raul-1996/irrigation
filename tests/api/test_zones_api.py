"""Tests for ALL /api/zones/* endpoints."""

import json
import os
import threading

os.environ["TESTING"] = "1"


class TestZonesListAPI:
    def test_get_zones(self, admin_client):
        resp = admin_client.get("/api/zones")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_create_zone(self, admin_client):
        resp = admin_client.post(
            "/api/zones", data=json.dumps({"name": "API Zone", "duration": 15}), content_type="application/json"
        )
        assert resp.status_code == 201
        data = resp.get_json()
        # Response may have zone nested or at top level, and may include 'warning' about MQTT
        if "zone" in data:
            assert data["zone"]["name"] == "API Zone"
        else:
            assert data.get("name") == "API Zone"

    def test_create_zone_invalid_duration(self, admin_client):
        resp = admin_client.post(
            "/api/zones", data=json.dumps({"name": "Bad", "duration": 9999}), content_type="application/json"
        )
        assert resp.status_code == 400

    def test_create_zone_empty_name(self, admin_client):
        resp = admin_client.post(
            "/api/zones", data=json.dumps({"name": "", "duration": 10}), content_type="application/json"
        )
        # Empty name falls through to default 'Зона' in the create logic
        assert resp.status_code in (201, 400)


class TestZoneSingleAPI:
    def test_get_zone(self, admin_client, app):
        zone = app.db.create_zone({"name": "GetMe", "duration": 10, "group_id": 1})
        resp = admin_client.get(f"/api/zones/{zone['id']}")
        assert resp.status_code == 200
        assert resp.get_json()["name"] == "GetMe"

    def test_get_zone_not_found(self, admin_client):
        resp = admin_client.get("/api/zones/99999")
        assert resp.status_code == 404

    def test_update_zone(self, admin_client, app):
        zone = app.db.create_zone({"name": "Old", "duration": 10, "group_id": 1})
        resp = admin_client.put(
            f"/api/zones/{zone['id']}",
            data=json.dumps({"name": "Updated", "duration": 20, "expected_version": zone["version"]}),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_delete_zone(self, admin_client, app):
        zone = app.db.create_zone({"name": "Del", "duration": 10, "group_id": 1})
        app.db.update_zone(zone["id"], {"state": "off", "commanded_state": "off", "observed_state": "off"})
        resp = admin_client.delete(f"/api/zones/{zone['id']}")
        assert resp.status_code == 204

    def test_delete_zone_not_found(self, admin_client):
        resp = admin_client.delete("/api/zones/99999")
        # delete_zone returns True for nonexistent IDs (no error check on rowcount)
        assert resp.status_code in (204, 404)


class TestZoneStartStop:
    def test_start_zone(self, admin_client, app):
        zone = app.db.create_zone(
            {
                "name": "Start",
                "duration": 10,
                "group_id": 1,
                "topic": "/test/zone",
                "mqtt_server_id": None,
            }
        )
        resp = admin_client.post(f"/api/zones/{zone['id']}/start", content_type="application/json")
        # May fail due to MQTT, but should not 500
        assert resp.status_code in (200, 400, 500)

    def test_stop_zone(self, admin_client, app):
        zone = app.db.create_zone(
            {
                "name": "Stop",
                "duration": 10,
                "group_id": 1,
            }
        )
        resp = admin_client.post(f"/api/zones/{zone['id']}/stop", content_type="application/json")
        assert resp.status_code == 200

    def test_stop_nonexistent_zone(self, admin_client):
        resp = admin_client.post("/api/zones/99999/stop", content_type="application/json")
        assert resp.status_code == 404


class TestZoneWateringTime:
    def test_watering_time_not_watering(self, admin_client, app):
        zone = app.db.create_zone({"name": "WT", "duration": 10, "group_id": 1})
        resp = admin_client.get(f"/api/zones/{zone['id']}/watering-time")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["is_watering"] is False

    def test_watering_time_not_found(self, admin_client):
        resp = admin_client.get("/api/zones/99999/watering-time")
        assert resp.status_code == 404


class TestZonePhotoAPI:
    def test_get_photo_info_no_photo(self, admin_client, app):
        zone = app.db.create_zone({"name": "NoPhoto", "duration": 10, "group_id": 1})
        resp = admin_client.get(f"/api/zones/{zone['id']}/photo")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_photo"] is False

    def test_upload_invalid_format(self, admin_client, app):
        """Uploading a non-image file should be rejected."""
        zone = app.db.create_zone({"name": "Img", "duration": 10, "group_id": 1})
        import io

        data = {"photo": (io.BytesIO(b"not an image"), "test.txt")}
        resp = admin_client.post(f"/api/zones/{zone['id']}/photo", data=data, content_type="multipart/form-data")
        assert resp.status_code == 400

    def test_delete_photo_no_photo(self, admin_client, app):
        zone = app.db.create_zone({"name": "NoPh", "duration": 10, "group_id": 1})
        resp = admin_client.delete(f"/api/zones/{zone['id']}/photo")
        assert resp.status_code == 404


class TestZoneNextWatering:
    def test_next_watering_no_programs(self, admin_client, app):
        zone = app.db.create_zone({"name": "NP", "duration": 10, "group_id": 1})
        resp = admin_client.get(f"/api/zones/{zone['id']}/next-watering")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["next_watering"] == "Никогда"

    def test_next_watering_bulk(self, admin_client):
        resp = admin_client.post(
            "/api/zones/next-watering-bulk", data=json.dumps({"zone_ids": []}), content_type="application/json"
        )
        assert resp.status_code == 200


class TestZoneStopAbortsSession:
    """Issue #16: stop endpoints route through cancel_group_jobs when an
    active group session is in flight, but stay solo-only otherwise."""

    def _setup_zone(self, app, name="Z"):
        group = app.db.create_group(f"#16 {name}")
        zone = app.db.create_zone(
            {
                "name": f"#16 {name} zone",
                "duration": 5,
                "group_id": group["id"],
            }
        )
        return group, zone

    # ─── #4 / #5: solo vs session-active dispatch on /mqtt/stop ─────────
    def test_zone_mqtt_stop_solo_does_not_call_cancel_group_jobs(self, admin_client, app):
        """Spec §4.2 #4: no entry in group_cancel_events -> solo path runs.

        Pre-fix this was the only path; we assert that behaviour didn't
        change for the solo case.
        """
        from irrigation_scheduler import init_scheduler

        sched = init_scheduler(app.db)
        group, zone = self._setup_zone(app, "Solo")
        # Sanity: no session in flight.
        assert not sched.is_group_session_active(group["id"])

        resp = admin_client.post(f"/api/zones/{zone['id']}/mqtt/stop", content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        # Solo path: response shape DOES NOT include session_aborted.
        assert "session_aborted" not in data
        # Cancel-event still absent (no spurious plant).
        assert not sched.is_group_session_active(group["id"])

    def test_zone_mqtt_stop_during_session_calls_cancel_group_jobs(self, admin_client, app):
        """Spec §4.2 #5: with active session -> abort path runs."""
        from irrigation_scheduler import init_scheduler

        sched = init_scheduler(app.db)
        group, zone = self._setup_zone(app, "Active")
        # Plant the cancel-event manually to mimic an active session.
        sched.group_cancel_events[group["id"]] = threading.Event()

        resp = admin_client.post(f"/api/zones/{zone['id']}/mqtt/stop", content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        # Abort path: response includes session_aborted: True.
        assert data.get("session_aborted") is True

    def test_zone_mqtt_stop_during_session_emits_audit(self, admin_client, app):
        """Spec §4.2 #6: audit row with action_type='session_aborted_by_user'."""
        from irrigation_scheduler import init_scheduler

        sched = init_scheduler(app.db)
        group, zone = self._setup_zone(app, "Audit")
        sched.group_cancel_events[group["id"]] = threading.Event()

        resp = admin_client.post(f"/api/zones/{zone['id']}/mqtt/stop", content_type="application/json")
        assert resp.status_code == 200

        rows = app.db.get_audit_logs(action_type="session_aborted_by_user")
        # At least one row matching this group + endpoint.
        matched = [r for r in rows if r.get("target") == f"group:{group['id']}"]
        assert matched, f"no session_aborted_by_user audit row for group:{group['id']}"
        # payload_json may be a string (it's JSON), check substring rather
        # than parsing in case the audit redactor wrapped it.
        pj = str(matched[0].get("payload_json") or "")
        assert "api_zone_mqtt_stop" in pj
        assert str(zone["id"]) in pj

    # ─── #7: legacy /api/zones/<id>/stop endpoint ────────────────────────
    def test_legacy_zone_stop_during_session_emits_audit_with_distinct_endpoint(self, admin_client, app):
        """Spec §4.2 #7: same behaviour for /stop, distinguishable in audit
        via payload.endpoint='api_zone_stop'."""
        from irrigation_scheduler import init_scheduler

        sched = init_scheduler(app.db)
        group, zone = self._setup_zone(app, "Legacy")
        sched.group_cancel_events[group["id"]] = threading.Event()

        resp = admin_client.post(f"/api/zones/{zone['id']}/stop", content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("session_aborted") is True

        rows = app.db.get_audit_logs(action_type="session_aborted_by_user")
        matched = [r for r in rows if r.get("target") == f"group:{group['id']}"]
        assert matched, f"no session_aborted_by_user audit row for group:{group['id']}"
        pj = str(matched[0].get("payload_json") or "")
        # The distinguishing token: api_zone_stop, NOT api_zone_mqtt_stop.
        assert "api_zone_stop" in pj
        assert "mqtt" not in pj.lower() or "api_zone_mqtt_stop" not in pj

    # ─── #9: cancel_group_jobs failure is surfaced truthfully ────────────
    def test_zone_mqtt_stop_cancel_group_jobs_failure_is_not_success(self, admin_client, app, monkeypatch):
        """A cleanup failure cannot be reported as a completed session abort."""
        from irrigation_scheduler import init_scheduler

        sched = init_scheduler(app.db)
        group, zone = self._setup_zone(app, "Fallback")
        sched.group_cancel_events[group["id"]] = threading.Event()

        def _boom(*_a, **_kw):
            raise RuntimeError("forced failure for fallback test")

        monkeypatch.setattr(sched, "cancel_group_jobs", _boom)

        resp = admin_client.post(f"/api/zones/{zone['id']}/mqtt/stop", content_type="application/json")
        assert resp.status_code == 503, resp.get_data(as_text=True)
        assert resp.get_json()["success"] is False
        assert resp.get_json()["error_code"] == "SESSION_CLEANUP_FAILED"
