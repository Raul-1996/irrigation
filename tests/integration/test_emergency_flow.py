"""Integration test: emergency stop → resume flow."""

import os

os.environ["TESTING"] = "1"


class TestEmergencyFlow:
    def test_emergency_stop_stops_all_zones(self, admin_client, app):
        """Emergency stop should stop all running zones."""
        # Create a zone and set it ON
        zone = app.db.create_zone(
            {
                "name": "Emergency",
                "duration": 10,
                "group_id": 1,
            }
        )
        app.db.update_zone(zone["id"], {"state": "on", "watering_start_time": "2026-01-01 10:00:00"})

        # Emergency stop
        resp = admin_client.post("/api/emergency-stop", content_type="application/json")
        assert resp.status_code == 503
        body = resp.get_json()
        assert body["success"] is False
        assert body["physical_stop_confirmed"] is True
        assert body["sessions_quiesced"] is False
        assert app.config["EMERGENCY_STOP"] is True

    def test_emergency_resume(self, admin_client, app):
        """Emergency resume should clear emergency state."""
        # First stop
        admin_client.post("/api/emergency-stop", content_type="application/json")

        # Then resume
        resp = admin_client.post("/api/emergency-resume", content_type="application/json")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_cannot_start_during_emergency(self, admin_client, app):
        """Should not be able to start a zone during emergency stop."""
        zone = app.db.create_zone(
            {
                "name": "NoStart",
                "duration": 10,
                "group_id": 1,
            }
        )

        # Emergency stop
        admin_client.post("/api/emergency-stop", content_type="application/json")

        # Try to start zone
        resp = admin_client.post(f"/api/zones/{zone['id']}/start", content_type="application/json")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False

    def test_group_start_from_first_blocked_during_emergency(self, admin_client, app):
        """Sequential group start must be blocked during emergency stop."""
        admin_client.post("/api/emergency-stop", content_type="application/json")

        resp = admin_client.post("/api/groups/1/start-from-first", content_type="application/json")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False
        assert "Аварийная остановка" in data["message"]

    def test_run_selected_blocked_during_emergency(self, admin_client, app):
        """Ad-hoc run of selected zones must be blocked during emergency stop."""
        zone = app.db.create_zone({"name": "SelNoStart", "duration": 10, "group_id": 1})
        admin_client.post("/api/emergency-stop", content_type="application/json")

        resp = admin_client.post("/api/groups/1/run-selected", json={"zones": [zone["id"]]})
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False
        assert "Аварийная остановка" in data["message"]

    def test_program_run_blocked_during_emergency(self, admin_client, app):
        """Manual program run must be blocked during emergency stop."""
        zone = app.db.create_zone({"name": "ProgNoStart", "duration": 10, "group_id": 1})
        program = app.db.create_program({"name": "P-Emergency", "time": "06:00", "zones": [zone["id"]]})
        admin_client.post("/api/emergency-stop", content_type="application/json")

        resp = admin_client.post(f"/api/programs/{program['id']}/run", content_type="application/json")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False
        assert "Аварийная остановка" in data["message"]

    def test_telegram_group_start_blocked_during_emergency(self, admin_client, app):
        """Telegram group start must respect the emergency stop flag."""
        admin_client.post("/api/emergency-stop", content_type="application/json")

        from routes import telegram as tg

        notice = tg._do_group_start(1)
        assert "Аварийная остановка" in notice
