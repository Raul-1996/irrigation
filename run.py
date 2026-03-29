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
        try:
            # Flask 2.3+: has asgi_app
            asgi_app = app.asgi_app  # type: ignore[attr-defined]
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Flask has no asgi_app, using WSGIMiddleware: %s", e)
            # Wrap WSGI into ASGI for Hypercorn
            try:
                from hypercorn.middleware.wsgi import WSGIMiddleware
            except ImportError as e:
                logger.debug("Exception in line_25: %s", e)
                from hypercorn.middleware import wsgi as _wsgi
                WSGIMiddleware = _wsgi.WSGIMiddleware  # type: ignore[attr-defined]
            asgi_app = WSGIMiddleware(app)
        asyncio.run(serve(asgi_app, cfg))
    except ImportError as e:
        logger.debug("Exception in line_31: %s", e)
        # Fallback to Flask dev server
        app.run(debug=False, host='0.0.0.0', port=port)
