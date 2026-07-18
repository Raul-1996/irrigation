"""Phase-2 regressions for the system-status API package."""

import os
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import Mock


def test_finding_80_status_probe_uses_unique_ephemeral_mqtt_client_id(admin_client, app, monkeypatch):
    """A public status probe must not take over the long-lived hub session."""
    saved_client_id = "irrigation-live-client"
    app.db.create_mqtt_server(
        {
            "name": "status-probe",
            "host": "broker.invalid",
            "port": 1883,
            "client_id": saved_client_id,
            "enabled": 1,
        }
    )
    created_client_ids = []

    class FakeClient:
        def __init__(self, _callback_api, *, client_id):
            created_client_ids.append(client_id)

        def connect(self, _host, _port, _keepalive):
            return 0

        def disconnect(self):
            return 0

    fake_mqtt = SimpleNamespace(
        CallbackAPIVersion=SimpleNamespace(VERSION2=object()),
        Client=FakeClient,
    )
    monkeypatch.setattr("routes.system_status_api.mqtt", fake_mqtt)
    monkeypatch.setitem(app.config, "TESTING", False)

    response = admin_client.get("/api/status")

    assert response.status_code == 200
    assert len(created_client_ids) == 1
    assert created_client_ids[0] != saved_client_id
    assert created_client_ids[0].startswith("wb-status-")


def test_finding_102_logs_apply_filters_before_repository_limit(admin_client, app):
    """An old matching row remains visible after 1,000 newer non-matches."""
    with sqlite3.connect(app.db.db_path) as conn:
        conn.execute(
            "INSERT INTO logs(type, details, timestamp) VALUES (?, ?, ?)",
            ("phase2-target", "old matching event", "2026-06-10 12:00:00"),
        )
        conn.executemany(
            "INSERT INTO logs(type, details, timestamp) VALUES (?, ?, ?)",
            [
                ("phase2-noise", f"new event {index}", f"2026-07-{1 + index // 40:02d} 12:{index % 40:02d}:00")
                for index in range(1000)
            ],
        )

    response = admin_client.get("/api/logs?type=phase2-target&from=2026-06-01&to=2026-06-15")

    assert response.status_code == 200
    assert [entry["details"] for entry in response.get_json()] == ["old matching event"]


def test_finding_102_log_dates_use_controller_local_calendar(admin_client, app, monkeypatch):
    """SQL filtering and rendered timestamps use the same local calendar day."""
    previous_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Etc/GMT-6")
    time.tzset()
    try:
        with sqlite3.connect(app.db.db_path) as conn:
            conn.executemany(
                "INSERT INTO logs(type, details, timestamp) VALUES (?, ?, ?)",
                [
                    ("phase2-local-date", "local July 18", "2026-07-17 20:00:00"),
                    ("phase2-local-date", "local July 19", "2026-07-18 20:00:00"),
                ],
            )

        response = admin_client.get(
            "/api/logs?type=phase2-local-date&from=2026-07-18&to=2026-07-18"
        )

        assert response.status_code == 200
        assert [
            (entry["details"], entry["timestamp"])
            for entry in response.get_json()
        ] == [("local July 18", "2026-07-18 02:00:00")]
    finally:
        if previous_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", previous_tz)
        time.tzset()


def test_finding_139_status_deduplicates_program_cancellation_queries(admin_client, app, monkeypatch):
    """Status query count scales with programs/groups, not zones/programs."""
    group = app.db.create_group("query-amplification")
    zones = [
        app.db.create_zone(
            {
                "name": f"zone-{index}",
                "duration": 10,
                "group_id": group["id"],
            }
        )
        for index in range(32)
    ]
    zone_ids = [zone["id"] for zone in zones]
    for index in range(16):
        app.db.create_program(
            {
                "name": f"program-{index}",
                "time": "00:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "zones": zone_ids,
            }
        )

    cancellation_queries = []

    def record_cancellation_query(program_id, run_date, group_id):
        cancellation_queries.append((program_id, run_date, group_id))
        return False

    monkeypatch.setattr(app.db, "is_program_run_cancelled_for_group", record_cancellation_query)
    monkeypatch.setattr("routes.system_status_api.weather_skip_today", lambda: False)

    response = admin_client.get("/api/status")

    assert response.status_code == 200
    assert len(cancellation_queries) <= 16
    assert len(set(cancellation_queries)) == len(cancellation_queries)


def test_finding_148_health_cancel_does_not_create_ownerless_group_session(admin_client, monkeypatch):
    """Cancelling an idle group delegates cleanup without planting a session event."""
    scheduler = SimpleNamespace(group_cancel_events={}, cancel_group_jobs=Mock())
    monkeypatch.setattr("routes.system_status_api.get_scheduler", lambda: scheduler)

    response = admin_client.post("/api/health/group/37/cancel")

    assert response.status_code == 200
    scheduler.cancel_group_jobs.assert_called_once_with(37)
    assert scheduler.group_cancel_events == {}
