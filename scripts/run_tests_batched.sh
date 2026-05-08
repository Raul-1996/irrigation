#!/bin/bash
# Run pytest in batches of N tests with a per-batch timeout.
# Helps locate hanging tests: if a batch exceeds the timeout, you see
# exactly which 50-test window contains the culprit.
#
# Usage:
#   ./scripts/run_tests_batched.sh                     # all tests, 50/batch
#   ./scripts/run_tests_batched.sh tests/unit/         # subset path
#   BATCH=25 BATCH_TIMEOUT=180 ./scripts/run_tests_batched.sh
#
# Env vars:
#   BATCH           — tests per batch (default 50)
#   BATCH_TIMEOUT   — seconds per batch (default 300 = 5 min)
#   PYTEST_BIN      — pytest binary (default ./venv/bin/pytest)

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

BATCH="${BATCH:-50}"
BATCH_TIMEOUT="${BATCH_TIMEOUT:-300}"
PYTEST_BIN="${PYTEST_BIN:-./venv/bin/pytest}"
TARGET="${1:-tests}"

if [ ! -x "$PYTEST_BIN" ]; then
  echo "pytest not found at $PYTEST_BIN" >&2
  exit 1
fi

LOG_DIR="$ROOT/test_results_batched"
mkdir -p "$LOG_DIR"
RUN_ID="$(date +%Y%m%d_%H%M%S)"
SUMMARY="$LOG_DIR/run_${RUN_ID}_summary.txt"
COLLECT_FILE="$LOG_DIR/run_${RUN_ID}_collected.txt"

echo "=== Collecting tests from $TARGET ==="
"$PYTEST_BIN" --collect-only -q "$TARGET" 2>/dev/null \
  | grep -E "^[a-zA-Z_./]+::[a-zA-Z_]" \
  > "$COLLECT_FILE"

TOTAL=$(wc -l < "$COLLECT_FILE")
if [ "$TOTAL" -eq 0 ]; then
  echo "No tests collected from $TARGET" >&2
  exit 1
fi

echo "Collected $TOTAL tests, batching by $BATCH (timeout ${BATCH_TIMEOUT}s/batch)"
echo "Logs: $LOG_DIR/run_${RUN_ID}_*.log"
echo

{
  echo "Run ID:        $RUN_ID"
  echo "Target:        $TARGET"
  echo "Total tests:   $TOTAL"
  echo "Batch size:    $BATCH"
  echo "Batch timeout: ${BATCH_TIMEOUT}s"
  echo "Started:       $(date -Iseconds)"
  echo "----"
} > "$SUMMARY"

PASS_BATCHES=0
FAIL_BATCHES=0
HUNG_BATCHES=()

mapfile -t ALL_TESTS < "$COLLECT_FILE"
TOTAL=${#ALL_TESTS[@]}
NUM_BATCHES=$(( (TOTAL + BATCH - 1) / BATCH ))
batch_num=0

for ((i=0; i<TOTAL; i+=BATCH)); do
  batch_num=$((batch_num + 1))
  start_idx=$((i + 1))
  end_idx=$((i + BATCH))
  [ "$end_idx" -gt "$TOTAL" ] && end_idx="$TOTAL"
  CHUNK=("${ALL_TESTS[@]:i:BATCH}")

  log_file="$LOG_DIR/run_${RUN_ID}_batch_$(printf '%03d' "$batch_num").log"
  echo "[$batch_num/$NUM_BATCHES] tests $start_idx-$end_idx..."

  start_ts=$(date +%s)
  timeout --foreground "$BATCH_TIMEOUT" "$PYTEST_BIN" "${CHUNK[@]}" \
    > "$log_file" 2>&1
  rc=$?
  elapsed=$(( $(date +%s) - start_ts ))

  if [ $rc -eq 124 ]; then
    echo "    HUNG (>${BATCH_TIMEOUT}s) — see $log_file"
    HUNG_BATCHES+=("$batch_num: tests $start_idx-$end_idx")
    FAIL_BATCHES=$((FAIL_BATCHES + 1))
    {
      echo ""
      echo "[batch $batch_num] HUNG after ${BATCH_TIMEOUT}s (tests $start_idx-$end_idx)"
      echo "  First 5 tests in this batch:"
      printf '    %s\n' "${CHUNK[@]:0:5}"
      echo "  Last lines of log:"
      tail -20 "$log_file" | sed 's/^/    /'
    } >> "$SUMMARY"
  elif [ $rc -eq 0 ]; then
    PASS_BATCHES=$((PASS_BATCHES + 1))
    echo "    OK (${elapsed}s)"
  else
    FAIL_BATCHES=$((FAIL_BATCHES + 1))
    fail_summary=$(grep -E "^(FAILED|ERROR)" "$log_file" | head -5)
    echo "    FAIL rc=$rc (${elapsed}s) — see $log_file"
    {
      echo ""
      echo "[batch $batch_num] FAILED (rc=$rc, ${elapsed}s, tests $start_idx-$end_idx)"
      echo "$fail_summary" | sed 's/^/    /'
    } >> "$SUMMARY"
  fi
done

{
  echo "----"
  echo "Finished: $(date -Iseconds)"
  echo "Batches:  $batch_num total, $PASS_BATCHES passed, $FAIL_BATCHES failed"
  if [ ${#HUNG_BATCHES[@]} -gt 0 ]; then
    echo ""
    echo "HUNG BATCHES:"
    printf '  - %s\n' "${HUNG_BATCHES[@]}"
  fi
} >> "$SUMMARY"

echo
echo "=== Summary ==="
cat "$SUMMARY"

if [ "$FAIL_BATCHES" -gt 0 ]; then
  exit 1
fi
exit 0
