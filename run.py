#!/usr/bin/env python3
"""
Скрипт для запуска WB-Irrigation Flask приложения
"""

import logging
import os
import secrets
import signal
import ssl
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request

from dotenv import load_dotenv

from services.security import HttpTransportConfig, local_http_probe_url, resolve_http_transport

# Validate bind/TLS before importing app.py, whose imports initialise SQLite
# and whose module-level boot path reconciles physical state. This also loads
# the same deployment .env that app.py would load later.
load_dotenv()
_STARTUP_HTTP_PROFILE = resolve_http_transport()

from app import app  # transport validation must precede stateful app import

logger = logging.getLogger(__name__)


def _http_executor_workers(flask_app) -> int:
    """Return the explicit WSGI executor size used by Hypercorn."""
    try:
        return max(2, int(flask_app.config.get("HTTP_EXECUTOR_WORKERS", 8)))
    except (AttributeError, TypeError, ValueError):
        return 8


def _graceful_shutdown(signum, frame):
    """Handle SIGTERM/SIGINT: send OFF to all zones, then exit."""
    logger.info("Received signal %s, initiating graceful shutdown...", signum)
    try:
        from services.shutdown import shutdown_all_zones_off

        shutdown_all_zones_off()
    except Exception as exc:
        logger.warning("Graceful shutdown error: %s", exc)
    sys.exit(0)


def _get_asgi_app(flask_app):
    """Get ASGI app from Flask, with fallback to WSGIMiddleware."""
    # Flask 2.3+: native ASGI
    if hasattr(flask_app, "asgi_app"):
        return flask_app.asgi_app

    # Hypercorn WSGIMiddleware (name varies by version)
    try:
        from hypercorn.middleware.wsgi import WSGIMiddleware

        return WSGIMiddleware(flask_app)
    except (ImportError, AttributeError):
        pass

    try:
        from hypercorn.middleware.wsgi import AsyncioWSGIMiddleware

        return AsyncioWSGIMiddleware(flask_app)
    except (ImportError, AttributeError):
        pass

    try:
        from hypercorn.middleware import wsgi as _wsgi

        if hasattr(_wsgi, "AsyncioWSGIMiddleware"):
            return _wsgi.AsyncioWSGIMiddleware(flask_app)
        if hasattr(_wsgi, "_WSGIMiddleware"):
            return _wsgi._WSGIMiddleware(flask_app)
    except (ImportError, AttributeError):
        pass

    raise ImportError("Cannot find WSGI-to-ASGI middleware in hypercorn")


def _listener_probe_url(profile: HttpTransportConfig) -> str:
    return local_http_probe_url(profile, path="/healthz")


def _probe_http_listener(
    url: str,
    startup_token: str,
    *,
    urlopen_fn=None,
    timeout_sec: float = 1.0,
) -> bool:
    """Prove the listener belongs to this process, not an old port owner."""
    if urlopen_fn is None:
        from urllib.request import urlopen as urlopen_fn

    request = Request(url, headers={"X-WB-Startup-Probe": startup_token})
    kwargs = {"timeout": max(0.05, float(timeout_sec))}
    if url.lower().startswith("https://"):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        kwargs["context"] = context
    try:
        with urlopen_fn(request, **kwargs) as response:
            echoed = str(getattr(response, "headers", {}).get("X-WB-Startup-Probe") or "")
            return int(getattr(response, "status", 0)) == 200 and secrets.compare_digest(echoed, startup_token)
    except (OSError, TimeoutError, TypeError, ValueError):
        return False


def _listener_ready_loop(
    flask_app,
    profile: HttpTransportConfig,
    startup_token: str,
    stop_event: threading.Event,
) -> None:
    url = _listener_probe_url(profile)
    while not stop_event.is_set():
        if _probe_http_listener(url, startup_token):
            try:
                from services.app_init import notify_http_listener_ready

                if notify_http_listener_ready(health_probe_url=url):
                    return
            except Exception:
                logger.exception("HTTP listener is accepting requests but READY=1 notification failed")
        stop_event.wait(0.1)


def _start_listener_ready_notifier(flask_app, profile: HttpTransportConfig):
    stop_event = threading.Event()
    if not os.environ.get("NOTIFY_SOCKET"):
        return None, stop_event
    startup_token = secrets.token_urlsafe(32)
    flask_app.config["HTTP_STARTUP_PROBE_TOKEN"] = startup_token
    thread = threading.Thread(
        target=_listener_ready_loop,
        args=(flask_app, profile, startup_token, stop_event),
        name="http-listener-ready",
        daemon=True,
    )
    thread.start()
    return thread, stop_event


def main(flask_app=app, *, profile: HttpTransportConfig | None = None) -> int:
    profile = profile or _STARTUP_HTTP_PROFILE
    if profile.insecure_external_acknowledged:
        logger.critical(
            "External plaintext HTTP explicitly enabled on %s; admin credentials and sessions are not transport-secure",
            profile.bind,
        )

    # Register signal handlers before starting the server
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    ready_thread, ready_stop = _start_listener_ready_notifier(flask_app, profile)
    try:
        import asyncio

        from hypercorn.asyncio import serve
        from hypercorn.config import Config

        cfg = Config()
        cfg.bind = [profile.bind]
        if profile.tls_enabled:
            cfg.certfile = profile.tls_certfile
            cfg.keyfile = profile.tls_keyfile
        asgi_app = _get_asgi_app(flask_app)

        async def _serve_with_bounded_executor():
            loop = asyncio.get_running_loop()
            executor = ThreadPoolExecutor(
                max_workers=_http_executor_workers(flask_app),
                thread_name_prefix="wb-http",
            )
            loop.set_default_executor(executor)
            await serve(asgi_app, cfg)

        asyncio.run(_serve_with_bounded_executor())
    except ImportError:
        # Fallback to Flask dev server
        logger.info("Hypercorn not available, using Flask dev server")
        ssl_context = (profile.tls_certfile, profile.tls_keyfile) if profile.tls_enabled else None
        flask_app.run(
            debug=False,
            host=profile.bind_host,
            port=profile.port,
            ssl_context=ssl_context,
        )
    finally:
        ready_stop.set()
        if ready_thread is not None and ready_thread is not threading.current_thread():
            ready_thread.join(timeout=2.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
