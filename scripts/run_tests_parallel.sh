#!/bin/bash
# Run pytest in parallel using pytest-xdist.
#
# Strategy:
#   --dist=loadfile groups all tests from one file onto one worker.
#   This keeps file-level fixtures (DB reload, app singleton patching)
#   thread-safe per worker, while spreading work across N processes.
#   Tests marked `@pytest.mark.serial` are run AFTER the parallel pass,
#   in a single process, to avoid race conditions on shared state
#   (MQTT broker, sse_hub, APScheduler, etc.).
#
# Usage:
#   ./scripts/run_tests_parallel.sh                      # all tests, 10 workers
#   ./scripts/run_tests_parallel.sh tests/unit/          # subset
#   WORKERS=8 ./scripts/run_tests_parallel.sh            # custom worker count
#   DIST=loadgroup ./scripts/run_tests_parallel.sh       # alternate dist mode
#   SKIP_SERIAL=1 ./scripts/run_tests_parallel.sh        # parallel only, no second pass
#
# Env vars:
#   WORKERS       — parallel workers (default 10; host has 16 cores)
#   DIST          — xdist distribution mode (default loadfile)
#                   options: load | loadfile | loadgroup | loadscope | worksteal
#   SKIP_SERIAL   — if set, skip the post-pass for @pytest.mark.serial tests
#   PYTEST_BIN    — pytest binary (default ./venv/bin/pytest)
#   EXTRA_ARGS    — extra args appended to both pytest invocations

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

WORKERS="${WORKERS:-10}"
DIST="${DIST:-loadfile}"
PYTEST_BIN="${PYTEST_BIN:-./venv/bin/pytest}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
TARGET="${1:-tests}"

if [ ! -x "$PYTEST_BIN" ]; then
  echo "pytest not found at $PYTEST_BIN" >&2
  exit 1
fi

LOG_DIR="$ROOT/test_results_batched"
mkdir -p "$LOG_DIR"
RUN_ID="$(date +%Y%m%d_%H%M%S)"
PARALLEL_LOG="$LOG_DIR/parallel_${RUN_ID}.log"
SERIAL_LOG="$LOG_DIR/serial_${RUN_ID}.log"
SUMMARY="$LOG_DIR/parallel_${RUN_ID}_summary.txt"

echo "=== Parallel pytest runner ==="
echo "Target:    $TARGET"
echo "Workers:   $WORKERS"
echo "Dist mode: $DIST"
echo "Logs:      $PARALLEL_LOG"
echo

START_TS=$(date +%s)

# Phase 1: parallel pass (deselect serial tests)
echo "--- Phase 1: parallel run (-n=$WORKERS --dist=$DIST), excluding @serial ---"
# shellcheck disable=SC2086
"$PYTEST_BIN" "$TARGET" \
  -n="$WORKERS" \
  --dist="$DIST" \
  -m "not serial" \
  $EXTRA_ARGS \
  2>&1 | tee "$PARALLEL_LOG"
PARALLEL_RC=${PIPESTATUS[0]}

PARALLEL_END_TS=$(date +%s)
PARALLEL_DURATION=$((PARALLEL_END_TS - START_TS))
echo
echo "Parallel phase: ${PARALLEL_DURATION}s, exit=$PARALLEL_RC"

SERIAL_RC=0
if [ -z "${SKIP_SERIAL:-}" ]; then
  # Phase 2: serial pass for @pytest.mark.serial tests
  echo
  echo "--- Phase 2: serial run (-n0) for @pytest.mark.serial tests ---"
  # Check if any tests are marked serial; if not, skip silently
  SERIAL_COUNT=$("$PYTEST_BIN" "$TARGET" -m serial --collect-only -q 2>/dev/null \
    | grep -cE "^[a-zA-Z_./]+::[a-zA-Z_]" || true)
  if [ "$SERIAL_COUNT" -gt 0 ]; then
    echo "Found $SERIAL_COUNT serial tests, running sequentially..."
    # shellcheck disable=SC2086
    "$PYTEST_BIN" "$TARGET" \
      -m serial \
      $EXTRA_ARGS \
      2>&1 | tee "$SERIAL_LOG"
    SERIAL_RC=${PIPESTATUS[0]}
  else
    echo "No tests marked @pytest.mark.serial — skipping phase 2."
  fi
fi

END_TS=$(date +%s)
TOTAL_DURATION=$((END_TS - START_TS))

{
  echo "Run ID:           $RUN_ID"
  echo "Target:           $TARGET"
  echo "Workers:          $WORKERS"
  echo "Dist mode:        $DIST"
  echo "Parallel time:    ${PARALLEL_DURATION}s"
  echo "Total time:       ${TOTAL_DURATION}s"
  echo "Parallel rc:      $PARALLEL_RC"
  echo "Serial rc:        $SERIAL_RC"
  echo "Finished:         $(date -Iseconds)"
} | tee "$SUMMARY"

# Aggregate exit code: non-zero if either phase failed
if [ "$PARALLEL_RC" -ne 0 ] || [ "$SERIAL_RC" -ne 0 ]; then
  exit 1
fi
exit 0
