"""Release regressions for truthful data/API contracts."""

from __future__ import annotations


def _create_zone(app, name: str = "CAS zone") -> dict:
    zone = app.db.create_zone({"name": name, "duration": 10, "group_id": 1})
    assert zone is not None
    return zone


def _create_program(app, *, program_type: str = "time-based") -> dict:
    zone = _create_zone(app, f"{program_type} zone")
    program = app.db.create_program(
        {
            "name": f"{program_type} program",
            "time": "06:00",
            "days": [0],
            "zones": [zone["id"]],
            "type": program_type,
        }
    )
    assert program is not None
    return program


def test_zone_put_requires_caller_owned_expected_version(admin_client, app):
    zone = _create_zone(app)

    response = admin_client.put(f"/api/zones/{zone['id']}", json={"name": "unsafe write"})

    assert response.status_code == 428
    assert response.get_json()["error_code"] == "EXPECTED_VERSION_REQUIRED"
    assert app.db.get_zone(zone["id"])["name"] == zone["name"]


def test_zone_read_models_expose_the_next_cas_version(admin_client, app):
    zone = _create_zone(app)

    single = admin_client.get(f"/api/zones/{zone['id']}").get_json()
    listed = admin_client.get("/api/zones").get_json()

    assert single["version"] == zone["version"]
    assert next(item for item in listed if item["id"] == zone["id"])["version"] == zone["version"]


def test_zone_put_rejects_stale_expected_version_without_overwrite(admin_client, app):
    zone = _create_zone(app)
    first = admin_client.put(
        f"/api/zones/{zone['id']}",
        json={"name": "first writer", "expected_version": zone["version"]},
    )
    assert first.status_code == 200
    first_zone = first.get_json()
    assert first_zone["success"] is True
    assert first_zone["version"] == zone["version"] + 1
    assert app.db.get_zone(zone["id"])["version"] == first_zone["version"]

    stale = admin_client.put(
        f"/api/zones/{zone['id']}",
        json={"name": "stale writer", "expected_version": zone["version"]},
    )

    assert stale.status_code == 409
    payload = stale.get_json()
    assert payload["error_code"] == "ZONE_VERSION_CONFLICT"
    assert payload["expected_version"] == zone["version"]
    assert payload["current_version"] == first_zone["version"]
    assert app.db.get_zone(zone["id"])["name"] == "first writer"


def test_program_log_and_stats_report_unsupported_instead_of_fake_data(admin_client, app):
    program = _create_program(app)

    log_response = admin_client.get(f"/api/programs/{program['id']}/log")
    stats_response = admin_client.get(f"/api/programs/{program['id']}/stats")

    for response, capability in ((log_response, "program_log"), (stats_response, "program_stats")):
        assert response.status_code == 501
        payload = response.get_json()
        assert payload == {
            "success": False,
            "supported": False,
            "capability": capability,
            "error_code": "PROGRAM_RUN_IDENTITY_UNAVAILABLE",
            "message": "Program execution identity is not stored; this capability is unavailable",
        }


def test_program_create_rejects_smart_as_unsupported(admin_client, app):
    zone = _create_zone(app, "Smart candidate")

    response = admin_client.post(
        "/api/programs",
        json={
            "name": "Unsupported smart",
            "time": "06:00",
            "days": [0],
            "zones": [zone["id"]],
            "type": "smart",
        },
    )

    assert response.status_code == 422
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["supported"] is False
    assert payload["error_code"] == "PROGRAM_TYPE_UNSUPPORTED"
    assert app.db.get_programs() == []


def test_existing_smart_program_cannot_be_enabled_or_run(admin_client, app, monkeypatch):
    from routes import programs_api

    program = _create_program(app, program_type="smart")
    app.db.update_program(program["id"], {"enabled": False})
    monkeypatch.setattr(programs_api, "get_scheduler", lambda: None)

    enabled = admin_client.patch(f"/api/programs/{program['id']}/enabled", json={"enabled": True})
    run = admin_client.post(f"/api/programs/{program['id']}/run")

    for response in (enabled, run):
        assert response.status_code == 422
        assert response.get_json()["error_code"] == "PROGRAM_TYPE_UNSUPPORTED"
    assert app.db.get_program(program["id"])["enabled"] is False
