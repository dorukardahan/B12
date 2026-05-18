#!/bin/bash
# B12 Codex CLI — Tiered context emission with spillover.
#
# Codex 0.130.0 silently caps SessionStart/UserPromptSubmit/PostCompact
# additionalContext at ~2,500 tokens (issue #22861 2026-05-15). Anything
# above the cap is spilled to a temp file and replaced inline with a
# preview + "Full hook output saved to: <path>" pointer that the model
# must notice and open with a tool call. Without tiering, B12's 6 KB
# SessionStart payload becomes invisible (silent recall failure that
# looks like a B12 bug).
#
# Usage from a Codex hook:
#   FULL_CONTEXT=$(b12_compose_session_context ...)
#   b12_codex_emit_with_spillover "$FULL_CONTEXT" "$SESSION_ID"
#
# Emits B12's prioritized top-K injected directly (≤ ~2,400 tokens, leaving
# headroom under the ~2,500 cap), then spills the remainder to
# ~/.B12/staging/spillover-<sid>.md with a model-visible pointer.

# Token budget: keep direct payload below cap with headroom for the pointer
# block + Codex's own framing overhead. ~2,400 tokens × 4 chars/token ≈ 9,600
# bytes. Empirically B12's SessionStart payloads are denser (Turkish + URLs
# + dates) so we use a conservative 9,200-byte ceiling.
B12_CODEX_DIRECT_BYTES="${B12_CODEX_DIRECT_BYTES:-9200}"
B12_SPILLOVER_DIR="${B12_DATA_DIR:-$HOME/.B12}/staging"

# Emit context with tiered spillover.
# Args: $1 = full context string, $2 = session-id (used in spillover filename)
#
# Codex review PR #41 round 5 — the original implementation used bash
# `${#full}` and `${full:0:N}` which are CHARACTER-based in UTF-8
# locales. B12 supports Turkish (2-byte UTF-8 chars: ı, ş, ç, ö, ü, ğ,
# İ, …) so 9,200 characters of Turkish content is ~13-15 KB of bytes
# — well over Codex's silent ~2,500-token additionalContext cap. The
# truncation must be byte-correct, so we offload size + cut math to
# python which can do `len(s.encode("utf-8"))` and byte-slice the
# UTF-8 bytes back to a valid str.
b12_codex_emit_with_spillover() {
  local full="$1"
  local sid="${2:-unknown}"

  mkdir -p "$B12_SPILLOVER_DIR" 2>/dev/null || true
  local spill_file="$B12_SPILLOVER_DIR/spillover-${sid}.md"

  python3 - "$full" "$spill_file" "$B12_CODEX_DIRECT_BYTES" << 'PYEOF'
import os, sys

full, spill_file, cap_raw = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    cap = int(cap_raw)
except ValueError:
    cap = 9200

# Byte-correct size — encode once, work in bytes.
encoded = full.encode("utf-8")
size = len(encoded)

if size <= cap:
    sys.stdout.write(full)
    sys.exit(0)

# Cut on a newline boundary within the byte budget so the inline
# portion is well-formed Markdown. If no newline lands in the budget,
# fall back to a hard byte cut and decode with error-replacement so a
# multibyte char that straddles the boundary doesn't yield invalid
# UTF-8 on stdout.
budget = encoded[:cap]
newline_idx = budget.rfind(b"\n")
if newline_idx > 0:
    cut_bytes = budget[: newline_idx + 1]
else:
    cut_bytes = budget

head = cut_bytes.decode("utf-8", errors="replace")
cut_size = len(cut_bytes)

try:
    with open(spill_file, "w", encoding="utf-8") as fh:
        fh.write(full)
except OSError:
    pass

sys.stdout.write(head)
sys.stdout.write("\n\n---\n")
sys.stdout.write(
    f"_B12: {size} bytes total; first {cut_size} bytes shown above._\n"
)
sys.stdout.write(
    f"_Full hook output saved to: {spill_file} — open with `Read` if you need the deferred memories._\n"
)
PYEOF
}

# If invoked directly (smoke test), emit synthetic context to stdout.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  TEST_SID="${1:-smoke}"
  TEST_BODY="${2:-Hello from B12 spillover smoke.}"
  b12_codex_emit_with_spillover "$TEST_BODY" "$TEST_SID"
fi
