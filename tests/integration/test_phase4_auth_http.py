"""Phase 4 regressions for HTTP safety, streaming and runtime environment."""

from __future__ import annotations

import asyncio
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


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
