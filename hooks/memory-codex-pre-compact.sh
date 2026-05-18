#!/bin/bash
# B12 Codex CLI — PreCompact hook (Plan §2 CX2).
#
# Mirrors memory-precompact.sh unchanged. Codex's PreCompactCommandInput
# (codex-rs/hooks/src/schema.rs:299) uses the same three fields the
# Claude Code PreCompact hook reads — session_id, transcript_path, cwd
# — and the underlying memory-precompact.sh extraction logic is
# format-agnostic (transcript_adapter.py auto-detects JSONL shape).
#
# We delegate rather than copy so the priority-weighted extraction
# logic stays single-sourced and any future memory-precompact.sh fix
# applies to Codex sessions automatically.
#
# Fail-open guard outer wrapper.

{
  set -o pipefail 2>/dev/null || true

  B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
  B12_HOOK_DIR_LOCAL="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
  LOG_DIR="$B12_BASE/memory-logs"
  ERR_LOG="$LOG_DIR/codex-hook-errors.log"
  mkdir -p "$LOG_DIR" 2>/dev/null || true

  DELEGATE="$B12_HOOK_DIR_LOCAL/memory-precompact.sh"
  if [ ! -x "$DELEGATE" ]; then
    # Fallback to source-tree location for in-repo smoke tests.
    DELEGATE="$(dirname "$0")/memory-precompact.sh"
  fi
  if [ ! -x "$DELEGATE" ]; then
    printf '[%s] memory-codex-pre-compact: delegate not found (memory-precompact.sh)\n' \
      "$(date '+%Y-%m-%d %H:%M:%S')" >> "$ERR_LOG" 2>/dev/null || true
    exit 0
  fi

  # Pass-through. The delegate inherits stdin and writes its own
  # JSON output (suppressed by Codex if PreCompact doesn't accept it).
  # Do NOT `exec`: that replaces the current process so the outer
  # fail-open wrapper would never run if the delegate exits non-zero
  # (Codex review PR #42 round 1). Run inline + swallow non-zero so
  # control returns to the wrapper for the exit-0 guarantee.
  "$DELEGATE" || true

} 2>>"${ERR_LOG:-/dev/null}" || true
exit 0
