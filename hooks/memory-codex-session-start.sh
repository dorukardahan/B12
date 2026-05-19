#!/bin/bash
# B12 Codex CLI — SessionStart hook (Plan §2 CX1).
#
# Fires when Codex opens a session (`source` ∈ startup|resume|clear).
# Emits B12 context as JSON `{hookSpecificOutput: {hookEventName,
# additionalContext}}` per codex-rs/hooks/src/schema.rs:336.
#
# Routes payload through hooks/_b12_codex_spillover.sh so the body stays
# under Codex 0.130.0's silent ~2,500-token cap on additionalContext
# (issue #22861); overflow lands in ~/.B12/staging/spillover-<sid>.md
# with a model-visible pointer.
#
# Wrapped in fail-open guard `{ ... } || true; exit 0` — issue #22008
# documents a failed Stop hook burning a 5-hour Pro quota via hidden
# subagent spawning; CX0's plan dictates the same posture for every
# memory-codex-*.sh.

# Fail-open outer wrapper. Body runs inside `{ ... }`; any error path
# falls through to `exit 0`. Stderr is captured to the error log so
# triage is possible without surfacing failure to Codex.
{
  set -o pipefail 2>/dev/null || true

  B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
  B12_HOOK_DIR_LOCAL="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
  STATE_DIR="$B12_BASE/state"
  LOG_DIR="$B12_BASE/memory-logs"
  ERR_LOG="$LOG_DIR/codex-hook-errors.log"
  mkdir -p "$STATE_DIR" "$LOG_DIR" 2>/dev/null || true

  # Source spillover helper.
  SPILL="$B12_HOOK_DIR_LOCAL/_b12_codex_spillover.sh"
  [ -f "$SPILL" ] || SPILL="$(dirname "$0")/_b12_codex_spillover.sh"
  # shellcheck disable=SC1090
  . "$SPILL" 2>/dev/null || true

  # Read Codex's JSON input from stdin.
  INPUT=""
  if [ ! -t 0 ]; then
    INPUT=$(cat)
  fi

  SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
  CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)
  SOURCE=$(printf '%s' "$INPUT" | jq -r '.source // "startup"' 2>/dev/null)
  [ -z "$SID" ] && SID="unknown"

  PROJECT=$(basename "${CWD:-$PWD}")
  ACTIVE_GOAL_FILE="$STATE_DIR/active-codex-goal-${SID}.txt"

  # Compose body. Sections (in priority order):
  #   1. Active /goal (if any) — first because it shapes everything else.
  #   2. Top-K recent memories for this project (FTS query via the
  #      shared MCP daemon would be heavier; we use a fast SQLite
  #      grep for the session-start emission and let the model pull
  #      richer context via memory_search MCP tool if needed).
  #   3. Cross-project highlights (latest 3 decisions / learnings).
  BODY=""

  if [ -f "$ACTIVE_GOAL_FILE" ]; then
    GOAL_BODY=$(cat "$ACTIVE_GOAL_FILE" 2>/dev/null)
    BODY="${BODY}🎯 Active /goal (Codex session $SID):
${GOAL_BODY}

---
"
  fi

  # Cross-platform DB resolution via shared helper (closes the real
  # bug noted in B12_polyglot_audit_2026-05-19.md §C3: this hook
  # previously only looked at macOS + Linux, missing the Windows /
  # WSL `~/AppData/Local/mcp-memory/sqlite_vec.db` branch.
  # shellcheck source=./_b12_common.sh disable=SC1091
  . "$B12_HOOK_DIR_LOCAL/_b12_common.sh" 2>/dev/null || true
  if command -v b12_resolve_db_path >/dev/null 2>&1; then
    DB="$(b12_resolve_db_path)"
  else
    DB="$HOME/Library/Application Support/mcp-memory/sqlite_vec.db"
    [ -f "$DB" ] || DB="$HOME/.local/share/mcp-memory/sqlite_vec.db"
    [ -f "$DB" ] || DB="$HOME/AppData/Local/mcp-memory/sqlite_vec.db"
  fi

  if [ -f "$DB" ]; then
    BODY="${BODY}📚 B12 — recent context for **${PROJECT}** (Codex SessionStart, source=${SOURCE}):

"
    # SQL via python so we get safe parameter binding — directory
    # basenames can contain apostrophes (john's-app) AND LIKE wildcards
    # `%` / `_`. The latter (round 3 P2) would silently match unrelated
    # tags (`proj:my_app` LIKE `%proj:myXapp%`). We escape % / _ with a
    # backslash and add ESCAPE '\\' to the LIKE clause.
    DB_OUT=$(python3 - "$DB" "$PROJECT" << 'PYEOF' 2>/dev/null
import sys, sqlite3
db_path, project = sys.argv[1], sys.argv[2]
# Escape LIKE wildcards. Must precede the backslash escape so we do not
# double-escape our own escape character.
proj_escaped = project.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
try:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Tag boundary fix (Codex review PR #41 round 4): the tags column is
    # comma-separated (`proj:B12,user:codex,...`). Plain LIKE substring
    # matching means `proj:api` would also match `proj:api-v2`. Wrap the
    # haystack and needle in commas so `proj:api` matches `,proj:api,`
    # exactly. `','||tags||','` produces the wrapped haystack at query
    # time without modifying stored rows.
    proj_pat = f"%,proj:{proj_escaped},%"
    proj_recent = conn.execute(
        """
        SELECT content, memory_type
        FROM memories
        WHERE (',' || tags || ',') LIKE ? ESCAPE '\\' AND deleted_at IS NULL
        ORDER BY updated_at DESC LIMIT 6
        """,
        (proj_pat,),
    ).fetchall()
    if proj_recent:
        for row in proj_recent:
            content = (row["content"] or "").replace("\n", " ").replace("  ", " ")[:200]
            mtype = row["memory_type"] or "general"
            print(f"- {content} [type={mtype}]")
    else:
        print("- (no project memories yet — write some with the memory_store MCP tool)")
    cross = conn.execute(
        """
        SELECT content
        FROM memories
        WHERE memory_type IN ('decision', 'learning', 'gotcha')
          AND (',' || tags || ',') NOT LIKE ? ESCAPE '\\' AND deleted_at IS NULL
        ORDER BY updated_at DESC LIMIT 3
        """,
        (proj_pat,),
    ).fetchall()
    if cross:
        print("")
        print("🔗 Cross-project highlights:")
        for row in cross:
            content = (row["content"] or "").replace("\n", " ").replace("  ", " ")[:180]
            print(f"- {content}")
except Exception:
    # Fail-open — empty stdout is fine; outer hook wrapper still exits 0.
    pass
PYEOF
)
    if [ -n "$DB_OUT" ]; then
      BODY="${BODY}${DB_OUT}
"
    fi
  fi

  # Empty-body guard — no JSON output at all is the cleanest signal to
  # Codex that the hook had nothing to add. Returning an empty
  # additionalContext would still surface a useless header in the model
  # view.
  if [ -z "$BODY" ]; then
    exit 0
  fi

  # Tier the emission through the spillover helper. If the helper isn't
  # loaded (deployment regression — see install.sh copy_hooks fix from
  # PR #41 round 3), DO NOT ship the raw payload: that would re-expose
  # issue #22861's silent ~2,500-token cap. Hard-truncate at the same
  # byte ceiling using python (bash ${var:0:N} is character-based and
  # UTF-8-unsafe for Turkish — Codex review PR #41 round 5).
  _b12_codex_truncate_bytes() {
    python3 - "$1" "${2:-9200}" << 'PYEOF'
import sys
text, cap = sys.argv[1], int(sys.argv[2])
encoded = text.encode("utf-8")
if len(encoded) <= cap:
    sys.stdout.write(text)
else:
    cut = encoded[:cap]
    nl = cut.rfind(b"\n")
    if nl > 0: cut = cut[: nl + 1]
    sys.stdout.write(cut.decode("utf-8", errors="replace"))
PYEOF
  }

  if command -v b12_codex_emit_with_spillover >/dev/null 2>&1; then
    EMIT=$(b12_codex_emit_with_spillover "$BODY" "$SID" 2>/dev/null)
    if [ -z "$EMIT" ]; then
      EMIT=$(_b12_codex_truncate_bytes "$BODY" 9200)
    fi
  else
    EMIT=$(_b12_codex_truncate_bytes "$BODY" 9200)
    {
      printf '[%s] memory-codex-session-start: spillover helper missing — byte-truncated body\n' "$(date '+%Y-%m-%d %H:%M:%S')"
    } >> "${ERR_LOG:-/dev/null}" 2>/dev/null
  fi

  # Build the wire-shape JSON. We do NOT trust raw shell interpolation
  # for the user-visible payload — use python's json.dumps for safe
  # escaping.
  python3 - "$EMIT" << 'PYEOF' 2>/dev/null
import json, sys
ctx = sys.argv[1]
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": ctx,
    }
}))
PYEOF

} 2>>"${ERR_LOG:-/dev/null}" || true
exit 0
