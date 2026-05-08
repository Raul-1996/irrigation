# Wave 2 — Observability Baseline: Implementation Design Document

**Status:** Ready for implementation (design-only, no code changes in this doc).
**Target branch base:** `main @ feb7956`
**Prerequisite waves:** Wave 1 (MASTER-C2 logging pipe, PHYS-1/2/3, SEC-003..015) — MERGED.
**Owner sign-off required:** §11 Open Questions.
**Reviewer:** Backend Architect (this doc) → Executor agent (follow-up).
**Doc scope:** Wave 2 of the Phase 5 roadmap — observability baseline only. Does **not** cover Wave 3+ (reconciler metrics, SSE removal, a11y, tests coverage push).

---

## 0. Doc conventions

- File paths are **absolute repo paths** rooted at `/opt/claude-agents/irrigation/`. Example: `services/logging_setup.py` means `/opt/claude-agents/irrigation/services/logging_setup.py`.
- Code blocks are **illustrative** — they are part of the specification that the executor must implement. They are **not** to be copy-pasted blindly: every snippet has a sibling "Acceptance" line specifying what the finished code must satisfy.
- Terminology:
  - `root logger` = `logging.getLogger()`.
  - `app logger` = `logging.getLogger('app')` (named, used by legacy callers).
  - `/healthz`, `/readyz`, `/metrics` — new endpoints; the legacy `/health` (from `routes/system_status_api.py`) stays as a back-compat alias and is NOT removed in Wave 2.
- Severity shorthand for deliverables:
  - **MUST** — required for Wave 2 exit criteria.
  - **SHOULD** — recommended; can be deferred with owner approval.
  - **MAY** — optional nice-to-have.

---

## Section 1 — Scope mapping (Wave 2 features → audit IDs)

Mapping derived from `irrigation-audit/reports/audit-report.md`. Line numbers are the anchor locations in that file as of commit `feb7956`.

| # | Wave 2 feature | Primary audit ID (section)                      | Secondary / cross-refs                                           | Audit-report lines |
|---|----------------|--------------------------------------------------|------------------------------------------------------------------|--------------------|
| 1 | Structured JSON logs + rotation  | **MASTER-C2** "Диагностический blackout" (§4)    | N/A (MASTER-C2 already partially done in Wave 1; JSON extension is the §255 "вторая очередь") | 228–259, 255 (target-state §6.2 extended formatter) |
| 2 | `/healthz`, `/readyz`, `/metrics`| **MASTER-M5** "Нет health/readiness/metrics"     | **N15** "Нет `/readyz`, `/metrics`"; target-state §6.3            | 759–780; 1012 (N15 row) |
| 3 | Correlation-ID middleware        | Extended **MASTER-C2** (target-state §6.2: `correlation_id`/`zone_id`/`command_id` fields) | MASTER-M5 (readable logs for /readyz failure reasons) | 255; 780 (dep on C2) |
| 4 | systemd `WatchdogSec=60` + `sd_notify`| **MASTER-M2** "Systemd unit без WatchdogSec"    | **N13** row; MASTER-M5 heartbeat source                           | 693–716; 1010 (N13 row) |
| 5 | logrotate config                 | **MASTER-C2** "`telegram.txt` 520 KB без ротации" | **HIGH-O1** (from findings/sre.md) — `telegram.txt` rotation item; Волна 1 row 1.2 already handled the immediate telegram.txt concern | 243 (`telegram.txt` 520 KB), 254 ("logrotate for telegram.txt") |

### Roadmap alignment (Phase-5 plan, audit lines 1070–1088, Волна 3)

The audit file labels this work **Волна 3**, not Волна 2. Internally we renumbered to Wave 2 **because** Wave 1 was cut smaller than the audit's Волна 1 (MASTER-C2 done, PHYS-2 done, MASTER-H2 done; but не все P0). For the executor this is a pure naming change — functional scope is identical to audit rows **3.1, 3.2, 3.3, 3.4**.

| Audit row | Short name                        | Wave 2 feature # |
|-----------|-----------------------------------|-------------------|
| 3.1       | `prometheus-client` + `/metrics`  | F2                |
| 3.2       | `/healthz` + `/readyz` + checks   | F2                |
| 3.3       | Correlation-ID + structured JSON  | F1 + F3           |
| 3.4       | systemd `WatchdogSec` + sd_notify | F4                |
| (derived) | logrotate for mosquitto + app.log | F5                |

### Out-of-scope for Wave 2 (explicit)

- MASTER-C1 reconciler metrics (`wb_observed_ack_latency_ms`, `wb_zones_active` from state machine) — Wave 3, depends on state machine being merged.
- Telegraf scrape config on `10.2.5.244` (audit row 3.6) — Wave 3, blocked by owner decision §9.15.
- SSE removal (audit rows 3.7–3.8) — Wave 3.
- Frontend hygiene (3.9–3.12) — Wave 3+.
- `User=wb-irrigation` non-root migration (part of MASTER-M2) — Wave 2 touches the unit file; **non-root migration is deferred** to Wave 3 because it also requires chown-ing `/opt/wb-irrigation` and `/mnt/data/irrigation-logs` on the production device — owner decision needed on directory ownership and whether to keep `mosquitto.service` dependency working across user boundaries. See §11 Open Question Q4.

---

## Section 2 — Feature 1: Structured JSON logs

### 2.1 Current state (read of `services/logging_setup.py`)

The file already has a `JSONFormatter` class (lines 44–76) written by-hand and wired in via the `WB_LOG_FORMAT` env flag (default `'json'`, lines 83–85). `setup_logging()` (lines 119–244) attaches a `TimedRotatingFileHandler` on the **root** logger with `JSONFormatter` and a `PIIFilter`.

**What is already in place (Wave 1 delivered):**
- Root-logger file handler with JSON formatting, `when='midnight'`, `backupCount=7` (lines 198–202).
- PII redaction filter attached to the handler (line 201).
- Idempotence guard so re-calling `setup_logging()` does not double-add handlers (lines 193–196).
- Console handler also switches to JSON when `WB_LOG_FORMAT=json` (lines 170–173).
- `import_export.log` sibling handler with the same policy (lines 204–212).

**Gaps vs. Wave 2 target (the delta executor must close):**
1. The hand-rolled `JSONFormatter.format()` only emits **7 fixed fields** (timestamp, level, module, message) + a fixed whitelist of extras (`zone_id, group_id, program_id, action, topic, duration, source, error`, line 61). **It drops arbitrary `extra=` kwargs that callers may pass** — notably `correlation_id`, `request_id`, `command_id`. The Wave 2 contract needs these.
2. Timestamp precision is seconds (`'%Y-%m-%dT%H:%M:%S'`, line 55). No milliseconds, no timezone suffix. Grafana / `jq` pipelines prefer RFC 3339 with ms: `2026-04-20T14:22:11.482+03:00`.
3. No `funcName` / `lineno` in the payload — grepping a field like `"funcName":"start_zone"` is valuable for post-incident.
4. No schema version field — future structural changes cannot be rolled out without breaking consumers. Need `"v": 1` at minimum.

### 2.2 Package choice & version pin

**Decision: use `python-json-logger>=2.0,<3.0`.** Reasons:
- Battle-tested (MIT license, 1800+ GH stars, CPython 3.8–3.12 supported).
- Drop-in `jsonlogger.JsonFormatter` subclasses `logging.Formatter`, so `TimedRotatingFileHandler.setFormatter()` still works unchanged.
- `add_fields()` hook gives a clean override point for static fields (service, version) and for flattening `extra` dicts.
- Size: ~30 KB wheel — negligible for an eMMC-constrained Wirenboard deployment.

**Alternative considered:** keep the hand-rolled `JSONFormatter`. **Rejected** because we'd have to reimplement the `extra`-flattening logic and risk accidental PII exfiltration through unfiltered fields. `python-json-logger` supports a `reserved_attrs` set that excludes standard `LogRecord` internals (e.g. `args`, `msg`, `stack_info`) while letting user-provided `extra=` pass through.

**Alternative considered:** `structlog`. **Rejected** for Wave 2 — it requires every call-site to migrate from `logger.info("msg %s", arg)` to `log.info("msg", key=value)`. Too invasive for a P2 wave.

### 2.3 Target output schema (per log line)

Every log line MUST be a single-line JSON object parseable by `json.loads`. Field order is not guaranteed, but the following keys MUST appear when applicable:

| Field             | Always present | Type      | Source                                                | Example                                   |
|-------------------|-----------------|-----------|-------------------------------------------------------|-------------------------------------------|
| `timestamp`       | yes             | string    | `record.created` → RFC 3339 + ms + TZ offset          | `"2026-04-20T14:22:11.482+03:00"`         |
| `level`           | yes             | string    | `record.levelname`                                    | `"INFO"`                                  |
| `logger`          | yes             | string    | `record.name`                                         | `"services.zone_control"`                 |
| `message`         | yes             | string    | `record.getMessage()` (with %-args resolved)          | `"Zone 5 started by user alice"`          |
| `module`          | yes             | string    | `record.module`                                       | `"zone_control"`                          |
| `funcName`        | yes             | string    | `record.funcName`                                     | `"start_zone"`                            |
| `lineno`          | yes             | int       | `record.lineno`                                       | `123`                                     |
| `v`               | yes             | int       | constant `1` — schema version                          | `1`                                       |
| `service`         | yes             | string    | constant `"wb-irrigation"`                            | `"wb-irrigation"`                         |
| `app_version`     | yes             | string    | from `VERSION` file, cached                           | `"2.0.0"`                                 |
| `correlation_id`  | when set        | string    | `contextvars.ContextVar` (Feature 3); `None` → omit    | `"f3a8-1c92-..."`                         |
| `request_id`      | when set        | string    | alias of `correlation_id`; emitted for compat          | `"f3a8-1c92-..."`                         |
| `zone_id`         | when set        | int       | `extra={"zone_id": 5}` at call site                    | `5`                                       |
| `group_id`        | when set        | int       | `extra={"group_id": 2}`                                | `2`                                       |
| `program_id`      | when set        | int       | `extra={"program_id": 17}`                             | `17`                                      |
| `command_id`      | when set        | string    | `extra={"command_id": "..."}` — ULID from reconciler   | `"01HTV..."` (future Wave 3)              |
| `action`, `topic`, `duration`, `source`, `error` | when set | varies | carried through `extra=`; flattened                    | `"topic": "wb-irrigation/zone/5/cmd"`     |
| `exception`       | on exc_info     | string    | `Formatter.formatException(record.exc_info)`          | Multi-line traceback (escaped as JSON string) |

**Acceptance (Feature 1 — schema):**
- `jq -c '.'` over 24 h of `backups/app.log` never errors.
- Every line contains `timestamp`, `level`, `logger`, `message`, `v`, `service`.
- At least one line per request contains `correlation_id` (assuming Feature 3 is also deployed).

### 2.4 Target implementation shape (illustrative)

```python
# services/logging_setup.py — changes described; NOT to be copy-pasted verbatim.

from pythonjsonlogger import jsonlogger  # new dependency

_APP_VERSION_CACHED: str | None = None

def _get_app_version() -> str:
    global _APP_VERSION_CACHED
    if _APP_VERSION_CACHED is None:
        try:
            from pathlib import Path
            _APP_VERSION_CACHED = Path(__file__).resolve().parent.parent.joinpath('VERSION').read_text().strip()
        except OSError:
            _APP_VERSION_CACHED = 'unknown'
    return _APP_VERSION_CACHED


class WBJsonFormatter(jsonlogger.JsonFormatter):
    """Project formatter: RFC3339-ms timestamp, static fields, correlation_id lookup."""

    RESERVED = {
        # pythonjsonlogger defaults + ours
        'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename',
        'levelno', 'msecs', 'msg', 'pathname', 'process', 'processName',
        'relativeCreated', 'stack_info', 'thread', 'threadName',
    }

    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)

        # RFC 3339 with ms + TZ offset; respect `TZ` env var already set in setup_logging().
        from datetime import datetime
        ts = datetime.fromtimestamp(record.created).astimezone()
        log_record['timestamp'] = ts.isoformat(timespec='milliseconds')

        log_record['level'] = record.levelname
        log_record['logger'] = record.name
        log_record['module'] = record.module
        log_record['funcName'] = record.funcName
        log_record['lineno'] = record.lineno
        log_record['v'] = 1
        log_record['service'] = 'wb-irrigation'
        log_record['app_version'] = _get_app_version()

        # Correlation ID from contextvars (Feature 3 installs the ContextVar).
        try:
            from services.correlation import get_correlation_id  # Feature 3 module
            cid = get_correlation_id()
            if cid:
                log_record['correlation_id'] = cid
                log_record['request_id'] = cid
        except ImportError:
            pass  # Feature 3 not yet merged; graceful degradation.

        # Drop internal noise
        for k in list(log_record.keys()):
            if k in self.RESERVED:
                log_record.pop(k, None)
```

**Wiring in `setup_logging()` — changes:**
- Replace every `fh.setFormatter(JSONFormatter())` with `fh.setFormatter(WBJsonFormatter())`.
- Keep the existing `PIIFilter` attached to every handler.
- Keep `TimedRotatingFileHandler(..., when='midnight', backupCount=7, encoding='utf-8')` — rotation unchanged.
- Remove the hand-rolled `JSONFormatter` class **only after** all tests pass with the new formatter. A two-commit migration is safest:
  1. Commit A: add `WBJsonFormatter`, switch file handler, keep old class.
  2. Commit B: drop old `JSONFormatter` class once CI green.

**Acceptance (Feature 1 — code):**
- `python-json-logger` in `requirements.txt` with exact pin `>=2.0,<3.0` (§7).
- `services/logging_setup.py` uses `WBJsonFormatter` for both the file handler and the console handler when `WB_LOG_FORMAT=json`.
- Console handler when `WB_LOG_FORMAT=plain` continues to produce the current `'%(asctime)s [%(levelname)s] [%(name)s] %(message)s'` format — **see 2.5 backward compat**.
- No call site needs to change (existing `logger.info("foo %s", bar, extra={"zone_id": 5})` Just Works).

### 2.5 Backward compatibility — console vs file

**Recommendation:** **file handler = JSON always; console handler = JSON by default but honour `WB_LOG_FORMAT=plain` for dev comfort.**

Rationale:
- Production (`wb-irrigation.service`) runs non-interactively — its stdout/stderr go to `journald`. `journalctl -u wb-irrigation -o json` already wraps stdout, so a nested JSON payload is useful (`jq '.MESSAGE | fromjson'` works).
- Dev (`python run.py` from a terminal) benefits from the plain-text format for readability; developers can set `WB_LOG_FORMAT=plain` in their shell.
- Test-suite already expects a `PYTEST_CURRENT_TEST`-aware branch (current lines 140–159) — no new fragility introduced.

**Env contract (existing, unchanged):**

| `WB_LOG_FORMAT` value | File handler | Console handler |
|-----------------------|--------------|-----------------|
| `json` (default)      | JSON         | JSON            |
| `plain`               | JSON *(still JSON — we never want plain in rotated files)* | plain `%(asctime)s [%(levelname)s] [%(name)s] %(message)s` |
| anything else         | treated as `json` (current line 85 behaviour) | treated as `json` |

### 2.6 Rotation policy — keep as-is

- `TimedRotatingFileHandler(when='midnight', interval=1, backupCount=7, encoding='utf-8', utc=False)` → preserved.
- Rationale: the existing policy rolls files nightly with 7-day retention. logrotate (Feature 5) is NOT used for `app.log`. See §6 below for why.

### 2.7 Tests (Feature 1)

Target file: `tests/unit/test_logging_setup_json.py` (new; the existing `tests/unit/test_logging_setup.py` only has 2 smoke tests and should not be overloaded).

Required cases — one `test_` function per assertion:

1. `test_json_formatter_required_fields`: Build a `LogRecord` manually, format it with `WBJsonFormatter`, `json.loads` the output, assert all 8 always-present keys (`timestamp`, `level`, `logger`, `message`, `module`, `funcName`, `lineno`, `v`, `service`).
2. `test_json_formatter_timestamp_rfc3339_ms`: Assert regex `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}$`.
3. `test_json_formatter_includes_extra_dict`: Call `logger.info("msg", extra={"zone_id": 5, "command_id": "01H..."})`, capture via `caplog` / manual handler, assert fields present at top level (not nested).
4. `test_json_formatter_drops_reserved`: Assert `args`, `msg`, `pathname` are NOT in output.
5. `test_json_formatter_exception`: Trigger `try: 1/0 except: logger.exception("boom")`; assert `exception` string field present.
6. `test_json_formatter_correlation_id_from_contextvar`: Set `correlation_id_var.set("abc")` (Feature 3), log, assert `correlation_id` and `request_id` both `== "abc"`.
7. `test_json_formatter_correlation_id_omitted_when_unset`: With no contextvar value, assert `correlation_id` key absent (not `None`).
8. `test_pii_filter_still_active`: Call `logger.info("password=secret123")`, assert output contains `[REDACTED]` and NOT `secret123`.
9. `test_timed_rotating_handler_still_attached`: After `setup_logging()`, `isinstance(h, TimedRotatingFileHandler) for h in root.handlers` has exactly 1 match for `app.log`.

**Mock strategy:**
- Use `freezegun.freeze_time('2026-04-20T14:22:11.482+03:00')` for timestamp assertion (add to `requirements-dev.txt` if not already there — check `requirements-dev.txt` before adding).
- For correlation_id: `from services.correlation import correlation_id_var; token = correlation_id_var.set("abc"); try: ...; finally: correlation_id_var.reset(token)`.
- For file-handler test: use `tmp_path` fixture to avoid polluting `backups/`.

---

## Section 3 — Feature 2: Health endpoints

### 3.1 Blueprint placement: new file `routes/health_api.py`

**Recommendation: create new blueprint `routes/health_api.py`**, NOT add to `routes/system_status_api.py`.

Rationale:
- `routes/system_status_api.py` is already 606 LOC and owns `/api/status`, `/api/health-details`, `/api/scheduler/*`, `/api/server-time`, `/api/logs`, `/api/water`, and the legacy `/health` alias. Adding three more routes + a Prometheus registry worsens the "god-module" smell (MASTER-L1).
- `/healthz`, `/readyz`, `/metrics` are **operational** endpoints — semantically unrelated to application status (zone/group watering). Separating them mirrors the k8s/SRE convention.
- The new blueprint is tiny (~150 LOC) and easy to unit-test in isolation.
- The existing `/health` route at `routes/system_status_api.py:202-228` stays as an unauthenticated back-compat alias — **no edits to that function** in Wave 2. It already returns `{ok, db, scheduler, mqtt_configured}` with 200/503 status codes.

**File structure (new):**

```
routes/health_api.py          ~150 LOC
  - Blueprint health_api_bp
  - /healthz                  (5 LOC liveness)
  - /readyz                   (readiness, checks registry)
  - /metrics                  (Prometheus text exposition)
  - module-level metric definitions (Counter/Gauge/Histogram)
  - module-level _readiness_checks list
```

The blueprint must be registered in `app.py` **and** marked CSRF-exempt **and** marked session-auth-exempt.

### 3.2 `/healthz` — liveness

**Contract:**
- Method: `GET` only.
- No session auth required.
- No CSRF (it's GET; CSRF already only applies to mutating methods, but be explicit).
- Returns `200 OK` with body `{"status": "ok"}` if the Flask process is alive enough to respond.
- **Does not touch DB, MQTT, or scheduler.** Its failure mode is purely "Flask event loop wedged / OOM / deadlocked" — in which case the response never returns at all and systemd watchdog (Feature 4) kills the process.

**Illustrative code:**

```python
@health_api_bp.route('/healthz', methods=['GET'])
def healthz():
    return {"status": "ok"}, 200
```

**Auth exemption:** `/healthz` must be excluded from the auth `before_request` hook in `app.py:279-303`. The current hook only intercepts `request.path.startswith('/api/')` — `/healthz` does NOT start with `/api/`, so it is already bypassed. **No change required in `app.py` auth logic.**

**Acceptance:**
- `curl -fs http://localhost:8080/healthz` returns 200 within 50 ms.
- Under session cookie absent: still 200.
- Under `TESTING=1`: still 200.

### 3.3 `/readyz` — readiness with checks table

**Contract:**
- Method: `GET` only.
- No auth, no CSRF (same rationale as `/healthz`).
- Aggregates multiple checks; returns `200` if all pass, `503` if any fails.
- Each check has a hard timeout (see table); timeout → check fails.
- Response body is structured JSON listing each check's status.

**Readiness checks table:**

| # | Check              | Pass condition                                                         | Fail → status                  | Implementation module / function                                    | Timeout  |
|---|--------------------|------------------------------------------------------------------------|---------------------------------|---------------------------------------------------------------------|----------|
| 1 | `db`               | `SELECT 1` succeeds against `irrigation.db`                            | 503, `db: "fail"`               | `database.db._connect()` + `cursor.execute("SELECT 1")`             | 2 s      |
| 2 | `scheduler`        | `get_scheduler()` returns non-None **and** `scheduler.running is True` | 503, `scheduler: "fail"`         | `irrigation_scheduler.get_scheduler()`                              | 100 ms   |
| 3 | `mqtt`             | At least one broker in `db.get_mqtt_servers()` has a cached client in `services.mqtt_pub._MQTT_CLIENTS` with `.is_connected() == True`. **If no servers configured at all → SKIP (report as `"skipped"`)** — don't penalise a fresh install. | 503, `mqtt: "fail"`              | `services.mqtt_pub._MQTT_CLIENTS` + `is_connected()`                | 500 ms   |
| 4 | `boot_reconcile`   | Flag `services.app_init._boot_sync_done` is True (executor to add this flag in Wave 2; defaults to False, set True at end of `_boot_sync()`) | 503, `boot_reconcile: "fail"`   | `services.app_init`                                                 | n/a (bool read) |
| 5 | `disk_space`       | `os.statvfs('/opt/wb-irrigation')` reports ≥ 50 MB free                  | 503, `disk_space: "fail"`        | inline `os.statvfs`                                                 | n/a      |

**Check ordering:** cheap first → slow last. `boot_reconcile` (bool) → `disk_space` (stat) → `scheduler` (attr) → `mqtt` (paho call) → `db` (round-trip).

**Short-circuit behaviour:** **do NOT short-circuit**. Run every check; return all results. Reason: operator debugging — a 503 with only one check reported is less useful than seeing which of the five failed.

**Response shape (example — happy path):**

```json
{
  "status": "ok",
  "checks": {
    "db":              {"status": "ok", "duration_ms": 3},
    "scheduler":       {"status": "ok", "duration_ms": 0},
    "mqtt":            {"status": "ok", "duration_ms": 12, "brokers": 1},
    "boot_reconcile":  {"status": "ok"},
    "disk_space":      {"status": "ok", "free_mb": 28412}
  }
}
```

**Response shape (example — one failing):**

```json
{
  "status": "fail",
  "checks": {
    "db":              {"status": "ok", "duration_ms": 3},
    "scheduler":       {"status": "fail", "duration_ms": 0, "reason": "scheduler.running is False"},
    "mqtt":            {"status": "ok", "duration_ms": 12, "brokers": 1},
    "boot_reconcile":  {"status": "ok"},
    "disk_space":      {"status": "ok", "free_mb": 28412}
  }
}
```
HTTP status 503.

**Illustrative implementation shape:**

```python
import time, os, sqlite3

def _check_db():
    t0 = time.perf_counter()
    try:
        conn = sqlite3.connect('irrigation.db', timeout=2)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return {"status": "ok", "duration_ms": int((time.perf_counter() - t0) * 1000)}
    except Exception as e:
        return {"status": "fail", "duration_ms": int((time.perf_counter() - t0) * 1000),
                "reason": type(e).__name__}

def _check_scheduler():
    from irrigation_scheduler import get_scheduler
    sched = get_scheduler()
    if sched is None:
        return {"status": "fail", "reason": "scheduler not initialised"}
    running = bool(getattr(sched, 'running', False))
    return {"status": "ok" if running else "fail",
            "reason": None if running else "scheduler.running is False"}

# ... similarly for mqtt, boot_reconcile, disk_space ...

_CHECKS = [
    ("boot_reconcile", _check_boot_reconcile),
    ("disk_space",     _check_disk_space),
    ("scheduler",      _check_scheduler),
    ("mqtt",           _check_mqtt),
    ("db",             _check_db),
]

@health_api_bp.route('/readyz', methods=['GET'])
def readyz():
    results = {name: fn() for name, fn in _CHECKS}
    all_ok = all(r["status"] == "ok" or r["status"] == "skipped" for r in results.values())
    return {"status": "ok" if all_ok else "fail", "checks": results}, (200 if all_ok else 503)
```

**Acceptance:**
- `curl -fs http://localhost:8080/readyz` returns 200 with all-ok payload on a freshly booted healthy instance.
- When `sched.shutdown()` is called manually, next `/readyz` call returns 503 with `scheduler.status == "fail"`.
- When DB file is deleted (test), returns 503 with `db.status == "fail"`.
- Endpoint responds within 3 s even when DB is locked (2 s timeout + margin).

### 3.4 `/metrics` — Prometheus text exposition

**Library:** `prometheus-client>=0.20,<1.0` (see §7). Latest 0.21 is fine; pin max `<1.0` to shield against a hypothetical API break.

**Auth:** None (metrics are not secret but also not confidential). **Future hardening (Wave 3 per MASTER-M5 audit §776): IP allow-list via nginx** — out of Wave 2 scope. A TODO comment in the code is acceptable.

**Content type:** `from prometheus_client import CONTENT_TYPE_LATEST, generate_latest`, then `return generate_latest(registry), 200, {"Content-Type": CONTENT_TYPE_LATEST}`.

**Registry:** Use a **dedicated** `CollectorRegistry()` (not the default global one). Reason: default registry has process/GC collectors pre-registered, which pollute output and make testing painful. We **opt-in** to process/GC collectors explicitly.

#### 3.4.1 Required metrics (minimum 10, per Wave 2 exit criterion)

Column "Where incremented" is the code location the executor must add a hook to. If the source module does not yet exist (e.g. reconciler — Wave 3), the metric is still **declared** but its increment call is deferred. Minimum 10 metrics that can be populated in Wave 2 are asterisked (*).

| # | Metric name                               | Type      | Labels                                  | Unit    | Source / where incremented                                                                 | Populated in Wave 2? |
|---|-------------------------------------------|-----------|-----------------------------------------|---------|--------------------------------------------------------------------------------------------|----------------------|
|  1 | `wb_build_info`                          | Gauge (const 1)| `version`, `commit`, `python_version`   | —       | Set once at `init_metrics()`; reads `VERSION`, `os.getenv('GIT_COMMIT')`, `platform.python_version()` | * yes |
|  2 | `wb_http_requests_total`                 | Counter   | `method`, `endpoint`, `status_code`     | —       | Flask `after_request` middleware in `app.py`; label `endpoint = request.endpoint or "unknown"` | * yes |
|  3 | `wb_http_request_duration_seconds`       | Histogram | `method`, `endpoint`                    | seconds | Same `after_request`: `time.time() - request._started_at` (perf timer already exists at `app.py:198-212`) | * yes |
|  4 | `wb_http_requests_in_flight`             | Gauge     | —                                       | —       | `before_request` inc, `after_request` dec (atomic via Gauge.inc/dec is thread-safe)        | * yes |
|  5 | `wb_process_start_time_seconds`          | Gauge     | —                                       | seconds (unix) | Set once at `init_metrics()`: `int(time.time())`                                        | * yes |
|  6 | `wb_db_query_duration_seconds`           | Histogram | `operation` (`read`/`write`)             | seconds | Wrapper around `db._connect()` — pragma: keep out of hot path; Wave 2 scope = add only to `db.get_zones()` and `db.get_groups()` as baseline | * yes (partial) |
|  7 | `wb_mqtt_clients_connected`              | Gauge     | —                                       | —       | Set by a 5-second background thread that counts `services.mqtt_pub._MQTT_CLIENTS` where `is_connected()`; OR lazily on `/metrics` call (recommended — saves a thread). | * yes |
|  8 | `wb_mqtt_publish_total`                  | Counter   | `result` (`ok`/`fail`)                   | —       | `services.mqtt_pub.publish_mqtt_value()` success/exception paths                            | * yes |
|  9 | `wb_scheduler_jobs_total`                | Gauge     | —                                       | —       | Set by `/metrics` handler on scrape: `len(get_scheduler().get_jobs())`                     | * yes |
| 10 | `wb_scheduler_running`                   | Gauge     | —                                       | — (0/1) | Set on scrape: `int(bool(get_scheduler().running))`                                        | * yes |
| 11 | `wb_zones_total`                         | Gauge     | `state` (`on`/`off`)                     | —       | Set on scrape via `db.get_zones()` aggregation                                             | * yes |
| 12 | `wb_logging_records_total`               | Counter   | `level` (`INFO`/`WARNING`/`ERROR`/`CRITICAL`) | —   | A custom `logging.Handler` installed on the root logger in `init_metrics()` that just `.inc()` the counter | * yes |
| 13 | `wb_readyz_check_status`                 | Gauge     | `check` (`db`, `scheduler`, ...)        | — (0/1) | Updated at every `/readyz` call with the latest per-check result (1=ok, 0=fail)            | * yes |
| 14 | `wb_watchdog_heartbeats_total`           | Counter   | —                                       | —       | `services.systemd_notify.heartbeat_thread` increments on every `WATCHDOG=1` send           | yes (once Feature 4 lands) |
| 15 | `wb_zone_start_total` / `wb_zone_stop_total` | Counter | `source` (`manual`/`scheduler`/`program`) | —       | `services.zone_control.start_zone` / `stop_zone`                                           | * yes (small edit) |

**Exit criterion check:** **13 metrics populated in Wave 2** ≥ 10 required. Pass.

Optional/deferred (Wave 3+):
- `wb_observed_ack_latency_ms` (Histogram, labels `zone_id`) — needs reconciler (MASTER-C1).
- `wb_zone_fault_total` — needs state machine fault paths.
- `wb_scheduler_lag_seconds` — needs scheduler tick instrumentation.

#### 3.4.2 Metric registration pattern

All metrics live as module-level globals in `routes/health_api.py`. Use `prometheus_client.CollectorRegistry()`:

```python
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

_REGISTRY = CollectorRegistry()

WB_BUILD_INFO = Gauge(
    'wb_build_info', 'Build info', ['version', 'commit', 'python_version'], registry=_REGISTRY,
)
WB_HTTP_REQUESTS = Counter(
    'wb_http_requests_total', 'Total HTTP requests', ['method', 'endpoint', 'status_code'],
    registry=_REGISTRY,
)
WB_HTTP_DURATION = Histogram(
    'wb_http_request_duration_seconds', 'HTTP request duration', ['method', 'endpoint'],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=_REGISTRY,
)
# ... etc.

def init_metrics(app, db):
    """Populate one-shot gauges and install the log-count handler.
    Called from services/app_init.py at startup."""
    import platform
    WB_BUILD_INFO.labels(
        version=app.config.get('APP_VERSION', 'unknown'),
        commit=os.getenv('GIT_COMMIT', 'unknown'),
        python_version=platform.python_version(),
    ).set(1)
    WB_PROCESS_START_TIME.set(int(time.time()))
    # Install log counter handler
    import logging
    logging.getLogger().addHandler(_LogCountHandler())
```

Hooks in middleware (`app.py`):

```python
# before_request
WB_HTTP_IN_FLIGHT.inc()

# after_request
WB_HTTP_IN_FLIGHT.dec()
endpoint = request.endpoint or 'unknown'
WB_HTTP_REQUESTS.labels(request.method, endpoint, str(resp.status_code)).inc()
duration = time.time() - getattr(request, '_started_at', time.time())
WB_HTTP_DURATION.labels(request.method, endpoint).observe(duration)
```

**Cardinality concern:** Flask may produce `request.endpoint = None` for 404s. Label value `"unknown"` keeps cardinality bounded. Do **not** use `request.path` as a label — it's unbounded. The Flask blueprint + view-function name (`request.endpoint`) is cardinality-safe.

#### 3.4.3 `/metrics` endpoint

```python
@health_api_bp.route('/metrics', methods=['GET'])
def metrics():
    # Lazy gauges that reflect current state on each scrape:
    try:
        from irrigation_scheduler import get_scheduler
        sched = get_scheduler()
        WB_SCHEDULER_JOBS.set(len(sched.get_jobs()) if sched else 0)
        WB_SCHEDULER_RUNNING.set(1 if (sched and getattr(sched, 'running', False)) else 0)
    except Exception:
        pass
    try:
        zones = db.get_zones() or []
        on = sum(1 for z in zones if str(z.get('state')) == 'on')
        WB_ZONES_TOTAL.labels('on').set(on)
        WB_ZONES_TOTAL.labels('off').set(len(zones) - on)
    except Exception:
        pass
    # ... mqtt clients connected ...
    return generate_latest(_REGISTRY), 200, {'Content-Type': CONTENT_TYPE_LATEST}
```

**Auth exemption:** `/metrics` is GET-only, does not start with `/api/`, so current auth hook in `app.py:279-303` already ignores it. **No edits required in `app.py` auth logic.**

**CSRF exemption:** CSRF-exempt is moot — Flask-WTF only checks CSRF on `POST`/`PUT`/`DELETE`/`PATCH`. `/metrics` is GET-only. Still, for safety, apply `csrf.exempt(metrics)` at blueprint registration time — defence in depth.

**Acceptance (Feature 2):**
- `curl -fs http://localhost:8080/metrics` returns 200 with content-type `text/plain; version=0.0.4; charset=utf-8`.
- Response body contains **at least 10 distinct `HELP` lines** and the required metric names.
- `promtool check metrics < /metrics-body` returns success (if `promtool` available — optional CI step).
- All three endpoints survive `ab -c 10 -n 1000 /metrics` in <500 ms p95.

### 3.5 CSRF exempt / public-POST list

The app.py whitelists `_ALLOWED_PUBLIC_POSTS` and `_ALLOWED_PUBLIC_PATTERNS` (lines 256–268) for POST methods. The three new endpoints (`/healthz`, `/readyz`, `/metrics`) are **GET-only** and do **not** need entries in these structures. The auth `before_request` hook (`app.py:279-303`) also only intercepts `/api/*`, so GET `/healthz|readyz|metrics` paths bypass auth naturally.

**Explicit change required in `app.py`:**
- Register the new blueprint: `app.register_blueprint(health_api_bp)` in the loop at line 319.
- **Exempt the blueprint from CSRF as a whole** (defensive — future POST additions won't accidentally require tokens):
  ```python
  from routes.health_api import health_api_bp
  csrf.exempt(health_api_bp)
  app.register_blueprint(health_api_bp)
  ```
- `init_metrics(app, db)` must be called once — best place: `services/app_init.initialize_app()` after `_boot_sync`. Add one call to `init_metrics` at the end of `initialize_app()`.

---

## Section 4 — Feature 3: Correlation-ID middleware

### 4.1 Goal

Every HTTP request gets a unique ID. The ID is:
1. Read from the incoming `X-Request-ID` header if well-formed; else generated.
2. Bound to a `contextvars.ContextVar` so the logging formatter (Feature 1) picks it up automatically.
3. Echoed back as `X-Request-ID` response header (debugging).
4. Available to every logger call in that request's thread/context — no explicit plumbing.

### 4.2 Where it lives

**New file:** `services/correlation.py` (small, ~40 LOC). Contains:
- `correlation_id_var: ContextVar[Optional[str]] = ContextVar('wb_correlation_id', default=None)`
- `get_correlation_id() -> Optional[str]` — read.
- `set_correlation_id(value: str) -> Token` — set, returns token for reset.
- `validate_correlation_id(raw: str | None) -> str | None` — input sanitizer (see 4.4).
- `generate_correlation_id() -> str` — `str(uuid.uuid4())`.

**Hooks in `app.py`:**

```python
# Near existing before_request/after_request block (app.py:197-212)
from services.correlation import (
    correlation_id_var, validate_correlation_id, generate_correlation_id,
)

@app.before_request
def _assign_correlation_id():
    raw = request.headers.get('X-Request-ID')
    cid = validate_correlation_id(raw) or generate_correlation_id()
    token = correlation_id_var.set(cid)
    request._correlation_id_token = token  # for teardown reset
    request._correlation_id = cid

@app.after_request
def _propagate_correlation_id(resp):
    cid = getattr(request, '_correlation_id', None)
    if cid:
        resp.headers['X-Request-ID'] = cid
    return resp

@app.teardown_request
def _reset_correlation_id(exc):
    token = getattr(request, '_correlation_id_token', None)
    if token is not None:
        try:
            correlation_id_var.reset(token)
        except ValueError:
            pass  # context already torn down
```

**Hook ordering note:** Flask `before_request` hooks run in registration order. Place `_assign_correlation_id` **before** any other `before_request` in `app.py` so auth/rate-limit logs carry the ID. Currently `_perf_start_timer` (line 197) is first — keep it there; the correlation assignment should come **immediately after** it but **before** `_auth_before_request` (line 279) and `_require_admin_for_mutations` (line 325) and `_general_api_rate_limit` (line 435).

**Simpler alternative:** use `logging.LoggerAdapter`. **Rejected** because adapters must be instantiated per-call site — every `logger.info(...)` in the codebase would need to become `get_adapter().info(...)`. ContextVar + formatter read is transparent.

### 4.3 Header name & semantics

- Accepted header: `X-Request-ID` (case-insensitive per HTTP spec — Flask normalises).
- Also accept `X-Correlation-ID` as an alias (industry convention varies). Implementation: check X-Request-ID first, fall through to X-Correlation-ID.
- Response header: `X-Request-ID` always echoed (even if caller used X-Correlation-ID inbound) — single canonical name outbound for consistency.

### 4.4 Input validation

**Format rule:** `^[A-Za-z0-9\-_]{8,64}$`.

Rationale:
- Must be printable ASCII (logs are scanned by `jq`, grep; control chars break this).
- Must not include shell-special chars (`;`, `$`, backticks) to prevent log-injection into downstream consumers.
- 8–64 chars: long enough to be useful, short enough not to bloat logs.
- Reject everything else; generate a UUIDv4 instead.

```python
import re
_CID_RE = re.compile(r'^[A-Za-z0-9\-_]{8,64}$')

def validate_correlation_id(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if _CID_RE.match(raw):
        return raw
    return None

def generate_correlation_id() -> str:
    import uuid
    return str(uuid.uuid4())  # length 36, safe charset
```

**Sanity check for malicious input:**
- `X-Request-ID: '; DROP TABLE zones; --` → `validate_correlation_id` returns None (contains spaces, `'`, `;`) → a fresh UUID is generated → no injection risk.
- `X-Request-ID: <script>alert(1)</script>` → rejected (`<`, `>`, `(`, `)`) → fresh UUID.
- `X-Request-ID: 0000` → rejected (length 4 < 8) → fresh UUID.
- `X-Request-ID: valid-trace-id-123` → accepted verbatim.

### 4.5 Tests (Feature 3)

Target file: `tests/unit/test_correlation.py` (new).

| # | Test name                                   | Given                                                 | Expect                                                                                  |
|---|---------------------------------------------|-------------------------------------------------------|-----------------------------------------------------------------------------------------|
| 1 | `test_header_present_flows_into_log`        | `GET /api/server-time` with `X-Request-ID: abc123-def456` | `caplog` captures a record whose JSON has `correlation_id == "abc123-def456"`           |
| 2 | `test_header_missing_generates_uuid`        | `GET /api/server-time` with no X-Request-ID           | Response header `X-Request-ID` is a valid UUIDv4 string                                 |
| 3 | `test_header_malicious_sanitised`           | `X-Request-ID: ; DROP TABLE zones`                    | Response `X-Request-ID` is a fresh UUIDv4, NOT the malicious value                      |
| 4 | `test_header_too_short_regenerated`         | `X-Request-ID: ab`                                    | Response `X-Request-ID` is UUID (length 36), not "ab"                                   |
| 5 | `test_header_too_long_regenerated`          | `X-Request-ID: <70 chars>`                            | Response `X-Request-ID` is UUID                                                          |
| 6 | `test_correlation_id_propagates_to_logs`    | GET any endpoint → grep handler output                | Log line JSON contains `correlation_id`                                                 |
| 7 | `test_correlation_id_alias_header`          | `X-Correlation-ID: valid-id-12345`                    | Accepted; echoed back as `X-Request-ID`                                                 |
| 8 | `test_correlation_id_resets_between_requests` | two sequential requests                              | Log records from request 2 do NOT have request 1's correlation_id                       |
| 9 | `test_contextvar_isolated_across_threads`   | two threads log concurrently                          | Each thread's correlation_id stays distinct (ContextVar guarantees — regression test)   |

**Mock strategy:**
- Use Flask's built-in `app.test_client()` — sends requests in the same process, exercises all `before_request` hooks.
- For log-capture: custom `logging.Handler` subclass collecting `self.records = []`; attach to root in fixture, detach in teardown.

---

## Section 5 — Feature 4: systemd watchdog (`WatchdogSec=60`)

### 5.1 Current `wb-irrigation.service` (read of file)

Current content (18 lines, verbatim):

```ini
[Unit]
Description=WB-Irrigation Flask app
After=network-online.target mosquitto.service
Wants=network-online.target
Requires=mosquitto.service

[Service]
Type=simple
WorkingDirectory=/opt/wb-irrigation/irrigation
Environment=TESTING=0
Environment=UI_THEME=auto
ExecStart=/opt/wb-irrigation/irrigation/venv/bin/python run.py
TimeoutStopSec=20
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 5.2 Target `wb-irrigation.service`

```ini
[Unit]
Description=WB-Irrigation Flask app
After=network-online.target mosquitto.service
Wants=network-online.target
Requires=mosquitto.service

[Service]
Type=notify
NotifyAccess=main
WorkingDirectory=/opt/wb-irrigation/irrigation
Environment=TESTING=0
Environment=UI_THEME=auto
Environment=WB_WATCHDOG_ENABLED=1
ExecStart=/opt/wb-irrigation/irrigation/venv/bin/python run.py
TimeoutStartSec=60
TimeoutStopSec=45
WatchdogSec=60
Restart=always
RestartSec=5
StartLimitBurst=5
StartLimitIntervalSec=300

[Install]
WantedBy=multi-user.target
```

**Changes vs. current (itemised):**
- `Type=simple` → `Type=notify` — systemd waits for `READY=1` signal before marking service active; removes the "Flask is up but scheduler hasn't booted" gap.
- `NotifyAccess=main` — only the main PID can send to `$NOTIFY_SOCKET` (security hardening; prevents helper threads/child processes from spoofing readiness).
- `Environment=WB_WATCHDOG_ENABLED=1` — kill-switch via env so we can disable the heartbeat thread in dev without editing code.
- `TimeoutStartSec=60` — boot_sync on cold WB eMMC takes 10–20 s; 60 s gives headroom.
- `TimeoutStopSec=20` → `45` — matches audit MASTER-M2 recommendation for graceful MQTT-publish drain on shutdown.
- `WatchdogSec=60` — if app does not send `WATCHDOG=1` for 60 s, systemd kills + restarts per `Restart=always`.
- `Restart=on-failure` → `always` — restart on clean exit too (defensive; per MASTER-M2).
- `StartLimitBurst=5` / `StartLimitIntervalSec=300` — if app crashes 5 times in 5 min, enter failure state; prevents infinite crash loop.
- **Non-root (`User=wb-irrigation`) deferred to Wave 3** (see §11 Q4).

### 5.3 Python-side notifier: manual NOTIFY_SOCKET vs `systemd-python`

**Recommendation: manual NOTIFY_SOCKET datagram writes (no new dependency).**

Trade-off table:

| Aspect               | `systemd-python` (v234+)                          | Manual NOTIFY_SOCKET                       |
|----------------------|---------------------------------------------------|--------------------------------------------|
| Install dependency   | `libsystemd-dev` (C headers) + compilation on WB  | none                                       |
| Python deps added    | `systemd-python>=234`                              | none                                       |
| Portability          | Linux only (fine — prod is Linux)                 | Linux only                                 |
| Surface area         | `daemon.notify("READY=1")`, `daemon.notify("WATCHDOG=1")` | ~30 LOC socket code                  |
| Dev ergonomics       | `pip install systemd-python` fails on macOS without libsystemd — dev friction | works on all platforms as a no-op when `$NOTIFY_SOCKET` env var is unset |
| Failure mode when not under systemd | import error at module load time | silent no-op (env var not set)             |

**Decision: manual.** Dev friction of compiling `libsystemd-dev` on Wirenboard ARM + macOS dev laptops outweighs the ~30 LOC we save.

### 5.4 Illustrative notifier implementation

**New file:** `services/systemd_notify.py` (~60 LOC).

```python
"""systemd sd_notify bridge (NOTIFY_SOCKET) — no C dependencies.

Protocol: datagram to $NOTIFY_SOCKET with ASCII payload.
  READY=1            — boot complete; systemd marks service active (Type=notify).
  STATUS=...         — human-readable status.
  WATCHDOG=1         — heartbeat (WatchdogSec required in unit).
  STOPPING=1         — graceful shutdown starting.

When $NOTIFY_SOCKET is not set (dev / not under systemd), every call is a no-op.
"""
import logging
import os
import socket
import threading
import time

logger = logging.getLogger(__name__)

_HEARTBEAT_THREAD: threading.Thread | None = None
_HEARTBEAT_STOP = threading.Event()
_WATCHDOG_INTERVAL_SEC = 20  # WatchdogSec=60 in unit → send every 20s = 3x safety margin.


def _notify(message: str) -> bool:
    addr = os.environ.get('NOTIFY_SOCKET')
    if not addr:
        return False
    # Abstract socket convention: a leading '@' means abstract namespace → replace with NUL byte.
    if addr.startswith('@'):
        addr = '\0' + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(message.encode('utf-8'), addr)
        return True
    except (OSError, socket.error) as e:
        logger.warning("sd_notify send failed: %s", e)
        return False


def notify_ready(status: str = "Application ready") -> bool:
    ok = _notify(f"READY=1\nSTATUS={status}\n")
    if ok:
        logger.info("sd_notify READY=1 sent", extra={"status": status})
    return ok


def notify_watchdog() -> bool:
    return _notify("WATCHDOG=1\n")


def notify_stopping() -> bool:
    return _notify("STOPPING=1\nSTATUS=Shutting down\n")


def _heartbeat_loop():
    logger.info("sd_notify heartbeat thread started (interval=%ds)", _WATCHDOG_INTERVAL_SEC)
    while not _HEARTBEAT_STOP.is_set():
        try:
            notify_watchdog()
            # Wave 3 hook: also increment wb_watchdog_heartbeats_total here.
        except Exception as e:
            logger.warning("sd_notify heartbeat error: %s", e)
        _HEARTBEAT_STOP.wait(_WATCHDOG_INTERVAL_SEC)
    logger.info("sd_notify heartbeat thread stopped")


def start_heartbeat() -> None:
    global _HEARTBEAT_THREAD
    if os.environ.get('WB_WATCHDOG_ENABLED', '1') != '1':
        logger.info("sd_notify heartbeat disabled via WB_WATCHDOG_ENABLED=0")
        return
    if _HEARTBEAT_THREAD is not None and _HEARTBEAT_THREAD.is_alive():
        return
    _HEARTBEAT_STOP.clear()
    _HEARTBEAT_THREAD = threading.Thread(
        target=_heartbeat_loop, name="sd-notify-heartbeat", daemon=True,
    )
    _HEARTBEAT_THREAD.start()


def stop_heartbeat(timeout: float = 5.0) -> None:
    _HEARTBEAT_STOP.set()
    if _HEARTBEAT_THREAD is not None:
        _HEARTBEAT_THREAD.join(timeout=timeout)
```

### 5.5 Thread lifecycle wiring

- **`services/app_init.initialize_app()`** — after `_boot_sync()` completes successfully and monitors started:
  ```python
  from services.systemd_notify import notify_ready, start_heartbeat
  start_heartbeat()       # must start BEFORE notify_ready to avoid a gap
  notify_ready(status=f"boot_sync done, {zone_count} zones loaded")
  ```
  Use a new boolean `_boot_sync_done = True` flag on the module (this also feeds /readyz check #4).

- **Shutdown path** — extend `services/app_init._register_shutdown_handlers()` (at `services/app_init.py:289-318`):
  ```python
  def _signal_handler(signum, frame):
      from services.systemd_notify import notify_stopping, stop_heartbeat
      notify_stopping()
      stop_heartbeat(timeout=2.0)
      shutdown_all_zones_off(db=db)
      # ...rest as today...
  ```
  and ensure the atexit hook also calls `stop_heartbeat(timeout=1.0)` before `shutdown_all_zones_off`.

### 5.6 Heartbeat condition

**Wave 2 policy:** heartbeat fires unconditionally every 20 s as long as the thread is alive. This covers "Python process wedged in GIL-deadlock" (thread stalls → no heartbeat → systemd kills us). It does **not** cover "scheduler thread is dead but Flask event loop is fine" — that liveness check needs the reconciler (Wave 3).

**Wave 3 upgrade path (documented for future):** replace the unconditional heartbeat with a check:
```python
sched = get_scheduler()
if sched and sched.running and reconciler_last_tick_age < 60:
    notify_watchdog()
```
→ then if scheduler/reconciler dies, no WATCHDOG=1, systemd restarts us. Out of Wave 2 scope.

### 5.7 Tests (Feature 4)

Target: `tests/unit/test_systemd_notify.py` (new).

| # | Test                                           | Mock                                                           | Assert                                                     |
|---|------------------------------------------------|----------------------------------------------------------------|------------------------------------------------------------|
| 1 | `test_notify_noop_without_env`                 | `monkeypatch.delenv('NOTIFY_SOCKET')`                          | `_notify("READY=1")` returns `False`, no exception         |
| 2 | `test_notify_abstract_socket_prefix`           | `NOTIFY_SOCKET=@fake`                                          | `socket.sendto` called with address starting `\0fake`      |
| 3 | `test_notify_ready_sends_proper_payload`       | fake Unix datagram server on `tmp_path/sock`                   | Received bytes `== b"READY=1\nSTATUS=Application ready\n"` |
| 4 | `test_heartbeat_thread_starts_and_stops`       | stub `_notify` to count calls; `_WATCHDOG_INTERVAL_SEC` patched to 0.05 | After 0.2 s, call count ≥ 3; after `stop_heartbeat()`, thread dies in ≤ 1 s |
| 5 | `test_heartbeat_disabled_via_env`              | `WB_WATCHDOG_ENABLED=0`                                        | `start_heartbeat()` does nothing, thread handle `None`     |
| 6 | `test_notify_survives_socket_error`            | monkeypatch `socket.socket` to raise `OSError`                 | No exception bubbled; returns `False`                      |

**End-to-end integration test (deferred to Wave 2.1):** run under actual systemd + `systemd-run --property=Type=notify --property=WatchdogSec=3 ...`; out of CI scope (needs privileged runner). Mark as manual QA step in §10 deploy notes.

---

## Section 6 — Feature 5: logrotate config

### 6.1 Scope decision — what actually needs logrotate

Audit (lines 243–254) called out three log files:

| File                                  | Current rotation                                           | Needs logrotate? |
|---------------------------------------|------------------------------------------------------------|-------------------|
| `backups/app.log`                     | Python `TimedRotatingFileHandler` midnight × 7 (Wave 1)     | **No** — Python handler rotates in-process. A second rotator would fight. |
| `backups/import-export.log`           | Same `TimedRotatingFileHandler` policy (Wave 1)              | **No** — same reason |
| `services/logs/telegram.txt`          | **None.** 520 KB and growing.                                | **Yes** — not written via stdlib logging; rotation must be external |
| `/var/log/mosquitto/mosquitto.log`    | Mosquitto's own `log_dest file` + no rotation by default     | **Yes** — external broker, grows unbounded |
| `backups/*.log.gz` (Wave 1 artefacts) | produced by Python handler                                   | No                 |

**Recommendation: logrotate covers `telegram.txt` + `mosquitto.log`. Leave `app.log` / `import-export.log` to the Python handler.**

### 6.2 File path & content

**New file:** `configs/logrotate.d/wb-irrigation`

Content (complete, ready to drop into `/etc/logrotate.d/wb-irrigation`):

```logrotate
# /etc/logrotate.d/wb-irrigation
# Managed by wb-irrigation repo at configs/logrotate.d/wb-irrigation
# Applies to Mosquitto log and Telegram bot log (telegram.txt).
# The main app.log is rotated by Python's TimedRotatingFileHandler and
# MUST NOT be listed here — two rotators on the same file cause races.

/var/log/mosquitto/mosquitto.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    dateext
    dateformat -%Y%m%d
}

/opt/wb-irrigation/irrigation/services/logs/telegram.txt {
    size 1M
    rotate 4
    weekly
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    dateext
    dateformat -%Y%m%d
}
```

**Directive rationale:**
- `daily` / `weekly`+`size 1M` — mosquitto is chatty (MQTT keepalive, connect/disconnect), telegram is low-volume so size-triggered is better.
- `rotate 14` / `rotate 4` — 2 weeks for MQTT (investigations), 4 weeks for telegram (low churn).
- `compress` + `delaycompress` — compress old rotations, except the most recent (so tail-f on `.log.1` still works).
- `missingok` — don't fail cron if file absent (fresh install).
- `notifempty` — skip rotation of 0-byte files.
- `copytruncate` — **chosen over `create` + reload** because neither mosquitto nor the Telegram bot has a SIGHUP-for-reopen contract we want to rely on. `copytruncate` rotates by copying and truncating in place; tiny window of data loss is acceptable for non-critical logs. For `app.log` we'd want `create` but §6.1 explains why app.log is NOT in this config.
- `dateext` — adds `-YYYYMMDD` suffix to rotated files (easier than `.1.gz`, `.2.gz` numbering for ops).

### 6.3 Install procedure

On production device (10.2.5.244), post-merge:
```bash
sudo cp /opt/wb-irrigation/irrigation/configs/logrotate.d/wb-irrigation /etc/logrotate.d/wb-irrigation
sudo chmod 644 /etc/logrotate.d/wb-irrigation
sudo chown root:root /etc/logrotate.d/wb-irrigation
# Dry-run to verify
sudo logrotate --debug /etc/logrotate.d/wb-irrigation
# If clean:
sudo logrotate --force /etc/logrotate.d/wb-irrigation   # one-shot test rotation
```

**Ownership note:** `telegram.txt` is currently owned by whatever user runs `wb-irrigation.service` (today: `root`). `copytruncate` preserves ownership, so no chown step needed. When Wave 3 migrates to `User=wb-irrigation`, ownership stays correct because the process itself writes the file.

### 6.4 Tests

Pure config file → no pytest test. Acceptance is the deploy-time dry-run `logrotate --debug`. Mark as manual in §10.

**Optional CI lint (MAY):** add `pre-commit` hook running `logrotate --debug -d` against `configs/logrotate.d/wb-irrigation` in a sandbox — nice-to-have, not Wave 2 blocker.

---

## Section 7 — `requirements.txt` diff

Add to `requirements.txt`:

```
# Wave 2 observability baseline
python-json-logger>=2.0,<3.0       # structured JSON log formatter
prometheus-client>=0.20,<1.0       # /metrics endpoint + Counters/Gauges/Histograms
# systemd-python deliberately NOT added — we use manual NOTIFY_SOCKET in services/systemd_notify.py
# to avoid libsystemd-dev compile dependency on Wirenboard ARM / macOS dev laptops.
```

**`requirements-dev.txt` (verify before adding):**
- `freezegun>=1.2` — used by test §2.7 #2 (RFC3339 timestamp assertion). If not present, add; else skip.

**No removals.** No version bumps to existing pins (risk-minimising — Wave 2 is additive).

**Validation steps for executor:**
1. `pip install -r requirements.txt` in a clean venv succeeds without build errors.
2. `pip list | grep -E 'python-json-logger|prometheus-client'` shows both.
3. `python -c "from pythonjsonlogger import jsonlogger; from prometheus_client import CollectorRegistry"` succeeds.

---

## Section 8 — Branches plan

**Base:** `main @ feb7956` (Wave 1 tip).

Proposed branch names (prefix `wave2/`):

| Order | Branch                              | Features | Files touched                                                                                                  | Est LOC diff | Conflicts with                              |
|-------|-------------------------------------|----------|----------------------------------------------------------------------------------------------------------------|---------------|---------------------------------------------|
| 1     | `wave2/f1-json-logs`                | F1       | `services/logging_setup.py`, `requirements.txt`, `tests/unit/test_logging_setup_json.py`                        | +220 / -40    | none                                        |
| 2     | `wave2/f3-correlation-id`           | F3       | `services/correlation.py` (new), `app.py` (middleware), `services/logging_setup.py` (import hook), `tests/unit/test_correlation.py` (new) | +180 / -5     | **F1** (logging_setup.py formatter reads correlation_id) |
| 3     | `wave2/f2-health-endpoints`         | F2       | `routes/health_api.py` (new), `app.py` (blueprint register + csrf.exempt + init_metrics call), `services/app_init.py` (init_metrics + `_boot_sync_done`), `requirements.txt`, `tests/unit/test_health_api.py` (new) | +550 / -10    | F3 (shared `app.py` `before_request` stack — resolve by merging F3 first) |
| 4     | `wave2/f4-systemd-watchdog`         | F4       | `wb-irrigation.service`, `services/systemd_notify.py` (new), `services/app_init.py` (start/stop heartbeat hooks), `tests/unit/test_systemd_notify.py` (new) | +180 / -5     | F2 (both edit `services/app_init.initialize_app`) — small, manual merge |
| 5     | `wave2/f5-logrotate`                | F5       | `configs/logrotate.d/wb-irrigation` (new)                                                                      | +30 / 0       | none                                        |

**Merge order (rationale):**
1. **F1 first** — self-contained; F3 depends on the `WBJsonFormatter` import hook existing.
2. **F3 second** — adds `correlation.py` + middleware; F2 will want to use correlation_id in /readyz logs.
3. **F2 third** — biggest diff; needs `app.py` middleware slot already stabilised by F3.
4. **F4 fourth** — edits `services/app_init.py` alongside F2's `init_metrics` call. Trivial conflict.
5. **F5 last** — config-only; zero conflict risk.

**Alternative fast-path:** F1+F3 can land together as one PR if the same executor implements them sequentially — they're closely coupled. F2, F4, F5 must be separate PRs for reviewability.

**CI gates per PR (per existing `.github/workflows/ci.yml`, assumed from Wave 1):**
- `pytest tests/` all pass (regression vs. Wave 1 baseline 802/2, see §9.4).
- `ruff check` clean on changed files.
- `pip install -r requirements.txt` succeeds.
- (MAY) `promtool check metrics` against a scraped `/metrics` — future CI enhancement, not blocking Wave 2.

---

## Section 9 — pytest strategy

### 9.1 New test files to create

| Feature | Test file                                              | Estimated test count |
|---------|--------------------------------------------------------|----------------------|
| F1      | `tests/unit/test_logging_setup_json.py`                | 9                    |
| F2      | `tests/unit/test_health_api.py`                        | 14                   |
| F3      | `tests/unit/test_correlation.py`                       | 9                    |
| F4      | `tests/unit/test_systemd_notify.py`                    | 6                    |
| F5      | (no pytest — config file)                              | 0                    |
| Integr. | `tests/integration/test_observability_smoke.py`        | 3 (end-to-end: /healthz, /readyz, /metrics all 200 with Flask test client) |
| **Total new tests** |                                              | **~41**              |

### 9.2 Baseline and non-regression

- **Current baseline (per user: post-Wave-1 merges):** `802 passed / 2 failed`.
- The 2 failing tests are SSE-related (pre-existing; covered by MASTER-C4, scheduled for removal in Wave 3).
- **Wave 2 exit criterion:** `802 + 41 = 843 passed / 2 failed / 0 new failing`. Any net new failing test from Wave 2 = ship blocker.

### 9.3 Mock strategies for /readyz checks

| Check       | Mock approach                                                                                              |
|-------------|-------------------------------------------------------------------------------------------------------------|
| DB down     | `monkeypatch.setattr(sqlite3, 'connect', raise_OperationalError)` OR temporary rename of `irrigation.db`    |
| MQTT down   | Clear `services.mqtt_pub._MQTT_CLIENTS` dict; or patch `is_connected()` to return False                      |
| Scheduler down | Patch `irrigation_scheduler.get_scheduler` to return a MagicMock with `running=False`                    |
| boot_reconcile pending | Patch `services.app_init._boot_sync_done = False` before calling endpoint                        |
| Disk full   | Patch `os.statvfs` to return a namedtuple with `f_bavail * f_frsize < 50 * 1024 * 1024`                     |

Each check has an independent "fail" test; then one "all-fail" test to verify aggregate status=503.

### 9.4 `conftest.py` additions

Probably zero — existing `tests/conftest.py` (Wave 1) already provides:
- `client` fixture (Flask test client with TESTING=1)
- `temp_db` fixture
- `app` fixture

If any Wave 2 test needs a fresh logger setup per test (to avoid cross-contamination of the formatter config), add a fixture:

```python
@pytest.fixture
def isolated_logging(tmp_path):
    """Reset root logger handlers for the duration of the test."""
    import logging
    root = logging.getLogger()
    saved = root.handlers[:]
    saved_level = root.level
    root.handlers.clear()
    yield
    root.handlers = saved
    root.setLevel(saved_level)
```

### 9.5 Running the suite

Per existing `start_tests.sh` or `pytest.ini` / `pyproject.toml` — executor should NOT touch test config. If a new marker is needed (e.g. `@pytest.mark.observability`) it is nice-to-have; not required for Wave 2.

---

## Section 10 — Deploy notes for owner (10.2.5.244)

After the Wave 2 PRs merge to `main` (or the production branch — clarify §11 Q2) and CI is green:

### 10.1 Pre-deploy checklist

- [ ] Confirm Wave 1 is already on the device (`curl -fs http://10.2.5.244:8080/health` returns JSON today).
- [ ] Backup: `sudo cp /etc/systemd/system/wb-irrigation.service /root/wb-irrigation.service.pre-wave2.bak`.
- [ ] Snapshot DB: `sqlite3 /opt/wb-irrigation/irrigation/irrigation.db ".backup /root/irrigation.pre-wave2.bak"`.

### 10.2 Deploy steps (in order)

```bash
# 1. Pull new code
cd /opt/wb-irrigation/irrigation
sudo git fetch origin
sudo git checkout main
sudo git pull --ff-only

# 2. Install new Python deps
sudo /opt/wb-irrigation/irrigation/venv/bin/pip install -r requirements.txt

# 3. Install new systemd unit
sudo cp /opt/wb-irrigation/irrigation/wb-irrigation.service /etc/systemd/system/wb-irrigation.service
sudo systemctl daemon-reload

# 4. Install logrotate config
sudo cp /opt/wb-irrigation/irrigation/configs/logrotate.d/wb-irrigation /etc/logrotate.d/wb-irrigation
sudo chmod 644 /etc/logrotate.d/wb-irrigation
sudo chown root:root /etc/logrotate.d/wb-irrigation
sudo logrotate --debug /etc/logrotate.d/wb-irrigation   # dry-run

# 5. Restart service
sudo systemctl restart wb-irrigation.service

# 6. Watch boot
sudo journalctl -u wb-irrigation -f --since "1 minute ago"
# Expect: "sd_notify READY=1 sent" within 30 s; then "sd_notify heartbeat thread started"
```

### 10.3 Post-deploy verification

```bash
# Service active
sudo systemctl status wb-irrigation.service   # expect Active: active (running)

# Watchdog wired
sudo systemctl show wb-irrigation.service -p WatchdogUSec   # expect WatchdogUSec=1min

# Endpoints
curl -fs http://localhost:8080/healthz                       # {"status":"ok"}
curl -fs http://localhost:8080/readyz | jq .                 # {"status":"ok", "checks":{...}}
curl -fs http://localhost:8080/metrics | head -30            # HELP/TYPE lines

# Log format
tail -n 5 /opt/wb-irrigation/irrigation/backups/app.log | jq -c '.'  # should parse; every line valid JSON

# Correlation ID round-trip
curl -fsv -H 'X-Request-ID: deploy-verify-12345' http://localhost:8080/healthz 2>&1 | grep X-Request-ID
# expect both request & response to show deploy-verify-12345
```

### 10.4 Rollback

If `/readyz` returns 503 persistently or service fails to start:
```bash
sudo cp /root/wb-irrigation.service.pre-wave2.bak /etc/systemd/system/wb-irrigation.service
sudo systemctl daemon-reload
sudo git checkout feb7956   # Wave 1 tip
sudo /opt/wb-irrigation/irrigation/venv/bin/pip install -r requirements.txt
sudo systemctl restart wb-irrigation.service
```

logrotate config is safe to leave in place during rollback (no runtime impact on Wave 1).

### 10.5 Monitoring for 48 h post-deploy

- Tail `/var/log/syslog | grep wb-irrigation` for `"Watchdog timeout"` — should never appear. If it does: inspect `journalctl -u wb-irrigation` for the last `sd_notify WATCHDOG=1 sent` timestamp and investigate why the heartbeat stopped.
- `curl localhost:8080/metrics` and eyeball `wb_http_requests_total`, `wb_mqtt_publish_total` counters incrementing.
- `sudo logrotate --debug /etc/logrotate.d/wb-irrigation` after 24 h — no errors.

---

## Section 11 — Open questions (owner decision required before implementation)

| # | Question                                                                                   | Default if no decision                                        | Impact if changed later                                         |
|---|--------------------------------------------------------------------------------------------|---------------------------------------------------------------|-----------------------------------------------------------------|
| Q1 | Should `/metrics` be IP-restricted at nginx (LAN + 127.0.0.1 only)?                       | No restriction in Wave 2; Wave 3 will add nginx allow-list (per MASTER-M5 §776) | Minor — post-deploy nginx edit; no code change                    |
| Q2 | Deploy to `main` directly, or to `refactor/v2` per Wave 1 convention?                      | Follow Wave 1 (whichever branch Wave 1 landed on; confirm with executor) | Affects CI gate; no code change                                  |
| Q3 | Add `X-Correlation-ID` as incoming alias, or only accept `X-Request-ID`?                   | Accept both (§4.3)                                           | Tiny — one regex + one extra header lookup                      |
| Q4 | Migrate `wb-irrigation.service` to `User=wb-irrigation` in Wave 2, or defer to Wave 3?    | **Defer to Wave 3** (this doc) — requires chown of prod dirs, owner approval | Wave 3 will do a chown-migration window; `WatchdogSec` etc. still land in Wave 2 unchanged |
| Q5 | Should `wb_build_info` include `GIT_COMMIT` env var? If yes, who sets it in systemd unit?  | Add `Environment=GIT_COMMIT=...` in unit file during deploy; default `"unknown"` | Minor — missing label value just says "unknown"                 |
| Q6 | Does the console handler stay JSON in prod too (journald double-wraps), or switch to plain? | **JSON in prod, plain in dev via `WB_LOG_FORMAT=plain`** (§2.5) | Affects `journalctl` readability; can be toggled via env at any time |
| Q7 | Should logrotate cover `/opt/wb-irrigation/backups/*.log.gz` older than 30 days (cleanup)? | Out of Wave 2; Python handler already purges via `backupCount=7` | Minor — optional cleanup stanza could be added post-deploy        |
| Q8 | Is `freezegun` an acceptable new dev dependency for timestamp test (§2.7 #2)?              | Yes — widely used, MIT, no runtime impact                    | None                                                            |

---

## Section 12 — Summary checklist (for executor)

Features (MUST for Wave 2 exit):
- [ ] **F1** — `WBJsonFormatter` (python-json-logger) replaces hand-rolled `JSONFormatter` in `services/logging_setup.py`. File + console handlers emit RFC 3339 ms + 8 required fields. 9 unit tests green.
- [ ] **F2** — `routes/health_api.py` new blueprint with `/healthz`, `/readyz` (5 checks), `/metrics` (≥10 populated metrics). Blueprint registered + CSRF-exempt. `init_metrics()` called from `app_init`. 14 unit tests + 3 integration tests green.
- [ ] **F3** — `services/correlation.py` ContextVar + `X-Request-ID` middleware in `app.py`. Malicious input sanitised. 9 unit tests green.
- [ ] **F4** — `wb-irrigation.service` → `Type=notify` + `WatchdogSec=60`; `services/systemd_notify.py` with manual NOTIFY_SOCKET bridge; heartbeat thread started in `app_init`, stopped in shutdown handler. 6 unit tests green.
- [ ] **F5** — `configs/logrotate.d/wb-irrigation` with mosquitto + telegram.txt stanzas. `logrotate --debug` clean.

Cross-cutting:
- [ ] `requirements.txt` += `python-json-logger>=2.0,<3.0`, `prometheus-client>=0.20,<1.0`.
- [ ] Regression: total tests `≥ 843 passed / 2 failed / 0 new failed` (baseline 802 + 41 new).
- [ ] Deploy doc (§10) reviewed and acknowledged by owner.
- [ ] Open questions §11 answered (or explicitly accepted defaults).

**End of design document.**
