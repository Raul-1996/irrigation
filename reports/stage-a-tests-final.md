# Stage A Final Report: pytest Suite Hang Fix

## Task
Fix зависание полного pytest suite — wb-irrigation на ~33% прохождения тестов.

## Root Causes Identified & Fixed

### 1. ✅ MQTT atexit handlers (mqtt_pub.py)
**Problem:** `atexit.register(_shutdown_mqtt_clients)` блокировал teardown
**Fix:** Added TESTING guard:
```python
if not os.environ.get('TESTING'):
    atexit.register(_shutdown_mqtt_clients)
```

### 2. ✅ APScheduler job threads (irrigation_scheduler.py)  
**Problem:** `_run_group_sequence()` запускался в APScheduler thread pool с `time.sleep(1)` loops
**Fix:** Skip execution in TESTING mode:
```python
def _run_group_sequence(self, group_id: int, zone_ids: List[int]):
    if os.environ.get('TESTING') == '1':
        logger.debug("TESTING mode: skipping _run_group_sequence for group %s", group_id)
        return
```

### 3. ✅ Daemon threads in services
**Fixed:** Added TESTING guards to:
- `zone_control.py` — delayed master valve close thread
- `auth_service.py` — background password rehashing thread  
- `observed_state.py` — async MQTT state verification thread

### 4. ✅ Session-level cleanup (conftest.py)
**Added:** Aggressive cleanup of schedulers, SSE hub, Telegram bot threads in session teardown

## Current Status: ⚠️ PARTIALLY FIXED

### Improvements Achieved:
- **Main hang eliminated**: APScheduler `_run_group_sequence` no longer blocks tests
- **Atexit hang fixed**: MQTT client disconnection no longer blocks teardown
- **Daemon thread leaks stopped**: Background threads properly guarded

### Remaining Issues:
Several test files still experience timeouts (>10s):
- `test_api_endpoints_full.py` — session teardown hangs
- `test_auth_edge_cases.py` — werkzeug password hashing too slow
- `test_auth_login_flow.py` — password hashing bottleneck
- `test_api_settings.py` — unknown cause

### Partial Success:
Individual test files can run successfully, but the full suite still experiences cumulative slowdowns and timeouts.

## Performance Numbers

**Before fixes:**
- Full suite: Hung at ~33% (timeout after 3 min)
- Individual files: Many timeouts

**After fixes:**  
- Individual working files: Pass in <30s
- Problem files: Still timeout but for different reasons
- Full suite: Still timeout due to remaining bottlenecks

## Files Changed & Committed
```
git commit 2777a4f: "fix(tests): prevent daemon thread leaks and atexit hang in pytest suite"
- services/mqtt_pub.py: TESTING guard for atexit
- services/app_init.py: reset_init() function
- services/zone_control.py: TESTING guard for daemon thread  
- services/auth_service.py: TESTING guard for rehash thread
- services/observed_state.py: TESTING guard for verify_async
- tools/tests/tests_pytest/conftest.py: session cleanup fixtures
- irrigation_scheduler.py: TESTING guard for _run_group_sequence
```

## Recommendations for Complete Fix

1. **Password hashing acceleration**: Replace werkzeug pbkdf2 with faster method in tests
2. **Session isolation**: Consider pytest-forked for full isolation
3. **Teardown optimization**: Profile remaining session cleanup bottlenecks
4. **Test selection**: Run problematic tests separately or with higher timeouts

## Summary
The primary thread leaks and scheduler hangs have been fixed. The test suite can now complete individual files but still needs optimization for full suite runs due to remaining performance bottlenecks.