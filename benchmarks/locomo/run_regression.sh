#!/bin/bash
# B12 LoCoMo Regression Test — CI-friendly wrapper
#
# Runs the benchmark against key storage×search combinations,
# compares against baseline, and exits 1 on any regression.
#
# Usage:
#   ./benchmarks/locomo/run_regression.sh              # Compare against baseline
#   ./benchmarks/locomo/run_regression.sh --create      # Create initial baseline
#   ./benchmarks/locomo/run_regression.sh --threshold 0.10  # Custom threshold
#
# Run from repo root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASELINE="${SCRIPT_DIR}/baseline-v11.json"
THRESHOLD=0.05
CREATE_BASELINE=false

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --create) CREATE_BASELINE=true; shift ;;
    --threshold) THRESHOLD="$2"; shift 2 ;;
    --baseline) BASELINE="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# Find Python — prefer B12 venv, fall back to system
PYTHON=""
for p in \
  "$HOME/.local/pipx/venvs/mcp-memory-service/bin/python3" \
  "$HOME/.local/b12-venv/bin/python3" \
  "python3"; do
  if command -v "$p" &>/dev/null 2>&1 || [ -x "$p" ]; then
    PYTHON="$p"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  echo "ERROR: No Python3 found."
  exit 1
fi

echo "╔══════════════════════════════════════════════════╗"
echo "║  B12 LoCoMo Regression Test                      ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "  Python:    $PYTHON"
echo "  Baseline:  $BASELINE"
echo "  Threshold: ${THRESHOLD} ($(echo "$THRESHOLD * 100" | bc)%)"
echo ""

T_START=$(date +%s)

if [ "$CREATE_BASELINE" = true ]; then
  echo "Creating initial baseline..."
  echo ""

  "$PYTHON" "${SCRIPT_DIR}/eval_b12.py" \
    --mode observations \
    --search keyword \
    --output json \
    --save-baseline "$BASELINE"

  T_END=$(date +%s)
  DURATION=$((T_END - T_START))
  echo ""
  echo "Baseline created at $BASELINE"
  echo "Duration: ${DURATION}s"
  exit 0
fi

# Check baseline exists
if [ ! -f "$BASELINE" ]; then
  echo "No baseline found at $BASELINE"
  echo "Creating initial baseline (first run)..."
  echo ""

  "$PYTHON" "${SCRIPT_DIR}/eval_b12.py" \
    --mode observations \
    --search keyword \
    --output json \
    --save-baseline "$BASELINE"

  T_END=$(date +%s)
  DURATION=$((T_END - T_START))
  echo ""
  echo "Baseline created. Re-run to compare."
  echo "Duration: ${DURATION}s"
  exit 0
fi

# Run regression test
echo "Running benchmark and comparing against baseline..."
echo ""

EXIT_CODE=0
"$PYTHON" "${SCRIPT_DIR}/eval_b12.py" \
  --mode observations \
  --search keyword \
  --output json \
  --compare "$BASELINE" \
  --threshold "$THRESHOLD" || EXIT_CODE=$?

T_END=$(date +%s)
DURATION=$((T_END - T_START))

echo ""
echo "────────────────────────────────────────────────"
if [ $EXIT_CODE -eq 0 ]; then
  echo "  RESULT: PASS (no regressions)"
else
  echo "  RESULT: FAIL (regressions detected)"
fi
echo "  Duration: ${DURATION}s"
echo "────────────────────────────────────────────────"

exit $EXIT_CODE
