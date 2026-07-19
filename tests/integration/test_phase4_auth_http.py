"""Phase 4 regressions for HTTP safety, streaming and runtime environment."""

from __future__ import annotations

import asyncio
import os
import queue
import re
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


def test_guest_can_still_apply_fail_safe_postpone(guest_client, app):
    group = app.db.create_group("public fail-safe postpone")
    zone = app.db.create_zone({"name": "delay me", "duration": 5, "group_id": group["id"]})
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


def test_public_postpone_can_extend_existing_safety_deadline(guest_client, app):
    from services.api_rate_limiter import reset_all

    group = app.db.create_group("extend public postpone")
    zone = app.db.create_zone({"name": "extend protection", "duration": 5, "group_id": group["id"]})
    previous = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
    app.db.update_zone_postpone(zone["id"], previous, "manual")
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

    assert response.status_code == 500
    body = response.get_json()
    assert body["success"] is False
    assert body.get("state") != "off"
    assert cancel_event.is_set(), "the sequencer must not advance to another zone"
    physical_stop.assert_called()
    scheduler.cancel_group_jobs.assert_not_called()


def test_mqtt_stop_fallback_does_not_claim_off_when_publish_returns_false(admin_client, app):
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
        patch.object(route, "_publish_mqtt_value", return_value=False),
    ):
        response = admin_client.post(f"/api/zones/{zone['id']}/mqtt/stop", content_type="application/json")

    assert response.status_code == 500
    assert response.get_json()["success"] is False
    assert app.db.get_zone(zone["id"])["state"] == "on"


@pytest.mark.parametrize("observed_state", ["on", "unknown"])
@pytest.mark.parametrize("endpoint", ["legacy", "mqtt"])
def test_public_stop_forces_off_retry_when_observed_state_is_not_off(
    guest_client, app, endpoint, observed_state
):
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
            patch.object(route, "_publish_mqtt_value", return_value=False),
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


def test_sse_saturation_preserves_workers_for_ready_and_control(app, monkeypatch):
    """One live stream on a two-worker executor must leave one control worker."""
    import routes.zones_watering_api as route
    from run import _get_asgi_app, _http_executor_workers

    queues: list[queue.Queue] = []

    def register_client():
        msg_queue: queue.Queue = queue.Queue()
        queues.append(msg_queue)
        return msg_queue

    monkeypatch.setattr(route._sse_hub, "ensure_hub_started", lambda: None)
    monkeypatch.setattr(route._sse_hub, "register_client", register_client)
    monkeypatch.setattr(route._sse_hub, "unregister_client", lambda _queue: None)
    app.config.update(
        HTTP_EXECUTOR_WORKERS=2,
        HTTP_CONTROL_WORKER_RESERVE=1,
        SSE_HTTP_MAX_CLIENTS=1,
    )
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

            overflow_task, overflow_events, _overflow_started = _start_asgi_request(
                asgi_app, "/api/mqtt/zones-sse"
            )
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
