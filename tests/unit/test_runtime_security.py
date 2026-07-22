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


def test_http_listener_notification_starts_armed_watchdog_before_ready(monkeypatch):
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
        lambda **_kwargs: events.append("WATCHDOG") or True,
    )
    monkeypatch.setattr(systemd_notify, "watchdog_is_armed", lambda: True)

    assert app_init.notify_http_listener_ready() is True
    assert events == ["WATCHDOG", "READY=1"]
    assert app_init.notify_http_listener_ready() is True
    assert events == ["WATCHDOG", "READY=1"]


def test_http_listener_withholds_ready_when_armed_watchdog_cannot_start(monkeypatch):
    from services import app_init, systemd_notify

    monkeypatch.setattr(app_init, "_boot_sync_done", True)
    monkeypatch.setattr(app_init, "_boot_recovery_done", True)
    monkeypatch.setattr(app_init, "_http_listener_ready_notified", False, raising=False)
    monkeypatch.setattr(app_init, "_start_health_bound_heartbeat", lambda **_kwargs: False)
    monkeypatch.setattr(systemd_notify, "watchdog_is_armed", lambda: True)
    notify_ready = MagicMock(return_value=True)
    monkeypatch.setattr(systemd_notify, "notify_ready", notify_ready)

    assert app_init.notify_http_listener_ready() is False
    notify_ready.assert_not_called()


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

    assert (
        run._probe_http_listener(
            "http://127.0.0.1:8080/healthz",
            "current-process",
            urlopen_fn=urlopen,
        )
        is False
    )
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
        resolve_http_transport({"WB_HTTP_PROBE_HOST": "controller.example.test"}),
        "current-process",
        threading.Event(),
    )

    notify_ready.assert_called_once_with(health_probe_url="http://controller.example.test:8080/healthz")


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


def test_empty_bracket_bind_is_rejected_before_listener_start():
    from services.security import HttpTransportConfigurationError, resolve_http_transport

    with pytest.raises(HttpTransportConfigurationError, match="WB_HTTP_BIND_HOST"):
        resolve_http_transport(
            {
                "WB_HTTP_BIND_HOST": "[]",
                "WB_HTTP_ALLOW_INSECURE_EXTERNAL": "1",
            }
        )


def test_local_probe_url_uses_resolved_scheme_and_custom_bind(monkeypatch, tmp_path: Path):
    from services import security

    plaintext = security.resolve_http_transport(
        {
            "WB_HTTP_BIND_HOST": "127.0.0.2",
            "PORT": "9123",
        }
    )
    assert security.local_http_probe_url(plaintext, path="/readyz") == "http://127.0.0.2:9123/readyz"

    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_text("certificate", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    monkeypatch.setattr(security, "_validate_tls_cert_pair", lambda *_args: None, raising=False)
    tls = security.resolve_http_transport(
        {
            "WB_HTTP_BIND_HOST": "192.0.2.20",
            "PORT": "9443",
            "WB_HTTP_TLS_CERTFILE": str(cert),
            "WB_HTTP_TLS_KEYFILE": str(key),
        }
    )
    assert security.local_http_probe_url(tls, path="/readyz") == "https://192.0.2.20:9443/readyz"


@pytest.mark.parametrize(
    ("configured", "normalized", "url_host"),
    [
        ("controller.example.test", "controller.example.test", "controller.example.test"),
        ("CONTROLLER.LOCAL.", "controller.local.", "controller.local."),
        ("192.0.2.44", "192.0.2.44", "192.0.2.44"),
        ("2001:db8::44", "2001:db8::44", "[2001:db8::44]"),
        ("[2001:db8::45]", "2001:db8::45", "[2001:db8::45]"),
    ],
)
def test_explicit_probe_host_override_accepts_host_only_dns_and_ip(
    configured,
    normalized,
    url_host,
):
    from services.security import local_http_probe_url, resolve_http_transport

    profile = resolve_http_transport({"WB_HTTP_PROBE_HOST": configured, "PORT": "9123"})

    assert profile.probe_host == normalized
    assert local_http_probe_url(profile, path="/readyz") == f"http://{url_host}:9123/readyz"


@pytest.mark.parametrize(
    "configured",
    [
        "https://controller.example.test",
        "controller.example.test/path",
        "controller.example.test?check=1",
        "user@controller.example.test",
        "controller.example.test:8080",
        "0.0.0.0",
        "::",
        "*",
        "*.example.test",
        "bad host",
        "bad\nhost",
        "bad_host.example.test",
        "[::1]:8080",
        "fe80::1%eth0",
        "999.999.999.999",
    ],
)
def test_explicit_probe_host_override_rejects_non_host_and_wildcard_values(configured):
    from services.security import HttpTransportConfigurationError, resolve_http_transport

    with pytest.raises(HttpTransportConfigurationError, match="WB_HTTP_PROBE_HOST"):
        resolve_http_transport({"WB_HTTP_PROBE_HOST": configured})


def test_unauthenticated_https_probe_verifies_by_default_and_needs_explicit_bypass(tmp_path: Path):
    from services import security

    tls_profile = MagicMock(tls_enabled=True)
    default = security.resolve_http_probe_tls(tls_profile, {})
    assert default.ca_file is None
    assert default.insecure_tls is False

    ca_file = tmp_path / "controller-ca.crt"
    ca_file.write_text("private CA bundle", encoding="utf-8")
    private_ca = security.resolve_http_probe_tls(
        tls_profile,
        {"WB_HTTP_PROBE_CA_FILE": str(ca_file)},
    )
    assert private_ca.ca_file == str(ca_file)
    assert private_ca.insecure_tls is False

    explicit_bypass = security.resolve_http_probe_tls(
        tls_profile,
        {"WB_HTTP_PROBE_INSECURE_TLS": "1"},
    )
    assert explicit_bypass.insecure_tls is True
    with pytest.raises(security.HttpTransportConfigurationError, match="mutually exclusive"):
        security.resolve_http_probe_tls(
            tls_profile,
            {
                "WB_HTTP_PROBE_CA_FILE": str(ca_file),
                "WB_HTTP_PROBE_INSECURE_TLS": "1",
            },
        )


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


def test_tls_configuration_requires_a_complete_parseable_certificate_pair(monkeypatch, tmp_path: Path):
    from services import security

    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_text("certificate", encoding="utf-8")

    with pytest.raises(security.HttpTransportConfigurationError, match="both be configured"):
        security.resolve_http_transport(
            {
                "WB_HTTP_BIND_HOST": "0.0.0.0",
                "WB_HTTP_TLS_CERTFILE": str(cert),
            }
        )

    key.write_text("key", encoding="utf-8")
    with pytest.raises(security.HttpTransportConfigurationError, match="valid TLS certificate"):
        security.resolve_http_transport(
            {
                "WB_HTTP_BIND_HOST": "0.0.0.0",
                "WB_HTTP_TLS_CERTFILE": str(cert),
                "WB_HTTP_TLS_KEYFILE": str(key),
            }
        )

    validate_pair = MagicMock()
    monkeypatch.setattr(security, "_validate_tls_cert_pair", validate_pair, raising=False)
    profile = security.resolve_http_transport(
        {
            "WB_HTTP_BIND_HOST": "0.0.0.0",
            "WB_HTTP_TLS_CERTFILE": str(cert),
            "WB_HTTP_TLS_KEYFILE": str(key),
        }
    )
    validate_pair.assert_called_once_with(str(cert), str(key))
    assert profile.tls_enabled is True
    assert profile.bind == "0.0.0.0:8080"


def test_tls_transport_forces_secure_session_cookie(app, monkeypatch, tmp_path):
    from app import _configure_session_cookie_secure
    from services import security

    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_text("certificate", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    monkeypatch.setenv("WB_HTTP_TLS_CERTFILE", str(cert))
    monkeypatch.setenv("WB_HTTP_TLS_KEYFILE", str(key))
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    monkeypatch.setattr(security, "_validate_tls_cert_pair", lambda *_args: None, raising=False)

    _configure_session_cookie_secure(app)

    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_invalid_secure_cookie_override_fails_closed(app, monkeypatch):
    from app import _configure_session_cookie_secure
    from services.security import HttpTransportConfigurationError

    monkeypatch.setenv("SESSION_COOKIE_SECURE", "sometimes")

    with pytest.raises(HttpTransportConfigurationError, match="SESSION_COOKIE_SECURE"):
        _configure_session_cookie_secure(app)


def test_heartbeat_recovers_after_tls_certificate_temporarily_disappears(
    monkeypatch,
    tmp_path: Path,
):
    from services import app_init, security, systemd_notify

    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_text("certificate", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    monkeypatch.setenv("WB_HTTP_TLS_CERTFILE", str(cert))
    monkeypatch.setenv("WB_HTTP_TLS_KEYFILE", str(key))
    monkeypatch.setattr(security, "_validate_tls_cert_pair", lambda *_args: None, raising=False)
    notify = MagicMock(return_value=True)
    monkeypatch.setattr(systemd_notify, "notify_watchdog", notify)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    urlopen = MagicMock(return_value=Response())
    cert.unlink()
    assert app_init._health_heartbeat_once(urlopen_fn=urlopen, timeout_sec=0.1) is False
    cert.write_text("certificate", encoding="utf-8")
    assert app_init._health_heartbeat_once(urlopen_fn=urlopen, timeout_sec=0.1) is True
    notify.assert_called_once_with()


def test_armed_systemd_watchdog_cannot_be_disabled_by_app_env(monkeypatch):
    from services import app_init

    fake_thread = MagicMock()
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    monkeypatch.setenv("WATCHDOG_USEC", "60000000")
    monkeypatch.setenv("WB_WATCHDOG_ENABLED", "0")
    monkeypatch.setattr(app_init, "_HEALTH_HEARTBEAT_THREAD", None)
    monkeypatch.setattr(app_init.threading, "Thread", MagicMock(return_value=fake_thread))

    assert app_init._start_health_bound_heartbeat() is True
    fake_thread.start.assert_called_once_with()


def test_example_environment_documents_each_supported_http_exposure_mode():
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")

    assert "WB_HTTP_BIND_HOST=127.0.0.1" in example
    assert "WB_HTTP_TLS_CERTFILE=" in example
    assert "WB_HTTP_TLS_KEYFILE=" in example
    assert "WB_HTTP_ALLOW_INSECURE_EXTERNAL=1" in example
    assert "WB_HTTP_TRUSTED_PROXY_HOPS=1" in example
    assert "WB_HTTP_PROBE_CA_FILE=" in example
    assert "WB_HTTP_PROBE_INSECURE_TLS=1" in example
