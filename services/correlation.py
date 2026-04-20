"""Wave 2 F3 — correlation ID (X-Request-ID) middleware support.

Every HTTP request carries a unique ID that:
  1. Is read from the incoming `X-Request-ID` header if well-formed, else
     from `X-Correlation-ID` alias, else auto-generated (UUIDv4).
  2. Is bound to a :class:`contextvars.ContextVar` so the JSON log formatter
     (F1 :class:`services.logging_setup.WBJsonFormatter`) automatically emits
     it as top-level `correlation_id` / `request_id` fields.
  3. Is echoed back as the `X-Request-ID` response header.
  4. Is isolated per-request / per-thread via ContextVar semantics — no
     explicit per-call plumbing required.

The header is validated against a strict regex to prevent log-injection
and keep downstream consumers (jq, Loki, Grafana search) safe.
"""
from __future__ import annotations

import re
import uuid
from contextvars import ContextVar, Token
from typing import Optional


# ── ContextVar ──────────────────────────────────────────────────────────────
# `default=None` lets :func:`get_correlation_id` return None when no request
# is in flight (CLI jobs, APScheduler ticks, tests).  The WBJsonFormatter uses
# that to omit the key entirely rather than emit `"correlation_id": null`.
correlation_id_var: ContextVar[Optional[str]] = ContextVar(
    'wb_correlation_id', default=None,
)


# ── Validation ──────────────────────────────────────────────────────────────
# Rules:
#   * printable ASCII only (logs are scanned by jq/grep — control chars break this)
#   * no shell-special / quote characters (prevents log-injection)
#   * 8..64 chars (long enough to be useful, short enough not to bloat logs)
# Anything failing the regex is rejected — caller then generates a fresh UUID.
_CID_RE = re.compile(r'^[A-Za-z0-9\-_]{8,64}$')


def validate_correlation_id(raw: Optional[str]) -> Optional[str]:
    """Return ``raw`` when it matches :data:`_CID_RE`; else ``None``.

    Examples::

        validate_correlation_id(None)            == None
        validate_correlation_id('')              == None
        validate_correlation_id('  abc123xyz ')  == 'abc123xyz'   # trimmed, 9 chars, accepted
        validate_correlation_id('abc')           == None           # too short
        validate_correlation_id("'; DROP ..")    == None           # illegal chars
        validate_correlation_id('A' * 100)       == None           # too long
        validate_correlation_id('trace-123_xyz') == 'trace-123_xyz'
    """
    if not raw:
        return None
    try:
        trimmed = raw.strip()
    except AttributeError:
        return None
    if _CID_RE.match(trimmed):
        return trimmed
    return None


def generate_correlation_id() -> str:
    """Generate a fresh UUIDv4 as a correlation ID.

    Length 36, charset limited to `[0-9a-f-]` — always passes
    :func:`validate_correlation_id`.
    """
    return str(uuid.uuid4())


# ── ContextVar helpers ──────────────────────────────────────────────────────

def get_correlation_id() -> Optional[str]:
    """Return the current request's correlation ID or ``None``.

    Read by :class:`services.logging_setup.WBJsonFormatter` on every log
    record — keep it fast (single ContextVar.get()).
    """
    return correlation_id_var.get()


def set_correlation_id(value: str) -> Token:
    """Bind ``value`` to the current context; return the reset token.

    The caller (typically Flask `before_request`) must later call
    :meth:`contextvars.ContextVar.reset` with this token in `teardown_request`
    to prevent leakage across requests on the same worker thread.
    """
    return correlation_id_var.set(value)


def reset_correlation_id(token: Token) -> None:
    """Restore the ContextVar state captured by ``token``.

    Swallows :class:`ValueError` when the context was already torn down
    (e.g. exception during request handling left Flask in a weird state).
    """
    try:
        correlation_id_var.reset(token)
    except (ValueError, LookupError):
        pass


# ── Header extraction (called by app.py before_request) ─────────────────────

def extract_or_generate(headers) -> str:
    """Extract a correlation ID from request headers or generate a fresh one.

    Accepts either a Flask/Werkzeug :class:`EnvironHeaders` (case-insensitive
    `.get()`) or a plain dict.  Checks `X-Request-ID` first, then
    `X-Correlation-ID` (industry-convention alias, Q3).
    """
    raw = None
    try:
        raw = headers.get('X-Request-ID')
        if not raw:
            raw = headers.get('X-Correlation-ID')
    except (AttributeError, KeyError):
        raw = None
    return validate_correlation_id(raw) or generate_correlation_id()
