"""Truthful HTTP contract for unresolved emergency-stop work."""

from unittest.mock import Mock, patch


def _complete_scheduler_result(app, group_id):
    zone_ids = sorted(int(zone["id"]) for zone in app.db.get_zones_by_group(int(group_id)))
    return {
        "success": True,
        "aggregate_valid": True,
        "stopped": zone_ids,
        "unresolved": [],
        "unverified_zone_ids": [],
        "retry_scheduled": False,
        "group_id": int(group_id),
    }


def test_emergency_stop_separates_physical_failure_from_quiesced_sessions(admin_client, app):
    stats = {
        "success": False,
        "errors": ["phase-b-timeout"],
        "zones_failed": [17],
        "zones_still_active_after_wait": 1,
        "masters_failed_publish": 1,
    }

    scheduler = Mock()
    scheduler.cancel_group_jobs.side_effect = lambda group_id, **_kwargs: _complete_scheduler_result(app, group_id)

    with (
        patch("services.zone_control.emergency_stop_all", return_value=stats),
        patch("routes.system_emergency_api.get_scheduler", return_value=scheduler),
    ):
        response = admin_client.post("/api/emergency-stop")

    assert 500 <= response.status_code < 600
    body = response.get_json()
    assert body["success"] is False
    assert body["physical_stop_confirmed"] is False
    assert body["sessions_quiesced"] is True
    assert body["zones_failed"] == [17]
    assert body["masters_failed_publish"] == 1
    assert body["stats"] == stats
    assert "warning" in body
    assert "zones_failed" in body["warning"]
    assert "errors" in body["warning"]
    assert app.config["EMERGENCY_STOP"] is True
    app.config["EMERGENCY_STOP"] = False


def test_emergency_stop_rejects_invalid_scheduler_partition_after_physical_success(admin_client, app):
    group = app.db.create_group("emergency invalid scheduler aggregate")
    zone = app.db.create_zone({"name": "must be in scheduler partition", "duration": 5, "group_id": group["id"]})
    stats = {
        "success": True,
        "errors": [],
        "zones_failed": [],
        "zones_still_active_after_wait": 0,
        "masters_failed_publish": 0,
    }
    scheduler = Mock()

    def cancel_group(group_id, **_kwargs):
        result = _complete_scheduler_result(app, group_id)
        if int(group_id) == group["id"]:
            result["stopped"] = []
        return result

    scheduler.cancel_group_jobs.side_effect = cancel_group

    with (
        patch("services.zone_control.emergency_stop_all", return_value=stats),
        patch("routes.system_emergency_api.get_scheduler", return_value=scheduler),
    ):
        response = admin_client.post("/api/emergency-stop")

    assert response.status_code == 503
    body = response.get_json()
    assert body["success"] is False
    assert body["physical_stop_confirmed"] is True
    assert body["sessions_quiesced"] is False
    assert body["zones_failed"] == []
    assert body["masters_failed_publish"] == 0
    assert body.get("message") != "Аварийная остановка выполнена"
    assert app.config["EMERGENCY_STOP"] is True
    assert zone["id"] not in body.get("zones_failed", [])
    app.config["EMERGENCY_STOP"] = False


def test_emergency_stop_returns_success_only_after_physical_off_and_session_quiesce(admin_client, app):
    group = app.db.create_group("emergency complete scheduler aggregate")
    zone = app.db.create_zone({"name": "fully stopped", "duration": 5, "group_id": group["id"]})
    stats = {
        "success": True,
        "errors": [],
        "zones_failed": [],
        "zones_still_active_after_wait": 0,
        "masters_failed_publish": 0,
    }
    scheduler = Mock()
    scheduler.cancel_group_jobs.side_effect = lambda group_id, **_kwargs: _complete_scheduler_result(app, group_id)

    with (
        patch("services.zone_control.emergency_stop_all", return_value=stats),
        patch("routes.system_emergency_api.get_scheduler", return_value=scheduler),
    ):
        response = admin_client.post("/api/emergency-stop")

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["physical_stop_confirmed"] is True
    assert body["sessions_quiesced"] is True
    assert body["zones_failed"] == []
    assert body["masters_failed_publish"] == 0
    assert body["stats"] == stats
    assert "warning" not in body
    assert app.config["EMERGENCY_STOP"] is True
    assert zone["id"] in _complete_scheduler_result(app, group["id"])["stopped"]
    app.config["EMERGENCY_STOP"] = False
