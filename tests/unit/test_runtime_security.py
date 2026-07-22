"""Release regressions for listener readiness and HTTP transport gates."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_initialize_does_not_emit_ready_or_start_heartbeat_before_http_listener(test_db):
    import irrigation_scheduler
    import routes.health_api as health_api
    import services.systemd_notify as systemd_notify
    import services.watchdog as watchdog
    from services import app_init

    scheduler = MagicMock()
    scheduler.complete_boot_recovery.return_value = True
    scheduler.boot_recovery_handoff_is_durable.return_value = True
    flask_app = MagicMock()
    flask_app.config = {"TESTING": False}

    app_init.reset_init()
    with (
        patch.object(app_init, "_boot_sync", return_value=True),
        patch.object(irrigation_scheduler, "init_scheduler"),
        patch.object(irrigation_scheduler, "get_scheduler", return_value=scheduler),
        patch.object(watchdog, "start_watchdog"),
        patch.object(app_init, "_start_monitors"),
        patch.object(app_init, "_warm_mqtt_clients"),
        patch.object(app_init, "_register_shutdown_handlers"),
        patch.object(app_init, "_start_health_bound_heartbeat") as start_heartbeat,
        patch.object(health_api, "init_metrics"),
        patch.object(systemd_notify, "notify_ready") as notify_ready,
    ):
        app_init.initialize_app(flask_app, test_db, start_watchdog_fn=MagicMock())

    assert app_init._boot_sync_done is True
    assert app_init._boot_recovery_done is True
    start_heartbeat.assert_not_called()
    notify_ready.assert_not_called()


def test_http_listener_notification_orders_ready_before_watchdog(monkeypatch):
    from services import app_init, systemd_notify

    events: list[str] = []
    monkeypatch.setattr(app_init, "_boot_sync_done", True)
    monkeypatch.setattr(app_init, "_boot_recovery_done", True)
    monkeypatch.setattr(app_init, "_boot_zone_count", 4)
    monkeypatch.setattr(app_init, "_http_listener_ready_notified", False, raising=False)
    monkeypatch.setattr(
        systemd_notify,
        "notify_ready",
        lambda **_kwargs: events.append("READY=1") or True,
    )
    monkeypatch.setattr(
        app_init,
        "_start_health_bound_heartbeat",
        lambda: events.append("WATCHDOG") or True,
    )

    assert app_init.notify_http_listener_ready() is True
    assert events == ["READY=1", "WATCHDOG"]
    assert app_init.notify_http_listener_ready() is True
    assert events == ["READY=1", "WATCHDOG"]


def test_http_listener_notification_refuses_unreconciled_boot(monkeypatch):
    from services import app_init, systemd_notify

    monkeypatch.setattr(app_init, "_boot_sync_done", False)
    monkeypatch.setattr(app_init, "_boot_recovery_done", True)
    monkeypatch.setattr(app_init, "_http_listener_ready_notified", False, raising=False)
    notify_ready = MagicMock(return_value=True)
    monkeypatch.setattr(systemd_notify, "notify_ready", notify_ready)

    assert app_init.notify_http_listener_ready() is False
    notify_ready.assert_not_called()


def test_listener_probe_requires_token_from_current_process():
    import run

    class Response:
        status = 200
        headers = {"X-WB-Startup-Probe": "different-process"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    urlopen = MagicMock(return_value=Response())

    assert run._probe_http_listener(
        "http://127.0.0.1:8080/healthz",
        "current-process",
        urlopen_fn=urlopen,
    ) is False
    request = urlopen.call_args.args[0]
    assert request.headers["X-wb-startup-probe"] == "current-process"


def test_ipv6_wildcard_listener_is_probed_over_ipv6_loopback():
    import run
    from services.security import resolve_http_transport

    profile = resolve_http_transport(
        {
            "WB_HTTP_BIND_HOST": "::",
            "WB_HTTP_ALLOW_INSECURE_EXTERNAL": "1",
        }
    )

    assert run._listener_probe_url(profile) == "http://[::1]:8080/healthz"


def test_ready_notification_runs_only_after_listener_probe_accepts(monkeypatch):
    import run
    from services import app_init
    from services.security import resolve_http_transport

    probes = iter([False, True])
    notify_ready = MagicMock(return_value=True)
    monkeypatch.setattr(run, "_probe_http_listener", lambda *_args, **_kwargs: next(probes))
    monkeypatch.setattr(app_init, "notify_http_listener_ready", notify_ready)

    run._listener_ready_loop(
        MagicMock(),
        resolve_http_transport({}),
        "current-process",
        threading.Event(),
    )

    notify_ready.assert_called_once_with()


def test_listener_probe_retries_transient_systemd_notification_failure(monkeypatch):
    import run
    from services import app_init
    from services.security import resolve_http_transport

    notify_ready = MagicMock(side_effect=[False, True])
    monkeypatch.setattr(run, "_probe_http_listener", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(app_init, "notify_http_listener_ready", notify_ready)

    run._listener_ready_loop(
        MagicMock(),
        resolve_http_transport({}),
        "current-process",
        threading.Event(),
    )

    assert notify_ready.call_count == 2


def test_external_plain_http_requires_explicit_operator_acknowledgement():
    from services.security import HttpTransportConfigurationError, resolve_http_transport

    with pytest.raises(HttpTransportConfigurationError, match="external plaintext HTTP"):
        resolve_http_transport({"WB_HTTP_BIND_HOST": "0.0.0.0", "PORT": "8080"})

    profile = resolve_http_transport(
        {
            "WB_HTTP_BIND_HOST": "0.0.0.0",
            "PORT": "8080",
            "WB_HTTP_ALLOW_INSECURE_EXTERNAL": "1",
        }
    )
    assert profile.external_bind is True
    assert profile.tls_enabled is False
    assert profile.insecure_external_acknowledged is True


def test_trusted_proxy_hops_are_rejected_on_direct_external_bind():
    from services.security import HttpTransportConfigurationError, resolve_http_transport

    with pytest.raises(HttpTransportConfigurationError, match="loopback bind"):
        resolve_http_transport(
            {
                "WB_HTTP_BIND_HOST": "0.0.0.0",
                "WB_HTTP_ALLOW_INSECURE_EXTERNAL": "1",
                "WB_HTTP_TRUSTED_PROXY_HOPS": "1",
            }
        )


@pytest.mark.parametrize("value", ["-1", "2", "many"])
def test_invalid_trusted_proxy_hops_fail_closed(value):
    from services.security import HttpTransportConfigurationError, resolve_http_transport

    with pytest.raises(HttpTransportConfigurationError, match="WB_HTTP_TRUSTED_PROXY_HOPS"):
        resolve_http_transport({"WB_HTTP_TRUSTED_PROXY_HOPS": value})


def test_trusted_proxy_requires_secure_cookie_or_explicit_plaintext_acknowledgement():
    from services.security import HttpTransportConfigurationError, resolve_http_transport

    with pytest.raises(HttpTransportConfigurationError, match="SESSION_COOKIE_SECURE=1"):
        resolve_http_transport({"WB_HTTP_TRUSTED_PROXY_HOPS": "1"})


def test_proxy_headers_are_ignored_by_default_and_trusted_only_when_opted_in():
    from flask import Flask, request

    from app import _configure_trusted_proxy
    from services.security import resolve_http_transport

    def make_app(profile):
        flask_app = Flask(__name__)
        flask_app.config.update(SECRET_KEY="proxy-test", TESTING=False)
        _configure_trusted_proxy(flask_app, profile)

        @flask_app.get("/identity")
        def identity():
            return {"ip": request.remote_addr, "scheme": request.scheme}

        return flask_app

    untrusted = make_app(resolve_http_transport({}))
    ignored = untrusted.test_client().get(
        "/identity",
        headers={"X-Forwarded-For": "198.51.100.9", "X-Forwarded-Proto": "https"},
    )
    assert ignored.get_json() == {"ip": "127.0.0.1", "scheme": "http"}

    trusted_profile = resolve_http_transport(
        {
            "WB_HTTP_BIND_HOST": "127.0.0.1",
            "WB_HTTP_TRUSTED_PROXY_HOPS": "1",
            "SESSION_COOKIE_SECURE": "1",
        }
    )
    trusted = make_app(trusted_profile)
    accepted = trusted.test_client().get(
        "/identity",
        headers={"X-Forwarded-For": "198.51.100.9", "X-Forwarded-Proto": "https"},
    )
    assert accepted.get_json() == {"ip": "198.51.100.9", "scheme": "https"}


def test_proxy_client_identity_reaches_existing_api_rate_limiter(monkeypatch):
    from flask import Flask

    from app import _configure_trusted_proxy
    from services import api_rate_limiter
    from services.security import resolve_http_transport

    observed_ips: list[str] = []
    monkeypatch.setattr(
        api_rate_limiter,
        "_is_allowed",
        lambda ip, *_args: observed_ips.append(ip) or (True, 0),
    )
    flask_app = Flask(__name__)
    flask_app.config.update(SECRET_KEY="rate-limit-test", TESTING=False)
    _configure_trusted_proxy(
        flask_app,
        resolve_http_transport(
            {
                "WB_HTTP_TRUSTED_PROXY_HOPS": "1",
                "SESSION_COOKIE_SECURE": "1",
            }
        ),
    )

    @flask_app.post("/limited")
    @api_rate_limiter.rate_limit("test", max_requests=1, window_sec=60)
    def limited():
        return {"ok": True}

    response = flask_app.test_client().post(
        "/limited",
        headers={"X-Forwarded-For": "203.0.113.44", "X-Forwarded-Proto": "https"},
    )

    assert response.status_code == 200
    assert observed_ips == ["203.0.113.44"]


def test_tls_configuration_requires_a_complete_readable_certificate_pair(tmp_path: Path):
    from services.security import HttpTransportConfigurationError, resolve_http_transport

    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_text("certificate", encoding="utf-8")

    with pytest.raises(HttpTransportConfigurationError, match="both be configured"):
        resolve_http_transport(
            {
                "WB_HTTP_BIND_HOST": "0.0.0.0",
                "WB_HTTP_TLS_CERTFILE": str(cert),
            }
        )

    key.write_text("key", encoding="utf-8")
    profile = resolve_http_transport(
        {
            "WB_HTTP_BIND_HOST": "0.0.0.0",
            "WB_HTTP_TLS_CERTFILE": str(cert),
            "WB_HTTP_TLS_KEYFILE": str(key),
        }
    )
    assert profile.tls_enabled is True
    assert profile.bind == "0.0.0.0:8080"


def test_tls_transport_forces_secure_session_cookie(app, monkeypatch, tmp_path):
    from app import _configure_session_cookie_secure

    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_text("certificate", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    monkeypatch.setenv("WB_HTTP_TLS_CERTFILE", str(cert))
    monkeypatch.setenv("WB_HTTP_TLS_KEYFILE", str(key))
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)

    _configure_session_cookie_secure(app)

    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_invalid_secure_cookie_override_fails_closed(app, monkeypatch):
    from app import _configure_session_cookie_secure
    from services.security import HttpTransportConfigurationError

    monkeypatch.setenv("SESSION_COOKIE_SECURE", "sometimes")

    with pytest.raises(HttpTransportConfigurationError, match="SESSION_COOKIE_SECURE"):
        _configure_session_cookie_secure(app)
