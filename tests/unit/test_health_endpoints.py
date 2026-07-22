"""Wave 2 F2 — unit tests for /healthz and /readyz endpoints.

Covers design doc §3.2 and §3.3 (healthz + readyz behaviours):
  - /healthz: 200 always, no auth, no DB touch
  - /readyz: aggregates all checks, 200/503, runs every check
  - individual check fail paths for each of the 5 checks
"""

import os
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def health_api(client):
    """Return the live routes.health_api module that the Flask app is using.

    The `app` fixture reloads routes.* via importlib, so a plain
    ``from routes import health_api`` at module import time would capture
    a stale instance whose _check_* attrs are NOT the ones invoked at
    request time.  This fixture resolves the current instance via
    ``sys.modules`` AFTER the app fixture has finished its reload dance.
    """
    return sys.modules["routes.health_api"]


# ── /healthz ───────────────────────────────────────────────────────────────


def test_healthz_returns_200(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {"status": "ok"}


def test_healthz_no_auth_required(guest_client):
    """Guest (no session) must also get 200 — this is a liveness probe."""
    resp = guest_client.get("/healthz")
    assert resp.status_code == 200


def test_healthz_does_not_touch_db(client):
    """Even if the DB is busted, /healthz must still return 200 — it should
    not call into the DB layer at all.
    """
    with patch.object(sqlite3, "connect", side_effect=sqlite3.OperationalError("boom")):
        resp = client.get("/healthz")
    assert resp.status_code == 200


def test_healthz_echoes_startup_token_only_to_matching_private_probe(client, app):
    app.config["HTTP_STARTUP_PROBE_TOKEN"] = "current-process-token"

    public = client.get("/healthz")
    stale = client.get("/healthz", headers={"X-WB-Startup-Probe": "old-process-token"})
    matching = client.get("/healthz", headers={"X-WB-Startup-Probe": "current-process-token"})

    assert public.get_json() == {"status": "ok"}
    assert "X-WB-Startup-Probe" not in public.headers
    assert "X-WB-Startup-Probe" not in stale.headers
    assert matching.headers["X-WB-Startup-Probe"] == "current-process-token"


# ── /readyz ────────────────────────────────────────────────────────────────


def _stub_all_checks_ok(health_api):
    """Patch every private _check_* to return ok."""
    return patch.multiple(
        health_api,
        _check_boot_reconcile=lambda: {"status": "ok"},
        _check_disk_space=lambda min_free_mb=50: {"status": "ok", "free_mb": 9999},
        _check_scheduler=lambda: {"status": "ok", "duration_ms": 1},
        _check_mqtt=lambda db: {"status": "skipped", "brokers": 0, "duration_ms": 1},
        _check_db=lambda db_path="irrigation.db": {"status": "ok", "duration_ms": 1},
    )


def test_readyz_all_checks_ok_returns_200(client, health_api):
    with _stub_all_checks_ok(health_api):
        resp = client.get("/readyz")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert set(data["checks"].keys()) == {"boot_reconcile", "disk_space", "scheduler", "mqtt", "db"}
    for check, res in data["checks"].items():
        assert res["status"] in ("ok", "skipped"), f"check {check}: {res}"


def test_readyz_db_fail_returns_503(client, health_api):
    with patch.multiple(
        health_api,
        _check_boot_reconcile=lambda: {"status": "ok"},
        _check_disk_space=lambda min_free_mb=50: {"status": "ok", "free_mb": 9999},
        _check_scheduler=lambda: {"status": "ok", "duration_ms": 1},
        _check_mqtt=lambda db: {"status": "skipped", "brokers": 0},
        _check_db=lambda db_path="irrigation.db": {
            "status": "fail",
            "duration_ms": 5,
            "reason": "OperationalError",
        },
    ):
        resp = client.get("/readyz")
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["status"] == "fail"
    assert data["checks"]["db"]["status"] == "fail"


def test_readyz_scheduler_fail_returns_503(client, health_api):
    with patch.multiple(
        health_api,
        _check_boot_reconcile=lambda: {"status": "ok"},
        _check_disk_space=lambda min_free_mb=50: {"status": "ok", "free_mb": 9999},
        _check_scheduler=lambda: {
            "status": "fail",
            "duration_ms": 0,
            "reason": "scheduler.running is False",
        },
        _check_mqtt=lambda db: {"status": "skipped", "brokers": 0},
        _check_db=lambda db_path="irrigation.db": {"status": "ok"},
    ):
        resp = client.get("/readyz")
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["checks"]["scheduler"]["status"] == "fail"


def test_readyz_boot_reconcile_gate(client, health_api, monkeypatch):
    """When app_init._boot_sync_done is False, check must fail."""
    from services import app_init

    monkeypatch.setattr(app_init, "_boot_sync_done", False, raising=False)
    # Still stub the other checks to ok so we isolate the boot_reconcile fail.
    with patch.multiple(
        health_api,
        _check_disk_space=lambda min_free_mb=50: {"status": "ok", "free_mb": 1000},
        _check_scheduler=lambda: {"status": "ok"},
        _check_mqtt=lambda db: {"status": "skipped", "brokers": 0},
        _check_db=lambda db_path="irrigation.db": {"status": "ok"},
    ):
        resp = client.get("/readyz")
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["checks"]["boot_reconcile"]["status"] == "fail"
    # And once we flip the gate, readyz goes back to ok.
    monkeypatch.setattr(app_init, "_boot_sync_done", True, raising=False)
    monkeypatch.setattr(app_init, "_boot_recovery_done", True, raising=False)
    with patch.multiple(
        health_api,
        _check_disk_space=lambda min_free_mb=50: {"status": "ok", "free_mb": 1000},
        _check_scheduler=lambda: {"status": "ok"},
        _check_mqtt=lambda db: {"status": "skipped", "brokers": 0},
        _check_db=lambda db_path="irrigation.db": {"status": "ok"},
    ):
        resp2 = client.get("/readyz")
    assert resp2.status_code == 200


def test_readyz_disk_space_fail(client, health_api):
    """< 50 MB free → disk_space check fails → 503."""
    # Build a fake statvfs result: f_bavail * f_frsize < 50 MB.
    fake_stat = MagicMock()
    fake_stat.f_bavail = 10  # 10 blocks * 4 KiB = 40 KB
    fake_stat.f_frsize = 4096
    with (
        patch.object(os, "statvfs", return_value=fake_stat),
        patch.multiple(
            health_api,
            _check_boot_reconcile=lambda: {"status": "ok"},
            _check_scheduler=lambda: {"status": "ok"},
            _check_mqtt=lambda db: {"status": "skipped", "brokers": 0},
            _check_db=lambda db_path="irrigation.db": {"status": "ok"},
        ),
    ):
        resp = client.get("/readyz")
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["checks"]["disk_space"]["status"] == "fail"
    assert "free_mb" in data["checks"]["disk_space"]


def test_readyz_runs_all_checks_even_when_one_fails(client, health_api):
    """Do-not-short-circuit: when db fails, scheduler check must still run."""
    calls = {"scheduler": 0, "mqtt": 0}

    def _sched():
        calls["scheduler"] += 1
        return {"status": "ok", "duration_ms": 1}

    def _mqtt(db):
        calls["mqtt"] += 1
        return {"status": "skipped", "brokers": 0}

    with patch.multiple(
        health_api,
        _check_boot_reconcile=lambda: {"status": "ok"},
        _check_disk_space=lambda min_free_mb=50: {"status": "ok", "free_mb": 100},
        _check_scheduler=_sched,
        _check_mqtt=_mqtt,
        _check_db=lambda db_path="irrigation.db": {"status": "fail", "reason": "Err"},
    ):
        resp = client.get("/readyz")
    assert resp.status_code == 503
    # All checks ran, none were skipped due to short-circuit.
    assert calls["scheduler"] == 1
    assert calls["mqtt"] == 1


def test_readyz_mqtt_skipped_does_not_fail(client, health_api):
    """mqtt check returning 'skipped' is not a failure (fresh install)."""
    with patch.multiple(
        health_api,
        _check_boot_reconcile=lambda: {"status": "ok"},
        _check_disk_space=lambda min_free_mb=50: {"status": "ok", "free_mb": 100},
        _check_scheduler=lambda: {"status": "ok"},
        _check_mqtt=lambda db: {"status": "skipped", "brokers": 0, "reason": "no brokers configured"},
        _check_db=lambda db_path="irrigation.db": {"status": "ok"},
    ):
        resp = client.get("/readyz")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["checks"]["mqtt"]["status"] == "skipped"


def test_readyz_redacts_check_topology_from_unauthenticated_clients(client, app, health_api):
    app.config["TESTING"] = False
    try:
        with _stub_all_checks_ok(health_api):
            response = client.get("/readyz")
    finally:
        app.config["TESTING"] = True

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_minimal_health_probes_bypass_session_and_password_policy_db(client, app, health_api, monkeypatch):
    ensure_password_policy = MagicMock(side_effect=AssertionError("health probe touched auth DB"))
    monkeypatch.setattr(app.db, "ensure_password_change_required", ensure_password_policy)
    app.config["TESTING"] = False
    try:
        with _stub_all_checks_ok(health_api):
            liveness = client.get("/healthz")
            readiness = client.get("/readyz")
    finally:
        app.config["TESTING"] = True

    assert liveness.status_code == 200
    assert readiness.status_code == 200
    assert readiness.get_json() == {"status": "ok"}
    assert "Set-Cookie" not in liveness.headers
    assert "Set-Cookie" not in readiness.headers
    ensure_password_policy.assert_not_called()


def test_metrics_requires_authenticated_admin(client, app):
    app.config["TESTING"] = False
    try:
        response = client.get("/metrics")
    finally:
        app.config["TESTING"] = True

    assert response.status_code == 401
    assert response.get_json()["error_code"] == "UNAUTHENTICATED"


# ── Individual check function unit tests (no Flask) ────────────────────────


def test_check_db_fail_when_path_bad(health_api, tmp_path):
    """_check_db on a non-writable / nonexistent path returns fail."""
    # sqlite3.connect on a directory path raises OperationalError.
    result = health_api._check_db(db_path=str(tmp_path))
    assert result["status"] == "fail"
    assert "duration_ms" in result


def test_check_disk_space_reports_free_mb(health_api):
    """_check_disk_space returns ok + free_mb on normal cwd."""
    result = health_api._check_disk_space(min_free_mb=1)
    assert result["status"] == "ok"
    assert isinstance(result["free_mb"], int)
    assert result["free_mb"] >= 1


def test_check_mqtt_fails_when_only_one_of_two_enabled_brokers_is_connected(health_api, monkeypatch):
    from services import mqtt_pub

    class ConnectedClient:
        @staticmethod
        def is_connected():
            return True

    db = MagicMock()
    db.get_mqtt_servers.return_value = [
        {"id": 11, "host": "broker-a", "port": 1883, "enabled": 1},
        {"id": 12, "host": "broker-b", "port": 1883, "enabled": 1},
    ]
    monkeypatch.setattr(
        mqtt_pub,
        "snapshot_mqtt_clients",
        lambda: {
            11: SimpleNamespace(
                client=ConnectedClient(),
                config_fingerprint="fp:11",
            )
        },
    )
    monkeypatch.setattr(
        mqtt_pub,
        "mqtt_server_config_fingerprint",
        lambda server: f"fp:{server['id']}",
    )

    result = health_api._check_mqtt(db)

    assert result["status"] == "fail"
    assert result["brokers"] == 2
    assert result["connected"] == 1
    assert result["unavailable"] == 1


def test_check_mqtt_ignores_explicitly_disabled_brokers(health_api, monkeypatch):
    from services import mqtt_pub

    class ConnectedClient:
        @staticmethod
        def is_connected():
            return True

    db = MagicMock()
    db.get_mqtt_servers.return_value = [
        {"id": 21, "host": "broker-a", "port": 1883, "enabled": 1},
        {"id": 22, "host": "broker-disabled", "port": 1883, "enabled": 0},
    ]
    monkeypatch.setattr(
        mqtt_pub,
        "snapshot_mqtt_clients",
        lambda: {
            21: SimpleNamespace(
                client=ConnectedClient(),
                config_fingerprint="fp:21",
            )
        },
    )
    monkeypatch.setattr(
        mqtt_pub,
        "mqtt_server_config_fingerprint",
        lambda server: f"fp:{server['id']}",
    )

    result = health_api._check_mqtt(db)

    assert result["status"] == "ok"
    assert result["brokers"] == 1
    assert result["connected"] == 1
    assert result["unavailable"] == 0
