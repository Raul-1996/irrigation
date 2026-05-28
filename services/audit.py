"""Audit decorator for Flask mutation routes.

Wraps a route handler so that every invocation is recorded in ``audit_log``
with action_type / target / payload (filtered) / actor / IP / duration / result.

Usage:

    from services.audit import audit_log

    @bp.route('/api/zones/<int:zone_id>/start', methods=['POST'])
    @audit_log('zone_manual_start',
               target_extractor=lambda *a, **kw: f"zone:{kw.get('zone_id')}")
    def start_zone(zone_id):
        ...

Behaviour:
  - Records on success AND on exception (with error_msg + result='error').
  - Skips GET / HEAD / OPTIONS (audit is for mutations only — applying the
    decorator to multi-method routes is safe).
  - Best-effort: a failure to write the audit row never breaks the handler.
  - Strips secrets from payload via key blacklist.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from typing import Any, Callable, Iterable

# werkzeug HTTPException — needed so we can classify 4xx aborts as
# `failure:{code}` instead of bucket them with real handler errors.  Import
# guard keeps the module loadable in environments where flask/werkzeug isn't
# present (tooling, schema-migration scripts, etc.).
try:
    from werkzeug.exceptions import HTTPException as _WerkzeugHTTPException
except Exception:
    _WerkzeugHTTPException = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ─── Debug-flag TTL cache ──────────────────────────────────────────────────
# `debug_audit()` is called on every MQTT publish / scheduler timer plant —
# hitting SQLite for `settings.logging.debug` on each call would melt the
# Wirenboard CPU. We cache the flag for a short TTL (5s by default) and
# invalidate explicitly when the toggle endpoint flips it.
_DEBUG_FLAG_TTL_SEC = 5.0
_DEBUG_FLAG_LOCK = threading.Lock()
_DEBUG_FLAG_CACHE: dict[str, Any] = {"value": False, "fetched_at": 0.0}


def _is_debug_audit_enabled() -> bool:
    """Return ``settings.logging.debug`` with a short TTL cache.

    Best-effort — any failure (DB busy, import problem) returns False so we
    err on the side of less audit noise rather than more.
    """
    now = time.time()
    with _DEBUG_FLAG_LOCK:
        cached_at = _DEBUG_FLAG_CACHE.get("fetched_at") or 0.0
        if (now - cached_at) < _DEBUG_FLAG_TTL_SEC:
            return bool(_DEBUG_FLAG_CACHE.get("value"))
    # Miss — refresh outside the lock to avoid blocking concurrent readers.
    try:
        from database import db as _db  # local import — avoid circular

        val = bool(_db.get_logging_debug())
    except Exception:
        val = False
    with _DEBUG_FLAG_LOCK:
        _DEBUG_FLAG_CACHE["value"] = val
        _DEBUG_FLAG_CACHE["fetched_at"] = now
    return val


def invalidate_debug_audit_cache() -> None:
    """Force re-read of the debug flag on next ``debug_audit`` call.

    Call from the ``/api/logging/debug`` toggle endpoint and from the
    auto-off APScheduler job so flipping the flag takes effect immediately
    instead of waiting up to ``_DEBUG_FLAG_TTL_SEC`` seconds.
    """
    with _DEBUG_FLAG_LOCK:
        _DEBUG_FLAG_CACHE["fetched_at"] = 0.0


# Keys whose values must NEVER be logged to audit_log payload.
_SECRET_KEY_FRAGMENTS = (
    "password",
    "token",
    "secret",
    "api_key",
    "apikey",
    "csrf",
    "session",
    "authorization",
    "cookie",
)

# Cap for individual payload values (avoid runaway logging of large blobs).
_MAX_VALUE_LEN = 1024
_MAX_PAYLOAD_KEYS = 64
# H4: hard cap on recursion depth — defends against malicious deeply-nested
# JSON bodies (or pathological self-referential structures from upstream
# code) that would otherwise blow the Python stack while we recurse from
# audit-logging.  Anything past this depth is summarised.
_MAX_REDACT_DEPTH = 8


def _is_secret_key(key: str) -> bool:
    try:
        kl = str(key).lower()
    except (TypeError, AttributeError):
        return False
    return any(frag in kl for frag in _SECRET_KEY_FRAGMENTS)


def _redact(value: Any, _depth: int = 0) -> Any:
    """Truncate strings, recurse into dicts/lists, drop secret keys.

    The optional ``_depth`` argument is internal; callers should not pass it.
    Recursion is capped at :data:`_MAX_REDACT_DEPTH` so that adversarial
    deeply-nested input cannot overflow the stack via audit logging.
    """
    if _depth >= _MAX_REDACT_DEPTH:
        # Summarise without further recursion.
        if isinstance(value, dict):
            return {"__truncated_depth__": True, "__keys__": len(value)}
        if isinstance(value, (list, tuple)):
            return ["__truncated_depth__", f"len={len(value)}"]
        if isinstance(value, str):
            return value[:_MAX_VALUE_LEN] + ("…(truncated)" if len(value) > _MAX_VALUE_LEN else "")
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        try:
            return str(value)[:_MAX_VALUE_LEN]
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_PAYLOAD_KEYS:
                out["__truncated__"] = True
                break
            if _is_secret_key(k):
                out[str(k)] = "***"
            else:
                out[str(k)] = _redact(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v, _depth + 1) for v in list(value)[:_MAX_PAYLOAD_KEYS]]
    if isinstance(value, str):
        if len(value) > _MAX_VALUE_LEN:
            return value[:_MAX_VALUE_LEN] + "…(truncated)"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # Fallback: stringify
    try:
        s = str(value)
    except (TypeError, ValueError):
        return None
    return s[:_MAX_VALUE_LEN]


def _extract_payload(req) -> dict[str, Any] | None:
    """Build a redacted dict from request JSON body + form fields."""
    payload: dict[str, Any] = {}
    try:
        body = req.get_json(silent=True)
        if isinstance(body, dict):
            for k, v in body.items():
                payload[str(k)] = v
        elif body is not None:
            payload["__body__"] = body
    except (ValueError, TypeError, AttributeError) as e:
        logger.debug("audit payload: get_json failed: %s", e)
    try:
        form = req.form.to_dict() if req.form else {}
        for k, v in form.items():
            payload.setdefault(str(k), v)
    except (AttributeError, ValueError, TypeError) as e:
        logger.debug("audit payload: form failed: %s", e)
    try:
        # Include query string args for completeness
        args = req.args.to_dict() if req.args else {}
        for k, v in args.items():
            payload.setdefault(f"__qs__{k}", v)
    except (AttributeError, ValueError, TypeError) as e:
        logger.debug("audit payload: args failed: %s", e)

    if not payload:
        return None
    return _redact(payload)


def _resolve_actor(req) -> str:
    """Pull the actor name from Flask session (admin/viewer/guest)."""
    try:
        from flask import session  # local import — avoid circular at module load

        if not session:
            return "guest"
        user = session.get("user") or session.get("username")
        if user:
            return str(user)
        role = session.get("role")
        return str(role) if role else "guest"
    except (ImportError, RuntimeError, AttributeError):
        return "guest"


def _resolve_ip(req) -> str | None:
    """Return the real client IP from the WSGI environment.

    B11: do NOT read X-Forwarded-For directly here. ProxyFix (enabled via
    TRUSTED_PROXY=1 in app.py) is the single place that may rewrite
    request.remote_addr from XFF. Without ProxyFix, XFF is spoofable from any
    client and must NOT be used for audit attribution.
    """
    try:
        return req.remote_addr
    except (AttributeError, KeyError):
        return None


def _safe_status_code(resp: Any) -> int | None:
    """Best-effort extraction of HTTP status from a Flask handler return value."""
    try:
        # Flask Response object
        if hasattr(resp, "status_code"):
            return int(resp.status_code)
        # (body, code) or (body, code, headers)
        if isinstance(resp, tuple) and len(resp) >= 2:
            cand = resp[1]
            if isinstance(cand, int):
                return int(cand)
        return 200
    except (TypeError, ValueError, AttributeError):
        return None


def audit_log(
    action_type: str,
    target_extractor: Callable[..., str] | None = None,
    payload_filter: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    source: str = "api",
    skip_methods: Iterable[str] = ("GET", "HEAD", "OPTIONS"),
):
    """Return a decorator that records a row in ``audit_log`` per call.

    Args:
        action_type: short snake_case identifier (e.g. ``zone_manual_start``).
        target_extractor: optional callable ``(*args, **kwargs) -> str`` that
            yields a target string like ``"zone:5"``.  Failures are swallowed.
        payload_filter: optional final transform on the redacted payload
            dict — e.g. to drop additional fields or whitelist specific ones.
        source: ``'api'`` (default), ``'ui'``, ``'scheduler'`` etc.
        skip_methods: HTTP methods that should NOT be recorded (default: read-only).
    """
    skip_set = {m.upper() for m in skip_methods}

    def decorator(fn: Callable):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            from flask import request  # local import — no Flask dep at module load

            t0 = time.time()
            method = None
            try:
                method = (request.method or "").upper()
            except (RuntimeError, AttributeError):
                method = None

            # Skip read-only methods entirely (still execute handler)
            if method and method in skip_set:
                return fn(*args, **kwargs)

            # Resolve metadata BEFORE handler runs (in case handler clears session)
            actor = _resolve_actor(request)
            ip = _resolve_ip(request)
            target: str | None = None
            if target_extractor is not None:
                try:
                    target = str(target_extractor(*args, **kwargs))
                except (TypeError, ValueError, KeyError, AttributeError) as e:
                    logger.debug("audit target_extractor failed: %s", e)
                    target = None

            payload = _extract_payload(request)
            if payload is not None and payload_filter is not None:
                try:
                    payload = payload_filter(payload)
                except (TypeError, ValueError, KeyError) as e:
                    logger.debug("audit payload_filter failed: %s", e)

            error_msg: str | None = None
            result_str = "success"
            status_code: int | None = None
            handler_result: Any = None
            try:
                handler_result = fn(*args, **kwargs)
                status_code = _safe_status_code(handler_result)
                if status_code is not None:
                    if 200 <= status_code < 400:
                        result_str = f"success:{status_code}"
                    else:
                        result_str = f"failure:{status_code}"
                return handler_result
            except Exception as exc:
                # Audit any *handler* failure but DO NOT swallow KeyboardInterrupt,
                # SystemExit, or GeneratorExit (those are BaseException not
                # Exception).  The previous ``except BaseException`` branch
                # was catching shutdown/cancel signals and turning them into
                # noisy audit rows while still re-raising — but it could also
                # mask issues in the asyncio runtime by intercepting the
                # cancel sequence.  Catch only Exception subclasses; the
                # ``finally`` block below still runs for BaseException
                # propagation, so the audit row gets written if a write was
                # in flight.
                #
                # S1 FIX: werkzeug HTTPException 4xx is a CLIENT problem, not
                # an audit-pipeline error.  Things like
                # ``request.get_json()`` raising BadRequest on malformed JSON
                # used to mark every such call as result='error', drowning
                # legitimate server-side audit failures in noise (and
                # firing ops alerts daily).  Reclassify 4xx as
                # ``failure:{code}`` (mirrors the explicit-status path
                # above) and leave error_msg None.  5xx HTTPException and
                # all other Exception subclasses still flag 'error'.
                if _WerkzeugHTTPException is not None and isinstance(exc, _WerkzeugHTTPException):
                    code = getattr(exc, "code", None)
                    if isinstance(code, int) and 400 <= code < 500:
                        status_code = code
                        result_str = f"failure:{code}"
                        raise
                error_msg = f"{type(exc).__name__}: {exc}"[:512]
                result_str = "error"
                raise
            finally:
                duration_ms = int((time.time() - t0) * 1000)
                # Best-effort write — never raise from inside the audit hook
                try:
                    from database import db as _db  # local — avoid circular import

                    _db.add_audit(
                        action_type=action_type,
                        source=source,
                        target=target,
                        payload=payload,
                        result=result_str,
                        error=error_msg,
                        ip=ip,
                        duration_ms=duration_ms,
                        actor=actor,
                    )
                except Exception:
                    logger.exception("audit_log decorator: add_audit failed (action=%s)", action_type)

        return wrapper

    return decorator


# ─── Debug-only emit ───────────────────────────────────────────────────────
def debug_audit(
    action_type: str,
    source: str = "system",
    target: str | None = None,
    payload: Any = None,
    actor: str | None = "system",
    duration_ms: int | None = None,
    result: str = "debug",
) -> None:
    """Emit an audit row only when ``settings.logging.debug`` is enabled.

    Used for high-volume diagnostic events (MQTT publishes, scheduler timer
    plant/cancel/fire, program-queue transitions) that would overflow
    audit_log in normal operation.

    Best-effort: any failure (DB busy, import problem) is logged but never
    propagates — audit must never break the hot path.
    """
    try:
        if not _is_debug_audit_enabled():
            return
        from database import db as _db  # local — avoid circular at module load

        if isinstance(payload, dict):
            payload = _redact(payload)
        _db.add_audit(
            action_type=action_type,
            source=source,
            target=target,
            payload=payload,
            result=result,
            error=None,
            ip=None,
            duration_ms=duration_ms,
            actor=actor,
        )
    except Exception:
        logger.exception("debug_audit failed (action=%s)", action_type)


# Convenience helper for non-route call sites (scheduler jobs, MQTT callbacks).
def record_audit(
    action_type: str,
    source: str = "scheduler",
    target: str | None = None,
    payload: Any = None,
    result: str = "success",
    error: str | None = None,
    actor: str | None = "system",
    duration_ms: int | None = None,
):
    """Direct audit-row insert for non-HTTP code paths.  Best-effort."""
    try:
        from database import db as _db

        if isinstance(payload, dict):
            payload = _redact(payload)
        _db.add_audit(
            action_type=action_type,
            source=source,
            target=target,
            payload=payload,
            result=result,
            error=error,
            ip=None,
            duration_ms=duration_ms,
            actor=actor,
        )
    except Exception:
        logger.exception("record_audit failed (action=%s)", action_type)
