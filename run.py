#!/usr/bin/env python3
"""
Скрипт для запуска WB-Irrigation Flask приложения
"""

import os
import signal
import sys
import logging
from app import app

logger = logging.getLogger(__name__)


def _graceful_shutdown(signum, frame):
    """Handle SIGTERM/SIGINT: send OFF to all zones, then exit."""
    logger.info('Received signal %s, initiating graceful shutdown...', signum)
    try:
        from services.shutdown import shutdown_all_zones_off
        shutdown_all_zones_off()
    except Exception as exc:
        logger.warning('Graceful shutdown error: %s', exc)
    sys.exit(0)


def _get_asgi_app(flask_app):
    """Get ASGI app from Flask, with fallback to WSGIMiddleware."""
    # Flask 2.3+: native ASGI
    if hasattr(flask_app, 'asgi_app'):
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
        if hasattr(_wsgi, 'AsyncioWSGIMiddleware'):
            return _wsgi.AsyncioWSGIMiddleware(flask_app)
        if hasattr(_wsgi, '_WSGIMiddleware'):
            return _wsgi._WSGIMiddleware(flask_app)
    except (ImportError, AttributeError):
        pass

    raise ImportError("Cannot find WSGI-to-ASGI middleware in hypercorn")


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8080'))

    # Register signal handlers before starting the server
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    try:
        from hypercorn.asyncio import serve
        from hypercorn.config import Config
        import asyncio
        cfg = Config()
        cfg.bind = [f"0.0.0.0:{port}"]
        asgi_app = _get_asgi_app(app)
        asyncio.run(serve(asgi_app, cfg))
    except ImportError:
        # Fallback to Flask dev server
        logger.info("Hypercorn not available, using Flask dev server")
        app.run(debug=False, host='0.0.0.0', port=port)
