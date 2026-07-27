"""Fail-closed liveness contract for the APScheduler wrapper (issue #62)."""

from __future__ import annotations

import contextlib
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from apscheduler.schedulers.base import STATE_PAUSED, STATE_RUNNING, STATE_STOPPED


def _make_scheduler(test_db, monkeypatch, *, monotonic: float = 100.0):
    import irrigation_scheduler as scheduler_module

    clock = [monotonic]
    monkeypatch.setattr(scheduler_module.time, "monotonic", lambda: clock[0])
    scheduler = scheduler_module.IrrigationScheduler(test_db)
    return scheduler, clock


def _mark_apscheduler_running(scheduler):
    scheduler.is_running = True
    scheduler.scheduler.state = STATE_RUNNING
    scheduler.scheduler._thread = MagicMock()
    scheduler.scheduler._thread.is_alive.return_value = True


def test_not_started_scheduler_health_fails_closed_during_heartbeat_grace(test_db, monkeypatch):
    scheduler, clock = _make_scheduler(test_db, monkeypatch)
    clock[0] = 101.0

    health = scheduler.health_status()

    assert health == {
        "healthy": False,
        "wrapper_running": False,
        "is_running": False,
        "apscheduler_state": STATE_STOPPED,
        "apscheduler_running": False,
        "background_thread_alive": False,
        "heartbeat_age_seconds": 1.0,
        "heartbeat_stale": False,
        "volatile_jobstore_available": True,
        "reason": "wrapper_not_running",
        "reasons": [
            "wrapper_not_running",
            "apscheduler_not_running",
            "background_thread_missing",
        ],
    }


def test_start_schedules_immediate_30_second_volatile_heartbeat_before_apscheduler(test_db):
    from irrigation_scheduler import IrrigationScheduler

    scheduler = IrrigationScheduler(test_db)
    apscheduler = MagicMock()
    apscheduler.timezone = ZoneInfo("Pacific/Kiritimati")
    events = []

    def record_add_job(*args, **kwargs):
        events.append(("heartbeat", args, kwargs))

    apscheduler.add_job.side_effect = record_add_job
    apscheduler.start.side_effect = lambda: events.append(("start", (), {}))
    apscheduler.get_jobs.return_value = []
    scheduler.scheduler = apscheduler
    scheduler.schedule_postpone_sweeper = MagicMock()
    scheduler.schedule_audit_cleanup = MagicMock()
    scheduler.schedule_daily_backup = MagicMock()
    scheduler.schedule_water_balance_recalc = MagicMock()

    scheduler.start()

    assert [event[0] for event in events[:2]] == ["heartbeat", "start"]
    _, args, kwargs = events[0]
    assert args[0].__self__ is scheduler
    assert args[0].__name__ == "_record_heartbeat"
    assert kwargs["id"] == "scheduler_heartbeat"
    assert kwargs["jobstore"] == "volatile"
    assert kwargs["replace_existing"] is True
    assert kwargs["next_run_time"].tzinfo is apscheduler.timezone
    assert kwargs["next_run_time"] <= datetime.now(apscheduler.timezone)
    assert kwargs["trigger"].interval.total_seconds() == 30


def test_start_resets_heartbeat_grace_window(test_db, monkeypatch):
    scheduler, clock = _make_scheduler(test_db, monkeypatch)
    scheduler.scheduler = MagicMock()
    scheduler.scheduler.timezone = ZoneInfo("UTC")
    scheduler.schedule_postpone_sweeper = MagicMock()
    scheduler.schedule_audit_cleanup = MagicMock()
    scheduler.schedule_daily_backup = MagicMock()
    scheduler.schedule_water_balance_recalc = MagicMock()
    clock[0] = 250.0

    scheduler.start()

    assert scheduler._last_heartbeat_monotonic == 250.0


def test_real_started_scheduler_has_live_volatile_heartbeat(test_db):
    from irrigation_scheduler import IrrigationScheduler

    scheduler = IrrigationScheduler(test_db)
    scheduler.start()
    try:
        health = scheduler.health_status()
        heartbeat_job = scheduler.scheduler.get_job("scheduler_heartbeat", jobstore="volatile")

        assert heartbeat_job is not None
        assert health["healthy"] is True
        assert health["apscheduler_running"] is True
        assert health["background_thread_alive"] is True
        assert health["heartbeat_stale"] is False
    finally:
        scheduler.stop()


def test_healthy_live_scheduler_reports_all_liveness_signals(test_db, monkeypatch):
    scheduler, clock = _make_scheduler(test_db, monkeypatch)
    _mark_apscheduler_running(scheduler)
    clock[0] = 129.0

    health = scheduler.health_status()

    assert health["healthy"] is True
    assert health["wrapper_running"] is True
    assert health["is_running"] is True
    assert health["apscheduler_state"] == STATE_RUNNING
    assert health["apscheduler_running"] is True
    assert health["background_thread_alive"] is True
    assert health["heartbeat_age_seconds"] == 29.0
    assert health["heartbeat_stale"] is False
    assert health["volatile_jobstore_available"] is True
    assert health["reason"] is None
    assert health["reasons"] == []


def test_scheduler_health_fails_when_heartbeat_is_stale_after_grace(test_db, monkeypatch):
    scheduler, clock = _make_scheduler(test_db, monkeypatch)
    _mark_apscheduler_running(scheduler)
    clock[0] = 190.001

    health = scheduler.health_status()

    assert health["healthy"] is False
    assert health["heartbeat_age_seconds"] == pytest.approx(90.001)
    assert health["heartbeat_stale"] is True
    assert health["reason"] == "heartbeat_stale"
    assert health["reasons"] == ["heartbeat_stale"]


def test_scheduler_health_fails_for_dead_thread_while_wrapper_flag_is_true(test_db, monkeypatch):
    scheduler, _ = _make_scheduler(test_db, monkeypatch)
    _mark_apscheduler_running(scheduler)
    scheduler.scheduler._thread.is_alive.return_value = False

    health = scheduler.health_status()

    assert health["is_running"] is True
    assert health["apscheduler_running"] is True
    assert health["background_thread_alive"] is False
    assert health["healthy"] is False
    assert health["reason"] == "background_thread_dead"
    assert health["reasons"] == ["background_thread_dead"]


def test_scheduler_health_fails_when_apscheduler_is_paused(test_db, monkeypatch):
    scheduler, _ = _make_scheduler(test_db, monkeypatch)
    _mark_apscheduler_running(scheduler)
    scheduler.scheduler.state = STATE_PAUSED

    health = scheduler.health_status()

    assert health["is_running"] is True
    assert health["apscheduler_state"] == STATE_PAUSED
    assert health["apscheduler_running"] is False
    assert health["healthy"] is False
    assert health["reason"] == "apscheduler_not_running"
    assert health["reasons"] == ["apscheduler_not_running"]


def test_scheduler_health_fails_when_volatile_jobstore_disappears(test_db, monkeypatch):
    scheduler, _ = _make_scheduler(test_db, monkeypatch)
    _mark_apscheduler_running(scheduler)
    scheduler.scheduler._jobstores.pop("volatile")

    health = scheduler.health_status()

    assert health["volatile_jobstore_available"] is False
    assert health["healthy"] is False
    assert health["reason"] == "volatile_jobstore_unavailable"
    assert health["reasons"] == ["volatile_jobstore_unavailable"]


def test_heartbeat_records_only_monotonic_in_memory_progress(test_db, monkeypatch):
    scheduler, clock = _make_scheduler(test_db, monkeypatch)
    scheduler.db = MagicMock()
    clock[0] = 123.5

    scheduler._record_heartbeat()

    assert scheduler._last_heartbeat_monotonic == 123.5
    scheduler.db.assert_not_called()
    assert scheduler.db.method_calls == []


def test_readyz_scheduler_check_uses_central_fail_closed_snapshot(client):
    health_api = sys.modules["routes.health_api"]
    fake_scheduler = MagicMock(is_running=True)
    fake_scheduler.health_status.return_value = {
        "healthy": False,
        "wrapper_running": True,
        "is_running": True,
        "apscheduler_state": STATE_RUNNING,
        "apscheduler_running": True,
        "background_thread_alive": False,
        "heartbeat_age_seconds": 3.0,
        "heartbeat_stale": False,
        "volatile_jobstore_available": True,
        "reason": "background_thread_dead",
        "reasons": ["background_thread_dead"],
    }

    with patch("irrigation_scheduler.get_scheduler", return_value=fake_scheduler):
        result = health_api._check_scheduler()

    assert result["status"] == "fail"
    assert result["reason"] == "background_thread_dead"
    assert result["reasons"] == ["background_thread_dead"]
    assert result["background_thread_alive"] is False
    assert result["is_running"] is True
    fake_scheduler.health_status.assert_called_once_with()


def test_system_health_returns_503_for_dead_scheduler_thread(client):
    system_status_api = sys.modules["routes.system_status_api"]
    fake_scheduler = MagicMock(is_running=True)
    snapshot = {
        "healthy": False,
        "wrapper_running": True,
        "is_running": True,
        "apscheduler_state": STATE_RUNNING,
        "apscheduler_running": True,
        "background_thread_alive": False,
        "heartbeat_age_seconds": 2.0,
        "heartbeat_stale": False,
        "volatile_jobstore_available": True,
        "reason": "background_thread_dead",
        "reasons": ["background_thread_dead"],
    }
    fake_scheduler.health_status.return_value = snapshot

    with patch.object(system_status_api, "get_scheduler", return_value=fake_scheduler):
        response = client.get("/health")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["scheduler"] is False
    assert payload["scheduler_health"] == snapshot
    fake_scheduler.health_status.assert_called_once_with()


@pytest.mark.parametrize(
    ("reason", "thread_alive", "heartbeat_stale"),
    [
        ("background_thread_dead", False, False),
        ("heartbeat_stale", True, True),
    ],
)
def test_readyz_returns_503_for_scheduler_liveness_failure(
    client,
    reason,
    thread_alive,
    heartbeat_stale,
):
    health_api = sys.modules["routes.health_api"]
    fake_scheduler = MagicMock(is_running=True)
    fake_scheduler.health_status.return_value = {
        "healthy": False,
        "wrapper_running": True,
        "is_running": True,
        "apscheduler_state": STATE_RUNNING,
        "apscheduler_running": True,
        "background_thread_alive": thread_alive,
        "heartbeat_age_seconds": 91.0 if heartbeat_stale else 2.0,
        "heartbeat_stale": heartbeat_stale,
        "volatile_jobstore_available": True,
        "reason": reason,
        "reasons": [reason],
    }

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=fake_scheduler),
        patch.multiple(
            health_api,
            _check_boot_reconcile=lambda: {"status": "ok"},
            _check_disk_space=lambda min_free_mb=50: {"status": "ok", "free_mb": 1000},
            _check_mqtt=lambda db: {"status": "skipped", "brokers": 0},
            _check_db=lambda db_path="irrigation.db": {"status": "ok"},
        ),
    ):
        response = client.get("/readyz")

    assert response.status_code == 503
    scheduler_check = response.get_json()["checks"]["scheduler"]
    assert scheduler_check["status"] == "fail"
    assert scheduler_check["reason"] == reason


def test_scheduler_status_keeps_compatibility_fields_and_adds_liveness(client):
    system_status_api = sys.modules["routes.system_status_api"]
    fake_scheduler = MagicMock(is_running=True)
    fake_scheduler.get_active_programs.return_value = {7: {"job_ids": ["program:7"]}}
    fake_scheduler.get_active_zones.return_value = {3: datetime(2026, 7, 27, 8, 0, 0)}
    fake_scheduler.health_status.return_value = {
        "healthy": False,
        "wrapper_running": True,
        "is_running": True,
        "apscheduler_state": STATE_RUNNING,
        "apscheduler_running": True,
        "background_thread_alive": False,
        "heartbeat_age_seconds": 4.0,
        "heartbeat_stale": False,
        "volatile_jobstore_available": True,
        "reason": "background_thread_dead",
        "reasons": ["background_thread_dead"],
    }

    with patch.object(system_status_api, "get_scheduler", return_value=fake_scheduler):
        response = client.get("/api/scheduler/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_programs"] == {"7": {"job_ids": ["program:7"]}}
    assert payload["active_zones"] == {"3": "2026-07-27T08:00:00"}
    assert payload["is_running"] is True
    assert payload["healthy"] is False
    assert payload["background_thread_alive"] is False
    assert payload["heartbeat_age_seconds"] == 4.0
    assert payload["heartbeat_stale"] is False
    assert payload["reason"] == "background_thread_dead"
    assert payload["reasons"] == ["background_thread_dead"]
    fake_scheduler.health_status.assert_called_once_with()


def test_metrics_scheduler_gauges_use_real_liveness(client):
    health_api = sys.modules["routes.health_api"]
    apscheduler = SimpleNamespace(get_jobs=MagicMock(return_value=[object(), object()]))
    health_status = MagicMock(return_value={"healthy": False})
    fake_scheduler = SimpleNamespace(scheduler=apscheduler, health_status=health_status)

    with (
        patch("irrigation_scheduler.get_scheduler", return_value=fake_scheduler),
        patch.object(health_api, "WB_SCHEDULER_JOBS") as jobs_gauge,
        patch.object(health_api, "WB_SCHEDULER_RUNNING") as running_gauge,
    ):
        response = client.get("/metrics")

    assert response.status_code == 200
    jobs_gauge.set.assert_called_once_with(2)
    running_gauge.set.assert_called_once_with(0)
    health_status.assert_called_once_with()
