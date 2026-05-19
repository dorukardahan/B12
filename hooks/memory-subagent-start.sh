#!/bin/bash
# B12 Memory System — SubagentStart Hook (v1, 2026-05-19, PR-B2)
# Primes each subagent with a per-agent memory recall pass before it
# takes its first turn. Pairs with the long-standing SubagentStop hook
# (memory-subagent-stop.sh) which captures the response on the way out.
#
# Fires on: SubagentStart (general-purpose, Explore, Plan, custom agents).
# Output: hookSpecificOutput.additionalContext (<=10 KB direct emit;
#         Anthropic caps SubagentStart additionalContext at 50 KB per
#         anthropics/claude-code#52628).
# Budget: <2.5s, async daemon recall when socket is up; cold path falls
#         through to FTS5 substring + content_hash dedup.
#
# Why: SubagentStop already captures what the subagent SAID. The
# subagent's INPUT context — the parent task description, files in
# scope, accumulated decisions — is exactly the place B12 has the most
# leverage. Without this hook, every general-purpose Agent spawn starts
# from a cold context window and re-derives knowledge B12 already holds.

set -o pipefail 2>/dev/null || true

_B12_HOOK_DIR="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
# shellcheck disable=SC1091
. "$_B12_HOOK_DIR/_b12_common.sh" 2>/dev/null || true

( sleep 5 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
AGENT_TYPE=$(printf '%s' "$INPUT" | jq -r '.agent_type // "general-purpose"' 2>/dev/null)
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // ""' 2>/dev/null)
TRANSCRIPT=$(printf '%s' "$INPUT" | jq -r '.transcript_path // ""' 2>/dev/null)
# Task description is the parent's brief to the subagent. Codex review
# PR #44 round 2 P2: SubagentStart's wire payload only carries
# session_id, transcript_path, cwd, agent_type, agent_id — NOT a
# description/task field. Recover the task by scanning the parent
# transcript's most recent Agent / Task tool_use input. tail -n 200 is
# enough because a SubagentStart fires within seconds of the tool call.
TASK=""
if [ -n "$TRANSCRIPT" ] && [ -r "$TRANSCRIPT" ]; then
  TASK=$(tail -n 200 "$TRANSCRIPT" 2>/dev/null | jq -rs '
    [.[] | select(.message.content?) | .message.content[]?
       | select(.type == "tool_use" and (.name == "Agent" or .name == "Task"))
       | (.input.description // .input.prompt // .input.task // "")] | last // ""
  ' 2>/dev/null)
fi
# Final fallback: best-effort generic query keyed on agent_type alone.
[ -z "$TASK" ] && [ "$AGENT_TYPE" = "general-purpose" ] && { echo '{}'; exit 0; }

# Recall query: agent-type + first 200 chars of task (when present).
QUERY=$(printf 'agent:%s %s' "$AGENT_TYPE" "${TASK:0:200}")
_UID=$(id -u 2>/dev/null || echo $$)
SOCK="/tmp/b12-embed-${_UID}.sock"

# Platform-aware DB path via shared resolver (Darwin / Linux / WSL /
# Windows-bash). Codex review PR #44 P2 originated the inline switch
# here; v11.54 extracted it into hooks/_b12_common.sh so every shell
# hook stays in lockstep.
DB_PATH="$(b12_resolve_db_path)"

# Daemon recall (preferred). On cold-daemon path (socket missing or model
# still loading) fall through to a direct FTS5 SQLite query — same pattern
# memory-retrieval.sh uses. Codex review PR #44 round 3 P2: first-session
# subagent spawns commonly hit this path and previously returned `{}`.
HITS=""
if [ -S "$SOCK" ]; then
  REQ=$(printf '{"op":"recall","query":%s,"db_path":%s,"limit":4,"threshold":0.45}' \
    "$(printf '%s' "$QUERY" | jq -Rs .)" \
    "$(printf '%s' "$DB_PATH" | jq -Rs .)")
  RESP=$(printf '%s\n' "$REQ" | nc -w2 -U "$SOCK" 2>/dev/null)
  if [ -n "$RESP" ]; then
    HITS=$(printf '%s' "$RESP" | jq -r '
      if .ok and (.results | length) > 0 then
        "## B12 recall (subagent-scoped, top \(.results | length))\n"
        + ([.results[] | "- " + (.preview // .display // "")] | join("\n"))
      else "" end
    ' 2>/dev/null)
  fi
fi

# Cold-daemon fallback: FTS5 substring lookup. No embedding, no scoring —
# just surface 4 recent memories whose content matches the query's key
# tokens. Better than `{}` when the daemon hasn't loaded yet.
if [ -z "$HITS" ] && [ -r "$DB_PATH" ] && command -v sqlite3 >/dev/null 2>&1; then
  # Extract 3-5 meaningful words from QUERY (skip 1-2 char tokens, skip 'agent:' prefix)
  TOKENS=$(printf '%s' "$QUERY" | sed 's/agent:[^ ]* //' | tr -s '[:punct:][:space:]' ' ' \
            | awk '{for(i=1;i<=NF;i++)if(length($i)>2)print $i}' | head -5 | tr '\n' ' ')
  if [ -n "$(printf '%s' "$TOKENS" | tr -d ' ')" ]; then
    # FTS5 OR-match — broad recall in cold path.
    FTS_QUERY=$(printf '%s' "$TOKENS" | awk '{for(i=1;i<=NF;i++)printf "\"%s\"%s",$i,(i<NF?" OR ":"")}')
    FTS_RES=$(sqlite3 "$DB_PATH" \
      "SELECT '[' || m.memory_type || '] ' || substr(replace(m.content, char(10), ' '), 1, 200)
       FROM memory_content_fts fts JOIN memories m ON m.id = fts.rowid
       WHERE fts.content MATCH '$FTS_QUERY' AND m.deleted_at IS NULL
         AND m.memory_type NOT IN ('session_summary', 'progress')
       ORDER BY rank LIMIT 4;" 2>/dev/null)
    if [ -n "$FTS_RES" ]; then
      HITS=$(printf '## B12 recall (subagent-scoped, cold-path FTS)\n%s' \
              "$(printf '%s' "$FTS_RES" | sed 's/^/- /')")
    fi
  fi
fi

# Emit additionalContext only when we have something to add — empty
# inject would waste a system-prompt slot.
if [ -n "$HITS" ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"SubagentStart","additionalContext":%s}}\n' \
    "$(printf '%s' "$HITS" | jq -Rs .)"
else
  echo '{}'
fi
