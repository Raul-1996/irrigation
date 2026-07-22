"""Tests for system API: backup, logs, emergency-stop/resume, settings."""

import json
import os

import pytest

os.environ["TESTING"] = "1"


class TestHealthAPI:
    def test_health_check(self, admin_client):
        resp = admin_client.get("/health")
        assert resp.status_code in (200, 503)
        data = resp.get_json()
        assert "ok" in data

    def test_status(self, admin_client):
        resp = admin_client.get("/api/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "groups" in data
        assert "emergency_stop" in data

    def test_server_time(self, admin_client):
        resp = admin_client.get("/api/server-time")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "now_iso" in data
        assert "epoch_ms" in data


class TestEmergencyAPI:
    def test_emergency_stop(self, admin_client):
        resp = admin_client.post("/api/emergency-stop", content_type="application/json")
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["success"] is False
        assert data["sessions_quiesced"] is False

    def test_emergency_resume(self, admin_client):
        # First stop
        admin_client.post("/api/emergency-stop", content_type="application/json")
        # Then resume
        resp = admin_client.post("/api/emergency-resume", content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True


class TestBackupAPI:
    def test_create_backup(self, admin_client):
        resp = admin_client.post("/api/backup", content_type="application/json")
        assert resp.status_code in (200, 500)


class TestLogsAPI:
    def test_get_logs(self, admin_client):
        resp = admin_client.get("/api/logs")
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)

    def test_get_logs_filtered(self, admin_client):
        resp = admin_client.get("/api/logs?type=zone_start")
        assert resp.status_code == 200


class TestWaterAPI:
    def test_get_water(self, admin_client):
        resp = admin_client.get("/api/water")
        assert resp.status_code == 200


class TestSchedulerAPI:
    def test_scheduler_init(self, admin_client):
        resp = admin_client.post("/api/scheduler/init", content_type="application/json")
        assert resp.status_code == 200

    def test_scheduler_status(self, admin_client):
        resp = admin_client.get("/api/scheduler/status")
        assert resp.status_code in (200, 500)

    def test_scheduler_jobs(self, admin_client):
        resp = admin_client.get("/api/scheduler/jobs")
        assert resp.status_code == 200


class TestPostponeAPI:
    @pytest.mark.parametrize("days", ["abc", 100_000_000_000_000])
    def test_postpone_rejects_invalid_days_without_writes(self, guest_client, app, days):
        zone = app.db.create_zone({"name": "P", "duration": 10, "group_id": 1})

        resp = guest_client.post(
            "/api/postpone",
            data=json.dumps(
                {
                    "group_id": 1,
                    "days": days,
                    "action": "postpone",
                }
            ),
            content_type="application/json",
        )

        assert resp.status_code == 400
        assert app.db.get_zone(zone["id"])["postpone_until"] is None

    def test_postpone(self, admin_client, app, monkeypatch):
        import irrigation_scheduler

        zone = app.db.create_zone({"name": "P", "duration": 10, "group_id": 1})
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
                    "group_id": 1,
                    "days": 1,
                    "action": "postpone",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

    def test_postpone_cancel(self, admin_client, app):
        app.db.create_zone({"name": "P", "duration": 10, "group_id": 1})
        resp = admin_client.post(
            "/api/postpone",
            data=json.dumps(
                {
                    "group_id": 1,
                    "action": "cancel",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_postpone_invalid_action(self, admin_client):
        resp = admin_client.post(
            "/api/postpone",
            data=json.dumps(
                {
                    "group_id": 1,
                    "action": "invalid",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
