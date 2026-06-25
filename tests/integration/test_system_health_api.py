"""Feature A: /api/status surfaces zones in fault as system_health so the
UI can show a prominent alert when watering is broken."""

import os

os.environ["TESTING"] = "1"


class TestSystemHealth:
    def test_status_ok_when_no_faults(self, client, app):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "system_health" in data
        assert data["system_health"]["ok"] is True
        assert data["system_health"]["faults"] == []

    def test_status_reports_zone_fault(self, client, app):
        zone = app.db.create_zone({"name": "СбойЗона", "duration": 10, "group_id": 1, "topic": "/d/f/K1"})
        # Relay did not confirm — zone is parked in fault and not watering.
        app.db.update_zone(zone["id"], {"state": "fault", "last_fault": "2026-06-25 23:41:35"})

        resp = client.get("/api/status")
        assert resp.status_code == 200
        health = resp.get_json()["system_health"]
        assert health["ok"] is False
        match = [f for f in health["faults"] if f["zone_id"] == zone["id"]]
        assert len(match) == 1
        f = match[0]
        assert f["type"] == "zone_fault"
        assert f["zone_name"] == "СбойЗона"
        assert f["since"] == "2026-06-25 23:41:35"
        assert "реле" in f["reason"].lower()

    def test_recovered_zone_drops_out_of_faults(self, client, app):
        zone = app.db.create_zone({"name": "Z", "duration": 10, "group_id": 1, "topic": "/d/r/K1"})
        app.db.update_zone(zone["id"], {"state": "fault", "last_fault": "2026-06-25 23:41:35"})
        assert client.get("/api/status").get_json()["system_health"]["ok"] is False
        # Operator/recovery cleared it back to off.
        app.db.update_zone(zone["id"], {"state": "off"})
        health = client.get("/api/status").get_json()["system_health"]
        assert health["ok"] is True
        assert all(f["zone_id"] != zone["id"] for f in health["faults"])
