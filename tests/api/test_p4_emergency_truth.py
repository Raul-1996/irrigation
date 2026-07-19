"""Truthful HTTP contract for unresolved emergency-stop work."""

from unittest.mock import patch


def test_emergency_stop_returns_5xx_and_success_false_when_physical_work_is_unresolved(admin_client):
    stats = {
        "success": False,
        "errors": ["phase-b-timeout"],
        "zones_failed": [17],
        "zones_still_active_after_wait": 1,
        "masters_failed_publish": 1,
    }

    with patch("services.zone_control.emergency_stop_all", return_value=stats):
        response = admin_client.post("/api/emergency-stop")

    assert 500 <= response.status_code < 600
    body = response.get_json()
    assert body["success"] is False
    assert body["stats"] == stats
    assert "warning" in body
    assert "zones_failed" in body["warning"]
    assert "errors" in body["warning"]


def test_emergency_stop_returns_success_only_for_fully_resolved_stats(admin_client):
    stats = {
        "success": True,
        "errors": [],
        "zones_failed": [],
        "zones_still_active_after_wait": 0,
        "masters_failed_publish": 0,
    }

    with patch("services.zone_control.emergency_stop_all", return_value=stats):
        response = admin_client.post("/api/emergency-stop")

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["stats"] == stats
    assert "warning" not in body
