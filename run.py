#!/usr/bin/env python3
"""
Скрипт для запуска WB-Irrigation Flask приложения
"""

import os
from app import app

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8080'))
    try:
        from hypercorn.asyncio import serve
        from hypercorn.config import Config
        import asyncio
        cfg = Config()
        cfg.bind = [f"0.0.0.0:{port}"]
        try:
            # Flask 2.3+: has asgi_app
            asgi_app = app.asgi_app  # type: ignore[attr-defined]
        except Exception:
            # Wrap WSGI into ASGI for Hypercorn
            try:
                from hypercorn.middleware.wsgi import WSGIMiddleware
            except Exception:
                from hypercorn.middleware import wsgi as _wsgi
                WSGIMiddleware = _wsgi.WSGIMiddleware  # type: ignore[attr-defined]
            asgi_app = WSGIMiddleware(app)
        asyncio.run(serve(asgi_app, cfg))
    except Exception:
        # Fallback to Flask dev server
        app.run(debug=False, host='0.0.0.0', port=port)
