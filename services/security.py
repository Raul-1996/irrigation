import ipaddress
import os
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Mapping

from flask import current_app, jsonify, redirect, request, session, url_for


class HttpTransportConfigurationError(RuntimeError):
    """Raised when the HTTP transport could expose sessions unexpectedly."""


@dataclass(frozen=True, slots=True)
class HttpTransportConfig:
    bind_host: str
    port: int
    tls_certfile: str | None
    tls_keyfile: str | None
    external_bind: bool
    trusted_proxy_hops: int
    insecure_external_acknowledged: bool

    @property
    def tls_enabled(self) -> bool:
        return self.tls_certfile is not None and self.tls_keyfile is not None

    @property
    def bind(self) -> str:
        host = self.bind_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{self.port}"

    @property
    def probe_host(self) -> str:
        if self.bind_host in {"::", "[::]"}:
            return "::1"
        if self.bind_host == "0.0.0.0":
            return "127.0.0.1"
        if self.bind_host == "localhost":
            return "127.0.0.1"
        return self.bind_host


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def parse_strict_bool(value: object, *, name: str) -> bool:
    """Parse an operator-facing boolean without unsafe typo fallbacks."""
    normalized = str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise HttpTransportConfigurationError(f"{name} must be one of 1/0, true/false, yes/no, on/off")


def _is_external_bind(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    if normalized == "localhost":
        return False
    try:
        return not ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        # A hostname can resolve beyond the controller; treat it as external.
        return True


def _readable_regular_file(raw_path: str, *, name: str) -> str:
    path = Path(raw_path).expanduser()
    if not path.is_file() or not os.access(path, os.R_OK):
        raise HttpTransportConfigurationError(f"{name} must name a readable regular file")
    return str(path)


def resolve_http_transport(environ: Mapping[str, str] | None = None) -> HttpTransportConfig:
    """Resolve the native WB listener with an explicit external-HTTP gate.

    The safe default is loopback HTTP. An externally reachable listener must
    either have a real certificate/key pair or carry the explicit
    ``WB_HTTP_ALLOW_INSECURE_EXTERNAL=1`` acknowledgement. The latter exists
    for isolated legacy WB LANs; it is intentionally noisy at startup.
    """
    env = os.environ if environ is None else environ
    bind_host = str(env.get("WB_HTTP_BIND_HOST", "127.0.0.1")).strip()
    if not bind_host or any(char.isspace() or ord(char) < 32 for char in bind_host):
        raise HttpTransportConfigurationError("WB_HTTP_BIND_HOST must be a non-empty host without whitespace")
    try:
        port = int(str(env.get("PORT", "8080")).strip())
    except (TypeError, ValueError) as error:
        raise HttpTransportConfigurationError("PORT must be an integer in 1..65535") from error
    if not 1 <= port <= 65535:
        raise HttpTransportConfigurationError("PORT must be an integer in 1..65535")

    try:
        trusted_proxy_hops = int(str(env.get("WB_HTTP_TRUSTED_PROXY_HOPS", "0")).strip())
    except (TypeError, ValueError) as error:
        raise HttpTransportConfigurationError(
            "WB_HTTP_TRUSTED_PROXY_HOPS must be 0 or 1"
        ) from error
    if trusted_proxy_hops not in {0, 1}:
        raise HttpTransportConfigurationError("WB_HTTP_TRUSTED_PROXY_HOPS must be 0 or 1")

    cert_raw = str(env.get("WB_HTTP_TLS_CERTFILE", "")).strip()
    key_raw = str(env.get("WB_HTTP_TLS_KEYFILE", "")).strip()
    if bool(cert_raw) != bool(key_raw):
        raise HttpTransportConfigurationError(
            "WB_HTTP_TLS_CERTFILE and WB_HTTP_TLS_KEYFILE must both be configured"
        )
    certfile = _readable_regular_file(cert_raw, name="WB_HTTP_TLS_CERTFILE") if cert_raw else None
    keyfile = _readable_regular_file(key_raw, name="WB_HTTP_TLS_KEYFILE") if key_raw else None

    external_bind = _is_external_bind(bind_host)
    allow_raw = env.get("WB_HTTP_ALLOW_INSECURE_EXTERNAL")
    allow_insecure = (
        parse_strict_bool(allow_raw, name="WB_HTTP_ALLOW_INSECURE_EXTERNAL")
        if allow_raw is not None
        else False
    )
    tls_enabled = certfile is not None and keyfile is not None
    if trusted_proxy_hops and external_bind:
        raise HttpTransportConfigurationError(
            "WB_HTTP_TRUSTED_PROXY_HOPS requires a loopback bind so clients cannot bypass "
            "the trusted reverse proxy and spoof forwarding headers"
        )
    if external_bind and not tls_enabled and not allow_insecure:
        raise HttpTransportConfigurationError(
            "external plaintext HTTP is disabled; configure WB HTTP TLS or explicitly set "
            "WB_HTTP_ALLOW_INSECURE_EXTERNAL=1 for an isolated trusted LAN"
        )
    if trusted_proxy_hops and not tls_enabled:
        cookie_secure_raw = env.get("SESSION_COOKIE_SECURE")
        cookie_secure = (
            parse_strict_bool(cookie_secure_raw, name="SESSION_COOKIE_SECURE")
            if cookie_secure_raw is not None
            else False
        )
        if not cookie_secure and not allow_insecure:
            raise HttpTransportConfigurationError(
                "trusted reverse-proxy mode requires SESSION_COOKIE_SECURE=1 for HTTPS "
                "termination, or WB_HTTP_ALLOW_INSECURE_EXTERNAL=1 for an isolated trusted LAN"
            )

    return HttpTransportConfig(
        bind_host=bind_host.strip("[]"),
        port=port,
        tls_certfile=certfile,
        tls_keyfile=keyfile,
        external_bind=external_bind,
        trusted_proxy_hops=trusted_proxy_hops,
        insecure_external_acknowledged=bool(
            (external_bind or trusted_proxy_hops) and not tls_enabled and allow_insecure
        ),
    )


def _is_api_path() -> bool:
    """True if the current request targets an /api/* endpoint.

    Centralised so admin_required / user_required / role_required all use the
    same content-negotiation rule.  Browser-rendered pages keep the existing
    302-to-login UX; XHR/fetch callers get JSON 401/403 instead — see S2.
    """
    try:
        path = request.path or ""
        return path.startswith("/api/") or path == "/metrics"
    except RuntimeError:  # outside request context (shouldn't happen in views)
        return False


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if current_app.config.get("TESTING"):
            return view_func(*args, **kwargs)
        logged_in = session.get("logged_in") is True
        if not logged_in or session.get("role") != "admin":
            # S2 FIX: admin_required redirected non-admin users to /login (HTML)
            # for every protected route, including /api/*. fetch('/api/audit')
            # in templates/logs.html silently followed the 302 and tripped on
            # SyntaxError when parsing the login HTML as JSON — the audit page
            # was broken for non-admin viewers.  Distinguish content type:
            #   * /api/*   -> structured JSON 401 (anon) / 403 (logged-in non-admin)
            #   * non-API  -> keep the legacy 302 redirect to login
            if _is_api_path():
                if not logged_in:
                    return jsonify({"success": False, "error_code": "UNAUTHENTICATED"}), 401
                return jsonify({"success": False, "error_code": "FORBIDDEN"}), 403
            return redirect(url_for("auth_bp.login_page"))
        return view_func(*args, **kwargs)

    return wrapper


def user_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if current_app.config.get("TESTING"):
            return view_func(*args, **kwargs)
        # The explicit passwordless guest flow creates a logged-in read-only
        # viewer session. A bare anonymous role is not enough to expose pages.
        if session.get("logged_in") is not True or session.get("role") not in ["viewer", "user", "admin"]:
            if _is_api_path():
                return jsonify({"success": False, "error_code": "UNAUTHENTICATED"}), 401
            return redirect(url_for("auth_bp.login_page"))
        return view_func(*args, **kwargs)

    return wrapper


def role_required(*roles):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(*args, **kwargs):
            if current_app.config.get("TESTING"):
                return view_func(*args, **kwargs)
            logged_in = session.get("logged_in") is True
            if logged_in and session.get("role") in roles:
                return view_func(*args, **kwargs)
            if _is_api_path():
                if not logged_in:
                    return jsonify({"success": False, "error_code": "UNAUTHENTICATED"}), 401
                return jsonify({"success": False, "error_code": "FORBIDDEN"}), 403
            return redirect(url_for("auth_bp.login_page"))

        return wrapper

    return decorator
