"""General-purpose API rate limiter (per-IP, per-endpoint-group).

Provides a Flask decorator that returns 429 when a client exceeds the
configured request rate for a given endpoint group.

The existing ``rate_limiter.py`` handles login-specific lockout logic;
this module covers all other mutating API endpoints.
"""

import functools
import logging
import threading
import time
from typing import Dict, List, Tuple

from flask import jsonify, request

logger = logging.getLogger(__name__)

# {(ip, group_name): [timestamp, ...]}
_REQUESTS: Dict[Tuple[str, str], List[float]] = {}
_LOCK = threading.Lock()

# Prune old entries every N calls to avoid unbounded memory growth
_CALL_COUNT = 0
_PRUNE_EVERY = 500


def _prune_old(now: float, window: float = 120.0) -> None:
    """Remove entries older than *window* seconds."""
    cutoff = now - window
    keys_to_delete = []
    for key, timestamps in _REQUESTS.items():
        _REQUESTS[key] = [ts for ts in timestamps if ts > cutoff]
        if not _REQUESTS[key]:
            keys_to_delete.append(key)
    for k in keys_to_delete:
        del _REQUESTS[k]


def _is_allowed(ip: str, group: str, max_requests: int, window_sec: int) -> Tuple[bool, int]:
    """Check if *ip* may proceed for *group*.

    Returns (allowed, retry_after_seconds).
    """
    global _CALL_COUNT
    now = time.time()
    key = (ip, group)

    with _LOCK:
        _CALL_COUNT += 1
        if _CALL_COUNT % _PRUNE_EVERY == 0:
            _prune_old(now)

        timestamps = _REQUESTS.get(key, [])
        cutoff = now - window_sec
        recent = [ts for ts in timestamps if ts > cutoff]

        if len(recent) >= max_requests:
            # Calculate when the oldest relevant request will expire
            retry_after = int(recent[0] - cutoff) + 1
            _REQUESTS[key] = recent
            return False, max(1, retry_after)

        recent.append(now)
        _REQUESTS[key] = recent
        return True, 0


def rate_limit(group: str, max_requests: int = 30, window_sec: int = 60):
    """Decorator factory: limits requests per IP for the given *group*.

    Usage::

        @app.route('/api/foo', methods=['POST'])
        @rate_limit('foo', max_requests=10, window_sec=60)
        def api_foo():
            ...

    Returns HTTP 429 with ``Retry-After`` header when limit is exceeded.
    Skips rate-limiting when ``app.config['TESTING']`` is truthy.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # Skip in test mode
            try:
                from flask import current_app
                if current_app.config.get('TESTING'):
                    return fn(*args, **kwargs)
            except RuntimeError:
                pass

            ip = request.remote_addr or '0.0.0.0'
            allowed, retry_after = _is_allowed(ip, group, max_requests, window_sec)
            if not allowed:
                resp = jsonify({
                    'success': False,
                    'message': 'Too many requests',
                    'error_code': 'RATE_LIMITED',
                    'retry_after': retry_after,
                })
                resp.status_code = 429
                resp.headers['Retry-After'] = str(retry_after)
                return resp
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def reset_all() -> None:
    """Clear all rate limit state (useful in tests)."""
    with _LOCK:
        _REQUESTS.clear()
