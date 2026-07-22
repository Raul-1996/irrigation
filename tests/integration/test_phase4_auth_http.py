"""Phase 4 regressions for HTTP safety, streaming and runtime environment."""

from __future__ import annotations

import asyncio
import os
import queue
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from werkzeug.security import check_password_hash


def test_correlation_reset_is_idempotent():
    from services.correlation import correlation_id_var, reset_correlation_id

    token = correlation_id_var.set("phase4-correlation")
    reset_correlation_id(token)
    reset_correlation_id(token)
    assert correlation_id_var.get() is None


def test_session_cookie_secure_environment_override_is_applied(app, monkeypatch):
    from app import _configure_session_cookie_secure

    app.config["SESSION_COOKIE_SECURE"] = False
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "1")
    _configure_session_cookie_secure(app)
    assert app.config["SESSION_COOKIE_SECURE"] is True

    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    _configure_session_cookie_secure(app)
    assert app.config["SESSION_COOKIE_SECURE"] is False


def test_session_cookie_secure_override_reaches_set_cookie(app, monkeypatch):
    from app import _configure_session_cookie_secure

    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    _configure_session_cookie_secure(app)
    response = app.test_client().get("/login?guest=1")
    assert "Secure" in response.headers.get("Set-Cookie", "")


@pytest.mark.parametrize(
    "path",
    [
        "/api/zones/1/start",
        "/api/zones/1/mqtt/start",
        "/api/groups/1/start-from-first",
        "/api/groups/1/start-zone/2",
        "/api/groups/1/master-valve/open",
        "/api/emergency-resume",
    ],
)
def test_authenticated_unsafe_physical_actions_require_csrf(admin_client, app, path):
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = admin_client.post(path, content_type="application/json")
        assert response.status_code == 400
        assert "CSRF token is missing" in response.get_data(as_text=True)
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_guest_cannot_cancel_postpone_or_clear_database_state(guest_client, app):
    group = app.db.create_group("protected postpone cancel")
    zone = app.db.create_zone({"name": "postponed", "duration": 5, "group_id": group["id"]})
    postpone_until = "2099-12-31 23:59:59"
    app.db.update_zone_postpone(zone["id"], postpone_until, "manual")
    app.db.set_setting_value("password_must_change", "0")
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
    try:
        response = guest_client.post(
            "/api/postpone",
            json={"group_id": group["id"], "action": "cancel"},
        )
        assert response.status_code == 401
        assert response.get_json()["error_code"] == "UNAUTHENTICATED"
        assert app.db.get_zone(zone["id"])["postpone_until"] == postpone_until
    finally:
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)


def test_guest_can_still_apply_fail_safe_postpone(guest_client, app, monkeypatch):
    import irrigation_scheduler

    group = app.db.create_group("public fail-safe postpone")
    zone = app.db.create_zone({"name": "delay me", "duration": 5, "group_id": group["id"]})
    scheduler = Mock()
    scheduler.cancel_group_jobs.return_value = {
        "success": True,
        "aggregate_valid": True,
        "stopped": [zone["id"]],
        "unresolved": [],
        "unverified_zone_ids": [],
        "retry_scheduled": False,
        "group_id": group["id"],
    }
    monkeypatch.setattr(irrigation_scheduler, "get_scheduler", lambda: scheduler)
    app.db.set_setting_value("password_must_change", "0")
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
    try:
        response = guest_client.post(
            "/api/postpone",
            json={"group_id": group["id"], "days": 1, "action": "postpone"},
        )
        assert response.status_code == 200
        assert app.db.get_zone(zone["id"])["postpone_until"] is not None
    finally:
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)


def test_public_postpone_never_shortens_existing_safety_deadline(guest_client, app):
    from services.api_rate_limiter import reset_all

    group = app.db.create_group("monotonic public postpone")
    zone = app.db.create_zone({"name": "already protected", "duration": 5, "group_id": group["id"]})
    unpostponed_zone = app.db.create_zone({"name": "must remain unchanged", "duration": 5, "group_id": group["id"]})
    postpone_until = "2099-12-31 23:59:59"
    app.db.update_zone_postpone(zone["id"], postpone_until, "rain")
    app.db.set_setting_value("password_must_change", "0")
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
    reset_all()
    try:
        response = guest_client.post(
            "/api/postpone",
            json={"group_id": group["id"], "days": 1, "action": "postpone"},
        )
        assert response.status_code == 409
        assert response.get_json()["error_code"] == "POSTPONE_WOULD_SHORTEN"
        assert app.db.get_zone(zone["id"])["postpone_until"] == postpone_until
        assert app.db.get_zone(zone["id"])["postpone_reason"] == "rain"
        assert app.db.get_zone(unpostponed_zone["id"])["postpone_until"] is None
    finally:
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        reset_all()


@pytest.mark.parametrize("days", [-1, 0, "1", "not-an-integer", 1.5, True, 4, 366])
def test_public_postpone_requires_bounded_positive_integer_days(guest_client, app, days):
    from services.api_rate_limiter import reset_all

    group = app.db.create_group(f"strict postpone days {days!r}")
    zone = app.db.create_zone({"name": "unchanged", "duration": 5, "group_id": group["id"]})
    app.db.set_setting_value("password_must_change", "0")
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
    reset_all()
    try:
        response = guest_client.post(
            "/api/postpone",
            json={"group_id": group["id"], "days": days, "action": "postpone"},
        )
        assert response.status_code == 400
        assert response.get_json()["error_code"] == "INVALID_POSTPONE_DAYS"
        assert app.db.get_zone(zone["id"])["postpone_until"] is None
    finally:
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        reset_all()


@pytest.mark.parametrize("stored_deadline", ["", "   ", "not-a-date"])
def test_public_postpone_fails_closed_on_malformed_stored_deadline(guest_client, app, stored_deadline):
    from services.api_rate_limiter import reset_all

    group = app.db.create_group(f"malformed postpone {stored_deadline!r}")
    malformed_zone = app.db.create_zone({"name": "malformed", "duration": 5, "group_id": group["id"]})
    unset_zone = app.db.create_zone({"name": "unset", "duration": 5, "group_id": group["id"]})
    app.db.update_zone_postpone(malformed_zone["id"], stored_deadline, "rain")
    app.db.set_setting_value("password_must_change", "0")
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
    reset_all()
    try:
        response = guest_client.post(
            "/api/postpone",
            json={"group_id": group["id"], "days": 3, "action": "postpone"},
        )
        assert response.status_code == 409
        assert response.get_json()["error_code"] == "POSTPONE_INVALID_EXISTING"
        assert app.db.get_zone(malformed_zone["id"])["postpone_until"] == stored_deadline
        assert app.db.get_zone(malformed_zone["id"])["postpone_reason"] == "rain"
        assert app.db.get_zone(unset_zone["id"])["postpone_until"] is None
    finally:
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        reset_all()


def test_concurrent_public_and_telegram_postpone_is_monotonic(app, monkeypatch):
    import services.postpone as postpone_service

    group = app.db.create_group("concurrent channel postpone")
    zone = app.db.create_zone({"name": "serialized", "duration": 5, "group_id": group["id"]})
    original_get_zones = postpone_service.db.get_zones
    original_update = postpone_service.db.update_zone_postpone
    public_snapshot = threading.Event()
    telegram_calling = threading.Event()
    telegram_snapshot = threading.Event()
    public_written = threading.Event()
    results: dict[str, dict] = {}
    errors: dict[str, BaseException] = {}

    def controlled_get_zones(*args, **kwargs):
        snapshot = original_get_zones(*args, **kwargs)
        thread_name = threading.current_thread().name
        if thread_name == "public-postpone":
            public_snapshot.set()
            assert telegram_calling.wait(1.0)
        elif thread_name == "telegram-postpone":
            telegram_snapshot.set()
        return snapshot

    def controlled_update(zone_id, postpone_until=None, reason=None):
        thread_name = threading.current_thread().name
        if thread_name == "public-postpone":
            # Without one shared service lock, Telegram takes the same stale
            # snapshot and then writes its shorter deadline last.
            telegram_snapshot.wait(0.25)
            result = original_update(zone_id, postpone_until, reason)
            public_written.set()
            return result
        if thread_name == "telegram-postpone":
            assert public_written.wait(1.0)
        return original_update(zone_id, postpone_until, reason)

    monkeypatch.setattr(postpone_service.db, "get_zones", controlled_get_zones)
    monkeypatch.setattr(postpone_service.db, "update_zone_postpone", controlled_update)

    def invoke(channel: str, days: int):
        if channel == "telegram":
            telegram_calling.set()
        try:
            results[channel] = postpone_service.postpone_group(group["id"], days, source=channel)
        except BaseException as exc:  # captured for deterministic thread assertions
            errors[channel] = exc

    public_thread = threading.Thread(target=invoke, args=("public", 3), name="public-postpone")
    public_thread.start()
    assert public_snapshot.wait(1.0)
    telegram_thread = threading.Thread(target=invoke, args=("telegram", 1), name="telegram-postpone")
    telegram_thread.start()
    public_thread.join(timeout=2.0)
    telegram_thread.join(timeout=2.0)

    assert not public_thread.is_alive()
    assert not telegram_thread.is_alive()
    assert "public" in results
    assert isinstance(errors.get("telegram"), postpone_service.PostponeWouldShortenError)
    expected = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d 23:59:59")
    persisted = app.db.get_zone(zone["id"])
    assert persisted["postpone_until"] == expected
    assert persisted["postpone_reason"] == "manual"


def test_expired_postpone_cas_preserves_concurrent_longer_deadline(app):
    from services.postpone import clear_zone_postpone_if_expired

    group = app.db.create_group("postpone CAS preserve")
    zone = app.db.create_zone({"name": "CAS protected", "duration": 5, "group_id": group["id"]})
    observed = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
    longer = "2099-12-31 23:59:59"
    app.db.update_zone_postpone(zone["id"], observed, "manual")
    app.db.update_zone_postpone(zone["id"], longer, "rain")

    cleared = clear_zone_postpone_if_expired(
        zone["id"],
        observed,
        datetime.now(),
        db_facade=app.db,
    )

    assert cleared is False
    persisted = app.db.get_zone(zone["id"])
    assert persisted["postpone_until"] == longer
    assert persisted["postpone_reason"] == "rain"


@pytest.mark.parametrize("stored_deadline", [None, "", "   ", "not-a-date"])
def test_expired_postpone_cas_fails_closed_on_unset_or_invalid_deadline(app, stored_deadline):
    from services.postpone import clear_zone_postpone_if_expired

    group = app.db.create_group(f"postpone CAS invalid {stored_deadline!r}")
    zone = app.db.create_zone({"name": "CAS invalid", "duration": 5, "group_id": group["id"]})
    app.db.update_zone_postpone(zone["id"], stored_deadline, "rain")

    cleared = clear_zone_postpone_if_expired(
        zone["id"],
        stored_deadline,
        datetime.now(),
        db_facade=app.db,
    )

    assert cleared is False
    persisted = app.db.get_zone(zone["id"])
    assert persisted["postpone_until"] == stored_deadline
    assert persisted["postpone_reason"] == "rain"


def test_apply_and_expired_clear_share_one_postpone_lock(app, monkeypatch):
    import services.postpone as postpone_service

    group = app.db.create_group("postpone apply-clear serialization")
    zone = app.db.create_zone({"name": "serialized CAS", "duration": 5, "group_id": group["id"]})
    observed = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
    app.db.update_zone_postpone(zone["id"], observed, "rain")
    apply_snapshot = threading.Event()
    clear_calling = threading.Event()
    original_get_zones = postpone_service.db.get_zones
    results: dict[str, object] = {}

    def controlled_get_zones(*args, **kwargs):
        snapshot = original_get_zones(*args, **kwargs)
        if threading.current_thread().name == "postpone-apply":
            apply_snapshot.set()
            assert clear_calling.wait(1.0)
        return snapshot

    monkeypatch.setattr(postpone_service.db, "get_zones", controlled_get_zones)

    def apply_deadline():
        results["apply"] = postpone_service.postpone_group(group["id"], 3, source="api")

    def clear_expired():
        clear_calling.set()
        results["clear"] = postpone_service.clear_zone_postpone_if_expired(
            zone["id"],
            observed,
            datetime.now(),
            db_facade=app.db,
        )

    apply_thread = threading.Thread(target=apply_deadline, name="postpone-apply")
    apply_thread.start()
    assert apply_snapshot.wait(1.0)
    clear_thread = threading.Thread(target=clear_expired, name="postpone-clear")
    clear_thread.start()
    apply_thread.join(timeout=2.0)
    clear_thread.join(timeout=2.0)

    assert not apply_thread.is_alive()
    assert not clear_thread.is_alive()
    assert "apply" in results
    assert results["clear"] is False
    expected = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d 23:59:59")
    persisted = app.db.get_zone(zone["id"])
    assert persisted["postpone_until"] == expected
    assert persisted["postpone_reason"] == "manual"


def test_rain_postpone_claims_only_completely_unowned_zones(app):
    from services.postpone import apply_group_rain_postpone

    group = app.db.create_group("rain ownership apply")
    unowned = app.db.create_zone({"name": "unowned", "duration": 5, "group_id": group["id"]})
    manual = app.db.create_zone({"name": "manual", "duration": 5, "group_id": group["id"]})
    existing_rain = app.db.create_zone({"name": "rain", "duration": 5, "group_id": group["id"]})
    reason_only = app.db.create_zone({"name": "reason only", "duration": 5, "group_id": group["id"]})
    manual_deadline = "2099-12-31 23:59:59"
    rain_deadline = "2026-07-20 23:59:59"
    requested = "2026-07-21 23:59:59"
    app.db.update_zone_postpone(manual["id"], manual_deadline, "manual")
    app.db.update_zone_postpone(existing_rain["id"], rain_deadline, "rain")
    app.db.update_zone_postpone(reason_only["id"], None, "foreign")

    result = apply_group_rain_postpone(group["id"], requested, db_facade=app.db)

    assert result is not False
    assert result["updated_zone_ids"] == [unowned["id"]]
    assert app.db.get_zone(unowned["id"])["postpone_until"] == requested
    assert app.db.get_zone(unowned["id"])["postpone_reason"] == "rain"
    assert app.db.get_zone(manual["id"])["postpone_until"] == manual_deadline
    assert app.db.get_zone(manual["id"])["postpone_reason"] == "manual"
    assert app.db.get_zone(existing_rain["id"])["postpone_until"] == rain_deadline
    assert app.db.get_zone(existing_rain["id"])["postpone_reason"] == "rain"
    assert app.db.get_zone(reason_only["id"])["postpone_until"] is None
    assert app.db.get_zone(reason_only["id"])["postpone_reason"] == "foreign"


def test_rain_postpone_apply_rolls_back_complete_group_on_write_failure(app):
    from services.postpone import apply_group_rain_postpone

    group = app.db.create_group("rain ownership apply rollback")
    first = app.db.create_zone({"name": "first", "duration": 5, "group_id": group["id"]})
    second = app.db.create_zone({"name": "second", "duration": 5, "group_id": group["id"]})
    requested = "2026-07-21 23:59:59"
    with sqlite3.connect(app.db.db_path) as conn:
        conn.execute(
            f"""
            CREATE TRIGGER reject_second_rain_postpone
            BEFORE UPDATE OF postpone_until ON zones
            WHEN OLD.id = {int(second["id"])} AND NEW.postpone_reason = 'rain'
            BEGIN
                SELECT RAISE(ABORT, 'forced rain apply failure');
            END
            """
        )

    result = apply_group_rain_postpone(group["id"], requested, db_facade=app.db)

    assert result is False
    assert app.db.get_zone(first["id"])["postpone_until"] is None
    assert app.db.get_zone(first["id"])["postpone_reason"] is None
    assert app.db.get_zone(second["id"])["postpone_until"] is None
    assert app.db.get_zone(second["id"])["postpone_reason"] is None


def test_clear_group_rain_postpone_clears_only_current_rain_owners(app):
    from services.postpone import clear_group_rain_postpone

    group = app.db.create_group("rain ownership clear")
    rain = app.db.create_zone({"name": "rain", "duration": 5, "group_id": group["id"]})
    manual = app.db.create_zone({"name": "manual", "duration": 5, "group_id": group["id"]})
    foreign = app.db.create_zone({"name": "foreign", "duration": 5, "group_id": group["id"]})
    deadline = "2099-12-31 23:59:59"
    app.db.update_zone_postpone(rain["id"], deadline, "rain")
    app.db.update_zone_postpone(manual["id"], deadline, "manual")
    app.db.update_zone_postpone(foreign["id"], deadline, "weather-v2")

    assert clear_group_rain_postpone(group["id"], db_facade=app.db) is True

    assert app.db.get_zone(rain["id"])["postpone_until"] is None
    assert app.db.get_zone(rain["id"])["postpone_reason"] is None
    assert app.db.get_zone(manual["id"])["postpone_until"] == deadline
    assert app.db.get_zone(manual["id"])["postpone_reason"] == "manual"
    assert app.db.get_zone(foreign["id"])["postpone_until"] == deadline
    assert app.db.get_zone(foreign["id"])["postpone_reason"] == "weather-v2"


def test_clear_group_rain_postpone_rolls_back_on_write_failure(app):
    from services.postpone import clear_group_rain_postpone

    group = app.db.create_group("rain ownership clear rollback")
    first = app.db.create_zone({"name": "first", "duration": 5, "group_id": group["id"]})
    second = app.db.create_zone({"name": "second", "duration": 5, "group_id": group["id"]})
    deadline = "2099-12-31 23:59:59"
    app.db.update_zone_postpone(first["id"], deadline, "rain")
    app.db.update_zone_postpone(second["id"], deadline, "rain")
    with sqlite3.connect(app.db.db_path) as conn:
        conn.execute(
            f"""
            CREATE TRIGGER reject_second_rain_clear
            BEFORE UPDATE OF postpone_until ON zones
            WHEN OLD.id = {int(second["id"])} AND OLD.postpone_reason = 'rain'
                 AND NEW.postpone_until IS NULL
            BEGIN
                SELECT RAISE(ABORT, 'forced rain clear failure');
            END
            """
        )

    assert clear_group_rain_postpone(group["id"], db_facade=app.db) is False

    assert app.db.get_zone(first["id"])["postpone_until"] == deadline
    assert app.db.get_zone(first["id"])["postpone_reason"] == "rain"
    assert app.db.get_zone(second["id"])["postpone_until"] == deadline
    assert app.db.get_zone(second["id"])["postpone_reason"] == "rain"


def test_equal_manual_postpone_claims_rain_provenance_before_dry_clear(app):
    from services.postpone import (
        apply_group_postpone_deadline,
        clear_group_rain_postpone,
    )

    group = app.db.create_group("manual equal rain ownership")
    zone = app.db.create_zone({"name": "claimed by operator", "duration": 5, "group_id": group["id"]})
    deadline = "2099-12-31 23:59:59"
    app.db.update_zone_postpone(zone["id"], deadline, "rain")

    result = apply_group_postpone_deadline(group["id"], deadline, reason="manual")

    assert result["updated_zone_ids"] == [zone["id"]]
    claimed = app.db.get_zone(zone["id"])
    assert claimed["postpone_until"] == deadline
    assert claimed["postpone_reason"] == "manual"
    assert clear_group_rain_postpone(group["id"], db_facade=app.db) is True
    preserved = app.db.get_zone(zone["id"])
    assert preserved["postpone_until"] == deadline
    assert preserved["postpone_reason"] == "manual"


def test_postpone_scheduler_unresolved_is_pending_without_raw_fallback(app, monkeypatch):
    import irrigation_scheduler
    import services.zone_control as zone_control
    from services.postpone import postpone_group

    group = app.db.create_group("postpone structured unresolved")
    zone = app.db.create_zone({"name": "physically unknown", "duration": 5, "group_id": group["id"]})
    started_at = "2026-07-19 10:00:00"
    command_id = "postpone-unresolved-command"
    app.db.update_zone_versioned(
        zone["id"],
        {
            "state": "on",
            "commanded_state": "on",
            "observed_state": "on",
            "watering_start_time": started_at,
            "command_id": command_id,
        },
        expected_version=zone["version"],
    )
    scheduler = Mock()
    scheduler.cancel_group_jobs.return_value = {
        "success": False,
        "aggregate_valid": True,
        "stopped": [],
        "unresolved": [zone["id"]],
        "unverified_zone_ids": [],
        "retry_scheduled": True,
        "group_id": group["id"],
    }
    raw_fallback = Mock()
    monkeypatch.setattr(irrigation_scheduler, "get_scheduler", lambda: scheduler)
    monkeypatch.setattr(zone_control, "stop_all_in_group", raw_fallback)

    result = postpone_group(group["id"], 1, source="api")

    assert result["success"] is False
    assert result["pending"] is True
    assert result["error_code"] == "POSTPONE_STOP_PENDING"
    assert result["unresolved"] == [zone["id"]]
    assert result["retry_scheduled"] is True
    scheduler.cancel_group_jobs.assert_called_once_with(group["id"], master_close_immediately=True)
    raw_fallback.assert_not_called()
    persisted = app.db.get_zone(zone["id"])
    assert persisted["postpone_until"] == result["postpone_until"]
    assert persisted["postpone_reason"] == "manual"
    assert persisted["watering_start_time"] == started_at
    assert persisted["command_id"] == command_id


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "overlap",
        "foreign",
        "contradictory_success",
        "duplicates",
        "legacy_shape",
        "extra_field",
        "coerced_zone_id",
        "wrong_group",
        "non_bool_retry",
        "tuple_bucket",
        "retry_without_unresolved",
    ],
)
def test_postpone_rejects_non_exact_scheduler_aggregate_without_inventing_buckets(app, monkeypatch, case):
    import irrigation_scheduler
    import services.zone_control as zone_control
    from services.postpone import postpone_group

    group = app.db.create_group(f"strict postpone aggregate {case}")
    first = app.db.create_zone({"name": "first", "duration": 5, "group_id": group["id"]})
    second = app.db.create_zone({"name": "second", "duration": 5, "group_id": group["id"]})
    result = {
        "success": True,
        "aggregate_valid": True,
        "stopped": [first["id"], second["id"]],
        "unresolved": [],
        "unverified_zone_ids": [],
        "retry_scheduled": False,
        "group_id": group["id"],
    }
    if case == "missing":
        result["stopped"] = [first["id"]]
    elif case == "overlap":
        result.update(success=False, unresolved=[second["id"]], retry_scheduled=True)
    elif case == "foreign":
        result["stopped"] = [first["id"], second["id"], 999_999]
    elif case == "contradictory_success":
        result.update(stopped=[first["id"]], unresolved=[second["id"]])
    elif case == "duplicates":
        result["stopped"] = [first["id"], first["id"], second["id"]]
    elif case == "legacy_shape":
        result.pop("aggregate_valid")
        result.pop("unverified_zone_ids")
    elif case == "extra_field":
        result["physical_guess"] = "off"
    elif case == "coerced_zone_id":
        result["stopped"] = [str(first["id"]), second["id"]]
    elif case == "wrong_group":
        result["group_id"] = group["id"] + 1
    elif case == "non_bool_retry":
        result["retry_scheduled"] = 0
    elif case == "tuple_bucket":
        result["stopped"] = tuple(result["stopped"])
    elif case == "retry_without_unresolved":
        result["retry_scheduled"] = True

    scheduler = Mock()
    scheduler.cancel_group_jobs.return_value = result
    raw_fallback = Mock()
    monkeypatch.setattr(irrigation_scheduler, "get_scheduler", lambda: scheduler)
    monkeypatch.setattr(zone_control, "stop_all_in_group", raw_fallback)

    normalized = postpone_group(group["id"], 1, source="api")

    assert normalized["success"] is False
    assert normalized["aggregate_valid"] is False
    assert normalized["error_code"] == "POSTPONE_STOP_UNAVAILABLE"
    assert normalized["stopped"] == []
    assert normalized["unresolved"] == []
    assert normalized["unverified_zone_ids"] == [first["id"], second["id"]]
    assert normalized["retry_scheduled"] is False
    raw_fallback.assert_not_called()


def test_postpone_preserves_valid_unresolved_without_claiming_retry_owner(app, monkeypatch):
    import irrigation_scheduler
    from services.postpone import postpone_group

    group = app.db.create_group("postpone without retry owner")
    first = app.db.create_zone({"name": "confirmed", "duration": 5, "group_id": group["id"]})
    second = app.db.create_zone({"name": "unresolved", "duration": 5, "group_id": group["id"]})
    scheduler = Mock()
    scheduler.cancel_group_jobs.return_value = {
        "success": False,
        "aggregate_valid": True,
        "stopped": [first["id"]],
        "unresolved": [second["id"]],
        "unverified_zone_ids": [],
        "retry_scheduled": False,
        "group_id": group["id"],
    }
    monkeypatch.setattr(irrigation_scheduler, "get_scheduler", lambda: scheduler)

    result = postpone_group(group["id"], 1, source="api")

    assert result["success"] is False
    assert result["aggregate_valid"] is True
    assert result["stopped"] == [first["id"]]
    assert result["unresolved"] == [second["id"]]
    assert result["unverified_zone_ids"] == []
    assert result["retry_scheduled"] is False


def test_postpone_rejects_core_retry_ownership_claim(app, monkeypatch):
    import irrigation_scheduler
    import services.zone_control as zone_control
    from services.postpone import postpone_group

    group = app.db.create_group("core cannot own postpone retry")
    zone = app.db.create_zone({"name": "unverified core retry", "duration": 5, "group_id": group["id"]})
    central_stop = Mock(
        return_value={
            "success": False,
            "group_id": group["id"],
            "stopped": [],
            "unresolved": [zone["id"]],
            "retry_scheduled": True,
        }
    )
    monkeypatch.setattr(irrigation_scheduler, "get_scheduler", lambda: None)
    monkeypatch.setattr(irrigation_scheduler, "quiesce_group_session", lambda group_id: True)
    monkeypatch.setattr(zone_control, "stop_all_in_group", central_stop)

    result = postpone_group(group["id"], 1, source="api")

    assert result["success"] is False
    assert result["aggregate_valid"] is False
    assert result["error_code"] == "POSTPONE_STOP_UNAVAILABLE"
    assert result["stopped"] == []
    assert result["unresolved"] == []
    assert result["unverified_zone_ids"] == [zone["id"]]
    assert result["retry_scheduled"] is False
    central_stop.assert_called_once()


def test_postpone_accepts_core_unresolved_without_retry_claim(app, monkeypatch):
    import irrigation_scheduler
    import services.zone_control as zone_control
    from services.postpone import postpone_group

    group = app.db.create_group("core unresolved without retry")
    zone = app.db.create_zone({"name": "valid core unresolved", "duration": 5, "group_id": group["id"]})
    monkeypatch.setattr(irrigation_scheduler, "get_scheduler", lambda: None)
    monkeypatch.setattr(irrigation_scheduler, "quiesce_group_session", lambda group_id: True)
    monkeypatch.setattr(
        zone_control,
        "stop_all_in_group",
        lambda group_id, **kwargs: {
            "success": False,
            "group_id": group["id"],
            "stopped": [],
            "unresolved": [zone["id"]],
            "retry_scheduled": False,
        },
    )

    result = postpone_group(group["id"], 1, source="api")

    assert result["success"] is False
    assert result["aggregate_valid"] is True
    assert result["error_code"] == "POSTPONE_STOP_PENDING"
    assert result["stopped"] == []
    assert result["unresolved"] == [zone["id"]]
    assert result["unverified_zone_ids"] == []
    assert result["retry_scheduled"] is False


def test_postpone_without_scheduler_quiesces_session_before_confirmed_bulk_stop(app, monkeypatch):
    import irrigation_scheduler
    import services.zone_control as zone_control
    from services.postpone import postpone_group

    group = app.db.create_group("postpone safe fallback ordering")
    zone = app.db.create_zone({"name": "fallback", "duration": 5, "group_id": group["id"]})
    events: list[str] = []

    def quiesce_session(group_id):
        assert group_id == group["id"]
        events.append("quiesce-session")
        return True

    def confirmed_stop(group_id, **kwargs):
        assert group_id == group["id"]
        assert kwargs == {
            "reason": "postpone",
            "force": True,
            "master_close_immediately": True,
            "require_observed_confirmation": True,
        }
        events.append("confirmed-stop")
        return {
            "success": True,
            "stopped": [zone["id"]],
            "unresolved": [],
            "retry_scheduled": False,
            "group_id": group["id"],
        }

    monkeypatch.setattr(irrigation_scheduler, "get_scheduler", lambda: None)
    monkeypatch.setattr(irrigation_scheduler, "quiesce_group_session", quiesce_session, raising=False)
    monkeypatch.setattr(zone_control, "stop_all_in_group", confirmed_stop)

    result = postpone_group(group["id"], 1, source="telegram")

    assert result["success"] is True
    assert result["pending"] is False
    assert result["stopped"] == [zone["id"]]
    assert result["unresolved"] == []
    assert result["retry_scheduled"] is False
    assert events == ["quiesce-session", "confirmed-stop"]


def test_postpone_without_scheduler_still_calls_actual_confirmed_stop_and_returns_503(guest_client, app):
    import irrigation_scheduler
    import services.zone_control as zone_control
    from services.api_rate_limiter import reset_all

    assert irrigation_scheduler.get_scheduler() is None
    group = app.db.create_group("postpone real no-scheduler stop")
    zone = app.db.create_zone({"name": "must stop now", "duration": 5, "group_id": group["id"]})
    app.db.update_zone_versioned(
        zone["id"],
        {
            "state": "on",
            "commanded_state": "on",
            "observed_state": "on",
            "watering_start_time": "2026-07-19 10:00:00",
            "command_id": "no-scheduler-postpone",
        },
        expected_version=zone["version"],
    )
    app.db.set_setting_value("password_must_change", "0")
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
    reset_all()
    try:
        with patch.object(zone_control, "stop_all_in_group", wraps=zone_control.stop_all_in_group) as actual_stop:
            response = guest_client.post(
                "/api/postpone",
                json={"group_id": group["id"], "days": 1, "action": "postpone"},
            )
    finally:
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        reset_all()

    actual_stop.assert_called_once_with(
        group["id"],
        reason="postpone",
        force=True,
        master_close_immediately=True,
        require_observed_confirmation=True,
    )
    assert response.status_code == 503
    body = response.get_json()
    assert body["success"] is False
    assert body["pending"] is True
    assert body["error_code"] == "POSTPONE_SESSION_NOT_QUIESCED"
    assert body["aggregate_valid"] is True
    assert body["stopped"] == [zone["id"]]
    assert body["unresolved"] == []
    assert body["unverified_zone_ids"] == []
    assert body["retry_scheduled"] is False
    assert body["session_quiesced"] is False
    assert body["physical_stop_confirmed"] is True
    persisted = app.db.get_zone(zone["id"])
    assert persisted["state"] == "off"
    assert persisted["postpone_until"] == body["postpone_until"]


def test_postpone_api_returns_503_with_durable_deadline_when_stop_is_unresolved(guest_client, app, monkeypatch):
    import irrigation_scheduler
    from services.api_rate_limiter import reset_all

    group = app.db.create_group("postpone pending HTTP")
    zone = app.db.create_zone({"name": "pending HTTP", "duration": 5, "group_id": group["id"]})
    app.db.update_zone_versioned(
        zone["id"],
        {
            "state": "on",
            "commanded_state": "on",
            "observed_state": "on",
            "watering_start_time": "2026-07-19 10:00:00",
            "command_id": "pending-http-command",
        },
        expected_version=zone["version"],
    )
    scheduler = Mock()
    scheduler.cancel_group_jobs.return_value = {
        "success": False,
        "aggregate_valid": True,
        "stopped": [],
        "unresolved": [zone["id"]],
        "unverified_zone_ids": [],
        "retry_scheduled": True,
        "group_id": group["id"],
    }
    monkeypatch.setattr(irrigation_scheduler, "get_scheduler", lambda: scheduler)
    app.db.set_setting_value("password_must_change", "0")
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
    reset_all()
    try:
        response = guest_client.post(
            "/api/postpone",
            json={"group_id": group["id"], "days": 1, "action": "postpone"},
        )
    finally:
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        reset_all()

    assert response.status_code == 503
    body = response.get_json()
    assert body["success"] is False
    assert body["pending"] is True
    assert body["error_code"] == "POSTPONE_STOP_PENDING"
    assert body["unresolved"] == [zone["id"]]
    assert body["retry_scheduled"] is True
    assert app.db.get_zone(zone["id"])["postpone_until"] == body["postpone_until"]


def test_postpone_api_exposes_unverified_bucket_for_malformed_scheduler_evidence(guest_client, app, monkeypatch):
    import irrigation_scheduler
    from services.api_rate_limiter import reset_all

    group = app.db.create_group("postpone malformed aggregate HTTP")
    zone = app.db.create_zone({"name": "unverified HTTP", "duration": 5, "group_id": group["id"]})
    scheduler = Mock()
    scheduler.cancel_group_jobs.return_value = {
        "success": True,
        "aggregate_valid": True,
        # Missing the expected zone is not silently promoted to unresolved.
        "stopped": [],
        "unresolved": [],
        "unverified_zone_ids": [],
        "retry_scheduled": False,
        "group_id": group["id"],
    }
    monkeypatch.setattr(irrigation_scheduler, "get_scheduler", lambda: scheduler)
    app.db.set_setting_value("password_must_change", "0")
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
    reset_all()
    try:
        response = guest_client.post(
            "/api/postpone",
            json={"group_id": group["id"], "days": 1, "action": "postpone"},
        )
    finally:
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        reset_all()

    assert response.status_code == 503
    body = response.get_json()
    assert body["success"] is False
    assert body["error_code"] == "POSTPONE_STOP_UNAVAILABLE"
    assert body["aggregate_valid"] is False
    assert body["stopped"] == []
    assert body["unresolved"] == []
    assert body["unverified_zone_ids"] == [zone["id"]]
    assert body["retry_scheduled"] is False
    assert body["physical_stop_confirmed"] is False
    assert "результат физического подтверждения недоступен" in body["message"]


def test_postpone_ack_without_fresh_off_echo_keeps_activation_and_safety_jobs(guest_client, app, monkeypatch):
    import irrigation_scheduler
    import services.zone_control as zone_control
    from irrigation_scheduler import IrrigationScheduler
    from services.api_rate_limiter import reset_all

    group = app.db.create_group("postpone ACK without physical echo")
    server = app.db.create_mqtt_server(
        {
            "name": "postpone broker ACK",
            "host": "127.0.0.1",
            "port": 1883,
        }
    )
    zone = app.db.create_zone(
        {
            "name": "still physically unknown",
            "duration": 5,
            "group_id": group["id"],
            "mqtt_server_id": server["id"],
            "topic": "/zones/postpone-ack-only",
        }
    )
    started_at = "2026-07-19 10:00:00"
    command_id = "postpone-ack-only-command"
    app.db.update_zone_versioned(
        zone["id"],
        {
            "state": "on",
            "commanded_state": "on",
            "observed_state": "on",
            "watering_start_time": started_at,
            "command_id": command_id,
        },
        expected_version=zone["version"],
    )

    scheduler = IrrigationScheduler(app.db)
    hard_id = f"zone_hard_stop:{zone['id']}"
    cap_id = f"zone_cap_stop:{zone['id']}"
    assert scheduler.schedule_zone_hard_stop(
        zone["id"],
        datetime.now() + timedelta(minutes=5),
        activation_token=command_id,
    )
    scheduler.schedule_zone_cap(zone["id"], cap_minutes=60, activation_token=command_id)
    assert {hard_id, cap_id} <= {str(job.id) for job in scheduler.scheduler.get_jobs()}
    scheduler.active_zones[zone["id"]] = datetime.now() + timedelta(minutes=5)
    verifier = Mock()
    prepared = object()
    verifier.register_command.return_value = 101
    verifier.prepare_verification.return_value = prepared
    verifier.verify.return_value = False
    publish = Mock(return_value=True)
    monkeypatch.setattr(irrigation_scheduler, "get_scheduler", lambda: scheduler)

    app.db.set_setting_value("password_must_change", "0")
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
    reset_all()
    try:
        with (
            patch.object(zone_control, "db", app.db),
            patch.object(zone_control, "state_verifier", verifier),
            patch.object(zone_control, "publish_mqtt_value", publish),
            patch.object(zone_control, "water_monitor"),
        ):
            response = guest_client.post(
                "/api/postpone",
                json={"group_id": group["id"], "days": 1, "action": "postpone"},
            )
    finally:
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        reset_all()

    assert response.status_code == 503
    body = response.get_json()
    assert body["success"] is False
    assert body["pending"] is True
    assert body["error_code"] == "POSTPONE_STOP_PENDING"
    assert body["stopped"] == []
    assert body["unresolved"] == [zone["id"]]
    assert body["retry_scheduled"] is True
    publish.assert_called_once()
    assert publish.call_args.args[2] == "0"
    verifier.verify.assert_called_once_with(
        zone["id"],
        "off",
        generation=101,
        prepared=prepared,
    )

    persisted = app.db.get_zone(zone["id"])
    assert persisted["postpone_until"] == body["postpone_until"]
    assert persisted["postpone_reason"] == "manual"
    assert persisted["commanded_state"] == "off"
    assert persisted["observed_state"] == "unconfirmed"
    assert persisted["watering_start_time"] == started_at
    assert persisted["command_id"] == command_id
    jobs_by_id = {str(job.id): job for job in scheduler.scheduler.get_jobs()}
    assert {hard_id, cap_id} <= set(jobs_by_id)
    assert list(jobs_by_id[hard_id].args) == [zone["id"], command_id, True]
    assert list(jobs_by_id[cap_id].args) == [zone["id"], command_id, True]
    assert zone["id"] in scheduler.active_zones
    app.db.update_zone_versioned(
        zone["id"],
        {
            "state": "off",
            "commanded_state": "off",
            "observed_state": "off",
            "watering_start_time": None,
            "command_id": None,
            "mqtt_server_id": None,
            "topic": "",
        },
        expected_version=app.db.get_zone(zone["id"])["version"],
    )
    scheduler.cancel_zone_jobs(zone["id"], include_cap=True)


def test_telegram_postpone_reports_pending_physical_stop(monkeypatch):
    import services.postpone as postpone_service
    from routes.telegram import _do_group_postpone

    monkeypatch.setattr(
        postpone_service,
        "postpone_group",
        lambda group_id, days, source: {
            "success": False,
            "pending": True,
            "error_code": "POSTPONE_STOP_PENDING",
            "stopped": [],
            "unresolved": [7],
            "unverified_zone_ids": [],
            "aggregate_valid": True,
            "retry_scheduled": True,
            "postpone_until": "2026-07-20 23:59:59",
        },
    )

    message = _do_group_postpone(3, 1)

    assert "Отсрочка установлена" in message
    assert "физическое выключение пока не подтверждено" in message
    assert "7" in message
    assert "Защитные повторы продолжаются" in message


def test_telegram_postpone_does_not_invent_retry_owner_for_unverified_result(monkeypatch):
    import services.postpone as postpone_service
    from routes.telegram import _do_group_postpone

    monkeypatch.setattr(
        postpone_service,
        "postpone_group",
        lambda group_id, days, source: {
            "success": False,
            "pending": True,
            "error_code": "POSTPONE_STOP_UNAVAILABLE",
            "aggregate_valid": False,
            "stopped": [],
            "unresolved": [],
            "unverified_zone_ids": [7],
            "retry_scheduled": False,
            "postpone_until": "2026-07-20 23:59:59",
        },
    )

    message = _do_group_postpone(3, 1)

    assert "результат физического подтверждения недоступен" in message
    assert "Зоны без проверенного результата: 7" in message
    assert "Владелец защитного повтора не подтверждён" in message
    assert "Защитные повторы продолжаются" not in message


def test_public_postpone_can_extend_existing_safety_deadline(guest_client, app, monkeypatch):
    import irrigation_scheduler
    from services.api_rate_limiter import reset_all

    group = app.db.create_group("extend public postpone")
    zone = app.db.create_zone({"name": "extend protection", "duration": 5, "group_id": group["id"]})
    previous = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
    app.db.update_zone_postpone(zone["id"], previous, "manual")
    scheduler = Mock()
    scheduler.cancel_group_jobs.return_value = {
        "success": True,
        "aggregate_valid": True,
        "stopped": [zone["id"]],
        "unresolved": [],
        "unverified_zone_ids": [],
        "retry_scheduled": False,
        "group_id": group["id"],
    }
    monkeypatch.setattr(irrigation_scheduler, "get_scheduler", lambda: scheduler)
    app.db.set_setting_value("password_must_change", "0")
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
    reset_all()
    try:
        response = guest_client.post(
            "/api/postpone",
            json={"group_id": group["id"], "days": 3, "action": "postpone"},
        )
        assert response.status_code == 200
        updated = app.db.get_zone(zone["id"])["postpone_until"]
        assert datetime.fromisoformat(updated) > datetime.fromisoformat(previous)
    finally:
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        reset_all()


def test_admin_postpone_cancel_requires_valid_csrf_before_write(admin_client, app):
    group = app.db.create_group("csrf postpone cancel")
    zone = app.db.create_zone({"name": "postponed", "duration": 5, "group_id": group["id"]})
    postpone_until = "2099-12-31 23:59:59"
    app.db.update_zone_postpone(zone["id"], postpone_until, "manual")
    app.db.set_setting_value("password_must_change", "0")
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=True)
    try:
        page = admin_client.get("/")
        token_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.get_data(as_text=True))
        assert token_match is not None

        missing = admin_client.post(
            "/api/postpone",
            json={"group_id": group["id"], "action": "cancel"},
        )
        assert missing.status_code == 400
        assert "CSRF token is missing" in missing.get_data(as_text=True)
        assert app.db.get_zone(zone["id"])["postpone_until"] == postpone_until

        accepted = admin_client.post(
            "/api/postpone",
            json={"group_id": group["id"], "action": "cancel"},
            headers={"X-CSRFToken": token_match.group(1)},
        )
        assert accepted.status_code == 200
        assert app.db.get_zone(zone["id"])["postpone_until"] is None
    finally:
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)


def test_password_change_normalizes_old_and_new_edge_whitespace(admin_client, app):
    from services.api_rate_limiter import reset_all

    app.db.set_password("OldSecure123!")
    app.db.set_setting_value("password_must_change", "0")
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
    reset_all()
    try:
        response = admin_client.post(
            "/api/password",
            json={
                "old_password": "  OldSecure123!  ",
                "new_password": "  NewSecure456!  ",
            },
        )
        assert response.status_code == 200
        stored_hash = app.db.get_password_hash()
        assert check_password_hash(stored_hash, "NewSecure456!")
        assert not check_password_hash(stored_hash, "  NewSecure456!  ")

        login = app.test_client().post("/api/login", json={"password": "  NewSecure456!  "})
        assert login.status_code == 200
    finally:
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        reset_all()


@pytest.mark.parametrize("new_password", ["   password   ", "       abc       "])
def test_password_policy_cannot_be_bypassed_with_edge_whitespace(admin_client, app, new_password):
    from services.api_rate_limiter import reset_all

    app.db.set_password("OldSecure123!")
    original_hash = app.db.get_password_hash()
    app.db.set_setting_value("password_must_change", "0")
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
    reset_all()
    try:
        response = admin_client.post(
            "/api/password",
            json={"old_password": "OldSecure123!", "new_password": new_password},
        )
        assert response.status_code == 400
        assert app.db.get_password_hash() == original_hash
    finally:
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        reset_all()


def test_dotenv_timezone_is_loaded_and_synchronised_before_scheduler(tmp_path):
    from app import _load_runtime_environment

    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("WB_TZ=Etc/GMT-6\n", encoding="utf-8")
    previous_tz = os.environ.pop("TZ", None)
    previous_wb_tz = os.environ.pop("WB_TZ", None)
    try:
        _load_runtime_environment(dotenv_path=str(dotenv_path))
        assert os.environ["WB_TZ"] == "Etc/GMT-6"
        assert os.environ["TZ"] == "Etc/GMT-6"
        assert time.tzname[0] == "+06"
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        if previous_wb_tz is None:
            os.environ.pop("WB_TZ", None)
        else:
            os.environ["WB_TZ"] = previous_wb_tz
        if hasattr(time, "tzset"):
            time.tzset()


@pytest.mark.parametrize("endpoint", ["legacy", "mqtt"])
def test_session_stop_keeps_retry_jobs_when_any_physical_off_fails(admin_client, app, endpoint):
    import routes.zones_watering_api as route

    group = app.db.create_group(f"failed session OFF {endpoint}")
    zone = app.db.create_zone({"name": "active", "duration": 5, "group_id": group["id"]})
    cancel_event = threading.Event()
    scheduler = SimpleNamespace(
        group_cancel_events={group["id"]: cancel_event},
        is_group_session_active=Mock(return_value=True),
        cancel_group_jobs=Mock(),
    )
    path = f"/api/zones/{zone['id']}/{'stop' if endpoint == 'legacy' else 'mqtt/stop'}"

    with (
        patch.object(route, "get_scheduler", return_value=scheduler),
        patch("services.zone_control.stop_zone", return_value=False) as physical_stop,
    ):
        response = admin_client.post(path, content_type="application/json")

    assert response.status_code == 503
    body = response.get_json()
    assert body["success"] is False
    assert body["unresolved"] == [zone["id"]]
    assert body.get("state") != "off"
    assert cancel_event.is_set(), "the sequencer must not advance to another zone"
    physical_stop.assert_called()
    scheduler.cancel_group_jobs.assert_not_called()


@pytest.mark.parametrize("endpoint", ["legacy", "mqtt"])
def test_session_stop_rejects_scheduler_retry_aggregate_as_incomplete_abort(admin_client, app, endpoint):
    import routes.zones_watering_api as route

    group = app.db.create_group(f"scheduler unresolved {endpoint}")
    zone = app.db.create_zone({"name": "unconfirmed", "duration": 5, "group_id": group["id"]})
    cancel_event = threading.Event()
    scheduler = SimpleNamespace(
        group_cancel_events={group["id"]: cancel_event},
        is_group_session_active=Mock(return_value=True),
        cancel_group_jobs=Mock(
            return_value={
                "success": False,
                "aggregate_valid": True,
                "stopped": [],
                "unresolved": [zone["id"]],
                "unverified_zone_ids": [],
                "retry_scheduled": True,
                "group_id": group["id"],
            }
        ),
    )
    path = f"/api/zones/{zone['id']}/{'stop' if endpoint == 'legacy' else 'mqtt/stop'}"

    with (
        patch.object(route, "get_scheduler", return_value=scheduler),
        patch("services.zone_control.stop_zone", return_value=True),
    ):
        response = admin_client.post(path, content_type="application/json")

    assert response.status_code == 503
    body = response.get_json()
    assert body["success"] is False
    assert body["session_aborted"] is False
    assert body["stopped"] == []
    assert body["unresolved"] == []
    assert body["unverified_zone_ids"] == [zone["id"]]
    assert body["error_code"] == "SESSION_AGGREGATE_INVALID"
    assert cancel_event.is_set()
    scheduler.cancel_group_jobs.assert_called_once_with(group["id"])


@pytest.mark.parametrize("endpoint", ["legacy", "mqtt"])
@pytest.mark.parametrize(
    "invalid_case",
    [
        "none",
        "malformed",
        "extra",
        "missing",
        "wrong_group",
        "aggregate_false",
        "unverified",
        "incomplete",
        "retry_true",
        "coerced_ids",
    ],
)
def test_session_stop_rejects_non_exact_scheduler_aggregate(admin_client, app, endpoint, invalid_case):
    import routes.zones_watering_api as route

    group = app.db.create_group(f"strict session aggregate {endpoint} {invalid_case}")
    zones = [
        app.db.create_zone({"name": f"strict {invalid_case} {index}", "duration": 5, "group_id": group["id"]})
        for index in range(2)
    ]
    zone_ids = [zone["id"] for zone in zones]
    cancel_result = {
        "success": True,
        "aggregate_valid": True,
        "stopped": zone_ids,
        "unresolved": [],
        "unverified_zone_ids": [],
        "retry_scheduled": False,
        "group_id": group["id"],
    }
    if invalid_case == "none":
        cancel_result = None
    elif invalid_case == "malformed":
        cancel_result = ["not", "an", "aggregate"]
    elif invalid_case == "extra":
        cancel_result["error_code"] = None
    elif invalid_case == "missing":
        cancel_result.pop("aggregate_valid")
    elif invalid_case == "wrong_group":
        cancel_result["group_id"] = group["id"] + 1
    elif invalid_case == "aggregate_false":
        cancel_result["aggregate_valid"] = False
    elif invalid_case == "unverified":
        cancel_result["unverified_zone_ids"] = [zone_ids[0]]
    elif invalid_case == "incomplete":
        cancel_result["stopped"] = [zone_ids[0]]
    elif invalid_case == "retry_true":
        cancel_result["retry_scheduled"] = True
    elif invalid_case == "coerced_ids":
        cancel_result["stopped"] = [str(zone_ids[0]), zone_ids[1]]

    cancel_group_jobs = Mock(return_value=cancel_result)
    scheduler = SimpleNamespace(
        group_cancel_events={group["id"]: threading.Event()},
        is_group_session_active=Mock(return_value=True),
        cancel_group_jobs=cancel_group_jobs,
    )
    path = f"/api/zones/{zone_ids[0]}/{'stop' if endpoint == 'legacy' else 'mqtt/stop'}"

    with (
        patch.object(route, "get_scheduler", return_value=scheduler),
        patch("services.zone_control.stop_zone", return_value=True) as physical_stop,
    ):
        response = admin_client.post(path, content_type="application/json")

    assert response.status_code == 503
    body = response.get_json()
    assert body["success"] is False
    assert body["error_code"] == "SESSION_AGGREGATE_INVALID"
    assert body.get("state") != "off"
    assert body.get("message") != "Сессия группы остановлена"
    assert physical_stop.call_count == len(zone_ids)
    cancel_group_jobs.assert_called_once_with(group["id"])


@pytest.mark.parametrize("endpoint", ["legacy", "mqtt"])
def test_session_stop_accepts_only_exact_complete_scheduler_aggregate(admin_client, app, endpoint):
    import routes.zones_watering_api as route

    group = app.db.create_group(f"complete session aggregate {endpoint}")
    zones = [
        app.db.create_zone({"name": f"complete {endpoint} {index}", "duration": 5, "group_id": group["id"]})
        for index in range(2)
    ]
    zone_ids = [zone["id"] for zone in zones]
    cancel_group_jobs = Mock(
        return_value={
            "success": True,
            "aggregate_valid": True,
            "stopped": zone_ids,
            "unresolved": [],
            "unverified_zone_ids": [],
            "retry_scheduled": False,
            "group_id": group["id"],
        }
    )
    scheduler = SimpleNamespace(
        group_cancel_events={group["id"]: threading.Event()},
        is_group_session_active=Mock(return_value=True),
        cancel_group_jobs=cancel_group_jobs,
    )
    path = f"/api/zones/{zone_ids[0]}/{'stop' if endpoint == 'legacy' else 'mqtt/stop'}"

    with (
        patch.object(route, "get_scheduler", return_value=scheduler),
        patch("services.zone_control.stop_zone", return_value=True) as physical_stop,
    ):
        response = admin_client.post(path, content_type="application/json")

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["session_aborted"] is True
    assert body["stopped"] == zone_ids
    assert body["unresolved"] == []
    assert physical_stop.call_count == len(zone_ids)
    cancel_group_jobs.assert_called_once_with(group["id"])


@pytest.mark.parametrize("endpoint", ["legacy", "mqtt"])
def test_session_stop_fails_closed_when_strict_group_inventory_is_unavailable(admin_client, app, endpoint):
    import routes.zones_watering_api as route
    import services.zone_control as zone_control

    group = app.db.create_group(f"missing strict session inventory {endpoint}")
    zone = app.db.create_zone({"name": f"missing inventory {endpoint}", "duration": 5, "group_id": group["id"]})
    cancel_event = threading.Event()
    scheduler = SimpleNamespace(
        group_cancel_events={group["id"]: cancel_event},
        is_group_session_active=Mock(return_value=True),
        cancel_group_jobs=Mock(),
    )
    path = f"/api/zones/{zone['id']}/{'stop' if endpoint == 'legacy' else 'mqtt/stop'}"

    with (
        patch.object(route, "get_scheduler", return_value=scheduler),
        patch.object(zone_control, "_strict_group_zone_ids", return_value=None),
        patch.object(zone_control, "stop_zone") as physical_stop,
    ):
        response = admin_client.post(path, content_type="application/json")

    assert response.status_code == 503
    body = response.get_json()
    assert body["success"] is False
    assert body["session_aborted"] is False
    assert body["error_code"] == "SESSION_INVENTORY_UNAVAILABLE"
    assert body.get("state") != "off"
    assert cancel_event.is_set()
    physical_stop.assert_not_called()
    scheduler.cancel_group_jobs.assert_not_called()


def test_mqtt_stop_does_not_retry_raw_publish_after_central_failure(admin_client, app):
    import routes.zones_watering_api as route

    server = app.db.create_mqtt_server({"name": "fallback", "host": "127.0.0.1", "port": 1883})
    zone = app.db.create_zone(
        {
            "name": "fallback failure",
            "duration": 5,
            "group_id": 1,
            "mqtt_server_id": server["id"],
            "topic": "/devices/test/controls/K1",
            "state": "on",
        }
    )
    app.db.update_zone(zone["id"], {"state": "on", "watering_start_time": "2026-01-01 10:00:00"})

    with (
        patch.object(route, "get_scheduler", return_value=None),
        patch("services.zone_control.stop_zone", return_value=False),
        patch("services.mqtt_pub.publish_mqtt_value") as raw_publish,
    ):
        response = admin_client.post(f"/api/zones/{zone['id']}/mqtt/stop", content_type="application/json")

    assert response.status_code == 500
    assert response.get_json()["success"] is False
    assert app.db.get_zone(zone["id"])["state"] == "on"
    raw_publish.assert_not_called()


@pytest.mark.parametrize("observed_state", ["on", "unknown"])
@pytest.mark.parametrize("endpoint", ["legacy", "mqtt"])
def test_public_stop_forces_off_retry_when_observed_state_is_not_off(guest_client, app, endpoint, observed_state):
    import routes.zones_watering_api as route

    server = app.db.create_mqtt_server({"name": "observed mismatch", "host": "127.0.0.1", "port": 1883})
    zone = app.db.create_zone(
        {
            "name": "commanded off observed on",
            "duration": 5,
            "group_id": 1,
            "mqtt_server_id": server["id"],
            "topic": "/devices/test/controls/K2",
        }
    )
    app.db.update_zone(
        zone["id"],
        {"state": "off", "commanded_state": "off", "observed_state": observed_state},
    )
    path = f"/api/zones/{zone['id']}/{'stop' if endpoint == 'legacy' else 'mqtt/stop'}"
    app.db.set_setting_value("password_must_change", "0")
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)

    try:
        with (
            patch.object(route, "get_scheduler", return_value=None),
            patch("services.zone_control.publish_mqtt_value", return_value=False) as physical_publish,
        ):
            response = guest_client.post(path, content_type="application/json")
    finally:
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    assert response.status_code == 500
    assert response.get_json()["success"] is False
    physical_publish.assert_called_once()
    assert physical_publish.call_args.args[2] == "0"


def _asgi_scope(path: str, method: str = "GET") -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 43210),
        "server": ("127.0.0.1", 8080),
    }


def _start_asgi_request(asgi_app, path: str, method: str = "GET"):
    events: list[dict] = []
    response_started = asyncio.Event()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        events.append(message)
        if message["type"] == "http.response.start":
            response_started.set()

    task = asyncio.create_task(asgi_app(_asgi_scope(path, method), receive, send))
    return task, events, response_started


def _response_status(events: list[dict]) -> int:
    return next(event["status"] for event in events if event["type"] == "http.response.start")


@pytest.mark.parametrize("failure_stage", ["ensure", "register", "generator", "response"])
def test_sse_setup_exception_releases_lease_and_next_stream_is_accepted(app, monkeypatch, failure_stage):
    import routes.zones_watering_api as route

    monkeypatch.setattr(route, "_SSE_HTTP_ACTIVE", 0)
    assert route._SSE_HTTP_ACTIVE == 0
    first_queue: queue.Queue = queue.Queue()
    next_queue: queue.Queue = queue.Queue()
    unregistered: list[queue.Queue] = []
    app.config.update(
        HTTP_EXECUTOR_WORKERS=2,
        HTTP_CONTROL_WORKER_RESERVE=1,
        SSE_HTTP_MAX_CLIENTS=1,
    )
    monkeypatch.setattr(route._sse_hub, "ensure_hub_started", lambda: None)
    monkeypatch.setattr(route._sse_hub, "unregister_client", unregistered.append)

    def fail_setup(*_args, **_kwargs):
        raise ValueError(f"forced {failure_stage} failure")

    with monkeypatch.context() as fault:
        if failure_stage == "ensure":
            fault.setattr(route._sse_hub, "ensure_hub_started", fail_setup)
            fault.setattr(route._sse_hub, "register_client", lambda: first_queue)
        elif failure_stage == "register":
            fault.setattr(route._sse_hub, "register_client", fail_setup)
        elif failure_stage == "generator":
            fault.setattr(route._sse_hub, "register_client", lambda: first_queue)
            fault.setattr(route, "_sse_event_stream", fail_setup)
        else:
            fault.setattr(route._sse_hub, "register_client", lambda: first_queue)
            fault.setattr(route, "Response", fail_setup)

        with pytest.raises(ValueError, match=f"forced {failure_stage} failure"):
            app.test_client().get("/api/mqtt/zones-sse", buffered=False)

    assert route._SSE_HTTP_ACTIVE == 0
    expected_after_failure = [first_queue] if failure_stage in {"generator", "response"} else []
    assert unregistered == expected_after_failure

    monkeypatch.setattr(route._sse_hub, "register_client", lambda: next_queue)
    response = app.test_client().get("/api/mqtt/zones-sse", buffered=False)
    try:
        assert response.status_code == 200
        assert next(response.response) == b": connected\n\n"
    finally:
        response.close()
        response.close()

    assert route._SSE_HTTP_ACTIVE == 0
    assert unregistered == [*expected_after_failure, next_queue]


def test_sse_response_close_is_cross_context_safe_and_idempotent(admin_client, monkeypatch, recwarn):
    import routes.zones_watering_api as route

    msg_queue: queue.Queue = queue.Queue()
    unregistered = []
    close_errors: list[BaseException] = []
    monkeypatch.setattr(route, "_SSE_HTTP_ACTIVE", 0)
    monkeypatch.setattr(route._sse_hub, "ensure_hub_started", lambda: None)
    monkeypatch.setattr(route._sse_hub, "register_client", lambda: msg_queue)
    monkeypatch.setattr(route._sse_hub, "unregister_client", unregistered.append)

    response = admin_client.get("/api/mqtt/zones-sse", buffered=False)
    assert response.status_code == 200
    assert next(response.response) == b": connected\n\n"

    def close_response():
        try:
            response.close()
            response.close()
        except BaseException as exc:  # surfaced below instead of becoming unraisable
            close_errors.append(exc)

    closer = threading.Thread(target=close_response, name="cross-context-sse-close")
    closer.start()
    closer.join(timeout=1.0)
    assert not closer.is_alive()

    assert close_errors == []
    assert unregistered == [msg_queue]
    assert route._SSE_HTTP_ACTIVE == 0
    assert not any("ContextVar" in str(warning.message) for warning in recwarn)


def test_sse_saturation_preserves_workers_for_ready_and_control(app, monkeypatch, recwarn):
    """One live stream on a two-worker executor must leave one control worker."""
    import routes.zones_watering_api as route
    from run import _get_asgi_app, _http_executor_workers

    queues: list[queue.Queue] = []
    unregistered: list[queue.Queue] = []

    def register_client():
        msg_queue: queue.Queue = queue.Queue()
        queues.append(msg_queue)
        return msg_queue

    monkeypatch.setattr(route._sse_hub, "ensure_hub_started", lambda: None)
    monkeypatch.setattr(route._sse_hub, "register_client", register_client)
    monkeypatch.setattr(route._sse_hub, "unregister_client", unregistered.append)
    app.config.update(
        HTTP_EXECUTOR_WORKERS=2,
        HTTP_CONTROL_WORKER_RESERVE=1,
        SSE_HTTP_MAX_CLIENTS=1,
    )
    assert route._SSE_HTTP_ACTIVE == 0
    assert _http_executor_workers(app) == 2

    async def scenario():
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=_http_executor_workers(app), thread_name_prefix="test-http")
        loop.set_default_executor(executor)
        asgi_app = _get_asgi_app(app)
        stream_tasks: list[asyncio.Task] = []
        try:
            first_task, first_events, first_started = _start_asgi_request(asgi_app, "/api/mqtt/zones-sse")
            stream_tasks.append(first_task)
            await asyncio.wait_for(first_started.wait(), timeout=1.0)
            assert _response_status(first_events) == 200

            overflow_task, overflow_events, _overflow_started = _start_asgi_request(asgi_app, "/api/mqtt/zones-sse")
            done, _pending = await asyncio.wait({overflow_task}, timeout=1.0)
            assert overflow_task in done, "overflow SSE consumed the reserved control worker"
            assert _response_status(overflow_events) == 503

            ready_task, ready_events, _ready_started = _start_asgi_request(asgi_app, "/readyz")
            await asyncio.wait_for(ready_task, timeout=1.0)
            assert _response_status(ready_events) in {200, 503}

            control_task, control_events, _control_started = _start_asgi_request(
                asgi_app, "/api/zones/999999/stop", method="POST"
            )
            await asyncio.wait_for(control_task, timeout=1.0)
            assert _response_status(control_events) == 404
        finally:
            for msg_queue in queues:
                msg_queue.put(None)
            if stream_tasks:
                await asyncio.wait_for(asyncio.gather(*stream_tasks, return_exceptions=True), timeout=2.0)

    asyncio.run(scenario())
    assert unregistered == queues
    assert route._SSE_HTTP_ACTIVE == 0
    assert not any("ContextVar" in str(warning.message) for warning in recwarn)
