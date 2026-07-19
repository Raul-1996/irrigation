"""Phase-2 regressions for the system-status API package."""

import os
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock


def test_status_reuses_runtime_health_without_creating_diagnostic_client(admin_client, app, monkeypatch):
    """The polling endpoint must never open a broker connection in a WSGI worker."""
    from routes import system_status_api

    server = app.db.create_mqtt_server(
        {
            "name": "status-probe",
            "host": "broker.invalid",
            "port": 1883,
            "client_id": "irrigation-live-client",
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
    monkeypatch.setattr(system_status_api, "mqtt", fake_mqtt, raising=False)
    monkeypatch.setattr(
        system_status_api,
        "_snapshot_runtime_mqtt_health",
        lambda server_ids: {int(server["id"]): True},
        raising=False,
    )
    reset_cache = getattr(system_status_api, "_reset_runtime_mqtt_health_cache", None)
    if reset_cache is not None:
        reset_cache()
    monkeypatch.setitem(app.config, "TESTING", False)

    response = admin_client.get("/api/status")

    assert response.status_code == 200
    assert response.get_json()["mqtt_connected"] is True
    assert created_client_ids == []


def test_status_poll_never_durable_logs_mqtt_health(admin_client, app, monkeypatch):
    """Frequent status polls must not amplify warnings into SQLite writes."""
    add_log = Mock()
    monkeypatch.setattr(app.db, "add_log", add_log)

    for _ in range(3):
        response = admin_client.get("/api/status")
        assert response.status_code == 200

    add_log.assert_not_called()


def test_status_requires_accepted_connack_for_runtime_health(admin_client, app, monkeypatch):
    """A queued TCP/MQTT connect is not healthy until Paho accepts CONNACK."""
    from routes import system_status_api

    server = app.db.create_mqtt_server(
        {
            "name": "connack-pending",
            "host": "broker.invalid",
            "port": 1883,
            "enabled": 1,
        }
    )
    constructed = []

    class PendingClient:
        def __init__(self, *_args, **_kwargs):
            constructed.append(True)

        def connect(self, _host, _port, _keepalive):
            return 0

        def disconnect(self):
            return 0

        def is_connected(self):
            return False

    monkeypatch.setattr(
        system_status_api,
        "mqtt",
        SimpleNamespace(
            CallbackAPIVersion=SimpleNamespace(VERSION2=object()),
            Client=PendingClient,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        system_status_api,
        "_snapshot_runtime_mqtt_health",
        lambda server_ids: {int(server["id"]): False},
        raising=False,
    )
    reset_cache = getattr(system_status_api, "_reset_runtime_mqtt_health_cache", None)
    if reset_cache is not None:
        reset_cache()
    monkeypatch.setitem(app.config, "TESTING", False)

    response = admin_client.get("/api/status")

    assert response.status_code == 200
    assert response.get_json()["mqtt_connected"] is False
    assert constructed == []


def test_status_preserves_recent_runtime_health_when_snapshot_is_temporarily_unavailable(
    admin_client, app, monkeypatch
):
    """A rebuild/lock race must not erase a recent accepted runtime signal."""
    from routes import system_status_api

    server = app.db.create_mqtt_server(
        {
            "name": "last-known",
            "host": "broker.invalid",
            "port": 1883,
            "enabled": 1,
        }
    )
    snapshots = [{int(server["id"]): True}, {}]
    monkeypatch.setattr(
        system_status_api,
        "_snapshot_runtime_mqtt_health",
        lambda server_ids: snapshots.pop(0),
        raising=False,
    )
    monkeypatch.setattr(system_status_api, "_runtime_generation_token", lambda: b"stable-runtime")
    reset_cache = getattr(system_status_api, "_reset_runtime_mqtt_health_cache", None)
    assert reset_cache is not None
    reset_cache()

    first = admin_client.get("/api/status")
    second = admin_client.get("/api/status")

    assert first.get_json()["mqtt_connected"] is True
    assert second.get_json()["mqtt_connected"] is True


def test_status_does_not_carry_cached_health_across_password_rotation(admin_client, app, monkeypatch):
    """A connected old credential set says nothing about the rotated one."""
    from routes import system_status_api

    server = app.db.create_mqtt_server(
        {
            "name": "credential-rotation",
            "host": "broker.invalid",
            "port": 1883,
            "username": "irrigation",
            "password": "old-password",
            "enabled": 1,
        }
    )
    sid = int(server["id"])

    class AcceptedClient:
        @staticmethod
        def is_connected():
            return True

    old_config = app.db.get_mqtt_server(sid)
    fake_hub = SimpleNamespace(
        _SSE_HUB_LOCK=threading.Lock(),
        _SSE_HUB_MQTT={sid: AcceptedClient()},
        _SSE_HUB_SERVER_KEYS={sid: system_status_api._server_runtime_config(old_config)},
        _SSE_HUB_REQUESTED_GENERATION=7,
        _SSE_HUB_APPLIED_GENERATION=7,
    )
    monkeypatch.setattr(system_status_api, "_sse_hub", fake_hub)
    system_status_api._reset_runtime_mqtt_health_cache()

    healthy = admin_client.get("/api/status").get_json()
    assert healthy["mqtt_connected"] is True

    assert app.db.update_mqtt_server(sid, {"password": "rotated-password"}) is True
    rotated = admin_client.get("/api/status").get_json()

    assert rotated["mqtt_connected"] is False
    assert rotated["mqtt_health"]["status"] == "unknown"


def test_status_does_not_carry_cached_health_across_runtime_rebuild(admin_client, app, monkeypatch):
    """A rebuilt client generation needs its own accepted CONNACK."""
    from routes import system_status_api

    server = app.db.create_mqtt_server(
        {
            "name": "runtime-rebuild",
            "host": "broker.invalid",
            "port": 1883,
            "enabled": 1,
        }
    )
    snapshots = [{int(server["id"]): True}, {}]
    generation = {"value": b"generation-1"}
    monkeypatch.setattr(
        system_status_api,
        "_snapshot_runtime_mqtt_health",
        lambda servers: snapshots.pop(0),
    )
    monkeypatch.setattr(
        system_status_api,
        "_runtime_generation_token",
        lambda: generation["value"],
        raising=False,
    )
    system_status_api._reset_runtime_mqtt_health_cache()

    healthy = admin_client.get("/api/status").get_json()
    assert healthy["mqtt_connected"] is True

    generation["value"] = b"generation-2"
    rebuilt = admin_client.get("/api/status").get_json()

    assert rebuilt["mqtt_connected"] is False
    assert rebuilt["mqtt_health"]["status"] == "unknown"


def test_status_runtime_health_cache_is_bounded(monkeypatch):
    from routes import system_status_api

    reset_cache = getattr(system_status_api, "_reset_runtime_mqtt_health_cache", None)
    runtime_health = getattr(system_status_api, "_runtime_mqtt_health", None)
    assert reset_cache is not None
    assert runtime_health is not None
    reset_cache()
    servers = [
        {
            "id": server_id,
            "host": f"broker-{server_id}",
            "port": 1883,
            "enabled": 1,
        }
        for server_id in range(100)
    ]
    monkeypatch.setattr(
        system_status_api,
        "_snapshot_runtime_mqtt_health",
        lambda server_ids: {server_id: True for server_id in server_ids},
    )

    runtime_health(servers)

    assert len(system_status_api._MQTT_RUNTIME_HEALTH_CACHE) <= system_status_api.MQTT_RUNTIME_HEALTH_CACHE_MAX


def test_status_runtime_cache_key_does_not_store_plaintext_secret(monkeypatch):
    from routes import system_status_api

    secret = "cache-key-must-not-contain-this-password"
    system_status_api._reset_runtime_mqtt_health_cache()
    monkeypatch.setattr(
        system_status_api,
        "_snapshot_runtime_mqtt_health",
        lambda servers: {47: True},
    )
    monkeypatch.setattr(
        system_status_api,
        "_runtime_generation_token",
        lambda: b"test-generation",
        raising=False,
    )

    system_status_api._runtime_mqtt_health(
        [
            {
                "id": 47,
                "host": "broker.invalid",
                "port": 1883,
                "username": "irrigation",
                "password": secret,
                "enabled": 1,
            }
        ]
    )

    assert secret not in repr(tuple(system_status_api._MQTT_RUNTIME_HEALTH_CACHE))


def test_status_degrades_structurally_when_mqtt_secret_cannot_be_decrypted(admin_client, app, monkeypatch):
    from routes import system_status_api
    from utils import SecretDecryptionError

    def fail_to_decrypt():
        raise SecretDecryptionError("must not leak")

    monkeypatch.setattr(app.db, "get_mqtt_servers", fail_to_decrypt)

    response = admin_client.get("/api/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["mqtt_health"]["status"] == "degraded"
    assert payload["mqtt_health"]["error_code"] == "MQTT_SECRET_UNAVAILABLE"
    assert "must not leak" not in response.get_data(as_text=True)
    assert any(fault["type"] == "mqtt_secret_unavailable" for fault in payload["system_health"]["faults"])


def test_sensitive_status_gets_require_admin_but_public_status_remains_available(client, app, monkeypatch):
    monkeypatch.setitem(app.config, "TESTING", False)

    for path in ("/api/logs", "/api/scheduler/status", "/api/scheduler/jobs"):
        response = client.get(path)
        assert response.status_code == 403
        assert response.get_json()["error_code"] == "FORBIDDEN"

    assert client.get("/api/status").status_code == 200


def test_legacy_health_does_not_claim_empty_mqtt_configuration(admin_client):
    response = admin_client.get("/health")

    assert response.status_code == 200
    assert response.get_json()["mqtt_configured"] is False


def test_legacy_health_checks_database_instead_of_treating_empty_query_as_success(
    admin_client, monkeypatch
):
    from routes import system_status_api

    monkeypatch.setattr(system_status_api, "_database_is_healthy", lambda: False, raising=False)

    response = admin_client.get("/health")

    assert response.status_code == 503
    assert response.get_json()["db"] is False


def test_legacy_health_degrades_structurally_on_mqtt_secret_error(admin_client, app, monkeypatch):
    from utils import SecretDecryptionError

    def fail_to_decrypt():
        raise SecretDecryptionError("must not leak")

    monkeypatch.setattr(app.db, "get_mqtt_servers", fail_to_decrypt)

    response = admin_client.get("/health")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["mqtt_configured"] is False
    assert payload["mqtt_health"]["status"] == "degraded"
    assert payload["mqtt_health"]["error_code"] == "MQTT_SECRET_UNAVAILABLE"
    assert "must not leak" not in response.get_data(as_text=True)


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
