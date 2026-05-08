/**
 * audit.js — UI-side audit hook for WB-Irrigation.
 *
 * Picks up clicks on any element decorated with [data-audit-action] and
 * submits of <form data-audit-action="..."> and POSTs a small JSON record
 * to /api/audit/ui.  Designed to be:
 *   - dependency-free (vanilla JS, attached on DOMContentLoaded)
 *   - resilient (uses fetch with keepalive; falls back to navigator.sendBeacon)
 *   - silent on failure (an audit failure must NEVER break the UI)
 *
 * Markup contract (clicks):
 *   <button data-audit-action="zone_start_click"
 *           data-audit-target="zone:5"
 *           data-audit-context='{"src":"zones-table"}'>Полить</button>
 *
 * Markup contract (forms):
 *   <form data-audit-action="login_attempt"
 *         data-audit-target="auth"> ... </form>
 *   On submit, FormData is captured into context, with redaction of any field
 *   whose name matches /password|token|secret|csrf/i (also trimmed to ~64 keys).
 *   A field can opt-in to *also* having its value emitted (in addition to
 *   inclusion in the form payload) via:
 *     <input data-audit-include-value="true" ...>
 *
 * Optional element attributes:
 *   data-audit-target   — short string e.g. "zone:5", "program:3"
 *   data-audit-context  — JSON-serialised object (free-form) — redacted
 *                         server-side, but still keep it small (<256 chars).
 *
 * Public API (window.WBAudit):
 *   WBAudit.record(action, target, context)  — programmatic emit
 */
(function () {
  'use strict';

  var ENDPOINT = '/api/audit/ui';
  var MAX_PAYLOAD_BYTES = 4 * 1024;          // hard cap to protect SQLite
  var THROTTLE_MS = 100;                      // debounce repeated clicks/submits
  var MAX_FORM_FIELDS = 64;                   // trim huge forms
  var MAX_FIELD_VALUE_LEN = 256;              // trim each value

  // Sensitive field name patterns — values redacted client-side BEFORE leaving
  // the browser. Server-side redaction in services/audit.py:_redact is the
  // ultimate authority, but we redact early to minimise blast radius.
  var SENSITIVE_RE = /(password|passwd|pwd|token|secret|api_?key|auth|csrf|otp)/i;

  // throttle bookkeeping: last-emit timestamp keyed by action_type
  var _lastEmitAt = Object.create(null);

  function getCsrfToken() {
    try {
      var meta = document.querySelector('meta[name="csrf-token"]');
      if (meta && meta.content) return meta.content;
      // Fallback: hidden input rendered by Flask-WTF
      var inp = document.querySelector('input[name="csrf_token"]');
      return inp ? inp.value : null;
    } catch (e) {
      return null;
    }
  }

  function safeJsonParse(s) {
    if (!s) return null;
    try { return JSON.parse(s); } catch (e) { return { raw: String(s).slice(0, 200) }; }
  }

  function send(payload) {
    try {
      var body = JSON.stringify(payload);
      if (body.length > MAX_PAYLOAD_BYTES) {
        body = JSON.stringify({
          action: payload.action,
          target: payload.target,
          context: { __truncated__: true }
        });
      }
      var token = getCsrfToken();
      // Prefer fetch with keepalive — sendBeacon would not send Content-Type
      // application/json reliably across browsers, and our endpoint is
      // CSRF-exempt anyway (registered in _ALLOWED_PUBLIC_POSTS).
      if (typeof fetch === 'function') {
        var headers = { 'Content-Type': 'application/json' };
        if (token) headers['X-CSRFToken'] = token;
        fetch(ENDPOINT, {
          method: 'POST',
          credentials: 'same-origin',
          keepalive: true,
          headers: headers,
          body: body
        }).catch(function () { /* swallow — audit must never break UI */ });
        return;
      }
      // Last-resort beacon (no CSRF header possible)
      if (navigator && typeof navigator.sendBeacon === 'function') {
        var blob = new Blob([body], { type: 'application/json' });
        navigator.sendBeacon(ENDPOINT, blob);
      }
    } catch (e) {
      // never throw
    }
  }

  function record(action, target, context) {
    if (!action) return;
    var actionStr = String(action).slice(0, 64);
    // throttle: drop repeated emits of the same action within THROTTLE_MS
    var now = (typeof performance !== 'undefined' && performance.now)
      ? performance.now() : Date.now();
    var last = _lastEmitAt[actionStr] || 0;
    if (now - last < THROTTLE_MS) return;
    _lastEmitAt[actionStr] = now;
    send({
      action: actionStr,
      target: target ? String(target).slice(0, 128) : null,
      context: context || null
    });
  }

  /**
   * Parse a comma/whitespace-separated whitelist from `data-audit-fields`.
   * Returns null when the attribute is absent — callers treat that as
   * "no whitelist, fall back to default redaction".
   * Returns a Set of allowed field names when present (possibly empty).
   *
   * H6: opt-in whitelist.  On security-sensitive forms (e.g. login) the
   * default behaviour of capturing every non-sensitive field is too greedy —
   * leaks the username field by default.  With this opt-in, the form author
   * can declare exactly which fields are safe to record.  Sensitive
   * patterns (password, token, …) are still redacted on top of the
   * whitelist as a defence-in-depth measure.
   */
  function parseAuditFields(form) {
    try {
      if (!form || !form.dataset) return null;
      var raw = form.dataset.auditFields;
      if (raw === undefined || raw === null) return null;
      var allowed = Object.create(null);
      var parts = String(raw).split(/[\s,]+/);
      for (var i = 0; i < parts.length; i++) {
        var name = parts[i].trim();
        if (name) allowed[name] = true;
      }
      return allowed;
    } catch (e) {
      return null;
    }
  }

  /**
   * Convert a FormData-like form into a plain object with redaction.
   * - Skips fields whose name matches SENSITIVE_RE (replaced with '[REDACTED]').
   * - Trims each value to MAX_FIELD_VALUE_LEN chars.
   * - Caps total fields at MAX_FORM_FIELDS.
   * - Skips File objects (we never want raw binary in audit).
   * - If the form has `data-audit-fields="a,b,c"`, ONLY those fields are
   *   captured (others are dropped silently).  This is the H6 opt-in
   *   whitelist for forms with sensitive content (e.g. login).
   */
  function formToObject(form) {
    var out = {};
    if (!form || typeof form.elements === 'undefined') return out;
    var count = 0;
    var allowed = parseAuditFields(form);
    try {
      var fd;
      try { fd = new FormData(form); } catch (e) { fd = null; }
      if (fd && typeof fd.entries === 'function') {
        var it = fd.entries();
        var step;
        while ((step = it.next()) && !step.done) {
          if (count >= MAX_FORM_FIELDS) { out.__truncated__ = true; break; }
          var key = step.value[0];
          var val = step.value[1];
          if (!key) continue;
          if (allowed && !allowed[key]) continue;
          if (val && typeof File !== 'undefined' && val instanceof File) {
            out[key] = '[File:' + (val.name || '?') + ']';
          } else if (SENSITIVE_RE.test(key)) {
            out[key] = '[REDACTED]';
          } else {
            var s = String(val == null ? '' : val);
            if (s.length > MAX_FIELD_VALUE_LEN) s = s.slice(0, MAX_FIELD_VALUE_LEN) + '…';
            out[key] = s;
          }
          count++;
        }
        return out;
      }
      // Fallback: walk form.elements manually
      for (var i = 0; i < form.elements.length; i++) {
        if (count >= MAX_FORM_FIELDS) { out.__truncated__ = true; break; }
        var el = form.elements[i];
        if (!el || !el.name) continue;
        if (el.type === 'file' || el.type === 'button' || el.type === 'submit') continue;
        if (allowed && !allowed[el.name]) continue;
        if (SENSITIVE_RE.test(el.name)) {
          out[el.name] = '[REDACTED]';
        } else {
          var v = el.type === 'checkbox' ? (el.checked ? '1' : '0') : (el.value == null ? '' : String(el.value));
          if (v.length > MAX_FIELD_VALUE_LEN) v = v.slice(0, MAX_FIELD_VALUE_LEN) + '…';
          out[el.name] = v;
        }
        count++;
      }
    } catch (e) {
      // never throw
    }
    return out;
  }

  /**
   * Walk up from `el` looking for nearest [data-audit-action] ancestor.
   * Returns null if none found.
   */
  function findAuditElement(el) {
    while (el && el !== document) {
      if (el.dataset && el.dataset.auditAction) return el;
      el = el.parentNode;
    }
    return null;
  }

  function onDocClick(ev) {
    try {
      var startEl = (ev.target && ev.target.nodeType === 1) ? ev.target : null;
      if (!startEl) return;
      var el = findAuditElement(startEl);
      if (!el) return;
      // We deliberately ONLY log clicks on elements with explicit
      // data-audit-action. Tab toggles, modal open/close, and other navigation
      // primitives MUST NOT have that attribute (per product decision).
      var action = el.dataset.auditAction;
      var target = el.dataset.auditTarget || null;
      var ctx = el.dataset.auditContext ? safeJsonParse(el.dataset.auditContext) : null;
      // Auto-enrich context with current page path — useful when the same
      // action_type is used on multiple pages.
      try {
        var page = (typeof location !== 'undefined' && location.pathname) ? location.pathname : null;
        if (page) {
          ctx = ctx && typeof ctx === 'object' ? ctx : {};
          if (typeof ctx.page === 'undefined') ctx.page = page;
        }
      } catch (e) { /* ignore */ }
      // Optional: if element opts in via data-audit-include-value AND is an
      // input/select/textarea with a value, attach it (post-redaction).
      try {
        if (el.dataset.auditIncludeValue && 'value' in el) {
          var raw = String(el.value == null ? '' : el.value);
          var name = el.name || el.id || 'value';
          ctx = ctx && typeof ctx === 'object' ? ctx : {};
          ctx.value = SENSITIVE_RE.test(name)
            ? '[REDACTED]'
            : (raw.length > MAX_FIELD_VALUE_LEN ? raw.slice(0, MAX_FIELD_VALUE_LEN) + '…' : raw);
        }
      } catch (e) { /* ignore */ }
      record(action, target, ctx);
    } catch (e) {
      // never throw
    }
  }

  function onDocSubmit(ev) {
    try {
      var form = ev.target;
      if (!form || form.nodeType !== 1) return;
      // Forms log only when explicitly tagged. We do NOT auto-log every submit.
      if (!form.dataset || !form.dataset.auditAction) return;
      var action = form.dataset.auditAction;
      var target = form.dataset.auditTarget || null;
      var ctx = form.dataset.auditContext ? safeJsonParse(form.dataset.auditContext) : {};
      if (!ctx || typeof ctx !== 'object') ctx = {};
      // Attach redacted form payload
      ctx.form = formToObject(form);
      try {
        var page = (typeof location !== 'undefined' && location.pathname) ? location.pathname : null;
        if (page && typeof ctx.page === 'undefined') ctx.page = page;
      } catch (e) { /* ignore */ }
      record(action, target, ctx);
    } catch (e) {
      // never throw
    }
  }

  // Attach early — DOMContentLoaded guarantees document.body exists.
  function attach() {
    try {
      document.addEventListener('click', onDocClick, true);
      document.addEventListener('submit', onDocSubmit, true);
    } catch (e) {
      // ignore
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attach);
  } else {
    attach();
  }

  // Public API for code that wants to emit programmatically (fetch
  // success/fail, keyboard shortcut, etc.)
  window.WBAudit = { record: record };
})();
