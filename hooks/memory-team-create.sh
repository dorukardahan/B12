#!/bin/bash
# B12 Memory System — TeamCreate PostToolUse Hook (v1, 2026-05-19, PR-B2)
# Writes ~/.B12/state/team-<team_id>.json on every TeamCreate, so that
# each teammate's SessionStart hook can prime itself with the OTHER
# teammates' active goals + initial task brief.
#
# Fires on: PostToolUse with matcher = TeamCreate (experimental agent
# teams API, gated on CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1).
# Output: empty JSON; side-effect only.
# Budget: <500ms (just a file write).

set -o pipefail 2>/dev/null || true

_B12_HOOK_DIR="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
# shellcheck disable=SC1091
. "$_B12_HOOK_DIR/_b12_common.sh" 2>/dev/null || true

( sleep 3 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
case "$TOOL_NAME" in
  TeamCreate) ;;
  *) echo '{}'; exit 0 ;;
esac

B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
STATE_DIR="$B12_BASE/state"
mkdir -p "$STATE_DIR" 2>/dev/null || true

# Codex review PR #44 round 3 P2: teammate sessions have different
# session_ids than the TeamCreate caller. Capture each member's runtime
# agent_id from tool_response (where Claude Code's teams API assigns
# them) so the SessionStart resolver can match a teammate's
# CLAUDE_CODE_AGENT_ID env directly against a stored agent_id.
# Fallbacks: tool_response.members (preferred, runtime IDs) →
# tool_input.members (creation-time spec, no agent_id yet) →
# tool_input.agents (older shape).
# Field rename: caller_session_id makes it clear this is the lead's
# session, NOT each teammate's.
TEAM_PAYLOAD=$(printf '%s' "$INPUT" | jq -c '
  (.tool_response.members // .tool_input.members // .tool_input.agents // []) as $raw |
  {
    team_id:    (.tool_response.team_id // .tool_input.team_id // .tool_response.id // ""),
    team_name:  (.tool_response.team_name // .tool_input.team_name // .tool_input.name // ""),
    created_at: (now | floor),
    caller_session_id: (.session_id // ""),
    members:    ($raw | map({
                    name:        (.name // ""),
                    agent_id:    (.agent_id // .id // ""),
                    agent_type:  (.agent_type // .type // ""),
                    task:        (.task // .description // .prompt // "")
                }))
  }
' 2>/dev/null)

TEAM_ID=$(printf '%s' "$TEAM_PAYLOAD" | jq -r '.team_id // ""' 2>/dev/null)
if [ -z "$TEAM_ID" ] || [ "$TEAM_ID" = "null" ]; then
  # Cannot identify team — bail silently, no file write.
  echo '{}'
  exit 0
fi

# Safety: only basic chars in filename. Reject anything weird.
case "$TEAM_ID" in
  *[!A-Za-z0-9_-]*) echo '{}'; exit 0 ;;
esac

printf '%s\n' "$TEAM_PAYLOAD" >"$STATE_DIR/team-${TEAM_ID}.json" 2>/dev/null || true
echo '{}'
